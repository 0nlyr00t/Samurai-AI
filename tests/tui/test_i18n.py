import asyncio
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Input, OptionList, RichLog

from samurai_ai.agents.registry import ModelRegistry
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
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
from samurai_ai.storage.models import ModelEndpointRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.planning import PlanningRepository
from samurai_ai.storage.playbooks import PlaybookRepository
from samurai_ai.storage.recovery import RecoveryRepository
from samurai_ai.storage.redteam import RedTeamRepository
from samurai_ai.storage.reports import ReportRepository
from samurai_ai.storage.skills import SkillRepository
from samurai_ai.storage.swarm import SwarmRunRepository
from samurai_ai.storage.vault import VaultRepository
from samurai_ai.tui.app import LanguagePromptScreen, SamuraiApp


def make_stack(
    tmp_path: Path, name: str = "tui-i18n.db"
) -> tuple[
    OperationService,
    KernelService,
    SettingsService,
    RecoveryService,
    ToolExecutionService,
    VaultService,
    AgentRunRepository,
    PlaybookService,
    ReportService,
    CtfService,
    RedTeamService,
    PlanningService,
    SwarmRunRepository,
    CampaignMemoryService,
    SkillService,
    ModelService,
]:
    database = Database(tmp_path / name)
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
    agents = AgentRunRepository(database)
    playbooks = PlaybookService(service, kernel, PlaybookRepository(database))
    reports = ReportService(service, kernel, playbooks, ReportRepository(database))
    ctf = CtfService(service, kernel, CtfRepository(database))
    redteam = RedTeamService(service, kernel, RedTeamRepository(database))
    planning = PlanningService(service, kernel, PlanningRepository(database))
    swarms = SwarmRunRepository(database)
    memory = CampaignMemoryService(MemoryRepository(database))
    skills = SkillService(SkillRepository(database))
    models = ModelService(
        ModelRegistry(ModelEndpointRepository(database)),
        vault,
        kernel,
        service,
        ModelEndpointRepository(database),
        recovery_repository,
    )
    return (
        service,
        kernel,
        settings,
        recovery,
        execution,
        vault,
        agents,
        playbooks,
        reports,
        ctf,
        redteam,
        planning,
        swarms,
        memory,
        skills,
        models,
    )


def test_first_run_language_choice_persists_ru(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SAMURAI_LANG", raising=False)
    stack = make_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, LanguagePromptScreen)

            # arrows + enter: move to Русский and select it
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert not isinstance(app.screen, LanguagePromptScreen)
            assert stack[2].get_locale() == "ru"

            journal = app.query_one("#journal", RichLog)
            command_input = app.query_one("#command-input", Input)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "/help"
                await pilot.press("enter")
                await pilot.pause()
                assert any("Показать доступные команды" in message for message in messages)

                command_input.value = "/status"
                await pilot.press("enter")
                await pilot.pause()
                assert any("Операция не загружена" in message for message in messages)
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_first_run_language_choice_english_via_keyboard(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SAMURAI_LANG", raising=False)
    stack = make_stack(tmp_path)

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, LanguagePromptScreen)
            focused = app.screen.focused
            assert isinstance(focused, OptionList)
            assert focused.id == "language-options"
            await pilot.press("enter")
            await pilot.pause()
            assert not isinstance(app.screen, LanguagePromptScreen)
            assert stack[2].get_locale() == "en"
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_persisted_locale_ru_skips_chooser(tmp_path: Path) -> None:
    stack = make_stack(tmp_path)
    stack[2].set_locale("ru")

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert not isinstance(app.screen, LanguagePromptScreen)

            journal = app.query_one("#journal", RichLog)
            command_input = app.query_one("#command-input", Input)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "/help"
                await pilot.press("enter")
                await pilot.pause()
                assert any("Показать доступные команды" in message for message in messages)

                command_input.value = "/status"
                await pilot.press("enter")
                await pilot.pause()
                assert any("Операция не загружена" in message for message in messages)
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_lang_command_switches_language_without_losing_operation(tmp_path: Path) -> None:
    stack = make_stack(tmp_path)
    stack[2].set_locale("en")
    operation = stack[0].create(
        "Lang lab",
        OperationMode.CTF_HTB,
        ScopeContract(includes=("*.example.com",)),
    )

    async def exercise() -> None:
        app = SamuraiApp(*stack, operation.id)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            journal = app.query_one("#journal", RichLog)
            command_input = app.query_one("#command-input", Input)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "/lang"
                await pilot.press("enter")
                await pilot.pause()
                assert any("Current language: en" in message for message in messages)

                command_input.value = "/lang ru"
                await pilot.press("enter")
                await pilot.pause()
                assert stack[2].get_locale() == "ru"
                assert any("Язык изменён на ru" in message for message in messages)

                command_input.value = "/help"
                await pilot.press("enter")
                await pilot.pause()
                assert any("Показать доступные команды" in message for message in messages)

                command_input.value = "/status"
                await pilot.press("enter")
                await pilot.pause()
                assert any(operation.id in message for message in messages)

                command_input.value = "/lang de"
                await pilot.press("enter")
                await pilot.pause()
                assert any("Неподдерживаемый язык" in message for message in messages)
                assert stack[2].get_locale() == "ru"
            await pilot.press("ctrl+c")

    asyncio.run(exercise())
