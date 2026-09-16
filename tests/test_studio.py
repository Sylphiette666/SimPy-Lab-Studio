from __future__ import annotations

import io
import json
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from simlab.manufacturing import case_a_config
from simlab.studio import create_studio_app

API = "/api/studio"


def small_config():
    return case_a_config(until_seconds=7200, warmup_seconds=600, replications=2).model_dump(
        mode="json"
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("SIMLAB_OPENAI_BASE_URL", raising=False)
    with TestClient(create_studio_app(output_root=tmp_path)) as client:
        yield client


def new_session(client, mode="paper", config=None):
    response = client.post(
        API + "/sessions", json={"config": config or small_config(), "mode": mode}
    )
    assert response.status_code == 201, response.text
    return response.json()


def wait_run(client, session_id, run_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(f"{API}/sessions/{session_id}/runs/{run_id}")
        assert response.status_code == 200
        run = response.json()
        if run["status"] in {"succeeded", "failed"}:
            assert run["status"] == "succeeded", run
            return run
        time.sleep(0.01)
    pytest.fail("simulation did not finish in the bounded test window")


def test_bootstrap_static_and_origin_guards(client):
    response = client.get(API + "/bootstrap")
    assert response.status_code == 200
    assert response.json()["template"]["machines"][0]["name"] == "SV36262"
    assert response.json()["ai"]["available"] is False
    assert "api_key" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert (
        client.post(
            API + "/sessions", json={}, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert client.get(API + "/bootstrap", headers={"Host": "evil.example"}).status_code == 403
    assert (
        client.post(
            API + "/sessions", json={}, headers={"Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c["buffers"][0].update(capacity=6),
        lambda c: c["machines"][0].update(name="different"),
        lambda c: c.update(replications=51),
        lambda c: c.update(until_seconds=91 * 86400),
        lambda c: c["machines"][0].update(cycle_time_seconds=0.001),
        lambda c: c["machines"][0].update(availability=0.5, mttr_seconds=1e-8),
        lambda c: c["machines"][0].update(processing_power_kw=1e100),
        lambda c: c.update(warmup_seconds=c["until_seconds"]),
        lambda c: c.update(confidence_level=0.9999999999999999),
    ],
)
def test_invalid_models_and_paper_constraints(client, change):
    config = small_config()
    change(config)
    response = client.post(API + "/sessions", json={"config": config, "mode": "paper"})
    assert response.status_code == 422
    assert client.get(API + "/sessions").json() == {"sessions": []}


def test_custom_model_versions_stale_restore_and_reload(client, tmp_path):
    config = small_config()
    config["buffers"][0]["capacity"] = 8
    session = new_session(client, "custom", config)
    original = session["active_version_id"]
    config["machines"][0]["availability"] = 0.9
    endpoint = f"{API}/sessions/{session['id']}"
    body = {
        "config": config,
        "mode": "custom",
        "expected_version_id": original,
        "label": "availability",
    }
    response = client.post(endpoint + "/versions", json=body)
    assert response.status_code == 200
    updated = response.json()
    assert updated["versions"][-1]["changes"] == [
        {"path": "machines.0.availability", "before": 0.85, "after": 0.9}
    ]
    assert client.post(endpoint + "/versions", json=body).status_code == 409
    restored = client.post(
        endpoint + "/restore",
        json={"version_id": original, "expected_version_id": updated["active_version_id"]},
    ).json()
    assert len(restored["versions"]) == 3
    assert restored["active_version_id"] not in {original, updated["active_version_id"]}
    assert restored["versions"][-1]["config"]["machines"][0]["availability"] == 0.85
    with TestClient(create_studio_app(output_root=tmp_path)) as reloaded:
        assert reloaded.get(endpoint).json() == restored
    assert client.get(API + "/sessions/not-a-uuid").status_code == 404


def test_real_preview_study_and_portable_export(client):
    session = new_session(client)
    endpoint = f"{API}/sessions/{session['id']}"
    completed = {}
    for kind in ("preview", "study"):
        response = client.post(
            endpoint + "/runs", json={"version_id": session["active_version_id"], "kind": kind}
        )
        assert response.status_code == 202
        completed[kind] = wait_run(client, session["id"], response.json()["id"])
    preview = completed["preview"]["result"]
    assert len(preview["frames"]) == 601
    assert preview["frames"][0]["time_seconds"] == 0
    assert preview["frames"][-1]["time_seconds"] == 7200
    assert all(frame["material_balance_error"] == 0 for frame in preview["frames"])
    study = completed["study"]["result"]
    assert len(study["replications"]) == 2
    assert study["summary"]
    assert (
        preview["frames"][-1]["completed_window"]
        == study["replications"][0]["metrics"]["completed"]
    )
    exported = client.get(endpoint + "/export")
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert {"report.html", "session.json", "README.txt"} <= set(archive.namelist())
        assert any(name.endswith("_replications.csv") for name in archive.namelist())
        report = archive.read("report.html").decode()
        assert 'id="data"' in report
        assert "throughput_per_hour" in report
        assert "avg_wip" in report
        assert "specific_energy_kwh_per_part" in report
        actual = json.loads(archive.read(f"runs/{completed['preview']['id']}.json"))
        assert actual["result"] == preview


def test_export_without_runs_and_llm_requires_configuration(client):
    session = new_session(client)
    endpoint = f"{API}/sessions/{session['id']}"
    assert (
        client.post(
            endpoint + "/adjust",
            json={"prompt": "提高可用率", "expected_version_id": session["active_version_id"]},
        ).status_code
        == 503
    )
    with zipfile.ZipFile(io.BytesIO(client.get(endpoint + "/export").content)) as archive:
        assert "尚无完成的重复实验" in archive.read("report.html").decode()


def test_settings_and_validation_never_echo_or_persist_key(client, tmp_path):
    secret = "sk-THIS_IS_A_TEST_SECRET_123456789"
    response = client.put(
        API + "/settings/ai", json={"model": "test", "base_url": "", "api_key": secret}
    )
    assert response.status_code == 200 and response.json()["available"] is True
    assert secret not in response.text
    invalid = client.put(API + "/settings/ai", json={"model": "", "api_key": secret})
    assert invalid.status_code == 422 and secret not in invalid.text
    session = new_session(client)
    exported = client.get(f"{API}/sessions/{session['id']}/export")
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert all(secret.encode() not in archive.read(name) for name in archive.namelist())
    assert all(secret not in p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.json"))
    changed_provider = client.put(
        API + "/settings/ai", json={"model": "local", "base_url": "http://localhost:11434/v1"}
    )
    assert changed_provider.json()["available"] is False
    assert (
        client.put(
            API + "/settings/ai",
            json={"model": "test", "base_url": "https://example.com/?key=" + secret},
        ).status_code
        == 422
    )


class FakeAgent:
    def __init__(self, proposal, callback=None):
        self.proposal = proposal
        self.callback = callback

    def propose(self, **kwargs):
        if self.callback:
            self.callback(kwargs)
        return self.proposal


def test_llm_applies_validated_version_auto_preview_and_redacts(tmp_path):
    secret = "sk-THIS_IS_A_TEST_SECRET_123456789"
    seen = []

    def factory(settings):
        assert settings.api_key == secret
        return FakeAgent(
            {
                "note": "第一台可用率提高至90%。 " + secret,
                "changes": [{"path": "machines.0.availability", "value": 0.9}],
            },
            seen.append,
        )

    with TestClient(create_studio_app(output_root=tmp_path, agent_factory=factory)) as client:
        client.put(API + "/settings/ai", json={"model": "test", "api_key": secret})
        session = new_session(client)
        response = client.post(
            f"{API}/sessions/{session['id']}/adjust",
            json={
                "prompt": "第一台可用率改90%",
                "expected_version_id": session["active_version_id"],
            },
        )
        assert response.status_code == 200, response.text
        adjusted = response.json()
        assert adjusted["versions"][-1]["source"] == "llm"
        assert adjusted["versions"][-1]["config"]["machines"][0]["availability"] == 0.9
        assert secret not in response.text
        assert seen[0]["prompt"] == "第一台可用率改90%"
        assert adjusted["messages"][-1]["applied"] is True
        assert len(adjusted["runs"]) == 1
        wait_run(client, session["id"], adjusted["runs"][0]["id"])
    assert all(secret not in p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.json"))


@pytest.mark.parametrize(
    "changes",
    [
        [{"path": "buffers.0.capacity", "value": 10}],
        [{"path": "machines.0.availability", "value": 1.2}],
        [{"path": "__class__.__globals__", "value": "bad"}],
        [{"path": "machines.0.availability", "value": 0.9}] * 2,
    ],
)
def test_invalid_llm_adjustments_are_atomic(tmp_path, changes):
    def factory(settings):
        return FakeAgent({"note": "提案", "changes": changes})

    with TestClient(create_studio_app(output_root=tmp_path, agent_factory=factory)) as client:
        session = new_session(client)
        endpoint = f"{API}/sessions/{session['id']}"
        response = client.post(
            endpoint + "/adjust",
            json={"prompt": "优化", "expected_version_id": session["active_version_id"]},
        )
        assert response.status_code == 422
        assert client.get(endpoint).json() == session


def test_llm_no_change_explains_without_false_version(tmp_path):
    def factory(settings):
        return FakeAgent({"note": "需要更明确的目标。", "changes": []})

    with TestClient(create_studio_app(output_root=tmp_path, agent_factory=factory)) as client:
        session = new_session(client)
        response = client.post(
            f"{API}/sessions/{session['id']}/adjust",
            json={"prompt": "优化", "expected_version_id": session["active_version_id"]},
        )
        assert response.status_code == 200
        assert response.json()["active_version_id"] == session["active_version_id"]
        assert response.json()["messages"][-1]["applied"] is False
        assert response.json()["runs"] == []


def test_llm_in_flight_cannot_overwrite_newer_manual_version(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def pause(_kwargs):
        entered.set()
        assert release.wait(5)

    def factory(settings):
        return FakeAgent(
            {"note": "提高可用率", "changes": [{"path": "machines.0.availability", "value": 0.9}]},
            pause,
        )

    with TestClient(create_studio_app(output_root=tmp_path, agent_factory=factory)) as client:
        session = new_session(client)
        endpoint = f"{API}/sessions/{session['id']}"
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                client.post,
                endpoint + "/adjust",
                json={"prompt": "提高可用率", "expected_version_id": session["active_version_id"]},
            )
            assert entered.wait(5)
            config = small_config()
            config["machines"][0]["availability"] = 0.95
            manual = client.post(
                endpoint + "/versions",
                json={
                    "config": config,
                    "mode": "paper",
                    "expected_version_id": session["active_version_id"],
                },
            )
            assert manual.status_code == 200
            release.set()
            assert future.result().status_code == 409
        assert client.get(endpoint).json() == manual.json()


def test_failed_worker_is_reported_and_existing_model_is_preserved(client, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("provider secret should not escape")

    monkeypatch.setattr("simlab.live_manufacturing.generate_preview", fail)
    session = new_session(client)
    endpoint = f"{API}/sessions/{session['id']}"
    response = client.post(
        endpoint + "/runs", json={"version_id": session["active_version_id"], "kind": "preview"}
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = client.get(endpoint + "/runs/" + response.json()["id"]).json()
        if run["status"] == "failed":
            break
        time.sleep(0.01)
    assert run["status"] == "failed" and "secret" not in run["error"]
    assert run["result"] is None
    assert client.get(endpoint).json()["active_version_id"] == session["active_version_id"]


def test_restart_marks_interrupted_runs_failed(tmp_path):
    import uuid

    with TestClient(create_studio_app(output_root=tmp_path)) as first:
        session = new_session(first)
    path = tmp_path / session["id"] / "session.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["runs"].append(
        {
            "id": str(uuid.uuid4()),
            "version_id": session["active_version_id"],
            "kind": "preview",
            "status": "running",
            "progress": 0.1,
            "result": None,
            "error": None,
        }
    )
    path.write_text(json.dumps(saved), encoding="utf-8")
    with TestClient(create_studio_app(output_root=tmp_path)) as restarted:
        actual = restarted.get(f"{API}/sessions/{session['id']}").json()
        assert actual["runs"][0]["status"] == "failed"
        assert "重启" in actual["runs"][0]["error"]


def test_preview_short_window_and_export_does_not_mislabel_an_old_replay(client):
    config = small_config()
    config.update(until_seconds=3 * 86400, warmup_seconds=86400)
    session = new_session(client, config=config)
    endpoint = f"{API}/sessions/{session['id']}"
    started = client.post(
        endpoint + "/runs", json={"version_id": session["active_version_id"], "kind": "preview"}
    ).json()
    result = wait_run(client, session["id"], started["id"])["result"]
    assert result["duration_seconds"] == 2 * 86400
    assert result["truncated"] is True
    config["machines"][0]["availability"] = 0.95
    changed = client.post(
        endpoint + "/versions",
        json={
            "config": config,
            "mode": "paper",
            "expected_version_id": session["active_version_id"],
        },
    )
    assert changed.status_code == 200
    with zipfile.ZipFile(io.BytesIO(client.get(endpoint + "/export").content)) as archive:
        report = archive.read("report.html").decode()
        assert "当前选定版本尚无已完成的动画预览" in report
        assert '"frames": []' in report
        assert f"runs/{started['id']}.json" in archive.namelist()


def test_llm_receives_actual_current_version_metrics(tmp_path):
    seen = []

    def factory(settings):
        return FakeAgent({"note": "观察结果后暂不修改。", "changes": []}, seen.append)

    with TestClient(create_studio_app(output_root=tmp_path, agent_factory=factory)) as client:
        session = new_session(client)
        endpoint = f"{API}/sessions/{session['id']}"
        started = client.post(
            endpoint + "/runs", json={"version_id": session["active_version_id"], "kind": "study"}
        ).json()
        study = wait_run(client, session["id"], started["id"])["result"]
        response = client.post(
            endpoint + "/adjust",
            json={
                "prompt": "根据结果判断下一步",
                "expected_version_id": session["active_version_id"],
            },
        )
        assert response.status_code == 200
        assert seen[0]["context"]["latest_study_summary"] == study["summary"]


def test_llm_receives_actual_preview_window_and_sampling_scope(tmp_path):
    seen = []

    def factory(settings):
        return FakeAgent({"note": "先完成多次重复实验再判断。", "changes": []}, seen.append)

    with TestClient(create_studio_app(output_root=tmp_path, agent_factory=factory)) as client:
        config = small_config()
        config.update(until_seconds=3 * 86400, warmup_seconds=86400)
        session = new_session(client, config=config)
        endpoint = f"{API}/sessions/{session['id']}"
        started = client.post(
            endpoint + "/runs",
            json={"version_id": session["active_version_id"], "kind": "preview"},
        ).json()
        preview = wait_run(client, session["id"], started["id"])["result"]
        response = client.post(
            endpoint + "/adjust",
            json={
                "prompt": "这次结果能说明什么",
                "expected_version_id": session["active_version_id"],
            },
        )
        assert response.status_code == 200
        context = seen[0]["context"]
        assert context["single_run_preview_metrics"] == preview["frames"][-1]["metrics"]
        assert context["preview_metadata"] == {
            "duration_seconds": 2 * 86400,
            "configured_duration_seconds": 3 * 86400,
            "warmup_seconds": 86400,
            "truncated": True,
            "seed": preview["seed"],
            "observation_seconds": 86400,
        }
        assert "latest_study_summary" not in context
