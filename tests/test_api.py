from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from simlab.agent import ActionProposal, ProposedAction
from simlab.api import create_app
from simlab.config import ProjectConfig
from simlab.service import SimulationControlService

TOKEN = "test-secret"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def config() -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "project_name": "api-test",
            "simulation": {
                "until": 12,
                "max_arrivals": 10,
                "arrival_interarrival": {"kind": "deterministic", "value": 2},
                "stations": [
                    {
                        "name": "desk",
                        "capacity": 1,
                        "service_time": {"kind": "deterministic", "value": 1},
                    }
                ],
            },
            "experiment": {"replications": 2, "base_seed": 7},
        }
    )


def client(tmp_path: Path, *, agent_factory=None) -> TestClient:
    service = SimulationControlService(output_root=tmp_path)
    app = create_app(service=service, agent_factory=agent_factory, api_token=TOKEN)
    return TestClient(app)


def create_session(test_client: TestClient, session_id: str = "api-session") -> dict:
    response = test_client.post(
        "/v1/sessions",
        headers=HEADERS,
        json={"session_id": session_id, "config": config().model_dump(mode="json")},
    )
    assert response.status_code == 201
    return response.json()


def test_api_requires_bearer_token_when_configured(tmp_path: Path) -> None:
    with client(tmp_path) as test_client:
        assert test_client.get("/health").status_code == 200
        response = test_client.post(
            "/v1/sessions",
            json={"config": config().model_dump(mode="json")},
        )

    assert response.status_code == 401


def test_duplicate_session_maps_to_conflict(tmp_path: Path) -> None:
    with client(tmp_path) as test_client:
        create_session(test_client, "duplicate-session")
        duplicate = test_client.post(
            "/v1/sessions",
            headers=HEADERS,
            json={
                "session_id": "duplicate-session",
                "config": config().model_dump(mode="json"),
            },
        )

    assert duplicate.status_code == 409


def test_human_approval_flow_and_run(tmp_path: Path) -> None:
    with client(tmp_path) as test_client:
        session = create_session(test_client)
        proposal_response = test_client.post(
            f"/v1/sessions/{session['session_id']}/proposals",
            headers=HEADERS,
            json={
                "operation_id": "proposal-api-1",
                "expected_version": 0,
                "action": {
                    "action": "set_parameter",
                    "path": "simulation.stations.0.capacity",
                    "value": 2,
                },
            },
        )
        assert proposal_response.status_code == 201
        proposal = proposal_response.json()
        assert proposal["status"] == "pending"

        approval = test_client.post(
            f"/v1/sessions/{session['session_id']}/proposals/{proposal['proposal_id']}:approve",
            headers=HEADERS,
            json={
                "operation_id": "approval-api-1",
                "expected_version": 1,
                "reason": "人工确认",
            },
        )
        assert approval.status_code == 200
        assert approval.json()["status"] == "applied"

        current = test_client.get(f"/v1/sessions/{session['session_id']}", headers=HEADERS).json()
        assert current["workflow_version"] == 3
        assert current["config"]["simulation"]["stations"][0]["capacity"] == 2

        run_response = test_client.post(
            f"/v1/sessions/{session['session_id']}/runs",
            headers=HEADERS,
            json={"operation_id": "run-api-1", "expected_version": 3, "workers": 1},
        )
        assert run_response.status_code == 201
        run = run_response.json()
        assert run["status"] == "succeeded"
        assert "result" not in run

        result_response = test_client.get(
            f"/v1/sessions/{session['session_id']}/runs/{run['run_id']}",
            params={"include_result": True},
            headers=HEADERS,
        )
        assert result_response.json()["result"]["schema_version"] == "1.1"


def test_version_conflict_maps_to_http_409(tmp_path: Path) -> None:
    with client(tmp_path) as test_client:
        session = create_session(test_client)
        first = test_client.post(
            f"/v1/sessions/{session['session_id']}/proposals",
            headers=HEADERS,
            json={
                "operation_id": "first",
                "expected_version": 0,
                "action": {
                    "action": "set_parameter",
                    "path": "experiment.replications",
                    "value": 3,
                },
            },
        )
        stale = test_client.post(
            f"/v1/sessions/{session['session_id']}/proposals",
            headers=HEADERS,
            json={
                "operation_id": "stale",
                "expected_version": 0,
                "action": {
                    "action": "set_parameter",
                    "path": "experiment.replications",
                    "value": 4,
                },
            },
        )

    assert first.status_code == 201
    assert stale.status_code == 409


class StubAgent:
    def propose(self, current_config, kpi_summary, allowed_actions, objective=None):
        return ActionProposal(
            summary="建议扩大容量",
            actions=[
                ProposedAction(
                    action_type="set_parameter",
                    target="simulation.stations.0.capacity",
                    proposed_value=2,
                    rationale="利用率较高",
                    expected_effect="降低等待",
                    risk="low",
                    requires_approval=True,
                )
            ],
            caveats=[],
        )


def test_ai_endpoint_returns_pending_proposal_without_applying(tmp_path: Path) -> None:
    with client(tmp_path, agent_factory=lambda _config: StubAgent()) as test_client:
        session = create_session(test_client, "ai-session")
        response = test_client.post(
            f"/v1/sessions/{session['session_id']}/proposals:generate",
            headers=HEADERS,
            json={
                "operation_id": "ai-api-1",
                "expected_version": 0,
                "objective": "降低等待时间",
            },
        )
        current = test_client.get(f"/v1/sessions/{session['session_id']}", headers=HEADERS).json()

    assert response.status_code == 201
    assert response.json()["proposals"][0]["status"] == "pending"
    assert current["config"]["simulation"]["stations"][0]["capacity"] == 1


def test_websocket_replays_versioned_events(tmp_path: Path) -> None:
    with client(tmp_path) as test_client:
        session = create_session(test_client, "ws-session")
        with test_client.websocket_connect(
            f"/v1/sessions/{session['session_id']}/events",
            headers=HEADERS,
        ) as websocket:
            event = websocket.receive_json()

    assert event["sequence"] == 1
    assert event["event"] == "session_created"
