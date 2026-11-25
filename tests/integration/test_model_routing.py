from pathlib import Path

import pytest

from samurai_ai.agents.registry import ModelRegistry
from samurai_ai.domain.models import ModelNotFoundError
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.model_routing import ModelRoutingService
from samurai_ai.services.models import ModelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.services.settings import SettingsService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.database import Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.models import CustomModelRepository, ModelEndpointRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.recovery import RecoveryRepository
from samurai_ai.storage.vault import VaultRepository


def _stack(path: Path):
    database = Database(path)
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    recovery = RecoveryRepository(database)
    RecoveryService(operations, kernel, recovery)
    vault = VaultService(
        VaultRepository(database), operations.repository, path.parent / "vault.key"
    )
    endpoints = ModelEndpointRepository(database)
    custom_models = CustomModelRepository(database)
    models = ModelService(
        ModelRegistry(endpoints, custom_models=custom_models),
        vault,
        kernel,
        operations,
        endpoints,
        recovery,
        custom_models=custom_models,
    )
    models.register_endpoint("gpt", "http://127.0.0.1:9/v1", None)
    models.add_model("gpt/main")
    models.add_model("gpt/fast")
    settings = SettingsService(recovery)
    settings.set_value("model.default", "gpt/main@v1")
    operation = operations.create(
        "Routing lab", OperationMode.CTF_HTB, ScopeContract(includes=("127.0.0.1",))
    )
    return operations, models, settings, operation.id


def test_handoff_is_one_shot_and_survives_replay(tmp_path: Path) -> None:
    operations, models, settings, operation_id = _stack(tmp_path / "routing.db")
    routing = ModelRoutingService(settings, operations, models)
    routing.set_execution_model("gpt/fast@v1")

    assert routing.handoff(operation_id, "gpt/main@v1") == "gpt/fast@v1"
    assert routing.handoff(operation_id, "gpt/main@v1") is None
    assert routing.active_model(operation_id) == "gpt/fast@v1"
    assert ModelRoutingService(settings, operations, models).active_model(
        operation_id
    ) == "gpt/fast@v1"
    routing.clear_execution_model()
    assert routing.active_model(operation_id) == "gpt/main@v1"
    routing.set_execution_model("gpt/fast@v1")

    routing.reset(operation_id, reason="operator_restart")
    assert routing.active_model(operation_id) == "gpt/main@v1"
    assert routing.handoff(operation_id, "gpt/main@v1") == "gpt/fast@v1"
    events = [
        event
        for event in operations.events(operation_id)
        if event.event_type.startswith("model.")
    ]
    assert [event.event_type for event in events] == [
        "model.handoff",
        "model.route.reset",
        "model.handoff",
    ]
    assert all("checklist" not in event.payload for event in events)


def test_routing_validates_configuration_and_avoids_noop(tmp_path: Path) -> None:
    operations, models, settings, operation_id = _stack(tmp_path / "validation.db")
    routing = ModelRoutingService(settings, operations, models)

    assert routing.handoff(operation_id, "gpt/main@v1") is None
    with pytest.raises(ModelNotFoundError):
        routing.set_execution_model("missing/fast@v1")
    routing.set_execution_model("gpt/main@v1")
    assert routing.handoff(operation_id, "gpt/main@v1") is None
    assert routing.active_model(operation_id) == "gpt/main@v1"
