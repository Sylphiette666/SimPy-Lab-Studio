"""Local manufacturing application: durable versions, real simulation and LLM changes."""

from __future__ import annotations

import copy
import csv
import html
import io
import json
import threading
import uuid
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from simlab.experiment import flatten_numeric
from simlab.manufacturing import (
    DAY,
    ManufacturingConfig,
    case_a_config,
    validate_case_a_adaptation,
)
from simlab.studio_ai import (
    AdjustmentProposal,
    AISettings,
    ManufacturingAdjustmentAgent,
    StudioAIError,
    apply_proposal,
    redact_secret,
)
from simlab.studio_profiles import AIProfileStore, ProfileInput

Mode = Literal["paper", "custom"]
ROOT = "/api/studio"
CHINESE_ASSUMPTIONS = [
    "时间单位为秒；仿真第 0 秒为周一 00:00。默认运行 30 天，前 1 天为预热期。",
    "生产线为串行设备和有限中间缓冲区。原料按需供应，原料库和成品不计入在制品。",
    "设备加工中断后从剩余进度继续；故障计时采用累计加工时间，维修期间耗用空闲功率。",
    "产出率按包括休息时段在内的日历时间计算。能耗与在制品指标排除预热期。",
    "论文模式保持案例一设备顺序和缓冲区容量；自定义模式允许另建串行模型。",
    "动画展示真实仿真状态的采样回放，可能跳过两帧之间的短事件；不代表实时工厂数据。",
    "调整生成新版本并从第 0 秒重新仿真。动画暂停只暂停回放，不是保留在制品连续改模。",
    "预览为单次运行；最终评价用多次独立重复及正态近似置信区间。",
    "论文未披露的计时细节采用已记录的实现假设，结果并非精确复现或真实工厂预测。",
]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class NewSession(_Body):
    config: ManufacturingConfig
    mode: Mode = "paper"


class NewVersion(NewSession):
    expected_version_id: str
    label: str = Field(default="手动调整", min_length=1, max_length=100)


class RestoreVersion(_Body):
    version_id: str
    expected_version_id: str


class AdjustModel(_Body):
    prompt: str = Field(min_length=1, max_length=6000)
    expected_version_id: str
    ai_profile_id: str | None = Field(default=None, min_length=1, max_length=36)


class NewRun(_Body):
    version_id: str
    kind: Literal["preview", "study"] = "preview"


class UpdateAI(_Body):
    model: str = Field(min_length=1, max_length=160)
    base_url: str = Field(default="", max_length=1000)
    api_key: str | None = Field(default=None, max_length=4096, repr=False)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def _identifier(value: str) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(404, "未找到该会话、版本或运行。") from exc
    return value


def _validate(config: ManufacturingConfig, mode: Mode) -> None:
    if mode == "paper" and validate_case_a_adaptation(config):
        raise HTTPException(
            422,
            "论文模式必须保留案例一的 4 台设备、顺序及 3 个容量为 5 的缓冲区。"
            "如需改变这些条件，请切换到自定义实验。",
        )
    if len(config.machines) > 12 or len(config.breaks) > 40:
        raise HTTPException(422, "应用最多支持 12 台串行设备和 40 段每周休息时间。")
    if config.until_seconds > 90 * DAY or config.replications > 50:
        raise HTTPException(422, "应用单次实验最多运行 90 天、50 次重复。")
    if not 0.5 <= config.confidence_level <= 0.999:
        raise HTTPException(422, "置信水平应在 0.5 到 0.999 之间。")
    if any(len(m.name) > 80 or m.cycle_time_seconds < 1 for m in config.machines):
        raise HTTPException(422, "设备名最多 80 个字符，加工时间至少为 1 秒。")
    if any(len(b.name) > 80 or b.capacity > 10000 for b in config.buffers):
        raise HTTPException(422, "缓冲区名称最多 80 个字符，容量最多为 10000。")
    if config.raw_buffer_capacity > 100000:
        raise HTTPException(422, "原料缓冲区容量最多为 100000。")
    if any(m.mttf_seconds < 0.1 for m in config.machines):
        raise HTTPException(422, "故障间隔过短，预计事件量超出本机应用范围。")
    if any(max(m.idle_power_kw, m.processing_power_kw) > 1e9 for m in config.machines):
        raise HTTPException(422, "设备功率超过允许范围。")
    # Conservative processing-event estimate; repair rates also need a floor.
    effort = config.until_seconds * sum(1 / m.cycle_time_seconds for m in config.machines)
    failure_effort = sum(
        config.until_seconds / m.mttf_seconds for m in config.machines if m.availability < 1
    )
    if (effort + failure_effort) * config.replications > 8_000_000:
        raise HTTPException(422, "模型预计事件量过大，请缩短仿真时长或减少重复次数。")
    if len(config.name) > 160:
        raise HTTPException(422, "模型名称最多 160 个字符。")


def _differences(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            result.extend(_differences(before.get(key), after.get(key), f"{path}.{key}".strip(".")))
        return result
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        result = []
        for index, (left, right) in enumerate(zip(before, after, strict=True)):
            result.extend(_differences(left, right, f"{path}.{index}"))
        return result
    return [] if before == after else [{"path": path, "before": before, "after": after}]


class _SessionStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.sessions: dict[str, dict[str, Any]] = {}
        self.adjusting: set[str] = set()
        for path in self.root.glob("*/session.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                _identifier(data["id"])
                if path.parent.name != data["id"]:
                    continue
                version_ids = set()
                for version in data["versions"]:
                    version_ids.add(_identifier(version["id"]))
                    ManufacturingConfig.model_validate(version["config"])
                    if version["mode"] not in {"paper", "custom"}:
                        raise ValueError("invalid stored mode")
                if data["active_version_id"] not in version_ids:
                    raise ValueError("invalid stored active version")
                for run in data["runs"]:
                    _identifier(run["id"])
                    if run["version_id"] not in version_ids:
                        raise ValueError("invalid stored run version")
                    if run["status"] in {"queued", "running"}:
                        run.update(status="failed", error="应用重启中断了本次运行，请重新运行。")
                self.save(data)
                self.sessions[data["id"]] = data
            except (OSError, ValueError, KeyError, TypeError, HTTPException):
                # A malformed local file must not prevent opening other sessions.
                continue

    def get(self, session_id: str) -> dict[str, Any]:
        _identifier(session_id)
        if session_id not in self.sessions:
            raise HTTPException(404, "会话不存在。")
        return self.sessions[session_id]

    def save(self, session: dict[str, Any]) -> None:
        folder = self.root / session["id"]
        folder.mkdir(exist_ok=True)
        temp = folder / "session.json.tmp"
        temp.write_text(_json(session), encoding="utf-8")
        temp.replace(folder / "session.json")

    def head(self, session: dict[str, Any], expected: str) -> dict[str, Any]:
        if expected != session["active_version_id"]:
            raise HTTPException(409, "模型已在其他操作中更新，请刷新后重新提交。")
        return self.version(session, expected)

    def version(self, session: dict[str, Any], version_id: str) -> dict[str, Any]:
        _identifier(version_id)
        for version in session["versions"]:
            if version["id"] == version_id:
                return version
        raise HTTPException(404, "模型版本不存在。")

    def append_version(
        self,
        session: dict[str, Any],
        config: dict[str, Any],
        mode: Mode,
        *,
        label: str,
        source: str,
        note: str,
    ) -> dict[str, Any]:
        parent = (
            self.version(session, session["active_version_id"]) if session["versions"] else None
        )
        changes = _differences(parent["config"], config) if parent else []
        if parent and parent["mode"] != mode:
            changes.append({"path": "mode", "before": parent["mode"], "after": mode})
        version = {
            "id": str(uuid.uuid4()),
            "parent_id": parent["id"] if parent else None,
            "label": label,
            "source": source,
            "created_at": _now(),
            "config": config,
            "mode": mode,
            "changes": changes,
            "note": note,
        }
        session["versions"].append(version)
        session["active_version_id"] = version["id"]
        return version

    def run(self, session: dict[str, Any], run_id: str) -> dict[str, Any]:
        _identifier(run_id)
        for run in session["runs"]:
            if run["id"] == run_id:
                return run
        raise HTTPException(404, "仿真运行不存在。")

    def run_result(self, session_id: str, run: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(run)
        result["result"] = None
        if run["status"] == "succeeded":
            path = self.root / session_id / f"{run['id']}.json"
            try:
                result["result"] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                result.update(status="failed", error="运行结果文件缺失或损坏，请重新运行。")
        return result


def _csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    buffer = io.StringIO(newline="")
    fields = list(dict.fromkeys(key for row in rows for key in row))
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        safe = {}
        for key, value in row.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                value = "'" + value
            safe[key] = value
        writer.writerow(safe)
    return "\ufeff" + buffer.getvalue()


def _report(session: dict[str, Any], runs: list[dict[str, Any]]) -> str:
    """Self-contained portable HTML: real results and a simple sampled-state replay."""
    rows = []
    for run in runs:
        result = run.get("result") or {}
        version = next(v for v in session["versions"] if v["id"] == run["version_id"])
        label = html.escape(version["label"])
        if run["kind"] == "study" and run["status"] == "succeeded":
            for metric in result.get("summary", []):
                if metric["metric"] in {
                    "throughput_per_hour",
                    "avg_wip",
                    "specific_energy_kwh_per_part",
                }:
                    rows.append(
                        f"<tr><td>{label}</td><td>{html.escape(metric['metric'])}</td>"
                        f"<td>{metric.get('mean')}</td><td>{metric.get('ci_low')} ~ "
                        f"{metric.get('ci_high')}</td><td>{metric.get('n')}</td></tr>"
                    )
    preview = next(
        (
            r
            for r in reversed(runs)
            if r["kind"] == "preview"
            and r["status"] == "succeeded"
            and r["version_id"] == session["active_version_id"]
        ),
        None,
    )
    replay = preview["result"] if preview else {"frames": []}
    data = json.dumps(replay, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    changes = html.escape(
        _json(
            [
                {
                    "label": v["label"],
                    "mode": v["mode"],
                    "note": v["note"],
                    "changes": v["changes"],
                    **({"ai_config": v["ai_config"]} if "ai_config" in v else {}),
                }
                for v in session["versions"]
            ]
        )
    )
    pending = "" if rows else "<p>尚无完成的重复实验；单次动画不能作为最终统计结论。</p>"
    preview_label = next(
        (v["label"] for v in session["versions"] if preview and v["id"] == preview["version_id"]),
        "",
    )
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>制造仿真实验报告</title>'
        "<style>body{font:16px system-ui;max-width:1050px;margin:40px auto;padding:0 22px;"
        "color:#183142;background:#f6f8fa}h1,h2{color:#0d4d55}table{border-collapse:collapse;"
        "width:100%;background:white}td,th{padding:10px;border:1px solid #d6e1e5;text-align:left}"
        "pre{white-space:pre-wrap;word-break:break-word;background:white;padding:18px}"
        ".machine{display:inline-block;border:2px solid #558a8b;border-radius:12px;padding:14px;"
        "margin:6px;min-width:110px;background:white}.muted{color:#607582}button{padding:8px 16px}"
        "input{width:70%}</style><h1>制造仿真实验报告</h1>"
        f"<p>导出时间：{html.escape(_now())} · 模型版本：{len(session['versions'])}</p>"
        "<p class=muted>各版本从相同初始状态重新运行。动画是实际事件状态采样；"
        "最终指标依据独立重复实验，不能代替真实工厂校准。完整配置、原始运行和 CSV 随包提供。</p>"
        "<h2>实验结果</h2>" + pending + "<table><tr><th>版本</th><th>指标</th><th>均值</th>"
        "<th>置信区间</th><th>有效重复</th></tr>"
        + "".join(rows)
        + "</table><h2>最终选定版本的动画回放</h2>"
        + (
            f"<p>版本：{html.escape(preview_label)}</p>"
            if preview
            else "<p>当前选定版本尚无已完成的动画预览。其他版本的运行仍保存在 runs/ 中。</p>"
        )
        + '<button id="play">播放 / 暂停</button> <input id="seek" type="range" min="0" value="0">'
        '<p id="clock"></p><div id="line"></div><pre id="metrics"></pre>'
        f"<h2>调整记录</h2><pre>{changes}</pre>"
        '<script type="application/json" id="data">' + data + "</script>"
        "<script>const data=JSON.parse(document.getElementById('data').textContent);"
        "const frames=data.frames||[];const seek=document.getElementById('seek');let timer=null;"
        "seek.max=Math.max(0,frames.length-1);function render(){const f=frames[+seek.value];"
        "if(!f)return;document.getElementById('clock').textContent='仿真时间 '+"
        "(f.time_seconds/3600).toFixed(2)+' 小时 · 完成 '+f.completed_total+' 件';"
        "const line=document.getElementById('line');line.replaceChildren();"
        "f.machines.forEach((m,i)=>{const el=document.createElement('div');el.className='machine';"
        "el.textContent=m.name+' · '+m.state;line.append(el);if(f.buffers[i]){const b="
        "document.createElement('span');b.textContent=' → '+f.buffers[i].level+'/'+"
        "f.buffers[i].capacity+' → ';line.append(b)}});document.getElementById('metrics')."
        "textContent=JSON.stringify(f.metrics,null,2)}seek.oninput=render;"
        "document.getElementById('play').onclick=()=>{if(timer){clearInterval(timer);timer=null;"
        "return}if(!frames.length)return;timer=setInterval(()=>{seek.value=(+seek.value+1)%"
        "frames.length;render()},150)};render();</script></html>"
    )


def create_studio_app(
    *,
    output_root: str | Path = "outputs/studio",
    agent_factory: Callable[[AISettings], Any] | None = None,
) -> FastAPI:
    """Create a local application with optional Windows-encrypted credential storage."""
    store = _SessionStore(Path(output_root))
    profiles = AIProfileStore(
        store.root / "ai_profiles.json", AISettings.from_environment(), store.lock
    )
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="simlab-studio")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="SimPy 制造仿真实验室", lifespan=lifespan)
    app.state.store = store

    @app.middleware("http")
    async def local_browser_guard(request: Request, call_next: Callable):
        if request.url.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}:
            return JSONResponse({"detail": "此应用仅接受本机访问。"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            own_origin = f"{request.url.scheme}://{request.url.netloc}"
            if (origin and origin.rstrip("/") != own_origin) or request.headers.get(
                "sec-fetch-site"
            ) == "cross-site":
                return JSONResponse({"detail": "不允许跨站修改本机仿真。"}, status_code=403)
            length = request.headers.get("content-length", "0")
            if not length.isdigit() or int(length) > 2_000_000:
                return JSONResponse({"detail": "请求内容过大。"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith(ROOT):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Avoid FastAPI's default echo of invalid api_key values in validation errors.
        details = [
            {"path": ".".join(str(p) for p in item["loc"]), "message": item["msg"]}
            for item in exc.errors()
        ]
        return JSONResponse({"detail": "输入未通过校验。", "errors": details}, status_code=422)

    @app.get(ROOT + "/bootstrap")
    def bootstrap():
        with store.lock:
            return {
                "template": case_a_config().model_dump(mode="json"),
                "ai": profiles.snapshot().public(injected=agent_factory is not None),
                "assumptions": CHINESE_ASSUMPTIONS,
                "limits": {"machines": 12, "days": 90, "replications": 50},
            }

    @app.put(ROOT + "/settings/ai")
    def configure_ai(body: UpdateAI):
        with store.lock:
            settings = profiles.snapshot()
            try:
                update = ProfileInput(
                    name=settings.profile_name,
                    model=body.model,
                    base_url=body.base_url,
                    api_format=settings.api_format,
                    api_key=body.api_key,
                )
            except ValidationError as exc:
                raise HTTPException(422, "模型名称或 API 地址未通过校验。") from exc
            profiles.update(settings.profile_id, update)
            return profiles.snapshot().public(injected=agent_factory is not None)

    @app.get(ROOT + "/ai/profiles")
    def list_ai_profiles():
        return profiles.catalog(injected=agent_factory is not None)

    @app.post(ROOT + "/ai/profiles", status_code=201)
    def create_ai_profile(body: ProfileInput):
        with store.lock:
            profiles.create(body)
            return profiles.catalog(injected=agent_factory is not None)

    @app.put(ROOT + "/ai/profiles/{profile_id}")
    def update_ai_profile(profile_id: str, body: ProfileInput):
        with store.lock:
            profiles.update(profile_id, body)
            return profiles.catalog(injected=agent_factory is not None)

    @app.post(ROOT + "/ai/profiles/{profile_id}/activate")
    def activate_ai_profile(profile_id: str):
        with store.lock:
            profiles.activate(profile_id)
            return profiles.catalog(injected=agent_factory is not None)

    @app.delete(ROOT + "/ai/profiles/{profile_id}")
    def delete_ai_profile(profile_id: str):
        with store.lock:
            profiles.delete(profile_id)
            return profiles.catalog(injected=agent_factory is not None)

    @app.get(ROOT + "/sessions")
    def list_sessions():
        with store.lock:
            return {
                "sessions": [
                    {
                        "id": s["id"],
                        "created_at": s["created_at"],
                        "name": store.version(s, s["active_version_id"])["config"]["name"],
                    }
                    for s in sorted(
                        store.sessions.values(), key=lambda item: item["created_at"], reverse=True
                    )
                ]
            }

    @app.post(ROOT + "/sessions", status_code=201)
    def create_session(body: NewSession):
        _validate(body.config, body.mode)
        with store.lock:
            session = {
                "id": str(uuid.uuid4()),
                "created_at": _now(),
                "active_version_id": None,
                "versions": [],
                "messages": [],
                "runs": [],
            }
            store.append_version(
                session,
                body.config.model_dump(mode="json"),
                body.mode,
                label="初始模型",
                source="initial",
                note="用户选择的初始输入模型。",
            )
            store.sessions[session["id"]] = session
            store.save(session)
            return copy.deepcopy(session)

    @app.get(ROOT + "/sessions/{session_id}")
    def get_session(session_id: str):
        with store.lock:
            return copy.deepcopy(store.get(session_id))

    @app.post(ROOT + "/sessions/{session_id}/versions")
    def create_version(session_id: str, body: NewVersion):
        _validate(body.config, body.mode)
        with store.lock:
            session = store.get(session_id)
            store.head(session, body.expected_version_id)
            store.append_version(
                session,
                body.config.model_dump(mode="json"),
                body.mode,
                label=body.label,
                source="manual",
                note="手动编辑后保存的新模型；重新运行时从第 0 秒开始。",
            )
            store.save(session)
            return copy.deepcopy(session)

    @app.post(ROOT + "/sessions/{session_id}/restore")
    def restore_version(session_id: str, body: RestoreVersion):
        with store.lock:
            session = store.get(session_id)
            store.head(session, body.expected_version_id)
            target = store.version(session, body.version_id)
            store.append_version(
                session,
                copy.deepcopy(target["config"]),
                target["mode"],
                label=f"恢复：{target['label']}"[:100],
                source="restore",
                note=f"恢复自版本 {target['id']}。",
            )
            store.save(session)
            return copy.deepcopy(session)

    def do_run(session_id: str, run_id: str, config: ManufacturingConfig, kind: str) -> None:
        try:
            with store.lock:
                session = store.get(session_id)
                run = store.run(session, run_id)
                run.update(status="running", progress=0.1)
                store.save(session)
            from simlab.live_manufacturing import generate_preview, run_config_study

            if kind == "preview":
                result = generate_preview(
                    config,
                    max_frames=600,
                    preview_seconds=min(config.until_seconds, config.warmup_seconds + DAY),
                )
            else:
                result = run_config_study(config)
            serialized = _json(result)
            with store.lock:
                path = store.root / session_id / f"{run_id}.json"
                temporary = path.with_suffix(".json.tmp")
                temporary.write_text(serialized, encoding="utf-8")
                temporary.replace(path)
                run.update(status="succeeded", progress=1.0, finished_at=_now())
                store.save(session)
        except Exception:
            # Engine/provider details are not safe to echo. Keep previous successful runs intact.
            with store.lock:
                session = store.get(session_id)
                run = store.run(session, run_id)
                run.update(
                    status="failed",
                    error="仿真未能完成，请检查模型参数后重试。",
                    finished_at=_now(),
                )
                store.save(session)

    def enqueue(session: dict[str, Any], version_id: str, kind: str) -> dict[str, Any]:
        version = store.version(session, version_id)
        config = ManufacturingConfig.model_validate(version["config"])
        _validate(config, version["mode"])
        outstanding = sum(
            r["status"] in {"queued", "running"} for s in store.sessions.values() for r in s["runs"]
        )
        if outstanding >= 4:
            raise HTTPException(429, "已有 4 项仿真等待或运行中，请稍后再试。")
        for run in session["runs"]:
            if (
                run["version_id"] == version_id
                and run["kind"] == kind
                and run["status"] in {"queued", "running"}
            ):
                return copy.deepcopy(run)
        run = {
            "id": str(uuid.uuid4()),
            "status": "queued",
            "kind": kind,
            "version_id": version_id,
            "created_at": _now(),
            "result": None,
            "error": None,
            "progress": 0.0,
        }
        session["runs"].append(run)
        store.save(session)
        executor.submit(do_run, session["id"], run["id"], config, kind)
        return copy.deepcopy(run)

    @app.post(ROOT + "/sessions/{session_id}/runs", status_code=202)
    def start_run(session_id: str, body: NewRun):
        with store.lock:
            return enqueue(store.get(session_id), body.version_id, body.kind)

    @app.get(ROOT + "/sessions/{session_id}/runs/{run_id}")
    def get_run(session_id: str, run_id: str):
        with store.lock:
            return store.run_result(session_id, store.run(store.get(session_id), run_id))

    @app.post(ROOT + "/sessions/{session_id}/adjust")
    def adjust_model(session_id: str, body: AdjustModel):
        if not body.prompt.strip():
            raise HTTPException(422, "请输入调整目标。")
        with store.lock:
            session = store.get(session_id)
            version = copy.deepcopy(store.head(session, body.expected_version_id))
            if session_id in store.adjusting:
                raise HTTPException(409, "该会话已有一项 LLM 调整正在生成。")
            ai_settings = profiles.snapshot(body.ai_profile_id)
            if not ai_settings.api_key and agent_factory is None:
                raise HTTPException(503, "请先配置 LLM API 密钥；仿真和手动编辑可以直接使用。")
            context: dict[str, Any] = {"previous_messages": copy.deepcopy(session["messages"][-8:])}
            for run in reversed(session["runs"]):
                if run["version_id"] == version["id"] and run["status"] == "succeeded":
                    result = store.run_result(session_id, run)["result"] or {}
                    if run["kind"] == "study":
                        context["latest_study_summary"] = result.get("summary")
                        break
                    frames = result.get("frames", [])
                    if frames and "single_run_preview_metrics" not in context:
                        context["single_run_preview_metrics"] = frames[-1].get("metrics")
                        context["preview_metadata"] = {
                            key: result.get(key)
                            for key in (
                                "duration_seconds",
                                "configured_duration_seconds",
                                "warmup_seconds",
                                "truncated",
                                "seed",
                            )
                        }
                        context["preview_metadata"]["observation_seconds"] = frames[-1].get(
                            "observation_seconds"
                        )
            store.adjusting.add(session_id)
        try:
            agent = (
                agent_factory(ai_settings)
                if agent_factory
                else ManufacturingAdjustmentAgent(ai_settings)
            )
            proposal = AdjustmentProposal.model_validate(
                agent.propose(
                    config=version["config"],
                    prompt=body.prompt,
                    mode=version["mode"],
                    context=context,
                )
            )
            candidate = ManufacturingConfig.model_validate(
                apply_proposal(version["config"], proposal, version["mode"])
            )
            _validate(candidate, version["mode"])
            with store.lock:
                session = store.get(session_id)
                store.head(session, body.expected_version_id)
                note = redact_secret(proposal.note, ai_settings.api_key)
                session["messages"].append(
                    {
                        "role": "user",
                        "content": redact_secret(body.prompt, ai_settings.api_key),
                        "created_at": _now(),
                        "version_id": version["id"],
                    }
                )
                if not _differences(version["config"], candidate.model_dump(mode="json")):
                    session["messages"].append(
                        {
                            "role": "assistant",
                            "content": note,
                            "created_at": _now(),
                            "version_id": version["id"],
                            "applied": False,
                            "ai_config": ai_settings.audit(),
                        }
                    )
                else:
                    new_version = store.append_version(
                        session,
                        candidate.model_dump(mode="json"),
                        version["mode"],
                        label=f"LLM 调整 {len(session['versions'])}",
                        source="llm",
                        note=note,
                    )
                    new_version["ai_config"] = ai_settings.audit()
                    session["messages"].append(
                        {
                            "role": "assistant",
                            "content": note,
                            "created_at": _now(),
                            "version_id": new_version["id"],
                            "applied": True,
                            "ai_config": ai_settings.audit(),
                        }
                    )
                    # Persist successful model application even when the worker queue is full.
                    store.save(session)
                    try:
                        enqueue(session, new_version["id"], "preview")
                    except HTTPException as exc:
                        if exc.status_code != 429:
                            raise
                        session["messages"][-1]["content"] += " 仿真队列已满，请稍后点击运行预览。"
                store.save(session)
                return copy.deepcopy(session)
        except StudioAIError as exc:
            raise HTTPException(422, str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(422, "LLM 修改未通过模型参数校验，当前模型未更改。") from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                502, "LLM 请求未能完成，当前模型未更改。请检查配置后重试。"
            ) from exc
        finally:
            with store.lock:
                store.adjusting.discard(session_id)

    @app.get(ROOT + "/sessions/{session_id}/export")
    def export_session(session_id: str):
        with store.lock:
            session = copy.deepcopy(store.get(session_id))
            runs = [store.run_result(session_id, run) for run in session["runs"]]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("session.json", _json(session))
            archive.writestr("report.html", _report(session, runs))
            archive.writestr(
                "README.txt",
                "制造仿真实验导出\n直接用浏览器打开 report.html。\n"
                "versions/ 为完整配置与修改记录；runs/ 为实际采样帧与重复实验。\n"
                "未完成的运行仅保存状态。API 密钥不包含在导出中。\n"
                + "\n".join(CHINESE_ASSUMPTIONS),
            )
            for version in session["versions"]:
                archive.writestr(f"versions/{version['id']}.json", _json(version))
            for run in runs:
                prefix = f"runs/{run['id']}"
                archive.writestr(prefix + ".json", _json(run))
                result = run.get("result") or {}
                if run["kind"] == "study" and run["status"] == "succeeded":
                    archive.writestr(prefix + "_summary.csv", _csv(result.get("summary", [])))
                    rows = []
                    for record in result.get("replications", []):
                        rows.append(
                            {
                                "replication": record.get("replication"),
                                "seed": record.get("seed"),
                                **flatten_numeric(record.get("metrics", {})),
                            }
                        )
                    archive.writestr(prefix + "_replications.csv", _csv(rows))
        return Response(
            buffer.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="simlab-session-{session_id}.zip"'
            },
        )

    static = Path(__file__).parent / "static" / "studio"
    if static.is_dir():
        app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        path = static / "index.html"
        if not path.is_file():
            raise HTTPException(503, "应用界面文件缺失，请重新安装包含 studio 静态文件的发行包。")
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    return app
