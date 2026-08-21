"""Plane-backed attachment availability checks for audit artifact pointers."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from audit.api import _availability_resolver, list_audit
from orchestrator.attachments.repository import AttachmentRepository
from orchestrator.plane_repository_context import ApplicationPlaneSource
from tests.attachments.conftest import (
    StubDatabase,
    attachment_plane_source,
    insert_sample,
)


class _PlaneRuntime:
    def __init__(self, repositories):
        self.repositories = repositories

    @contextmanager
    def transaction(self):
        yield object()


def test_attachment_pointer_uses_owner_scoped_app_plane_source() -> None:
    database = StubDatabase()
    source = attachment_plane_source(database)
    attachment_id = insert_sample(
        AttachmentRepository.from_plane_source(source),
        user_id="owner-1",
    )
    runtime = _PlaneRuntime(source.plane_repositories)
    orchestrator = SimpleNamespace(
        plane_repository_source=ApplicationPlaneSource(
            plane_runtime=runtime,
            plane_repositories=runtime.repositories,
        )
    )

    owner_resolver = _availability_resolver(orchestrator, "owner-1")
    assert owner_resolver(
        {"store": "user_attachments", "artifact_id": attachment_id}
    )
    assert not _availability_resolver(orchestrator, "another-owner")(
        {"store": "user_attachments", "artifact_id": attachment_id}
    )
    assert owner_resolver({"store": "opaque-integration", "artifact_id": "x"})
    assert owner_resolver({})


def test_attachment_pointer_failure_remains_opaque() -> None:
    class _Unavailable:
        def get_by_id(self, _attachment_id, _owner_id):
            raise RuntimeError("unavailable")

    resolver = _availability_resolver(
        SimpleNamespace(attachment_repository=_Unavailable()),
        "owner-1",
    )
    assert resolver({"store": "user_attachments", "artifact_id": "a1"})


@pytest.mark.asyncio
async def test_list_and_attachment_availability_run_together_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_thread = threading.get_ident()
    observed_threads: list[int] = []

    class _Attachments:
        def get_by_id(self, attachment_id, owner_id):
            observed_threads.append(threading.get_ident())
            assert (attachment_id, owner_id) == ("a1", "owner-1")
            return object()

    class _AuditRepository:
        def list_for_user(self, owner_id, **values):
            observed_threads.append(threading.get_ident())
            assert owner_id == "owner-1"
            assert values["availability_resolver"](
                {"store": "user_attachments", "artifact_id": "a1"}
            )
            return [], None

    orchestrator = SimpleNamespace(
        audit_repo=_AuditRepository(),
        attachment_repository=_Attachments(),
    )
    request = SimpleNamespace(
        query_params={},
        app=SimpleNamespace(state=SimpleNamespace(orchestrator=orchestrator)),
    )
    monkeypatch.setattr("audit.api.get_recorder", lambda: None)

    response = await list_audit(
        request,
        limit=1,
        cursor=None,
        event_class=[],
        outcome=[],
        from_ts=None,
        to_ts=None,
        q=None,
        user_id="owner-1",
        payload={"sub": "owner-1"},
    )

    assert response.items == []
    assert len(observed_threads) == 2
    assert all(thread_id != loop_thread for thread_id in observed_threads)
