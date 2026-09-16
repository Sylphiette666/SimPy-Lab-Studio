from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from simlab.studio import create_studio_app
from simlab.studio_ai import AISettings
from simlab.studio_credentials import (
    CredentialProtectionError,
    protect_secret,
    unprotect_secret,
)
from simlab.studio_profiles import AIProfileStore, ProfileInput

PROFILES = "/api/studio/ai/profiles"
WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI integration")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "SIMLAB_OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


def fields(**updates):
    return dict(name="Private model", model="fixture-model", base_url="https://fixture.invalid/v1",
                **updates)


def store(folder):
    return AIProfileStore(folder / "ai_profiles.json", AISettings(), threading.RLock())


@WINDOWS
def test_dpapi_decrypts_in_another_process_without_plaintext_on_disk(tmp_path):
    secret = "fixture-dpapi-中文-key-123456"
    encrypted = protect_secret(secret, "profile-one", "https://fixture.invalid/v1")
    assert secret not in encrypted
    child = subprocess.run(
        [sys.executable, "-c", "import sys,json,hashlib; "
         "from simlab.studio_credentials import unprotect_secret; "
         "value=json.load(sys.stdin); "
         "secret=unprotect_secret(value,'profile-one','https://fixture.invalid/v1'); "
         "print(hashlib.sha256(secret.encode()).hexdigest())"],
        input=json.dumps(encrypted), text=True, capture_output=True, check=True,
    )
    assert child.stdout.strip() == hashlib.sha256(secret.encode()).hexdigest()
    for profile_id, endpoint in [("another-profile", "https://fixture.invalid/v1"),
                                 ("profile-one", "https://other.invalid/v1")]:
        with pytest.raises(CredentialProtectionError):
            unprotect_secret(encrypted, profile_id, endpoint)
    with pytest.raises(CredentialProtectionError):
        unprotect_secret(encrypted[:-12] + "AAAA", "profile-one", "https://fixture.invalid/v1")


@pytest.mark.parametrize("value", ["plaintext-secret", "dpapi-user-v1:!!", "dpapi-user-v1:"])
def test_invalid_stored_values_never_become_credentials(value):
    with pytest.raises(CredentialProtectionError):
        unprotect_secret(value, "profile-one", "https://fixture.invalid/v1")


@WINDOWS
def test_saved_keys_restore_and_stay_out_of_catalogs_and_experiment_exports(tmp_path):
    secret = "fixture-private-stored-key-123456789"
    with TestClient(create_studio_app(output_root=tmp_path)) as client:
        response = client.post(PROFILES, json=fields(api_key=secret, remember_key=True))
        assert response.status_code == 201, response.text
        catalog = response.json()
        assert catalog["key_storage_available"]
        saved = next(p for p in catalog["profiles"] if p["id"] != "default")
        assert saved["remember_key"] and saved["has_key"] and not saved["key_storage_error"]
        client.post(f"{PROFILES}/{saved['id']}/activate")
    content = (tmp_path / "ai_profiles.json").read_text(encoding="utf-8")
    encrypted = json.loads(content)["protected_keys"][saved["id"]]
    assert secret not in content and "api_key" not in content
    captured = []

    def agent(settings):
        captured.append(settings.api_key)
        return SimpleNamespace(propose=lambda **_: {"note": "保持模型", "changes": []})

    with TestClient(create_studio_app(output_root=tmp_path, agent_factory=agent)) as client:
        catalog = client.get(PROFILES).json()
        assert next(p for p in catalog["profiles"] if p["id"] == saved["id"])["has_key"]
        restored = client.post(f"{PROFILES}/{saved['id']}/key",
                               headers={"X-Simlab-Key-Access": "settings"},
                               json={"base_url": saved["base_url"]})
        assert restored.status_code == 200 and restored.json() == {"api_key": secret}
        assert restored.headers["cache-control"] == "no-store"
        config = client.get("/api/studio/bootstrap").json()["template"]
        session = client.post("/api/studio/sessions", json={"config": config}).json()
        endpoint = f"/api/studio/sessions/{session['id']}"
        response = client.post(endpoint + "/adjust", json={
            "prompt": "保持模型", "expected_version_id": session["active_version_id"],
        })
        assert response.status_code == 200 and captured == [secret]
        for result in [response, client.get(PROFILES), client.get("/api/studio/bootstrap"),
                       client.get(endpoint)]:
            assert secret not in result.text and encrypted not in result.text
        with zipfile.ZipFile(io.BytesIO(client.get(endpoint + "/export").content)) as archive:
            assert "ai_profiles.json" not in archive.namelist()
            for name in archive.namelist():
                assert secret.encode() not in archive.read(name)
                assert encrypted.encode() not in archive.read(name)


@WINDOWS
def test_opt_in_existing_memory_key_then_opt_out_and_clear(tmp_path):
    profiles = store(tmp_path)
    body = fields(api_key="fixture-memory-secret")
    profiles.create(ProfileInput(**body))
    profile_id = next(key for key in profiles.profiles if key != "default")
    assert not profiles.protected_keys
    profiles.update(profile_id, ProfileInput(**fields(remember_key=True)))
    assert store(tmp_path).snapshot(profile_id).api_key == body["api_key"]
    profiles.update(profile_id, ProfileInput(**fields(remember_key=False)))
    assert not profiles.protected_keys
    assert profiles.snapshot(profile_id).api_key == body["api_key"]
    assert not store(tmp_path).snapshot(profile_id).api_key
    profiles.update(profile_id, ProfileInput(**fields(remember_key=True)))
    profiles.update(profile_id, ProfileInput(**fields(api_key="", remember_key=True)))
    assert not profiles.snapshot(profile_id).api_key and not profiles.protected_keys
    assert not store(tmp_path).snapshot(profile_id).api_key


@WINDOWS
def test_endpoint_change_rotation_copy_and_delete_are_isolated(tmp_path):
    profiles = store(tmp_path)
    profiles.create(ProfileInput(**fields(api_key="first-key", remember_key=True)))
    first = next(key for key in profiles.profiles if key != "default")
    profiles.create(ProfileInput(**fields()))  # Copying metadata cannot copy a credential.
    copied = next(key for key in profiles.profiles if key not in {"default", first})
    assert not profiles.snapshot(copied).api_key and copied not in profiles.protected_keys
    edited = fields(remember_key=True)
    edited["model"] = "renamed-model"
    profiles.update(first, ProfileInput(**edited))
    assert store(tmp_path).snapshot(first).api_key == "first-key"
    profiles.update(first, ProfileInput(**fields(api_key="rotated-key", remember_key=True)))
    assert store(tmp_path).snapshot(first).api_key == "rotated-key"
    changed = fields(remember_key=True)
    changed["base_url"] = "https://another.invalid/v1"
    profiles.update(first, ProfileInput(**changed))
    assert not profiles.snapshot(first).api_key and first not in profiles.protected_keys
    profiles.update(first, ProfileInput(**fields(api_key="delete-key", remember_key=True)))
    profiles.delete(first)
    stored = json.loads(profiles.path.read_text(encoding="utf-8"))
    assert first not in stored.get("protected_keys", {})
    assert first not in store(tmp_path).profiles


@WINDOWS
def test_unreadable_ciphertext_preserved_until_explicit_replacement_or_removal(tmp_path):
    profiles = store(tmp_path)
    profiles.create(ProfileInput(**fields(api_key="not-readable-elsewhere", remember_key=True)))
    profile_id = next(key for key in profiles.profiles if key != "default")
    saved = json.loads(profiles.path.read_text(encoding="utf-8"))
    saved["protected_keys"][profile_id] = "dpapi-user-v1:corrupted"
    profiles.path.write_text(json.dumps(saved), encoding="utf-8")
    restarted = store(tmp_path)
    public = next(p for p in restarted.catalog()["profiles"] if p["id"] == profile_id)
    assert public["remember_key"] and public["key_storage_error"] and not public["has_key"]
    restarted.update(profile_id, ProfileInput(**fields(remember_key=True)))
    assert restarted.protected_keys[profile_id] == "dpapi-user-v1:corrupted"
    restarted.update(profile_id, ProfileInput(**fields(api_key="replacement", remember_key=True)))
    assert store(tmp_path).snapshot(profile_id).api_key == "replacement"


def test_unavailable_protection_fails_without_saving_metadata_or_plaintext(tmp_path, monkeypatch):
    monkeypatch.setattr("simlab.studio_credentials.persistence_available", lambda: False)
    profiles = store(tmp_path)
    with pytest.raises(HTTPException) as error:
        profiles.create(ProfileInput(**fields(api_key="must-not-persist", remember_key=True)))
    assert error.value.status_code == 422
    assert "must-not-persist" not in error.value.detail
    assert not profiles.path.exists() and len(profiles.profiles) == 1
    profiles.create(ProfileInput(**fields(api_key="memory-only")))
    assert "memory-only" not in profiles.path.read_text(encoding="utf-8")
    assert set(json.loads(profiles.path.read_text(encoding="utf-8"))) == {
        "active_profile_id", "profiles",
    }


@WINDOWS
def test_encryption_and_atomic_write_failures_leave_old_key_usable(tmp_path, monkeypatch):
    profiles = store(tmp_path)
    profiles.create(ProfileInput(**fields(api_key="original-key", remember_key=True)))
    profile_id = next(key for key in profiles.profiles if key != "default")
    original = profiles.path.read_bytes()
    with monkeypatch.context() as patch:
        def failed_encryption(*_):
            raise CredentialProtectionError("密钥保护失败。")
        patch.setattr("simlab.studio_profiles.protect_secret", failed_encryption)
        with pytest.raises(HTTPException):
            profiles.update(profile_id, ProfileInput(**fields(
                api_key="new-key", remember_key=True,
            )))
    with monkeypatch.context() as patch:
        def failed_replace(*_):
            raise OSError("test write failure")
        patch.setattr(Path, "replace", failed_replace)
        with pytest.raises(HTTPException):
            profiles.update(profile_id, ProfileInput(**fields(remember_key=False)))
    assert profiles.path.read_bytes() == original
    assert profiles.snapshot(profile_id).api_key == "original-key"
    assert store(tmp_path).snapshot(profile_id).api_key == "original-key"
    assert not profiles.path.with_suffix(".json.tmp").exists()


def test_opt_in_without_a_key_requires_input(tmp_path):
    profiles = store(tmp_path)
    with pytest.raises(HTTPException) as error:
        profiles.create(ProfileInput(**fields(remember_key=True)))
    assert error.value.status_code == 422 and not profiles.path.exists()


def test_settings_key_access_requires_header_same_origin_and_matching_endpoint(tmp_path):
    secret = "fixture-settings-only-secret"
    with TestClient(create_studio_app(output_root=tmp_path)) as client:
        catalog = client.post(PROFILES, json=fields(api_key=secret)).json()
        saved = next(p for p in catalog["profiles"] if p["id"] != "default")
        endpoint = f"{PROFILES}/{saved['id']}/key"
        body = {"base_url": saved["base_url"] + "/"}
        headers = {"X-Simlab-Key-Access": "settings"}
        assert client.get(endpoint).status_code == 405
        assert client.post(endpoint, json=body).status_code == 403
        for extra in [{"Origin": "https://other.invalid"}, {"Sec-Fetch-Site": "cross-site"}]:
            response = client.post(endpoint, headers=headers | extra, json=body)
            assert response.status_code == 403 and secret not in response.text
        response = client.post(endpoint, headers=headers,
                               json={"base_url": "https://other.invalid/v1"})
        assert response.status_code == 409 and secret not in response.text
        response = client.post(endpoint, headers=headers | {"Origin": "http://testserver"},
                               json=body)
        assert response.status_code == 200 and response.json() == {"api_key": secret}
        assert response.headers["cache-control"] == "no-store"
        assert secret not in client.get(PROFILES).text
        assert client.post(PROFILES + "/missing/key", headers=headers, json=body).status_code == 404
        client.put(f"{PROFILES}/{saved['id']}", json=fields(api_key=""))
        assert client.post(endpoint, headers=headers, json=body).json() == {"api_key": ""}
