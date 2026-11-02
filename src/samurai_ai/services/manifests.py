"""Installed tool manifests: fail-closed loading, sha256 pinning, replace by id."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from samurai_ai.domain.tool_manifest import ManifestError, ToolManifest


class ManifestStoreError(ValueError):
    pass


class ManifestStore:
    """One JSON file per manifest under the state directory.

    Installation is the trust boundary: a manifest is either shipped in the repo
    or installed by the operator with ``--trusted``, and its sha256 is recorded
    next to it so a later edit is visible.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, manifest_id: str) -> Path:
        return self.directory / f"{manifest_id}.json"

    def _digest_path(self, manifest_id: str) -> Path:
        return self.directory / f"{manifest_id}.sha256"

    def install(
        self, source: Path, *, trusted: bool, replace: bool = False
    ) -> ToolManifest:
        if not trusted:
            raise ManifestStoreError(
                "an installed manifest needs --trusted after you read it"
            )
        try:
            raw = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise ManifestStoreError(f"cannot read manifest {source}: {exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ManifestStoreError(f"manifest is not JSON: {exc}") from exc
        try:
            manifest = ToolManifest.model_validate({**payload, "trusted": True})
        except Exception as exc:  # pydantic wraps ManifestError
            raise ManifestStoreError(str(exc)) from exc
        path = self._path(manifest.id)
        if path.exists() and not replace:
            raise ManifestStoreError(
                f"manifest {manifest.id} is already installed; pass --replace"
            )
        self.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")
        self._digest_path(manifest.id).write_text(
            hashlib.sha256(raw.encode("utf-8")).hexdigest() + "\n", encoding="utf-8"
        )
        return manifest

    def remove(self, manifest_id: str) -> bool:
        path = self._path(manifest_id)
        if not path.exists():
            return False
        path.unlink()
        self._digest_path(manifest_id).unlink(missing_ok=True)
        return True

    def load_all(self) -> tuple[ToolManifest, ...]:
        """Parse every installed manifest; a broken file is skipped, not fatal."""
        if not self.directory.exists():
            return ()
        manifests: list[ToolManifest] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                manifests.append(ToolManifest.model_validate({**payload, "trusted": True}))
            except Exception:
                continue
        return tuple(manifests)

    def digest(self, manifest_id: str) -> str | None:
        try:
            return self._digest_path(manifest_id).read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def is_tampered(self, manifest: ToolManifest) -> bool:
        recorded = self.digest(manifest.id)
        path = self._path(manifest.id)
        if recorded is None or not path.exists():
            return True
        current = hashlib.sha256(path.read_bytes()).hexdigest()
        return current != recorded


__all__ = ["ManifestStore", "ManifestStoreError", "ManifestError"]
