import pytest
from pydantic import ValidationError

from samurai_ai.domain.kernel import OperationPlan
from samurai_ai.domain.playbooks import (
    AssetKind,
    AssetValidationError,
    BountyProgramRules,
    DuplicateCandidateError,
    bug_bounty_template,
    finding_fingerprint,
    normalize_asset_value,
    scope_target_for_asset,
)


def test_normalize_domain_lowercases_and_strips_trailing_dot() -> None:
    assert normalize_asset_value(AssetKind.DOMAIN, "Example.COM.") == "example.com"


def test_normalize_domain_idna() -> None:
    assert normalize_asset_value(AssetKind.DOMAIN, "bücher.de") == "xn--bcher-kva.de"


def test_normalize_domain_rejects_single_label() -> None:
    with pytest.raises(AssetValidationError):
        normalize_asset_value(AssetKind.DOMAIN, "localhost")


def test_normalize_ip_canonical() -> None:
    assert normalize_asset_value(AssetKind.IP, " 10.0.0.1 ") == "10.0.0.1"
    assert normalize_asset_value(AssetKind.IP, "::1") == "::1"


def test_normalize_ip_rejects_garbage() -> None:
    with pytest.raises(AssetValidationError):
        normalize_asset_value(AssetKind.IP, "999.1.1.1")


def test_normalize_url_strips_default_port_and_fragment() -> None:
    assert (
        normalize_asset_value(AssetKind.URL, "HTTPS://Example.COM:443/a?b=1#frag")
        == "https://example.com/a?b=1"
    )
    assert normalize_asset_value(AssetKind.URL, "http://example.com") == "http://example.com/"
    assert (
        normalize_asset_value(AssetKind.URL, "http://example.com:8080")
        == "http://example.com:8080/"
    )


def test_normalize_url_rejects_non_http() -> None:
    with pytest.raises(AssetValidationError):
        normalize_asset_value(AssetKind.URL, "ftp://example.com")


def test_normalize_service() -> None:
    assert normalize_asset_value(AssetKind.SERVICE, "Example.COM:8443") == "example.com:8443"
    assert normalize_asset_value(AssetKind.SERVICE, "10.0.0.1:22") == "10.0.0.1:22"


def test_normalize_service_rejects_bad_port() -> None:
    with pytest.raises(AssetValidationError):
        normalize_asset_value(AssetKind.SERVICE, "example.com:0")
    with pytest.raises(AssetValidationError):
        normalize_asset_value(AssetKind.SERVICE, "example.com")


def test_scope_target_for_service_is_host() -> None:
    assert scope_target_for_asset(AssetKind.SERVICE, "example.com:443") == "example.com"
    assert scope_target_for_asset(AssetKind.URL, "https://example.com/x") == (
        "https://example.com/x"
    )


def test_fingerprint_is_stable_and_normalized() -> None:
    first = finding_fingerprint("  Reflected   XSS ", "Example.com", " web ")
    second = finding_fingerprint("reflected xss", "example.com", "web")
    assert first == second
    assert len(first) == 64


def test_fingerprint_differs_by_category() -> None:
    assert finding_fingerprint("xss", "a.com", "web") != finding_fingerprint(
        "xss", "a.com", "api"
    )


def test_program_rules_dedupe_asset_types() -> None:
    rules = BountyProgramRules(
        operation_id="op_1",
        asset_types=(AssetKind.DOMAIN, AssetKind.DOMAIN, AssetKind.URL),
    )
    assert rules.asset_types == (AssetKind.DOMAIN, AssetKind.URL)


def test_program_rules_require_asset_type() -> None:
    with pytest.raises(ValidationError, match="at least one asset type"):
        BountyProgramRules(operation_id="op_1", asset_types=())


def test_duplicate_candidate_error_carries_context() -> None:
    error = DuplicateCandidateError("ab" * 32, "find_123")
    assert error.existing_finding_id == "find_123"
    assert "find_123" in str(error)


def test_template_is_a_valid_plan_structure() -> None:
    template = bug_bounty_template()
    plan = OperationPlan(
        operation_id="op_1",
        title=template.title,
        tasks=template.tasks,
        gates=template.gates,
    )
    assert plan.task_by_id("testing") is not None
    assert plan.gate_by_id("ready-to-report") is not None
    task_ids = [task.id for task in plan.tasks]
    assert task_ids == ["recon", "asset-inventory", "testing", "validation", "reporting"]
