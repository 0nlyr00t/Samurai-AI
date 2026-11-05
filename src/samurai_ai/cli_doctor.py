"""`samurai doctor`: is this machine able to run the harness at all.

Setup failures surface as one bad command at a time; the doctor asks them all at
once, in the order an operator would fix them, with the command for this OS. Only
blockers set a non-zero exit, so it works as a setup gate in a script.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from samurai_ai.config import (
    config_directory,
    default_database_path,
    sandbox_allowlist_path,
    vault_key_path,
)
from samurai_ai.services.tool_keys import CATALOGUE
from samurai_ai.services.vault import VaultService, load_master_key
from samurai_ai.storage.database import Database
from samurai_ai.storage.vault import VaultRepository

#: profiles whose image must be pulled before the first tool run
SAMPLE_PROFILES = ("sandbox.default@v1", "sandbox.scoped@v1", "sandbox.httpx@v1")


def register_doctor_command(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    doctor = subcommands.add_parser("doctor", help="Check that this machine can run the harness")
    doctor.set_defaults(handler=_doctor)


def _os_hint(package_manager_commands: dict[str, str]) -> str:
    """The install command for the system in front of us, or the source hint."""
    if shutil.which("brew"):
        return package_manager_commands.get("brew", "")
    for manager in ("apt-get", "dnf", "pacman", "apk"):
        if shutil.which(manager):
            return package_manager_commands.get(manager, "")
    return package_manager_commands.get("other", "")


def _run(argv: list[str], timeout: float = 10.0) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return done.returncode, (done.stdout or done.stderr).strip()


def _binary(name: str, why: str, hints: dict[str, str], *, blocker: bool) -> dict[str, object]:
    found = shutil.which(name) is not None
    return {
        "name": name,
        "ok": found,
        "blocker": blocker,
        "detail": why if not found else shutil.which(name) or "",
        "hint": "" if found else _os_hint(hints),
    }


def _checks(deep: bool) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = [
        _binary(
            "docker",
            "sandboxes and every container tool run through it",
            {
                "brew": "brew install --cask docker",
                "apt-get": "sudo apt-get install docker.io",
                "dnf": "sudo dnf install docker-ce",
                "pacman": "sudo pacman -S docker",
                "other": "https://docs.docker.com/engine/install/",
            },
            blocker=True,
        )
    ]
    code, detail = _run(["docker", "version", "--format", "{{.Server.Version}}"])
    checks.append(
        {
            "name": "docker daemon",
            "ok": code == 0,
            "blocker": True,
            "detail": f"server {detail}" if code == 0 else "daemon is not answering",
            "hint": "" if code == 0 else "start Docker Desktop or `sudo systemctl start docker`",
        }
    )
    optional = (
        ("ssh", "remote daemons; optional"),
        ("cosign", "release signature checks; optional"),
    )
    for name, why in optional:
        checks.append(
            _binary(
                name,
                why,
                {
                    "brew": f"brew install {name}",
                    "apt-get": f"sudo apt-get install {name}",
                    "dnf": f"sudo dnf install {name}",
                    "pacman": f"sudo pacman -S {name}",
                },
                blocker=False,
            )
        )

    config = config_directory()
    writable = config.exists() and os.access(config, os.W_OK)
    checks.append(
        {
            "name": "state directory",
            "ok": writable,
            "blocker": True,
            "detail": str(config),
            "hint": "" if writable else f"create it: mkdir -p {config}",
        }
    )
    if config.exists():
        mode = stat.S_IMODE(config.stat().st_mode)
        private = not mode & (stat.S_IRWXG | stat.S_IRWXO)
        checks.append(
            {
                "name": "state directory permissions",
                "ok": private,
                "blocker": False,
                "detail": f"{mode:o}",
                "hint": "" if private else f"chmod 700 {config}",
            }
        )

    allowlist = sandbox_allowlist_path()
    checks.append(
        {
            "name": "sandbox allowlist signature",
            "ok": allowlist.exists(),
            "blocker": False,
            "detail": "signed" if allowlist.exists() else "not signed yet",
            "hint": "" if allowlist.exists() else "samurai sandbox sign",
        }
    )

    database = default_database_path()
    checks.append(
        {
            "name": "database",
            "ok": database.exists(),
            "blocker": False,
            "detail": str(database),
            "hint": "" if database.exists() else "created on first run",
        }
    )

    if deep and shutil.which("docker"):
        from samurai_ai.execution.sandbox import get_profile

        missing = []
        for profile_id in SAMPLE_PROFILES:
            try:
                image = get_profile(profile_id).image
            except Exception:  # noqa: BLE001 - a missing profile is not a doctor failure
                continue
            if _run(["docker", "image", "inspect", image])[0] != 0:
                missing.append(image)
        checks.append(
            {
                "name": "pinned images",
                "ok": not missing,
                "blocker": False,
                "detail": "present" if not missing else f"{len(missing)} not pulled",
                "hint": "" if not missing else f"docker pull {missing[0]}",
            }
        )
    return checks


def _key_state() -> tuple[int, int]:
    """(keys set, catalogue size) — values never leave the vault."""
    path = default_database_path()
    if not path.exists():
        return 0, len(CATALOGUE)
    try:
        database = Database(path)
        vault = VaultService(VaultRepository(database), None, vault_key_path())  # type: ignore[arg-type]
        set_keys = sum(1 for entry in CATALOGUE if vault.exists(entry.env_name.lower()))
    except Exception:  # noqa: BLE001 - an unreadable vault is reported elsewhere
        return 0, len(CATALOGUE)
    return set_keys, len(CATALOGUE)


def _doctor(args: argparse.Namespace) -> int:
    checks = _checks(deep=not getattr(args, "shallow", False))
    for check in checks:
        mark = "ok  " if check["ok"] else ("FAIL" if check["blocker"] else "warn")
        print(f"{mark} {check['name']}\t{check['detail']}")
        if check["hint"]:
            print(f"     -> {check['hint']}")
    blockers = [check for check in checks if not check["ok"] and check["blocker"]]
    set_keys, total = _key_state()
    print(f"keys\t{set_keys}/{total} set in /settings (optional)")

    if blockers:
        print(f"\n{len(blockers)} blocker(s) — the harness cannot run until they are fixed")
        return 2
    warnings = [check for check in checks if not check["ok"]]
    if warnings:
        print(f"\nusable, with {len(warnings)} warning(s)")
        return 1
    print("\nall checks passed")
    return 0


__all__ = ["register_doctor_command"]


_ = (load_master_key, Path, sys)
