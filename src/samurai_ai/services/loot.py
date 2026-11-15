"""Per-operation LOOT snapshots in the operator's working directory.

The export deliberately contains captured credential values. It is not a report
or a public artifact: directories are private, files are owner-only, and a local
.gitignore prevents accidental commits.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from samurai_ai.services.ctf import CtfService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.tool_keys import CATALOGUE
from samurai_ai.services.vault import VaultService


def default_loot_root(cwd: Path) -> Path:
    """The working-directory default, overridable for isolated/headless runs."""
    return Path(os.environ.get("SAMURAI_LOOT_DIR", str(cwd / "loot"))).expanduser().resolve()


def _default_service_key(name: str) -> bool:
    if name.endswith("-access-token") or name.endswith(("_api_key", "_api_token", "_org_id")):
        return True
    return name in {entry.env_name.lower() for entry in CATALOGUE}


class LootExportService:
    def __init__(
        self,
        kernel: KernelService,
        vault: VaultService,
        ctf: CtfService,
        root: Path,
        *,
        service_key: Callable[[str], bool] = _default_service_key,
    ) -> None:
        self.kernel = kernel
        self.vault = vault
        self.ctf = ctf
        self.root = root
        self.service_key = service_key

    def signature(self, operation_id: str) -> str:
        payload = self._payload(operation_id, include_values=False)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def export(self, operation_id: str) -> dict[str, Path]:
        directory = self.root / operation_id
        self._private_directory(directory)
        payload = self._payload(operation_id, include_values=True)
        paths = {
            name: directory / f"{name}.json" for name in ("findings", "credentials", "flags")
        }
        for name, path in paths.items():
            self._atomic_json(path, payload[name])
        ignore = self.root / ".gitignore"
        self._atomic_bytes(ignore, b"*\n")
        return paths

    def _payload(self, operation_id: str, *, include_values: bool) -> dict[str, Any]:
        credentials = []
        for metadata in self.vault.list_metadata(operation_id):
            if self.service_key(metadata.name):
                continue
            item = metadata.model_dump(mode="json")
            if include_values:
                item["value"] = self.vault.get(metadata.name, operation_id)
            credentials.append(item)
        return {
            "findings": [
                finding.model_dump(mode="json")
                for finding in self.kernel.kernel.list_findings(operation_id)
            ],
            "credentials": credentials,
            "flags": [
                flag.model_dump(mode="json") for flag in self.ctf.list_flags(operation_id)
            ],
        }

    def _private_directory(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        if directory != self.root:
            os.chmod(self.root, 0o700)

    def _atomic_json(self, path: Path, payload: Any) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        self._atomic_bytes(path, text.encode("utf-8"))

    @staticmethod
    def _atomic_bytes(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
