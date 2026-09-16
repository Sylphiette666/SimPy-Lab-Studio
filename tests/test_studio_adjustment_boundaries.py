import io
import json
import threading
import zipfile

from test_studio_adjustments import Agent, create, setup_client, wait


def test_no_changes_is_saved_as_conversation(tmp_path):
    class NoChange:
        def propose(self, **kwargs):
            return {"note": "无需调整", "changes": []}
    _, client = setup_client(tmp_path, lambda _: NoChange())
    with client:
        _, route, body = create(client)
        job = client.post(route + "/adjustments", json=body).json()
        assert wait(client, route, job["id"])["status"] == "no_changes"
        session = client.get(route).json()
        assert len(session["versions"]) == 1
        assert session["messages"][-1]["content"] == "无需调整"
        assert session["messages"][-1]["applied"] is False


def test_proposals_redact_keys_in_prompt_note_and_export(tmp_path):
    secret = "secret-review-only-key-921"

    class Echo:
        def propose(self, **kwargs):
            return {"note": "请勿回显 " + secret,
                    "changes": [{"path": "machines.0.availability", "value": .9}]}
    _, client = setup_client(tmp_path, lambda _: Echo())
    with client:
        client.put("/api/studio/settings/ai", json={
            "model": "fixture", "api_key": secret, "base_url": "https://example.com/v1",
        })
        _, route, body = create(client)
        body["prompt"] += secret
        job = client.post(route + "/adjustments", json=body).json()
        assert wait(client, route, job["id"])["status"] == "ready"
        assert secret not in client.get(route).text
        with zipfile.ZipFile(io.BytesIO(client.get(route + "/export").content)) as archive:
            assert all(secret.encode() not in archive.read(name) for name in archive.namelist())
        for file in tmp_path.rglob("*.json"):
            assert secret not in file.read_text("utf-8")


def test_apply_revalidates_saved_constraints(tmp_path):
    app, client = setup_client(tmp_path, lambda _: Agent())
    with client:
        session, route, body = create(client)
        job = client.post(route + "/adjustments", json=body).json()
        assert wait(client, route, job["id"])["status"] == "ready"
        with app.state.store.lock:
            stored = app.state.store.sessions[session["id"]]["adjustments"][0]
            stored["proposal"]["changes"] = [{"path": "buffers.0.capacity", "value": 100}]
        # Validation errors on persisted proposals must be safe HTTP failures.
        response = client.post(route + "/adjustments/" + job["id"] + "/apply")
        assert response.status_code == 422
        assert len(client.get(route).json()["versions"]) == 1


def test_cancelled_provider_calls_still_count_toward_worker_bound(tmp_path):
    release = threading.Event()
    started = threading.Event()

    class Blocked:
        def propose(self, **kwargs):
            started.set()
            assert release.wait(5)
            return {"note": "done", "changes": []}
    app, client = setup_client(tmp_path, lambda _: Blocked())
    with client:
        try:
            first = None
            for _ in range(4):
                _, route, body = create(client)
                response = client.post(route + "/adjustments", json=body)
                assert response.status_code == 202
                if first is None:
                    first = (route, response.json()["id"])
                # Leave queued jobs present until the capacity check below.
            assert started.wait(2)
            assert client.post(first[0] + "/adjustments/" + first[1] + "/cancel").status_code == 200
            _, route, body = create(client)
            assert client.post(route + "/adjustments", json=body).status_code == 429
        finally:
            release.set()
        app.state.adjustments.executor.shutdown(wait=True)


def test_apply_disk_failure_rolls_back_memory(tmp_path, monkeypatch):
    app, client = setup_client(tmp_path, lambda _: Agent())
    with client:
        _, route, body = create(client)
        job = client.post(route + "/adjustments", json=body).json()
        assert wait(client, route, job["id"])["status"] == "ready"
        before = client.get(route).json()
        original = app.state.store.save

        def broken_save(session):
            raise OSError("disk full")
        monkeypatch.setattr(app.state.store, "save", broken_save)
        assert client.post(route + "/adjustments/" + job["id"] + "/apply").status_code == 500
        assert json.dumps(client.get(route).json()) == json.dumps(before)
        monkeypatch.setattr(app.state.store, "save", original)
        assert client.post(route + "/adjustments/" + job["id"] + "/apply").status_code == 200
