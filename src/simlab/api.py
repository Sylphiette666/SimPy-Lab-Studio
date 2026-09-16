"""FastAPI 控制面：把控制服务暴露为版本化的 REST/WebSocket API。

所有写操作都携带 operation_id（幂等键）与 expected_version（乐观锁）；
AI 生成的内容始终是待审批提案，绝不直接执行。
"""
import asyncio
import os
import secrets
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from simlab import __version__
from simlab.agent import OpenAIProposalAgent, ProposalGenerationError
from simlab.config import ProjectConfig
from simlab.policy import ActionPolicyError
from simlab.service import (
    ControlServiceError,
    OperationInProgress,
    RunExecutionError,
    RunNotFound,
    SessionNotFound,
    SimulationControlService,
    StaleProposal,
)
from simlab.workflow import (
    Action,
    IdempotencyConflict,
    InvalidTransition,
    ProposalNotFound,
    VersionConflict,
)


class _RequestModel(BaseModel):
    # 所有请求模型拒绝未知字段，防止客户端注入未定义参数。
    model_config = ConfigDict(extra="forbid")


OperationId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=90,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]*$",
    ),
]
SessionId = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"),
]


class CreateSessionRequest(_RequestModel):
    """创建控制会话的请求体。"""

    config: ProjectConfig
    session_id: SessionId | None = None


class ManualProposalRequest(_RequestModel):
    """人工提交单个动作提案的请求体。"""

    action: Action
    operation_id: OperationId
    expected_version: int = Field(ge=0)


class AIProposalRequest(_RequestModel):
    """请求 AI 生成动作提案的请求体。"""

    operation_id: OperationId
    expected_version: int = Field(ge=0)
    objective: str | None = Field(default=None, max_length=4000)


class DecisionRequest(_RequestModel):
    """审批（同意/拒绝）提案的请求体。"""

    operation_id: OperationId
    expected_version: int = Field(ge=0)
    reason: str | None = Field(default=None, max_length=2000)


class RunRequest(_RequestModel):
    """启动一轮不可变仿真运行的请求体。"""

    operation_id: OperationId
    expected_version: int = Field(ge=0)
    workers: int = Field(default=1, ge=1, le=16)


AgentFactory = Callable[[ProjectConfig], OpenAIProposalAgent]


def create_app(
    *,
    service: SimulationControlService | None = None,
    agent_factory: AgentFactory | None = None,
    api_token: str | None = None,
) -> FastAPI:
    """构建 FastAPI 应用；service 与 agent_factory 可注入以便测试。"""

    control = service or SimulationControlService(
        output_root=os.getenv("SIMLAB_API_OUTPUT_ROOT", "outputs/api_sessions")
    )
    configured_token = api_token if api_token is not None else os.getenv("SIMLAB_API_TOKEN")

    def build_agent(config: ProjectConfig) -> OpenAIProposalAgent:
        if agent_factory is not None:
            return agent_factory(config)
        settings = config.openai
        return OpenAIProposalAgent(
            model=settings.model,
            max_output_tokens=min(settings.max_output_tokens, 4000),
            timeout_seconds=settings.timeout_seconds,
            max_retries=settings.max_retries,
            store=settings.store,
            base_url=settings.base_url,
            api_key_env=settings.api_key_env,
        )

    def require_auth(
        authorization: Annotated[str | None, Header()] = None,
    ) -> str:
        if not configured_token:
            return "local-user"
        scheme, _, credential = (authorization or "").partition(" ")
        # 恒定时间比较令牌，避免时序侧信道。
        if scheme.lower() != "bearer" or not secrets.compare_digest(credential, configured_token):
            raise HTTPException(status_code=401, detail="invalid bearer token")
        return "token-admin"

    app = FastAPI(
        title="SimPy KPI Lab Control API",
        version=__version__,
        description=(
            "Versioned, human-in-the-loop control plane for immutable SimPy experiment runs. "
            "AI output is always a pending, allowlisted proposal and never executes code."
        ),
    )
    app.state.control_service = control

    # 领域异常统一映射为 HTTP 状态码：404 不存在、409 冲突、400/502/500 其余。
    @app.exception_handler(SessionNotFound)
    @app.exception_handler(RunNotFound)
    @app.exception_handler(ProposalNotFound)
    async def not_found_handler(_request, error: Exception):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(VersionConflict)
    @app.exception_handler(StaleProposal)
    @app.exception_handler(IdempotencyConflict)
    @app.exception_handler(InvalidTransition)
    @app.exception_handler(OperationInProgress)
    async def conflict_handler(_request, error: Exception):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(ActionPolicyError)
    @app.exception_handler(ValueError)
    async def bad_request_handler(_request, error: Exception):
        return JSONResponse(status_code=400, content={"detail": str(error)})

    @app.exception_handler(ProposalGenerationError)
    async def proposal_error_handler(_request, error: Exception):
        return JSONResponse(status_code=502, content={"detail": str(error)})

    @app.exception_handler(RunExecutionError)
    async def run_error_handler(_request, error: Exception):
        return JSONResponse(status_code=500, content={"detail": str(error)})

    @app.exception_handler(ControlServiceError)
    async def service_conflict_handler(_request, error: Exception):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/v1/sessions", status_code=201, tags=["sessions"])
    def create_session(
        request: CreateSessionRequest,
        _actor: Annotated[str, Depends(require_auth)],
    ):
        return control.create_session(request.config, session_id=request.session_id)

    @app.get("/v1/sessions/{session_id}", tags=["sessions"])
    def get_session(
        session_id: str,
        _actor: Annotated[str, Depends(require_auth)],
    ):
        return control.get_session(session_id)

    @app.get("/v1/sessions/{session_id}/allowed-actions", tags=["proposals"])
    def get_allowed_actions(
        session_id: str,
        _actor: Annotated[str, Depends(require_auth)],
    ):
        return control.get_allowed_actions(session_id)

    @app.post("/v1/sessions/{session_id}/proposals", status_code=201, tags=["proposals"])
    def create_manual_proposal(
        session_id: str,
        request: ManualProposalRequest,
        actor: Annotated[str, Depends(require_auth)],
    ):
        return control.submit_action(
            session_id,
            request.action,
            operation_id=request.operation_id,
            expected_version=request.expected_version,
            actor=actor,
        )

    @app.post(
        "/v1/sessions/{session_id}/proposals:generate",
        status_code=201,
        tags=["proposals"],
    )
    async def create_ai_proposal(
        session_id: str,
        request: AIProposalRequest,
        actor: Annotated[str, Depends(require_auth)],
    ):
        session = control.get_session(session_id)
        kpi_summary: Any = []
        if session.run_ids:
            latest = control.get_run(session_id, session.run_ids[-1])
            if latest.result is not None:
                kpi_summary = latest.result.get("summary", [])
        agent = build_agent(session.config)
        return await asyncio.to_thread(
            control.submit_ai_plan,
            session_id,
            agent,
            operation_id=request.operation_id,
            expected_version=request.expected_version,
            objective=request.objective,
            kpi_summary=kpi_summary,
            actor=f"{actor}:openai",
        )

    @app.post(
        "/v1/sessions/{session_id}/proposals/{proposal_id}:approve",
        tags=["approvals"],
    )
    def approve_proposal(
        session_id: str,
        proposal_id: str,
        request: DecisionRequest,
        actor: Annotated[str, Depends(require_auth)],
    ):
        return control.approve_and_apply(
            session_id,
            proposal_id,
            operation_id=request.operation_id,
            expected_version=request.expected_version,
            actor=actor,
            reason=request.reason,
        )

    @app.post(
        "/v1/sessions/{session_id}/proposals/{proposal_id}:reject",
        tags=["approvals"],
    )
    def reject_proposal(
        session_id: str,
        proposal_id: str,
        request: DecisionRequest,
        actor: Annotated[str, Depends(require_auth)],
    ):
        return control.reject(
            session_id,
            proposal_id,
            operation_id=request.operation_id,
            expected_version=request.expected_version,
            actor=actor,
            reason=request.reason,
        )

    @app.post("/v1/sessions/{session_id}/runs", status_code=201, tags=["runs"])
    async def run_session(
        session_id: str,
        request: RunRequest,
        _actor: Annotated[str, Depends(require_auth)],
    ):
        record = await asyncio.to_thread(
            control.run,
            session_id,
            operation_id=request.operation_id,
            expected_version=request.expected_version,
            workers=request.workers,
        )
        return record.model_dump(mode="json", exclude={"result"})

    @app.get("/v1/sessions/{session_id}/runs/{run_id}", tags=["runs"])
    def get_run(
        session_id: str,
        run_id: str,
        _actor: Annotated[str, Depends(require_auth)],
        include_result: bool = Query(default=False),
    ):
        record = control.get_run(session_id, run_id)
        exclude = set() if include_result else {"result"}
        return record.model_dump(mode="json", exclude=exclude)

    @app.get("/v1/sessions/{session_id}/audit", tags=["audit"])
    def get_audit(
        session_id: str,
        _actor: Annotated[str, Depends(require_auth)],
    ):
        return {
            "workflow": control.get_workflow_audit(session_id),
            "events": control.list_events(session_id),
        }

    @app.websocket("/v1/sessions/{session_id}/events")
    async def session_events(
        websocket: WebSocket,
        session_id: str,
        after_sequence: int = Query(default=0, ge=0),
    ) -> None:
        authorization = websocket.headers.get("authorization")
        scheme, _, credential = (authorization or "").partition(" ")
        if configured_token and (
            scheme.lower() != "bearer" or not secrets.compare_digest(credential, configured_token)
        ):
            await websocket.close(code=4401, reason="invalid token")
            return
        try:
            control.get_session(session_id)
        except SessionNotFound:
            await websocket.close(code=4404, reason="session not found")
            return

        await websocket.accept()
        cursor = after_sequence
        idle_ticks = 0
        try:
            while True:
                events = control.list_events(session_id, cursor)
                if events:
                    for event in events:
                        await websocket.send_json(event.model_dump(mode="json"))
                        cursor = event.sequence
                    idle_ticks = 0
                else:
                    idle_ticks += 1
                    # 60 个空闲周期（约 15 秒）发一次心跳，防止代理空闲超时断连。
                    if idle_ticks >= 60:
                        await websocket.send_json({"event": "heartbeat", "after_sequence": cursor})
                        idle_ticks = 0
                await asyncio.sleep(0.25)
        except (WebSocketDisconnect, RuntimeError):
            return

    return app


def run_api(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """启动 uvicorn 服务；uvicorn 属于可选依赖，缺失时给出安装提示。"""

    try:
        import uvicorn
    except ImportError as error:  # pragma: no cover - depends on optional packaging.
        raise RuntimeError(
            'API dependencies are missing; install with pip install -e ".[api]"'
        ) from error
    uvicorn.run("simlab.api:app", host=host, port=port, reload=reload)


# 模块级单例，供 `uvicorn simlab.api:app` 直接引用。
app = create_app()


__all__ = [
    "AIProposalRequest",
    "CreateSessionRequest",
    "DecisionRequest",
    "ManualProposalRequest",
    "RunRequest",
    "app",
    "create_app",
    "run_api",
]
