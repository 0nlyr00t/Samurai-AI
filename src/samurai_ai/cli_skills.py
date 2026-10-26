from __future__ import annotations

import argparse
import json
from pathlib import Path

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
