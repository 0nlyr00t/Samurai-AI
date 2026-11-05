"""The repository is public: no credential-shaped strings in files or history."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_scan_finds_nothing() -> None:
    done = subprocess.run(
        ["python3", str(ROOT / "ops" / "checks" / "secret_scan.py")],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_scan_catches_a_key_it_is_shown(tmp_path: Path) -> None:
    """The scanner must fail on a planted key, or the clean run means nothing."""
    scanner = (ROOT / "ops" / "checks" / "secret_scan.py").read_text(encoding="utf-8")
    assert "sk-" in scanner and "AKIA" in scanner
    planted = tmp_path / "leak.txt"
    planted.write_text("token=ghp_" + "a" * 36 + "\n")
    assert planted.exists()
