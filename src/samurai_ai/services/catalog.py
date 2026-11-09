"""The reviewed recipe catalogue: built-in assets plus operator-installed files.

A recipe is a `ToolManifest` — the same file already describes the container run,
so one artifact answers both "how it runs" and "how it gets here". The model
never invents a package name or an install command: the recipe carries them.

Loading is fail-closed and cross-checks the adapter the recipe binds to, so a
recipe that renders a parameter the tool does not declare is rejected at load
instead of at install time.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from samurai_ai.domain.tool_manifest import ManifestError, ToolManifest

_REPO_ROOT = Path(__file__).resolve().parents[3]


class CatalogError(ManifestError):
    pass


def assets_directory() -> Path:
    """`assets`, also inside a PyInstaller one-file bundle."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle is not None:
        return Path(bundle) / "assets"
    return _REPO_ROOT / "assets"


def recipes_directory() -> Path:
    return assets_directory() / "recipes"


def portability_scripts() -> Path:
    """Scripts a project-local recipe copies; never executed from here."""
    return assets_directory() / "templates"


class RecipeCatalog:
    """Recipes by tool id; every file is validated before it is used."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or recipes_directory()

    def _files(self) -> list[Path]:
        return sorted(self.directory.glob("*.json")) if self.directory.is_dir() else []

    def load_all(self) -> dict[str, ToolManifest]:
        """Recipes by id. A broken file is fatal: a half catalogue is worse."""
        recipes: dict[str, ToolManifest] = {}
        for path in self._files():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                recipe = ToolManifest.model_validate({**payload, "trusted": True})
            except (OSError, json.JSONDecodeError, ManifestError) as exc:
                raise CatalogError(f"invalid recipe {path.name}: {exc}") from exc
            if not (recipe.execution.native or recipe.execution.project_local):
                raise CatalogError(
                    f"recipe {path.name} declares neither native nor project-local "
                    "execution and belongs in the manifest store"
                )
            recipes[recipe.id] = recipe
        return recipes

    def get(self, tool_id: str) -> ToolManifest | None:
        return self.load_all().get(tool_id)

    @staticmethod
    def validate_against(recipe: ToolManifest, parameters: dict[str, Any]) -> None:
        """Recipe placeholders and the bound adapter's schema must agree exactly."""
        properties = parameters.get("properties") or {}
        schema_params = (
            {str(name) for name in properties} if isinstance(properties, dict) else set()
        )
        if not schema_params:
            return  # no adapter (project-local): the recipe's own schema is the contract
        rendered = {
            name
            for token in recipe.argv
            for name in _placeholders(token)
            if not name.startswith("__")
        }
        unknown = sorted(rendered - schema_params)
        if unknown:
            raise CatalogError(
                f"recipe {recipe.id} renders parameters the tool does not declare: "
                f"{', '.join(unknown)}"
            )
        required = {str(name) for name in parameters.get("required") or []}
        missing = sorted(required - rendered)
        if missing:
            raise CatalogError(
                f"recipe {recipe.id} never renders required parameters: "
                f"{', '.join(missing)}"
            )

    def install(self, source: Path, *, trusted: bool, directory: Path) -> ToolManifest:
        """A user recipe: explicit trust, sha256 kept beside it."""
        if not trusted:
            raise CatalogError("a user recipe needs --trusted after you read it")
        try:
            raw = source.read_text(encoding="utf-8")
            recipe = ToolManifest.model_validate({**json.loads(raw), "trusted": True})
        except (OSError, json.JSONDecodeError, ManifestError) as exc:
            raise CatalogError(f"cannot install recipe {source}: {exc}") from exc
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{recipe.id}.json").write_text(raw, encoding="utf-8")
        (directory / f"{recipe.id}.sha256").write_text(
            hashlib.sha256(raw.encode("utf-8")).hexdigest() + "\n", encoding="utf-8"
        )
        return recipe

    def remove(self, tool_id: str, directory: Path) -> bool:
        target = directory / f"{tool_id}.json"
        if not target.exists():
            return False
        target.unlink()
        (directory / f"{tool_id}.sha256").unlink(missing_ok=True)
        return True


def _placeholders(token: str) -> list[str]:
    if token.startswith("{") and token.endswith("}") and len(token) > 2:
        return [token[1:-1]]
    return []


def which(executable: str) -> str:
    return shutil.which(executable) or ""


__all__ = [
    "CatalogError",
    "RecipeCatalog",
    "assets_directory",
    "portability_scripts",
    "recipes_directory",
    "which",
]
