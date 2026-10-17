"""Exercises framework_runtime.py against the application's real Plane, session and audit facades.
Startup and shutdown tests retain the existing assignment supervisor and its owned store.
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator import framework_runtime
from orchestrator.framework_credentials import FrameworkCredentialService
from orchestrator.orchestrator import Orchestrator
from orchestrator.projection_surfaces import connections
from orchestrator.work_operations import FrameworkWorkOperations
from persistent_agents.models import AssignmentError
from persistent_agents.runtime import start_assignment_runtime
from persistent_agents.service import AssignmentService
from persistent_agents.tests.test_runtime_composition_088 import host as host, plane as plane
from shared.feature_flags import flags
from tests.helpers.session_consent_088 import consent_from_store
from tests.test_runtime_composition_074 import _StartAsyncTasks


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setitem(flags._flags, "framework_credentials", True)
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-framework-runtime-audit")


@pytest.mark.asyncio
async def test_issuer_without_assignments_uses_current_real_session_and_audit(host):
    credentials = framework_runtime.start_framework_runtime(host)
    assert isinstance(credentials, FrameworkCredentialService)
    assert credentials is host.framework_credentials
    assert host.framework_work_operations is None and host.persistent_assignments is None
    assert host.persistent_assignment_runner is None
    assert credentials._sessions is host.runtime_composition.plane.repositories.history.sessions
    assert credentials._sessions is host.web_sessions._sessions.repository
    assert credentials._audit is host.audit_repo
    owner, sid = str(uuid4()), uuid4().hex
    host.web_sessions.create(sid, user_id=owner, access_token="synthetic-access",
                             refresh_token="synthetic-refresh", hard_max_seconds=3600)
    assert "Issue a new key" in await connections.render(host, owner, (), {})
    observation = consent_from_store(host.web_sessions, owner, sid).observation
    row, token = credentials.issue(owner_id=owner, caller=observation, name="Runtime reader",
        scopes=("operations.read",), expires_in_seconds=300, max_admissions=1)
    assert credentials.resolve_bearer(token).owner_id == owner
    assert credentials.list(owner_id=owner)[0]["consumed_admissions"] == 0
    credentials.revoke(owner_id=owner, credential_id=row["credential_id"])
    assert credentials.resolve_bearer(token) is None
    host.web_sessions.delete(sid)
    with pytest.raises(AssignmentError):
        credentials.issue(owner_id=owner, caller=observation, name="Retired session",
            scopes=("operations.read",), expires_in_seconds=300, max_admissions=1)
    events, _ = host.audit_repo.list_for_user(owner)
    assert {event.action_type for event in events} == {"auth.framework_issue", "auth.framework_revoke"}
    assert host.audit_repo.verify_chain(owner) is None
    framework_runtime.stop_framework_runtime(host)
    assert host.framework_credentials is None and host.framework_work_operations is None
    assert host.runtime_composition.plane.runtime.health().ready


@pytest.mark.asyncio
async def test_mounted_operations_use_the_same_real_credential_fences(host, monkeypatch):
    monkeypatch.setitem(flags._flags, "persistent_agents", True)
    runner = start_assignment_runtime(host)
    try:
        credentials = framework_runtime.start_framework_runtime(host)
        operations = host.framework_work_operations
        owner, sid = str(uuid4()), uuid4().hex
        host.web_sessions.create(sid, user_id=owner, access_token="synthetic-access",
                                 refresh_token="synthetic-refresh", hard_max_seconds=3600)
        row, token = credentials.issue(owner_id=owner,
            caller=consent_from_store(host.web_sessions, owner, sid).observation,
            name="Runtime reader", scopes=("operations.read",), expires_in_seconds=300, max_admissions=1)
        caller = credentials.resolve_bearer(token)
        assert (await operations.list(caller))["operations"] == []
        with pytest.raises(AssignmentError, match="framework_scope_required"):
            await operations.result(caller, str(uuid4()))
        credentials.revoke(owner_id=owner, credential_id=row["credential_id"])
        with pytest.raises(AssignmentError, match="framework_credential_authority_unavailable"):
            await operations.list(caller)
    finally:
        framework_runtime.stop_framework_runtime(host)
        await runner.stop()
        runner.store.close()


def test_flag_off_does_not_construct_or_require_any_dependencies(monkeypatch):
    monkeypatch.setitem(flags._flags, "framework_credentials", False)
    host = SimpleNamespace()
    assert framework_runtime.start_framework_runtime(host) is None
    assert host.framework_credentials is None and host.framework_work_operations is None
    framework_runtime.stop_framework_runtime(host)


@pytest.mark.parametrize("attribute", ["framework_credentials", "framework_work_operations"])
def test_repeated_mount_preserves_existing_facades(host, attribute):
    existing = object()
    setattr(host, attribute, existing)
    with pytest.raises(RuntimeError, match="already initialized"):
        framework_runtime.start_framework_runtime(host)
    assert getattr(host, attribute) is existing


@pytest.mark.parametrize("failure", ["missing_plane", "missing_audit", "invalid_audit", "credentials"])
def test_issuer_constructor_failure_publishes_no_partial_facade(host, monkeypatch, failure):
    if failure == "missing_plane":
        host.runtime_composition = None
    elif failure == "missing_audit":
        del host.audit_repo
    elif failure == "invalid_audit":
        host.audit_repo = object()
    else:
        def refuse(**kwargs):
            raise RuntimeError("synthetic credential construction failure")
        monkeypatch.setattr(framework_runtime, "FrameworkCredentialService", refuse)
    with pytest.raises((RuntimeError, AttributeError, ValueError)):
        framework_runtime.start_framework_runtime(host)
    assert host.framework_credentials is None and host.framework_work_operations is None


@pytest.mark.parametrize("failure", ["type", "owner", "runtime", "repository", "operations"])
def test_work_facade_requires_the_exact_existing_assignment_composition(host, monkeypatch, failure):
    assignments = AssignmentService(host)
    host.persistent_assignments = assignments
    try:
        if failure == "type":
            host.persistent_assignments = object()
        elif failure == "owner":
            assignments.orch = object()
        elif failure == "runtime":
            assignments.store.plane_runtime = object()
        elif failure == "repository":
            assignments.store.repository = object()
        else:
            def refuse(**kwargs):
                raise RuntimeError("synthetic Work facade construction failure")
            monkeypatch.setattr(framework_runtime, "FrameworkWorkOperations", refuse)
        with pytest.raises(RuntimeError):
            framework_runtime.start_framework_runtime(host)
        assert host.framework_credentials is None and host.framework_work_operations is None
        assert not assignments.store.async_runtime.snapshot().closed
    finally:
        assignments.store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "constructor", "cancel"])
async def test_normal_start_finally_detaches_facades_and_settles_the_existing_runner(host, monkeypatch, failure):
    events, mounted = [], []
    host.async_task_manager = _StartAsyncTasks(events)

    async def close_plane():
        assert host.framework_credentials is None and host.framework_work_operations is None
        assert mounted[0]._loop.done()
        assert mounted[0].store.async_runtime.snapshot().closed
        events.append("plane.close")

    host.runtime_composition.close = close_plane
    host._close_started_services_once = lambda: Orchestrator._close_started_services_once(host)
    host._close_started_services = lambda: Orchestrator._close_started_services(host)

    async def construct():
        runner = start_assignment_runtime(host)
        mounted.append(runner)
        framework_runtime.start_framework_runtime(host)
        assert isinstance(host.framework_work_operations, FrameworkWorkOperations)
        assert host.framework_work_operations.assignments is runner.service
        assert host.framework_work_operations.store is runner.store

    host._run_started_server = construct
    if failure:
        error = asyncio.CancelledError if failure == "cancel" else RuntimeError

        def refuse(**kwargs):
            raise error("synthetic Work facade construction failure")
        monkeypatch.setattr(framework_runtime, "FrameworkWorkOperations", refuse)
        with pytest.raises(error, match="synthetic Work facade"):
            await Orchestrator.start(host)
    else:
        await Orchestrator.start(host)
    assert events[-1] == "plane.close"
    assert host.runtime_composition.plane.runtime.health().ready
    assert mounted[0]._active == {} and mounted[0]._loop.done()
