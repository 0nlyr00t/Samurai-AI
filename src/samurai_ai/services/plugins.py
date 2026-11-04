"""Plugin packages: validate a directory, then install it as one unit.

Validation is fail-closed and runs on the source directory before a single byte is
copied: every file must be declared, every digest must match, ids must carry the
plugin's prefix and a profile may not be softer than the core ones.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from samurai_ai.domain.kernel import OperationPlan
from samurai_ai.domain.playbooks import PlaybookTemplate
from samurai_ai.domain.plugin import PluginError, PluginFile, PluginManifest
from samurai_ai.domain.sandbox import SandboxProfile, SandboxProfileError
from samurai_ai.domain.tool_manifest import ToolManifest

MANIFEST_NAME = "plugin.json"
_IGNORED = {".DS_Store"}


@dataclass(frozen=True)
class Package:
    """A validated plugin, still on disk where it was validated."""

    directory: Path
    manifest: PluginManifest
    tools: tuple[ToolManifest, ...]
    profiles: tuple[SandboxProfile, ...]
    playbooks: tuple[PlaybookTemplate, ...]

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def tool_ids(self) -> tuple[str, ...]:
        return tuple(tool.id for tool in self.tools)

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return tuple(profile.id for profile in self.profiles)

    @property
    def playbook_names(self) -> tuple[str, ...]:
        return tuple(playbook.name for playbook in self.playbooks)


def digest_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_prefix(kind: str, ident: str, name: str) -> None:
    prefix = f"{name}."
    if not ident.startswith(prefix):
        raise PluginError(
            f"{kind} {ident!r} must start with the plugin name ({prefix}…)"
        )


def _check_hardening(profile: SandboxProfile) -> None:
    """A plugin profile may differ in limits, not in the hardening floor."""
    # build_argv always renders a read-only root and a full capability drop, so
    # the only ways to soften a profile are the user and the network class
    if profile.network_mode == "scoped" and profile.proxy_flag is None:
        raise PluginError(
            f"profile {profile.id}: a scoped profile needs the tool's proxy flag"
        )
    if profile.user == "0:0" and not profile.description:
        raise PluginError(
            f"profile {profile.id}: a root container must say why in the description"
        )


def _load(
    directory: Path, item: PluginFile, name: str
) -> ToolManifest | SandboxProfile | PlaybookTemplate:
    path = directory / item.path
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PluginError(f"cannot read {item.path}: {exc}") from exc
    actual = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if actual != item.sha256:
        raise PluginError(f"{item.path}: sha256 does not match the manifest")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PluginError(f"{item.path} is not JSON: {exc}") from exc

    if item.kind == "tool":
        manifest = ToolManifest.model_validate({**payload, "trusted": True})
        _require_prefix("tool", manifest.id, name)
        return manifest
    if item.kind == "profile":
        profile = SandboxProfile.model_validate(payload)
        _require_prefix("profile", profile.id, name)
        _check_hardening(profile)
        return profile
    template: PlaybookTemplate = PlaybookTemplate.model_validate(payload)
    _require_prefix("playbook", template.name, name)
    # structural check (unique ids, known dependencies, no cycles) via the plan
    OperationPlan(
        operation_id="plugin-validation",
        title=template.title,
        tasks=template.tasks,
        gates=template.gates,
    )
    return template


def validate_package(directory: Path) -> Package:
    """Read the whole package and refuse anything surprising."""
    if not directory.is_dir():
        raise PluginError(f"not a directory: {directory}")
    manifest_path = directory / MANIFEST_NAME
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PluginError(f"no {MANIFEST_NAME} in {directory}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PluginError(f"{MANIFEST_NAME} is not JSON: {exc}") from exc
    try:
        manifest = PluginManifest.model_validate(payload)
    except PluginError:
        raise
    except Exception as exc:  # pydantic wraps our validators
        raise PluginError(str(exc)) from exc

    present = {
        str(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file() and path.name not in _IGNORED
    }
    present.discard(MANIFEST_NAME)
    listed = manifest.relative_paths()
    if extra := sorted(present - listed):
        raise PluginError(f"files not listed in the manifest: {', '.join(extra)}")
    if missing := sorted(listed - present):
        raise PluginError(f"files listed but absent: {', '.join(missing)}")

    tools: list[ToolManifest] = []
    profiles: list[SandboxProfile] = []
    playbooks: list[PlaybookTemplate] = []
    for item in manifest.files:
        loaded = _load(directory, item, manifest.name)
        if isinstance(loaded, ToolManifest):
            tools.append(loaded)
        elif isinstance(loaded, SandboxProfile):
            profiles.append(loaded)
        else:
            playbooks.append(loaded)

    if len({tool.id for tool in tools}) != len(tools):
        raise PluginError("two tools share the same id")
    return Package(
        directory=directory,
        manifest=manifest,
        tools=tuple(tools),
        profiles=tuple(profiles),
        playbooks=tuple(playbooks),
    )


def copy_package(package: Package, target: Path) -> Path:
    """Copy the validated files into the store; the manifest goes along."""
    target.mkdir(parents=True, exist_ok=True)
    for item in package.manifest.files:
        destination = target / item.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(package.directory / item.path, destination)
    shutil.copyfile(package.directory / MANIFEST_NAME, target / MANIFEST_NAME)
    return target


__all__ = [
    "MANIFEST_NAME",
    "Package",
    "PluginError",
    "SandboxProfileError",
    "copy_package",
    "digest_of",
    "validate_package",
]
