from __future__ import annotations

import argparse
import json
from pathlib import Path

from samurai_ai.domain.learned_skills import LearnedSkillMode, LearnedSkillStatus
from samurai_ai.services.learned_skills import LearnedSkillService
from samurai_ai.services.skills import SkillService


def register_skill_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    skill = subcommands.add_parser("skill", help="Manage trusted custom skills")
    skill_commands = skill.add_subparsers(dest="skill_command", required=True)

    install = skill_commands.add_parser(
        "install", help="Install a skill from a markdown/text file"
    )
    install.add_argument("name")
    install.add_argument("skill_file", type=Path)
    install.add_argument(
        "--trusted",
        action="store_true",
        help="confirm the skill content was reviewed and is trusted",
    )

    skill_commands.add_parser("list", help="List installed skills (latest versions)")

    show = skill_commands.add_parser("show", help="Show a skill's latest version")
    show.add_argument("name")

    versions = skill_commands.add_parser("versions", help="List all versions of a skill")
    versions.add_argument("name")


def register_learned_skill_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    learned = subcommands.add_parser(
        "learned-skill", help="Inspect and gate agent-distilled (learned) skills"
    )
    commands = learned.add_subparsers(dest="learned_command", required=True)

    list_parser = commands.add_parser("list", help="List learned skills")
    list_parser.add_argument("--workspace", default=None)
    list_parser.add_argument(
        "--status", choices=[status.value for status in LearnedSkillStatus], default=None
    )

    show = commands.add_parser("show", help="Show a learned skill in full")
    show.add_argument("skill_id")

    extract = commands.add_parser(
        "extract", help="Distil a learned skill from a finished operation"
    )
    extract.add_argument("operation_id")
    extract.add_argument("workspace")

    promote = commands.add_parser("promote", help="Promote a learned skill to active")
    promote.add_argument("skill_id")

    reject = commands.add_parser("reject", help="Retire a learned skill")
    reject.add_argument("skill_id")

    mode = commands.add_parser("mode", help="Get or set the learned-skills injection mode")
    mode.add_argument(
        "mode", nargs="?", choices=[item.value for item in LearnedSkillMode], default=None
    )

    curate = commands.add_parser("curate", help="Retire active skills that never helped")
    curate.add_argument("--workspace", default=None)


def handle_learned_skill_command(
    args: argparse.Namespace, learned: LearnedSkillService
) -> int | None:
    if args.command != "learned-skill":
        return None
    command = args.learned_command
    if command == "list":
        status = LearnedSkillStatus(args.status) if args.status else None
        skills = learned.all(args.workspace, status)
        if args.json_output:
            _print_json([skill.model_dump(mode="json") for skill in skills])
        else:
            for skill in skills:
                fitness = skill.fitness
                print(
                    f"{skill.id}\t{skill.status.value}\t{skill.workspace}\t"
                    f"reuses={fitness.reuses}\tfindings={fitness.findings_after_reuse}\t"
                    f"{skill.title}"
                )
        return 0
    if command == "show":
        skill = learned.get(args.skill_id)
        if args.json_output:
            _print_json(skill.model_dump(mode="json"))
        else:
            print(f"{skill.id}\t{skill.status.value}\t{skill.workspace}")
            print(f"when: {skill.cue}")
            print(f"evidence: {', '.join(skill.evidence) or '(none)'}")
            print(skill.body)
        return 0
    if command == "extract":
        admitted = learned.extract(args.operation_id, args.workspace)
        if args.json_output:
            _print_json([skill.model_dump(mode="json") for skill in admitted])
        else:
            print(f"extracted {len(admitted)} learned skill(s)")
        return 0
    if command == "promote":
        skill = learned.promote(args.skill_id)
        print(f"{skill.id}\t{skill.status.value}")
        return 0
    if command == "reject":
        skill = learned.reject(args.skill_id)
        print(f"{skill.id}\t{skill.status.value}")
        return 0
    if command == "mode":
        if args.mode is None:
            print(learned.mode().value)
        else:
            learned.set_mode(LearnedSkillMode(args.mode))
            print(learned.mode().value)
        return 0
    retired = learned.curate(args.workspace)
    print(f"retired {retired} learned skill(s)")
    return 0


def handle_skill_command(args: argparse.Namespace, skills: SkillService) -> int | None:
    if args.command != "skill":
        return None
    if args.skill_command == "install":
        body = args.skill_file.read_text(encoding="utf-8")
        skill = skills.install(args.name, body, trusted=args.trusted)
        if args.json_output:
            _print_json(skill.model_dump(mode="json"))
        else:
            print(
                f"installed skill {skill.name} v{skill.version} "
                f"(sha256 {skill.sha256[:16]}…)"
            )
        return 0
    if args.skill_command == "list":
        installed = skills.list_skills()
        if args.json_output:
            _print_json([skill.model_dump(mode="json") for skill in installed])
        else:
            for skill in installed:
                print(
                    f"{skill.name}\tv{skill.version}\tsha256={skill.sha256[:16]}…\t"
                    f"{skill.created_at.isoformat()}"
                )
        return 0
    if args.skill_command == "show":
        skill = skills.latest(args.name)
        if args.json_output:
            _print_json(skill.model_dump(mode="json"))
        else:
            print(f"{skill.name}\tv{skill.version}\tsha256={skill.sha256}")
            print(skill.body)
        return 0
    versions = skills.list_versions(args.name)
    if args.json_output:
        _print_json([skill.model_dump(mode="json") for skill in versions])
    else:
        for skill in versions:
            print(f"{skill.name}\tv{skill.version}\tsha256={skill.sha256[:16]}…")
    return 0


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
