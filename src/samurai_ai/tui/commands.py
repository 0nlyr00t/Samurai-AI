"""One registry of operator actions.

Slash commands, the command palette and the key overlay are three views of the
same table, so a new action is added once. Handlers live on the app; a command
declares whether it takes an argument and what to ask for when the palette
invokes it.
"""

from __future__ import annotations

from dataclasses import dataclass

GROUP_SESSION = "session"
GROUP_OPERATION = "operation"
GROUP_KERNEL = "kernel"
GROUP_TOOLS = "tools"
GROUP_PLAYBOOKS = "playbooks"
GROUP_VIEW = "view"
GROUP_CONTROL = "control"

GROUPS: tuple[str, ...] = (
    GROUP_SESSION,
    GROUP_OPERATION,
    GROUP_KERNEL,
    GROUP_TOOLS,
    GROUP_PLAYBOOKS,
    GROUP_VIEW,
    GROUP_CONTROL,
)


@dataclass(frozen=True)
class CommandSpec:
    name: str
    group: str
    handler: str
    help_key: str
    takes_argument: bool = False
    prompt_key: str | None = None
    needs_operation: bool = False


def _spec(
    name: str,
    group: str,
    handler: str,
    *,
    argument: bool = False,
    prompt_key: str | None = None,
    operation: bool = False,
) -> CommandSpec:
    return CommandSpec(
        name=name,
        group=group,
        handler=handler,
        help_key=f"help.{name.removeprefix('/')}",
        takes_argument=argument,
        prompt_key=prompt_key,
        needs_operation=operation,
    )


COMMANDS: tuple[CommandSpec, ...] = (
    # operation
    _spec("/new", GROUP_OPERATION, "_cmd_new_operation"),
    _spec("/open", GROUP_OPERATION, "_open_latest_operation"),
    _spec("/close", GROUP_OPERATION, "_close_operation", operation=True),
    _spec("/status", GROUP_OPERATION, "_show_status"),
    _spec(
        "/scope",
        GROUP_OPERATION,
        "_show_scope",
        argument=True,
        prompt_key="palette.prompt.target",
        operation=True,
    ),
    _spec("/reconcile", GROUP_OPERATION, "_show_reconcile", operation=True),
    _spec(
        "/phase",
        GROUP_OPERATION,
        "_show_phase",
        argument=True,
        prompt_key="palette.prompt.phase",
        operation=True,
    ),
    _spec("/snapshots", GROUP_OPERATION, "_show_snapshots", operation=True),
    _spec("/resume", GROUP_OPERATION, "_show_resume"),
    _spec("/replay", GROUP_OPERATION, "_show_replay", operation=True),
    _spec("/exit", GROUP_OPERATION, "_cmd_exit"),
    _spec("/help", GROUP_OPERATION, "_show_help"),
    # kernel
    _spec("/plan", GROUP_KERNEL, "_show_plan", operation=True),
    _spec("/applyplan", GROUP_KERNEL, "_apply_proposed_plan", operation=True),
    _spec("/discardplan", GROUP_KERNEL, "_discard_proposed_plan", operation=True),
    _spec("/tasks", GROUP_KERNEL, "_show_task_board", operation=True),
    _spec("/gates", GROUP_KERNEL, "_show_gates", operation=True),
    _spec("/approvals", GROUP_KERNEL, "_show_approvals", operation=True),
    _spec("/findings", GROUP_KERNEL, "_show_finding_board", operation=True),
    _spec("/graph", GROUP_KERNEL, "_show_graph_board", operation=True),
    _spec("/coverage", GROUP_KERNEL, "_show_coverage_board", operation=True),
    _spec("/theme", GROUP_VIEW, "_show_themes"),
    _spec("/keys", GROUP_VIEW, "_show_keys"),
    _spec("/evidence", GROUP_KERNEL, "_show_evidence", operation=True),
    # tools and agents
    _spec("/tools", GROUP_TOOLS, "_show_tools"),
    _spec("/runs", GROUP_TOOLS, "_show_runs", operation=True),
    _spec(
        "/inspect",
        GROUP_TOOLS,
        "_show_inspector",
        argument=True,
        prompt_key="palette.prompt.run",
        operation=True,
    ),
    _spec("/agents", GROUP_TOOLS, "_show_agents", operation=True),
    _spec("/sessions", GROUP_TOOLS, "_show_sessions", operation=True),
    _spec(
        "/subagent",
        GROUP_TOOLS,
        "_cmd_subagent",
        argument=True,
        prompt_key="palette.prompt.subagent",
        operation=True,
    ),
    _spec("/swarm", GROUP_TOOLS, "_show_swarms", operation=True),
    # playbooks and data
    _spec("/secrets", GROUP_PLAYBOOKS, "_show_secrets", operation=True),
    _spec("/assets", GROUP_PLAYBOOKS, "_show_assets", operation=True),
    _spec("/report", GROUP_PLAYBOOKS, "_show_reports", operation=True),
    _spec("/hypotheses", GROUP_PLAYBOOKS, "_show_hypotheses", operation=True),
    _spec("/flags", GROUP_PLAYBOOKS, "_show_flags", operation=True),
    _spec("/attack-path", GROUP_PLAYBOOKS, "_show_attack_path", operation=True),
    _spec("/debrief", GROUP_PLAYBOOKS, "_show_debriefs", operation=True),
    _spec("/test-cases", GROUP_PLAYBOOKS, "_show_test_cases", operation=True),
    _spec("/runbook", GROUP_PLAYBOOKS, "_show_runbooks", operation=True),
    _spec("/memory", GROUP_PLAYBOOKS, "_show_memory", argument=True),
    _spec("/lessons", GROUP_PLAYBOOKS, "_handle_lessons_command", argument=True),
    _spec("/login", GROUP_SESSION, "_handle_login_command", argument=True),
    _spec("/whoami", GROUP_SESSION, "_handle_whoami_command", argument=True),
    _spec("/logout", GROUP_SESSION, "_handle_logout_command", argument=True),
    _spec("/role", GROUP_SESSION, "_handle_role_command", argument=True),
    _spec("/team", GROUP_SESSION, "_handle_team_command", operation=True, argument=True),
    _spec("/constraint", GROUP_PLAYBOOKS, "_handle_constraint_command", argument=True),
    _spec("/traffic", GROUP_PLAYBOOKS, "_handle_traffic_command", operation=True, argument=True),
    _spec("/retest", GROUP_PLAYBOOKS, "_handle_retest_command", operation=True, argument=True),
    _spec("/skills", GROUP_PLAYBOOKS, "_show_skills", argument=True),
    _spec("/rules", GROUP_VIEW, "_handle_rules_command", argument=True),
    _spec("/playbooks", GROUP_PLAYBOOKS, "_show_custom_playbooks"),
    # view and control
    _spec("/lang", GROUP_VIEW, "_show_lang", argument=True, prompt_key="palette.prompt.locale"),
    _spec("/model", GROUP_VIEW, "_show_model_picker"),
    _spec("/connect", GROUP_VIEW, "_show_model_picker"),
    _spec("/settings", GROUP_VIEW, "_show_settings"),
    _spec("/mouse", GROUP_VIEW, "_toggle_mouse"),
    _spec("/trust", GROUP_VIEW, "_prompt_trust"),
    # takes an optional word ("code") but must not open a prompt for it
    _spec("/copy", GROUP_VIEW, "_copy_last", argument=True),
    _spec("/expand", GROUP_VIEW, "_expand_outputs"),
    _spec("/last", GROUP_VIEW, "_jump_to_last_notice"),
    _spec("/quiet", GROUP_VIEW, "_set_exec_quiet"),
    _spec("/verbose", GROUP_VIEW, "_set_exec_verbose"),
    _spec(
        "/goal",
        GROUP_CONTROL,
        "_handle_goal_command",
        argument=True,
        prompt_key="palette.prompt.goal",
        operation=True,
    ),
    _spec("/ask", GROUP_CONTROL, "_set_mode_ask"),
    _spec("/yolo", GROUP_CONTROL, "_toggle_yolo"),
    _spec("/never", GROUP_CONTROL, "_set_mode_never"),
    _spec("/sandbox", GROUP_CONTROL, "_cycle_sandbox"),
    _spec("/undo", GROUP_CONTROL, "_undo_last_turn"),
    _spec("/compact", GROUP_CONTROL, "_compact_now"),
    _spec("/clear", GROUP_CONTROL, "_clear_chat"),
)

_BY_NAME = {spec.name: spec for spec in COMMANDS}


def command_path(spec: CommandSpec) -> str:
    """The path the access gate checks: the slash name without its leading slash."""
    return spec.name.lstrip("/")


def find_command(name: str) -> CommandSpec | None:
    return _BY_NAME.get(name)


def commands_in(group: str) -> tuple[CommandSpec, ...]:
    return tuple(spec for spec in COMMANDS if spec.group == group)
