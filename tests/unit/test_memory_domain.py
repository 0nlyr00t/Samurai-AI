from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from samurai_ai.domain.memory import MemoryEntry, MemoryError, MemoryKind


def test_memory_entry_defaults() -> None:
    entry = MemoryEntry(workspace="acme-bb", title="Baseline recon notes")
    assert entry.id.startswith("mem_")
    assert entry.kind is MemoryKind.NOTE
    assert entry.retention_days is None
    assert entry.version == 1
    assert not entry.is_expired()


def test_memory_workspace_validation() -> None:
    with pytest.raises(ValidationError):
        MemoryEntry(workspace="Bad Workspace", title="x")
    with pytest.raises(ValidationError):
        MemoryEntry(workspace="", title="x")
    with pytest.raises(ValidationError):
        MemoryEntry(workspace="-lead", title="x")


def test_memory_workspace_error_is_value_error() -> None:
    assert issubclass(MemoryError, ValueError)


def test_memory_retention_expiry() -> None:
    entry = MemoryEntry(workspace="ws", title="t", retention_days=7)
    assert not entry.is_expired()
    future = datetime.now(UTC) + timedelta(days=8)
    assert entry.is_expired(now=future)
    fresh = datetime.now(UTC) + timedelta(days=6)
    assert not entry.is_expired(now=fresh)


def test_memory_touch_bumps_version() -> None:
    entry = MemoryEntry(workspace="ws", title="t")
    before = entry.updated_at
    entry.touch()
    assert entry.version == 2
    assert entry.updated_at >= before


def test_memory_bounds() -> None:
    with pytest.raises(ValidationError):
        MemoryEntry(workspace="ws", title="t", body="x" * 8193)
    with pytest.raises(ValidationError):
        MemoryEntry(workspace="ws", title="t", retention_days=0)
    with pytest.raises(ValidationError):
        MemoryEntry(workspace="ws", title="t", tags=tuple(f"t{i}" for i in range(17)))
