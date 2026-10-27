import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from rich.text import Text
from textual.widgets import Button, Input, OptionList, RichLog, Static

from samurai_ai.agents.registry import ModelRegistry
from samurai_ai.domain.agents import AgentRun, AgentRunStatus
from samurai_ai.domain.ctf import FlagRecord, Hypothesis
from samurai_ai.domain.kernel import ArtifactKind, PlanTask, RiskClass, StageGate, TaskStatus
from samurai_ai.domain.memory import MemoryKind
from samurai_ai.domain.operations import OperationEvent, OperationMode
from samurai_ai.domain.planning import KillChainPhase, PlannedTest, Runbook
from samurai_ai.domain.playbooks import AssetKind, AssetLedgerEntry, PlaybookTemplate
from samurai_ai.domain.redteam import AttackStep, Debrief
from samurai_ai.domain.reports import BountyReport, ReportFormat
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.swarm import AgentSpecialization, SwarmRun
from samurai_ai.services.ctf import CtfService
from samurai_ai.services.execution import ToolExecutionService
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
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.memory import MemoryRepository
from samurai_ai.storage.models import CustomModelRepository, ModelEndpointRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.planning import PlanningRepository
from samurai_ai.storage.playbooks import PlaybookRepository
from samurai_ai.storage.recovery import RecoveryRepository
from samurai_ai.storage.redteam import RedTeamRepository
from samurai_ai.storage.reports import ReportRepository
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
    skills = SkillService(SkillRepository(database))
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
            playbooks, reports, ctf, redteam, planning, swarms, memory, skills,
            models, operation.id,
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
                    ("/tasks", "recon"),
                    ("/gates", "recon-complete"),
                    ("/findings", finding.id),
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

                assert not any("tui-secret-plaintext" in message for message in messages)

            from textual.widgets import ContentSwitcher

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
        playbooks,
        ReportService(service, kernel, playbooks, ReportRepository(database)),
        CtfService(service, kernel, CtfRepository(database)),
        RedTeamService(service, kernel, RedTeamRepository(database)),
        PlanningService(service, kernel, PlanningRepository(database)),
        SwarmRunRepository(database),
        CampaignMemoryService(MemoryRepository(database)),
        SkillService(SkillRepository(database)),
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
            brand = str(app.query_one("#brand", Static).render())
            assert "Samurai AI" in brand
            assert __version__ in brand
            status_bar = str(app.query_one("#status-bar", Static).render())
            assert "model=fake/fake-model@v1" in status_bar
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_yolo_toggles_access_mode(tmp_path: Path) -> None:
    stack = _minimal_stack(tmp_path)
    settings = stack[2]
    settings.set_locale("en")

    def dot_style(status_bar: Static) -> str:
        content = status_bar.render()
        for span in getattr(content, "spans", []):
            if content.plain[span.start:span.end] == "●":
                return str(span.style)
        return ""

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            status_bar = app.query_one("#status-bar", Static)
            assert "●" in str(status_bar.render())
            assert "81,207,102" in dot_style(status_bar)  # green: ask
            command_input = app.query_one("#command-input", Input)
            command_input.value = "/yolo"
            await pilot.press("enter")
            await pilot.pause()
            assert settings.get_value("tui.yolo") == "1"
            assert "255,107,107" in dot_style(status_bar)  # red: yolo
            command_input.value = "/yolo"
            await pilot.press("enter")
            await pilot.pause()
            assert settings.get_value("tui.yolo") == "0"
            assert "81,207,102" in dot_style(status_bar)
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
            from textual.widgets import ContentSwitcher, Tabs

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


def _chat_stack(tmp_path: Path):  # type: ignore[no-untyped-def]
    provider = _ChatProvider()
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
                assert "hello-from-tool" in text
                assert "ran it" in text
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
            assert "Fresh recon op" in str(app.query_one("#opname", Static).render())
            from textual.widgets import ContentSwitcher

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
            # mouse capture is off by default: native terminal selection
            command_input.value = "/mouse"
            await pilot.press("enter")
            await pilot.pause()
            assert settings.get_value("tui.mouse") == "1"
            command_input.value = "/mouse"
            await pilot.press("enter")
            await pilot.pause()
            assert settings.get_value("tui.mouse") == "0"
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
