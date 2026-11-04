import json
from pathlib import Path

from samurai_ai.cli import main


def _run_json(args: list[str], capsys) -> object:
    result = main(args)
    assert result == 0
    return json.loads(capsys.readouterr().out)


def _drive_to_verification(database: str, capsys, with_finding: bool = True) -> tuple[str, str]:
    """Create a bounty operation, init the playbook, validate one finding."""
    operation = _run_json(
        [
            "--db",
            database,
            "--json",
            "operation",
            "create",
            "Report CLI",
            "--mode",
            "bug-bounty",
            "--include",
            "*.example.com",
        ],
        capsys,
    )
    operation_id = operation["id"]
    assert main(["--db", database, "playbook", "init", operation_id]) == 0
    for status in ("planning", "approval"):
        assert main(["--db", database, "operation", "transition", operation_id, status]) == 0
    assert main(["--db", database, "plan", "approve", operation_id]) == 0
    for status in ("execution", "verification"):
        assert main(["--db", database, "operation", "transition", operation_id, status]) == 0
    capsys.readouterr()
    if not with_finding:
        return operation_id, ""

    artifact = _run_json(
        [
            "--db",
            database,
            "--json",
            "artifact",
            "add",
            operation_id,
            "--kind",
            "note",
            "--text",
            "proof of concept",
            "--requirement",
            "finding-evidence",
        ],
        capsys,
    )
    finding = _run_json(
        [
            "--db",
            database,
            "--json",
            "finding",
            "propose",
            operation_id,
            "Reflected XSS",
            "--severity",
            "high",
            "--asset",
            "https://api.example.com/",
            "--evidence",
            artifact["id"],
        ],
        capsys,
    )
    assert main(["--db", database, "finding", "validate", finding["id"]]) == 0
    capsys.readouterr()
    return operation_id, finding["id"]


def test_cli_report_generate_export_and_mark_reported(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    operation_id, finding_id = _drive_to_verification(database, capsys)

    report = _run_json(
        ["--db", database, "--json", "report", "generate", operation_id], capsys
    )
    assert report["finding_ids"] == [finding_id]

    reports = _run_json(
        ["--db", database, "--json", "report", "list", operation_id], capsys
    )
    assert [item["id"] for item in reports] == [report["id"]]

    assert main(["--db", database, "report", "show", report["id"]]) == 0
    shown = capsys.readouterr().out
    assert "Reflected XSS" in shown

    dest = tmp_path / "package"
    manifest = _run_json(
        [
            "--db",
            database,
            "--json",
            "report",
            "export",
            operation_id,
            report["id"],
            "--dest",
            str(dest),
        ],
        capsys,
    )
    assert (dest / "report.md").exists()
    assert (dest / "manifest.json").exists()
    assert len(list((dest / "artifacts").iterdir())) == 1
    assert manifest["report_id"] == report["id"]

    updated = _run_json(
        ["--db", database, "--json", "report", "mark-reported", operation_id, finding_id],
        capsys,
    )
    assert updated[0]["status"] == "reported"


def test_cli_report_requires_validated_findings(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "cli.db")
    operation_id, _ = _drive_to_verification(database, capsys, with_finding=False)
    assert main(["--db", database, "report", "generate", operation_id]) == 2
    capsys.readouterr()


def _fake_browser(monkeypatch) -> None:
    """Print a stand-in PDF instead of starting Docker."""
    import base64

    from samurai_ai.execution.pdf import PdfPrinter

    payload = b"%PDF-1.7 stand-in"

    def print_pdf(self, html: str, run_id: str) -> bytes:
        assert "<html" in html.lower()
        return payload

    monkeypatch.setattr(PdfPrinter, "print", print_pdf)
    _ = base64


def test_a_pdf_report_keeps_its_html_source_and_becomes_an_artifact(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    database = str(tmp_path / "pdf.db")
    operation_id, _ = _drive_to_verification(database, capsys)
    _fake_browser(monkeypatch)
    report = _run_json(
        ["--db", database, "--json", "report", "generate", operation_id, "--format", "pdf"],
        capsys,
    )
    assert report["format"] == "pdf"
    assert report["path"].endswith(".pdf")
    root = tmp_path / "reports" / operation_id
    pdf = root / report["path"].split("/")[-1]
    assert pdf.read_bytes().startswith(b"%PDF")
    assert list(root.glob("*.source.html")), "the printable source must stay"

    artifacts = _run_json(
        ["--db", database, "--json", "artifact", "list", operation_id], capsys
    )
    assert any(item["path"].endswith(".pdf") for item in artifacts)
