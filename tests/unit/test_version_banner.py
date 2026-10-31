"""``--version`` prints the version, and nothing else."""

from __future__ import annotations

import pytest

from samurai_ai import __version__
from samurai_ai.cli import build_parser, print_version_banner


def test_version_banner_prints_the_name_and_the_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_version_banner()
    out = capsys.readouterr().out
    assert "samurai" in out
    assert __version__ in out
    assert len(out.strip().splitlines()) == 1


def test_version_flag_prints_the_banner(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--version"])
    assert __version__ in capsys.readouterr().out
