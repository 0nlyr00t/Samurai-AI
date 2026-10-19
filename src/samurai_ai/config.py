import os
from pathlib import Path


def data_directory() -> Path:
    configured = os.environ.get("SAMURAI_HOME", ".samurai")
    return Path(configured).expanduser().resolve()


def default_database_path() -> Path:
    return data_directory() / "samurai.db"


def vault_key_path() -> Path:
    return data_directory() / "vault.key"


def sandbox_allowlist_path() -> Path:
    return data_directory() / "sandbox-allowlist.sig"
