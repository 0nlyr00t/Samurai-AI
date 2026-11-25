import pytest

from samurai_ai.domain.experiments import (
    ExperimentConfig,
    ExperimentError,
    MetricDirection,
    improved,
    parse_metric,
)


def test_metric_parser_uses_the_last_finite_or_invalid_value() -> None:
    output = "METRIC score=5\nother\nMETRIC score=3.5\n"
    assert parse_metric(output, "score") == 3.5
    assert parse_metric(output + "METRIC score=nan\n", "score") is None
    assert parse_metric(output + "METRIC score=bad\n", "score") is None
    assert parse_metric(output, "other") is None


def test_direction_requires_strict_improvement() -> None:
    assert improved(3, 5, MetricDirection.LOWER)
    assert not improved(5, 5, MetricDirection.LOWER)
    assert improved(7, 5, MetricDirection.HIGHER)
    assert not improved(4, 5, MetricDirection.HIGHER)
    assert improved(1, None, MetricDirection.LOWER)


def test_experiment_limit_is_bounded() -> None:
    with pytest.raises(ExperimentError):
        ExperimentConfig("exp", "score", MetricDirection.LOWER, 0, "secret")
    with pytest.raises(ExperimentError):
        ExperimentConfig("exp", "score", MetricDirection.LOWER, 21, "secret")
