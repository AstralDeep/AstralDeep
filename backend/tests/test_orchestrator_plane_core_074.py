"""Focused app-Plane proofs for Orchestrator's former Database callers."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import threading
from types import SimpleNamespace

from astralplane.repositories.background_tasks import BackgroundTaskStatus
import pytest

from orchestrator.history import HistoryManager
from orchestrator.orchestrator import Orchestrator


class _Runtime:
    def __init__(self) -> None:
        self.transactions = 0
        self.ready = True

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield object()

    def health(self):
        return SimpleNamespace(ready=self.ready)


class _Theme:
    def __init__(self) -> None:
        self.record = SimpleNamespace(theme={"preset": "ocean"})
        self.put_calls = []

    def get(self, _transaction, *, owner_id):
        assert owner_id == "owner-1"
        return self.record

    def put(self, _transaction, *, owner_id, theme):
        self.put_calls.append((owner_id, dict(theme)))


class _Identity:
    def __init__(self) -> None:
        self.upserts = []
        self.admin_limits = []

    def list_external_identities(self, _transaction, *, owner_id, limit):
        assert (owner_id, limit) == ("owner-1", 100)
        return (
            SimpleNamespace(
                provider="orcid",
                subject="0000-0002-1825-0097",
                issuer="https://orcid.org",
                agent_id="journal-review-1",
                verified_at=123,
            ),
        )

    def upsert_identity(self, _transaction, **values):
        self.upserts.append(values)

    def list_identities_for_administration(self, _transaction, *, limit):
        self.admin_limits.append(limit)
        return ()


class _BackgroundTasks:
    def __init__(self) -> None:
        self.records = {}
        self.marked = []

    def list_for_owner(self, _transaction, *, owner_id, status, limit):
        assert owner_id == "owner-1"
        assert limit == 20
        return tuple(self.records.get(status, ()))

    def mark_notified(self, _transaction, *, owner_id, task_id):
        self.marked.append((owner_id, task_id))
        return True


def _orchestrator():
    runtime = _Runtime()
    theme = _Theme()
    identity = _Identity()
    background = _BackgroundTasks()
    repositories = SimpleNamespace(
        preferences=SimpleNamespace(theme=theme),
        identity=identity,
        background_tasks=background,
    )
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.plane_repository_source = SimpleNamespace(
        plane_runtime=runtime,
        plane_repositories=repositories,
    )
    orchestrator.attachment_purge_coordinator = SimpleNamespace(
        assert_globally_ready=lambda _transaction: None,
    )
    return orchestrator, runtime, theme, identity, background


def _task(task_id, status, *, created_at, notified=False):
    return SimpleNamespace(
        task_id=task_id,
        conversation_id=f"chat-{task_id}",
        status=status,
        summary=None,
        completed_at=created_at,
        created_at=created_at,
        notified=notified,
    )


def test_preferences_and_identity_use_one_typed_transaction() -> None:
    orchestrator, runtime, theme, identity, _background = _orchestrator()

    preferences = orchestrator._load_user_preferences("owner-1")

    assert preferences["theme"] == {"preset": "ocean"}
    assert preferences["verified_external_identities"]["orcid"] == {
        "subject": "0000-0002-1825-0097",
        "issuer": "https://orcid.org",
        "verified_by_agent": "journal-review-1",
        "verified_at": 123,
    }
    assert runtime.transactions == 1
    assert theme.put_calls == []
    assert identity.upserts == []


def test_theme_profile_and_readiness_use_application_plane() -> None:
    orchestrator, runtime, theme, identity, _background = _orchestrator()

    orchestrator._save_theme_preference("owner-1", {"preset": "forest"})
    orchestrator._save_user_profile(
        {
            "sub": "owner-1",
            "email": "owner@example.test",
            "preferred_username": "owner",
            "name": "Owner",
            "realm_access": {"roles": ["user"]},
            "resource_access": {"astral-frontend": {"roles": ["operator"]}},
        }
    )
    orchestrator._probe_plane_readiness()

    assert theme.put_calls == [("owner-1", {"preset": "forest"})]
    assert len(identity.upserts) == 1
    upsert = identity.upserts[0]
    assert upsert["owner_id"] == "owner-1"
    assert upsert["email"] == "owner@example.test"
    assert set(upsert["roles"]) == {"user", "operator"}
    assert isinstance(upsert["observed_at"], int)
    assert identity.admin_limits == [1]
    assert runtime.transactions == 3


def test_readiness_refuses_incomplete_physical_attachment_purge() -> None:
    orchestrator, runtime, _theme, _identity, _background = _orchestrator()

    def refuse(_transaction) -> None:
        raise RuntimeError("purge_reconciliation_incomplete")

    orchestrator.attachment_purge_coordinator = SimpleNamespace(
        assert_globally_ready=refuse,
    )

    with pytest.raises(RuntimeError, match="purge_reconciliation_incomplete"):
        orchestrator._probe_plane_readiness()

    assert runtime.transactions == 1


@pytest.mark.asyncio
async def test_application_readiness_offloads_plane_and_requires_publication_recovery() -> None:
    orchestrator, _runtime, _theme, _identity, _background = _orchestrator()
    loop_thread = threading.get_ident()
    probe_threads: list[int] = []

    def probe_plane() -> None:
        probe_threads.append(threading.get_ident())

    orchestrator._probe_plane_readiness = probe_plane
    orchestrator.generated_agent_publication_service = SimpleNamespace(
        readiness=lambda: asyncio.sleep(0, result=SimpleNamespace(ready=True))
    )

    await orchestrator._probe_application_readiness()

    assert probe_threads and probe_threads[0] != loop_thread

    orchestrator.generated_agent_publication_service = SimpleNamespace(
        readiness=lambda: asyncio.sleep(0, result=SimpleNamespace(ready=False))
    )
    with pytest.raises(RuntimeError, match="publication recovery"):
        await orchestrator._probe_application_readiness()


def test_background_replay_is_bounded_ordered_and_owner_scoped() -> None:
    orchestrator, runtime, _theme, _identity, background = _orchestrator()
    now = datetime(2026, 8, 14, tzinfo=UTC)
    background.records = {
        BackgroundTaskStatus.COMPLETED: (
            _task("b", BackgroundTaskStatus.COMPLETED, created_at=now),
            _task(
                "hidden",
                BackgroundTaskStatus.COMPLETED,
                created_at=now + timedelta(seconds=1),
                notified=True,
            ),
        ),
        BackgroundTaskStatus.FAILED: (
            _task("a", BackgroundTaskStatus.FAILED, created_at=now),
        ),
    }

    records = orchestrator._background_tasks_for_replay("owner-1")
    orchestrator._mark_background_tasks_notified(
        "owner-1", tuple(record.task_id for record in records)
    )

    assert [record.task_id for record in records] == ["a", "b"]
    assert background.marked == [("owner-1", "a"), ("owner-1", "b")]
    assert runtime.transactions == 2


def test_legacy_json_history_is_never_imported_or_renamed(tmp_path) -> None:
    legacy = tmp_path / "chats.json"
    legacy.write_text('{"sensitive": "unchanged"}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="AstralPlane import/recovery"):
        HistoryManager(data_dir=str(tmp_path))

    assert legacy.read_text(encoding="utf-8") == '{"sensitive": "unchanged"}'
    assert not (tmp_path / "chats.json.bak").exists()
