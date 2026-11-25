from pathlib import Path

from test_model_routing import _stack

from samurai_ai.domain.reviewer import ReviewerConfig
from samurai_ai.services.reviewer import ReviewerService


def test_reviewer_configuration_replays_and_disables(tmp_path: Path) -> None:
    operations, models, _settings, operation_id = _stack(tmp_path / "reviewer.db")
    service = ReviewerService(operations, models)
    assert service.config(operation_id) is None

    enabled = service.enable(
        operation_id, ReviewerConfig("gpt/main@v1", "gpt/fast@v1")
    )
    assert enabled.fallback == "gpt/fast@v1"
    assert ReviewerService(operations, models).config(operation_id) == enabled
    assert service.disable(operation_id)
    assert service.config(operation_id) is None
    assert not service.disable(operation_id)
