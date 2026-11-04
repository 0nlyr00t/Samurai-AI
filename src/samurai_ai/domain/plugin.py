"""Plugins: one directory, one manifest, and only data inside.

A plugin ships tools as manifests, playbooks as JSON templates and sandbox
profiles that reuse an image already pinned by digest. Nothing executes from the
plugin itself — the trust model therefore stays the one the tools already use:
explicit `--trusted`, a sha256 per file, and `tampered` when a file moves after
installation.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PLUGIN_API = 1
FileKind = Literal["tool", "playbook", "profile"]
KINDS: tuple[FileKind, ...] = ("tool", "playbook", "profile")

_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_VERSION = re.compile(r"\d+\.\d+\.\d+\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_NAME = 32
_MAX_FILES = 64


class PluginError(ValueError):
    pass


class PluginFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: path inside the plugin directory, forward slashes, no traversal
    path: str = Field(min_length=1, max_length=200)
    sha256: str
    kind: FileKind

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if value.startswith("/") or "\\" in value:
            raise PluginError(f"plugin file path must be relative with slashes: {value!r}")
        parts = value.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise PluginError(f"plugin file path has an empty or dotted segment: {value!r}")
        if not value.endswith(".json"):
            raise PluginError(f"plugin files must be JSON: {value!r}")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise PluginError(f"sha256 must be 64 lowercase hex chars: {value!r}")
        return value


class PluginManifest(BaseModel):
    """`plugin.json` — the package as a whole, validated before anything is copied."""

    model_config = ConfigDict(extra="forbid")

    plugin_api: int = PLUGIN_API
    name: str
    version: str
    description: str = Field(default="", max_length=400)
    author: str = Field(default="", max_length=120)
    files: tuple[PluginFile, ...] = Field(min_length=1, max_length=_MAX_FILES)

    @field_validator("plugin_api")
    @classmethod
    def validate_api(cls, value: int) -> int:
        if value != PLUGIN_API:
            raise PluginError(
                f"plugin_api {value} is not supported, this build speaks {PLUGIN_API}"
            )
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if len(value) > _MAX_NAME or not _NAME.fullmatch(value):
            raise PluginError(f"plugin name must be kebab-case, up to {_MAX_NAME}: {value!r}")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not _VERSION.fullmatch(value):
            raise PluginError(f"plugin version must look like 1.2.3: {value!r}")
        return value

    @model_validator(mode="after")
    def validate_files(self) -> PluginManifest:
        seen: set[str] = set()
        for item in self.files:
            if item.path in seen:
                raise PluginError(f"file listed twice: {item.path}")
            seen.add(item.path)
        if not any(item.kind == "tool" for item in self.files):
            raise PluginError("a plugin must ship at least one tool")
        return self

    @property
    def tool_files(self) -> tuple[PluginFile, ...]:
        return tuple(item for item in self.files if item.kind == "tool")

    def relative_paths(self) -> set[str]:
        return {item.path for item in self.files}


__all__ = [
    "KINDS",
    "PLUGIN_API",
    "FileKind",
    "PluginError",
    "PluginFile",
    "PluginManifest",
]
