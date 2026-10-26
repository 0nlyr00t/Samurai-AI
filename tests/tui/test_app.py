import asyncio
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Button, Input, OptionList, RichLog, Static

from samurai_ai.agents.registry import ModelRegistry
from samurai_ai.domain.agents import AgentRun, AgentRunStatus
from samurai_ai.domain.ctf import FlagRecord, Hypothesis
from samurai_ai.domain.kernel import ArtifactKind, PlanTask, RiskClass, StageGate
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
            header = app.query_one("#operation-header", Static)
            assert operation.id in str(header.render())

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

            assert journal.display
            await pilot.press("ctrl+o")
            assert not journal.display
            await pilot.press("ctrl+o")
            assert journal.display
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def _minimal_stack(tmp_path: Path, provider_factory=None):  # type: ignore[no-untyped-def]
    database = Database(tmp_path / "tui2.db")
    operation_repository = OperationRepository(database)
    service = OperationService(operation_repository)
    kernel = KernelService(service, KernelRepository(database))
    recovery_repository = RecoveryRepository(database)
    settings = SettingsService(recovery_repository)
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
    stack[2].set_locale("en")

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
            # pick the second model and high reasoning, then apply
            await pilot.press("down")
            await pilot.press("tab")
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert stack[2].get_value("model.default") is not None
            assert stack[2].get_value("model.reasoning_effort") == "high"
            status_bar = app.query_one("#status-bar", Static)
            assert "model=" in str(status_bar.render())
            await pilot.press("ctrl+c")

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
