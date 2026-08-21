"""Typed AstralPlane helpers for Deep web-session integration tests."""

from __future__ import annotations

from collections.abc import Iterable

from astralplane.repositories.history import SessionRecord
from astralplane.repositories.revocations import RevocationQueueRecord
from orchestrator.session_store import WebSessionStore
from tests.helpers.voice_plane_runtime import (
    PlaneTestRuntime,
    isolated_plane_runtime,
)


def web_session_store(runtime: PlaneTestRuntime) -> WebSessionStore:
    """Bind Deep's session policy to the already-created Plane runtime."""

    return WebSessionStore(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )


def get_session_record(
    runtime: PlaneTestRuntime,
    session_id: str,
) -> SessionRecord | None:
    """Read one opaque session through Plane's explicit admin boundary."""

    with runtime.transaction() as transaction:
        return runtime.repositories.history.sessions.get_by_session_id_for_administration(
            transaction,
            session_id=session_id,
        )


def replace_session_record(
    runtime: PlaneTestRuntime,
    record: SessionRecord,
) -> SessionRecord:
    """Replace one fixture session atomically through owner-scoped APIs."""

    repository = runtime.repositories.history.sessions
    with runtime.transaction() as transaction:
        current = repository.get_by_session_id_for_administration(
            transaction,
            session_id=record.session_id,
        )
        if current is not None:
            repository.delete(
                transaction,
                owner_id=current.owner_id,
                session_id=current.session_id,
            )
        return repository.put(transaction, record)


def revocation_records(
    runtime: PlaneTestRuntime,
    owner_id: str,
) -> tuple[RevocationQueueRecord, ...]:
    """Return only one owner's queued revocations through the typed contract."""

    with runtime.transaction() as transaction:
        return runtime.repositories.revocations.pending_for_owner(
            transaction,
            owner_id=owner_id,
            limit=200,
        )


def purge_revocations(
    runtime: PlaneTestRuntime,
    owner_ids: Iterable[str],
) -> None:
    """Remove fixture-owned queue rows without an administrative SQL escape."""

    repository = runtime.repositories.revocations
    with runtime.transaction() as transaction:
        for owner_id in owner_ids:
            for record in repository.pending_for_owner(
                transaction,
                owner_id=owner_id,
                limit=200,
            ):
                repository.resolve(
                    transaction,
                    owner_id=owner_id,
                    queue_id=record.queue_id,
                )


__all__ = (
    "PlaneTestRuntime",
    "get_session_record",
    "isolated_plane_runtime",
    "purge_revocations",
    "replace_session_record",
    "revocation_records",
    "web_session_store",
)
