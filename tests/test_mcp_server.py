from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from simlab.api import create_app
from simlab.config import ProjectConfig
from simlab.mcp_server import create_mcp_server
from simlab.service import SimulationControlService


def config() -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "project_name": "mcp-test",
            "simulation": {
                "until": 10,
                "arrival_interarrival": {"kind": "deterministic", "value": 2},
                "stations": [
                    {
                        "name": "desk",
                        "service_time": {"kind": "deterministic", "value": 1},
                    }
                ],
            },
            "experiment": {"replications": 2},
        }
    )


def test_mcp_exposes_proposal_tools_but_not_human_decisions(tmp_path: Path) -> None:
    server = create_mcp_server(SimulationControlService(output_root=tmp_path))
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}

    assert "validate_project" in names
    assert "submit_proposal" in names
    assert "get_allowed_actions" in names
    assert "create_session" not in names
    assert "run_current_config" not in names
    assert "approve" not in names
    assert "reject" not in names


def test_mcp_validates_config_and_returns_structured_allowlist(tmp_path: Path) -> None:
    server = create_mcp_server(SimulationControlService(output_root=tmp_path))
    result = asyncio.run(
        server.call_tool(
            "validate_project",
            {"config": config().model_dump(mode="json")},
        )
    )

    assert result.is_error is False
    assert result.structured_content["valid"] is True
    assert result.structured_content["project_name"] == "mcp-test"
    pairs = {
        (item["action_type"], item["target"])
        for item in result.structured_content["allowed_actions"]
    }
    assert ("set_parameter", "simulation.stations.0.capacity") in pairs


def test_mcp_can_only_create_pending_proposal_in_existing_session(tmp_path: Path) -> None:
    service = SimulationControlService(output_root=tmp_path)
    session = service.create_session(config(), session_id="mcp-existing")
    server = create_mcp_server(service)

    result = asyncio.run(
        server.call_tool(
            "submit_proposal",
            {
                "session_id": session.session_id,
                "operation_id": "mcp-proposal-1",
                "expected_version": 0,
                "action": {
                    "action": "set_parameter",
                    "path": "simulation.stations.0.capacity",
                    "value": 2,
                },
            },
        )
    )

    assert result.is_error is False
    assert result.structured_content["status"] == "pending"
    current = service.get_session(session.session_id)
    assert current.config.simulation.stations[0].capacity == 1


def test_standalone_mcp_uses_the_rest_service_session(tmp_path: Path) -> None:
    service = SimulationControlService(output_root=tmp_path)
    app = create_app(service=service, api_token="shared-secret")

    with TestClient(
        app,
        headers={"Authorization": "Bearer shared-secret"},
    ) as api_client:
        created = api_client.post(
            "/v1/sessions",
            json={
                "session_id": "shared-session",
                "config": config().model_dump(mode="json"),
            },
        )
        assert created.status_code == 201
        server = create_mcp_server(
            api_url="http://testserver",
            api_token="shared-secret",
            http_client=api_client,
        )
        result = asyncio.run(
            server.call_tool(
                "submit_proposal",
                {
                    "session_id": "shared-session",
                    "operation_id": "remote-mcp-proposal",
                    "expected_version": 0,
                    "action": {
                        "action": "set_parameter",
                        "path": "experiment.replications",
                        "value": 3,
                    },
                },
            )
        )
        current = api_client.get("/v1/sessions/shared-session")

    assert result.is_error is False
    assert result.structured_content["status"] == "pending"
    assert current.json()["workflow"]["proposals"][0]["status"] == "pending"
