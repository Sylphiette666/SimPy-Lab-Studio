"""Port-independent drafts and experiment-only backups. Credentials are excluded."""

from __future__ import annotations

import copy
import io
import json
import shutil
import threading
import time
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException

from simlab.manufacturing import ManufacturingConfig


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temp.replace(path)


class Workspace:
    def __init__(self, store, sanitize):
        self.store, self.sanitize = store, sanitize
        self.path = store.root / "workspace.json"
        try:
            self.data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            self.data = {}
        if not isinstance(self.data, dict):
            self.data = {}
        self.batches = None
        self.data.setdefault("automatic_backup", False)
        self.stopped = threading.Event()
        self.worker = None

    def start(self):
        self.worker = threading.Thread(target=self._loop, daemon=True, name="studio-backup")
        self.worker.start()

    def close(self):
        self.stopped.set()
        if self.worker:
            self.worker.join(timeout=5)

    def _loop(self):
        while not self.stopped.wait(60):
            try:
                with self.store.lock:
                    due = (
                        self.data.get("automatic_backup")
                        and time.time() - self.data.get("last_backup_at", 0) >= 86400
                    )
                if due:
                    self.backup()
            except Exception:
                with self.store.lock:
                    self.data["backup_error"] = "自动备份未完成，请检查磁盘空间并手动备份。"

    def drafts(self, session_id):
        self.store.get(session_id)
        try:
            data = json.loads((self.store.root / session_id / "drafts.json").read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def write_draft(self, session_id, kind, value, revision):
        if kind not in {"input", "graph", "layout"}:
            raise HTTPException(422, "未知草稿类型。")
        with self.store.lock:
            drafts = self.drafts(session_id)
            old = drafts.get(kind, {"revision": 0})
            if revision != old["revision"]:
                raise HTTPException(
                    409, "另一窗口已更新草稿，本窗口输入仍保留；请导出或重新载入后核对。"
                )
            if value is not None:
                try:
                    json.loads(value)
                except ValueError as exc:
                    raise HTTPException(422, "草稿格式无效。") from exc
                value = self.sanitize(value)
            drafts[kind] = {
                "revision": revision + 1,
                "value": value,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            atomic_json(self.store.root / session_id / "drafts.json", drafts)
            return drafts[kind]

    def settings(self):
        with self.store.lock:
            result = copy.deepcopy(self.data)
            sid = result.get("last_session_id")
            result["drafts"] = self.drafts(sid) if sid in self.store.sessions else {}
            return result

    def configure(self, updates):
        with self.store.lock:
            if updates.get("last_session_id"):
                self.store.get(updates["last_session_id"])
            self.data.update(updates)
            atomic_json(self.path, self.data)
            return copy.deepcopy(self.data)

    def archive_bytes(self, session_ids=None):
        stream = io.BytesIO()
        with self.store.lock, zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("backup.json", json.dumps({"schema": "simlab-backup-1"}))
            for sid in session_ids if session_ids is not None else self.store.sessions:
                session = self.store.get(sid)
                prefix = f"sessions/{sid}/"
                archive.writestr(prefix + "session.json", self.sanitize(json.dumps(session)))
                for run in session["runs"]:
                    result = self.store.run_result(sid, run)
                    if result.get("result") is not None:
                        archive.writestr(
                            prefix + f"{run['id']}.json",
                            self.sanitize(json.dumps(result["result"])),
                        )
                archive.writestr(
                    prefix + "drafts.json", self.sanitize(json.dumps(self.drafts(sid)))
                )
                if self.batches:
                    for job in self.batches.jobs.values():
                        if job["session_id"] == sid:
                            archive.writestr(
                                prefix + f"batches/{job['id']}.json", self.sanitize(json.dumps(job))
                            )
        return stream.getvalue()

    def backup(self):
        data = self.archive_bytes()
        with self.store.lock:
            folder = self.store.root / "backups"
            folder.mkdir(exist_ok=True)
            name = (
                datetime.now(UTC).strftime("backup-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8] + ".zip"
            )
            path = folder / name
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(path)
            self.data.update(last_backup_at=time.time(), backup_error=None)
            atomic_json(self.path, self.data)
            for old in sorted(folder.glob("backup-*.zip"), key=lambda p: p.stat().st_mtime)[:-7]:
                old.unlink()
            return {"name": name, "bytes": len(data)}

    def backups(self):
        return [
            {
                "name": p.name,
                "bytes": p.stat().st_size,
                "created_at": datetime.fromtimestamp(p.stat().st_mtime, UTC).isoformat(),
            }
            for p in sorted((self.store.root / "backups").glob("backup-*.zip"), reverse=True)
        ]

    def read_backup(self, name):
        if name not in {item["name"] for item in self.backups()}:
            raise HTTPException(404, "备份不存在。")
        return (self.store.root / "backups" / name).read_bytes()

    def inspect_archive(self, data):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if (
                    len(entries) > 5000
                    or sum(i.file_size for i in entries) > 100_000_000
                    or len({i.filename for i in entries}) != len(entries)
                ):
                    raise ValueError("oversized or duplicate archive entries")
                names = archive.namelist()
                if any(name.startswith(("/", "\\")) or ".." in name.split("/") for name in names):
                    raise ValueError("invalid archive path")
                session_files = [
                    name
                    for name in names
                    if name == "session.json"
                    or (name.startswith("sessions/") and name.endswith("/session.json"))
                ]
                if not session_files or len(session_files) > 100:
                    raise ValueError("missing sessions")
                prepared = []
                for filename in session_files:
                    original = json.loads(archive.read(filename))
                    # Only known experiment fields are restored; never settings or executable files.
                    session = {
                        key: copy.deepcopy(original[key])
                        for key in (
                            "id",
                            "created_at",
                            "active_version_id",
                            "versions",
                            "messages",
                            "runs",
                        )
                    }
                    if not isinstance(session["created_at"], str):
                        raise ValueError("invalid timestamp")
                    if not 1 <= len(session["versions"]) <= 1000 or len(session["runs"]) > 2000:
                        raise ValueError("too many versions or runs")
                    ids = set()
                    for version in session["versions"]:
                        if str(uuid.UUID(version["id"])) != version["id"] or version["id"] in ids:
                            raise ValueError("invalid version identifiers")
                        ids.add(version["id"])
                        config = ManufacturingConfig.model_validate(version["config"])
                        if len(config.machines) > 12 or version["mode"] not in {"paper", "custom"}:
                            raise ValueError("invalid model")
                    if session["active_version_id"] not in ids or not isinstance(
                        session["messages"], list
                    ):
                        raise ValueError("invalid active version or messages")
                    for message in session["messages"]:
                        if (
                            not isinstance(message, dict)
                            or message.get("role") not in {"user", "assistant"}
                            or not isinstance(message.get("content"), str)
                        ):
                            raise ValueError("invalid message")
                    prefix = filename.removesuffix("session.json")
                    runs, run_ids = {}, set()
                    for run in session["runs"]:
                        rid = run["id"]
                        if (
                            str(uuid.UUID(rid)) != rid
                            or rid in run_ids
                            or run["version_id"] not in ids
                            or run["kind"] not in {"preview", "study"}
                        ):
                            raise ValueError("invalid run")
                        run_ids.add(rid)
                        run["result"] = None
                        result_path = prefix + rid + ".json" if prefix else f"runs/{rid}.json"
                        if run["status"] == "succeeded" and result_path in names:
                            result = json.loads(archive.read(result_path))
                            result = result if prefix else result.get("result")
                            if not isinstance(result, dict):
                                raise ValueError("invalid result")
                            runs[rid] = result
                        else:
                            run.update(
                                status="failed", error="导入时原任务未完成或缺少结果，请重新运行。"
                            )
                    new_id = str(uuid.uuid4())
                    session["id"] = new_id
                    session["display_name"] = (
                        str(
                            original.get("display_name") or session["versions"][0]["config"]["name"]
                        )[:150]
                        + "（恢复）"
                    )
                    session["tags"] = [str(t)[:24] for t in original.get("tags", [])[:10]]
                    session["archived"] = bool(original.get("archived", False))
                    # Unapplied drafts retain old base IDs; remap only their session context.
                    drafts = {}
                    if prefix + "drafts.json" in names:
                        incoming = json.loads(archive.read(prefix + "drafts.json"))
                        for kind in ("input", "graph", "layout"):
                            item = incoming.get(kind)
                            if item and isinstance(item.get("value"), str):
                                value = json.loads(item["value"])
                                if kind == "graph" and isinstance(value, dict):
                                    value["contextKey"] = (
                                        new_id
                                        + "/"
                                        + str(value.get("contextKey", "")).split("/")[-1]
                                    )
                                drafts[kind] = {
                                    "revision": 1,
                                    "value": self.sanitize(json.dumps(value)),
                                }
                    restored_jobs = []
                    for name in names:
                        if (
                            prefix
                            and name.startswith(prefix + "batches/")
                            and name.endswith(".json")
                        ):
                            job = json.loads(archive.read(name))
                            if job["base_version_id"] not in ids or len(job["scenarios"]) > 16:
                                raise ValueError("invalid batch")
                            for row in job["scenarios"]:
                                ManufacturingConfig.model_validate(row["config"])
                            job["id"], job["session_id"] = str(uuid.uuid4()), new_id
                            job["request"]["request_id"] = job["id"]
                            if job["status"] in {"queued", "running", "cancelling"}:
                                job.update(
                                    status="interrupted", error="从备份恢复，未完成任务需重新提交。"
                                )
                            restored_jobs.append(job)
                    drafts["_batches"] = restored_jobs
                    prepared.append((json.loads(self.sanitize(json.dumps(session))), runs, drafts))
                return prepared
        except (ValueError, KeyError, TypeError, AttributeError, zipfile.BadZipFile) as exc:
            raise HTTPException(
                422, "实验包无效或超出限制；请使用本软件导出的实验 ZIP 或备份。"
            ) from exc

    def restore(self, prepared):
        # All content is parsed before any write, with fresh IDs and no archive extraction.
        with self.store.lock:
            stage = self.store.root / ("restore-" + uuid.uuid4().hex)
            stage.mkdir()
            moved, job_paths, jobs = [], [], []
            try:
                for session, runs, drafts in prepared:
                    folder = stage / session["id"]
                    atomic_json(folder / "session.json", session)
                    atomic_json(
                        folder / "drafts.json", {k: v for k, v in drafts.items() if k != "_batches"}
                    )
                    jobs.extend(drafts.get("_batches", []))
                    for rid, result in runs.items():
                        atomic_json(folder / f"{rid}.json", result)
                for session, _, _ in prepared:
                    target = self.store.root / session["id"]
                    (stage / session["id"]).rename(target)
                    moved.append(target)
                if self.batches:
                    for job in jobs:
                        path = self.batches.folder / (job["id"] + ".json")
                        atomic_json(path, job)
                        job_paths.append(path)
                for session, _, _ in prepared:
                    self.store.sessions[session["id"]] = session
                if self.batches:
                    self.batches.jobs.update({j["id"]: j for j in jobs})
            except Exception:
                for target in moved:
                    shutil.rmtree(target)
                for path in job_paths:
                    path.unlink()
                raise
            finally:
                shutil.rmtree(stage)
            return {"session_ids": [session["id"] for session, _, _ in prepared]}
