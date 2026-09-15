"""API/model profiles with isolated credentials and optional Windows encryption."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from simlab.studio_ai import AISettings, APIFormat, validate_endpoint
from simlab.studio_credentials import (
    CredentialProtectionError,
    persistence_available,
    protect_secret,
    unprotect_secret,
)


class ProfileMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=60)
    model: str = Field(min_length=1, max_length=160)
    base_url: str = Field(default="", max_length=1000)
    api_format: APIFormat = "auto"

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        return validate_endpoint(value)


class ProfileInput(ProfileMetadata):
    api_key: str | None = Field(default=None, max_length=4096, repr=False)
    remember_key: bool | None = Field(default=None, strict=True)


class StoredProfile(ProfileMetadata):
    id: str = Field(min_length=1, max_length=36)

    @field_validator("id")
    @classmethod
    def identifier(cls, value: str) -> str:
        if value != "default" and str(uuid.UUID(value)) != value:
            raise ValueError("配置标识无效。")
        return value


class _Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active_profile_id: str
    profiles: list[StoredProfile] = Field(min_length=1, max_length=20)
    protected_keys: dict[str, str] = Field(default_factory=dict, max_length=20, repr=False)


class AIProfileStore:
    """All reads and atomic mutations share the session store's reentrant lock."""

    def __init__(self, path: Path, initial: AISettings, lock: threading.RLock):
        self.path = path
        self.lock = lock
        default = StoredProfile(
            id="default", name="默认配置", model=initial.model, base_url=initial.base_url
        )
        self.active_profile_id = default.id
        self.profiles = {default.id: default}
        self.keys: dict[str, str] = {}
        self.protected_keys: dict[str, str] = {}
        if path.is_file():
            try:
                saved = _Catalog.model_validate_json(path.read_text(encoding="utf-8"))
                loaded = {profile.id: profile for profile in saved.profiles}
                if len(loaded) != len(saved.profiles) or saved.active_profile_id not in loaded:
                    raise ValueError("invalid profile catalog")
                self.profiles = loaded
                self.active_profile_id = saved.active_profile_id
                self.protected_keys = {
                    profile_id: value for profile_id, value in saved.protected_keys.items()
                    if profile_id in loaded
                }
            except (OSError, ValueError):
                # A broken configuration must not prevent accessing existing simulations.
                pass
        for profile_id, encrypted in self.protected_keys.items():
            try:
                self.keys[profile_id] = unprotect_secret(
                    encrypted, profile_id, self.profiles[profile_id].base_url
                )
            except CredentialProtectionError:
                # Preserve unreadable ciphertext so editing metadata cannot destroy it.
                pass
        # An environment credential belongs only to its original default endpoint.
        current_default = self.profiles.get("default")
        if (current_default and current_default.base_url == default.base_url
                and "default" not in self.protected_keys):
            self.keys["default"] = initial.api_key

    def snapshot(self, profile_id: str | None = None) -> AISettings:
        with self.lock:
            profile = self._get(profile_id or self.active_profile_id)
            return AISettings(
                profile_id=profile.id,
                profile_name=profile.name,
                model=profile.model,
                base_url=profile.base_url,
                api_format=profile.api_format,
                api_key=self.keys.get(profile.id, ""),
            )

    def catalog(self, *, injected: bool = False) -> dict[str, Any]:
        with self.lock:
            profiles = []
            for profile_id in self.profiles:
                public = self.snapshot(profile_id).public(injected=injected)
                public["id"] = public.pop("profile_id")
                public["name"] = public.pop("profile_name")
                public["has_key"] = bool(self.keys.get(profile_id))
                public["remember_key"] = profile_id in self.protected_keys
                public["key_storage_error"] = public["remember_key"] and not public["has_key"]
                profiles.append(public)
            return {"active_profile_id": self.active_profile_id, "profiles": profiles,
                    "key_storage_available": persistence_available()}

    def _get(self, profile_id: str) -> StoredProfile:
        if profile_id not in self.profiles:
            raise HTTPException(404, "API 与模型配置不存在。")
        return self.profiles[profile_id]

    def _commit(
        self, profiles: dict[str, StoredProfile], active: str, keys: dict[str, str],
        protected_keys: dict[str, str],
    ) -> None:
        # Metadata and ciphertext change atomically, before publishing memory state.
        saved = _Catalog(active_profile_id=active, profiles=list(profiles.values()),
                         protected_keys=protected_keys)
        temp = self.path.with_suffix(".json.tmp")
        try:
            # Keep the original catalog format until the user opts into persistence.
            temp.write_text(saved.model_dump_json(
                indent=2, exclude=None if protected_keys else {"protected_keys"},
            ), encoding="utf-8")
            temp.replace(self.path)
        except OSError as exc:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise HTTPException(500, "配置保存失败，请检查本机输出目录。") from exc
        self.profiles = profiles
        self.active_profile_id = active
        self.keys = keys
        self.protected_keys = protected_keys

    def _protected_update(
        self, profile: StoredProfile, key: str, body: ProfileInput,
        *, same_endpoint: bool = False,
    ) -> dict[str, str]:
        saved = self.protected_keys.copy()
        old = saved.pop(profile.id, None)
        remember = body.remember_key if body.remember_key is not None else bool(old)
        # Clearing a key or changing endpoint without a new key removes the old copy.
        if body.api_key == "" or (old and not same_endpoint and body.api_key is None):
            return saved
        if not remember:
            return saved
        if same_endpoint and old and body.api_key is None:
            saved[profile.id] = old
            return saved
        try:
            saved[profile.id] = protect_secret(key, profile.id, profile.base_url)
        except CredentialProtectionError as exc:
            raise HTTPException(422, str(exc)) from None
        return saved

    def create(self, body: ProfileInput) -> None:
        with self.lock:
            if len(self.profiles) >= 20:
                raise HTTPException(422, "最多保存 20 组 API 与模型配置。")
            profile_id = str(uuid.uuid4())
            profile = StoredProfile(
                id=profile_id, **body.model_dump(exclude={"api_key", "remember_key"})
            )
            self._commit(
                {**self.profiles, profile_id: profile},
                self.active_profile_id,
                {**self.keys, profile_id: body.api_key or ""},
                self._protected_update(profile, body.api_key or "", body),
            )

    def update(self, profile_id: str, body: ProfileInput) -> None:
        with self.lock:
            previous = self._get(profile_id)
            profile = StoredProfile(
                id=profile_id, **body.model_dump(exclude={"api_key", "remember_key"})
            )
            key = body.api_key
            if key is None:
                # Never forward the previous endpoint's credential to a newly selected URL.
                key = self.keys.get(profile_id, "") if previous.base_url == profile.base_url else ""
            self._commit(
                {**self.profiles, profile_id: profile},
                self.active_profile_id,
                {**self.keys, profile_id: key},
                self._protected_update(profile, key, body,
                                       same_endpoint=previous.base_url == profile.base_url),
            )

    def activate(self, profile_id: str) -> None:
        with self.lock:
            self._get(profile_id)
            self._commit(self.profiles.copy(), profile_id, self.keys.copy(),
                         self.protected_keys.copy())

    def delete(self, profile_id: str) -> None:
        with self.lock:
            self._get(profile_id)
            if profile_id == self.active_profile_id:
                raise HTTPException(422, "请先启用另一组配置，再删除当前配置。")
            self._commit(
                {key: value for key, value in self.profiles.items() if key != profile_id},
                self.active_profile_id,
                {key: value for key, value in self.keys.items() if key != profile_id},
                {key: value for key, value in self.protected_keys.items() if key != profile_id},
            )
