from __future__ import annotations

import io
import json
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from simlab.manufacturing import case_a_config
from simlab.studio import create_studio_app

API = "/api/studio"
PROFILES = API + "/ai/profiles"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for variable in (
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "SIMLAB_OPENAI_BASE_URL",
        "SIMLAB_OPENAI_MODEL",
    ):
        monkeypatch.delenv(variable, raising=False)


def profile(**overrides):
    return {
        "name": "DeepSeek",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "api_format": "chat_completions",
        **overrides,
    }


def create_profile(client, **overrides):
    before = {p["id"] for p in client.get(PROFILES).json()["profiles"]}
    response = client.post(PROFILES, json=profile(**overrides))
    assert response.status_code == 201, response.text
    return next(p for p in response.json()["profiles"] if p["id"] not in before)


def session(client):
    config = case_a_config(until_seconds=7200, warmup_seconds=0, replications=2)
    response = client.post(API + "/sessions", json={"config": config.model_dump(mode="json")})
    assert response.status_code == 201
    return response.json()


def adjust(client, model_session, **extra):
    return client.post(
        f"{API}/sessions/{model_session['id']}/adjust",
        json={
            "prompt": "提高产出率",
            "expected_version_id": model_session["active_version_id"],
            **extra,
        },
    )


def test_profiles_create_switch_update_delete_and_legacy_settings(tmp_path):
    with TestClient(create_studio_app(output_root=tmp_path)) as client:
        initial = client.get(PROFILES).json()
        assert initial["active_profile_id"] == "default"
        assert initial["profiles"][0]["model"] == "gpt-4.1-mini"
        assert initial["profiles"][0]["available"] is False
        new = create_profile(client)
        assert client.get(PROFILES).json()["active_profile_id"] == "default"
        assert new["provider"] == "DeepSeek"
        switched = client.post(f"{PROFILES}/{new['id']}/activate").json()
        assert switched["active_profile_id"] == new["id"]
        ai = client.get(API + "/bootstrap").json()["ai"]
        assert ai["profile_id"] == new["id"] and ai["model"] == "deepseek-chat"
        edited = client.put(
            API + "/settings/ai",
            json={"model": "deepseek-reasoner", "base_url": "https://api.deepseek.com"},
        )
        assert edited.status_code == 200
        assert edited.json()["api_format"] == "chat_completions"
        assert edited.json()["profile_name"] == "DeepSeek"
        assert client.delete(f"{PROFILES}/{new['id']}").status_code == 422
        assert client.post(f"{PROFILES}/missing/activate").status_code == 404
        assert client.delete(f"{PROFILES}/missing").status_code == 404
        client.post(f"{PROFILES}/default/activate")
        assert len(client.delete(f"{PROFILES}/{new['id']}").json()["profiles"]) == 1
        assert client.delete(f"{PROFILES}/default").status_code == 422


def test_credentials_are_isolated_cleared_on_endpoint_change_and_never_persist(tmp_path):
    secret_a = "sk-profile-A-secret-abcdefghijklmnop"
    secret_b = "sk-profile-B-secret-abcdefghijklmnop"
    seen = []

    def factory(settings):
        seen.append(settings)
        return SimpleNamespace(propose=lambda **_: {"note": "无需更改", "changes": []})

    with TestClient(create_studio_app(output_root=tmp_path, agent_factory=factory)) as client:
        client.put(API + "/settings/ai", json={"model": "model-a", "api_key": secret_a})
        new = create_profile(client, api_key=secret_b)
        current = session(client)
        assert adjust(client, current).status_code == 200
        client.post(f"{PROFILES}/{new['id']}/activate")
        assert adjust(client, current).status_code == 200
        client.put(f"{PROFILES}/{new['id']}", json=profile(name="模型 B", model="new-b"))
        assert adjust(client, current).status_code == 200
        client.put(f"{PROFILES}/{new['id']}", json=profile(base_url="http://localhost:11434/v1"))
        assert adjust(client, current).status_code == 200  # Injected fake requires no key.
        client.post(f"{PROFILES}/default/activate")
        assert adjust(client, current).status_code == 200
        assert [item.api_key for item in seen] == [secret_a, secret_b, secret_b, "", secret_a]
        for endpoint in (PROFILES, API + "/bootstrap", f"{API}/sessions/{current['id']}"):
            response = client.get(endpoint)
            assert secret_a not in response.text and secret_b not in response.text
            assert "api_key" not in response.text
        exported = client.get(f"{API}/sessions/{current['id']}/export")
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            for name in archive.namelist():
                assert secret_a.encode() not in archive.read(name)
                assert secret_b.encode() not in archive.read(name)
    for path in tmp_path.rglob("*.json"):
        saved = path.read_text(encoding="utf-8")
        assert secret_a not in saved and secret_b not in saved and "api_key" not in saved
    with TestClient(create_studio_app(output_root=tmp_path)) as restarted:
        assert all(not item["available"] for item in restarted.get(PROFILES).json()["profiles"])


def test_restart_restores_selection_and_metadata_without_credentials(tmp_path):
    with TestClient(create_studio_app(output_root=tmp_path)) as client:
        new = create_profile(client, api_key="private-token", api_format="responses")
        client.post(f"{PROFILES}/{new['id']}/activate")
        assert client.get(API + "/bootstrap").json()["ai"]["available"] is True
    saved = json.loads((tmp_path / "ai_profiles.json").read_text(encoding="utf-8"))
    assert saved["active_profile_id"] == new["id"]
    with TestClient(create_studio_app(output_root=tmp_path)) as restarted:
        ai = restarted.get(API + "/bootstrap").json()["ai"]
        assert ai["profile_id"] == new["id"]
        assert ai["api_format"] == "responses"
        assert ai["model"] == "deepseek-chat" and ai["available"] is False
        assert adjust(restarted, session(restarted)).status_code == 503


def test_environment_key_only_belongs_to_matching_default_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "environment-token")
    with TestClient(create_studio_app(output_root=tmp_path)) as client:
        assert client.get(API + "/bootstrap").json()["ai"]["available"] is True
        other = create_profile(client, base_url="", model="same-provider-model")
        assert other["available"] is False
        client.put(
            API + "/settings/ai",
            json={"model": "new-provider", "base_url": "https://api.example.com/v1"},
        )
    with TestClient(create_studio_app(output_root=tmp_path)) as restarted:
        assert restarted.get(API + "/bootstrap").json()["ai"]["available"] is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": " "},
        {"model": " "},
        {"name": "x" * 61},
        {"base_url": "https://example.com?api_key=TOP_SECRET"},
        {"base_url": "https://user:TOP_SECRET@example.com"},
        {"api_format": "unknown"},
        {"unexpected": "TOP_SECRET"},
    ],
)
def test_invalid_profile_cannot_change_selection_or_echo_secrets(tmp_path, overrides):
    with TestClient(create_studio_app(output_root=tmp_path)) as client:
        before = client.get(PROFILES).json()
        response = client.post(PROFILES, json=profile(api_key="TOP_SECRET", **overrides))
        assert response.status_code == 422
        assert "TOP_SECRET" not in response.text
        assert client.get(PROFILES).json() == before


def test_maximum_profiles_and_clear_credential(tmp_path):
    with TestClient(create_studio_app(output_root=tmp_path)) as client:
        new = create_profile(client, api_key="a-token")
        assert new["available"] is True
        response = client.put(f"{PROFILES}/{new['id']}", json=profile(api_key=""))
        assert not next(p for p in response.json()["profiles"] if p["id"] == new["id"])["available"]
        for index in range(18):
            create_profile(client, name=f"Configuration {index}")
        assert client.post(PROFILES, json=profile()).status_code == 422
        assert len(client.get(PROFILES).json()["profiles"]) == 20


def test_inflight_call_retains_snapshot_when_profile_edited_and_switched(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    seen = []

    def factory(settings):
        seen.append(settings)

        def propose(**_):
            entered.set()
            assert release.wait(10)
            return {
                "note": "第一台设备可用率提高。",
                "changes": [{"path": "machines.0.availability", "value": 0.9}],
            }

        return SimpleNamespace(propose=propose)

    with TestClient(create_studio_app(output_root=tmp_path, agent_factory=factory)) as client:
        new = create_profile(client, api_key="original-private-key")
        current = session(client)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(adjust, client, current, ai_profile_id=new["id"])
            assert entered.wait(10)
            try:
                client.put(
                    f"{PROFILES}/{new['id']}",
                    json=profile(
                        model="edited-model",
                        base_url="https://new.example.com/v1",
                        api_key="new-key",
                    ),
                )
                client.post(f"{PROFILES}/{new['id']}/activate")
                assert seen[0].model == "deepseek-chat"
                assert seen[0].api_key == "original-private-key"
                with pytest.raises(FrozenInstanceError):
                    seen[0].model = "mutated"
            finally:
                release.set()
            response = future.result(timeout=10)
        assert response.status_code == 200, response.text
        result = response.json()
        audit = result["versions"][-1]["ai_config"]
        assert audit == result["messages"][-1]["ai_config"]
        assert audit["profile_id"] == new["id"]
        assert audit["model"] == "deepseek-chat"
        assert audit["base_url"] == "https://api.deepseek.com"
        assert audit["provider"] == "DeepSeek" and audit["api_format"] == "chat_completions"
        assert "original-private-key" not in response.text and "api_key" not in response.text
        assert client.get(API + "/bootstrap").json()["ai"]["model"] == "edited-model"


def test_optional_captured_profile_selects_that_model_and_missing_profile_fails(tmp_path):
    seen = []

    def factory(settings):
        seen.append(settings)
        return SimpleNamespace(propose=lambda **_: {"note": "暂不更改", "changes": []})

    with TestClient(create_studio_app(output_root=tmp_path, agent_factory=factory)) as client:
        new = create_profile(client)
        current = session(client)
        response = adjust(client, current, ai_profile_id=new["id"])
        assert response.status_code == 200
        assert seen[-1].model == "deepseek-chat"
        assert response.json()["messages"][-1]["ai_config"]["profile_id"] == new["id"]
        assert client.get(PROFILES).json()["active_profile_id"] == "default"
        assert adjust(client, current, ai_profile_id="missing").status_code == 404


def test_corrupt_metadata_does_not_break_existing_session(tmp_path):
    with TestClient(create_studio_app(output_root=tmp_path)) as client:
        current = session(client)
    (tmp_path / "ai_profiles.json").write_text('{"api_key":"never-read"}', encoding="utf-8")
    with TestClient(create_studio_app(output_root=tmp_path)) as restarted:
        assert restarted.get(PROFILES).json()["active_profile_id"] == "default"
        assert restarted.get(f"{API}/sessions/{current['id']}").status_code == 200
