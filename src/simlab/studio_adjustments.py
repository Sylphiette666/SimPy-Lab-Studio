"""Durable proposals with explicit application and bounded, cancellable waiting."""

from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from fastapi import HTTPException
from pydantic import ValidationError

from simlab.manufacturing import ManufacturingConfig
from simlab.studio_ai import (
    AdjustmentProposal,
    ManufacturingAdjustmentAgent,
    StudioAIError,
    apply_proposal,
    redact_secret,
)


def now():
    return datetime.now(UTC).isoformat()


class AdjustmentService:
    def __init__(self, store, profiles, factory, validate, differences, *, timeout=180):
        self.store, self.profiles, self.factory = store, profiles, factory
        self.validate, self.differences = validate, differences
        self.timeout = timeout
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="studio-proposal")
        self.slots = threading.BoundedSemaphore(4)
        self.timers = {}
        for session in store.sessions.values():
            for job in session.get("adjustments", []):
                if job["status"] in {"queued", "running"}:
                    job.update(status="failed", error="应用已重启，请重新生成方案。")
            store.save(session)

    def close(self):
        with self.store.lock:
            for (session_id, job_id), timer in list(self.timers.items()):
                timer.cancel()
                self.cancel(session_id, job_id)
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _get(self, session_id, job_id):
        session = self.store.get(session_id)
        job = next((item for item in session.get("adjustments", [])
                    if item["id"] == job_id), None)
        if job is None:
            raise HTTPException(404, "调整请求不存在。")
        return session, job

    def get(self, session_id, job_id):
        with self.store.lock:
            return copy.deepcopy(self._get(session_id, job_id)[1])

    def start(self, session_id, body):
        with self.store.lock:
            session = self.store.get(session_id)
            jobs = session.setdefault("adjustments", [])
            # A retry after a lost HTTP response must not consume another provider call.
            existing = next((j for j in jobs if j["id"] == body.request_id), None)
            if existing:
                if existing["expected_version_id"] != body.expected_version_id:
                    raise HTTPException(409, "请求标识已用于另一方案，请重新生成请求。")
                return copy.deepcopy(existing)
            version = copy.deepcopy(self.store.head(session, body.expected_version_id))
            if session_id in self.store.adjusting:
                raise HTTPException(409, "已有调整正在生成，请先取消或等待完成。")
            if any(j["status"] == "ready" for j in jobs):
                raise HTTPException(409, "请先应用或放弃待确认方案。")
            settings = self.profiles.snapshot(body.ai_profile_id)
            if not settings.api_key and self.factory is None:
                raise HTTPException(503, "请先配置 LLM API 密钥。")
            if not body.prompt.strip():
                raise HTTPException(422, "请输入调整目标。")
            context = {"previous_messages": copy.deepcopy(session["messages"][-8:])}
            for run in reversed(session["runs"]):
                if run["version_id"] != version["id"] or run["status"] != "succeeded":
                    continue
                result = self.store.run_result(session_id, run)["result"] or {}
                if run["kind"] == "study":
                    context["latest_study_summary"] = result.get("summary")
                    break
                frames = result.get("frames", [])
                if frames and "single_run_preview_metrics" not in context:
                    context["single_run_preview_metrics"] = frames[-1].get("metrics")
                    context["preview_metadata"] = {
                        key: result.get(key) for key in (
                            "duration_seconds", "configured_duration_seconds", "warmup_seconds",
                            "truncated", "seed",
                        )
                    }
                    context["preview_metadata"]["observation_seconds"] = frames[-1].get(
                        "observation_seconds"
                    )
            if not self.slots.acquire(blocking=False):
                raise HTTPException(429, "调整服务仍在处理先前请求，请稍后重试。")
            job = {
                "id": str(body.request_id), "status": "queued", "created_at": now(),
                "expected_version_id": version["id"],
                "prompt": redact_secret(body.prompt, settings.api_key),
                "ai_config": settings.audit(), "error": None,
            }
            jobs.append(job)
            try:
                self.store.save(session)
            except Exception:
                jobs.remove(job)
                self.slots.release()
                raise
            self.store.adjusting.add(session_id)
            timer = threading.Timer(self.timeout, self._expire, args=(session_id, job["id"]))
            timer.daemon = True
            self.timers[session_id, job["id"]] = timer
            self.executor.submit(self._generate, session_id, job["id"], version, context,
                                 settings, body.prompt)
            timer.start()
            return copy.deepcopy(job)

    def _finish(self, session, job, status, **fields):
        was_running = job["status"] in {"queued", "running"}
        job.update(status=status, finished_at=now(), **fields)
        if was_running:
            self.store.adjusting.discard(session["id"])
        timer = self.timers.pop((session["id"], job["id"]), None)
        if timer:
            timer.cancel()
        if status == "no_changes":
            session["messages"].extend([
                {"role": "user", "content": job["prompt"], "created_at": job["created_at"],
                 "version_id": job["expected_version_id"], "request_id": job["id"]},
                {"role": "assistant", "content": job["note"], "created_at": now(),
                 "version_id": job["expected_version_id"], "applied": False,
                 "ai_config": job["ai_config"], "request_id": job["id"]},
            ])
        self.store.save(session)

    def _expire(self, session_id, job_id):
        with self.store.lock:
            session, job = self._get(session_id, job_id)
            if job["status"] in {"queued", "running"}:
                self._finish(session, job, "failed",
                             error="生成方案超时，可重试；本次结果不会应用。")

    def _generate(self, session_id, job_id, version, context, settings, prompt):
        agent = None
        try:
            with self.store.lock:
                session, job = self._get(session_id, job_id)
                if job["status"] != "queued":
                    return
                job.update(status="running", started_at=now())
                self.store.save(session)
            agent = (self.factory(settings) if self.factory
                     else ManufacturingAdjustmentAgent(settings))
            proposal = AdjustmentProposal.model_validate(agent.propose(
                config=version["config"], prompt=prompt, mode=version["mode"], context=context,
            ))
            candidate = ManufacturingConfig.model_validate(
                apply_proposal(version["config"], proposal, version["mode"])
            )
            self.validate(candidate, version["mode"])
            changes = self.differences(version["config"], candidate.model_dump(mode="json"))
            with self.store.lock:
                session, job = self._get(session_id, job_id)
                # Cancellation and timeout win even if a provider later returns successfully.
                if job["status"] != "running":
                    return
                self.store.head(session, version["id"])
                note = redact_secret(proposal.note, settings.api_key)
                self._finish(session, job, "ready" if changes else "no_changes", note=note,
                             changes=changes, proposal=proposal.model_copy(
                                 update={"note": note}).model_dump(mode="json"))
        except Exception as exc:
            with self.store.lock:
                session, job = self._get(session_id, job_id)
                if job["status"] in {"queued", "running"}:
                    if isinstance(exc, StudioAIError):
                        error = redact_secret(str(exc), settings.api_key)
                    elif isinstance(exc, HTTPException):
                        error = redact_secret(str(exc.detail), settings.api_key)
                    elif isinstance(exc, ValidationError):
                        error = "方案未通过参数校验，请调整目标后重试。"
                    else:
                        error = "生成方案失败，请检查连接后重试。"
                    self._finish(session, job, "failed", error=error)
        finally:
            # Close only clients we own; injected agents may share their test transport.
            try:
                if agent is not None and self.factory is None:
                    agent.client.close()
            finally:
                self.slots.release()

    def cancel(self, session_id, job_id):
        with self.store.lock:
            session, job = self._get(session_id, job_id)
            if job["status"] == "applied":
                raise HTTPException(409, "方案已经应用，可从历史版本恢复。")
            if job["status"] in {"queued", "running", "ready"}:
                # A ready proposal no longer owns the session's in-flight marker.
                self._finish(session, job, "cancelled")
            return copy.deepcopy(job)

    def apply(self, session_id, job_id):
        with self.store.lock:
            session, job = self._get(session_id, job_id)
            if job["status"] == "applied":
                return copy.deepcopy(session)
            if job["status"] != "ready":
                raise HTTPException(409, "此方案已取消、失败或尚未就绪。")
            version = self.store.head(session, job["expected_version_id"])
            try:
                candidate = ManufacturingConfig.model_validate(apply_proposal(
                    version["config"], AdjustmentProposal.model_validate(job["proposal"]),
                    version["mode"],
                ))
            except (StudioAIError, ValidationError):
                raise HTTPException(422, "保存的方案未通过参数校验，请放弃后重新生成。") from None
            self.validate(candidate, version["mode"])
            previous = copy.deepcopy(session)
            new = self.store.append_version(
                session, candidate.model_dump(mode="json"), version["mode"],
                label=f"AI 确认调整 {len(session['versions'])}", source="llm", note=job["note"],
            )
            new["ai_config"] = job["ai_config"]
            session["messages"].extend([
                {"role": "user", "content": job["prompt"], "created_at": job["created_at"],
                 "version_id": version["id"]},
                {"role": "assistant", "content": job["note"], "created_at": now(),
                 "version_id": new["id"], "applied": True, "ai_config": job["ai_config"]},
            ])
            job.update(status="applied", applied_version_id=new["id"], applied_at=now())
            try:
                self.store.save(session)
            except OSError:
                session.clear()
                session.update(previous)
                raise HTTPException(500, "方案保存失败，请检查磁盘后重试。") from None
            return copy.deepcopy(session)
