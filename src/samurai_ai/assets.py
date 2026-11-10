"""Where the shipped data files live: wordlists, recipes, templates, images.

Inside the package so a wheel or a PyInstaller bundle carries them; the repo
layout is only the development view of the same directory. `SAMURAI_ASSETS`
overrides it, which is what a packaged install with external data would use.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def assets_root(subdirectory: str = "") -> Path:
    override = os.environ.get("SAMURAI_ASSETS")
    if override:
        base = Path(override).expanduser()
    else:
        bundle = getattr(sys, "_MEIPASS", None)
        # the bundle puts assets next to the module tree, not inside the package
        base = (
            Path(bundle) / "assets"
            if bundle is not None
            else Path(__file__).resolve().parent / "assets"
        )
    return base / subdirectory if subdirectory else base


__all__ = ["assets_root"]
