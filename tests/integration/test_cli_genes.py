"""`samurai gene`: the gate is enforced from the CLI too."""

import json
from pathlib import Path

import pytest

from samurai_ai.cli import main
from samurai_ai.config import genes_path
from samurai_ai.services.genes import GeneService, GeneStore


def use_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # monkeypatch, not os.environ: a leaked home breaks the TUI theme tests
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))


def only_gene_id() -> str:
    genes = GeneService(GeneStore(genes_path())).list_genes()
    assert len(genes) == 1
    return genes[0].id


def test_the_gate_refuses_a_gene_without_fitness(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_home(monkeypatch, tmp_path)
    assert main(["gene", "add", "planner", "Hint", "look at headers"]) == 0
    assert main(["gene", "promote", only_gene_id()]) == 2
    assert "samples" in capsys.readouterr().err


def test_a_gene_is_gated_then_rolled_back(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_home(monkeypatch, tmp_path)
    assert main(["gene", "add", "planner", "Hint", "body"]) == 0
    gene_id = only_gene_id()
    service = GeneService(GeneStore(genes_path()))
    service.record_fitness(gene_id, samples=4, median_solve_rate=0.7)

    assert main(["gene", "promote", gene_id]) == 0
    assert service.active("planner") is not None
    assert main(["gene", "rollback", "planner"]) == 0
    assert service.active("planner") is None
    assert "retired" in capsys.readouterr().out


def test_rollback_without_anything_active_is_an_error(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_home(monkeypatch, tmp_path)
    assert main(["gene", "rollback", "planner"]) == 1
    assert "nothing active" in capsys.readouterr().err


def test_fitness_can_come_from_an_eval_report(tmp_path: Path, capsys, monkeypatch) -> None:
    use_home(monkeypatch, tmp_path)
    assert main(["gene", "add", "planner", "Hint", "body"]) == 0
    gene_id = only_gene_id()
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "config": {"repeats": 2},
                "aggregate": {"solve_rate": 0.5, "total_invalid_actions": 1},
                "results": [{}, {}],
            }
        )
    )
    assert main(["gene", "fitness", gene_id, "--report", str(report)]) == 0
    service = GeneService(GeneStore(genes_path()))
    fitness = service.list_genes()[0].fitness
    assert fitness.samples == 4 and fitness.median_solve_rate == 0.5
    assert main(["gene", "promote", gene_id]) == 0


def test_fitness_needs_numbers_or_a_report(tmp_path: Path, capsys, monkeypatch) -> None:
    use_home(monkeypatch, tmp_path)
    assert main(["gene", "add", "planner", "Hint", "body"]) == 0
    assert main(["gene", "fitness", only_gene_id()]) == 2
    assert "report" in capsys.readouterr().err
