"""Durable, bounded parameter scans with cooperative cancellation between replications."""

from __future__ import annotations

import copy
import itertools
import json
import math
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from fastapi import HTTPException
from pydantic import ValidationError

from simlab.experiment import aggregate, set_dotted_value
from simlab.live_manufacturing import _replication_seed
from simlab.manufacturing import ManufacturingConfig, _metric_catalog, run_manufacturing_replication
from simlab.studio_statistics import METRICS
from simlab.studio_workspace import atomic_json


def numeric_parameters(config):
    items = []
    for kind in ("machines", "buffers"):
        for index, item in enumerate(config[kind]):
            for key, value in item.items():
                if key != "name":
                    items.append(
                        {
                            "path": f"{kind}.{index}.{key}",
                            "name": item["name"],
                            "field": key,
                            "value": value,
                        }
                    )
    return items


class Batches:
    def __init__(self, store, validate):
        self.store, self.validate = store, validate
        self.folder = store.root / "batches"
        self.folder.mkdir(exist_ok=True)
        self.jobs = {}
        self.closing = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio-batch")
        for path in self.folder.glob("*.json"):
            try:
                job = json.loads(path.read_text("utf-8"))
                if str(uuid.UUID(job["id"])) != path.stem:
                    continue
                if job["status"] in {"queued", "running", "cancelling"}:
                    job.update(
                        status="interrupted", error="软件关闭中断了任务，已完成的方案仍保留。"
                    )
                    atomic_json(path, job)
                self.jobs[job["id"]] = job
            except (ValueError, KeyError, TypeError, OSError):
                continue

    def close(self):
        self.closing.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def save(self, job):
        atomic_json(self.folder / (job["id"] + ".json"), job)

    def public(self, job, detail=True):
        data = copy.deepcopy(job)
        for row in data["scenarios"]:
            row.pop("replications", None)
            if not detail:
                row.pop("config", None)
        return data

    def get(self, sid, jid):
        self.store.get(sid)
        job = self.jobs.get(jid)
        if not job or job["session_id"] != sid:
            raise HTTPException(404, "批量任务不存在。")
        return job

    def start(self, sid, body):
        with self.store.lock:
            session = self.store.get(sid)
            if body.request_id in self.jobs:
                job = self.get(sid, body.request_id)
                if job["request"] != body.model_dump(mode="json"):
                    raise HTTPException(409, "同一请求编号不能用于不同参数。")
                return self.public(job)
            if (
                sum(j["status"] in {"queued", "running", "cancelling"} for j in self.jobs.values())
                >= 2
            ):
                raise HTTPException(429, "最多同时保留 2 个运行或排队的批量任务。")
            version = self.store.head(session, body.expected_version_id)
            allowed = {p["path"] for p in numeric_parameters(version["config"])}
            if not set(body.grid).issubset(allowed):
                raise HTTPException(422, "只能扫描当前设备与容器的数值参数。")
            count = math.prod(len(values) for values in body.grid.values())
            if not 1 <= count <= 16:
                raise HTTPException(422, "每批限 1–16 个参数组合。")
            scenarios, effort = [], 0
            for index, values in enumerate(itertools.product(*body.grid.values())):
                config = copy.deepcopy(version["config"])
                parameters = dict(zip(body.grid, values, strict=True))
                for path, value in parameters.items():
                    if path.endswith(".capacity"):
                        if not float(value).is_integer():
                            raise HTTPException(422, "缓冲容量必须为整数。")
                        value = int(value)
                    set_dotted_value(config, path, value)
                try:
                    validated = ManufacturingConfig.model_validate(config)
                    self.validate(validated, version["mode"])
                except ValidationError as exc:
                    raise HTTPException(
                        422, f"第 {index + 1} 组参数未通过校验，请检查范围和关联约束。"
                    ) from exc
                effort += (
                    validated.until_seconds
                    * validated.replications
                    * sum(
                        1 / m.cycle_time_seconds + (1 / m.mttf_seconds if m.availability < 1 else 0)
                        for m in validated.machines
                    )
                )
                scenarios.append(
                    {
                        "index": index,
                        "parameters": parameters,
                        "config": validated.model_dump(mode="json"),
                        "status": "queued",
                    }
                )
            if effort > 8_000_000:
                raise HTTPException(422, "整批预计事件量过大，请减少参数组合、时长或重复次数。")
            job = {
                "id": body.request_id,
                "session_id": sid,
                "base_version_id": version["id"],
                "mode": version["mode"],
                "request": body.model_dump(mode="json"),
                "status": "queued",
                "created_at": datetime.now(UTC).isoformat(),
                "scenarios": scenarios,
                "completed_replications": 0,
                "total_replications": count * validated.replications,
                "cancel_requested": False,
            }
            self.save(job)
            self.jobs[job["id"]] = job
            self.pool.submit(self.run, job)
            return self.public(job)

    def run(self, job):
        try:
            with self.store.lock:
                job["status"] = "running"
                self.save(job)
            for row in job["scenarios"]:
                config = ManufacturingConfig.model_validate(row["config"])
                records = []
                for replication in range(config.replications):
                    with self.store.lock:
                        if self.closing.is_set() or job["cancel_requested"]:
                            job["status"] = "interrupted" if self.closing.is_set() else "cancelled"
                            row["status"] = "cancelled"
                            self.save(job)
                            return
                        row["status"] = "running"
                    records.append(
                        run_manufacturing_replication(
                            config,
                            _replication_seed(config, replication),
                            replication=replication,
                            scenario="current",
                        )
                    )
                    with self.store.lock:
                        job["completed_replications"] += 1
                        self.save(job)
                summary = aggregate(records, config.confidence_level, _metric_catalog(config))
                means = {m["metric"]: m["mean"] for m in summary if m["metric"] in METRICS}
                targets = job["request"]["targets"]
                score = (
                    (sum(((means[k] - v) / v) ** 2 for k, v in targets.items()) / len(targets))
                    ** 0.5
                    if targets and all(means.get(k) is not None for k in targets)
                    else None
                )
                with self.store.lock:
                    row.update(
                        status="succeeded",
                        summary=summary,
                        means=means,
                        calibration_error=score,
                        replications=records,
                    )
                    self.save(job)
            with self.store.lock:
                job["status"] = "succeeded"
                self.save(job)
        except Exception:
            with self.store.lock:
                job.update(
                    status="failed", error="批量实验未完成；已完成结果保留，请检查参数或缩小规模。"
                )
                self.save(job)
