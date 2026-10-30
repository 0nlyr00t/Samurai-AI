"""Where Samurai keeps its state.

One directory holds everything the operator owns: the database, the vault key,
the sandbox allowlist, and the appearance files. It lives under the XDG config
directory so a machine has one obvious place to back up, sync or delete.

The old per-project ``.samurai/`` is not consulted any more; :func:`legacy_directory`
only reports it so the operator can point ``--db`` or ``SAMURAI_HOME`` at it.
"""

import os
from pathlib import Path

APP_NAME = "samurai"
LEGACY_NAME = ".samurai"


def config_directory() -> Path:
    """The state directory.

    ``SAMURAI_HOME`` wins (old spelling kept working), then the XDG config
    directory, then ``~/.config``.
    """
    configured = os.environ.get("SAMURAI_HOME") or os.environ.get("SAMURAI_CONFIG_HOME")
    if configured:
        return Path(configured).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base).expanduser() / APP_NAME


def data_directory() -> Path:
    """Alias kept for callers that read as "the data directory"."""
    return config_directory()


def default_database_path() -> Path:
    return config_directory() / "samurai.db"


def vault_key_path() -> Path:
    return config_directory() / "vault.key"


def sandbox_allowlist_path() -> Path:
    return config_directory() / "sandbox-allowlist.sig"


def legacy_directory(start: Path | None = None) -> Path | None:
    """The old ``.samurai`` next to the working directory, when one is left over."""
    candidate = (start or Path.cwd()) / LEGACY_NAME
    return candidate if (candidate / "samurai.db").is_file() else None


def is_overridden() -> bool:
    """Whether the operator chose the directory, so a legacy hint would be noise."""
    return bool(os.environ.get("SAMURAI_HOME") or os.environ.get("SAMURAI_CONFIG_HOME"))
