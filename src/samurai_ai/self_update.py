"""Self-update for the samurai CLI.

The latest released version is discovered with ``git ls-remote --tags``
(which works for private repositories through the operator's git credential
helper — no GitHub API token needed) and the update itself is delegated to
``uv tool install`` from the git tag, so the same credentials apply.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/0nlyr00t/Samurai-AI.git"
UV_INSTALL_HINT = "install uv first: curl -LsSf https://astral.sh/uv/install.sh | sh"

_TAG_LINE = re.compile(r"refs/tags/v(\d+\.\d+\.\d+)$")


def version_key(version: str) -> tuple[int, int, int]:
    parts = version.lstrip("v").split(".")
    try:
        major, minor, patch = (int(part) for part in parts[:3])
    except ValueError:
        return (0, 0, 0)
    return (major, minor, patch)


def parse_latest_tag(ls_remote_output: str) -> str | None:
    """Pick the highest vX.Y.Z tag from ``git ls-remote --tags`` output."""
    versions = []
    for line in ls_remote_output.splitlines():
        if line.endswith("^{}"):  # annotated-tag deref lines duplicate the tag
            continue
        match = _TAG_LINE.search(line.strip())
        if match:
            versions.append(match.group(1))
    if not versions:
        return None
    return max(versions, key=version_key)


def is_newer(latest: str, current: str) -> bool:
    return version_key(latest) > version_key(current)


def latest_tag(repo_url: str = REPO_URL, timeout: float = 30.0) -> str | None:
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", repo_url],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return parse_latest_tag(result.stdout)


def install_tag(
    tag: str, repo_url: str = REPO_URL
) -> subprocess.CompletedProcess[bytes]:
    uv = shutil.which("uv")
    if uv is None:
        raise FileNotFoundError(UV_INSTALL_HINT)
    return subprocess.run(
        [uv, "tool", "install", "--force", "--refresh", f"git+{repo_url}@v{tag}"],
        check=False,
    )


def _uv_git_cache() -> Path | None:
    """uv's bare-clone cache (``<cache>/git-v0``), if uv can name its cache."""
    uv = shutil.which("uv")
    if uv is None:
        return None
    try:
        result = subprocess.run(
            [uv, "cache", "dir"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()) / "git-v0"


def clear_uv_git_cache() -> bool:
    """Drop uv's cached git clones.

    A history rewrite on the remote leaves that cache with dangling objects,
    and the next install dies deep inside git ("unable to read sha1 file").
    The cache is disposable by definition, so a failed install may clear it
    and retry once.
    """
    cache = _uv_git_cache()
    if cache is None or not cache.exists():
        return False
    shutil.rmtree(cache)
    return True


def run_update(check: bool = False) -> int:
    from samurai_ai import __version__

    if getattr(sys, "frozen", False):
        print(
            "this is a standalone binary build; re-run install.sh to update:\n"
            "  curl -fsSL https://raw.githubusercontent.com/0nlyr00t/Samurai-AI/"
            "main/install.sh | bash"
        )
        return 1
    print(f"current version: {__version__}")
    tag = latest_tag()
    if tag is None:
        print(
            "error: could not query release tags (check network and git access "
            f"to {REPO_URL})",
            file=sys.stderr,
        )
        return 2
    print(f"latest release:  {tag}")
    if check:
        if is_newer(tag, __version__):
            print("update available — run: samurai update")
            return 1
        print("up to date")
        return 0
    if not is_newer(tag, __version__):
        print("already up to date")
        return 0
    try:
        result = install_tag(tag)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if result.returncode != 0:
        # most often this is uv's stale git cache after a history rewrite
        if clear_uv_git_cache():
            print("note: cleared uv's git cache, retrying")
            result = install_tag(tag)
    if result.returncode != 0:
        print("error: uv tool install failed", file=sys.stderr)
        return 2
    print(f"updated to v{tag}")
    return 0
