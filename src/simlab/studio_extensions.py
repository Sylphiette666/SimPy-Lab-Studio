"""Routes for drafts, model tables, diagnostics, batch experiments and backups."""

from __future__ import annotations

import base64
import copy
import math
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException
from fastapi.responses import Response
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from simlab.manufacturing import ManufacturingConfig
from simlab.studio_ai import AISettings, redact_secret
from simlab.studio_batches import Batches, numeric_parameters
from simlab.studio_profiles import ProfileInput
from simlab.studio_statistics import METRICS, analyze_study
from simlab.studio_tables import apply_table, read_table, template_csv
from simlab.studio_workspace import Workspace


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DraftBody(Body):
    value: str | None = Field(default=None, max_length=1_500_000)
    revision: int = Field(ge=0)


class WorkspaceBody(Body):
    last_session_id: str | None = None
    automatic_backup: bool | None = None


class Metadata(Body):
    display_name: str = Field(min_length=1, max_length=160)
    tags: list[str] = Field(default_factory=list, max_length=10)
    archived: bool = False

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, values):
        if any(not v.strip() or len(v) > 24 for v in values):
            raise ValueError("标签须为 1–24 个字符")
        return list(dict.fromkeys(v.strip() for v in values))


class TableBody(Body):
    filename: str = Field(max_length=200)
    data: str = Field(max_length=1_400_000, repr=False)
    config: ManufacturingConfig | None = None
    mode: Literal["paper", "custom"] = "custom"
    mapping: dict[str, str] | None = None


class RestoreBody(Body):
    data: str = Field(max_length=34_000_000, repr=False)
    apply: bool = False


class BatchBody(Body):
    request_id: str
    expected_version_id: str
    grid: dict[str, list[float]] = Field(min_length=1, max_length=3)
    targets: dict[str, float] = Field(default_factory=dict)

    @field_validator("request_id")
    @classmethod
    def identifier(cls, value):
        if str(uuid.UUID(value)) != value:
            raise ValueError("请求编号无效")
        return value

    @field_validator("grid")
    @classmethod
    def grid_values(cls, value):
        if any(not 1 <= len(v) <= 16 or len(v) != len(set(v)) for v in value.values()):
            raise ValueError("每个参数限 1–16 个不重复的取值")
        return value

    @field_validator("targets")
    @classmethod
    def valid_targets(cls, value):
        if not set(value).issubset(METRICS) or any(
            v <= 0 or not math.isfinite(v) for v in value.values()
        ):
            raise ValueError("实际指标须为有限正数")
        return value


class ProbeBody(ProfileInput):
    profile_id: str | None = None


def decode_data(value):
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise HTTPException(422, "文件编码无效，请重新选择文件。") from exc


def probe(settings):
    start = time.monotonic()
    try:
        with OpenAI(
            api_key=settings.api_key, base_url=settings.base_url or None, timeout=15, max_retries=0
        ) as client:
            if settings.uses_responses:
                result = client.responses.create(
                    model=settings.model, input="Reply OK.", max_output_tokens=32, store=False
                )
            else:
                extra = (
                    {"thinking": {"type": "disabled"}}
                    if urlsplit(settings.base_url).hostname == "api.deepseek.com"
                    else {}
                )
                result = client.chat.completions.create(
                    model=settings.model,
                    messages=[{"role": "user", "content": "Reply OK."}],
                    max_tokens=16,
                    **({"extra_body": extra} if extra else {}),
                )
            usage = getattr(result, "usage", None)
            return {
                "ok": True,
                "category": "success",
                "message": "服务已接受此模型和接口协议的测试请求。",
                "latency_ms": round((time.monotonic() - start) * 1000),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
    except APITimeoutError:
        category, message = "timeout", "请求超时，请检查网络或稍后重试。"
    except APIConnectionError:
        category, message = "connection", "无法连接服务，请检查地址、网络和代理设置。"
    except APIStatusError as exc:
        category, message = {
            401: ("authentication", "密钥无效或已失效，请重新填写。"),
            403: ("permission", "服务拒绝访问，请检查账户权限。"),
            404: ("not_found", "地址、模型或接口路径不存在，请核对三项配置。"),
            429: ("rate_limit", "服务限流或账户额度不足，请检查服务商控制台。"),
        }.get(exc.status_code, ("protocol", "服务未接受测试参数，请核对模型与接口协议。"))
    except Exception:
        category, message = "unexpected", "测试未完成，请检查连接配置。"
    return {
        "ok": False,
        "category": category,
        "message": message,
        "latency_ms": round((time.monotonic() - start) * 1000),
    }


@dataclass
class Extensions:
    workspace: Workspace
    batches: Batches

    def start(self):
        self.workspace.start()

    def close(self):
        self.workspace.close()
        self.batches.close()


def install(app, store, profiles, validate, differences, csv_text, connection_probe=None):
    root = "/api/studio"

    def sanitize(value):
        for profile_id in profiles.profiles:
            value = redact_secret(value, profiles.snapshot(profile_id).api_key)
        return value

    workspace, batches = Workspace(store, sanitize), Batches(store, validate)
    workspace.batches = batches
    slots = threading.BoundedSemaphore(2)

    @app.get(root + "/workspace")
    def read_workspace():
        return workspace.settings()

    @app.put(root + "/workspace")
    def write_workspace(body: WorkspaceBody):
        return workspace.configure(body.model_dump(exclude_none=True))

    @app.get(root + "/sessions/{session_id}/drafts")
    def read_drafts(session_id: str):
        with store.lock:
            return workspace.drafts(session_id)

    @app.put(root + "/sessions/{session_id}/drafts/{kind}")
    def write_draft(session_id: str, kind: str, body: DraftBody):
        return workspace.write_draft(session_id, kind, body.value, body.revision)

    @app.put(root + "/sessions/{session_id}/metadata")
    def metadata(session_id: str, body: Metadata):
        with store.lock:
            session = copy.deepcopy(store.get(session_id))
            session.update(body.model_dump())
            store.save(session)
            store.sessions[session_id] = session
            return session

    @app.post(root + "/tables/preview")
    def table_preview(body: TableBody):
        table = read_table(decode_data(body.data), body.filename)
        if body.config is None or body.mapping is None:
            return table
        candidate = apply_table(table, body.config.model_dump(mode="json"), body.mapping)
        try:
            config = ManufacturingConfig.model_validate(candidate)
        except ValidationError as exc:
            errors = [
                {"path": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()
            ]
            raise HTTPException(
                422, {"message": "导入参数不满足模型约束。", "errors": errors}
            ) from exc
        validate(config, body.mode)
        return {
            "config": config.model_dump(mode="json"),
            "changes": differences(body.config.model_dump(mode="json"), candidate),
        }

    @app.post(root + "/tables/template")
    def table_template(config: ManufacturingConfig):
        return Response(
            template_csv(config.model_dump(mode="json")),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="parameters.csv"'},
        )

    @app.post(root + "/ai/test")
    def test_connection(body: ProbeBody):
        with store.lock:
            key = body.api_key or ""
            if body.api_key is None and body.profile_id:
                current = profiles.snapshot(body.profile_id)
                if current.base_url == body.base_url:
                    key = current.api_key
            if not key:
                raise HTTPException(422, "请先填写密钥，或选择同一地址下已配置的密钥。")
            settings = AISettings(
                model=body.model, api_key=key, base_url=body.base_url, api_format=body.api_format
            )
        if not slots.acquire(blocking=False):
            raise HTTPException(429, "已有连接测试进行中，请稍后再试。")
        try:
            return (connection_probe or probe)(settings)
        finally:
            slots.release()

    @app.get(root + "/sessions/{session_id}/analysis/{version_id}")
    def analysis(
        session_id: str, version_id: str, baseline: str | None = None, precision: float = 0.05
    ):
        if not 0.001 <= precision <= 0.5:
            raise HTTPException(422, "目标相对半宽应为 0.1%–50%。")
        with store.lock:
            session = store.get(session_id)
            store.version(session, version_id)

            def study(vid):
                for run in reversed(session["runs"]):
                    if (
                        run["version_id"] == vid
                        and run["kind"] == "study"
                        and run["status"] == "succeeded"
                    ):
                        result = store.run_result(session_id, run)["result"]
                        if result:
                            return result
                raise HTTPException(422, "所选方案尚无可读取的完整重复实验，请先完成评估。")

            selected, control = study(version_id), study(baseline) if baseline else None
        return analyze_study(selected, control, precision)

    @app.get(root + "/sessions/{session_id}/parameters")
    def parameters(session_id: str):
        with store.lock:
            session = store.get(session_id)
            return numeric_parameters(
                store.version(session, session["active_version_id"])["config"]
            )

    @app.post(root + "/sessions/{session_id}/batches", status_code=202)
    def start_batch(session_id: str, body: BatchBody):
        return batches.start(session_id, body)

    @app.get(root + "/sessions/{session_id}/batches")
    def list_batches(session_id: str):
        with store.lock:
            store.get(session_id)
            return [
                batches.public(job, detail=False)
                for job in batches.jobs.values()
                if job["session_id"] == session_id
            ]

    @app.get(root + "/sessions/{session_id}/batches/{job_id}")
    def get_batch(session_id: str, job_id: str):
        with store.lock:
            return batches.public(batches.get(session_id, job_id))

    @app.post(root + "/sessions/{session_id}/batches/{job_id}/cancel")
    def cancel_batch(session_id: str, job_id: str):
        with store.lock:
            job = batches.get(session_id, job_id)
            if job["status"] in {"queued", "running", "cancelling"}:
                job.update(cancel_requested=True, status="cancelling")
                batches.save(job)
            return batches.public(job)

    @app.get(root + "/sessions/{session_id}/batches/{job_id}/export")
    def export_batch(session_id: str, job_id: str):
        with store.lock:
            job = batches.get(session_id, job_id)
            rows = [
                {
                    "scenario": row["index"] + 1,
                    "status": row["status"],
                    **row["parameters"],
                    **row.get("means", {}),
                    "calibration_error": row.get("calibration_error"),
                }
                for row in job["scenarios"]
            ]
            return Response(csv_text(rows), media_type="text/csv")

    @app.get(root + "/backups")
    def list_backups():
        return {"items": workspace.backups(), **workspace.settings()}

    @app.post(root + "/backups")
    def make_backup():
        return workspace.backup()

    @app.get(root + "/backups/{name}")
    def download_backup(name: str):
        return Response(workspace.read_backup(name), media_type="application/zip")

    @app.post(root + "/restore")
    def restore_backup(body: RestoreBody):
        prepared = workspace.inspect_archive(decode_data(body.data))
        if not body.apply:
            return {
                "sessions": [
                    {
                        "name": s["display_name"],
                        "versions": len(s["versions"]),
                        "runs": len(s["runs"]),
                    }
                    for s, _, _ in prepared
                ]
            }
        return workspace.restore(prepared)

    return Extensions(workspace, batches)
