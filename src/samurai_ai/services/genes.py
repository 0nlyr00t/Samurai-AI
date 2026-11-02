"""Gene pool: shadow → fitness → operator gate → active, with rollback."""

from __future__ import annotations

import json
import os
from pathlib import Path

from samurai_ai.domain.genes import (
    Gene,
    GeneError,
    GeneFitness,
    GeneGateError,
    GeneKind,
    GeneStatus,
)


class GeneStore:
    """One JSON document; the pool is small and rewritten atomically.

    Genes are decision-layer text, not evidence, so a file is enough and the
    database schema stays out of tonight's blast radius.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[Gene]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        genes: list[Gene] = []
        for item in raw if isinstance(raw, list) else ():
            try:
                fitness = GeneFitness(
                    samples=int(item.get("samples", 0)),
                    median_solve_rate=float(item.get("median_solve_rate", 0.0)),
                    median_invalid_actions=float(item.get("median_invalid_actions", 0.0)),
                    median_cost_usd=float(item.get("median_cost_usd", 0.0)),
                )
                genes.append(
                    Gene(
                        slot=str(item["slot"]),
                        kind=GeneKind(item["kind"]),
                        title=str(item["title"]),
                        body=str(item["body"]),
                        id=str(item["id"]),
                        version=int(item.get("version", 1)),
                        status=GeneStatus(item.get("status", "shadow")),
                        fitness=fitness,
                        notes=str(item.get("notes", "")),
                    )
                )
            except (KeyError, ValueError):
                continue
        return genes

    def save(self, genes: list[Gene]) -> None:
        payload = [
            {
                "id": gene.id,
                "slot": gene.slot,
                "kind": gene.kind.value,
                "title": gene.title,
                "body": gene.body,
                "version": gene.version,
                "status": gene.status.value,
                "samples": gene.fitness.samples,
                "median_solve_rate": gene.fitness.median_solve_rate,
                "median_invalid_actions": gene.fitness.median_invalid_actions,
                "median_cost_usd": gene.fitness.median_cost_usd,
                "notes": gene.notes,
            }
            for gene in genes
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)
        temp.replace(self.path)


class GeneService:
    def __init__(self, store: GeneStore) -> None:
        self.store = store

    def list_genes(self, status: GeneStatus | None = None) -> list[Gene]:
        genes = self.store.load()
        return [gene for gene in genes if status is None or gene.status is status]

    def active(self, slot: str) -> Gene | None:
        return next(
            (
                gene
                for gene in self.store.load()
                if gene.slot == slot and gene.status is GeneStatus.ACTIVE
            ),
            None,
        )

    def create(
        self, slot: str, kind: GeneKind | str, title: str, body: str, notes: str = ""
    ) -> Gene:
        genes = self.store.load()
        version = 1 + max(
            (gene.version for gene in genes if gene.slot == slot), default=0
        )
        gene = Gene(
            slot=slot, kind=GeneKind(kind), title=title, body=body, version=version, notes=notes
        )
        genes.append(gene)
        self.store.save(genes)
        return gene

    def record_fitness(
        self,
        gene_id: str,
        *,
        samples: int,
        median_solve_rate: float,
        median_invalid_actions: float = 0.0,
        median_cost_usd: float = 0.0,
    ) -> Gene:
        genes = self.store.load()
        gene = self._find(genes, gene_id)
        gene.fitness = GeneFitness(
            samples=samples,
            median_solve_rate=median_solve_rate,
            median_invalid_actions=median_invalid_actions,
            median_cost_usd=median_cost_usd,
        )
        self.store.save(genes)
        return gene

    def promote(self, gene_id: str) -> Gene:
        """The gate: comparable fitness, and no worse than the current active."""
        genes = self.store.load()
        gene = self._find(genes, gene_id)
        gene.promote()
        current = next(
            (
                item
                for item in genes
                if item.slot == gene.slot
                and item.status is GeneStatus.ACTIVE
                and item.id != gene.id
            ),
            None,
        )
        if current is not None:
            if current.fitness.comparable and not gene.fitness.better_than(current.fitness):
                raise GeneGateError(
                    f"gene {gene.id} is not better than the active {current.id} "
                    f"({gene.fitness.median_solve_rate} vs {current.fitness.median_solve_rate})"
                )
            current.status = GeneStatus.SHADOW
            gene.notes = (gene.notes + f"\nreplaced {current.id}").strip()
        self.store.save(genes)
        return gene

    def rollback(self, slot: str) -> Gene | None:
        """Retire the active gene and reactivate the previous one."""
        genes = self.store.load()
        current = next(
            (item for item in genes if item.slot == slot and item.status is GeneStatus.ACTIVE),
            None,
        )
        if current is None:
            return None
        current.retire()
        previous = next(
            (
                item
                for item in reversed(genes)
                if item.slot == slot
                and item.status is GeneStatus.SHADOW
                and item.id != current.id
            ),
            None,
        )
        if previous is not None:
            previous.status = GeneStatus.ACTIVE
        self.store.save(genes)
        return previous

    @staticmethod
    def _find(genes: list[Gene], gene_id: str) -> Gene:
        for gene in genes:
            if gene.id == gene_id:
                return gene
        raise GeneError(f"gene not found: {gene_id}")


__all__ = ["GeneService", "GeneStore", "GeneGateError", "GeneError"]
