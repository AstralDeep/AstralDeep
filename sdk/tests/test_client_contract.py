"""AstralClient/AsyncAstralClient against the local fake MCP server.

No network access, no real Deep backend — see ``sdk/tests/fake_server.py``
for exactly what wire behavior is emulated. Genuine end-to-end conformance
against the REAL server lives in
``backend/tests/test_framework_conformance_088.py``.
"""
from __future__ import annotations

import uuid

import pytest

from astral_sdk import AstralClient, AsyncAstralClient
from astral_sdk.errors import AstralAuthError, AstralConflictError, RetryExhaustedError
from astral_sdk.models import RetryPolicy
from tests.fake_server import FakeAstralState


def test_submit_then_get_round_trips(fake_server):
    client = AstralClient(fake_server.base_url, fake_server.state.valid_token)
    try:
        key = str(uuid.uuid4())
        op = client.submit_operation(idempotency_key=key, name="A note", instructions="Write it.")
        assert op.created is True
        assert op.title == "A note"
        assert op.disposition == "queued"
        fetched = client.get_operation(op.id)
        assert fetched.id == op.id
        assert fetched.created is None  # get never claims "created"
    finally:
        client.close()


def test_submit_replay_with_same_key_is_not_created_again(fake_server):
    with AstralClient(fake_server.base_url, fake_server.state.valid_token) as client:
        key = str(uuid.uuid4())
        first = client.submit_operation(idempotency_key=key, name="A", instructions="B")
        second = client.submit_operation(idempotency_key=key, name="A", instructions="B")
        assert first.id == second.id
        assert first.created is True
        assert second.created is False


def test_list_operations(fake_server):
    with AstralClient(fake_server.base_url, fake_server.state.valid_token) as client:
        client.submit_operation(idempotency_key=str(uuid.uuid4()), name="A", instructions="B")
        result = client.list_operations()
        assert len(result.operations) == 1
        assert result.page_full is False


def test_poll_reports_changed_only_when_revision_moved(fake_server):
    with AstralClient(fake_server.base_url, fake_server.state.valid_token) as client:
        op = client.submit_operation(idempotency_key=str(uuid.uuid4()), name="A", instructions="B")
        unchanged = client.poll_operation(op.id, after_revision=op.revision)
        assert unchanged.changed is False
        assert unchanged.operation is None
        changed = client.poll_operation(op.id, after_revision=op.revision - 1)
        assert changed.changed is True
        assert changed.operation is not None


def test_cancel_applies_once_and_a_stale_revision_conflicts(fake_server):
    with AstralClient(fake_server.base_url, fake_server.state.valid_token) as client:
        op = client.submit_operation(idempotency_key=str(uuid.uuid4()), name="A", instructions="B")
        first = client.cancel_operation(op.id, expected_revision=op.revision)
        assert first.applied is True
        assert first.operation.disposition == "cancelled"
        with pytest.raises(AstralConflictError):
            client.cancel_operation(op.id, expected_revision=op.revision)  # now stale


def test_get_artifact(fake_server):
    with AstralClient(fake_server.base_url, fake_server.state.valid_token) as client:
        op = client.submit_operation(idempotency_key=str(uuid.uuid4()), name="A", instructions="B")
        artifact = client.get_artifact(op.id)
        assert artifact.operation_id == op.id
        assert artifact.result == {"text": "synthetic result"}


def test_wait_for_terminal_stops_immediately_once_already_terminal(fake_server):
    with AstralClient(fake_server.base_url, fake_server.state.valid_token) as client:
        op = client.submit_operation(idempotency_key=str(uuid.uuid4()), name="A", instructions="B")
        client.cancel_operation(op.id, expected_revision=op.revision)
        terminal = client.wait_for_terminal(op.id, poll_interval_seconds=0.01, timeout_seconds=5)
        assert terminal.is_terminal
        assert terminal.disposition == "cancelled"


def test_invalid_token_raises_auth_error_with_the_challenge_code(fake_server):
    with AstralClient(fake_server.base_url, "afk_wrong-token") as client:
        with pytest.raises(AstralAuthError) as excinfo:
            client.get_operation("does-not-matter")
        # The JSON body only ever says "MCP authorization failed" — the real
        # machine-readable code rides the WWW-Authenticate challenge header.
        assert excinfo.value.code == "invalid_token"
        assert excinfo.value.status_code == 401


def test_missing_scope_raises_auth_error(fake_server_factory):
    server = fake_server_factory(FakeAstralState(scopes=frozenset({"operations.read"})))
    with AstralClient(server.base_url, server.state.valid_token) as client:
        with pytest.raises(AstralAuthError):
            client.submit_operation(idempotency_key="k", name="A", instructions="B")


def test_transient_failures_are_retried_and_then_succeed(fake_server):
    fake_server.state.fail_next_n = 2
    policy = RetryPolicy(max_attempts=5, base_delay_seconds=0.001, max_delay_seconds=0.01, jitter_seconds=0.0)
    with AstralClient(fake_server.base_url, fake_server.state.valid_token, retry_policy=policy) as client:
        op = client.submit_operation(idempotency_key=str(uuid.uuid4()), name="A", instructions="B")
        assert op.created is True


def test_retries_are_exhausted_when_the_server_stays_down(fake_server):
    fake_server.state.fail_next_n = 100
    policy = RetryPolicy(max_attempts=2, base_delay_seconds=0.001, max_delay_seconds=0.01, jitter_seconds=0.0)
    with AstralClient(fake_server.base_url, fake_server.state.valid_token, retry_policy=policy) as client:
        with pytest.raises(RetryExhaustedError):
            client.get_operation("anything")


def test_operation_not_found(fake_server):
    from astral_sdk.errors import AstralHTTPError

    with AstralClient(fake_server.base_url, fake_server.state.valid_token) as client:
        with pytest.raises(AstralHTTPError) as excinfo:
            client.get_operation(str(uuid.uuid4()))
        assert excinfo.value.code == "work_not_found"


def test_empty_token_is_rejected_before_any_request():
    with pytest.raises(ValueError):
        AstralClient("http://example.invalid", "")


# -- async twin -------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_client_submit_and_get(fake_server):
    client = AsyncAstralClient(fake_server.base_url, fake_server.state.valid_token)
    try:
        key = str(uuid.uuid4())
        op = await client.submit_operation(idempotency_key=key, name="A", instructions="B")
        assert op.created is True
        fetched = await client.get_operation(op.id)
        assert fetched.id == op.id
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_async_client_context_manager(fake_server):
    async with AsyncAstralClient(fake_server.base_url, fake_server.state.valid_token) as client:
        result = await client.list_operations()
        assert result.operations == ()


@pytest.mark.asyncio
async def test_async_client_wait_for_terminal(fake_server):
    async with AsyncAstralClient(fake_server.base_url, fake_server.state.valid_token) as client:
        op = await client.submit_operation(idempotency_key=str(uuid.uuid4()), name="A", instructions="B")
        await client.pause_operation(op.id, expected_revision=op.revision)
        # pausing is not terminal; cancel it next and confirm wait_for_terminal returns.
        paused = await client.get_operation(op.id)
        await client.cancel_operation(op.id, expected_revision=paused.revision)
        terminal = await client.wait_for_terminal(op.id, poll_interval_seconds=0.01, timeout_seconds=5)
        assert terminal.is_terminal
