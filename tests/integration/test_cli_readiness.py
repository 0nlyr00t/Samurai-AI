import json
from pathlib import Path

from samurai_ai import cli
from samurai_ai.cli import main
from samurai_ai.domain.kernel import PlanTask
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.graph import GraphService
from samurai_ai.services.readiness import ReadinessService
from samurai_ai.storage.graph import GraphRepository


def test_readiness_covers_both_hypothesis_registries(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    services = cli._make_services(None)
    operations, kernel, ctf, goals = services[0], services[1], services[11], services[15]
    operation = operations.create(
        "Readiness lab", OperationMode.CTF_HTB, ScopeContract(includes=("127.0.0.1",))
    )
    kernel.create_plan(operation.id, "Plan", [PlanTask(id="recon", title="Recon")], [])
    graph = GraphService(operations, kernel, GraphRepository(operations.repository.database))
    root = graph.origin(operation.id, "recon", "Origin")
    intent = graph.open_intent(operation.id, "recon", "Test SSH", root.id)
    hypothesis = ctf.propose_hypothesis(operation.id, "Web is vulnerable")
    goal = goals.create(operation.id, "Finish with evidence")
    goals.complete(goal.id)
    readiness = ReadinessService(
        operations, kernel, services[4], ctf, graph, goals
    ).report(operation.id)

    assert readiness.open_ids == (intent.id, hypothesis.id)
    assert not readiness.hypotheses_complete
    assert not readiness.evidence_success
    assert main(["eval", "readiness", operation.id, "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["open_ids"]) == {intent.id, hypothesis.id}

    graph.close_intent(intent.id, "exhausted", "SSH answered no usable banner")
    ctf.update_hypothesis(hypothesis.id, "abandon", "The response disproved the vector")

    assert main(["eval", "readiness", operation.id, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hypotheses_complete"] is True
    assert payload["hypotheses_closed"] == 2
    assert payload["evidence_success"] is True
