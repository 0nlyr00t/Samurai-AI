"""Gene pool: shadow by default, gate on comparable fitness, rollback."""

from pathlib import Path

import pytest

from samurai_ai.domain.genes import GeneError, GeneGateError, GeneKind, GeneStatus
from samurai_ai.services.genes import GeneService, GeneStore


def make_service(tmp_path: Path) -> GeneService:
    return GeneService(GeneStore(tmp_path / "genes.json"))


def activated(service: GeneService, slot: str, body: str, rate: float, invalid: float = 0.0):
    gene = service.create(slot, GeneKind.PROMPT_BLOCK, slot + " gene", body)
    service.record_fitness(
        gene.id, samples=3, median_solve_rate=rate, median_invalid_actions=invalid
    )
    return gene


def test_a_new_gene_is_shadow_and_needs_a_slot(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    gene = service.create("planner", GeneKind.PROMPT_BLOCK, "Planner hint", "look at headers")
    assert gene.status is GeneStatus.SHADOW
    assert service.active("planner") is None
    with pytest.raises(GeneError, match="kebab-case"):
        service.create("Planner!", GeneKind.PROMPT_BLOCK, "x", "y")


def test_the_gate_needs_comparable_fitness(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    gene = service.create("planner", GeneKind.PROMPT_BLOCK, "Planner hint", "look at headers")
    service.record_fitness(gene.id, samples=1, median_solve_rate=1.0)
    with pytest.raises(GeneGateError, match="samples"):
        service.promote(gene.id)


def test_a_better_gene_replaces_the_active_one(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    first = activated(service, "planner", "old body", 0.5)
    service.promote(first.id)
    assert service.active("planner").id == first.id  # type: ignore[union-attr]

    better = activated(service, "planner", "new body", 0.9)
    service.promote(better.id)
    assert service.active("planner").id == better.id  # type: ignore[union-attr]
    assert first.status is GeneStatus.SHADOW


def test_a_worse_gene_is_refused(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    first = activated(service, "planner", "old body", 0.9)
    service.promote(first.id)
    worse = activated(service, "planner", "new body", 0.5)
    with pytest.raises(GeneGateError, match="not better"):
        service.promote(worse.id)
    assert service.active("planner").id == first.id  # type: ignore[union-attr]


def test_rollback_returns_the_previous_gene(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    first = activated(service, "planner", "old body", 0.5)
    service.promote(first.id)
    second = activated(service, "planner", "new body", 0.9)
    service.promote(second.id)

    restored = service.rollback("planner")
    assert restored is not None and restored.id == first.id
    assert service.active("planner").id == first.id  # type: ignore[union-attr]
    statuses = {gene.id: gene.status for gene in service.list_genes()}
    assert statuses[second.id] is GeneStatus.RETIRED
    assert service.rollback("empty-slot") is None


def test_versions_increment_per_slot(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.create("planner", GeneKind.PROMPT_BLOCK, "one", "a")
    second = service.create("planner", GeneKind.PROMPT_BLOCK, "two", "b")
    assert second.version == 2


def test_the_pool_survives_a_reload(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    activated(service, "planner", "body", 0.7)
    again = GeneService(GeneStore(tmp_path / "genes.json"))
    assert [gene.slot for gene in again.list_genes()] == ["planner"]
