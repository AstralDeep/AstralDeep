"""Feature 054 — orchestrator credential-resolution tests.

Like the pre-054 version of this file, this bypasses full Orchestrator
construction (DB, agents, sockets) and exercises the REAL resolution
methods — ``Orchestrator._llm_context_user_id``,
``Orchestrator._resolve_llm_client_for`` and
``Orchestrator._drain_llm_discard_notes`` — bound onto a minimal stub via
``types.MethodType``, backed by a real ``UserLLMConfigStore`` over the
fake DB. Covers:

* user socket → the caller's OWN persisted record (source USER).
* ``websocket=None`` (background) → the SYSTEM record.
* scheduled-turn ``VirtualWebSocket`` → the SYSTEM record.
* no cross-fallback in either direction (gate / honest skip).
* credential_source audit values "user" / "system".
* the undecryptable-row drain emits the discarded_undecryptable audit.
"""
from __future__ import annotations

import types

import pytest

from orchestrator.async_tasks import (
    BackgroundTask,
    DurableUserTurnWebSocket,
    VirtualWebSocket,
)
from orchestrator.orchestrator import Orchestrator

from llm_config import CredentialSource, LLMUnavailable, build_llm_client
from llm_config.audit_events import record_llm_call
from llm_config.ws_handlers import handle_llm_config_set

ALICE = "alice-sub"
ALICE_KEY = "sk-alice-1234567890abcdefgh"
SYSTEM_KEY = "sk-system-1234567890abcdefg"


class _FakeWS:
    """Stands in for a live user WebSocket (hashable, identity-keyed)."""


def _make_stub(store, recorder):
    """Minimal orchestrator stub carrying exactly the state the real
    resolution methods touch, with the REAL unbound methods attached."""
    stub = types.SimpleNamespace()
    stub.ui_sessions = {}
    stub._llm_store = store
    stub._build_llm_client = build_llm_client
    stub._CredentialSource = CredentialSource
    stub.audit_recorder = recorder
    stub._llm_context_user_id = types.MethodType(
        Orchestrator._llm_context_user_id, stub)
    stub._resolve_llm_client_for = types.MethodType(
        Orchestrator._resolve_llm_client_for, stub)
    stub._drain_llm_discard_notes = types.MethodType(
        Orchestrator._drain_llm_discard_notes, stub)
    return stub


def _register(stub, ws, sub):
    stub.ui_sessions[ws] = {"sub": sub, "preferred_username": sub}


def _virtual_ws():
    return VirtualWebSocket(
        BackgroundTask(task_id="t1", chat_id="c1", user_id=ALICE))


def _seed_alice(store):
    store.set_sync(ALICE, provider="custom",
                   base_url="https://alice.example/v1",
                   model="alice-model", api_key=ALICE_KEY)


def _seed_system(store):
    store.set_system_sync(provider="openai",
                          base_url="https://api.openai.com/v1",
                          model="gpt-4o", api_key=SYSTEM_KEY,
                          updated_by="admin")


# ============================================================================
# _llm_context_user_id — which context owns the call
# ============================================================================


class TestLLMContextUserId:
    def test_none_websocket_is_system_context(self, store, fake_recorder):
        stub = _make_stub(store, fake_recorder)
        assert stub._llm_context_user_id(None) is None

    def test_virtual_websocket_is_system_context(self, store, fake_recorder):
        stub = _make_stub(store, fake_recorder)
        assert stub._llm_context_user_id(_virtual_ws()) is None

    def test_user_socket_maps_to_its_sub_claim(self, store, fake_recorder):
        stub = _make_stub(store, fake_recorder)
        ws = _FakeWS()
        _register(stub, ws, ALICE)
        assert stub._llm_context_user_id(ws) == ALICE


# ============================================================================
# _resolve_llm_client_for — the four contexts
# ============================================================================


class TestUserSocketResolution:
    async def test_user_socket_resolves_own_record(self, store, fake_recorder):
        stub = _make_stub(store, fake_recorder)
        _seed_alice(store)
        _seed_system(store)  # present but must NOT be used
        ws = _FakeWS()
        _register(stub, ws, ALICE)
        client, source, resolved = await stub._resolve_llm_client_for(ws)
        assert source is CredentialSource.USER
        assert source.value == "user"
        assert client.api_key == ALICE_KEY
        assert resolved.base_url == "https://alice.example/v1"
        assert resolved.model == "alice-model"

    async def test_unconfigured_user_is_gated_no_system_fallback(
            self, store, fake_recorder):
        """FR-019: user calls NEVER fall back to the system credential."""
        stub = _make_stub(store, fake_recorder)
        _seed_system(store)  # the system record exists...
        ws = _FakeWS()
        _register(stub, ws, "bob-sub")  # ...but bob has no personal record
        with pytest.raises(LLMUnavailable, match="provider setup"):
            await stub._resolve_llm_client_for(ws)

    async def test_user_never_resolves_another_users_record(
            self, store, fake_recorder):
        stub = _make_stub(store, fake_recorder)
        _seed_alice(store)
        ws_bob = _FakeWS()
        _register(stub, ws_bob, "bob-sub")
        with pytest.raises(LLMUnavailable):
            await stub._resolve_llm_client_for(ws_bob)

    async def test_successful_save_immediately_governs_typed_and_voice_calls(
        self,
        monkeypatch,
        store,
        fake_recorder,
        safe_send,
    ):
        """The tested USER model wins over stale cache and operator tiers."""

        store.set_sync(
            ALICE,
            provider="custom",
            base_url="https://old.example/v1",
            model="old-model",
            api_key=ALICE_KEY,
        )

        async def probe(**_kwargs):
            return True, None, None

        monkeypatch.setattr(
            "llm_config.ws_handlers.probe_chat_completion",
            probe,
        )
        saved = await handle_llm_config_set(
            safe_send=safe_send,
            websocket=object(),
            config={
                "provider": "custom",
                "base_url": "https://new.example/v1",
                "model": "new-model",
                "api_key": ALICE_KEY,
            },
            actor_user_id=ALICE,
            auth_principal=ALICE,
            store=store,
            recorder=fake_recorder,
        )
        assert saved is True
        assert (await store.get(ALICE)).model == "new-model"
        _seed_system(store)

        typed = _FakeWS()
        voice = DurableUserTurnWebSocket(typed, user_id=ALICE)
        runtime = _make_stub(store, fake_recorder)
        _register(runtime, typed, ALICE)
        _register(runtime, voice, ALICE)
        runtime._LLMUnavailable = LLMUnavailable
        runtime._llm_audit_principals = types.MethodType(
            Orchestrator._llm_audit_principals,
            runtime,
        )
        runtime._llm_unsupported_params = {}
        runtime.llm_reasoning_effort = None
        runtime._valid_reasoning_effort = lambda _value: None
        runtime.MAX_RETRIES = 1
        runtime.rote = types.SimpleNamespace(
            get_profile=lambda _socket: types.SimpleNamespace(
                device_type=types.SimpleNamespace(value="browser"),
                capabilities={},
            )
        )

        requested_models: list[str] = []
        built_models: list[str] = []

        class Completions:
            def create(self, **kwargs):
                requested_models.append(kwargs["model"])
                return types.SimpleNamespace(
                    choices=[types.SimpleNamespace(
                        message=types.SimpleNamespace(content="done")
                    )],
                    usage=types.SimpleNamespace(total_tokens=1),
                )

        def build(config, source):
            built_models.append(config.model)
            return (
                types.SimpleNamespace(
                    chat=types.SimpleNamespace(completions=Completions())
                ),
                source,
                config,
            )

        async def record(*_args, **_kwargs):
            return None

        async def usage(*_args, **_kwargs):
            return None

        runtime._build_llm_client = build
        runtime._record_llm_call = record
        runtime._emit_llm_usage_report = usage
        monkeypatch.setenv("FF_LLM_STREAMING", "false")
        monkeypatch.setenv("FF_MODEL_ROUTER", "true")
        monkeypatch.setenv(
            "MODEL_TIERS",
            '{"small":"operator-small","medium":"old-model",'
            '"large":"operator-large"}',
        )

        results = [
            await Orchestrator._call_llm(
                runtime,
                socket,
                [{"role": "user", "content": "hello"}],
                feature="tool_dispatch",
            )
            for socket in (typed, voice)
        ]
        system_result = await Orchestrator._call_llm(
            runtime,
            None,
            [{"role": "user", "content": "background work"}],
            feature="tool_dispatch",
        )

        assert [message.content for message, _usage in results] == [
            "done",
            "done",
        ]
        assert system_result[0].content == "done"
        assert built_models == ["new-model", "new-model", "gpt-4o"]
        assert requested_models == ["new-model", "new-model", "old-model"]


class TestSystemContextResolution:
    async def test_websocket_none_resolves_system_record(
            self, store, fake_recorder):
        stub = _make_stub(store, fake_recorder)
        _seed_alice(store)  # a configured user must NOT be borrowed
        _seed_system(store)
        client, source, resolved = await stub._resolve_llm_client_for(None)
        assert source is CredentialSource.SYSTEM
        assert source.value == "system"
        assert client.api_key == SYSTEM_KEY
        assert resolved.base_url == "https://api.openai.com/v1"
        assert resolved.base_url != "https://alice.example/v1"

    async def test_virtual_websocket_resolves_system_record(
            self, store, fake_recorder):
        """Scheduled turns run a user's chat but bill the system
        credential by explicit owner decision (FR-019)."""
        stub = _make_stub(store, fake_recorder)
        _seed_alice(store)
        _seed_system(store)
        client, source, resolved = await stub._resolve_llm_client_for(
            _virtual_ws())
        assert source is CredentialSource.SYSTEM
        assert client.api_key == SYSTEM_KEY

    async def test_no_system_record_degrades_honestly_no_user_fallback(
            self, store, fake_recorder):
        stub = _make_stub(store, fake_recorder)
        _seed_alice(store)  # users configured, system absent
        with pytest.raises(LLMUnavailable, match="system credential"):
            await stub._resolve_llm_client_for(None)
        with pytest.raises(LLMUnavailable):
            await stub._resolve_llm_client_for(_virtual_ws())


# ============================================================================
# credential_source audit values
# ============================================================================


class TestCredentialSourceAudit:
    async def test_user_and_system_resolutions_audit_as_user_and_system(
            self, store, fake_recorder):
        stub = _make_stub(store, fake_recorder)
        _seed_alice(store)
        _seed_system(store)
        ws = _FakeWS()
        _register(stub, ws, ALICE)

        for websocket, expected in ((ws, "user"), (None, "system")):
            _, source, resolved = await stub._resolve_llm_client_for(websocket)
            await record_llm_call(
                fake_recorder,
                actor_user_id=ALICE if expected == "user" else "system",
                auth_principal=ALICE if expected == "user" else "system",
                feature="tool_dispatch",
                credential_source=source,
                resolved=resolved,
                total_tokens=1,
                outcome="success",
            )
        sources = [
            c.args[0].inputs_meta["credential_source"]
            for c in fake_recorder.record.await_args_list
        ]
        assert sources == ["user", "system"]


# ============================================================================
# Undecryptable-record drain (FR-010)
# ============================================================================


class TestUndecryptableDrain:
    async def test_resolution_discards_audits_and_regates(
            self, store, fake_db, fake_recorder):
        from cryptography.fernet import Fernet
        stub = _make_stub(store, fake_recorder)
        wrong = Fernet(Fernet.generate_key())
        fake_db.users[ALICE] = {
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "api_key_enc": wrong.encrypt(b"sk-rotated-away").decode(),
            "updated_at": 1.0,
        }
        ws = _FakeWS()
        _register(stub, ws, ALICE)

        with pytest.raises(LLMUnavailable):
            await stub._resolve_llm_client_for(ws)

        # The unusable row was deleted (re-gate, FR-010)...
        assert ALICE not in fake_db.users
        # ...and the drain emitted the discarded_undecryptable audit.
        events = [c.args[0] for c in fake_recorder.record.await_args_list]
        assert len(events) == 1
        assert events[0].event_class == "llm_config_change"
        assert events[0].inputs_meta["action"] == "discarded_undecryptable"
        assert events[0].inputs_meta["scope"] == "user"
        assert events[0].actor_user_id == ALICE
