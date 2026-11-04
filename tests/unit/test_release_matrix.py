"""The release matrix and the installer must agree on asset names.

The installer builds `samurai-<os>-<arch>` from `uname`, so a platform missing in
the release silently falls back to a source build. These tests keep the two lists
honest instead of discovering the gap at tag time.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Intel Macs are served by the installer's source fallback instead of an asset
EXPECTED = {
    "linux-x86_64",
    "linux-arm64",
    "darwin-arm64",
    "windows-x86_64",
}


def workflow() -> str:
    return (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def test_every_desktop_platform_is_built() -> None:
    built = set(re.findall(r"artifact: samurai-([a-z0-9_]+(?:-[a-z0-9_]+)*)", workflow()))
    assert built == EXPECTED


def test_the_arm64_build_runs_under_qemu_with_binutils() -> None:
    text = workflow()
    assert "docker/setup-qemu-action" in text
    assert "platform: linux/arm64" in text
    # pyinstaller needs objdump inside the emulated container
    assert "apt-get install -y -qq binutils" in text


def test_no_target_arch_flag_is_passed_to_the_spec_build() -> None:
    # pyinstaller rejects --target-arch when a .spec file is given
    assert "--target-arch" not in workflow()


def test_the_installer_resolves_the_same_names() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'os="darwin"' in installer and 'os="linux"' in installer
    assert 'arch="arm64"' in installer and 'arch="x86_64"' in installer
    assert 'ASSET="samurai-${os}-${arch}"' in installer


def test_the_installer_documents_the_fallback_for_missing_assets() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "falling back to uv" in installer


def test_the_plain_build_step_runs_only_for_native_targets() -> None:
    """It used to run for cross-macos too, so the target-arch step collided with it."""
    text = workflow()
    plain = text.split("- name: Build standalone binary\n", 1)[1].split("\n", 1)[0]
    assert "startsWith(matrix.kind, 'native-')" in plain


def test_the_container_build_hands_the_output_back() -> None:
    """The QEMU build runs as root; without a chown the packaging step cannot write."""
    text = workflow()
    assert 'sudo chown -R "$(id -u):$(id -g)" dist build' in text
