"""The installer and the uninstaller, run for real in a throwaway HOME.

Every external command is a stub on PATH, so the tests describe the contract:
which asset is fetched, what is verified, and what a removal leaves behind.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def host_asset() -> str:
    """The installer resolves the asset from uname; the fixture must match it."""
    os_name = {"Darwin": "darwin", "Linux": "linux"}[os.uname().sysname]
    machine = os.uname().machine
    arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
    return f"samurai-{os_name}-{arch}"

CURL_STUB = """#!/usr/bin/env bash
set -e
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -H|--retry) shift 2 ;;
    -fsSL|-fsSI|-LsSf) shift ;;
    *) url="$1"; shift ;;
  esac
done
case "$url" in
  */releases/latest) echo '{"tag_name": "v9.9.9"}'; exit 0 ;;
esac
src="$FAKE_RELEASE/${url##*/}"
[ -f "$src" ] || exit 22
if [ -n "$out" ]; then cp "$src" "$out"; else cat "$src"; fi
"""

GIT_STUB = """#!/usr/bin/env bash
printf 'abc\\trefs/tags/v9.9.9\\n'
"""

SHA_STUB = """#!/usr/bin/env bash
exec shasum -a 256 "$@"
"""

UV_STUB = """#!/usr/bin/env bash
echo "uv $*" >> "$HOME/uv.log"
case "$1 $2" in
  "tool list") [ -f "$HOME/uv-installed" ] && echo "samurai v0.1.0" ;;
  "tool install") touch "$HOME/uv-installed" ;;
  "tool uninstall") rm -f "$HOME/uv-installed" ;;
esac
"""

COSIGN_STUB = """#!/usr/bin/env bash
echo "cosign $*" >> "$HOME/cosign.log"
[ -n "${COSIGN_FAIL:-}" ] && exit 1
exit 0
"""

DOCKER_STUB = """#!/usr/bin/env bash
echo "docker $*" >> "$HOME/docker.log"
case "$1 $2" in
  "image inspect") [ -n "${FAKE_IMAGES:-}" ] || exit 1 ;;
esac
exit 0
"""


@pytest.fixture
def stand(tmp_path: Path):
    bin_dir = tmp_path / "stubbin"
    home = tmp_path / "home"
    release = tmp_path / "release"
    for path in (bin_dir, home / ".local" / "bin", release):
        path.mkdir(parents=True, exist_ok=True)
    for name, body in (
        ("curl", CURL_STUB),
        ("git", GIT_STUB),
        ("sha256sum", SHA_STUB),
        ("uv", UV_STUB),
        ("cosign", COSIGN_STUB),
        ("docker", DOCKER_STUB),
    ):
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    binary = release / host_asset()
    binary.write_text("#!/bin/sh\necho samurai 9.9.9\n")
    digest = subprocess.run(
        ["shasum", "-a", "256", binary.name], cwd=release, capture_output=True, text=True
    ).stdout
    (release / f"{binary.name}.sha256").write_text(digest)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_RELEASE": str(release),
        "SHELL": "/bin/zsh",
    }
    (home / ".zshrc").write_text("# shell config\n")
    return {"home": home, "release": release, "env": env, "stub": bin_dir}


def run(script: str, stand, *args: str, **extra: str):
    env = {**stand["env"], "SAMURAI_OS": "", **extra}
    return subprocess.run(
        ["bash", str(ROOT / script), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_installer_puts_a_verified_binary_in_place(stand) -> None:
    done = run("install.sh", stand)
    assert done.returncode == 0, done.stderr
    assert "9.9.9" in done.stdout
    binary = stand["home"] / ".local" / "bin" / "samurai"
    assert binary.exists() and os.access(binary, os.X_OK)


def test_a_corrupted_download_is_refused(stand) -> None:
    (stand["release"] / host_asset()).write_text("tampered")
    done = run("install.sh", stand)
    assert done.returncode == 1
    assert "checksum mismatch" in done.stderr
    assert not (stand["home"] / ".local" / "bin" / "samurai").exists()


def test_the_path_block_is_written_once(stand) -> None:
    run("install.sh", stand)
    run("install.sh", stand)
    rc = (stand["home"] / ".zshrc").read_text()
    assert rc.count("# >>> samurai >>>") == 1
    assert ".local/bin" in rc


def test_a_missing_asset_falls_back_to_uv(stand) -> None:
    (stand["release"] / host_asset()).unlink()
    done = run("install.sh", stand)
    assert done.returncode == 0
    assert "falling back to uv" in done.stderr
    assert "tool install" in (stand["home"] / "uv.log").read_text()


def test_cosign_is_used_when_it_is_there(stand) -> None:
    (stand["release"] / f"{host_asset()}.bundle").write_text("bundle")
    done = run("install.sh", stand)
    assert done.returncode == 0
    arguments = (stand["home"] / "cosign.log").read_text()
    assert "verify-blob" in arguments and host_asset() in arguments


def test_a_failed_signature_stops_the_install(stand) -> None:
    (stand["release"] / f"{host_asset()}.bundle").write_text("bundle")
    done = run("install.sh", stand, COSIGN_FAIL="1")
    assert done.returncode == 1
    assert "signature check failed" in done.stderr
    assert not (stand["home"] / ".local" / "bin" / "samurai").exists()


def test_dry_run_leaves_nothing_behind(stand) -> None:
    done = run("install.sh", stand, "--dry-run")
    assert done.returncode == 0
    assert "dry run" in done.stdout
    assert not (stand["home"] / ".local" / "bin" / "samurai").exists()
    assert "# >>> samurai" not in (stand["home"] / ".zshrc").read_text()


def test_an_unknown_option_is_rejected(stand) -> None:
    assert run("install.sh", stand, "--nope").returncode == 2


def populated(stand) -> None:
    run("install.sh", stand)
    state = stand["home"] / ".config" / "samurai"
    state.mkdir(parents=True, exist_ok=True)
    (state / "samurai.db").write_text("db")


def test_the_uninstaller_removes_everything_by_default(stand) -> None:
    populated(stand)
    done = run("uninstall.sh", stand, "--yes")
    assert done.returncode == 0, done.stderr
    assert not (stand["home"] / ".local" / "bin" / "samurai").exists()
    assert not (stand["home"] / ".config" / "samurai").exists()
    assert "# >>> samurai" not in (stand["home"] / ".zshrc").read_text()


def test_keep_state_keeps_the_database(stand) -> None:
    populated(stand)
    done = run("uninstall.sh", stand, "--yes", "--keep-state")
    assert done.returncode == 0
    assert (stand["home"] / ".config" / "samurai" / "samurai.db").exists()
    assert not (stand["home"] / ".local" / "bin" / "samurai").exists()


def test_the_uninstaller_survives_being_run_twice(stand) -> None:
    populated(stand)
    assert run("uninstall.sh", stand, "--yes").returncode == 0
    again = run("uninstall.sh", stand, "--yes")
    assert again.returncode == 0
    assert "nothing to remove" in again.stdout


def test_the_uninstaller_removes_belt_images(stand) -> None:
    populated(stand)
    done = run("uninstall.sh", stand, "--yes", FAKE_IMAGES="1")
    assert done.returncode == 0
    log = (stand["home"] / "docker.log").read_text()
    assert "image rm ghcr.io/0nlyr00t/samurai-remote" in log


def test_the_uninstaller_keeps_an_unrelated_profile(stand) -> None:
    (stand["home"] / ".zshrc").write_text("# mine\nexport EDITOR=vim\n")
    populated(stand)
    run("uninstall.sh", stand, "--yes")
    assert (stand["home"] / ".zshrc").read_text().startswith("# mine")
