"""MCP 服务器：向 AI 客户端（如 IDE 助手）暴露"仅提案"控制子集。

有意做成只读/仅提交：不能创建会话、审批、拒绝或执行仿真；
真正的控制权始终留在面向人工的 REST 控制面上。
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server import MCPServer
from pydantic import BaseModel

from simlab.config import ProjectConfig
from simlab.policy import PolicyLimits, allowed_actions, validate_workload
from simlab.service import SimulationControlService


class _RemoteControlBackend:
    """Restricted REST client used by the standalone MCP process.

    It intentionally implements no session creation, run, approval, or rejection
    methods. Those operations remain on the human-facing REST control plane.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None,
        client: httpx.Client | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = client or httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=30.0
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self.client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as error:
            try:
                detail = error.response.json().get("detail", error.response.text)
            except (TypeError, ValueError):
                detail = error.response.text
            raise RuntimeError(
                f"SimLab API returned HTTP {error.response.status_code}: {detail}"
            ) from error
        except httpx.HTTPError as error:
            raise RuntimeError(f"cannot reach SimLab API: {error}") from error

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/sessions/{session_id}")

    def get_allowed_actions(self, session_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/sessions/{session_id}/allowed-actions")

    def submit_action(
        self,
        session_id: str,
        action: dict[str, Any],
        *,
        operation_id: str,
        expected_version: int,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/sessions/{session_id}/proposals",
            json={
                "action": action,
                "operation_id": operation_id,
                "expected_version": expected_version,
            },
        )

    def list_events(self, session_id: str, after_sequence: int) -> list[dict[str, Any]]:
        audit = self._request("GET", f"/v1/sessions/{session_id}/audit")
        return [event for event in audit["events"] if int(event["sequence"]) > after_sequence]


def _json_value(value: Any) -> Any:
    # 递归把 BaseModel 转为 JSON 兼容数据，供 MCP 工具返回值使用。
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def create_mcp_server(
    service: SimulationControlService | None = None,
    *,
    api_url: str | None = None,
    api_token: str | None = None,
    http_client: httpx.Client | None = None,
) -> MCPServer:
    """Create a proposal-only MCP facade over the human REST control plane.

    Tests or embedded deployments may inject the exact service used by FastAPI.
    The standalone command instead calls the configured REST API, ensuring both
    surfaces see the same sessions, versions, proposals, and audit events.
    """

    control: SimulationControlService | _RemoteControlBackend
    if service is not None:
        control = service
        limits = service.limits
    else:
        control = _RemoteControlBackend(
            api_url or os.getenv("SIMLAB_API_URL", "http://127.0.0.1:8000"),
            api_token if api_token is not None else os.getenv("SIMLAB_API_TOKEN"),
            client=http_client,
        )
        limits = PolicyLimits()

    server = MCPServer("SimPy KPI Lab")

    @server.tool()
    def validate_project(config: dict[str, Any]) -> dict[str, Any]:
        """Validate a project and return its exact declarative action catalog."""

        parsed = ProjectConfig.model_validate(config)
        validate_workload(parsed, limits)
        return {
            "valid": True,
            "project_name": parsed.project_name,
            "allowed_actions": [
                action.model_dump(mode="json") for action in allowed_actions(parsed)
            ],
        }

    @server.tool()
    def get_session(session_id: str) -> dict[str, Any]:
        """Read a human-created session and its current workflow version."""

        return _json_value(control.get_session(session_id))

    @server.tool()
    def get_allowed_actions(session_id: str) -> list[dict[str, Any]]:
        """Read exact action/target pairs allowed for the current config."""

        return _json_value(control.get_allowed_actions(session_id))

    @server.tool()
    def submit_proposal(
        session_id: str,
        action: dict[str, Any],
        operation_id: str,
        expected_version: int,
    ) -> dict[str, Any]:
        """Submit inert allowlisted data for later human approval.

        This tool cannot create sessions, approve, reject, run experiments,
        execute code, access files, or mutate a running SimPy environment.
        """

        if isinstance(control, SimulationControlService):
            proposal = control.submit_action(
                session_id,
                action,
                operation_id=operation_id,
                expected_version=expected_version,
                actor="mcp-client",
            )
        else:
            proposal = control.submit_action(
                session_id,
                action,
                operation_id=operation_id,
                expected_version=expected_version,
            )
        return _json_value(proposal)

    @server.tool()
    def list_events(session_id: str, after_sequence: int = 0) -> list[dict[str, Any]]:
        """Read ordered control events for audit or polling."""

        return _json_value(control.list_events(session_id, after_sequence))

    return server


# 模块级单例，供 `mcp` 命令入口与外部进程加载。
mcp = create_mcp_server()


def main() -> None:
    """独立进程入口：启动 MCP stdio 服务器。"""

    mcp.run()


if __name__ == "__main__":
    main()


__all__ = ["create_mcp_server", "main", "mcp"]
