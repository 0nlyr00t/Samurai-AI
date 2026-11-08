"""Modal screens, one module per dialog."""

from samurai_ai.tui.screens.access import AccessPromptScreen
from samurai_ai.tui.screens.argument import ArgumentScreen
from samurai_ai.tui.screens.board import BoardScreen, ItemDetailScreen
from samurai_ai.tui.screens.custom_endpoint import CustomEndpointScreen
from samurai_ai.tui.screens.help import HelpScreen
from samurai_ai.tui.screens.inspector import InspectorScreen, Section
from samurai_ai.tui.screens.key_capture import KeyCaptureScreen
from samurai_ai.tui.screens.language import LanguagePromptScreen
from samurai_ai.tui.screens.model_picker import (
    ADD_CUSTOM_ID,
    REASONING_LEVELS,
    ModelPickerScreen,
)
from samurai_ai.tui.screens.new_operation import NewOperationScreen
from samurai_ai.tui.screens.picker import PickerScreen
from samurai_ai.tui.screens.project import ProjectPickerScreen
from samurai_ai.tui.screens.sessions import SessionDetailScreen, SessionsScreen
from samurai_ai.tui.screens.settings import SettingsScreen
from samurai_ai.tui.screens.trust import TrustDirectoryScreen

__all__ = [
    "BoardScreen",
    "PickerScreen",
    "ItemDetailScreen",
    "SessionDetailScreen",
    "SessionsScreen",
    "ADD_CUSTOM_ID",
    "REASONING_LEVELS",
    "AccessPromptScreen",
    "ArgumentScreen",
    "CustomEndpointScreen",
    "HelpScreen",
    "InspectorScreen",
    "Section",
    "KeyCaptureScreen",
    "LanguagePromptScreen",
    "ModelPickerScreen",
    "NewOperationScreen",
    "ProjectPickerScreen",
    "SettingsScreen",
    "TrustDirectoryScreen",
]
