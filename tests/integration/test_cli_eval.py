import json
from pathlib import Path

import pytest

from samurai_ai.cli import main


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    return tmp_path


def _write_suite(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "name": "cli",
                "version": "1.0.0",
                "scenarios": [
                    {
                        "id": "solo",
                        "title": "Single task",
                        "scope_includes": ["example.com"],
                        "tasks": [{"id": "recon", "title": "Recon"}],
                        "script": {
                            "responses": [
                                '{"steps": ["probe it"]}',
                                '{"commands": [{"type": "complete_task"}]}',
                            ]
                        },
                        "expectations": {"max_invalid_actions": 0},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_eval_run_stdout_and_exit_zero(home: Path, capsys) -> None:
    suite = home / "suite.json"
    _write_suite(suite)
    assert main(["eval", "run", str(suite)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["suite"] == "cli"
    assert report["aggregate"]["passed"] == 1
    assert report["results"][0]["metrics"]["solved"] is True


def test_eval_run_out_file_and_failure_exit_one(home: Path, capsys) -> None:
    suite = home / "suite.json"
    _write_suite(suite)
    raw = json.loads(suite.read_text(encoding="utf-8"))
    raw["scenarios"][0]["expectations"] = {"min_tasks_done": 5}
    suite.write_text(json.dumps(raw), encoding="utf-8")
    out = home / "report.json"
    assert main(["eval", "run", str(suite), "--out", str(out)]) == 1
    captured = capsys.readouterr()
    assert "min_tasks_done" in captured.err
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["results"][0]["status"] == "failed"


def test_eval_run_invalid_suite_exit_two(home: Path, capsys) -> None:
    bad = home / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert main(["eval", "run", str(bad)]) == 2
    assert "invalid suite" in capsys.readouterr().err


def test_eval_compare_exit_codes(home: Path, capsys) -> None:
    suite = home / "suite.json"
    _write_suite(suite)
    baseline = home / "baseline.json"
    assert main(["eval", "run", str(suite), "--out", str(baseline)]) == 0
    capsys.readouterr()

    assert main(["eval", "compare", str(baseline), str(baseline)]) == 0
    comparison = json.loads(capsys.readouterr().out)
    assert comparison["regressions"] == []

    degraded = json.loads(baseline.read_text(encoding="utf-8"))
    degraded["results"][0]["metrics"]["solved"] = False
    current = home / "current.json"
    current.write_text(json.dumps(degraded), encoding="utf-8")
    assert main(["eval", "compare", str(baseline), str(current)]) == 1
    assert "solve regressed" in capsys.readouterr().err

    assert (
        main(["eval", "compare", str(baseline), str(home / "missing.json")]) == 2
    )
    assert "cannot read report" in capsys.readouterr().err
