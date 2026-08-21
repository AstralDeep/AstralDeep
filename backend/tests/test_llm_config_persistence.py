"""Feature 054 — T035: persistence + cross-socket semantics of the per-user
LLM configuration store.

Real Postgres-backed ``UserLLMConfigStore`` via a real orchestrator:

* configuration is keyed by USER (survives socket disconnect — the gate
  marker is per-socket, the record is not);
* cross-user isolation (B never resolves A's record);
* an undecryptable row is discarded, audited, and treated as unconfigured
  (FR-010);
* clearing re-gates every one of the user's connected sockets immediately;
* partial submissions are rejected per-field with nothing stored;
* blank-key-keeps-saved-key semantics at the surface level.

References: specs/054-byo-llm-setup/spec.md FR-005/FR-007/FR-009/FR-010.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator import llm_gate  # noqa: E402

SECRET = "sk-supersecret-persist-key-12345678901234"


class FakeRecorder:
    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)


def _uid() -> str:
    return f"persist054-{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="module")
def orch_module():
    from orchestrator.orchestrator import Orchestrator

    o = Orchestrator()
    try:
        yield o
    finally:
        asyncio.run(o._close_started_services())


@pytest.fixture
def orch(orch_module):
    o = orch_module
    o.ui_sessions = {}
    o._ws_llm_gated = {}
    o._ws_active_chat = {}
    o._ws_welcome = {}
    o._ff_llm_first_run = True
    sent = []

    async def _capture(ws, data):
        sent.append((ws, data))
        return True

    o._safe_send = _capture
    o.sent = sent
    o.send_ui_render = AsyncMock()
    o._record_llm_unconfigured = AsyncMock()
    o.audit_recorder = FakeRecorder()
    return o


def _register(orch, uid, device="browser"):
    ws = MagicMock()
    orch.ui_sessions[ws] = {
        "sub": uid,
        "preferred_username": f"{uid}@example",
        "realm_access": {"roles": ["user"]},
    }
    orch.rote.register_device(ws, {"device_type": device})
    return ws


async def _seed(orch, uid, base_url="https://api.example.com/v1",
                model="gpt-x", api_key=SECRET, provider="custom"):
    return await orch._llm_store.set(
        uid, provider=provider, base_url=base_url, model=model, api_key=api_key)


def _get_encrypted_user_record(orch, uid):
    plane = orch.runtime_composition.plane
    repository = plane.repositories.encrypted_llm_config
    with plane.runtime.transaction() as transaction:
        return repository.get_user(transaction, owner_id=uid)


def _replace_encrypted_user_ciphertext(orch, uid, ciphertext):
    plane = orch.runtime_composition.plane
    repository = plane.repositories.encrypted_llm_config
    with plane.runtime.transaction() as transaction:
        current = repository.get_user(transaction, owner_id=uid)
        assert current is not None
        return repository.upsert_user(
            transaction,
            owner_id=uid,
            provider=current.provider,
            base_url=current.base_url,
            model=current.model,
            api_key_ciphertext=ciphertext,
        )


def _mandatory_frames(orch):
    out = []
    for ws, data in orch.sent:
        try:
            f = json.loads(data)
        except (TypeError, ValueError):
            continue
        if f.get("type") == "chrome_render" and 'data-mandatory="1"' in f.get("html", ""):
            out.append((ws, f))
    return out


# ---------------------------------------------------------------------------
# Survival + isolation
# ---------------------------------------------------------------------------

async def test_config_survives_socket_disconnect(orch):
    uid = _uid()
    ws = _register(orch, uid)
    await _seed(orch, uid)
    try:
        orch._ws_llm_gated[id(ws)] = True  # pretend this socket had been gated

        # Disconnect cleanup clears ONLY the per-socket gate marker.
        llm_gate.clear_socket(orch, ws)
        assert id(ws) not in orch._ws_llm_gated

        # The persisted record is untouched — a fresh connect is configured.
        cfg = await orch._llm_store.get(uid)
        assert cfg is not None and cfg.api_key == SECRET
        assert await orch.llm_configured_for(uid) is True
    finally:
        await orch._llm_store.clear(uid)


async def test_cross_user_isolation(orch):
    uid_a, uid_b = _uid(), _uid()
    await _seed(orch, uid_a, base_url="https://a.example.com/v1", model="model-a")
    try:
        # B has no record and never sees A's.
        assert await orch._llm_store.get(uid_b) is None
        assert await orch.llm_configured_for(uid_b) is False

        ws_b = _register(orch, uid_b)
        with pytest.raises(orch._LLMUnavailable):
            await orch._resolve_llm_client_for(ws_b)

        # A's own resolution still returns A's record (sanity).
        ws_a = _register(orch, uid_a)
        _, source, resolved = await orch._resolve_llm_client_for(ws_a)
        assert source == orch._CredentialSource.USER
        assert resolved.base_url == "https://a.example.com/v1"
        # None of A's key material leaked into B's failure path.
        assert SECRET not in json.dumps([d for _, d in orch.sent])
    finally:
        await orch._llm_store.clear(uid_a)


# ---------------------------------------------------------------------------
# FR-010 — undecryptable row ⇒ discarded + unconfigured
# ---------------------------------------------------------------------------

async def test_undecryptable_row_discarded_and_treated_unconfigured(orch):
    uid = _uid()
    store = orch._llm_store
    await _seed(orch, uid)
    try:
        # The public Plane record exposes only opaque ciphertext, never the
        # decrypted secret held by the product store.
        encrypted = await asyncio.to_thread(
            _get_encrypted_user_record,
            orch,
            uid,
        )
        assert encrypted is not None
        assert encrypted.api_key_ciphertext not in (None, SECRET)
        assert SECRET not in repr(encrypted)

        # Model key rotation/corruption through the typed owner-scoped Plane
        # repository instead of borrowing a connection or issuing raw SQL.
        await asyncio.to_thread(
            _replace_encrypted_user_ciphertext,
            orch,
            uid,
            "not-a-fernet-token",
        )
        store.invalidate(uid)

        # The gate predicate treats the row as absent — no crash.
        assert await orch.llm_configured_for(uid) is False

        # The unusable row was deleted...
        assert await asyncio.to_thread(
            _get_encrypted_user_record,
            orch,
            uid,
        ) is None
        # ...the discard note was drained into an audit event...
        actions = [e.action_type for e in orch.audit_recorder.events]
        assert "llm_config.discarded_undecryptable" in actions
        # ...and the queue is empty (drained, not leaked).
        assert store.pop_discard_note() is None
    finally:
        await orch._llm_store.clear(uid)


# ---------------------------------------------------------------------------
# Clear ⇒ immediate re-gate on every socket
# ---------------------------------------------------------------------------

async def test_clear_regates_every_connected_socket(orch):
    uid = _uid()
    ws1 = _register(orch, uid)
    ws2 = _register(orch, uid)
    await _seed(orch, uid)

    removed = await orch._llm_store.clear(uid)
    assert removed is True
    assert await orch.llm_configured_for(uid) is False

    count = await llm_gate.regate_after_clear(orch, uid)
    assert count == 2
    gated = _mandatory_frames(orch)
    assert {id(ws) for ws, _ in gated} == {id(ws1), id(ws2)}
    assert orch._ws_llm_gated.get(id(ws1)) is True
    assert orch._ws_llm_gated.get(id(ws2)) is True


async def test_clear_regate_skips_watch_sockets(orch):
    uid = _uid()
    _register(orch, uid, device="browser")
    _register(orch, uid, device="watch")

    count = await llm_gate.regate_after_clear(orch, uid)

    assert count == 1  # the watch is never pushed the dialog (FR-017)
    assert len(_mandatory_frames(orch)) == 1


# ---------------------------------------------------------------------------
# Partial submissions — rejected per-field, nothing stored
# ---------------------------------------------------------------------------

def test_validate_config_submission_field_level_errors():
    from llm_config.ws_handlers import validate_config_submission

    # Key-required preset with missing model + key.
    _, errors = validate_config_submission(
        {"provider": "openai", "api_key": "", "model": ""})
    assert set(errors) == {"model", "api_key"}

    # Custom without an endpoint.
    _, errors = validate_config_submission(
        {"provider": "custom", "api_key": "k", "model": "m", "base_url": ""})
    assert "base_url" in errors

    # Non-http(s) endpoint.
    _, errors = validate_config_submission(
        {"provider": "custom", "api_key": "k", "model": "m", "base_url": "ftp://x"})
    assert "base_url" in errors

    # Unknown provider is itself a field error.
    _, errors = validate_config_submission(
        {"provider": "definitely-not-a-provider", "api_key": "k", "model": "m"})
    assert set(errors) == {"provider"}

    # Keyless local-runtime presets permit an empty key.
    fields, errors = validate_config_submission(
        {"provider": "ollama", "api_key": "", "model": "llama3"})
    assert errors == {}
    assert fields["base_url"] == "http://localhost:11434/v1"


async def test_partial_submission_stores_nothing_and_skips_probe(orch, monkeypatch):
    from llm_config.ws_handlers import handle_llm_config_set

    async def probe_must_not_run(**kwargs):
        raise AssertionError("probe must not run on an invalid submission")

    monkeypatch.setattr(
        "llm_config.ws_handlers.probe_chat_completion", probe_must_not_run)
    uid = _uid()
    ws = _register(orch, uid)

    saved = await handle_llm_config_set(
        safe_send=orch._safe_send,
        websocket=ws,
        config={"provider": "openai", "api_key": "", "model": ""},
        actor_user_id=uid,
        auth_principal=f"{uid}@example",
        store=orch._llm_store,
        recorder=orch.audit_recorder,
    )

    assert saved is False
    assert await orch._llm_store.get(uid) is None  # nothing partial stored
    assert orch.audit_recorder.events == []        # no audit before the probe
    errors = [json.loads(d) for _, d in orch.sent
              if json.loads(d).get("code") == "llm_config_invalid"]
    assert errors, "per-field rejection must be sent to the client"
    assert set(errors[-1]["fields"]) == {"model", "api_key"}


# ---------------------------------------------------------------------------
# Blank key keeps the saved key (surface-level write-only semantics)
# ---------------------------------------------------------------------------

async def test_blank_key_resolves_to_saved_key_at_surface(orch):
    from orchestrator.projection_surfaces.llm import _resolve_api_key

    uid = _uid()
    ws = _register(orch, uid)
    await _seed(orch, uid)
    try:
        # Blank submission ⇒ the persisted key, flagged as reused.
        key, used_saved = await _resolve_api_key(orch, ws, uid, {"api_key": ""})
        assert (key, used_saved) == (SECRET, True)

        # A typed key always wins.
        key, used_saved = await _resolve_api_key(
            orch, ws, uid, {"api_key": "sk-brand-new"})
        assert (key, used_saved) == ("sk-brand-new", False)
    finally:
        await orch._llm_store.clear(uid)

    # With no record at all, blank stays blank.
    key, used_saved = await _resolve_api_key(orch, ws, uid, {"api_key": ""})
    assert (key, used_saved) == ("", False)
