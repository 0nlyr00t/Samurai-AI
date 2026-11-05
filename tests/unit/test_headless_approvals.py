"""Unattended commands approve their own tool calls unless told to stop."""

import argparse

from samurai_ai.cli_headless import register_headless_commands
from samurai_ai.cli_swarm import register_swarm_commands
from samurai_ai.services.swarm import always_approve


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    register_swarm_commands(subcommands)
    return parser.parse_args(argv)


def test_a_swarm_run_approves_by_default() -> None:
    args = parse(["swarm", "run", "op_1", "--model", "m"])
    assert args.auto_approve is True


def test_ask_turns_the_prompt_back_on() -> None:
    args = parse(["swarm", "run", "op_1", "--model", "m", "--ask"])
    assert args.auto_approve is False


def test_the_resolver_approves_everything_it_is_handed() -> None:
    assert always_approve("apr_1") is True


def test_the_headless_command_also_asks_on_request() -> None:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    register_headless_commands(subcommands)
    args = parser.parse_args(["headless", "run", "op_1", "--model", "m"])
    assert args.auto_approve is True
    asked = parser.parse_args(["headless", "run", "op_1", "--model", "m", "--ask"])
    assert asked.auto_approve is False
