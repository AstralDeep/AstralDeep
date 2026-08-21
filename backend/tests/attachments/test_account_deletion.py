"""Account-retirement boundary stays durable and distinct from logout."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.attachments.account_lifecycle import (
    account_retirement_status,
    initiate_account_retirement,
    purge_user_attachments,
)


class _Coordinator:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.owners: list[str] = []

    def schedule_owner(self, *, owner_id: str):
        self.owners.append(owner_id)
        return self.outcome

    async def aschedule_owner(self, *, owner_id: str):
        self.owners.append(owner_id)
        return self.outcome

    async def aowner_cleanup_status(self, *, owner_id: str, cleanup_id: str):
        self.owners.append(f"{owner_id}:{cleanup_id}")
        return self.outcome


def test_account_retirement_delegates_to_durable_owner_namespace() -> None:
    outcome = SimpleNamespace(completed=True, metadata_rows_soft_deleted=2)
    coordinator = _Coordinator(outcome)

    observed = purge_user_attachments(coordinator, "owner-1")  # type: ignore[arg-type]

    assert observed is outcome
    assert coordinator.owners == ["owner-1"]


def test_incomplete_physical_purge_is_returned_without_false_success() -> None:
    outcome = SimpleNamespace(completed=False, metadata_rows_soft_deleted=2)
    coordinator = _Coordinator(outcome)

    observed = purge_user_attachments(coordinator, "owner-1")  # type: ignore[arg-type]

    assert observed.completed is False


@pytest.mark.asyncio
async def test_async_retirement_and_status_use_verified_owner_identity() -> None:
    outcome = SimpleNamespace(cleanup_id="cleanup-1")
    coordinator = _Coordinator(outcome)

    assert await initiate_account_retirement(coordinator, "owner-1") is outcome  # type: ignore[arg-type]
    assert await account_retirement_status(  # type: ignore[arg-type]
        coordinator,
        "owner-1",
        "cleanup-1",
    ) is outcome
    assert coordinator.owners == ["owner-1", "owner-1:cleanup-1"]


def test_account_purge_is_not_mapped_to_logout() -> None:
    backend = Path(__file__).resolve().parents[2]
    mounted_sources = (
        backend / "orchestrator" / "api.py",
        backend / "orchestrator" / "auth.py",
        backend / "orchestrator" / "web_auth.py",
        backend / "orchestrator" / "orchestrator.py",
    )

    for source in mounted_sources:
        text = source.read_text(encoding="utf-8")
        assert "purge_user_attachments" not in text
        assert "initiate_account_retirement" not in text
