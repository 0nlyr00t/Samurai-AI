from __future__ import annotations

import argparse
import json
from pathlib import Path

from samurai_ai.domain.ctf import ChallengeCategory, HypothesisStatus
from samurai_ai.domain.reports import ReportFormat
from samurai_ai.services.ctf import CtfService


def register_ctf_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    challenge = subcommands.add_parser("challenge", help="Manage the CTF/HTB challenge")
    challenge_commands = challenge.add_subparsers(dest="challenge_command", required=True)
    init = challenge_commands.add_parser(
        "init", help="Initialize the CTF/HTB playbook for an operation"
    )
    init.add_argument("operation_id")
    init.add_argument("--name", required=True)
    init.add_argument(
        "--category",
        required=True,
        choices=[item.value for item in ChallengeCategory],
    )
    init.add_argument("--description", default="")
    init.add_argument("--win-condition", default="")
    init.add_argument("--flag-format", default="")
    show = challenge_commands.add_parser("show", help="Show the challenge")
    show.add_argument("operation_id")
    summary = challenge_commands.add_parser("summary", help="Show challenge progress")
    summary.add_argument("operation_id")

    hypothesis = subcommands.add_parser("hypothesis", help="Manage the hypothesis log")
    hypothesis_commands = hypothesis.add_subparsers(dest="hypothesis_command", required=True)
    hyp_add = hypothesis_commands.add_parser("add", help="Propose a hypothesis")
    hyp_add.add_argument("operation_id")
    hyp_add.add_argument("title")
    hyp_add.add_argument(
        "--category",
        default=None,
        choices=[item.value for item in ChallengeCategory],
    )
    hyp_add.add_argument("--notes", default="")
    hyp_update = hypothesis_commands.add_parser(
        "update", help="Transition a hypothesis"
    )
    hyp_update.add_argument("hypothesis_id")
    hyp_update.add_argument(
        "--status",
        required=True,
        choices=["testing", "confirmed", "refuted", "abandoned"],
    )
    hyp_update.add_argument("--notes", default="")
    hyp_list = hypothesis_commands.add_parser("list", help="List hypotheses")
    hyp_list.add_argument("operation_id")
    hyp_list.add_argument(
        "--status",
        default=None,
        choices=[item.value for item in HypothesisStatus],
    )

    flag = subcommands.add_parser("flag", help="Manage the flag ledger")
    flag_commands = flag.add_subparsers(dest="flag_command", required=True)
    flag_add = flag_commands.add_parser("add", help="Record a captured flag")
    flag_add.add_argument("operation_id")
    flag_add.add_argument("--name", required=True)
    flag_add.add_argument("--value", required=True)
    flag_add.add_argument("--proof", default=None, help="proof artifact id")
    flag_list = flag_commands.add_parser("list", help="List captured flags")
    flag_list.add_argument("operation_id")

    writeup = subcommands.add_parser("writeup", help="Manage CTF/HTB writeups")
    writeup_commands = writeup.add_subparsers(dest="writeup_command", required=True)
    writeup_generate = writeup_commands.add_parser("generate", help="Generate a writeup")
    writeup_generate.add_argument("operation_id")
    writeup_generate.add_argument(
        "--format",
        default=ReportFormat.MARKDOWN.value,
        choices=[item.value for item in ReportFormat],
    )
    writeup_list = writeup_commands.add_parser("list", help="List writeups")
    writeup_list.add_argument("operation_id")
    writeup_show = writeup_commands.add_parser("show", help="Print a writeup")
    writeup_show.add_argument("operation_id")
    writeup_show.add_argument("writeup_id")
    writeup_export = writeup_commands.add_parser(
        "export", help="Export a writeup with a manifest"
    )
    writeup_export.add_argument("operation_id")
    writeup_export.add_argument("writeup_id")
    writeup_export.add_argument("dest", type=Path)

    replay = subcommands.add_parser("replay", help="Replay the solution journal")
    replay_commands = replay.add_subparsers(dest="replay_command", required=True)
    replay_show = replay_commands.add_parser(
        "show", help="Print the solution replay document"
    )
    replay_show.add_argument("operation_id")


def handle_ctf_command(args: argparse.Namespace, ctf: CtfService) -> int | None:
    if args.command == "challenge":
        return _handle_challenge(args, ctf)
    if args.command == "hypothesis":
        return _handle_hypothesis(args, ctf)
    if args.command == "flag":
        return _handle_flag(args, ctf)
    if args.command == "writeup":
        return _handle_writeup(args, ctf)
    if args.command == "replay":
        print(ctf.replay(args.operation_id), end="")
        return 0
    return None


def _handle_challenge(args: argparse.Namespace, ctf: CtfService) -> int:
    if args.challenge_command == "init":
        plan = ctf.init_ctf(
            args.operation_id,
            args.name,
            ChallengeCategory(args.category),
            description=args.description,
            win_condition=args.win_condition,
            flag_format=args.flag_format,
        )
        if args.json_output:
            _print_json(plan.model_dump(mode="json"))
        else:
            print(f"initialized ctf-htb playbook: plan {plan.id}")
            for task in plan.tasks:
                print(f"task\t{task.id}\t{task.risk_class.value}\t{task.title}")
            for gate in plan.gates:
                print(f"gate\t{gate.id}\t{gate.name}")
        return 0
    if args.challenge_command == "show":
        challenge = ctf.get_challenge(args.operation_id)
        if args.json_output:
            _print_json(challenge.model_dump(mode="json"))
        else:
            print(challenge.describe())
        return 0
    summary = ctf.summary(args.operation_id)
    if args.json_output:
        _print_json(summary.model_dump(mode="json"))
    else:
        print(
            f"hypotheses: {summary.hypotheses_open} open, "
            f"{summary.hypotheses_testing} testing, "
            f"{summary.hypotheses_confirmed} confirmed, "
            f"{summary.hypotheses_refuted} refuted, "
            f"{summary.hypotheses_abandoned} abandoned"
        )
        print(
            f"flags: {summary.flags_captured} captured, "
            f"writeups: {summary.writeups}"
        )
    return 0


def _handle_hypothesis(args: argparse.Namespace, ctf: CtfService) -> int:
    if args.hypothesis_command == "add":
        hypothesis = ctf.propose_hypothesis(
            args.operation_id,
            args.title,
            ChallengeCategory(args.category) if args.category else None,
            notes=args.notes,
        )
        _emit(hypothesis, args.json_output)
        return 0
    if args.hypothesis_command == "update":
        action = {
            "testing": "testing",
            "confirmed": "confirm",
            "refuted": "refute",
            "abandoned": "abandon",
        }[args.status]
        hypothesis = ctf.update_hypothesis(
            args.hypothesis_id,
            action,  # type: ignore[arg-type]
            notes=args.notes,
        )
        _emit(hypothesis, args.json_output)
        return 0
    status = HypothesisStatus(args.status) if args.status else None
    hypotheses = ctf.list_hypotheses(args.operation_id, status=status)
    if args.json_output:
        _print_json([item.model_dump(mode="json") for item in hypotheses])
    else:
        for item in hypotheses:
            print(f"{item.id}\t{item.status.value}\t{item.title}")
    return 0


def _handle_flag(args: argparse.Namespace, ctf: CtfService) -> int:
    if args.flag_command == "add":
        record = ctf.record_flag(
            args.operation_id, args.name, args.value, proof_artifact_id=args.proof
        )
        _emit(record, args.json_output)
        return 0
    flags = ctf.list_flags(args.operation_id)
    if args.json_output:
        _print_json([item.model_dump(mode="json") for item in flags])
    else:
        for item in flags:
            print(f"{item.id}\t{item.name}\t{item.value}")
    return 0


def _handle_writeup(args: argparse.Namespace, ctf: CtfService) -> int:
    if args.writeup_command == "generate":
        writeup = ctf.generate_writeup(
            args.operation_id, ReportFormat(args.format)
        )
        _emit(writeup, args.json_output)
        return 0
    if args.writeup_command == "list":
        writeups = ctf.list_writeups(args.operation_id)
        if args.json_output:
            _print_json([item.model_dump(mode="json") for item in writeups])
        else:
            for item in writeups:
                print(f"{item.id}\t{item.format.value}\t{item.path}")
        return 0
    if args.writeup_command == "show":
        print(ctf.read_writeup(args.operation_id, args.writeup_id), end="")
        return 0
    manifest = ctf.export_writeup(args.operation_id, args.writeup_id, args.dest)
    if args.json_output:
        _print_json(manifest.model_dump(mode="json"))
    else:
        print(f"exported {manifest.report_id}: {len(manifest.files)} files")
    return 0


def _emit(model: object, json_output: bool) -> None:
    if json_output:
        _print_json(model.model_dump(mode="json"))  # type: ignore[attr-defined]
    else:
        print(model)


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
