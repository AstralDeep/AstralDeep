"""Partial T027 reads against the real Plane facade and isolated PostgreSQL.

Synthetic operations use database-issued interactive session incarnations and
real database-clock observations through the public repositories. This read-only
fixture does not qualify external IAM or enable dispatch. Only the explicit
future-version fixture uses SQL, to reproduce storage written by a newer Plane.
"""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from astralplane.repositories.assignment_models import (
    AssignmentDefinition,
    AssignmentOperationAuthority,
    AssignmentOperationSpec,
)
from astralplane.repositories.history import SessionExecutionObservation, SessionRecord

from orchestrator.work_api import work_router
from orchestrator.work_service import WorkService
from persistent_agents.models import AssignmentError
from persistent_agents.runtime_values import digest
from persistent_agents.service import AssignmentService
from persistent_agents.store import AssignmentStore
from persistent_agents.tests.test_engine_postgres import plane as plane


@pytest.fixture
def records(plane):
    definition = AssignmentDefinition(
        name="Public task title", instructions="private instruction", source={},
        allowed_tools=(), consented_scopes=(), offline_grant_id=None,
        limits={"max_retries": 1, "max_concurrent_tasks": 1, "max_depth": 1, "max_tasks": 2,
                "model_calls": 2, "tool_calls": 2, "tokens": 100, "elapsed_ms": 1000},
    )
    repository = plane.repositories.assignments
    sessions = plane.repositories.history.sessions
    cipher = Fernet(Fernet.generate_key())
    identities = []
    with plane.transaction() as transaction:
        for owner in ("owner", "owner", "other"):
            identity = str(uuid4())
            now = int(datetime.now(UTC).timestamp())
            session = sessions.put(transaction, SessionRecord(
                session_id=str(uuid4()), owner_id=owner,
                access_token_ciphertext=cipher.encrypt(b"synthetic-access").decode(),
                refresh_token_ciphertext=cipher.encrypt(b"synthetic-refresh").decode(),
                interactive_anchor=now, hard_expires_at=now + 3600,
                last_refresh_at=now, resumed=False, created_at=now,
            ))
            state = sessions.get_execution_state(
                transaction, owner_id=owner, session_id=session.session_id)
            assert state is not None and state.credential.incarnation_id == session.incarnation_id
            observation = SessionExecutionObservation(
                credential=state.credential, started_at=state.observed_at,
                valid_until=state.observed_at + timedelta(seconds=15))
            record = repository.create_operation(
                transaction, owner_id=owner, assignment_id=identity,
                origin_namespace="web", caller_key=identity, command_digest=digest(identity),
                definition=definition, authority=observation,
                operation=AssignmentOperationSpec(
                    kind="chat", deadline_at=state.observed_at + timedelta(minutes=5),
                    source_retention="none", version=2,
                    authority=AssignmentOperationAuthority(
                        owner_id=owner, origin="interactive", reference_kind="session_incarnation",
                        reference_id=session.incarnation_id,
                        expires_at=state.observed_at + timedelta(minutes=10)),
                ),
            )
            assert record.operation["version"] == 2
            assert record.operation["authority"]["reference_id"] == session.incarnation_id
            identities.append(identity)
        grant = str(uuid4())
        plane.repositories.offline_grants.create_grant(
            transaction, grant_id=grant, owner_id="owner", agent_id=None,
            encrypted_refresh_token=b"opaque fixture bytes", issued_at=1, expires_at=9999999999999)
        limits = dict(definition.limits, cadence_seconds=60)
        limits.update({"daily_" + key: limits[key] for key in ("model_calls", "tool_calls", "tokens", "elapsed_ms")})
        legacy = repository.create_assignment(
            transaction, owner_id="owner", assignment_id=str(uuid4()), submission_id=str(uuid4()),
            submission_digest=digest("legacy"), definition=replace(
                definition, source={"reader": "fixture.read", "url": "https://example.org"},
                allowed_tools=("fixture.read",), consented_scopes=("tools:read",),
                offline_grant_id=grant, limits=limits))
    store = AssignmentStore(plane_runtime=plane)
    service = WorkService(AssignmentService(SimpleNamespace(), store=store, enabled=True, phi_gate=object()))
    try:
        yield plane, service, identities, legacy.assignment_id
    finally:
        store.close()


@pytest.mark.asyncio
async def test_real_owner_pagination_legacy_isolation_and_read_only_projection(records):
    plane, service, ids, legacy = records
    first = await service.list("owner", {"sub": "owner"}, limit=1)
    second = await service.list("owner", {"sub": "owner"}, limit=1, after_id=first["next_cursor"])
    last = await service.list("owner", {"sub": "owner"}, limit=1, after_id=second["next_cursor"])
    assert [first["operations"][0]["id"], second["operations"][0]["id"]] == sorted(ids[:2])
    assert last == {"operations": [], "next_cursor": None, "page_full": False}
    row = first["operations"][0]
    assert row["title"] == "Public task title" and row["kind"] == "chat"
    assert row["disposition"] == "queued" and row["revision"] == 1
    # No usage observation exists yet; the facade must not invent a zero.
    assert "tokens" not in row["usage"]["spent"]
    assert "spend_micro_units" not in row["usage"]["spent"]
    assert "private" not in str(row) and "authority" not in str(row)
    for identity in (ids[2], legacy, str(uuid4())):
        with pytest.raises(AssignmentError) as error:
            await service.get("owner", {"sub": "owner"}, identity)
        assert (error.value.code, error.value.status_code) == ("work_not_found", 404)
    assert (await service.get("other", {"sub": "other"}, ids[2]))["id"] == ids[2]
    assert (await service.get("owner", {"sub": "owner"}, row["id"]))["revision"] == 1


@pytest.mark.asyncio
async def test_real_poll_observes_committed_control_and_no_poll_side_effect(records):
    _, service, ids, _ = records
    owner = {"sub": "owner"}
    initial = await service.get("owner", owner, ids[0])
    unchanged = await service.poll("owner", owner, ids[0], after_revision=initial["revision"])
    assert unchanged == {"revision": 1, "changed": False, "resync_required": False, "operation": None}
    await service.store.transaction(lambda tx, repo: repo.apply_control(
        tx, owner_id="owner", assignment_id=ids[0], expected_state_version=initial["revision"],
        expected_instruction_revision=initial["instruction_revision"],
        expected_control_epoch=initial["control_epoch"], submission_id=str(uuid4()),
        submission_digest=digest("fixture-pause"), control="pause"))
    updated = await service.poll("owner", owner, ids[0], after_revision=initial["revision"])
    assert updated["changed"] is True and updated["operation"]["disposition"] == "paused"
    assert updated["revision"] > initial["revision"]
    assert (await service.get("owner", owner, ids[0]))["revision"] == updated["revision"]


@pytest.mark.asyncio
async def test_future_operation_payload_is_opaque_but_safe_outer_identity_is_readable(records):
    _, service, ids, _ = records
    def future_record(tx, repo):
        # Fixture-only corruption/forward-version simulation in the isolated schema.
        tx.execute(
            "UPDATE persistent_assignment SET data=jsonb_set(data, '{operation}', %s::jsonb) WHERE id=%s",
            ('{"version":3,"kind":{"private":"opaque"},"deadline_at":"private","authority":"private"}', ids[0]))
    await service.store.transaction(future_record)
    result = await service.get("owner", {"sub": "owner"}, ids[0])
    assert result["id"] == ids[0] and result["schema_supported"] is False
    assert result["disposition"] == "unsupported_version"
    assert result["kind"] is None and result["deadline_at"] is None
    assert "private" not in str(result)
    assert "actions" not in result


@pytest.mark.asyncio
async def test_http_reads_never_perform_synchronous_profile_persistence(records, monkeypatch):
    from tests.test_work_api_088 import override_read_auth
    plane, service, ids, _ = records
    profile_calls = []
    def save_profile(claims):
        profile_calls.append(claims["sub"])
        with plane.transaction() as transaction:
            plane.repositories.identity.upsert_identity(
                transaction, owner_id=claims["sub"], observed_at=1, roles=("user",))
    orch = SimpleNamespace(persistent_assignments=service.assignments, _save_user_profile=save_profile)
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(work_router, prefix="/api")
    override_read_auth(app, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for suffix in ("", "/" + ids[0], "/" + ids[0] + "/poll"):
            response = await client.get("/api/work/v1/operations" + suffix)
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    # An inherited helper catches profile-write exceptions, so HTTP 200 alone
    # would hide blocking I/O or a swallowed loop-guard rejection.
    assert profile_calls == []
