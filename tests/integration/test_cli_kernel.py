import json
from pathlib import Path

from samurai_ai.cli import main


def _run_json(args: list[str], capsys) -> object:
    result = main(args)
    assert result == 0
    return json.loads(capsys.readouterr().out)


def test_cli_kernel_end_to_end(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "kernel-cli.db")
    operation = _run_json(
        [
            "--db",
            database,
            "--json",
            "operation",
            "create",
            "Kernel CLI",
            "--mode",
            "red-team",
            "--objective",
            "Exercise the operation kernel",
            "--include",
            "192.0.2.0/24",
        ],
        capsys,
    )
    operation_id = operation["id"]

    assert main(["--db", database, "operation", "transition", operation_id, "planning"]) == 0
    capsys.readouterr()

    plan_file = tmp_path / "plan.json"
    plan_file.write_text(
        json.dumps(
            {
                "title": "Authorized lab plan",
                "tasks": [
                    {
                        "id": "recon",
                        "title": "Map the lab service",
                        "risk_class": "r1",
                        "required_artifacts": ["service-map"],
                    },
                    {
                        "id": "review",
                        "title": "Review the evidence",
                        "depends_on": ["recon"],
                        "required_artifacts": [],
                    },
                ],
                "gates": [
                    {
                        "id": "recon-complete",
                        "name": "Recon is complete",
                        "required_artifacts": ["service-proof"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    plan = _run_json(
        ["--db", database, "--json", "plan", "create", operation_id, str(plan_file)],
        capsys,
    )
    assert plan["approved"] is False

    assert main(["--db", database, "operation", "transition", operation_id, "approval"]) == 0
    capsys.readouterr()

    approval = _run_json(
        [
            "--db",
            database,
            "--json",
            "approval",
            "request",
            operation_id,
            "--action",
            "Run an active lab probe",
            "--risk",
            "r2",
            "--reason",
            "Planned in the approved ROE",
        ],
        capsys,
    )
    assert approval["status"] == "pending"

    assert main(["--db", database, "plan", "approve", operation_id]) == 0
    capsys.readouterr()
    assert (
        main(["--db", database, "operation", "transition", operation_id, "execution"]) == 2
    )
    assert "pending approvals" in capsys.readouterr().err

    approved = _run_json(
        [
            "--db",
            database,
            "--json",
            "approval",
            "approve",
            approval["id"],
            "--actor",
            "operator",
        ],
        capsys,
    )
    assert approved["status"] == "approved"
    assert main(["--db", database, "operation", "transition", operation_id, "execution"]) == 0
    capsys.readouterr()

    task_artifact = _run_json(
        [
            "--db",
            database,
            "--json",
            "artifact",
            "add",
            operation_id,
            "--kind",
            "command-output",
            "--task",
            "recon",
            "--requirement",
            "service-map",
            "--summary",
            "Lab service map",
            "--text",
            "192.0.2.10:443 open",
        ],
        capsys,
    )
    _run_json(
        [
            "--db",
            database,
            "--json",
            "artifact",
            "add",
            operation_id,
            "--kind",
            "note",
            "--gate",
            "recon-complete",
            "--requirement",
            "service-proof",
            "--summary",
            "Gate proof",
            "--text",
            "TLS endpoint responded as expected",
        ],
        capsys,
    )

    assert main(["--db", database, "task", "start", operation_id, "recon"]) == 0
    capsys.readouterr()
    assert main(["--db", database, "task", "complete", operation_id, "recon", "--force"]) == 0
    capsys.readouterr()
    assert main(["--db", database, "gate", "pass", operation_id, "recon-complete"]) == 0
    capsys.readouterr()
    assert main(["--db", database, "task", "start", operation_id, "review"]) == 0
    capsys.readouterr()
    assert main(["--db", database, "task", "complete", operation_id, "review", "--force"]) == 0
    capsys.readouterr()

    assert main(["--db", database, "operation", "transition", operation_id, "verification"]) == 0
    capsys.readouterr()
    assert main(["--db", database, "operation", "transition", operation_id, "reporting"]) == 0
    capsys.readouterr()

    finding = _run_json(
        [
            "--db",
            database,
            "--json",
            "finding",
            "create",
            operation_id,
            "Lab validation finding",
            "--severity",
            "medium",
            "--summary",
            "A controlled lab validation result",
        ],
        capsys,
    )
    _run_json(
        [
            "--db",
            database,
            "--json",
            "finding",
            "attach",
            finding["id"],
            task_artifact["id"],
        ],
        capsys,
    )
    validated = _run_json(
        ["--db", database, "--json", "finding", "validate", finding["id"]],
        capsys,
    )
    assert validated["status"] == "validated"
    reported = _run_json(
        ["--db", database, "--json", "finding", "report", finding["id"]],
        capsys,
    )
    assert reported["status"] == "reported"

    assert main(["--db", database, "operation", "transition", operation_id, "complete"]) == 0
    final_output = capsys.readouterr().out
    assert "complete" in final_output

    artifact_file = tmp_path / task_artifact["path"]
    assert artifact_file.read_text(encoding="utf-8") == "192.0.2.10:443 open"


def test_cli_gate_fails_without_required_evidence(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "kernel-negative.db")
    operation = _run_json(
        [
            "--db",
            database,
            "--json",
            "operation",
            "create",
            "Kernel negative",
            "--mode",
            "ctf-htb",
            "--include",
            "192.0.2.10",
        ],
        capsys,
    )
    plan_file = tmp_path / "negative-plan.json"
    plan_file.write_text(
        json.dumps(
            {
                "title": "Negative plan",
                "tasks": [],
                "gates": [
                    {
                        "id": "proof",
                        "name": "Proof required",
                        "required_artifacts": ["flag-proof"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _run_json(
        ["--db", database, "--json", "plan", "create", operation["id"], str(plan_file)],
        capsys,
    )

    assert main(["--db", database, "gate", "pass", operation["id"], "proof"]) == 2
    assert "missing required artifact" in capsys.readouterr().err
