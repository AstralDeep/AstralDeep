"""Account-retirement boundary stays durable, explicit, and unmounted."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from orchestrator.attachments.account_lifecycle import purge_user_attachments


class _Coordinator:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.owners: list[str] = []

    def schedule_owner(self, *, owner_id: str):
        self.owners.append(owner_id)
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


def test_account_purge_is_not_mapped_to_logout_or_an_unapproved_route() -> None:
    backend = Path(__file__).resolve().parents[2]
    mounted_sources = (
        backend / "orchestrator" / "api.py",
        backend / "orchestrator" / "auth.py",
        backend / "orchestrator" / "web_auth.py",
        backend / "orchestrator" / "orchestrator.py",
    )

    for source in mounted_sources:
        assert "purge_user_attachments" not in source.read_text(encoding="utf-8")
