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
from samurai_ai.execution.sandbox import load_profile_files
from samurai_ai.services.manifests import ManifestStore
from samurai_ai.services.playbooks import PlaybookService
from samurai_ai.storage.plugins import PluginFileRow, PluginRepository

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
    #: ident -> path inside the package, so installation does not guess layout
    tool_paths: dict[str, str]
    profile_paths: dict[str, str]
    playbook_paths: dict[str, str]

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
    tool_paths: dict[str, str] = {}
    profile_paths: dict[str, str] = {}
    playbook_paths: dict[str, str] = {}
    for item in manifest.files:
        loaded = _load(directory, item, manifest.name)
        if isinstance(loaded, ToolManifest):
            tools.append(loaded)
            tool_paths[loaded.id] = item.path
        elif isinstance(loaded, SandboxProfile):
            profiles.append(loaded)
            profile_paths[loaded.id] = item.path
        else:
            playbooks.append(loaded)
            playbook_paths[loaded.name] = item.path

    if len({tool.id for tool in tools}) != len(tools):
        raise PluginError("two tools share the same id")
    return Package(
        directory=directory,
        manifest=manifest,
        tools=tuple(tools),
        profiles=tuple(profiles),
        playbooks=tuple(playbooks),
        tool_paths=tool_paths,
        profile_paths=profile_paths,
        playbook_paths=playbook_paths,
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
    "InstalledPlugin",
    "PluginStore",
    "PluginStoreError",
    "Package",
    "PluginError",
    "SandboxProfileError",
    "copy_package",
    "digest_of",
    "validate_package",
]


class PluginStoreError(ValueError):
    pass


@dataclass(frozen=True)
class InstalledPlugin:
    name: str
    version: str
    directory: Path
    tool_ids: tuple[str, ...]
    profile_ids: tuple[str, ...]
    playbook_names: tuple[str, ...]


class PluginStore:
    """Install, list, check and remove plugin packages.

    Installation is the trust boundary: ``trusted=True`` says the operator read
    the package, and every file's digest is recorded so a later edit shows up as
    ``tampered``.
    """

    def __init__(
        self,
        root: Path,
        manifests: ManifestStore,
        playbooks: PlaybookService,
        profiles: Path,
        repository: PluginRepository,
    ) -> None:
        self.root = root
        self.manifests = manifests
        self.playbooks = playbooks
        self.profiles = profiles
        self.repository = repository

    def install(self, source: Path, *, trusted: bool, by: str = "operator") -> InstalledPlugin:
        if not trusted:
            raise PluginStoreError(
                "installing a plugin needs --trusted after you read the package"
            )
        package = validate_package(source)
        if self.repository.get(package.name) is not None:
            raise PluginStoreError(
                f"plugin {package.name} is already installed; remove it first"
            )
        self._check_collisions(package)

        target = self.root / package.name / package.manifest.version
        if target.exists():
            shutil.rmtree(target)
        copy_package(package, target)

        for tool in package.tools:
            self.manifests.install(target / package.tool_paths[tool.id], trusted=True)
        for profile in package.profiles:
            self.profiles.mkdir(parents=True, exist_ok=True)
            (self.profiles / f"{profile.id}.json").write_text(
                json.dumps(profile.model_dump(mode="json"), indent=2), encoding="utf-8"
            )
        for playbook in package.playbooks:
            self.playbooks.install_custom(
                target / package.playbook_paths[playbook.name], created_by=by
            )

        assets = [
            *[("tool", tool.id) for tool in package.tools],
            *[("profile", profile.id) for profile in package.profiles],
            *[("playbook", name) for name in package.playbook_names],
        ]
        self.repository.add(
            package.name,
            package.manifest.version,
            [
                PluginFileRow(path=item.path, kind=item.kind, sha256=item.sha256)
                for item in package.manifest.files
            ],
            assets,
            description=package.manifest.description,
            author=package.manifest.author,
            installed_by=by,
        )
        return InstalledPlugin(
            name=package.name,
            version=package.manifest.version,
            directory=target,
            tool_ids=package.tool_ids,
            profile_ids=package.profile_ids,
            playbook_names=package.playbook_names,
        )

    def _check_collisions(self, package: Package) -> None:
        known_tools = {manifest.id for manifest in self.manifests.load_all()}
        known_profiles = {
            profile.id for profile in load_profile_files(self.profiles)
        }
        for tool in package.tools:
            if tool.id in known_tools:
                raise PluginStoreError(f"tool {tool.id} is already installed")
        for profile in package.profiles:
            if profile.id in known_profiles:
                raise PluginStoreError(f"profile {profile.id} is already installed")
        for name in package.playbook_names:
            if self.playbooks.playbooks.get_custom_playbook(name) is not None:
                raise PluginStoreError(f"playbook {name} is already installed")

    def integrity(self, name: str) -> list[str]:
        """Paths that no longer match the recorded digest."""
        directory = self._package_directory(name)
        if directory is None:
            return []
        broken: list[str] = []
        for item in self.repository.files(name):
            path = directory / item.path
            if not path.exists() or digest_of(path) != item.sha256:
                broken.append(item.path)
        return broken

    def remove(self, name: str) -> None:
        for kind, ident in self.repository.assets(name):
            if kind == "tool":
                self.manifests.remove(ident)
            elif kind == "profile":
                (self.profiles / f"{ident}.json").unlink(missing_ok=True)
            elif kind == "playbook":
                self.playbooks.playbooks.remove_custom_playbook(ident)
        directory = self._package_directory(name)
        if directory is not None:
            shutil.rmtree(directory.parent, ignore_errors=True)
        self.repository.remove(name)

    def _package_directory(self, name: str) -> Path | None:
        row = self.repository.get(name)
        if row is None:
            return None
        return self.root / name / row.version


__all__ = ["InstalledPlugin", "PluginStore", "PluginStoreError"]
