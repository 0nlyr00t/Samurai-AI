import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.text import Text
from textual.widgets import Button, ContentSwitcher, Input, OptionList, RichLog, Static, Tab, Tabs

from samurai_ai.agents.orchestrator import AgentOrchestrator
from samurai_ai.agents.provider import Message, ToolCall
from samurai_ai.agents.registry import ModelRegistry
from samurai_ai.domain.agents import AgentRun, AgentRunStatus
from samurai_ai.domain.constraints import ConstraintKind
from samurai_ai.domain.ctf import FlagRecord, Hypothesis
from samurai_ai.domain.kernel import (
    ArtifactKind,
    FindingSeverity,
    FindingStatus,
    GateStatus,
    PlanTask,
    RiskClass,
    StageGate,
    TaskStatus,
)
from samurai_ai.domain.memory import MemoryKind
from samurai_ai.domain.operations import OperationEvent, OperationMode
from samurai_ai.domain.planning import KillChainPhase, PlannedTest, Runbook
from samurai_ai.domain.playbooks import AssetKind, AssetLedgerEntry, PlaybookTemplate
from samurai_ai.domain.redteam import AttackStep, Debrief
from samurai_ai.domain.reports import BountyReport, ReportFormat
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.swarm import AgentSpecialization, SwarmRun
from samurai_ai.domain.traffic import TrafficRole
from samurai_ai.services.ctf import CtfService
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.goals import GoalService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.memory import CampaignMemoryService
from samurai_ai.services.models import ModelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.planning import PlanningService
from samurai_ai.services.playbooks import PlaybookService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.services.redteam import RedTeamService
from samurai_ai.services.reports import ReportService
from samurai_ai.services.settings import SettingsService
from samurai_ai.services.skills import SkillService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.agents import AgentRunRepository
from samurai_ai.storage.ctf import CtfRepository
from samurai_ai.storage.database import Database
from samurai_ai.storage.execution import ToolRunRepository
from samurai_ai.storage.goals import GoalRepository
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.memory import MemoryRepository
from samurai_ai.storage.models import CustomModelRepository, ModelEndpointRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.planning import PlanningRepository
from samurai_ai.storage.playbooks import PlaybookRepository
from samurai_ai.storage.recovery import RecoveryRepository
from samurai_ai.storage.redteam import RedTeamRepository
from samurai_ai.storage.reports import ReportRepository
from samurai_ai.storage.skill_gaps import SkillGapRepository
from samurai_ai.storage.skills import SkillRepository
from samurai_ai.storage.swarm import SwarmRunRepository
from samurai_ai.storage.vault import VaultRepository
from samurai_ai.tui.app import SamuraiApp


def test_tui_loads_operation_and_processes_keyboard_commands(tmp_path: Path) -> None:
    database = Database(tmp_path / "tui.db")
    operation_repository = OperationRepository(database)
    service = OperationService(operation_repository)
    kernel = KernelService(service, KernelRepository(database))
    recovery_repository = RecoveryRepository(database)
    settings = SettingsService(recovery_repository)
    settings.set_locale("en")
    recovery = RecoveryService(service, kernel, recovery_repository)
    settings.set_value(
        "tui.trusted_dirs", json.dumps([str(Path.cwd().resolve())])
    )
    vault = VaultService(
        VaultRepository(database), operation_repository, tmp_path / "vault.key"
    )
    execution = ToolExecutionService(
        service, kernel, recovery, ToolRunRepository(database), vault=vault
    )
    agents = AgentRunRepository(database)
    playbooks = PlaybookService(service, kernel, PlaybookRepository(database))
    reports = ReportService(service, kernel, playbooks, ReportRepository(database))
    ctf = CtfService(service, kernel, CtfRepository(database))
    redteam = RedTeamService(service, kernel, RedTeamRepository(database))
    planning = PlanningService(service, kernel, PlanningRepository(database))
    operation = service.create(
        "TUI lab",
        OperationMode.CTF_HTB,
        ScopeContract(includes=("*.example.com",)),
        objective="Test the operator workflow",
    )
    kernel.create_plan(
        operation.id,
        "TUI kernel plan",
        [
            PlanTask(
                id="recon",
                title="Map the service",
                risk_class=RiskClass.R1,
                required_artifacts=("service-map",),
            )
        ],
        [StageGate(id="recon-complete", name="Recon complete")],
    )
    artifact = kernel.register_artifact(
        operation.id,
        ArtifactKind.NOTE,
        "service exposed on 443",
        summary="Service map",
        requirement="service-map",
        task_id="recon",
    )
    finding = kernel.create_finding(operation.id, "TUI finding")
    approval = kernel.request_approval(
        operation.id,
        "Run active lab probe",
        RiskClass.R2,
    )
    tool_result = execution.run_tool(operation.id, "local.echo", {"text": "hello tui"})
    secret = vault.add(
        "tui-token", "tui-secret-plaintext", operation.id, created_by="operator"
    )
    agent_run = AgentRun(
        operation_id=operation.id,
        task_id="recon",
        model_route=("fake/agent@v1",),
    )
    agents.add(agent_run)
    agent_run.finish(AgentRunStatus.COMPLETED)
    agents.finish(agent_run)
    swarms = SwarmRunRepository(database)
    swarm = SwarmRun(
        operation_id=operation.id,
        workers=1,
        task_ids=("recon",),
        model_route=("fake/agent@v1",),
    )
    swarms.add(swarm)
    swarm_member = AgentRun(
        operation_id=operation.id,
        task_id="recon",
        model_route=("fake/agent@v1",),
        swarm_id=swarm.id,
        specialization=AgentSpecialization.RECON,
    )
    agents.add(swarm_member)
    swarm_member.finish(AgentRunStatus.COMPLETED)
    agents.finish(swarm_member)
    memory = CampaignMemoryService(MemoryRepository(database))
    memory.add("tui-ws", "TUI methodology", kind=MemoryKind.METHODOLOGY)
    skills = SkillService(SkillRepository(database), gaps=SkillGapRepository(database))
    skills.install("tui-skill", "TUI skill body", trusted=True)
    custom_template = PlaybookTemplate(
        name="tui-custom",
        title="TUI custom playbook",
        tasks=(PlanTask(id="solo", title="Solo task"),),
        gates=(),
    )
    playbooks.playbooks.add_custom_playbook(custom_template, "ab" * 32, "operator")
    playbooks.playbooks.add_asset(
        AssetLedgerEntry(
            operation_id=operation.id, kind=AssetKind.DOMAIN, value="www.example.com"
        ),
        OperationEvent(operation_id=operation.id, event_type="asset.recorded", payload={}),
    )
    reports.reports.add_report(
        BountyReport(
            operation_id=operation.id,
            format=ReportFormat.MARKDOWN,
            sha256="ab" * 32,
            size_bytes=2,
            path="reports/tui/rep_tui.md",
            finding_ids=("find_1",),
            asset_count=1,
        ),
        OperationEvent(operation_id=operation.id, event_type="report.generated", payload={}),
    )
    hypothesis = Hypothesis(operation_id=operation.id, title="TUI hypothesis")
    ctf.ctf.add_hypothesis(
        hypothesis,
        OperationEvent(
            operation_id=operation.id, event_type="hypothesis.proposed", payload={}
        ),
    )
    ctf.ctf.add_flag(
        FlagRecord(operation_id=operation.id, name="user", value="HTB{tui_fl4g}"),
        OperationEvent(operation_id=operation.id, event_type="flag.captured", payload={}),
    )
    attack_step = AttackStep(
        operation_id=operation.id, name="TUI phish", technique_id="T1566"
    )
    redteam.redteam.add_step(
        attack_step,
        OperationEvent(
            operation_id=operation.id, event_type="attack-step.added", payload={}
        ),
    )
    redteam.redteam.add_debrief(
        Debrief(
            operation_id=operation.id,
            format=ReportFormat.MARKDOWN,
            sha256="cd" * 32,
            size_bytes=2,
            path="reports/tui/deb_tui.md",
            step_count=1,
        ),
        OperationEvent(operation_id=operation.id, event_type="debrief.generated", payload={}),
    )
    test_case = PlannedTest(
        operation_id=operation.id,
        name="TUI recon",
        technique_id="T1595",
        kill_chain_phase=KillChainPhase.RECONNAISSANCE,
    )
    planning.planning.add_test_case(
        test_case,
        OperationEvent(
            operation_id=operation.id, event_type="test-case.added", payload={}
        ),
    )
    planning.planning.add_runbook(
        Runbook(
            operation_id=operation.id,
            format=ReportFormat.MARKDOWN,
            sha256="ef" * 32,
            size_bytes=2,
            path="reports/tui/rb_tui.md",
            test_case_ids=(test_case.id,),
            stop_points=1,
        ),
        OperationEvent(operation_id=operation.id, event_type="runbook.generated", payload={}),
    )

    async def exercise() -> None:
        models = ModelService(
            ModelRegistry(ModelEndpointRepository(database)),
            vault,
            kernel,
            service,
            ModelEndpointRepository(database),
            recovery_repository,
        )
        app = SamuraiApp(
            service, kernel, settings, recovery, execution, vault, agents,
            AgentOrchestrator(models, kernel, execution, service, AgentRunRepository(database)),
            playbooks, reports, ctf, redteam, planning, swarms,
            GoalService(service, GoalRepository(database)),
            memory, skills, models, operation.id,
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            brand = app.query_one("#brand", Static)
            assert "Samurai AI" in str(brand.render())

            journal = app.query_one("#journal", RichLog)
            command_input = app.query_one("#command-input", Input)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "/scope sub.example.com"
                await pilot.press("enter")
                await pilot.pause()
                assert any("[allow]" in message for message in messages)

                command_input.value = "/status"
                await pilot.press("enter")
                await pilot.pause()
                assert any("version=1" in message for message in messages)

                command_input.value = "/plan"
                await pilot.press("enter")
                await pilot.pause()
                assert any("Test the operator workflow" in message for message in messages)

                for command, expected in (
                    ("/gates", "recon-complete"),
                    ("/evidence", artifact.id),
                    ("/approvals", approval.id),
                    ("/tools", "local.echo"),
                    ("/tools", "sandbox.exec"),
                    ("/runs", tool_result.run.id),
                    ("/agents", agent_run.id),
                    ("/swarm", swarm.id),
                    ("/swarm", swarm_member.id),
                    ("/memory", "TUI methodology"),
                    ("/skills", "tui-skill"),
                    ("/playbooks", "tui-custom"),
                    ("/secrets", secret.id),
                    ("/assets", "www.example.com"),
                    ("/report", "findings=1"),
                    ("/hypotheses", hypothesis.id),
                    ("/flags", "HTB{tui_fl4g}"),
                    ("/attack-path", "T1566"),
                    ("/debrief", "steps=1"),
                    ("/test-cases", "T1595"),
                    ("/runbook", "stops=1"),
                ):
                    command_input.value = command
                    await pilot.press("enter")
                    await pilot.pause()
                    assert any(expected in message for message in messages)

                # /tasks and /findings open boards instead of dumping text
                from samurai_ai.tui.screens import BoardScreen

                for command, expected in (("/tasks", "recon"), ("/findings", finding.id)):
                    command_input.value = command
                    await pilot.press("enter")
                    await pilot.pause()
                    assert isinstance(app.screen, BoardScreen)
                    listed = app.screen.query_one("#board-list", OptionList)
                    assert any(
                        expected in str(option.prompt) for option in listed.options
                    )
                    await pilot.press("escape")
                    await pilot.pause()

                assert not any("tui-secret-plaintext" in message for message in messages)


            views = app.query_one("#views", ContentSwitcher)
            assert views.display
            await pilot.press("ctrl+o")
            assert not views.display
            await pilot.press("ctrl+o")
            assert views.display
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def _minimal_stack(tmp_path: Path, provider_factory=None, trust_cwd: bool = True):  # type: ignore[no-untyped-def]
    database = Database(tmp_path / "tui2.db")
    operation_repository = OperationRepository(database)
    service = OperationService(operation_repository)
    kernel = KernelService(service, KernelRepository(database))
    recovery_repository = RecoveryRepository(database)
    settings = SettingsService(recovery_repository)
    if trust_cwd:
        settings.set_value(
            "tui.trusted_dirs", json.dumps([str(Path.cwd().resolve())])
        )
    recovery = RecoveryService(service, kernel, recovery_repository)
    vault = VaultService(
        VaultRepository(database), operation_repository, tmp_path / "vault.key"
    )
    execution = ToolExecutionService(
        service, kernel, recovery, ToolRunRepository(database), vault=vault
    )
    endpoints = ModelEndpointRepository(database)
    custom_models = CustomModelRepository(database)
    models = ModelService(
        ModelRegistry(endpoints, custom_models=custom_models),
        vault, kernel, service, endpoints,
        recovery_repository, provider_factory=provider_factory,
        custom_models=custom_models,
    )
    playbooks = PlaybookService(service, kernel, PlaybookRepository(database))
    return (
        service, kernel, settings, recovery, execution, vault,
        AgentRunRepository(database),
        AgentOrchestrator(models, kernel, execution, service, AgentRunRepository(database)),
        playbooks,
        ReportService(service, kernel, playbooks, ReportRepository(database)),
        CtfService(service, kernel, CtfRepository(database)),
        RedTeamService(service, kernel, RedTeamRepository(database)),
        PlanningService(service, kernel, PlanningRepository(database)),
        SwarmRunRepository(database),
        GoalService(OperationService(OperationRepository(database)), GoalRepository(database)),
        CampaignMemoryService(MemoryRepository(database)),
        SkillService(SkillRepository(database), gaps=SkillGapRepository(database)),
        models,
    )


def test_language_prompt_arrow_keys(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # first run: language prompt is up; arrows+enter choose Russian
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert stack[2].get_locale() == "ru"
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_model_picker_persists_default(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    settings = stack[2]
    settings.set_locale("en")
    vault = stack[5]
    models = stack[-1]
    vault.add("fake-key", "x")
    models.register_endpoint("fake", "http://127.0.0.1:9/v1", "fake-key")
    models.add_model("fake/fake-model")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command_input = app.query_one("#command-input", Input)
            command_input.value = "/model"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            from samurai_ai.tui.app import ModelPickerScreen
            assert isinstance(app.screen, ModelPickerScreen)
            # first entry is the sentinel; down lands on the configured model
            await pilot.press("down")
            await pilot.press("tab")
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert settings.get_value("model.default") == "fake/fake-model@v1"
            assert settings.get_value("model.reasoning_effort") == "high"
            assert (
                settings.get_value("model.reasoning_effort.fake/fake-model@v1")
                == "high"
            )
            status_bar = app.query_one("#status-bar", Static)
            assert "model=" in str(status_bar.render())
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_model_picker_configured_only_and_current(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    settings = stack[2]
    settings.set_locale("en")
    vault = stack[5]
    models = stack[-1]
    vault.add("giga-api-key", "x")
    models.register_endpoint("giga", "http://127.0.0.1:9/osav1", "giga-api-key")
    models.add_model("giga/giga-osa/deepseek-v4.1-flash-uncensored")
    current = "giga/giga-osa/deepseek-v4.1-flash-uncensored@v1"
    settings.set_value("model.default", current)
    settings.set_value(f"model.reasoning_effort.{current}", "high")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command_input = app.query_one("#command-input", Input)
            command_input.value = "/model"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            from samurai_ai.tui.app import REASONING_LEVELS, ModelPickerScreen

            assert isinstance(app.screen, ModelPickerScreen)
            options = app.screen.query_one("#model-options", OptionList)
            prompts = [
                str(options.get_option_at_index(i).prompt)
                for i in range(options.option_count)
            ]
            # only configured providers are listed, no unconfigured static ones
            assert not any("openai/gpt-4o@v1" in prompt for prompt in prompts)
            assert not any("moonshot/kimi-k2@v1" in prompt for prompt in prompts)
            assert any(current in prompt for prompt in prompts)
            # the configured model is highlighted and marked as current
            assert options.highlighted == 1  # sentinel at 0, current at 1
            assert "current" in str(options.get_option_at_index(1).prompt)
            # reasoning follows the per-model stored level
            reasoning = app.screen.query_one("#reasoning-options", OptionList)
            assert reasoning.highlighted == REASONING_LEVELS.index("high")

            await pilot.press("enter")
            await pilot.pause()
            assert settings.get_value("model.default") == current
            assert settings.get_value("model.reasoning_effort") == "high"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_command_history_up_down(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    settings = stack[2]
    settings.set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command_input = app.query_one("#command-input", Input)
            for command in ("/status", "/plan"):
                command_input.value = command
                await pilot.press("enter")
                await pilot.pause()
            assert command_input.value == ""
            await pilot.press("up")
            assert command_input.value == "/plan"
            await pilot.press("up")
            assert command_input.value == "/status"
            await pilot.press("down")
            assert command_input.value == "/plan"
            await pilot.press("down")
            assert command_input.value == ""
            import json as json_module

            assert json_module.loads(settings.get_value("tui.history")) == [
                "/status",
                "/plan",
            ]
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

        # history persists across sessions
        reopened = SamuraiApp(*stack)
        async with reopened.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command_input = reopened.query_one("#command-input", Input)
            await pilot.press("up")
            assert command_input.value == "/plan"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_double_ctrl_c_quits(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                await pilot.press("ctrl+c")
                await pilot.pause()
                assert app.is_running
                assert any("Ctrl+C" in str(message) for message in messages)
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert not app.is_running

    asyncio.run(exercise())


def test_custom_endpoint_wizard(tmp_path: Path) -> None:
    class _FakeProvider:
        async def health(self) -> bool:
            return True

        async def list_models(self) -> list[str]:
            return ["giga-osa/deepseek-v4.1-flash-uncensored", "giga-lite"]

        async def complete(self, request):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    stack = _minimal_stack(
        tmp_path, provider_factory=lambda provider: _FakeProvider()
    )
    settings = stack[2]
    settings.set_locale("en")
    vault = stack[5]
    models = stack[-1]

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            command_input = app.query_one("#command-input", Input)
            command_input.value = "/model"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            from samurai_ai.tui.app import CustomEndpointScreen, ModelPickerScreen

            assert isinstance(app.screen, ModelPickerScreen)
            # first option is the "+ add custom endpoint" sentinel
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, CustomEndpointScreen)
            screen = app.screen

            screen.query_one("#ce-id", Input).value = "giga"
            screen.query_one("#ce-url", Input).value = "https://api.giga.test/osav1"
            screen.query_one("#ce-key", Input).value = "wizard-secret"
            screen.query_one("#ce-tls", OptionList).highlighted = 1  # TLS off
            screen.query_one("#ce-fetch", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()

            options = screen.query_one("#ce-models", OptionList)
            assert options.display
            assert options.has_focus
            await pilot.press("enter")
            await pilot.pause()

            assert (
                settings.get_value("model.default")
                == "giga/giga-osa/deepseek-v4.1-flash-uncensored@v1"
            )
            assert vault.get("giga-api-key") == "wizard-secret"
            providers = {row["id"]: row for row in models.list_providers()}
            assert providers["giga"]["verify_tls"] is False
            status_bar = app.query_one("#status-bar", Static)
            assert "giga/giga-osa/deepseek-v4.1-flash-uncensored@v1" in str(
                status_bar.render()
            )
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_brand_shows_name_and_version(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    settings = stack[2]
    settings.set_locale("en")
    vault = stack[5]
    models = stack[-1]
    vault.add("fake-key", "x")
    models.register_endpoint("fake", "http://127.0.0.1:9/v1", "fake-key")
    models.add_model("fake/fake-model")
    settings.set_value("model.default", "fake/fake-model@v1")

    async def exercise() -> None:
        from samurai_ai import __version__

        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            strip = app.query_one("#brand", Static).render()
            assert "Samurai AI" in strip.plain
            assert "╪" not in strip.plain  # the mark stays out of the shell
            banner = "\n".join(line.text for line in app.query_one("#journal", RichLog).lines)
            assert "Samurai AI" in banner
            assert __version__ in banner
            status_bar = str(app.query_one("#status-bar", Static).render())
            assert "model=fake/fake-model@v1" in status_bar
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_permission_modes_cycle_through_commands(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    settings = stack[2]
    settings.set_locale("en")
    operation = stack[0].create(
        "Mode lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("*.example.com",)),
    )

    def dot_style(status_bar: Static) -> str:
        content = status_bar.render()
        for span in getattr(content, "spans", []):
            if content.plain[span.start:span.end] == "●":
                return str(span.style)
        return ""

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            status_bar = app.query_one("#status-bar", Static)
            assert "81,207,102" in dot_style(status_bar)  # green: ask
            assert "ask" in str(status_bar.render())

            command_input = app.query_one("#command-input", Input)
            command_input.value = "/yolo"
            await pilot.press("enter")
            await pilot.pause()
            assert settings.get_value("tui.permission_mode") == "yolo"
            assert settings.get_value("tui.yolo") == "1"  # legacy mirror
            assert "255,169,77" in dot_style(status_bar)  # amber: yolo

            command_input.value = "/never"
            await pilot.press("enter")
            await pilot.pause()
            assert settings.get_value("tui.permission_mode") == "never"
            assert "255,107,107" in dot_style(status_bar)  # red: never

            command_input.value = "/yolo"
            await pilot.press("enter")
            await pilot.pause()
            assert settings.get_value("tui.permission_mode") == "yolo"

            command_input.value = "/ask"
            await pilot.press("enter")
            await pilot.pause()
            assert settings.get_value("tui.permission_mode") == "ask"
            assert "81,207,102" in dot_style(status_bar)
            events = [
                event
                for event in stack[0].repository.events(operation.id)
                if event.event_type == "operator.permission_mode"
            ]
            assert [event.payload["to"] for event in events] == [
                "yolo",
                "never",
                "yolo",
                "ask",
            ]
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_statusbar_shows_current_directory(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            status_bar = str(app.query_one("#status-bar", Static).render())
            assert str(Path.cwd().resolve()).replace(str(Path.home()), "~", 1)[:20] in (
                status_bar
            )
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_tabs_switch_views(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    service, kernel, settings = stack[0], stack[1], stack[2]
    settings.set_locale("en")
    operation = service.create(
        "Tab lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("*.example.com",)),
        objective="Tab objective",
    )
    kernel.create_plan(
        operation.id,
        "Tab kernel plan",
        [PlanTask(id="recon", title="Map the service", risk_class=RiskClass.R1)],
        [StageGate(id="recon-complete", name="Recon complete")],
    )

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            tabs = app.query_one("#view-tabs", Tabs)
            switcher = app.query_one("#views", ContentSwitcher)
            assert switcher.current == "journal"
            tabs.active = "tab-ops"
            await pilot.pause()
            assert switcher.current == "view-ops"
            ops_log = app.query_one("#view-ops", RichLog)
            text = "\n".join(strip.text for strip in ops_log.lines)
            assert "Map the service" in text
            assert "recon-complete" in text
            tabs.active = "tab-bounty"
            await pilot.pause()
            assert switcher.current == "view-bounty"
            bounty_log = app.query_one("#view-bounty", RichLog)
            bounty_text = "\n".join(strip.text for strip in bounty_log.lines)
            assert "*.example.com" in bounty_text
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_command_suggestions(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            command_input = app.query_one("#command-input", Input)
            popup = app.query_one("#command-suggestions", OptionList)
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "/s"
                await pilot.pause()
                assert popup.display
                prompts = [
                    str(popup.get_option_at_index(i).prompt)
                    for i in range(popup.option_count)
                ]
                assert any(prompt.startswith("/status") for prompt in prompts)
                assert popup.highlighted == 0
                # arrows navigate the popup instead of the history
                await pilot.press("down")
                assert popup.highlighted == 1
                assert command_input.value == "/s"
                await pilot.press("up")
                assert popup.highlighted == 0
                # tab completes the highlighted command
                await pilot.press("tab")
                assert command_input.value.startswith("/")
                assert not popup.display
                # typing a full command hides the popup and enter executes
                command_input.value = "/status"
                await pilot.pause()
                assert not popup.display
                await pilot.press("enter")
                await pilot.pause()
                assert any("no operation" in str(m).lower() for m in messages)
                # escape closes the popup
                command_input.value = "/m"
                await pilot.pause()
                assert popup.display
                await pilot.press("escape")
                assert not popup.display
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_directory_trust_prompt(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path, trust_cwd=False)
    settings = stack[2]
    settings.set_locale("en")
    cwd = str(Path.cwd().resolve())

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)) as pilot:
            from samurai_ai.tui.app import TrustDirectoryScreen

            await pilot.pause()
            assert isinstance(app.screen, TrustDirectoryScreen)
            # deny first: arrows down twice, enter
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            status_bar = str(app.query_one("#status-bar", Static).render())
            assert "(untrusted)" in status_bar
            assert cwd not in json.loads(
                settings.get_value("tui.trusted_dirs") or "[]"
            )
            # /trust reopens the prompt; trust always
            command_input = app.query_one("#command-input", Input)
            command_input.value = "/trust"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, TrustDirectoryScreen)
            await pilot.press("enter")
            await pilot.pause()
            assert cwd in json.loads(settings.get_value("tui.trusted_dirs"))
            status_bar = str(app.query_one("#status-bar", Static).render())
            assert "(untrusted)" not in status_bar
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

        # second run: trusted, no prompt
        reopened = SamuraiApp(*stack)
        async with reopened.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert not isinstance(reopened.screen, TrustDirectoryScreen)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_ctrl_c_copies_selection_then_quits(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")

    async def exercise() -> None:
        from textual.selection import SELECT_ALL

        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            app.screen.selections = {journal: SELECT_ALL}
            await pilot.press("ctrl+c")
            await pilot.pause()
            # selection copied, app still running, no quit hint
            assert app.is_running
            assert "Samurai AI" in getattr(app, "_clipboard", "")
            app.screen.clear_selection()
            # with no selection, double ctrl+c quits
            await pilot.press("ctrl+c")
            assert app.is_running
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert not app.is_running

    asyncio.run(exercise())


def _message_text(message) -> str:  # type: ignore[no-untyped-def]
    markup = getattr(message, "markup", None)
    if isinstance(markup, str):
        return markup
    renderable = getattr(message, "renderable", None)
    if isinstance(renderable, Text):
        return renderable.plain
    return str(message)


class _ChatProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def health(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["test-model"]

    async def complete(self, request):  # type: ignore[no-untyped-def]
        from samurai_ai.agents.provider import ChatResponse, Usage

        self.calls += 1
        return ChatResponse(
            text=f"echo: {request.messages[-1].content}",
            usage=Usage(prompt_tokens=3, completion_tokens=2),
            model_id=request.model_id,
            provider_id="gpt",
            latency_ms=5,
        )


def _chat_stack(tmp_path: Path, provider=None):  # type: ignore[no-untyped-def]
    provider = provider or _ChatProvider()
    stack = _minimal_stack(tmp_path, provider_factory=lambda _p: provider)
    settings = stack[2]
    settings.set_locale("en")
    vault = stack[5]
    models = stack[-1]
    vault.add("gpt-key", "x")
    models.register_endpoint("gpt", "http://127.0.0.1:9/v1", "gpt-key")
    models.add_model("gpt/test-model")
    settings.set_value("model.default", "gpt/test-model@v1")
    operation = stack[0].create(
        "Chat lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("*.example.com",)),
    )
    return stack, provider, operation


def test_chat_model_call_is_auto_allowed(tmp_path: Path) -> None:
    stack, provider, operation = _chat_stack(tmp_path)

    async def exercise() -> None:
        from samurai_ai.tui.app import AccessPromptScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            command_input = app.query_one("#command-input", Input)
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                # model calls are covered by directory trust: no access prompt
                command_input.value = "hello world"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert not isinstance(app.screen, AccessPromptScreen)
                assert provider.calls == 1
                assert any("echo: hello world" in _message_text(m) for m in messages)
                approval = stack[1].kernel.list_approvals(operation.id)[-1]
                assert approval.decided_by == "auto"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_chat_yolo_auto_allows(tmp_path: Path) -> None:
    stack, provider, operation = _chat_stack(tmp_path)
    stack[2].set_value("tui.yolo", "1")

    async def exercise() -> None:
        from samurai_ai.tui.app import AccessPromptScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            command_input = app.query_one("#command-input", Input)
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "ping"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert not isinstance(app.screen, AccessPromptScreen)
                assert provider.calls == 1
                assert any("echo: ping" in _message_text(m) for m in messages)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_chat_without_model_shows_error(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            command_input = app.query_one("#command-input", Input)
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "hello"
                await pilot.press("enter")
                await pilot.pause()
                assert any("pick one with /model" in str(m) for m in messages)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


class _CodeChatProvider(_ChatProvider):
    async def complete(self, request):  # type: ignore[no-untyped-def]
        from samurai_ai.agents.provider import ChatResponse, Usage

        self.calls += 1
        return ChatResponse(
            text="Here you go:\n```python\nprint('hi')\n```\ndone",
            usage=Usage(prompt_tokens=3, completion_tokens=2),
            model_id=request.model_id,
            provider_id="gpt",
            latency_ms=5,
        )


def test_chat_renders_markdown_and_copy_commands(tmp_path: Path) -> None:
    provider = _CodeChatProvider()
    stack = _minimal_stack(tmp_path, provider_factory=lambda _p: provider)
    settings = stack[2]
    settings.set_locale("en")
    settings.set_value("tui.yolo", "1")
    vault = stack[5]
    models = stack[-1]
    vault.add("gpt-key", "x")
    models.register_endpoint("gpt", "http://127.0.0.1:9/v1", "gpt-key")
    models.add_model("gpt/test-model")
    settings.set_value("model.default", "gpt/test-model@v1")
    operation = stack[0].create(
        "Chat lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("*.example.com",)),
    )

    async def exercise() -> None:
        from rich.markdown import Markdown
        from rich.padding import Padding

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            command_input = app.query_one("#command-input", Input)
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "show me code"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                # operator message is a padded bubble, agent reply is Markdown
                assert any(isinstance(m, Padding) for m in messages)
                assert any(isinstance(m, Markdown) for m in messages)
                # /copy grabs the whole reply, /copy code only the fenced block
                command_input.value = "/copy"
                await pilot.press("enter")
                await pilot.pause()
                assert "Here you go:" in getattr(app, "_clipboard", "")
                command_input.value = "/copy code"
                await pilot.press("enter")
                await pilot.pause()
                assert getattr(app, "_clipboard", "") == "print('hi')"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_ops_strip_shows_agents_todo_and_ctx_tokens(tmp_path: Path) -> None:
    stack, provider, operation = _chat_stack(tmp_path)
    stack[2].set_value("tui.yolo", "1")
    kernel = stack[1]
    agents_repo = stack[6]
    kernel.create_plan(
        operation.id,
        "Strip plan",
        [
            PlanTask(id="recon", title="Map the service", risk_class=RiskClass.R1),
            PlanTask(
                id="exploit",
                title="Run the exploit",
                risk_class=RiskClass.R2,
                status=TaskStatus.IN_PROGRESS,
            ),
        ],
        [],
    )
    agents_repo.add(
        AgentRun(
            operation_id=operation.id,
            task_id="recon",
            model_route=("gpt/test-model@v1",),
        )
    )

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            strip_agents = app.query_one("#strip-agents", Static)
            strip_todo = app.query_one("#strip-todo", Static)
            strip_task = app.query_one("#strip-task", Static)
            assert strip_agents.display
            assert "agents 1/1" in str(strip_agents.render())
            assert strip_agents.tooltip is not None
            assert "todo 0/2" in str(strip_todo.render())
            assert "Run the exploit" in str(strip_task.render())
            command_input = app.query_one("#command-input", Input)
            command_input.value = "hello"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            status_bar = str(app.query_one("#status-bar", Static).render())
            assert "ctx 5 tok" in status_bar  # 3 prompt + 2 completion
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


class _ToolCallingProvider:
    """First reply requests a shell command; the second answers in text."""

    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[object] = []

    async def health(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["test-model"]

    async def complete(self, request):  # type: ignore[no-untyped-def]
        from samurai_ai.agents.provider import ChatResponse, ToolCall, Usage

        self.calls += 1
        self.requests.append(request)
        if self.calls == 1:
            return ChatResponse(
                text="",
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="shell_exec",
                        arguments={"command": "echo hello-from-tool"},
                    ),
                ),
                usage=Usage(prompt_tokens=3, completion_tokens=2),
                model_id=request.model_id,
                provider_id="gpt",
                latency_ms=5,
            )
        return ChatResponse(
            text="ran it",
            usage=Usage(prompt_tokens=4, completion_tokens=2),
            model_id=request.model_id,
            provider_id="gpt",
            latency_ms=5,
        )


def test_chat_tool_loop_executes_shell_command(tmp_path: Path) -> None:
    provider = _ToolCallingProvider()
    stack = _minimal_stack(tmp_path, provider_factory=lambda _p: provider)
    settings = stack[2]
    settings.set_locale("en")
    settings.set_value("tui.yolo", "1")
    vault = stack[5]
    models = stack[-1]
    vault.add("gpt-key", "x")
    models.register_endpoint("gpt", "http://127.0.0.1:9/v1", "gpt-key")
    models.add_model("gpt/test-model")
    settings.set_value("model.default", "gpt/test-model@v1")
    operation = stack[0].create(
        "Tool lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("*.example.com",)),
    )

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            command_input = app.query_one("#command-input", Input)
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "run echo for me"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                # round 1: tool call, round 2: final text answer
                assert provider.calls == 2
                text = "\n".join(_message_text(m) for m in messages)
                assert "echo hello-from-tool" in text
                # output is collapsed under an arrow, not spilled as a wall
                assert "▸ ⚙ echo hello-from-tool" in text
                assert "ran it" in text
                # /expand reveals the full collapsed output
                command_input.value = "/expand"
                await pilot.press("enter")
                await pilot.pause()
                text = "\n".join(_message_text(m) for m in messages)
                assert "▾ ⚙ echo hello-from-tool" in text
                assert "  hello-from-tool" in text
                # the tool result was fed back to the model
                second = provider.requests[1]
                history = [
                    getattr(m, "content", "")
                    for m in getattr(second, "messages", [])
                ]
                assert any("hello-from-tool" in entry for entry in history)
                # the run went through the governed execution service
                runs = stack[4].list_runs(operation.id)
                assert [run.tool_id for run in runs] == ["shell.exec"]
                assert runs[0].status.value == "completed"
                # thinking indicator is hidden once the reply is in
                assert not app.query_one("#thinking", Static).display
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_chat_tool_call_denied_by_operator(tmp_path: Path) -> None:
    provider = _ToolCallingProvider()
    stack = _minimal_stack(tmp_path, provider_factory=lambda _p: provider)
    settings = stack[2]
    settings.set_locale("en")
    vault = stack[5]
    models = stack[-1]
    vault.add("gpt-key", "x")
    models.register_endpoint("gpt", "http://127.0.0.1:9/v1", "gpt-key")
    models.add_model("gpt/test-model")
    settings.set_value("model.default", "gpt/test-model@v1")
    operation = stack[0].create(
        "Deny lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("*.example.com",)),
    )

    async def exercise() -> None:
        from samurai_ai.tui.app import AccessPromptScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            command_input = app.query_one("#command-input", Input)
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "run echo for me"
                await pilot.press("enter")
                for _ in range(100):
                    await pilot.pause()
                    if isinstance(app.screen, AccessPromptScreen):
                        break
                assert isinstance(app.screen, AccessPromptScreen)
                assert "echo hello-from-tool" in str(
                    app.screen.query_one("#access-action", Static).render()
                )
                # deny: pick the second option
                options = app.screen.query_one("#access-options", OptionList)
                options.highlighted = 1
                app.screen.dismiss(False)
                await app.workers.wait_for_complete()
                await pilot.pause()
                # no governed run happened, the denial went back to the model
                assert stack[4].list_runs(operation.id) == []
                assert provider.calls == 2
                text = "\n".join(_message_text(m) for m in messages)
                assert "denied by operator" in text
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_chat_without_operation_autocreates_one(tmp_path: Path) -> None:
    provider = _ChatProvider()
    stack = _minimal_stack(tmp_path, provider_factory=lambda _p: provider)
    settings = stack[2]
    settings.set_locale("en")
    settings.set_value("tui.yolo", "1")
    vault = stack[5]
    models = stack[-1]
    vault.add("gpt-key", "x")
    models.register_endpoint("gpt", "http://127.0.0.1:9/v1", "gpt-key")
    models.add_model("gpt/test-model")
    settings.set_value("model.default", "gpt/test-model@v1")
    # note: no operation exists at all

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert app.operation is None
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            command_input = app.query_one("#command-input", Input)
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "map the target"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                # first message spawned its own operation, named from the text
                assert app.operation is not None
                assert app.operation.name == "map the target"
                assert provider.calls == 1
                text = "\n".join(_message_text(m) for m in messages)
                assert "echo: map the target" in text
                assert "operation" not in text.lower() or "not loaded" not in text
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


class _EndlessToolProvider:
    """Always requests a tool until the tools are taken away."""

    def __init__(self) -> None:
        self.calls = 0

    async def health(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["test-model"]

    async def complete(self, request):  # type: ignore[no-untyped-def]
        from samurai_ai.agents.provider import ChatResponse, ToolCall, Usage

        self.calls += 1
        if request.tools:
            return ChatResponse(
                text="",
                tool_calls=(
                    ToolCall(
                        id=f"call-{self.calls}",
                        name="shell_exec",
                        arguments={"command": "true"},
                    ),
                ),
                usage=Usage(prompt_tokens=1, completion_tokens=1),
                model_id=request.model_id,
                provider_id="gpt",
                latency_ms=1,
            )
        return ChatResponse(
            text="final summary after the loop",
            usage=Usage(prompt_tokens=1, completion_tokens=1),
            model_id=request.model_id,
            provider_id="gpt",
            latency_ms=1,
        )


def test_chat_tool_loop_limit_forces_final_answer(tmp_path: Path) -> None:
    provider = _EndlessToolProvider()
    stack = _minimal_stack(tmp_path, provider_factory=lambda _p: provider)
    settings = stack[2]
    settings.set_locale("en")
    settings.set_value("tui.yolo", "1")
    vault = stack[5]
    models = stack[-1]
    vault.add("gpt-key", "x")
    models.register_endpoint("gpt", "http://127.0.0.1:9/v1", "gpt-key")
    models.add_model("gpt/test-model")
    settings.set_value("model.default", "gpt/test-model@v1")
    operation = stack[0].create(
        "Loop lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("*.example.com",)),
    )

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            command_input = app.query_one("#command-input", Input)
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "keep running true"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                # 8 tool rounds + 1 closing call without tools: the operator
                # always gets a final answer instead of silence
                assert provider.calls == 9
                text = "\n".join(_message_text(m) for m in messages)
                assert "final summary after the loop" in text
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_plus_button_creates_operation_tab(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")

    async def exercise() -> None:
        from samurai_ai.tui.app import NewOperationScreen

        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.click("#new-tab")
            await pilot.pause()
            assert isinstance(app.screen, NewOperationScreen)
            name_input = app.screen.query_one("#newop-name", Input)
            name_input.value = "Fresh recon op"
            modes = app.screen.query_one("#newop-modes", OptionList)
            modes.highlighted = 1  # red-team
            await pilot.press("enter")  # name -> scope field
            await pilot.pause()
            await pilot.press("enter")  # scope -> mode list
            await pilot.pause()
            await pilot.press("enter")  # select red-team
            await pilot.pause()
            assert not isinstance(app.screen, NewOperationScreen)
            assert app.operation is not None
            assert app.operation.name == "Fresh recon op"
            assert app.operation.mode.value == "red-team"
            assert "Fresh recon op" in str(app.query_one("#tab-chat", Tab).label)

            switcher = app.query_one("#views", ContentSwitcher)
            assert switcher.current == "journal"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_mouse_toggle_switches_capture_setting(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    settings = stack[2]
    settings.set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            command_input = app.query_one("#command-input", Input)
            # mouse capture is on by default, so the toggle turns it off first
            command_input.value = "/mouse"
            await pilot.press("enter")
            await pilot.pause()
            assert settings.get_value("tui.mouse") == "0"
            command_input.value = "/mouse"
            await pilot.press("enter")
            await pilot.pause()
            assert settings.get_value("tui.mouse") == "1"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_mouse_capture_is_on_until_it_is_turned_off(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    settings = stack[2]
    settings.set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            calls: list[str] = []
            with (
                patch.object(
                    app._driver, "_enable_mouse_support", lambda: calls.append("on"), create=True
                ),
                patch.object(
                    app._driver, "_disable_mouse_support", lambda: calls.append("off"), create=True
                ),
            ):
                app._apply_mouse()
                assert calls == ["on"]
                settings.set_value("tui.mouse", "0")
                app._apply_mouse()
                assert calls == ["on", "off"]
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_copy_without_reply_shows_hint(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            command_input = app.query_one("#command-input", Input)
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "/copy"
                await pilot.press("enter")
                await pilot.pause()
                assert any("nothing to copy" in str(m) for m in messages)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_mouse_drag_selects_and_ctrl_c_copies(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.mouse_down("#journal", offset=(2, 1))
            await pilot.hover("#journal", offset=(20, 1))
            await pilot.mouse_up("#journal", offset=(20, 1))
            await pilot.pause()
            assert app.screen.selections
            selected = app.screen.get_selected_text()
            assert selected
            # focus stays on the command input after selecting in the journal
            assert app.focused is app.query_one("#command-input", Input)
            # nothing is copied until Ctrl+C
            assert not getattr(app, "_clipboard", "")
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert selected.split("\n")[0][:10] in getattr(app, "_clipboard", "")
            app.screen.clear_selection()
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert not app.is_running

    asyncio.run(exercise())


class _SequenceToolProvider:
    """Emits one shell command per user turn, then answers in text."""

    def __init__(self, commands: list[str]) -> None:
        self.commands = list(commands)
        self.calls = 0
        self.requests: list[object] = []
        self._owed = False

    async def health(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["test-model"]

    async def complete(self, request):  # type: ignore[no-untyped-def]
        from samurai_ai.agents.provider import ChatResponse, ToolCall, Usage

        self.calls += 1
        self.requests.append(request)
        usage = Usage(prompt_tokens=3, completion_tokens=2)
        if self._owed or not self.commands:
            self._owed = False
            return ChatResponse(
                text="ran it",
                usage=usage,
                model_id=request.model_id,
                provider_id="gpt",
                latency_ms=5,
            )
        self._owed = True
        return ChatResponse(
            text="",
            tool_calls=(
                ToolCall(
                    id=f"call-{self.calls}",
                    name="shell_exec",
                    arguments={"command": self.commands.pop(0)},
                ),
            ),
            usage=usage,
            model_id=request.model_id,
            provider_id="gpt",
            latency_ms=5,
        )


def _sequence_stack(tmp_path: Path, commands: list[str]):  # type: ignore[no-untyped-def]
    provider = _SequenceToolProvider(commands)
    stack = _minimal_stack(tmp_path, provider_factory=lambda _p: provider)
    settings = stack[2]
    settings.set_locale("en")
    vault = stack[5]
    models = stack[-1]
    vault.add("gpt-key", "x")
    models.register_endpoint("gpt", "http://127.0.0.1:9/v1", "gpt-key")
    models.add_model("gpt/test-model")
    settings.set_value("model.default", "gpt/test-model@v1")
    operation = stack[0].create(
        "Gate lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("*.example.com",)),
    )
    return stack, provider, operation


async def _answer_access(app, pilot, allow: bool) -> None:  # type: ignore[no-untyped-def]
    from samurai_ai.tui.app import AccessPromptScreen

    for _ in range(100):
        await pilot.pause()
        if isinstance(app.screen, AccessPromptScreen):
            break
    assert isinstance(app.screen, AccessPromptScreen)
    app.screen.dismiss(allow)
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_chat_ask_mode_reuses_an_approval_for_the_same_tool(tmp_path: Path) -> None:
    stack, provider, operation = _sequence_stack(
        tmp_path, ["echo one", "echo two"]
    )

    async def exercise() -> None:
        from samurai_ai.tui.app import AccessPromptScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            command_input = app.query_one("#command-input", Input)
            command_input.value = "first"
            await pilot.press("enter")
            await _answer_access(app, pilot, allow=True)
            assert len(stack[4].list_runs(operation.id)) == 1

            # same tool, ordinary command: the stored approval covers it
            command_input.value = "second"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not isinstance(app.screen, AccessPromptScreen)
            assert len(stack[4].list_runs(operation.id)) == 2
            assert provider.calls == 4
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_chat_ask_mode_still_prompts_for_sensitive_commands(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, ["echo one", "cat .env"])

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            command_input = app.query_one("#command-input", Input)
            command_input.value = "first"
            await pilot.press("enter")
            await _answer_access(app, pilot, allow=True)

            # sensitive path: the reusable approval is not enough
            command_input.value = "second"
            await pilot.press("enter")
            await _answer_access(app, pilot, allow=True)
            assert len(stack[4].list_runs(operation.id)) == 2
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_restart_restores_the_transcript(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, ["echo one"])

    async def turn() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            command_input = app.query_one("#command-input", Input)
            command_input.value = "first question"
            await pilot.press("enter")
            await _answer_access(app, pilot, allow=True)
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(turn())

    async def restart() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            lines = [line.text for line in app.query_one("#journal", RichLog).lines]
            text = "\n".join(lines)
            assert "first question" in text
            assert "restored" in text
            assert "end of the restored history" in text
            # the model gets the turn back, behind the current system prompt
            assert app._chat_history[0].role == "system"
            assert any(m.content == "first question" for m in app._chat_history)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(restart())


def test_clear_wipes_the_transcript_here_and_in_the_database(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, ["echo one"])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            command_input = app.query_one("#command-input", Input)
            command_input.value = "first question"
            await pilot.press("enter")
            await _answer_access(app, pilot, allow=True)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.chat_history.tail(operation.id)[1] > 0

            command_input.value = "/clear"
            await pilot.press("enter")
            await pilot.pause()
            assert app.chat_history.tail(operation.id) == ([], 0, False)
            assert app._chat_history == []
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_undo_also_drops_the_persisted_turn(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            command_input = app.query_one("#command-input", Input)
            command_input.value = "doomed turn"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.chat_history.tail(operation.id)[1] > 0

            command_input.value = "/undo"
            await pilot.press("enter")
            await pilot.pause()
            assert app.chat_history.tail(operation.id)[1] == 0
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_chat_never_mode_never_prompts(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, ["cat .env"])
    stack[2].set_value("tui.permission_mode", "never")

    async def exercise() -> None:
        from samurai_ai.tui.app import AccessPromptScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            command_input = app.query_one("#command-input", Input)
            command_input.value = "go"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not isinstance(app.screen, AccessPromptScreen)
            run = stack[4].list_runs(operation.id)[0]
            assert run.tool_id == "shell.exec"
            approval = stack[1].kernel.get_approval(run.approval_id)
            assert approval is not None
            assert approval.decided_by == "never"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_never_mode_prompt_forbids_questions(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_value("tui.permission_mode", "never")
    stack[2].set_locale("ru")

    from samurai_ai.domain.permission import PermissionMode as _Mode

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            prompt = app._chat_system_prompt()
            assert "never" in prompt
            assert "решай сам" in prompt
            # switching back to ask drops the hint from the live history
            app._chat_history = [
                stack_app_message(app),
            ]
            app._set_permission_mode(_Mode.ASK)
            await pilot.pause()
            assert "решай сам" not in app._chat_history[0].content
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def stack_app_message(app):  # type: ignore[no-untyped-def]
    from samurai_ai.agents.provider import Message

    return Message(role="system", content="stale prompt")


def test_plan_mode_blocks_state_changing_calls(tmp_path: Path) -> None:
    stack, provider, operation = _sequence_stack(tmp_path, ["echo one", "echo two"])
    stack[2].set_value("tui.permission_mode", "never")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert "plan" not in str(app.query_one("#status-bar", Static).render())
            await pilot.press("shift+tab")
            await pilot.pause()
            assert app._plan_mode is True
            assert "plan" in str(app.query_one("#status-bar", Static).render())

            command_input = app.query_one("#command-input", Input)
            command_input.value = "go"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            # shell.exec changes state, so it is refused in plan mode
            assert stack[4].list_runs(operation.id) == []
            assert provider.calls == 2
            text = _message_text(provider.requests[1])
            assert "plan mode allows read-only" in text
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_plan_mode_admits_read_only_tools(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_value("tui.permission_mode", "never")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._set_plan_mode(True)
            assert app._tool_is_read_only("process.exec") is True
            assert app._tool_is_read_only("shell.exec") is False
            assert app._tool_is_read_only("nope.tool") is False
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_leaving_plan_mode_asks_even_in_never_mode(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_value("tui.permission_mode", "never")

    async def exercise() -> None:
        from samurai_ai.tui.app import AccessPromptScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._set_plan_mode(True)
            await pilot.press("shift+tab")
            for _ in range(100):
                await pilot.pause()
                if isinstance(app.screen, AccessPromptScreen):
                    break
            assert isinstance(app.screen, AccessPromptScreen)
            app.screen.dismiss(False)
            await pilot.pause()
            assert app._plan_mode is True  # staying in plan mode

            await pilot.press("shift+tab")
            for _ in range(100):
                await pilot.pause()
                if isinstance(app.screen, AccessPromptScreen):
                    break
            app.screen.dismiss(True)
            await pilot.pause()
            assert app._plan_mode is False
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_plan_mode_toggle_is_journalled(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._set_plan_mode(True)
            app._set_plan_mode(False)
            events = [
                event.payload["enabled"]
                for event in stack[0].repository.events(operation.id)
                if event.event_type == "operator.plan_mode"
            ]
            assert events == [True, False]
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_goal_command_starts_a_goal_and_shows_it(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            command_input = app.query_one("#command-input", Input)
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "/goal Get admin on DC01"
                await pilot.press("enter")
                await pilot.pause()
                goal = app._current_goal()
                assert goal is not None
                assert goal.objective == "Get admin on DC01"
                strip = app.query_one("#strip-goal", Static)
                assert strip.display
                assert "goal" in str(strip.render())
                assert any("Get admin on DC01" in str(m) for m in messages)

                command_input.value = "/goal pause"
                await pilot.press("enter")
                await pilot.pause()
                assert app._current_goal().state.value == "paused"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_goal_next_queues_without_replacing(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._handle_goal_command("first")
            app._handle_goal_command("next second")
            assert app._current_goal().objective == "first"
            states = [goal.state.value for goal in app.goals.list(operation.id)]
            assert states == ["active", "queued"]
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_goal_step_budget_blocks_the_goal(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                app.goals.create(operation.id, "short", budget_steps=1)
                app._count_goal_step()
                goal = app._current_goal()
                assert goal.state.value == "blocked"
                assert "step budget 1 spent" in goal.block_reason
                assert any("goal blocked" in str(m) for m in messages)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_goal_commands_without_a_goal_report_cleanly(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                app._handle_goal_command("pause")
                app._handle_goal_command("status")
                app._handle_goal_command("next")
                text = " ".join(str(m) for m in messages)
                assert "no current goal" in text
                assert "give an objective" in text
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


class _BlockingProvider:
    """Holds the first reply until the test releases it."""

    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[object] = []
        # threading.Event: complete() runs inside a worker thread
        self._gate = threading.Event()

    async def health(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["test-model"]

    def release(self) -> None:
        self._gate.set()

    async def complete(self, request):  # type: ignore[no-untyped-def]
        from samurai_ai.agents.provider import ChatResponse, Usage

        self.calls += 1
        self.requests.append(request)
        if self.calls == 1:
            await asyncio.to_thread(self._gate.wait)
        return ChatResponse(
            text="ok",
            usage=Usage(prompt_tokens=3, completion_tokens=2),
            model_id=request.model_id,
            provider_id="gpt",
            latency_ms=5,
        )


def test_ctrl_s_steers_the_running_turn(tmp_path: Path) -> None:
    """Ctrl-S must reach the model inside the same turn, not start a new one."""
    provider = _BlockingProvider()
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack = _minimal_stack(tmp_path, provider_factory=lambda _p: provider)
    settings = stack[2]
    settings.set_locale("en")
    settings.set_value("model.default", "gpt/test-model@v1")
    operation = stack[0].create(
        "Steer lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("*.example.com",)),
    )

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            command_input = app.query_one("#command-input", Input)
            command_input.value = "start"
            await pilot.press("enter")
            await pilot.pause()
            assert app._chat_busy is True

            command_input.value = "no, check the other host"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app._steer_queue == ["no, check the other host"]
            assert command_input.value == ""

            provider.release()
            await app.workers.wait_for_complete()
            await pilot.pause()
            # the steered message entered the same conversation as a user turn
            contents = [
                getattr(message, "content", "")
                for message in provider.requests[-1].messages
            ]
            assert any("check the other host" in str(entry) for entry in contents)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_ctrl_s_without_a_running_turn_sends(tmp_path: Path) -> None:
    stack, provider, operation = _sequence_stack(tmp_path, [])

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            command_input = app.query_one("#command-input", Input)
            command_input.value = "hello"
            await pilot.press("ctrl+s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._steer_queue == []
            assert provider.calls == 1
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_shell_mode_runs_without_a_model_round(tmp_path: Path) -> None:
    stack, provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_value("tui.permission_mode", "never")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            command_input = app.query_one("#command-input", Input)
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "!echo shell-mode-out"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                # no model call, but a governed tool run happened
                assert provider.calls == 0
                runs = stack[4].list_runs(operation.id)
                assert [run.tool_id for run in runs] == ["shell.exec"]
                text = "\n".join(str(m) for m in messages)
                assert "!echo shell-mode-out" in text
            # the output is in the conversation context for the next model turn
            assert any(
                "shell-mode-out" in message.content for message in app._chat_history
            )
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_shell_mode_denied_by_the_operator(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        from samurai_ai.tui.app import AccessPromptScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            command_input = app.query_one("#command-input", Input)
            command_input.value = "!cat .env"
            await pilot.press("enter")
            for _ in range(100):
                await pilot.pause()
                if isinstance(app.screen, AccessPromptScreen):
                    break
            assert isinstance(app.screen, AccessPromptScreen)
            app.screen.dismiss(False)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert stack[4].list_runs(operation.id) == []
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_ctrl_e_expands_collapsed_tool_output(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_value("tui.permission_mode", "never")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            command_input = app.query_one("#command-input", Input)
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "!echo expand-me"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert app._collapsed, "shell run must leave a folded entry"
                assert not any(
                    "  expand-me" in _message_text(m) for m in messages
                )

                await pilot.press("ctrl+e")
                await pilot.pause()
                assert any("  expand-me" in _message_text(m) for m in messages)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_ctrl_b_marks_a_shell_job_as_background(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_value("tui.permission_mode", "never")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # no job running: harmless
            await pilot.press("ctrl+b")
            await pilot.pause()
            app._shell_running = "sleep 5"
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert app._shell_background is True
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_undo_drops_the_last_turn_and_its_output(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_value("tui.permission_mode", "never")
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._chat_history = [
                Message(role="system", content="prompt"),
                Message(role="user", content="first"),
                Message(role="assistant", content="answer"),
                Message(role="user", content="[tool shell_exec] ok"),
                Message(role="user", content="second"),
                Message(role="assistant", content="answer2"),
            ]
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            command_input = app.query_one("#command-input", Input)
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "/undo"
                await pilot.press("enter")
                await pilot.pause()
                contents = [message.content for message in app._chat_history]
                assert contents == ["prompt", "first", "answer", "[tool shell_exec] ok"]
                assert any("undone" in str(m) for m in messages)
            events = [
                event.event_type
                for event in stack[0].repository.events(operation.id)
                if event.event_type == "chat.undo"
            ]
            assert events == ["chat.undo"]
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_undo_on_an_empty_history_reports(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            command_input = app.query_one("#command-input", Input)
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "/undo"
                await pilot.press("enter")
                await pilot.pause()
                assert any("nothing to undo" in str(m) for m in messages)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_compact_folds_old_messages_into_a_digest(tmp_path: Path) -> None:
    stack, provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._chat_history = [Message(role="system", content="prompt")]
            for index in range(20):
                app._chat_history.append(Message(role="user", content=f"u{index}"))
                app._chat_history.append(
                    Message(role="assistant", content=f"a{index}")
                )
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            command_input = app.query_one("#command-input", Input)
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "/compact"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert provider.calls == 1
                # system + digest + the kept tail
                assert len(app._chat_history) == 1 + 1 + app._COMPACT_KEEP
                assert app._chat_history[1].content.startswith("[compacted]")
                assert any("compacted" in str(m) for m in messages)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_compact_without_enough_history_skips(tmp_path: Path) -> None:
    stack, provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._chat_history = [
                Message(role="system", content="prompt"),
                Message(role="user", content="only"),
            ]
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            command_input = app.query_one("#command-input", Input)
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "/compact"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert provider.calls == 0
                assert any("nothing to compact" in str(m) for m in messages)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def _palette(app):  # type: ignore[no-untyped-def]
    from textual.command import CommandPalette

    return app.screen if isinstance(app.screen, CommandPalette) else None


def test_command_palette_opens_and_filters(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+p")
            await pilot.pause()
            palette = _palette(app)
            assert palette is not None

            await pilot.press("i", "n", "s", "p")
            await pilot.pause()
            rows = [str(option.prompt) for option in palette.query("OptionList").first().options]
            assert any(row.startswith("/inspect") for row in rows)
            assert not any(row.startswith("/exit") for row in rows)

            await pilot.press("escape")
            await pilot.pause()
            assert _palette(app) is None
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_palette_runs_an_action(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                await pilot.press("ctrl+p")
                await pilot.pause()
                for char in "status":
                    await pilot.press(char)
                await pilot.pause()
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                text = "\n".join(str(message) for message in messages)
                assert "[status]" in text
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_palette_asks_for_an_argument(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        from samurai_ai.tui.screens import ArgumentScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                await pilot.press("ctrl+p")
                await pilot.pause()
                for char in "scope":
                    await pilot.press(char)
                await pilot.pause()
                await pilot.press("enter")
                for _ in range(50):
                    await pilot.pause()
                    if isinstance(app.screen, ArgumentScreen):
                        break
                assert isinstance(app.screen, ArgumentScreen)
                app.screen.query_one("#argument-input", Input).value = "a.example.com"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                text = "\n".join(str(message) for message in messages)
                assert "a.example.com" in text
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_palette_cancelled_argument_runs_nothing(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        from samurai_ai.tui.screens import ArgumentScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+p")
            await pilot.pause()
            for char in "scope":
                await pilot.press(char)
            await pilot.pause()
            await pilot.press("enter")
            for _ in range(50):
                await pilot.pause()
                if isinstance(app.screen, ArgumentScreen):
                    break
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, ArgumentScreen)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_question_mark_opens_the_key_overlay(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        from samurai_ai.tui.screens import HelpScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            command_input = app.query_one("#command-input", Input)
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            # the field did not swallow the character
            assert command_input.value == ""

            await pilot.press("pagedown")
            await pilot.pause()
            assert app.screen.query_one("#help-body").scroll_offset.y >= 0
            await pilot.press("q")
            await pilot.pause()
            assert not isinstance(app.screen, HelpScreen)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_question_mark_types_when_the_field_has_text(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        from samurai_ai.tui.screens import HelpScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            command_input = app.query_one("#command-input", Input)
            command_input.value = "ready"
            await pilot.press("question_mark")
            await pilot.pause()
            assert not isinstance(app.screen, HelpScreen)
            assert command_input.value == "ready?"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_help_command_opens_the_overlay_with_russian_sections(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("ru")

    async def exercise() -> None:
        from samurai_ai.tui.screens import HelpScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._handle_command("/help")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            labels = [
                str(node.render())
                for node in app.screen.query(".help-section")
            ]
            assert "Ввод" in labels
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, HelpScreen)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def _seed_run(stack, operation_id: str, command: str = "echo inspector"):  # type: ignore[no-untyped-def]
    """Run a shell command through the governed service, approving it."""
    from samurai_ai.domain.execution import ApprovalRequiredError

    try:
        return stack[4].run_tool(operation_id, "shell.exec", {"command": command}).run
    except ApprovalRequiredError as exc:
        stack[1].approve(exc.approval_id, decided_by="test")
        result = stack[4].run_tool(
            operation_id, "shell.exec", {"command": command}, approval_id=exc.approval_id
        )
        return result.run


def test_inspector_shows_every_section(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")
    run = _seed_run(stack, operation.id)

    async def exercise() -> None:
        from samurai_ai.tui.screens import InspectorScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._handle_command(f"/inspect {run.id}")
            await pilot.pause()
            assert isinstance(app.screen, InspectorScreen)
            headings = [
                str(node.render()) for node in app.screen.query(".inspector-section")
            ]
            assert "TOOL" in headings
            assert "PARAMETERS (redacted)" in headings
            assert "SCOPE DECISION (re-checked now)" in headings
            assert "APPROVAL" in headings
            assert "EGRESS" in headings
            assert "ARTIFACTS" in headings
            assert "OUTPUT" in headings
            assert "TRUST" in headings
            body = "\n".join(
                str(node.render()) for node in app.screen.query(".inspector-line")
            )
            assert "echo inspector" in body
            assert "tool.run shell.exec" in body
            assert "decided by test" in body
            # empty groups say so instead of staying blank
            assert "none" in body
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_inspector_ctrl_i_opens_the_newest_run(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")
    _seed_run(stack, operation.id, "echo first")
    newest = _seed_run(stack, operation.id, "echo second")

    async def exercise() -> None:
        from samurai_ai.tui.screens import InspectorScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+i")
            await pilot.pause()
            assert isinstance(app.screen, InspectorScreen)
            assert newest.id in str(app.screen.query_one("#inspector-title", Static).render())
            await pilot.press("q")
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_inspector_unknown_run_reports(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                app._handle_command("/inspect run_missing")
                await pilot.pause()
                assert any("run_missing" in str(message) for message in messages)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_inspector_with_no_runs_reports(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                await pilot.press("ctrl+i")
                await pilot.pause()
                assert any("no runs" in str(message) for message in messages)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_inspector_copy_action_copies_the_command(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")
    run = _seed_run(stack, operation.id, "echo copy-me")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        copied: list[str] = []
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            with patch.object(app, "copy_to_clipboard", side_effect=copied.append):
                app._handle_command(f"/inspect {run.id}")
                await pilot.pause()
                await pilot.press("c")
                await pilot.pause()
            assert copied == ["echo copy-me"]
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_welcome_pane_lists_the_ways_to_start(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            welcome = app.query_one("#welcome", Static)
            assert welcome.display
            text = str(welcome.render())
            assert "No operation open" in text
            assert "Ctrl-P" in text
            assert "/new" in text
            assert "/open" not in text  # nothing to continue yet
            assert "?" in text
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_welcome_hides_once_an_operation_is_open(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert not app.query_one("#welcome", Static).display
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_open_command_loads_the_latest_operation(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            # close the auto-opened operation the way the operator would
            app.operation = None
            app._refresh_chrome()
            assert app.query_one("#welcome", Static).display
            assert "/open" in str(app.query_one("#welcome", Static).render())
            app._handle_command("/open")
            await pilot.pause()
            assert app.operation is not None
            assert app.operation.id == operation.id
            assert not app.query_one("#welcome", Static).display
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_open_without_history_reports(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                app._handle_command("/open")
                await pilot.pause()
                assert any("no earlier operation" in str(message) for message in messages)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def _session_stack(tmp_path: Path, responses: list[str] | None = None):
    """A stack whose subagent can run unattended (yolo, English)."""
    stack, provider, operation = _sequence_stack(tmp_path, responses or [])
    stack[2].set_locale("en")
    stack[2].set_value("tui.yolo", "1")
    return stack, provider, operation


def test_subagent_command_starts_a_child(tmp_path: Path) -> None:
    stack, _provider, operation = _session_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._handle_command("/subagent look around")
            for _ in range(20):
                await pilot.pause()
            children = app.child_runs()
            assert len(children) == 1
            assert children[0].created_by == "subagent"
            strip = str(app.query_one("#strip-agents", Static).render())
            # the child may already be done, so the strip shows either label
            assert "child" in strip or "/1" in strip
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_second_subagent_reports_the_running_child(tmp_path: Path) -> None:
    stack, _provider, operation = _session_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            first = await app.spawn_subagent("first job")
            assert first is not None
            assert await app.spawn_subagent("second job") is None
            assert [run.id for run in app.child_runs()] == [first]
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_subagent_bootstraps_a_plan_task(tmp_path: Path) -> None:
    stack, _provider, operation = _session_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.spawn_subagent("scan the host")
            plan = stack[1].get_current_plan(operation.id)
            assert plan is not None
            assert plan.tasks[0].title == "scan the host"
            assert plan.tasks[0].status is TaskStatus.IN_PROGRESS
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_sessions_screen_lists_and_opens_a_child(tmp_path: Path) -> None:
    stack, _provider, operation = _session_stack(tmp_path)

    async def exercise() -> None:
        from samurai_ai.tui.screens import SessionDetailScreen, SessionsScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            run_id = await app.spawn_subagent("look around")
            app._show_sessions()
            await pilot.pause()
            assert isinstance(app.screen, SessionsScreen)
            assert app.screen.query_one("#sessions-list", OptionList).option_count == 1
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, SessionDetailScreen)
            assert run_id is not None
            assert run_id in str(app.screen.query_one("#session-title", Static).render())
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_sessions_screen_empty_state(tmp_path: Path) -> None:
    stack, _provider, operation = _session_stack(tmp_path)

    async def exercise() -> None:
        from samurai_ai.tui.screens import SessionsScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._show_sessions()
            await pilot.pause()
            assert isinstance(app.screen, SessionsScreen)
            empty = app.screen.query_one("#sessions-empty", Static)
            assert "subagent" in str(empty.render())
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_stopping_a_child_fails_the_run_and_frees_the_task(tmp_path: Path) -> None:
    stack, _provider, operation = _session_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            run_id = await app.spawn_subagent("look around")
            assert run_id is not None
            app._stop_session(run_id)
            for _ in range(40):
                await pilot.pause()
                await asyncio.sleep(0.02)
            run = stack[6].get(run_id)
            assert run is not None
            assert run.status is AgentRunStatus.FAILED
            plan = stack[1].get_current_plan(operation.id)
            assert plan is not None
            assert plan.tasks[0].status is TaskStatus.PENDING
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_agent_spawn_tool_starts_a_child(tmp_path: Path) -> None:
    from samurai_ai.agents.provider import ToolCall

    stack, _provider, operation = _session_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            message = await app._run_chat_tool(
                ToolCall(id="1", name="agent_spawn", arguments={"instruction": "look"})
            )
            assert "started in the background" in message.content
            assert len(app.child_runs()) == 1
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_subagent_hands_its_notes_back_to_the_chat(tmp_path: Path) -> None:

    stack, _provider, operation = _session_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._handle_command("/subagent map the surface")
            for _ in range(30):
                await pilot.pause()
            handed = [m for m in app._chat_history if m.content.startswith("[tool agent_spawn")]
            assert handed, "the child never handed anything back"
            assert "status=" in handed[-1].content
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_subagent_digest_carries_blackboard_notes(tmp_path: Path) -> None:

    from samurai_ai.domain.agents import AgentRun

    stack, _provider, operation = _session_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            run = AgentRun(operation_id=operation.id, task_id="t1")
            app.service.repository.append_event(
                OperationEvent(
                    operation_id=operation.id,
                    event_type="agent.note",
                    payload={"run_id": run.id, "key": f"run.{run.id}.found", "value": "port 443"},
                )
            )
            digest = app._subagent_digest(run)
            assert "port 443" in digest
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def _png(tmp_path: Path, name: str = "shot.png", size: int = 2048) -> Path:
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    path = work / name
    path.write_bytes(b"\x89PNG" + b"0" * size)
    return path


def _vision_stack(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A stack on a vision-capable model, unattended (yolo)."""
    stack, provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_value("model.default", "openai/gpt-4o@v1")
    stack[2].set_value("tui.yolo", "1")
    return stack, provider, operation


async def _open(app, pilot, title: str) -> str:  # type: ignore[no-untyped-def]
    message = await app._run_chat_tool(
        ToolCall(id="1", name="open_intent", arguments={"title": title})
    )
    assert message.content.startswith("[tool open_intent] ")
    return message.content.split()[2]


def test_open_intent_bootstraps_a_task_and_origin(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            intent_id = await _open(app, pilot, "check /api/v1")
            plan = stack[1].get_current_plan(operation.id)
            assert plan is not None
            kinds = [node.kind.value for node in app.graph.for_operation(operation.id)]
            assert kinds == ["origin", "intent"]
            assert app.graph.get(intent_id).state.value == "open"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_record_fact_starts_the_intent_first(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            intent_id = await _open(app, pilot, "check /api/v1")
            message = await app._run_chat_tool(
                ToolCall(
                    id="1",
                    name="record_fact",
                    arguments={"intent": intent_id, "summary": "12 endpoints, no auth"},
                )
            )
            assert "recorded for" in message.content
            assert app.graph.get(intent_id).state.value == "running"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_close_intent_without_a_fact_returns_the_reason(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            intent_id = await _open(app, pilot, "check /api/v1")
            message = await app._run_chat_tool(
                ToolCall(
                    id="1",
                    name="close_intent",
                    arguments={"intent": intent_id, "outcome": "done"},
                )
            )
            assert "no fact yet" in message.content
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_propose_finding_needs_an_artifact_backed_fact(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            intent_id = await _open(app, pilot, "check /api/v1")
            bare = await app._run_chat_tool(
                ToolCall(
                    id="1",
                    name="record_fact",
                    arguments={"intent": intent_id, "summary": "no evidence"},
                )
            )
            fact_id = bare.content.split()[2]
            refused = await app._run_chat_tool(
                ToolCall(
                    id="1", name="propose_finding", arguments={"fact": fact_id, "title": "IDOR"}
                )
            )
            assert "no artifact" in refused.content

            artifact = stack[1].register_artifact(operation.id, ArtifactKind.COMMAND_OUTPUT, "body")
            good = await app._run_chat_tool(
                ToolCall(
                    id="1",
                    name="record_fact",
                    arguments={
                        "intent": intent_id,
                        "summary": "IDOR confirmed",
                        "artifact_ids": [artifact.id],
                    },
                )
            )
            proven = good.content.split()[2]
            message = await app._run_chat_tool(
                ToolCall(
                    id="1",
                    name="propose_finding",
                    arguments={"fact": proven, "title": "IDOR on /user", "severity": "medium"},
                )
            )
            assert "registered on" in message.content
            finding_id = message.content.split()[2]
            finding = stack[1].kernel.get_finding(finding_id)
            assert finding.evidence_ids == (artifact.id,)
            assert len(stack[1].kernel.list_findings(operation.id)) == 1  # no stray finding
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_the_frontier_reaches_the_system_prompt(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await _open(app, pilot, "check /api/v1")
            prompt = app._chat_system_prompt()
            assert "exploration graph" in prompt
            assert "check /api/v1" in prompt
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_repeating_intents_raise_the_stagnation_note(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            stuck = [await _open(app, pilot, "Enumerate endpoints on /api") for _ in range(3)]
            assert app._progress.last_stuck  # the loop would inject it here
            app._note_stagnation()
            notes = [
                m.content for m in app._chat_history if m.content.startswith("[progress]")
            ]
            assert notes
            assert len(stuck) == 3
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def _seed_graph(app, stack, operation, intent: bool = True):  # type: ignore[no-untyped-def]
    """A task with an origin, an intent and a fact — enough to fill a board."""
    plan = stack[1].create_plan(
        operation.id, "plan", [PlanTask(id="recon", title="Recon")], []
    )
    task_id = str(plan.tasks[0].id)
    root = app.graph.origin(operation.id, task_id, "gate lab")
    node = app.graph.open_intent(operation.id, task_id, "check /api/v1", root.id)
    app.graph.start_intent(node.id)
    return task_id, root, node


def test_graph_board_lists_open_work_and_hints(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            task_id, _root, intent = _seed_graph(app, stack, operation)
            app.graph.add_hint(operation.id, task_id, "do not touch /admin")

            rows = app._graph_rows(False)
            labels = [label for kind, _id, label in rows if kind == "item"]
            assert any("check /api/v1" in label for label in labels)
            assert any("do not touch /admin" in label for label in labels)
            assert intent.id in [item_id for kind, item_id, _l in rows if kind == "item"]
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_graph_board_hides_closed_nodes_until_filtered(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _task, _root, intent = _seed_graph(app, stack, operation)
            app.graph.close_intent(intent.id, "exhausted", "nothing there")

            assert all(i != intent.id for _k, i, _l in app._graph_rows(False))
            assert any(i == intent.id for _k, i, _l in app._graph_rows(True))
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_graph_board_stop_and_reset_actions(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _task, _root, intent = _seed_graph(app, stack, operation)

            app._graph_stop(intent.id)
            await pilot.pause()
            assert app.graph.get(intent.id).state.value == "blocked"

            app._graph_reset(intent.id)
            await pilot.pause()
            assert app.graph.get(intent.id).state.value == "open"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_graph_detail_shows_the_lineage_to_the_origin(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _task, root, intent = _seed_graph(app, stack, operation)

            sections = app._graph_sections(app.graph.get(intent.id))
            rendered = "\n".join(line for _heading, lines in sections for line in lines)
            assert "gate lab" in rendered
            assert root.id in rendered
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_graph_board_reports_an_empty_graph(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert app._graph_rows(False) == []
            app._handle_command("/graph")
            await pilot.pause()
            assert app._graph_rows(False) == []
            await pilot.press("escape")
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_constraints_reach_the_prompt_above_the_frontier(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            task_id, _root, _intent = _seed_graph(app, stack, operation)
            app.constraints.add(
                operation.id, task_id, ConstraintKind.DENY, "do not touch /admin"
            )

            prompt = app._chat_system_prompt()

            assert "OPERATOR CONSTRAINTS" in prompt
            assert "do not touch /admin" in prompt
            assert prompt.index("OPERATOR CONSTRAINTS") < prompt.index("exploration graph")
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_constraint_command_adds_lists_and_removes(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            task_id, _root, _intent = _seed_graph(app, stack, operation)

            app._handle_command("/constraint deny do not touch /admin")
            await pilot.pause()
            stored = app.constraints.for_task(task_id)
            assert [row.text for row in stored] == ["do not touch /admin"]

            app._handle_command("/constraint")
            await pilot.pause()
            app._handle_command(f"/constraint rm {stored[0].id}")
            await pilot.pause()
            assert app.constraints.for_task(task_id) == []
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_exhausted_rounds_settle_the_graph_before_the_summary(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            task_id, root, _intent = _seed_graph(app, stack, operation)
            app._graph_used = True
            intent = app.graph.open_intent(operation.id, task_id, "still open", root.id)

            seen: list[tuple[str, ...]] = []

            async def fake_request(operation_id, model_id, reasoning, tools):  # type: ignore[no-untyped-def]
                seen.append(tuple(spec.name for spec in tools))
                return None

            original = app._request_chat_completion
            app._request_chat_completion = fake_request  # type: ignore[method-assign]
            await app._settle(operation.id, "gpt/test-model@v1", None)
            app._request_chat_completion = original  # type: ignore[method-assign]

            assert seen == [("record_fact", "close_intent", "propose_finding")]
            assert app.graph.get(intent.id).state.value == "open"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_record_traffic_tool_and_bind_command(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            finding = stack[1].create_finding(operation.id, "IDOR on /user")

            message = await app._run_chat_tool(
                ToolCall(
                    id="1",
                    name="record_traffic",
                    arguments={
                        "method": "GET",
                        "url": "https://x/user/2",
                        "status": 200,
                        "response_body": '{"email":"a@b"}',
                    },
                )
            )
            assert "record_traffic" in message.content
            pair_id = message.content.split()[2]

            app._handle_command(f"/traffic bind {pair_id} {finding.id} proof")
            await pilot.pause()
            bindings = app.traffic.bindings(finding.id)
            assert [binding.role.value for binding in bindings] == ["proof"]
            assert stack[1].kernel.get_finding(finding.id).evidence_version == 1
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_retest_command_records_a_verdict(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            artifact = stack[1].register_artifact(
                operation.id, ArtifactKind.COMMAND_OUTPUT, "body"
            )
            finding = stack[1].create_finding(operation.id, "IDOR on /user")
            stack[1].attach_evidence(finding.id, artifact.id)
            stack[1].validate_finding(finding.id)

            app._handle_command(f"/retest {finding.id}")
            await pilot.pause()
            retest = app.retests.for_finding(finding.id)[0]
            assert retest.open

            app._handle_command(
                f"/retest verdict {retest.id} inconclusive the target sits behind a captcha"
                " | curl -> captcha page"
            )
            await pilot.pause()
            finished = app.retests.get(retest.id)
            assert finished.verdict.value == "inconclusive"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_finding_detail_lists_bound_traffic(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            finding = stack[1].create_finding(operation.id, "IDOR on /user")
            pair = app.traffic.record(operation.id, "GET", "https://x/user/2", 200)
            app.traffic.bind(finding.id, pair.id, TrafficRole.PROOF)

            lines = app._traffic_lines(finding.id)
            assert lines == [f"{pair.id} (proof) GET https://x/user/2 -> 200"]
            assert app._traffic_lines("fnd_missing") == []

            app._show_finding_detail(finding.id)
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_retest_fixed_accepts_a_control_pair(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            artifact = stack[1].register_artifact(
                operation.id, ArtifactKind.COMMAND_OUTPUT, "body"
            )
            finding = stack[1].create_finding(operation.id, "IDOR on /user")
            stack[1].attach_evidence(finding.id, artifact.id)
            stack[1].validate_finding(finding.id)
            pair = app.traffic.record(
                operation.id, "GET", "https://x/user/1", 200, response_body="own"
            )

            app._handle_command(f"/retest {finding.id}")
            await pilot.pause()
            retest = app.retests.for_finding(finding.id)[0]

            app._handle_command(
                f"/retest verdict {retest.id} fixed 403 on another object"
                f" | GET /user/2 -> 403 | {pair.id}"
            )
            await pilot.pause()
            finished = app.retests.get(retest.id)
            assert finished.control_pair == pair.id
            assert [(b.traffic_id, b.role.value) for b in app.traffic.bindings(finding.id)] == [
                (pair.id, "verification")
            ]
            assert stack[1].kernel.get_finding(finding.id).status is FindingStatus.FIXED
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_commands_are_gated_by_role(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.access.configure("https://idp.test", "client")  # nobody is signed in

            app._handle_command("/whoami")  # open command, reports the state
            await pilot.pause()
            app._handle_command("/tasks")
            await pilot.pause()
            log = " ".join(str(line.text) for line in app.query("RichLog").first().lines)
            assert "not signed in" in log
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_the_actor_is_the_signed_in_subject(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert app._actor() == "local"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_team_command_lists_the_owner(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.team.claim_owner(operation.id, app.access.current())
            app._handle_command("/team")
            await pilot.pause()
            log = " ".join(str(line.text) for line in app.query("RichLog").first().lines)
            assert "owner: local" in log
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_a_board_note_lands_on_the_target(tmp_path: Path) -> None:
    from samurai_ai.domain.teamwork import NoteTarget

    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            finding = stack[1].create_finding(operation.id, "IDOR on /user")
            app.team.claim_owner(operation.id, app.access.current())
            app.team.note(
                operation.id,
                NoteTarget.FINDING,
                finding.id,
                "проверь id 2",
                actor=app.access.current(),
            )
            await pilot.pause()
            assert "проверь id 2" in app._note_lines(finding.id)[0]
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_skills_gaps_report_lists_misses(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.skills.record_gap("invented-skill", app._memory_workspace())

            app._handle_command("/skills gaps")
            await pilot.pause()
            lines = [line.text for line in app.query_one("#journal", RichLog).lines]
            assert any("invented-skill" in line and "misses=1" in line for line in lines)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_a_verdict_on_the_board_teaches_a_lesson(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            artifact = stack[1].register_artifact(
                operation.id, ArtifactKind.COMMAND_OUTPUT, "body"
            )
            finding = stack[1].create_finding(operation.id, "IDOR on /user")
            stack[1].attach_evidence(finding.id, artifact.id)

            app._finding_validate(finding.id)
            await pilot.pause()

            lessons = app.lessons.all(app._memory_workspace())
            assert len(lessons) == 1
            assert lessons[0].kind.value == "positive"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_shadow_logs_what_inject_would_show(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            artifact = stack[1].register_artifact(
                operation.id, ArtifactKind.COMMAND_OUTPUT, "body"
            )
            finding = stack[1].create_finding(operation.id, "IDOR on /user")
            stack[1].attach_evidence(finding.id, artifact.id)
            app._finding_validate(finding.id)
            await pilot.pause()

            app._log_shadow_lessons()
            shadow = [
                event
                for event in app.service.repository.events(operation.id)
                if event.event_type == "lesson.shadow"
            ]
            assert shadow and shadow[0].payload["lesson_ids"]
            assert app.lessons.digest(app._memory_workspace()) == ("", ())
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_injected_lesson_reaches_the_prompt(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            artifact = stack[1].register_artifact(
                operation.id, ArtifactKind.COMMAND_OUTPUT, "body"
            )
            finding = stack[1].create_finding(operation.id, "IDOR on /user")
            stack[1].attach_evidence(finding.id, artifact.id)
            app._finding_validate(finding.id)
            await pilot.pause()

            lesson = app.lessons.all(app._memory_workspace())[0]
            app._handle_command("/lessons mode inject")
            caught = app._chat_system_prompt()
            assert "IDOR on /user" not in caught  # candidate, not adopted yet

            app._handle_command(f"/lessons adopt {lesson.id}")
            prompt = app._chat_system_prompt()
            assert "IDOR on /user" in prompt
            assert "earlier outcomes" in prompt
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_repeating_one_direction_raises_a_stagnation_note(tmp_path: Path) -> None:
    from samurai_ai.tui.progress import TerminalReason

    stack, _provider, operation = _session_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            for _ in range(3):
                await app._run_chat_tool(
                    ToolCall(id="1", name="shell_exec", arguments={"command": "nmap -sV x"})
                )
            app._note_stagnation()
            await pilot.pause()
            notes = [m.content for m in app._chat_history if m.content.startswith("[progress]")]
            assert notes and "nmap" in notes[0]
            lines = [line.text for line in app.query_one("#journal", RichLog).lines]
            assert any("repeating" in line for line in lines)
            app._end_turn(TerminalReason.ROUNDS)
            reasons = [
                event.payload["reason"]
                for event in app.service.repository.events(operation.id)
                if event.event_type == "chat.turn.end"
            ]
            assert reasons == ["rounds"]
            app._carry_previous_reason()
            assert app._chat_history[-1].content.startswith("[previous turn]")
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_read_image_returns_ocr_and_a_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from samurai_ai.agents.provider import ToolCall

    path = _png(tmp_path)
    monkeypatch.chdir(path.parent)
    monkeypatch.setattr("samurai_ai.tui.chat._ocr", lambda _p: "hello from ocr")
    stack, provider, operation = _vision_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            message = await app._run_chat_tool(
                ToolCall(id="1", name="read_image", arguments={"path": "shot.png"})
            )
            assert "hello from ocr" in message.content
            assert "ran it" in message.content  # the provider's description
            assert provider.requests[-1].images  # the image really rode along
            artifacts = [
                a for a in stack[1].kernel.list_artifacts(operation.id) if a.kind.value == "other"
            ]
            assert artifacts and artifacts[0].summary == "image: shot.png"
            assert artifacts[0].size_bytes > 0
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_read_image_refuses_a_path_outside_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from samurai_ai.agents.provider import ToolCall

    path = _png(tmp_path)
    monkeypatch.chdir(path.parent)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG")
    monkeypatch.setattr("samurai_ai.tui.chat._ocr", lambda _p: "")
    stack, _provider, operation = _vision_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            message = await app._run_chat_tool(
                ToolCall(id="1", name="read_image", arguments={"path": str(outside)})
            )
            assert "outside the working directory" in message.content
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_read_image_refuses_type_and_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from samurai_ai.agents.provider import ToolCall

    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "notes.txt").write_text("x")
    big = work / "big.png"
    big.write_bytes(b"\x89PNG" + b"0" * (8 * 1024 * 1024 + 1))
    monkeypatch.chdir(work)
    stack, _provider, operation = _vision_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            for name, expected in (("notes.txt", "unsupported type"), ("big.png", "limit")):
                message = await app._run_chat_tool(
                    ToolCall(id="1", name="read_image", arguments={"path": name})
                )
                assert expected in message.content
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_read_image_without_vision_or_ocr_explains_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from samurai_ai.agents.provider import ToolCall

    path = _png(tmp_path)
    monkeypatch.chdir(path.parent)
    monkeypatch.setattr("samurai_ai.tui.chat._ocr", lambda _p: None)
    stack, _provider, operation = _sequence_stack(tmp_path, [])

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            message = await app._run_chat_tool(
                ToolCall(id="1", name="read_image", arguments={"path": "shot.png"})
            )
            assert "tesseract" in message.content
            assert "/model" in message.content
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_read_image_needs_a_trusted_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from samurai_ai.agents.provider import ToolCall

    path = _png(tmp_path)
    monkeypatch.chdir(path.parent)
    stack, _provider, operation = _sequence_stack(tmp_path, [])

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._dir_trusted = False
            message = await app._run_chat_tool(
                ToolCall(id="1", name="read_image", arguments={"path": "shot.png"})
            )
            assert "not trusted" in message.content
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_memory_learn_tool_writes_a_redacted_entry(tmp_path: Path) -> None:
    from samurai_ai.agents.provider import ToolCall
    from samurai_ai.domain.memory import MemoryKind

    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[5].add("leak", "ghp_" + "z" * 30)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            message = await app._run_chat_tool(
                ToolCall(
                    id="1",
                    name="memory_learn",
                    arguments={
                        "kind": "failure",
                        "title": "  stale /etc/profile  breaks every shell ",
                        "body": "token ghp_" + "z" * 30,
                    },
                )
            )
            assert "stored" in message.content
            entry = stack[15].list_entries()[0]
            assert entry.kind is MemoryKind.FAILURE
            assert entry.created_by == "agent"
            assert entry.title == "stale /etc/profile breaks every shell"
            assert "ghp_" not in entry.body
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_memory_learn_is_off_with_the_setting(tmp_path: Path) -> None:
    from samurai_ai.agents.provider import ToolCall

    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_value("memory.agent_writes", "0")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            message = await app._run_chat_tool(
                ToolCall(id="1", name="memory_learn", arguments={"kind": "decision", "title": "x"})
            )
            assert "off" in message.content
            assert stack[15].list_entries() == []
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_memory_reaches_the_system_prompt(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.memory.add(app._memory_workspace(), "canary method", "always try HEAD first")
            prompt = app._chat_system_prompt()
            assert "canary method" in prompt
            assert "always try HEAD first" in prompt
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_agent_spawn_tool_is_blocked_in_plan_mode(tmp_path: Path) -> None:
    from samurai_ai.agents.provider import ToolCall

    stack, _provider, operation = _session_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._set_plan_mode(True)
            message = await app._run_chat_tool(
                ToolCall(id="1", name="agent_spawn", arguments={"instruction": "look"})
            )
            assert "blocked" in message.content
            assert app.child_runs() == []
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_subagent_asks_for_approvals_in_ask_mode(tmp_path: Path) -> None:
    from samurai_ai.tui.screens import AccessPromptScreen

    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            job = asyncio.create_task(app.spawn_subagent("look around"))
            for _ in range(40):
                await pilot.pause()
                if isinstance(app.screen, AccessPromptScreen):
                    break
            assert isinstance(app.screen, AccessPromptScreen)
            subtitle = str(app.screen.query_one("#access-action", Static).render())
            assert "agent_" in subtitle
            app.screen.dismiss(True)
            await asyncio.wait_for(job, 20)
            for _ in range(20):
                await pilot.pause()
            approvals = [
                item
                for item in stack[1].kernel.list_approvals(operation.id)
                if item.action.startswith("model.call")
            ]
            assert any(item.decided_by == "subagent" for item in approvals)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def _board_stack(tmp_path: Path):
    """A stack with a plan, a gate, findings and one evidence artifact."""
    stack, provider, _operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")
    operation = stack[0].create(
        "Board lab", OperationMode.BUG_BOUNTY, ScopeContract(includes=("a.test",))
    )
    stack[1].create_plan(
        operation.id,
        "Board plan",
        [
            PlanTask(id="recon", title="Recon the host", risk_class=RiskClass.R1),
            PlanTask(
                id="exploit",
                title="Exploit it",
                risk_class=RiskClass.R2,
                depends_on=("recon",),
            ),
        ],
        [StageGate(id="recon-done", name="Recon complete")],
    )
    finding = stack[1].create_finding(
        operation.id, "SQLi in login", FindingSeverity.HIGH, summary="payload works"
    )
    duplicate = stack[1].create_finding(operation.id, "Same SQLi", FindingSeverity.LOW)
    artifact = stack[1].register_artifact(
        operation.id, ArtifactKind.NOTE, "note.txt", "response dump"
    )
    return stack, provider, operation, finding, duplicate, artifact


def _highlight(options: OptionList, option_id: str) -> None:
    index = next(
        index for index, option in enumerate(options.options) if option.id == option_id
    )
    options.highlighted = index


def test_task_board_opens_and_starts_a_task(tmp_path: Path) -> None:
    stack, _provider, operation, *_rest = _board_stack(tmp_path)

    async def exercise() -> None:
        from samurai_ai.tui.screens import BoardScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+t")
            await pilot.pause()
            assert isinstance(app.screen, BoardScreen)
            listed = app.screen.query_one("#board-list", OptionList)
            assert "recon" in str(listed.options[1].prompt)
            listed.highlighted = 1
            await pilot.press("s")
            await pilot.pause()
            plan = stack[1].get_current_plan(operation.id)
            assert plan is not None
            assert plan.task_by_id("recon").status is TaskStatus.IN_PROGRESS  # type: ignore[union-attr]
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_task_board_completes_and_skips(tmp_path: Path) -> None:
    stack, _provider, operation, *_rest = _board_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._show_task_board()
            await pilot.pause()
            listed = app.screen.query_one("#board-list", OptionList)
            listed.highlighted = 1
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            plan = stack[1].get_current_plan(operation.id)
            assert plan is not None
            assert plan.task_by_id("recon").status is TaskStatus.DONE  # type: ignore[union-attr]
            # exploit is the only open task now, still first in the open group
            _highlight(app.screen.query_one("#board-list", OptionList), "exploit")
            await pilot.press("k")
            await pilot.pause()
            plan = stack[1].get_current_plan(operation.id)
            assert plan is not None
            assert plan.task_by_id("exploit").status is TaskStatus.SKIPPED  # type: ignore[union-attr]
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_task_board_passes_a_gate(tmp_path: Path) -> None:
    stack, _provider, operation, *_rest = _board_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._show_task_board()
            await pilot.pause()
            _highlight(
                app.screen.query_one("#board-list", OptionList), "gate:recon-done"
            )
            await pilot.press("g")
            await pilot.pause()
            plan = stack[1].get_current_plan(operation.id)
            assert plan is not None
            assert plan.gates[0].status is GateStatus.PASSED
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_task_board_filter_reveals_finished_rows(tmp_path: Path) -> None:
    stack, _provider, operation, *_rest = _board_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            stack[1].start_task(operation.id, "recon")
            stack[1].complete_task(operation.id, "recon")
            app._show_task_board()
            await pilot.pause()
            listed = app.screen.query_one("#board-list", OptionList)
            before = len(listed.options)
            await pilot.press("f")
            await pilot.pause()
            after = len(app.screen.query_one("#board-list", OptionList).options)
            assert after >= before
            headings = [
                str(option.prompt) for option in listed.options if str(option.id).startswith("g-")
            ]
            assert any("finished" in heading for heading in headings)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_task_detail_shows_dependencies(tmp_path: Path) -> None:
    stack, _provider, operation, *_rest = _board_stack(tmp_path)

    async def exercise() -> None:
        from samurai_ai.tui.screens import ItemDetailScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._show_task_board()
            await pilot.pause()
            _highlight(app.screen.query_one("#board-list", OptionList), "exploit")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ItemDetailScreen)
            headings = [str(node.render()) for node in app.screen.query(".item-heading")]
            assert "DEPENDS ON" in headings
            body = "\n".join(str(node.render()) for node in app.screen.query(".item-line"))
            assert "Recon the host" in body
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_finding_board_validates_with_evidence(tmp_path: Path) -> None:
    stack, _provider, operation, finding, _duplicate, artifact = _board_stack(tmp_path)
    stack[1].attach_evidence(finding.id, artifact.id)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+f")
            await pilot.pause()
            _highlight(app.screen.query_one("#board-list", OptionList), finding.id)
            await pilot.press("v")
            await pilot.pause()
            assert stack[1].kernel.get_finding(finding.id).status is FindingStatus.VALIDATED
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_finding_without_evidence_says_so(tmp_path: Path) -> None:
    stack, _provider, operation, finding, _duplicate, _artifact = _board_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                app._finding_validate(finding.id)
                await pilot.pause()
                assert any("no evidence" in str(message) for message in messages)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_traffic_bind_and_unbind_ask_for_a_pair(tmp_path: Path) -> None:
    from samurai_ai.tui.screens import ArgumentScreen

    stack, _provider, operation, finding, _duplicate, _artifact = _board_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            pair = app.traffic.record(operation.id, "GET", "https://a.test/x", 200)

            app._finding_traffic_bind(finding.id)
            for _ in range(20):
                await pilot.pause()
                if isinstance(app.screen, ArgumentScreen):
                    break
            assert isinstance(app.screen, ArgumentScreen)
            app.screen.dismiss(pair.id)
            for _ in range(20):
                await pilot.pause()
                if app.traffic.bindings(finding.id):
                    break
            assert [b.traffic_id for b in app.traffic.bindings(finding.id)] == [pair.id]
            assert stack[1].kernel.get_finding(finding.id).evidence_version == 1

            app._finding_traffic_unbind(finding.id)
            for _ in range(20):
                await pilot.pause()
                if isinstance(app.screen, ArgumentScreen):
                    break
            assert isinstance(app.screen, ArgumentScreen)
            app.screen.dismiss(pair.id)
            for _ in range(20):
                await pilot.pause()
                if not app.traffic.bindings(finding.id):
                    break
            assert app.traffic.bindings(finding.id) == []
            assert stack[1].kernel.get_finding(finding.id).evidence_version == 2
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_traffic_bind_from_the_board_takes_a_role(tmp_path: Path) -> None:
    from samurai_ai.tui.screens import ArgumentScreen

    stack, _provider, operation, finding, _duplicate, _artifact = _board_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            pair = app.traffic.record(operation.id, "GET", "https://a.test/x", 200)

            app._finding_traffic_bind(finding.id)
            for _ in range(20):
                await pilot.pause()
                if isinstance(app.screen, ArgumentScreen):
                    break
            app.screen.dismiss(f"{pair.id} proof")
            for _ in range(20):
                await pilot.pause()
                if app.traffic.bindings(finding.id):
                    break
            assert [b.role.value for b in app.traffic.bindings(finding.id)] == ["proof"]
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_finding_evidence_and_duplicate_prompt_for_an_id(tmp_path: Path) -> None:
    from samurai_ai.tui.screens import ArgumentScreen

    stack, _provider, operation, finding, duplicate, artifact = _board_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._finding_evidence(finding.id)
            for _ in range(20):
                await pilot.pause()
                if isinstance(app.screen, ArgumentScreen):
                    break
            assert isinstance(app.screen, ArgumentScreen)
            app.screen.dismiss(artifact.id)
            for _ in range(20):
                await pilot.pause()
                if stack[1].kernel.get_finding(finding.id).evidence_ids:
                    break
            assert stack[1].kernel.get_finding(finding.id).evidence_ids == (artifact.id,)

            app._finding_duplicate(duplicate.id)
            for _ in range(20):
                await pilot.pause()
                if isinstance(app.screen, ArgumentScreen):
                    break
            assert isinstance(app.screen, ArgumentScreen)
            app.screen.dismiss(finding.id)
            for _ in range(20):
                await pilot.pause()
                if stack[1].kernel.get_finding(duplicate.id).duplicate_of:
                    break
            assert stack[1].kernel.get_finding(duplicate.id).duplicate_of == finding.id
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_boards_report_missing_operation(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                app.action_open_task_board()
                await pilot.pause()
                assert any("No operation loaded" in str(message) for message in messages)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_boards_show_empty_states(tmp_path: Path) -> None:
    stack, _provider, _operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")
    operation = stack[0].create(
        "Empty", OperationMode.CTF_HTB, ScopeContract(includes=("b.test",))
    )

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._show_task_board()
            await pilot.pause()
            listed = app.screen.query_one("#board-list", OptionList)
            assert "no plan" in str(listed.options[0].prompt)
            await pilot.press("escape")
            await pilot.pause()
            app._show_finding_board()
            await pilot.pause()
            listed = app.screen.query_one("#board-list", OptionList)
            assert "no findings" in str(listed.options[0].prompt)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def _config(tmp_path: Path) -> Path:
    return tmp_path / "config"


def test_theme_starts_dark_and_switches(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        from samurai_ai.tui.screens import PickerScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(90, 26)) as pilot:
            await pilot.pause()
            assert app.theme == "dark"
            app._show_themes()
            await pilot.pause()
            assert isinstance(app.screen, PickerScreen)
            listed = app.screen.query_one("#picker-list", OptionList)
            labels = [str(option.prompt) for option in listed.options]
            assert any("light" in label for label in labels)
            index = next(i for i, label in enumerate(labels) if label.startswith("light"))
            listed.highlighted = index
            await pilot.press("enter")
            await pilot.pause()
            assert app.theme == "light"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_theme_choice_survives_a_restart(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(90, 26)) as pilot:
            await pilot.pause()
            app._on_theme_picked("high-contrast")
            await pilot.pause()
            assert app.theme == "high-contrast"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

        second = SamuraiApp(*stack, operation.id)
        async with second.run_test(size=(90, 26)) as pilot:
            await pilot.pause()
            assert second.theme == "high-contrast"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_theme_file_is_offered_and_broken_file_reported(tmp_path: Path) -> None:
    from samurai_ai.tui import theme as theme_module

    themes = _config(tmp_path) / "themes"
    themes.mkdir(parents=True)
    (themes / "mine.json").write_text(
        json.dumps(
            {
                "surface": "#101010",
                "panel": "#101010",
                "text": "#eeeeee",
                "text-dim": "#999999",
                "accent": "#00ffff",
                "success": "#00ff00",
                "warning": "#ffff00",
                "danger": "#ff0000",
            }
        )
    )
    (themes / "broken.json").write_text('{"nope": "#ffffff"}')
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(90, 26)) as pilot:
            await pilot.pause()
            assert "mine" in app.available_themes
            assert any("nope" in problem for problem in app._theme_problems)
            app._on_theme_picked("mine")
            await pilot.pause()
            assert app.token_color("accent") == "#00ffff"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    assert theme_module  # imported for clarity of the module under test
    asyncio.run(exercise())


def test_low_contrast_theme_warns_but_loads(tmp_path: Path) -> None:
    themes = _config(tmp_path) / "themes"
    themes.mkdir(parents=True)
    (themes / "faint.json").write_text(
        json.dumps(
            {
                "surface": "#101010",
                "panel": "#101010",
                "text": "#141414",
                "text-dim": "#999999",
                "accent": "#00ffff",
                "success": "#00ff00",
                "warning": "#ffff00",
                "danger": "#ff0000",
            }
        )
    )
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(90, 26)) as pilot:
            await pilot.pause()
            assert "faint" in app.available_themes
            assert any("text on surface" in problem for problem in app._theme_problems)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_key_override_is_loaded_and_fires(tmp_path: Path) -> None:
    from samurai_ai.tui import preferences

    preferences.keys_path().parent.mkdir(parents=True, exist_ok=True)
    preferences.keys_path().write_text(json.dumps({"toggle_journal": "ctrl+j"}))
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(90, 26)) as pilot:
            await pilot.pause()

            views = app.query_one("#views", ContentSwitcher)
            assert views.display is True
            await pilot.press("ctrl+j")
            await pilot.pause()
            assert views.display is False
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_conflicting_key_override_is_refused(tmp_path: Path) -> None:
    from samurai_ai.tui import preferences

    preferences.keys_path().parent.mkdir(parents=True, exist_ok=True)
    preferences.keys_path().write_text(json.dumps({"steer": "ctrl+t"}))
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(90, 26)) as pilot:
            await pilot.pause()
            assert app.keymap["steer"] == "ctrl+s"
            assert any("open_task_board" in problem for problem in app._key_problems)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_keys_screen_refuses_a_taken_key(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        from samurai_ai.tui.screens import KeyCaptureScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(90, 26)) as pilot:
            await pilot.pause()
            app._show_keys()
            await pilot.pause()
            listed = app.screen.query_one("#picker-list", OptionList)
            listed.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, KeyCaptureScreen)
            await pilot.press("ctrl+t")
            await pilot.pause()
            assert app.keymap["toggle_journal"] == "ctrl+o"
            journal = app.query_one("#journal", RichLog)
            assert any("taken" in str(line.text) for line in journal.lines)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_keys_screen_binds_a_free_key_and_saves(tmp_path: Path) -> None:
    from samurai_ai.tui import preferences

    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(90, 26)) as pilot:
            await pilot.pause()
            app._show_keys()
            await pilot.pause()
            listed = app.screen.query_one("#picker-list", OptionList)
            listed.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("ctrl+j")
            await pilot.pause()
            assert app.keymap["toggle_journal"] == "ctrl+j"
            saved = json.loads(preferences.keys_path().read_text())
            assert saved["toggle_journal"] == "ctrl+j"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_help_sheet_shows_the_current_board_keys(tmp_path: Path) -> None:
    from samurai_ai.tui import preferences

    preferences.keys_path().parent.mkdir(parents=True, exist_ok=True)
    preferences.keys_path().write_text(json.dumps({"open_task_board": "ctrl+y"}))
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(90, 26)) as pilot:
            await pilot.pause()
            await pilot.press("f1")
            await pilot.pause()
            lines = [str(node.render()) for node in app.screen.query(".help-line")]
            assert any("Ctrl+Y" in line for line in lines)
            assert not any("Ctrl+T" in line for line in lines)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_thinking_indicator_turns_without_words(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(90, 26)) as pilot:
            await pilot.pause()
            app._thinking_start()
            seen = []
            for _ in range(6):
                await asyncio.sleep(0.14)
                seen.append(app.query_one("#thinking").render().plain)
            app._thinking_stop()
            assert all(line in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" for line in seen)
            assert len(set(seen)) > 1
            assert not app.query_one("#thinking").display
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_greeting_is_the_name_and_version_only(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(90, 26)) as pilot:
            await pilot.pause()
            lines = [line.text for line in app.query_one("#journal", RichLog).lines]
            assert any("Samurai AI" in line for line in lines)
            assert not any("one shot" in line for line in lines)
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_chat_tab_returns_to_the_journal(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            switcher = app.query_one("#views", ContentSwitcher)
            await pilot.click("#tab-ops")
            await pilot.pause()
            assert switcher.current == "view-ops"
            chat = app.query_one("#tab-chat", Tab)
            assert str(chat.label) == operation.name  # the tab is the operation's chat
            await pilot.click("#tab-chat")
            await pilot.pause()
            assert switcher.current == "journal"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_chat_tab_is_a_plain_journal_without_an_operation(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert str(app.query_one("#tab-chat", Tab).label) == "Journal"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_creating_an_operation_activates_its_tab(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.click("#tab-bounty")
            await pilot.pause()
            command_input = app.query_one("#command-input", Input)
            with patch.object(app, "_show_new_operation", lambda: app._on_new_operation(
                ("gate lab", "bug-bounty", "")
            )):
                command_input.value = "/new"
                await pilot.press("enter")
                await pilot.pause()
            tabs = app.query_one("#view-tabs", Tabs)
            assert tabs.active == "tab-chat"
            assert str(app.query_one("#tab-chat", Tab).label) == "gate lab"
            assert app.query_one("#views", ContentSwitcher).current == "journal"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_clicking_a_tab_switches_the_view(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            switcher = app.query_one("#views", ContentSwitcher)
            assert switcher.current == "journal"
            await pilot.click("#tab-bounty")
            await pilot.pause()
            assert switcher.current == "view-bounty"
            await pilot.click("#tab-ops")
            await pilot.pause()
            assert switcher.current == "view-ops"
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_no_legacy_hint_even_with_an_old_state_directory(tmp_path: Path, monkeypatch) -> None:
    workdir = tmp_path / "work"
    (workdir / ".samurai").mkdir(parents=True)
    (workdir / ".samurai" / "samurai.db").write_text("")
    monkeypatch.chdir(workdir)
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(110, 26)) as pilot:
            await pilot.pause()
            text = " ".join(line.text for line in app.query_one("#journal", RichLog).lines)
            assert ".samurai" not in text
            assert "--db" not in text
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


class _ToolCallChatProvider(_ChatProvider):
    """First answer asks for a shell command, later answers are plain text."""

    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command

    async def complete(self, request):  # type: ignore[no-untyped-def]
        from samurai_ai.agents.provider import ChatResponse, ToolCall, Usage

        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                text="",
                tool_calls=(
                    ToolCall(
                        id="call_1", name="shell_exec", arguments={"command": self.command}
                    ),
                ),
                usage=Usage(prompt_tokens=3, completion_tokens=2),
                model_id=request.model_id,
                provider_id="gpt",
                latency_ms=5,
            )
        return ChatResponse(
            text="done",
            usage=Usage(prompt_tokens=3, completion_tokens=2),
            model_id=request.model_id,
            provider_id="gpt",
            latency_ms=5,
        )


def test_a_deny_rule_blocks_a_chat_tool_call(tmp_path: Path) -> None:
    stack, provider, operation = _chat_stack(tmp_path, _ToolCallChatProvider("rm temp"))
    stack[2].set_locale("en")
    stack[2].add_permission_rule("deny shell.exec: *rm*")

    async def exercise() -> None:
        from samurai_ai.tui.app import AccessPromptScreen

        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            command_input = app.query_one("#command-input", Input)
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "rm temp"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                # no prompt at all; the refusal goes back to the model instead
                assert not isinstance(app.screen, AccessPromptScreen)
                assert any("denied" in m.plain for m in messages if isinstance(m, Text))
                assert provider.calls == 2
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_the_rules_command_lists_and_edits(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            command_input = app.query_one("#command-input", Input)
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "/rules"
                await pilot.press("enter")
                await pilot.pause()
                assert any("no rules" in _message_text(m) for m in messages)

                command_input.value = "/rules add allow shell.exec: git *"
                await pilot.press("enter")
                await pilot.pause()
                assert [r.format() for r in stack[2].permission_rules()] == [
                    "allow shell.exec: git *"
                ]

                command_input.value = "/rules"
                await pilot.press("enter")
                await pilot.pause()
                assert any("git *" in _message_text(m) for m in messages)

                command_input.value = "/rules rm allow shell.exec: git *"
                await pilot.press("enter")
                await pilot.pause()
                assert stack[2].permission_rules() == ()
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


class _TwoToolRoundsProvider:
    """Round 1: two shell calls. Round 2: the conclusion. Round 3: extra work."""

    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[object] = []

    async def health(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["test-model"]

    async def complete(self, request):  # type: ignore[no-untyped-def]
        from samurai_ai.agents.provider import ChatResponse, ToolCall, Usage

        self.calls += 1
        self.requests.append(request)
        usage = Usage(prompt_tokens=3, completion_tokens=2)
        if self.calls == 1:
            return ChatResponse(
                text="проверяю обе гипотезы",
                tool_calls=(
                    ToolCall(
                        id="c1", name="shell_exec", arguments={"command": "echo first-result"}
                    ),
                    ToolCall(
                        id="c2", name="shell_exec", arguments={"command": "echo second-result"}
                    ),
                ),
                usage=usage, model_id=request.model_id, provider_id="gpt", latency_ms=5,
            )
        if self.calls == 2:
            return ChatResponse(
                text="обе гипотезы проверены: вход есть, второй хуже",
                usage=usage, model_id=request.model_id, provider_id="gpt", latency_ms=5,
            )
        return ChatResponse(
            text="полез дальше",
            usage=usage, model_id=request.model_id, provider_id="gpt", latency_ms=5,
        )


def _chat_ready_stack(tmp_path: Path, provider: object):
    stack = _minimal_stack(tmp_path, provider_factory=lambda _p: provider)
    stack[2].set_locale("en")
    stack[2].set_value("tui.yolo", "1")
    stack[5].add("gpt-key", "x")
    stack[-1].register_endpoint("gpt", "http://127.0.0.1:9/v1", "gpt-key")
    stack[-1].add_model("gpt/test-model")
    stack[2].set_value("model.default", "gpt/test-model@v1")
    operation = stack[0].create(
        "Stop lab", OperationMode.BUG_BOUNTY, ScopeContract(includes=("*.example.com",))
    )
    return stack, operation


def test_ctrl_c_stops_the_run_and_a_second_press_leaves(tmp_path: Path) -> None:
    """A stop finishes the round; an idle press only arms the exit."""
    provider = _TwoToolRoundsProvider()
    stack, operation = _chat_ready_stack(tmp_path, provider)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                app.query_one("#command-input", Input).value = "проверь вход"
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("ctrl+c")
                await pilot.pause()
                # the round's own calls still run: results must not be lost
                await app.workers.wait_for_complete()
                await pilot.pause()
                text = "\n".join(_message_text(m) for m in messages)
                assert "first-result" in text and "second-result" in text
                # and the model was told to conclude, which it did
                assert "обе гипотезы проверены" in text
                # the turn did not go on exploring after the conclusion
                assert provider.calls == 2
                assert "stopped" in text
                assert app.is_running
                await pilot.press("ctrl+c")
                await pilot.pause()
                assert not app.is_running

    asyncio.run(exercise())


def test_the_stop_note_reaches_the_model_once(tmp_path: Path) -> None:
    provider = _TwoToolRoundsProvider()
    stack, operation = _chat_ready_stack(tmp_path, provider)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.query_one("#command-input", Input).value = "проверь вход"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("ctrl+c")
            await app.workers.wait_for_complete()
            await pilot.pause()
            second = provider.requests[1]
            history = [getattr(m, "content", "") for m in getattr(second, "messages", [])]
            notes = [entry for entry in history if "The operator stopped the run" in entry]
            assert len(notes) == 1
            assert not app._stop_requested

    asyncio.run(exercise())


def test_ctrl_c_when_idle_only_arms_the_exit(tmp_path: Path) -> None:
    provider = _TwoToolRoundsProvider()
    stack, operation = _chat_ready_stack(tmp_path, provider)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                await pilot.press("ctrl+c")
                await pilot.pause()
                assert app.is_running
                assert "press Ctrl+C again to exit" in "\n".join(
                    _message_text(m) for m in messages
                )
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert not app.is_running

    asyncio.run(exercise())


def test_record_fact_on_an_unknown_intent_opens_it(tmp_path: Path) -> None:
    """Models hand back titles as if they were ids; that must not lose the turn."""
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            message = await app._run_chat_tool(
                ToolCall(
                    id="1",
                    name="record_fact",
                    arguments={"intent": "check /api/v1", "summary": "12 endpoints"},
                )
            )
            assert "recorded for" in message.content
            assert any(
                node.text() == "check /api/v1" for node in app.graph.for_operation(operation.id)
            )
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_a_tool_handler_error_becomes_a_result_not_a_crash(tmp_path: Path) -> None:
    stack, _provider, operation = _sequence_stack(tmp_path, [])
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ), patch.object(app, "_run_close_intent_tool", side_effect=RuntimeError("boom")):
                message = await app._run_chat_tool(
                    ToolCall(id="1", name="close_intent", arguments={"intent": "x"})
                )
            assert "error: boom" in message.content
            assert any("boom" in _message_text(m) for m in messages)

    asyncio.run(exercise())


def test_grey_process_lines_fold_into_one_line(tmp_path: Path) -> None:
    """Progress chatter buries the answers; it belongs behind one arrow."""
    provider = _TwoToolRoundsProvider()
    stack, operation = _chat_ready_stack(tmp_path, provider)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                app.query_one("#command-input", Input).value = "проверь вход"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                text = "\n".join(_message_text(m) for m in messages)
                # the grey lines of the turn are behind one collapsed entry
                assert "folded" in text
                assert app._collapsed, "the folded process log must stay openable"
                # the tool summaries and the answer stay visible
                assert "⚙ echo first-result" in text
                assert "обе гипотезы проверены" in text

    asyncio.run(exercise())


def test_enter_on_an_empty_line_opens_the_folded_block(tmp_path: Path) -> None:
    provider = _TwoToolRoundsProvider()
    stack, operation = _chat_ready_stack(tmp_path, provider)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                app.query_one("#command-input", Input).value = "проверь вход"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert app._collapsed
                await pilot.press("enter")
                await pilot.pause()
                text = "\n".join(_message_text(m) for m in messages)
                assert "▾ ⚙" in text

    asyncio.run(exercise())


def test_a_click_on_the_journal_opens_the_folded_block(tmp_path: Path) -> None:
    provider = _TwoToolRoundsProvider()
    stack, operation = _chat_ready_stack(tmp_path, provider)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.query_one("#command-input", Input).value = "проверь вход"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            before = len(app._collapsed)
            assert before
            await pilot.click("#journal")
            await pilot.pause()
            # one click opens the oldest folded block, the rest stay folded
            assert len(app._collapsed) == before - 1

    asyncio.run(exercise())


def test_a_failed_turn_shows_its_process_log_without_a_press(tmp_path: Path) -> None:
    """On failure the folded detail is exactly what the operator needs."""
    provider = _TwoToolRoundsProvider()
    stack, operation = _chat_ready_stack(tmp_path, provider)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            messages: list[str] = []
            original = app._request_chat_completion
            calls = {"n": 0}

            async def failing(*call_args):  # type: ignore[no-untyped-def]
                calls["n"] += 1
                if calls["n"] > 1:
                    raise RuntimeError("boom")
                return await original(*call_args)

            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ), patch.object(app, "_request_chat_completion", side_effect=failing):
                app.query_one("#command-input", Input).value = "проверь вход"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
            text = "\n".join(_message_text(m) for m in messages)
            assert "▾ ⚙" in text and "boom" in text

    asyncio.run(exercise())


def _select_journal(pilot, journal) -> None:  # type: ignore[no-untyped-def]
    """Drag over the journal the way an operator copies a line."""
    pilot.app.screen.clear_selection() if hasattr(pilot.app.screen, "clear_selection") else None
    await_down = pilot.mouse_down(offset=(2, 1), widget=journal)
    return await_down


def test_ctrl_c_copies_the_selection_instead_of_quitting(tmp_path: Path) -> None:
    """Copying is what a selected line is for; quitting is the rare case."""
    provider = _TwoToolRoundsProvider()
    stack, operation = _chat_ready_stack(tmp_path, provider)
    copied: list[str] = []

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            app.query_one("#command-input", Input).value = "проверь вход"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            with patch.object(app, "copy_to_clipboard", side_effect=copied.append):
                await pilot.mouse_down(offset=(2, 1), widget=journal)
                await pilot.hover(journal, offset=(30, 4))
                await pilot.mouse_up(offset=(30, 4), widget=journal)
                await pilot.pause()
                assert app._selected_text()
                await pilot.press("ctrl+c")
                await pilot.pause()
            assert copied, "the selection must reach the clipboard"
            assert copied[0].strip()
            # the app did not leave, and no stop was armed by the copy
            assert app.is_running
            assert not app._stop_requested

    asyncio.run(exercise())


def test_ctrl_shift_c_copies_and_says_so(tmp_path: Path) -> None:
    provider = _TwoToolRoundsProvider()
    stack, operation = _chat_ready_stack(tmp_path, provider)
    copied: list[str] = []

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            app.query_one("#command-input", Input).value = "проверь вход"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.mouse_down(offset=(2, 1), widget=journal)
            await pilot.hover(journal, offset=(30, 4))
            await pilot.mouse_up(offset=(30, 4), widget=journal)
            await pilot.pause()
            with patch.object(app, "copy_to_clipboard", side_effect=copied.append):
                app.action_copy_selection()
                await pilot.pause()
            assert copied and "copied" in "\n".join(
                _message_text(m) for m in app.query_one("#journal", RichLog).lines
            )

    asyncio.run(exercise())


def test_without_a_selection_ctrl_c_still_stops_and_quits(tmp_path: Path) -> None:
    provider = _TwoToolRoundsProvider()
    stack, operation = _chat_ready_stack(tmp_path, provider)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert app._selected_text() is None
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.is_running
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert not app.is_running

    asyncio.run(exercise())


def test_the_attach_tail_is_selectable_too(tmp_path: Path) -> None:
    """Every surface that holds text allows the drag, not just the journal."""
    from samurai_ai.i18n import Translator
    from samurai_ai.tui.screens.attach import AttachScreen
    from samurai_ai.tui.widgets import SelectableRichLog

    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.push_screen(AttachScreen(Translator("en"), tmp_path / "s.sock", "op_1"))
            await pilot.pause()
            log = app.screen.query_one("#attach-log")
            assert isinstance(log, SelectableRichLog)
            assert log.allow_select
            await pilot.press("escape")

    asyncio.run(exercise())


def test_the_progress_tab_shows_a_percentage_and_a_top_finding(tmp_path: Path) -> None:
    """The operator asked for a progress read, so it is one glance and no journal."""
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")
    operation = stack[0].create(
        "Hunt", OperationMode.BUG_BOUNTY, ScopeContract(includes=("*.example.com",))
    )
    stack[1].create_plan(
        operation.id,
        "Hunt plan",
        [
            PlanTask(id="t-1", title="Brute force SSH", risk_class=RiskClass.R1),
            PlanTask(id="t-2", title="Brute force the panel", risk_class=RiskClass.R1),
            PlanTask(id="t-3", title="Recon subdomains", risk_class=RiskClass.R1),
        ],
        [],
    )
    stack[1].start_task(operation.id, "t-1")
    stack[1].complete_task(operation.id, "t-1")
    finding = stack[1].create_finding(operation.id, "IDOR on /user", FindingSeverity.HIGH)

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert app.query_one("#tab-hunt").display
            app._switch_view("view-hunt")
            await pilot.pause()
            lines = "\n".join(strip.text for strip in app.query_one("#view-hunt").lines)
            assert "50%" in lines and "1/2" in lines
            assert "brute force" in lines
            # the recon task is not a hunt and stays out of the counters
            assert "Recon subdomains" not in lines
            assert finding.title in lines
            await pilot.press("escape")

    asyncio.run(exercise())


def test_a_new_finding_reaches_the_tab_without_a_refresh_beat(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")
    operation = stack[0].create(
        "Hunt", OperationMode.BUG_BOUNTY, ScopeContract(includes=("*.example.com",))
    )
    stack[1].create_plan(
        operation.id,
        "Hunt plan",
        [PlanTask(id="t-1", title="Password spray OWA", risk_class=RiskClass.R1)],
        [],
    )

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._switch_view("view-hunt")
            await pilot.pause()
            stack[1].create_finding(
                operation.id, "Valid creds for svc_backup", FindingSeverity.CRITICAL
            )
            app.refresh_hunt()
            await pilot.pause()
            lines = "\n".join(strip.text for strip in app.query_one("#view-hunt").lines)
            assert "Valid creds for svc_backup" in lines
            await pilot.press("escape")

    asyncio.run(exercise())


def test_the_tab_hides_when_the_plan_has_no_searches(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")
    operation = stack[0].create(
        "Recon only", OperationMode.BUG_BOUNTY, ScopeContract(includes=("*.example.com",))
    )
    stack[1].create_plan(
        operation.id,
        "Plan",
        [PlanTask(id="t-1", title="Enumerate subdomains", risk_class=RiskClass.R1)],
        [],
    )

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert not app.query_one("#tab-hunt").display
            await pilot.press("escape")

    asyncio.run(exercise())
