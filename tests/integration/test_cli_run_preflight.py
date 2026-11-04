"""`run` refuses to spend a run on a dead target unless told otherwise."""

from pathlib import Path

from samurai_ai.cli import main


def test_a_dead_target_stops_the_run(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "pre.db")
    assert main(["--db", database, "run", "Box", "--mode", "ctf-htb",
                 "--include", "no-such-host.invalid", "--task", "Recon",
                 "--model", "ghost/model@v1"]) == 2
    err = capsys.readouterr().err
    assert "does not answer" in err
    assert "no target answers" in err


def test_no_preflight_goes_on(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "pre.db")
    code = main(["--db", database, "run", "Box", "--mode", "ctf-htb",
                 "--include", "no-such-host.invalid", "--task", "Recon",
                 "--model", "ghost/model@v1", "--no-preflight"])
    # the operation is built and the run fails later on the unknown model
    assert code in (1, 2)
    assert "no target answers" not in capsys.readouterr().err
