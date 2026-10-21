import pytest
from pydantic import ValidationError

from samurai_ai.domain.reports import (
    BountyReport,
    ManifestFile,
    ReportFormat,
    ReportManifest,
)


def test_bounty_report_frozen() -> None:
    report = BountyReport(
        operation_id="op_1",
        format=ReportFormat.MARKDOWN,
        sha256="ab" * 32,
        size_bytes=10,
        path="reports/op_1/rep_1.md",
        finding_ids=("find_1",),
        asset_count=2,
    )
    with pytest.raises(ValidationError):
        report.asset_count = 5


def test_bounty_report_requires_valid_sha256() -> None:
    with pytest.raises(ValidationError):
        BountyReport(
            operation_id="op_1",
            format=ReportFormat.JSON,
            sha256="short",
            size_bytes=1,
            path="reports/op_1/rep_1.json",
            finding_ids=(),
            asset_count=0,
        )


def test_manifest_roundtrip() -> None:
    manifest = ReportManifest(
        report_id="rep_1",
        operation_id="op_1",
        files=(ManifestFile(path="report.md", sha256="cd" * 32, size_bytes=3),),
        finding_ids=("find_1", "find_2"),
    )
    restored = ReportManifest.model_validate_json(manifest.model_dump_json())
    assert restored == manifest
