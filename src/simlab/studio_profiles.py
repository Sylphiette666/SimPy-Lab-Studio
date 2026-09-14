"""Persistent API/model profiles with isolated, process-only credentials."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from simlab.studio_ai import AISettings, APIFormat, validate_endpoint


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
        if path.is_file():
            try:
                saved = _Catalog.model_validate_json(path.read_text(encoding="utf-8"))
                loaded = {profile.id: profile for profile in saved.profiles}
                if len(loaded) != len(saved.profiles) or saved.active_profile_id not in loaded:
                    raise ValueError("invalid profile catalog")
                self.profiles = loaded
                self.active_profile_id = saved.active_profile_id
            except (OSError, ValueError):
                # A broken configuration must not prevent accessing existing simulations.
                pass
        # An environment credential belongs only to its original default endpoint.
        current_default = self.profiles.get("default")
        if current_default and current_default.base_url == default.base_url:
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
                profiles.append(public)
            return {"active_profile_id": self.active_profile_id, "profiles": profiles}

    def _get(self, profile_id: str) -> StoredProfile:
        if profile_id not in self.profiles:
            raise HTTPException(404, "API 与模型配置不存在。")
        return self.profiles[profile_id]

    def _commit(
        self, profiles: dict[str, StoredProfile], active: str, keys: dict[str, str]
    ) -> None:
        # Publish in-memory changes only after the sanitized metadata has been saved.
        saved = _Catalog(active_profile_id=active, profiles=list(profiles.values()))
        temp = self.path.with_suffix(".json.tmp")
        try:
            temp.write_text(saved.model_dump_json(indent=2), encoding="utf-8")
            temp.replace(self.path)
        except OSError as exc:
            raise HTTPException(500, "配置保存失败，请检查本机输出目录。") from exc
        self.profiles = profiles
        self.active_profile_id = active
        self.keys = keys

    def create(self, body: ProfileInput) -> None:
        with self.lock:
            if len(self.profiles) >= 20:
                raise HTTPException(422, "最多保存 20 组 API 与模型配置。")
            profile_id = str(uuid.uuid4())
            profile = StoredProfile(id=profile_id, **body.model_dump(exclude={"api_key"}))
            self._commit(
                {**self.profiles, profile_id: profile},
                self.active_profile_id,
                {**self.keys, profile_id: body.api_key or ""},
            )

    def update(self, profile_id: str, body: ProfileInput) -> None:
        with self.lock:
            previous = self._get(profile_id)
            profile = StoredProfile(id=profile_id, **body.model_dump(exclude={"api_key"}))
            key = body.api_key
            if key is None:
                # Never forward the previous endpoint's credential to a newly selected URL.
                key = self.keys.get(profile_id, "") if previous.base_url == profile.base_url else ""
            self._commit(
                {**self.profiles, profile_id: profile},
                self.active_profile_id,
                {**self.keys, profile_id: key},
            )

    def activate(self, profile_id: str) -> None:
        with self.lock:
            self._get(profile_id)
            self._commit(self.profiles.copy(), profile_id, self.keys.copy())

    def delete(self, profile_id: str) -> None:
        with self.lock:
            self._get(profile_id)
            if profile_id == self.active_profile_id:
                raise HTTPException(422, "请先启用另一组配置，再删除当前配置。")
            self._commit(
                {key: value for key, value in self.profiles.items() if key != profile_id},
                self.active_profile_id,
                {key: value for key, value in self.keys.items() if key != profile_id},
            )
