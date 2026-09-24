"""Tests for orchestrator/work_submit.py's opt-in finite research acceptance against
real encrypted config and Plane: budget refusal precedes config capture, config stays
locked through the atomic audit, and replay ignores later changes.
"""

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from llm_config import research_profile as profile
from llm_config.user_store import UserLLMConfigStore
from orchestrator.work_submit import FixedResearchPreflight, WorkSubmitService
from persistent_agents.models import AssignmentError
from persistent_agents.runtime_values import thaw
from tests.test_work_submit_postgres_088 import (
    command,
    context,
    fixture as fixture,
    plane as plane,
    service as service,
    signing_key as signing_key,
    totals,
)

runtime = plane
source_service = service
pytestmark = pytest.mark.asyncio


def research_command(service, **changes):
    source = service.assignments.tool_bound("web-research-1:fetch_page")
    limits = {
        "model_calls": source["model_calls"] + 1,
        "tool_calls": source["tool_calls"],
        "tokens": source["tokens"] + profile.RESERVED_TOKENS,
        "elapsed_ms": source["elapsed_ms"] + profile.RESERVED_MILLISECONDS,
        "max_retries": 0,
    }
    limits.update(changes)
    return command(limits=limits)


@pytest.fixture
async def research_service(source_service, runtime, fixture, monkeypatch, tmp_path):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "preflight_test")
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-preflight-binding-" + "x" * 40)
    monkeypatch.delenv("AUDIT_HMAC_SECRET_PREFLIGHT_TEST", raising=False)
    store = UserLLMConfigStore(plane_runtime=runtime, data_dir=str(tmp_path))
    await store.set(
        fixture[1],
        provider="openai",
        base_url=profile.BASE_URL,
        model=profile.MODEL,
        api_key="synthetic-never-sent-provider-key",
    )
    return WorkSubmitService(
        source_service.assignments,
        source_service.audit,
        source_service.sessions,
        research_preflight=FixedResearchPreflight(store),
    )


def no_acceptance(runtime, owner):
    assert totals(runtime, owner) == (0, 0, 0)
    with runtime.transaction() as tx:
        assert (
            tx.fetch_one("SELECT count(*) AS n FROM persistent_assignment_action")["n"]
            == 0
        )


async def test_exact_minimum_accepts_without_persisting_private_config(
    research_service, fixture, runtime
):
    service = research_service
    accepted = await service.submit(
        await context(fixture, runtime), research_command(service)
    )
    assert accepted.created and totals(runtime, fixture[1]) == (1, 1, 1)
    assert accepted.record.definition.limits["tokens"] == profile.RESERVED_TOKENS
    assert accepted.record.definition.limits["elapsed_ms"] == 95000
    serialized = json.dumps(thaw(accepted.record))
    assert "synthetic-never-sent-provider-key" not in serialized
    assert "preflight_test" not in serialized and "api_key_ciphertext" not in serialized
    assert service.audit.verify_chain(fixture[1]) is None
    assert "synthetic" not in repr(service.research_preflight)
    with runtime.transaction() as tx:
        assert (
            tx.fetch_one("SELECT count(*) AS n FROM persistent_assignment_action")["n"]
            == 0
        )


@pytest.mark.parametrize("dimension", ["tokens", "elapsed_ms"])
async def test_budget_refusal_precedes_config_capture_and_acceptance(
    research_service, fixture, runtime, monkeypatch, dimension
):
    service = research_service
    values = json.loads(research_command(service))["limits"]
    values[dimension] -= 1
    capture = AsyncMock(side_effect=AssertionError("config touched"))
    monkeypatch.setattr(
        service.research_preflight.config_store, "capture_user", capture
    )
    with pytest.raises(AssignmentError) as caught:
        await service.submit(await context(fixture, runtime), command(limits=values))
    assert (caught.value.code, caught.value.status_code) == (
        "work_research_budget_insufficient",
        422,
    )
    capture.assert_not_called()
    no_acceptance(runtime, fixture[1])


@pytest.mark.parametrize(
    "change", ["missing", "foreign", "provider", "model", "base", "key", "ciphertext"]
)
async def test_unsupported_user_config_never_admits_or_falls_back(
    research_service, fixture, runtime, change
):
    service = research_service
    store = service.research_preflight.config_store
    await store.get(fixture[1])
    await store.set_system(
        provider="openai",
        base_url=profile.BASE_URL,
        model=profile.MODEL,
        api_key="synthetic-system-key",
        updated_by=fixture[1],
    )
    with runtime.transaction() as tx:
        repo = runtime.repositories.encrypted_llm_config
        row = repo.get_user(tx, owner_id=fixture[1])
        repo.delete_user(tx, owner_id=fixture[1])
        if change != "missing":
            repo.upsert_user(
                tx,
                owner_id="other-owner" if change == "foreign" else fixture[1],
                provider="custom" if change == "provider" else row.provider,
                base_url="https://other.invalid/v1"
                if change == "base"
                else row.base_url,
                model="other-model" if change == "model" else row.model,
                api_key_ciphertext=None
                if change == "key"
                else "not-fernet"
                if change == "ciphertext"
                else row.api_key_ciphertext,
            )
    with pytest.raises(AssignmentError) as caught:
        await service.submit(await context(fixture, runtime), research_command(service))
    assert (caught.value.code, caught.value.status_code) == (
        "work_research_profile_unavailable",
        503,
    )
    no_acceptance(runtime, fixture[1])


@pytest.mark.parametrize("change", ["missing", "conflict", "invalid-id"])
async def test_named_key_unavailable_refuses_new_admission(
    research_service, fixture, runtime, monkeypatch, change
):
    if change == "missing":
        monkeypatch.delenv("AUDIT_HMAC_SECRET")
    elif change == "conflict":
        monkeypatch.setenv(
            "AUDIT_HMAC_SECRET_PREFLIGHT_TEST", "conflicting-" + "y" * 40
        )
    else:
        monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "UPPERCASE")
    with pytest.raises(AssignmentError, match="work_research_profile_unavailable"):
        await research_service.submit(
            await context(fixture, runtime), research_command(research_service)
        )
    no_acceptance(runtime, fixture[1])


@pytest.mark.parametrize("change", ["row", "delete", "key"])
async def test_capture_cannot_adopt_a_replacement_before_acceptance(
    research_service, fixture, runtime, monkeypatch, change
):
    service = research_service
    original = FixedResearchPreflight.prepare

    async def prepare(self, **kwargs):
        value = await original(self, **kwargs)
        if change == "key":
            monkeypatch.setenv(
                "AUDIT_HMAC_SECRET", "synthetic-replaced-key-" + "z" * 40
            )
        else:
            with runtime.transaction() as tx:
                repo = runtime.repositories.encrypted_llm_config
                row = repo.get_user(tx, owner_id=fixture[1])
                repo.delete_user(tx, owner_id=fixture[1])
                if change == "row":
                    repo.upsert_user(
                        tx,
                        owner_id=fixture[1],
                        provider=row.provider,
                        base_url=row.base_url,
                        model=row.model,
                        api_key_ciphertext=row.api_key_ciphertext,
                    )
        return value

    monkeypatch.setattr(FixedResearchPreflight, "prepare", prepare)
    with pytest.raises(AssignmentError, match="work_research_profile_unavailable"):
        await service.submit(await context(fixture, runtime), research_command(service))
    no_acceptance(runtime, fixture[1])


async def test_key_loss_during_audit_rolls_back_operation_receipt_and_audit(
    research_service, fixture, runtime, monkeypatch
):
    service = research_service
    original = service.audit.insert_in_transaction

    def append(*args, **kwargs):
        value = original(*args, **kwargs)
        monkeypatch.delenv("AUDIT_HMAC_SECRET")
        return value

    monkeypatch.setattr(service.audit, "insert_in_transaction", append)
    with pytest.raises(AssignmentError, match="work_research_profile_unavailable"):
        await service.submit(await context(fixture, runtime), research_command(service))
    no_acceptance(runtime, fixture[1])


async def test_accepted_replay_ignores_new_config_key_and_budget_policy(
    source_service, fixture, runtime, research_service, monkeypatch
):
    body = command()
    first = await source_service.submit(await context(fixture, runtime), body)
    with runtime.transaction() as tx:
        runtime.repositories.encrypted_llm_config.delete_user(tx, owner_id=fixture[1])
    monkeypatch.delenv("AUDIT_HMAC_SECRET")
    replay = await research_service.submit(await context(fixture, runtime), body)
    assert (
        not replay.created and replay.record.assignment_id == first.record.assignment_id
    )
    assert totals(runtime, fixture[1]) == (1, 1, 1)
    assert len(fixture[-1]) == 1


async def test_cancelled_capture_cannot_later_accept(
    research_service, fixture, runtime, monkeypatch
):
    entered = asyncio.Event()

    async def capture(*args):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(
        research_service.research_preflight.config_store, "capture_user", capture
    )
    selected = await context(fixture, runtime)
    task = asyncio.create_task(
        research_service.submit(selected, research_command(research_service))
    )
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    no_acceptance(runtime, fixture[1])


@pytest.mark.parametrize("value", [True, {}, lambda: None])
async def test_only_exact_preflight_capability_is_accepted(source_service, value):
    with pytest.raises(TypeError, match="research_preflight"):
        WorkSubmitService(
            source_service.assignments,
            source_service.audit,
            source_service.sessions,
            research_preflight=value,
        )
    with pytest.raises(TypeError, match="USER configuration store"):
        FixedResearchPreflight(value)


async def test_foreign_runtime_and_prepared_owner_are_refused(
    research_service, fixture, runtime
):
    service = research_service
    selected = await context(fixture, runtime)
    definition = SimpleNamespace(
        limits=json.loads(research_command(service))["limits"],
        source={
            "profile": "public_page",
            "agent_id": "web-research-1",
            "tool_name": "fetch_page",
        },
        allowed_tools=("web-research-1:fetch_page",),
        consented_scopes=("tools:read",),
    )
    source = service.assignments.tool_bound("web-research-1:fetch_page")
    with pytest.raises(AssignmentError, match="work_research_profile_unavailable"):
        await service.research_preflight.prepare(
            owner_id=fixture[1],
            runtime=object(),
            definition=definition,
            source_bound=source,
        )
    prepared = await service.research_preflight.prepare(
        owner_id=fixture[1], runtime=runtime, definition=definition, source_bound=source
    )
    with runtime.transaction() as tx:
        for owner, value in (("other-owner", prepared), (selected.owner_id, None)):
            with pytest.raises(
                AssignmentError, match="work_research_profile_unavailable"
            ):
                service.research_preflight.assert_current(
                    tx, runtime=runtime, owner_id=owner, prepared=value
                )
    no_acceptance(runtime, fixture[1])


async def test_selected_config_stays_locked_through_atomic_audit(
    research_service, fixture, runtime, monkeypatch
):
    service = research_service
    attempting, finished = threading.Event(), threading.Event()
    writer_codes = []

    def writer():
        assert attempting.wait(5)
        try:
            with runtime.transaction() as tx:
                tx.execute("SET LOCAL lock_timeout = '100ms'")
                runtime.repositories.encrypted_llm_config.delete_user(
                    tx, owner_id=fixture[1]
                )
        except Exception as error:
            writer_codes.append(getattr(error, "pgcode", None))
        finally:
            finished.set()

    task = asyncio.create_task(asyncio.to_thread(writer))
    original = service.audit.insert_in_transaction

    def audit(*args, **kwargs):
        attempting.set()
        assert finished.wait(5), (
            "own config writer did not finish while audit remained open"
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(service.audit, "insert_in_transaction", audit)
    try:
        accepted = await service.submit(
            await context(fixture, runtime), research_command(service)
        )
    finally:
        attempting.set()
        await task
    assert accepted.created and writer_codes == ["55P03"]
    assert totals(runtime, fixture[1]) == (1, 1, 1)


async def test_lost_accept_ack_replays_without_second_preflight(
    research_service, fixture, runtime, monkeypatch
):
    service = research_service
    original = service.store.transaction
    lost = []

    async def transaction(callback, **kwargs):
        value = await original(callback, **kwargs)
        if getattr(value, "created", False):
            lost.append(True)
            monkeypatch.delenv("AUDIT_HMAC_SECRET")
            raise AssignmentError("assignment_transaction_unavailable", 503)
        return value

    monkeypatch.setattr(service.store, "transaction", transaction)
    accepted = await service.submit(
        await context(fixture, runtime), research_command(service)
    )
    assert not accepted.created and lost == [True]
    assert totals(runtime, fixture[1]) == (1, 1, 1) and len(fixture[-1]) == 1


async def test_search_scope_cannot_admit_the_exact_read_profile(
    research_service, fixture, runtime
):
    service = research_service
    permissions = service.assignments.orch.tool_permissions
    permissions.register_tool_scopes("web-research-1", {"fetch_page": "tools:search"})
    permissions.set_agent_scopes(fixture[1], "web-research-1", {"tools:search": True})
    with pytest.raises(AssignmentError, match="work_research_profile_unavailable"):
        await service.submit(await context(fixture, runtime), research_command(service))
    no_acceptance(runtime, fixture[1])


async def test_revoke_committed_during_config_wait_refuses_acceptance(
    research_service, fixture, runtime, monkeypatch
):
    service = research_service
    config = runtime.repositories.encrypted_llm_config
    original = config.get_user_for_update
    locked, requesting = threading.Event(), threading.Event()
    shared = {}

    def get_user(tx, *, owner_id):
        shared["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
        requesting.set()
        return original(tx, owner_id=owner_id)

    monkeypatch.setattr(config, "get_user_for_update", get_user)

    def writer():
        with runtime.transaction() as tx:
            original(tx, owner_id=fixture[1])
            blocker = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            locked.set()
            assert requesting.wait(5), "acceptance did not reach current config lock"
            end = time.monotonic() + 0.08
            while time.monotonic() < end:
                if tx.fetch_one(
                    "SELECT %s = ANY(pg_blocking_pids(%s)) AS waiting",
                    (blocker, shared["pid"]),
                )["waiting"]:
                    shared["observed_wait"] = True
                    break
                time.sleep(0.001)
            assert shared.get("observed_wait"), (
                "actual config row-lock wait not observed"
            )
            runtime.repositories.tool_policy_state.set_scopes(
                tx,
                owner_id=fixture[1],
                agent_id="web-research-1",
                scopes={"tools:read": False},
                updated_at=int(time.time() * 1000),
            )

    worker = asyncio.create_task(asyncio.to_thread(writer))
    try:
        assert await asyncio.to_thread(locked.wait, 5)
        with pytest.raises(AssignmentError, match="assignment_scope_revoked"):
            await service.submit(
                await context(fixture, runtime), research_command(service)
            )
    finally:
        await worker
    assert shared["observed_wait"]
    no_acceptance(runtime, fixture[1])
