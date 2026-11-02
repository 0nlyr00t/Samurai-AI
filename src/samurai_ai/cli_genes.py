"""`samurai gene`: the evolving prompt/policy pool with its gate."""

from __future__ import annotations

import argparse
import sys

from samurai_ai.config import genes_path
from samurai_ai.domain.genes import GeneError, GeneGateError, GeneKind, GeneStatus
from samurai_ai.services.genes import GeneService, GeneStore


def register_gene_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    gene = subcommands.add_parser("gene", help="Inspect and gate evolving prompt genes")
    commands = gene.add_subparsers(dest="gene_command", required=True)
    listing = commands.add_parser("list", help="List genes by status")
    listing.add_argument("--status", choices=[s.value for s in GeneStatus])
    add = commands.add_parser("add", help="Add a gene (born in shadow)")
    add.add_argument("slot")
    add.add_argument("title")
    add.add_argument("body")
    add.add_argument(
        "--kind", choices=[k.value for k in GeneKind], default=GeneKind.PROMPT_BLOCK.value
    )
    fitness = commands.add_parser("fitness", help="Record eval fitness for a gene")
    fitness.add_argument("gene_id")
    fitness.add_argument("--samples", type=int, required=True)
    fitness.add_argument("--solve-rate", type=float, required=True)
    fitness.add_argument("--invalid-actions", type=float, default=0.0)
    fitness.add_argument("--cost-usd", type=float, default=0.0)
    promote = commands.add_parser("promote", help="Pass the gate and activate a gene")
    promote.add_argument("gene_id")
    rollback = commands.add_parser("rollback", help="Retire the active gene, restore previous")
    rollback.add_argument("slot")


def handle_gene_command(args: argparse.Namespace) -> int | None:
    if args.command != "gene":
        return None
    service = GeneService(GeneStore(genes_path()))
    try:
        if args.gene_command == "list":
            status = GeneStatus(args.status) if args.status else None
            genes = service.list_genes(status)
            if not genes:
                print("no genes")
                return 0
            for gene in genes:
                print(
                    f"{gene.id}\t{gene.slot}\tv{gene.version}\t{gene.status.value}\t"
                    f"solve={gene.fitness.median_solve_rate}\tsamples={gene.fitness.samples}"
                )
            return 0
        if args.gene_command == "add":
            gene = service.create(args.slot, args.kind, args.title, args.body)
            print(f"added\t{gene.id}\tv{gene.version}")
            return 0
        if args.gene_command == "fitness":
            service.record_fitness(
                args.gene_id,
                samples=args.samples,
                median_solve_rate=args.solve_rate,
                median_invalid_actions=args.invalid_actions,
                median_cost_usd=args.cost_usd,
            )
            print(f"fitness\t{args.gene_id}")
            return 0
        if args.gene_command == "promote":
            gene = service.promote(args.gene_id)
            print(f"active\t{gene.id}\t{gene.slot}")
            return 0
        if service.active(args.slot) is None:
            print(f"nothing active in slot {args.slot}", file=sys.stderr)
            return 1
        restored = service.rollback(args.slot)
        print(f"retired\t{args.slot}\trestored={restored.id if restored else '-'}")
        return 0
    except (GeneError, GeneGateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
