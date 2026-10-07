"""Verifies encrypted provider persistence before acknowledgement and a bounded connection check.
Post-save failures remain warnings, while malformed inputs and failed writes cannot report a save.
"""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from llm_config import ws_handlers as handlers
from llm_config.tests.test_operation_status_060 import _accepted_claim, _coordinator
from orchestrator.work_admission import OperationState


USER = "saved-provider-owner"
KEY = "sk-saved-provider-secret-1234567890"
CONFIG = {"provider": "openai", "api_key": KEY, "model": "gpt-4o-mini"}


def sent(safe_send):
    return [json.loads(call.args[1]) for call in safe_send.await_args_list]


async def save(store, fake_recorder, safe_send, **kwargs):
    return await handlers.handle_llm_config_set(safe_send=safe_send, websocket=object(),
        config=CONFIG, actor_user_id=USER, auth_principal=USER, store=store,
        recorder=fake_recorder, **kwargs)


@pytest.mark.parametrize("error_class", ["auth_failed", "model_not_found", "contract_violation",
    "transport_error", "provider_unavailable", "other", None])
async def test_failed_post_save_probe_keeps_encrypted_settings_and_acks_first(
        store, fake_db, fake_recorder, safe_send, monkeypatch, error_class):
    async def probe(**kwargs):
        assert (await store.get(USER)).api_key == KEY
        assert sent(safe_send) == [{"type": "llm_config_ack", "ok": True}]
        return False, error_class, "private upstream " + KEY

    monkeypatch.setattr(handlers, "probe_chat_completion", probe)
    assert await save(store, fake_recorder, safe_send) is True
    assert KEY not in fake_db.users[USER]["api_key_enc"]
    ack, warning = sent(safe_send)
    assert ack["ok"] is True and warning["type"] == "notification"
    assert warning["title"] == "Provider settings saved" and warning["level"] == "warning"
    assert "Connection test failed" in warning["body"] and "saved settings remain" in warning["body"]
    assert KEY not in json.dumps(warning) and "private upstream" not in json.dumps(warning)
    events = [call.args[0] for call in fake_recorder.record.await_args_list]
    assert [event.inputs_meta["action"] for event in events] == ["created", "tested"]
    assert events[-1].outcome == "failure"


@pytest.mark.parametrize("error", [TimeoutError("private timeout"), RuntimeError("private provider " + KEY)])
async def test_post_save_probe_exception_is_a_warning_without_rejecting_storage(
        store, fake_recorder, safe_send, monkeypatch, error):
    async def probe(**kwargs):
        raise error

    monkeypatch.setattr(handlers, "probe_chat_completion", probe)
    assert await save(store, fake_recorder, safe_send) is True
    assert (await store.get(USER)).api_key == KEY
    assert sent(safe_send)[-1]["level"] == "warning"
    assert "private" not in json.dumps(sent(safe_send))


async def test_direct_save_unlocks_at_acknowledgement_before_probe(
        store, fake_recorder, safe_send, monkeypatch):
    unlocked = []

    async def after_save():
        assert sent(safe_send)[0]["ok"] is True
        assert await store.get(USER) is not None
        unlocked.append(True)
        return True

    async def probe(**kwargs):
        assert unlocked == [True]
        return True, None, None

    monkeypatch.setattr(handlers, "probe_chat_completion", probe)
    assert await save(store, fake_recorder, safe_send, after_save=after_save) is True
    assert len(sent(safe_send)) == 1


async def test_ack_delivery_failure_cannot_reclassify_committed_settings(
        store, fake_recorder, monkeypatch):
    deliver = AsyncMock(side_effect=RuntimeError("closed transport"))
    probe = AsyncMock(return_value=(True, None, None))
    monkeypatch.setattr(handlers, "probe_chat_completion", probe)
    assert await save(store, fake_recorder, deliver) is True
    assert (await store.get(USER)).api_key == KEY
    probe.assert_awaited_once()


async def test_unlock_projection_failure_still_runs_post_save_probe(
        store, fake_recorder, safe_send, monkeypatch):
    probe = AsyncMock(return_value=(True, None, None))
    monkeypatch.setattr(handlers, "probe_chat_completion", probe)
    assert await save(store, fake_recorder, safe_send,
        after_save=AsyncMock(side_effect=RuntimeError("projection unavailable"))) is True
    probe.assert_awaited_once()
    assert sent(safe_send)[0]["ok"] is True


@pytest.mark.parametrize("error", [ValueError("invalid store input"), RuntimeError("store unavailable")])
async def test_storage_failure_never_acknowledges_or_probes(
        store, fake_recorder, safe_send, monkeypatch, error):
    probe = AsyncMock()
    monkeypatch.setattr(handlers, "probe_chat_completion", probe)
    monkeypatch.setattr(store, "set", AsyncMock(side_effect=error))
    if isinstance(error, ValueError):
        assert await save(store, fake_recorder, safe_send) is False
    else:
        with pytest.raises(RuntimeError):
            await save(store, fake_recorder, safe_send)
    assert await store.get(USER) is None
    assert not any(frame["type"] == "llm_config_ack" for frame in sent(safe_send))
    probe.assert_not_awaited()


@pytest.mark.parametrize("field", ["provider", "api_key", "model", "base_url"])
@pytest.mark.parametrize("value", [True, 7, [], {}])
def test_non_text_settings_are_structural_validation_errors(field, value):
    _, errors = handlers.validate_config_submission(dict(CONFIG, **{field: value}))
    assert field in errors


async def test_fenced_save_defers_probe_until_outer_acknowledgement_and_stays_completed(
        store, fake_db, fake_recorder, safe_send, monkeypatch):
    coordinator = _coordinator()
    owner, _, claim = _accepted_claim(coordinator)
    probe = AsyncMock(return_value=(False, "auth_failed", KEY))
    monkeypatch.setattr(handlers, "probe_chat_completion", probe)
    runtime = handlers.LLMConfigOperationContext(coordinator=coordinator, fence=claim.fence,
        deadline_at_monotonic=time.monotonic() + 10,
        deadline_at_utc=datetime.now(UTC) + timedelta(seconds=10),
        emit_phase=AsyncMock(), unlock_after_save=AsyncMock())
    with handlers.active_llm_config_operation(runtime):
        assert await handlers.handle_llm_config_set(safe_send=safe_send, websocket=object(),
            config=CONFIG, actor_user_id=owner.owner_user_id, auth_principal=owner.owner_user_id,
            store=store, recorder=fake_recorder) is True
    probe.assert_not_awaited()
    safe_send.assert_not_awaited()
    assert runtime.failure is None and runtime.completed_operation.state is OperationState.COMPLETED
    assert KEY not in fake_db.users[owner.owner_user_id]["api_key_enc"]
    assert callable(runtime.connection_check)
    await runtime.connection_check()
    probe.assert_awaited_once()
    assert sent(safe_send)[0]["level"] == "warning" and runtime.failure is None
    assert coordinator.query_operation(owner=owner, operation_id=claim.fence.operation_id).state is OperationState.COMPLETED


async def test_post_save_check_has_a_bound_even_when_provider_hook_never_returns(
        store, fake_recorder, safe_send, monkeypatch):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def probe(**kwargs):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(handlers, "probe_chat_completion", probe)
    monkeypatch.setattr(handlers, "PROBE_TIMEOUT_SECONDS", 0.01, raising=False)
    assert await asyncio.wait_for(save(store, fake_recorder, safe_send), 2) is True
    assert entered.is_set() and cancelled.is_set()
    assert sent(safe_send)[-1]["level"] == "warning"


async def test_post_save_audit_and_warning_delivery_failures_cannot_undo_storage(
        store, fake_recorder, safe_send, monkeypatch):
    fake_recorder.record.side_effect = RuntimeError("audit unavailable")
    probe = AsyncMock(return_value=(False, "transport_error", KEY))
    monkeypatch.setattr(handlers, "probe_chat_completion", probe)
    safe_send.side_effect = [None, RuntimeError("closed warning transport")]
    assert await save(store, fake_recorder, safe_send) is True
    assert (await store.get(USER)).api_key == KEY
    probe.assert_awaited_once()
    assert sent(safe_send)[0]["ok"] is True


async def test_post_save_audit_waits_are_bounded_and_still_deliver_probe_warning(
        store, fake_recorder, safe_send, monkeypatch):
    cancelled = []

    async def held(event):
        try:
            await asyncio.Future()
        finally:
            cancelled.append(event.action_type)

    fake_recorder.record.side_effect = held
    monkeypatch.setattr(handlers, "PROBE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(handlers, "probe_chat_completion", AsyncMock(return_value=(False, "transport_error", None)))
    assert await asyncio.wait_for(save(store, fake_recorder, safe_send), 2) is True
    assert cancelled == ["llm_config.created", "llm_config.tested"]
    assert sent(safe_send)[-1]["level"] == "warning"
