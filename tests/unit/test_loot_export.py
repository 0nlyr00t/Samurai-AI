import json
import stat
from pathlib import Path

from samurai_ai.domain.ctf import FlagRecord
from samurai_ai.domain.operations import OperationEvent, OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.ctf import CtfService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.loot import LootExportService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.ctf import CtfRepository
from samurai_ai.storage.database import Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.vault import VaultRepository


def test_loot_export_is_private_complete_and_signature_tracks_changes(tmp_path: Path) -> None:
    database = Database(tmp_path / "loot.db")
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    vault = VaultService(VaultRepository(database), operations.repository, tmp_path / "vault.key")
    ctf = CtfService(operations, kernel, CtfRepository(database), vault)
    operation = operations.create(
        "Loot export", OperationMode.CTF_HTB, ScopeContract(includes=("127.0.0.1",))
    )
    finding = kernel.create_finding(operation.id, "Validated issue")
    vault.add("admin-password", "hunter2", operation.id)
    vault.add("provider-access-token", "not-loot", operation.id)
    flag = FlagRecord(operation_id=operation.id, name="user", value="FLAG{example}")
    ctf.ctf.add_flag(
        flag,
        OperationEvent(operation_id=operation.id, event_type="flag.captured", payload={}),
    )
    service = LootExportService(kernel, vault, ctf, tmp_path / "loot")

    first_signature = service.signature(operation.id)
    paths = service.export(operation.id)
    assert paths["credentials"].parent == tmp_path / "loot" / operation.id
    findings = json.loads(paths["findings"].read_text())
    credentials = json.loads(paths["credentials"].read_text())
    flags = json.loads(paths["flags"].read_text())
    assert findings[0]["id"] == finding.id
    assert credentials == [
        {
            "id": credentials[0]["id"],
            "operation_id": operation.id,
            "name": "admin-password",
            "created_at": credentials[0]["created_at"],
            "created_by": "operator",
            "value": "hunter2",
        }
    ]
    assert flags[0]["value"] == "FLAG{example}"
    assert (tmp_path / "loot" / ".gitignore").read_text() == "*\n"
    assert stat.S_IMODE((tmp_path / "loot").stat().st_mode) == 0o700
    assert stat.S_IMODE(paths["credentials"].stat().st_mode) == 0o600
    assert service.signature(operation.id) == first_signature

    ctf.ctf.add_flag(
        FlagRecord(operation_id=operation.id, name="root", value="FLAG{root}"),
        OperationEvent(operation_id=operation.id, event_type="flag.captured", payload={}),
    )
    assert service.signature(operation.id) != first_signature
