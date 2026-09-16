from __future__ import annotations

import json
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from simlab.manufacturing import case_a_config
from simlab.studio import create_studio_app

PROPOSAL = {"note": "提高可用率，须用完整实验验证。", "changes": [
    {"path": "machines.0.availability", "value": .9},
]}


def setup_client(tmp_path, factory):
    app = create_studio_app(output_root=tmp_path, agent_factory=factory)
    return app, TestClient(app)


def create(client):
    session = client.post("/api/studio/sessions", json={"config": case_a_config(
        until_seconds=3600, warmup_seconds=300, replications=2,
    ).model_dump(mode="json")}).json()
    route = f"/api/studio/sessions/{session['id']}"
    body = {"prompt": "提高到90%", "expected_version_id": session["active_version_id"],
            "request_id": str(uuid.uuid4())}
    return session, route, body


def wait(client, route, identifier):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(route + "/adjustments/" + identifier).json()
        if job["status"] not in {"running", "queued"}:
            return job
        time.sleep(.01)
    pytest.fail("proposal did not finish")


class Agent:
    def propose(self, **kwargs):
        return PROPOSAL


def test_preview_is_durable_and_apply_is_explicit_and_idempotent(tmp_path):
    app, client = setup_client(tmp_path, lambda _: Agent())
    with client:
        session, route, body = create(client)
        job = client.post(route + "/adjustments", json=body).json()
        assert wait(client, route, job["id"])["status"] == "ready"
        unchanged = client.get(route).json()
        assert len(unchanged["versions"]) == 1 and unchanged["runs"] == []
        assert client.post(route + "/adjustments", json=body).json()["id"] == job["id"]
    # Reopen from disk, with a different agent: apply must use the saved proposal.
    _, client = setup_client(tmp_path, lambda _: pytest.fail("unexpected AI call"))
    with client:
        endpoint = route + "/adjustments/" + job["id"] + "/apply"
        first = client.post(endpoint)
        assert first.status_code == 200
        applied = first.json()
        assert len(applied["versions"]) == 2
        assert applied["versions"][-1]["config"]["machines"][0]["availability"] == .9
        second = client.post(endpoint).json()
        assert second["active_version_id"] == applied["active_version_id"]
        assert len(second["versions"]) == 2 and len(second["messages"]) == 2


@pytest.mark.parametrize("action", ["cancel", "timeout"])
def test_late_reply_cannot_override_cancel_or_timeout(tmp_path, action):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    class Slow:
        def propose(self, **kwargs):
            started.set()
            assert release.wait(5)
            finished.set()
            return PROPOSAL

    app, client = setup_client(tmp_path, lambda _: Slow())
    with client:
        session, route, body = create(client)
        job = client.post(route + "/adjustments", json=body).json()
        try:
            assert started.wait(2)
            if action == "cancel":
                response = client.post(route + "/adjustments/" + job["id"] + "/cancel")
                assert response.status_code == 200
            else:
                app.state.adjustments._expire(session["id"], job["id"])
            assert client.post(route + "/adjustments/" + job["id"] + "/apply").status_code == 409
        finally:
            release.set()
        assert finished.wait(2)
        app.state.adjustments.executor.shutdown(wait=True)
        final = client.get(route).json()
        assert len(final["versions"]) == 1 and final["runs"] == []
        expected_status = "cancelled" if action == "cancel" else "failed"
        assert final["adjustments"][0]["status"] == expected_status


def test_stale_head_rejects_application(tmp_path):
    _, client = setup_client(tmp_path, lambda _: Agent())
    with client:
        session, route, body = create(client)
        job = client.post(route + "/adjustments", json=body).json()
        assert wait(client, route, job["id"])["status"] == "ready"
        config = session["versions"][0]["config"]
        config["name"] = "New name"
        assert client.post(route + "/versions", json={
            "config": config, "expected_version_id": body["expected_version_id"],
        }).status_code == 200
        assert client.post(route + "/adjustments/" + job["id"] + "/apply").status_code == 409
        assert len(client.get(route).json()["versions"]) == 2


def test_constraints_rechecked_and_failures_do_not_leak_secrets(tmp_path):
    class Invalid:
        def propose(self, **kwargs):
            return {"note": "Invalid", "changes": [{"path": "buffers.0.capacity", "value": 99}]}
    _, client = setup_client(tmp_path, lambda _: Invalid())
    with client:
        _, route, body = create(client)
        job = client.post(route + "/adjustments", json=body).json()
        assert wait(client, route, job["id"])["status"] == "failed"
        assert len(client.get(route).json()["versions"]) == 1


def test_request_bound_and_restart_recovery(tmp_path):
    started, release = threading.Event(), threading.Event()

    class Slow:
        def propose(self, **kwargs):
            started.set()
            assert release.wait(5)
            return PROPOSAL
    app, client = setup_client(tmp_path, lambda _: Slow())
    with client:
        session, route, body = create(client)
        job = client.post(route + "/adjustments", json=body).json()
        try:
            assert started.wait(2)
            assert client.post(route + "/adjustments", json={
                **body, "request_id": str(uuid.uuid4()),
            }).status_code == 409
            saved = json.loads((tmp_path / session["id"] / "session.json").read_text("utf-8"))
            assert saved["adjustments"][0]["status"] == "running"
        finally:
            client.post(route + "/adjustments/" + job["id"] + "/cancel")
            release.set()
        app.state.adjustments.executor.shutdown(wait=True)
    saved["adjustments"][0]["status"] = "running"
    (tmp_path / session["id"] / "session.json").write_text(json.dumps(saved), "utf-8")
    _, client = setup_client(tmp_path, lambda _: Agent())
    with client:
        assert client.get(route).json()["adjustments"][0]["status"] == "failed"
