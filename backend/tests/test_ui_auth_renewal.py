"""Exercise same-session UI access-token renewal and fenced credential lookup."""

import asyncio
import time
import uuid
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from orchestrator.ui_auth_renewal import (
    RegisteredSubjectTokenResolver,
    UIAuthRenewal,
    _authorization_claims,
    bind_registered_session,
    parse_refresh_frame,
    parse_refresh_message,
    refresh_claims_match,
    ready_for_renewal,
    session_identity,
    subject_auth_snapshot,
    subject_binding,
    capture_subject_token_resolver,
)


def _claims(*, token="old-token", iat=None, exp=None, **changes):
    generation = changes.get(
        "_auth_context_generation",
        changes.get("_auth_connection_generation", str(uuid.uuid4())),
    )
    claims = {
        "sub": "user-1",
        "iss": "https://issuer.example/realms/astral",
        "azp": "astral-frontend",
        "sid": "session-1",
        "iat": int(time.time()) - 30 if iat is None else iat,
        "exp": int(time.time()) + 30 if exp is None else exp,
        "jti": token,
        "scope": ["openid", "profile"],
        "realm_access": {"roles": ["user"]},
        "resource_access": {"astral-frontend": {"roles": ["user"]}},
        "groups": ["members"],
        "_raw_token": token,
        "_client_capabilities": ["ui_auth_refresh_v1"],
        "_auth_connection_generation": generation,
        "_auth_context_generation": generation,
    }
    claims.update(changes)
    return claims


def _context(websocket, generation, **changes):
    return SimpleNamespace(
        websocket=websocket,
        connection_generation=uuid.UUID(generation),
        registered=True,
        closing=False,
        work_registrations_pending=0,
        operations={},
        **changes,
    )


def _frame(generation, token="fresh-token", request_id=None, **changes):
    frame = {
        "type": "refresh_ui_auth",
        "request_id": request_id or str(uuid.uuid4()),
        "connection_generation": generation,
        "token": token,
    }
    frame.update(changes)
    return frame


def test_auth_wait_never_requests_renewal_without_a_current_registered_session():
    async def run():
        from orchestrator.orchestrator import Orchestrator

        runtime = Orchestrator.__new__(Orchestrator)
        runtime.ui_sessions = {}
        runtime._safe_send = AsyncMock()
        for resolver in (
            None,
            SimpleNamespace(is_current=lambda: False),
            SimpleNamespace(is_current=lambda: True, websocket=object()),
        ):
            assert await runtime._await_ui_auth_refresh(object(), resolver) is None
        runtime._safe_send.assert_not_awaited()
    asyncio.run(run())


def test_refresh_replaces_token_on_same_registered_socket_without_resetting_owner_state():
    async def run():
        websocket = object()
        generation = str(uuid.uuid4())
        original = _claims(_auth_connection_generation=generation)
        original["_server_preference"] = {"theme": "dark"}
        sessions = {websocket: original}
        context = _context(websocket, generation)
        contexts = {id(websocket): context}
        fresh = _claims(
            token="fresh-token",
            iat=original["iat"] + 1,
            exp=original["exp"] + 60,
            _raw_token="ignored-claim-value",
            _auth_connection_generation="wrong-claim-value",
        )
        async def verify(token):
            assert token == "fresh-token"
            return fresh
        renewal = UIAuthRenewal(sessions, contexts, verify)
        ack = await renewal.handle(websocket, _frame(generation))
        updated = sessions[websocket]
        assert ack == {
            "type": "ui_auth_refreshed",
            "request_id": ack["request_id"],
            "connection_generation": generation,
            "accepted": True,
        }
        assert updated["_raw_token"] == "fresh-token"
        assert updated["_auth_connection_generation"] == generation
        assert updated["_server_preference"] == {"theme": "dark"}
        assert contexts[id(websocket)] is context
        assert context.connection_generation == uuid.UUID(generation)
        assert "fresh-token" not in str(ack)
    asyncio.run(run())


def test_refresh_refuses_identity_authority_stale_token_and_replayed_request():
    async def run():
        failures = [
            {"sub": "other"},
            {"sid": "other-session"},
            {"iss": "https://wrong.example"},
            {"iss": "https://issuer.example/realms/astral/"},
            {"azp": "other-client"},
            {"aud": ["account", "other-api"]},
            {"scope": ["openid", "profile", "admin"]},
            {"realm_access": {"roles": ["admin"]}},
            {"resource_access": {"astral-frontend": {"roles": ["admin"]}}},
            {"groups": ["administrators"]},
            {"iat": 0},
        ]
        for changes in failures:
            websocket = object()
            generation = str(uuid.uuid4())
            original = _claims(_auth_connection_generation=generation)
            sessions = {websocket: original}
            contexts = {id(websocket): _context(websocket, generation)}
            fresh = _claims(token="new", iat=original["iat"] + 1, exp=original["exp"] + 60)
            fresh.update(changes)
            async def verify(_token):
                return fresh
            ack = await UIAuthRenewal(sessions, contexts, verify).handle(
                websocket, _frame(generation)
            )
            assert ack["accepted"] is False
            assert ack.get("closed_code") in {
                "invalid", "identity_changed", "stale_connection",
                "authority_changed", "session_unavailable",
            }
            assert sessions[websocket] is original
            assert "new" not in str(ack)

        websocket = object()
        generation = str(uuid.uuid4())
        original = _claims(_auth_connection_generation=generation)
        sessions = {websocket: original}
        contexts = {id(websocket): _context(websocket, generation)}
        fresh = _claims(token="new", iat=original["iat"] + 1, exp=original["exp"] + 60)
        async def verify(_token):
            return fresh
        renewal = UIAuthRenewal(sessions, contexts, verify)
        request_id = str(uuid.uuid4())
        assert (await renewal.handle(websocket, _frame(generation, request_id=request_id)))["accepted"]
        replay = await renewal.handle(websocket, _frame(generation, request_id=request_id))
        assert replay["accepted"] is False
        assert replay["closed_code"] == "invalid"
        stale = await renewal.handle(websocket, _frame(generation))
        assert stale["accepted"] is False
    asyncio.run(run())


def test_malformed_preferred_identity_claims_do_not_fall_back():
    assert session_identity(_claims(azp="", client_id="astral-frontend")) is None
    assert session_identity(_claims(sid=None, session_state="session-1")) is None


def test_falsey_malformed_authorization_containers_are_rejected():
    for name, value in (("realm_access", []), ("resource_access", "")):
        claims = _claims(**{name: value})
        assert _authorization_claims(claims) is None
        assert not refresh_claims_match(claims, claims)


def test_refresh_requires_exact_live_registration_and_non_mock_auth():
    async def run():
        websocket = object()
        generation = str(uuid.uuid4())
        original = _claims(_auth_connection_generation=generation)
        sessions = {websocket: original}
        context = _context(websocket, str(uuid.uuid4()))
        contexts = {id(websocket): context}
        called = False
        async def verify(_token):
            nonlocal called
            called = True
            return original
        ack = await UIAuthRenewal(sessions, contexts, verify).handle(
            websocket, _frame(generation)
        )
        assert ack["accepted"] is False
        assert not called
        assert ready_for_renewal(original, generation, mock_auth=True) is False
        assert parse_refresh_frame(_frame(generation, extra=True)) is None
        wrong_socket = object()
        ack = await UIAuthRenewal(sessions, contexts, verify).handle(
            wrong_socket, _frame(generation)
        )
        assert ack["accepted"] is False
        assert not called
    asyncio.run(run())


def test_identity_requires_same_claim_key_for_client_and_session_ids():
    original = _claims()
    assert session_identity(original) is not None
    assert refresh_claims_match(original, {**original, "client_id": "astral-frontend"})
    fallback_client = {key: value for key, value in original.items() if key != "azp"}
    fallback_client["client_id"] = "astral-frontend"
    assert refresh_claims_match(fallback_client, dict(fallback_client))
    fallback_session = {key: value for key, value in original.items() if key != "sid"}
    fallback_session["session_state"] = "session-1"
    assert refresh_claims_match(fallback_session, dict(fallback_session))
    assert not refresh_claims_match(
        {**fallback_client, "azp": "astral-frontend"}, fallback_client
    )
    assert not refresh_claims_match(
        {**fallback_session, "sid": "session-1"}, fallback_session
    )
    assert not refresh_claims_match(
        {**original, "azp": None}, {**original, "client_id": "astral-frontend"}
    )
    assert not refresh_claims_match(
        {**original, "sid": None}, {**original, "session_state": "session-1"}
    )
    assert session_identity({**original, "sid": None, "session_state": None}) is None


def test_snapshots_and_authorization_validation_are_bounded_and_fail_closed():
    claims = _claims()
    snapshot = subject_auth_snapshot(claims)
    assert snapshot["_raw_token"] == "old-token"
    assert snapshot["sub"] == "user-1"
    assert session_identity(None) is None
    assert session_identity({**claims, "sub": " "}) is None
    assert session_identity({**claims, "sid": None, "session_state": None}) is None
    assert subject_binding(None) is None
    assert subject_binding({**claims, "azp": None, "client_id": None}) is None
    assert not refresh_claims_match(claims, {**claims, "scope": {"unexpected": True}})
    assert not refresh_claims_match(claims, {**claims, "resource_access": []})
    assert not refresh_claims_match(
        claims,
        {**claims, "resource_access": {"astral-frontend": {"roles": [1]}}},
    )
    assert not refresh_claims_match(
        claims,
        {**claims, "realm_access": {"roles": [None]}},
    )
    assert not ready_for_renewal(
        {**claims, "resource_access": {"astral-frontend": None}},
        claims["_auth_connection_generation"],
        mock_auth=False,
    )


def test_registration_negotiates_only_valid_nonmock_stable_sessions():
    generation = str(uuid.uuid4())
    claims = _claims()
    enabled = bind_registered_session(
        claims,
        token="fresh-authenticated-token",
        capabilities=["ui_auth_refresh_v1", None, 4],
        connection_generation=generation,
        mock_auth=False,
    )
    assert enabled is True
    assert claims["_auth_refresh_enabled"] is True
    assert claims["_auth_connection_generation"] == generation
    assert claims["_auth_context_generation"] == generation
    assert claims["_raw_token"] == "fresh-authenticated-token"
    assert claims["_client_capabilities"] == ["ui_auth_refresh_v1"]

    assert bind_registered_session(
        _claims(),
        token="validated-token",
        capabilities=["ui_auth_refresh_v1"],
        connection_generation=generation,
        mock_auth=True,
    ) is False
    assert bind_registered_session(
        {**_claims(), "sid": None, "session_state": None},
        token="validated-token",
        capabilities=["ui_auth_refresh_v1"],
        connection_generation=generation,
        mock_auth=False,
    ) is False
    assert bind_registered_session(
        {},
        token="validated-token",
        capabilities=9,
        connection_generation=None,
        mock_auth=False,
    ) is False


def test_refresh_frame_parser_rejects_malformed_duplicates_and_oversize_tokens():
    generation = str(uuid.uuid4())
    valid = _frame(generation)
    assert parse_refresh_frame(valid) is not None
    assert parse_refresh_message(json.dumps(valid)) == valid
    assert parse_refresh_message('{"type":"refresh_ui_auth","type":"refresh_ui_auth"}') is None
    assert parse_refresh_message("not-json") is None
    assert parse_refresh_frame(None) is None
    assert parse_refresh_frame({**valid, "request_id": "not-a-uuid"}) is None
    assert parse_refresh_frame({**valid, "token": "x" * 32769}) is None


def test_operation_binding_capture_uses_baseline_identity_and_fence():
    websocket = object()
    generation = str(uuid.uuid4())
    claims = _claims(_auth_context_generation=generation)
    operation_id = uuid.uuid4()
    operation = SimpleNamespace(
        owner=SimpleNamespace(owner_user_id="user-1"),
        fence="fence-1",
        lease_lost=False,
        subject_auth_claims=subject_auth_snapshot(claims),
    )
    context = _context(websocket, generation)
    context.operations[operation_id] = operation
    active = {
        "operation": SimpleNamespace(operation_id=operation_id),
        "execution_fence": "fence-1",
        "owner": operation.owner,
        "connection_generation": generation,
    }
    resolver = capture_subject_token_resolver(
        {websocket: claims}, {id(websocket): context}, websocket, "user-1", active
    )
    assert resolver is not None
    assert resolver.operation_id == operation_id
    assert resolver.operation is operation
    assert resolver.resolve() == "old-token"
    assert capture_subject_token_resolver(
        {websocket: claims}, {id(websocket): context}, websocket, "other", active
    ) is None
    active["execution_fence"] = "stale"
    assert capture_subject_token_resolver(
        {websocket: claims}, {id(websocket): context}, websocket, "user-1", active
    ) is None


def test_refresh_refusal_after_verifier_replacement_does_not_touch_new_session():
    async def run():
        websocket = object()
        generation = str(uuid.uuid4())
        original = _claims(_auth_connection_generation=generation)
        replacement = {**original, "sid": "replacement-session"}
        sessions = {websocket: original}
        contexts = {id(websocket): _context(websocket, generation)}

        async def verify(_token):
            sessions[websocket] = replacement
            return _claims(
                token="new",
                iat=original["iat"] + 1,
                exp=original["exp"] + 60,
            )

        ack = await UIAuthRenewal(sessions, contexts, verify).handle(
            websocket, _frame(generation)
        )
        assert ack["accepted"] is False
        assert sessions[websocket] is replacement
    asyncio.run(run())


def test_subject_token_resolver_requires_same_socket_identity_and_live_fence():
    websocket = object()
    generation = str(uuid.uuid4())
    claims = _claims(_auth_connection_generation=generation)
    operation_id = uuid.uuid4()
    operation = SimpleNamespace(
        lease_lost=False,
        fence="fence-1",
        owner=SimpleNamespace(owner_user_id="user-1"),
    )
    context = _context(websocket, generation)
    context.operations[operation_id] = operation
    resolver = RegisteredSubjectTokenResolver(
        sessions={websocket: claims},
        contexts={id(websocket): context},
        websocket=websocket,
        owner_id="user-1",
        connection_generation=generation,
        identity=session_identity(claims),
        operation_id=operation_id,
        execution_fence="fence-1",
        operation=operation,
        original_binding=subject_binding(claims),
        original_token="old-token",
        original_exp=claims["exp"],
    )
    assert resolver.resolve() == "old-token"
    assert "old-token" not in repr(resolver)
    operation.lease_lost = True
    assert resolver.resolve() is None


def test_subject_token_resolver_rejects_replaced_socket_generation_and_fence():
    websocket = object()
    generation = str(uuid.uuid4())
    claims = _claims(_auth_connection_generation=generation)
    operation_id = uuid.uuid4()
    operation = SimpleNamespace(
        lease_lost=False,
        fence="fence-1",
        owner=SimpleNamespace(owner_user_id="user-1"),
    )
    context = _context(websocket, generation)
    context.operations[operation_id] = operation
    sessions = {websocket: claims}
    contexts = {id(websocket): context}
    resolver = RegisteredSubjectTokenResolver(
        sessions=sessions,
        contexts=contexts,
        websocket=websocket,
        owner_id="user-1",
        connection_generation=generation,
        identity=session_identity(claims),
        operation_id=operation_id,
        execution_fence="fence-1",
        operation=operation,
        original_binding=subject_binding(claims),
        original_token="old-token",
        original_exp=claims["exp"],
    )
    assert resolver.resolve() == "old-token"
    sessions[websocket] = {**claims, "sub": "other"}
    assert resolver.resolve() is None
    sessions[websocket] = claims
    context.connection_generation = uuid.uuid4()
    assert resolver.resolve() is None


def test_authenticated_websocket_ingress_renews_expired_token_for_same_running_operation(caplog):
    caplog.set_level("INFO", logger="Orchestrator")
    async def run():
        from orchestrator.orchestrator import Orchestrator

        websocket = object()
        generation = str(uuid.uuid4())
        now = int(time.time())
        original = _claims(
            token="expired-token",
            iat=now - 90,
            exp=now - 1,
            _auth_connection_generation=generation,
            _auth_refresh_enabled=True,
        )
        operation_id = uuid.uuid4()
        operation = SimpleNamespace(
            operation_id=operation_id,
            owner=SimpleNamespace(owner_user_id="user-1"),
            fence="fence-stable",
            lease_lost=False,
        )
        operation.subject_auth_claims = original
        context = _context(websocket, generation)
        context.operations[operation_id] = operation
        orchestrator = Orchestrator.__new__(Orchestrator)
        orchestrator.ui_sessions = {websocket: original}
        orchestrator._connection_contexts = {id(websocket): context}
        sent = []

        async def verify(token):
            assert token == "renewed-token"
            return _claims(
                token="renewed-token",
                iat=now,
                exp=now + 120,
                _auth_connection_generation="claim-forgery-is-ignored",
            )

        async def safe_send(_websocket, payload):
            sent.append(payload)

        orchestrator.validate_token = verify
        orchestrator._safe_send = safe_send
        resolver = RegisteredSubjectTokenResolver(
            sessions=orchestrator.ui_sessions,
            contexts=orchestrator._connection_contexts,
            websocket=websocket,
            owner_id="user-1",
            connection_generation=generation,
            identity=session_identity(original),
            operation_id=operation_id,
            execution_fence="fence-stable",
            operation=operation,
            original_binding=subject_binding(original),
            original_token="expired-token",
            original_exp=original["exp"],
            renewable=True,
        )
        assert resolver.resolve() is None
        request_id = str(uuid.uuid4())
        await orchestrator.handle_ui_message(
            websocket,
            json.dumps(_frame(generation, "renewed-token", request_id)),
        )
        assert json.loads(sent[0]) == {
            "type": "ui_auth_refreshed",
            "request_id": request_id,
            "connection_generation": generation,
            "accepted": True,
        }
        assert resolver.resolve() == "renewed-token"
        assert context.operations[operation_id] is operation
        assert operation.fence == "fence-stable"
        assert all("renewed-token" not in message for message in sent)
        assert f"request={request_id} connection={generation} accepted=True" in caplog.text
        assert "renewed-token" not in caplog.text
        assert "expired-token" not in caplog.text
    asyncio.run(run())


def test_registration_ready_advertises_renewal_only_on_current_socket():
    async def run():
        from orchestrator.orchestrator import Orchestrator

        websocket = object()
        generation = str(uuid.uuid4())
        session = _claims(_auth_connection_generation=generation)
        bind_registered_session(
            session,
            token="registered-token",
            capabilities=["ui_auth_refresh_v1"],
            connection_generation=generation,
            mock_auth=False,
        )
        context = _context(websocket, generation)
        orchestrator = Orchestrator.__new__(Orchestrator)
        orchestrator.ui_sessions = {websocket: session}
        orchestrator._connection_contexts = {id(websocket): context}
        sent = []

        async def safe_send(_websocket, payload):
            sent.append(payload)

        orchestrator._safe_send = safe_send
        await orchestrator._publish_registration_ready(
            context, (session, '{"type":"rote_config"}')
        )
        assert json.loads(sent[1]) == {
            "type": "ui_auth_refresh_ready",
            "connection_generation": generation,
        }
        assert "registered-token" not in "".join(sent)

        context.connection_generation = uuid.uuid4()
        sent.clear()
        await orchestrator._publish_registration_ready(
            context, (session, '{"type":"rote_config"}')
        )
        assert len(sent) == 1
    asyncio.run(run())


def test_delegation_result_is_discarded_if_operation_fence_changes_during_exchange():
    async def run():
        from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
        from orchestrator.orchestrator import Orchestrator

        websocket = object()
        generation = str(uuid.uuid4())
        claims = _claims(
            _auth_context_generation=generation,
            _auth_connection_generation=generation,
            _auth_refresh_enabled=False,
        )
        operation_id = uuid.uuid4()
        owner = SimpleNamespace(owner_user_id="user-1")
        work = SimpleNamespace(
            operation_id=operation_id,
            owner=owner,
            fence="fence-1",
            lease_lost=False,
            subject_auth_claims=subject_auth_snapshot(claims),
        )
        context = _context(websocket, generation)
        context.operations[operation_id] = work
        orchestrator = Orchestrator.__new__(Orchestrator)
        orchestrator.ui_sessions = {websocket: claims}
        orchestrator._connection_contexts = {id(websocket): context}
        orchestrator.agent_cards = {
            "agent": SimpleNamespace(skills=[SimpleNamespace(id="read")])
        }
        orchestrator.security_flags = {}

        class Permissions:
            @staticmethod
            def is_tool_allowed(*_args):
                return True

            @staticmethod
            def get_enabled_scope_names(*_args):
                return ["tools:read"]

        class Delegation:
            async def exchange_token_for_agent(self, token, *_args):
                assert token == "old-token"
                work.lease_lost = True
                return {"access_token": "child-token"}

        orchestrator.tool_permissions = Permissions()
        orchestrator.delegation = Delegation()
        active = {
            "operation": SimpleNamespace(operation_id=operation_id),
            "owner": owner,
            "execution_fence": "fence-1",
            "connection_generation": generation,
        }
        context_token = _CONNECTION_OPERATION_CONTEXT.set(active)
        try:
            result = await orchestrator._get_delegation_token(
                websocket, "agent", "user-1"
            )
        finally:
            _CONNECTION_OPERATION_CONTEXT.reset(context_token)
        assert result is None
        assert claims["_delegation_fault"] == "session_auth_unavailable"
    asyncio.run(run())


def test_legacy_missing_session_id_uses_only_captured_token_until_expiry(monkeypatch):
    websocket = object()
    generation = str(uuid.uuid4())
    claims = _claims(
        token="captured",
        _auth_connection_generation=generation,
        _auth_refresh_enabled=False,
        sid=None,
    )
    operation_id = uuid.uuid4()
    operation = SimpleNamespace(
        lease_lost=False,
        fence="fence-1",
        owner=SimpleNamespace(owner_user_id="user-1"),
        subject_auth_claims=claims,
    )
    context = _context(websocket, generation)
    context.operations[operation_id] = operation
    sessions = {websocket: claims}
    resolver = RegisteredSubjectTokenResolver(
        sessions=sessions,
        contexts={id(websocket): context},
        websocket=websocket,
        owner_id="user-1",
        connection_generation=generation,
        identity=None,
        operation_id=operation_id,
        execution_fence="fence-1",
        operation=operation,
        original_binding=subject_binding(claims),
        original_token="captured",
        original_exp=claims["exp"],
        renewable=False,
    )
    sessions[websocket] = {**claims, "_raw_token": "different-session-token"}
    assert resolver.resolve() == "captured"
    monkeypatch.setattr(
        "orchestrator.ui_auth_renewal.time.time",
        lambda: claims["exp"] + 1,
    )
    assert resolver.resolve() is None


def test_nonrenewable_stable_session_rejects_same_socket_session_rotation():
    websocket = object()
    generation = str(uuid.uuid4())
    claims = _claims(
        token="captured",
        _auth_connection_generation=generation,
        _auth_refresh_enabled=False,
    )
    operation_id = uuid.uuid4()
    operation = SimpleNamespace(
        lease_lost=False,
        fence="fence-1",
        owner=SimpleNamespace(owner_user_id="user-1"),
        subject_auth_claims=claims,
    )
    context = _context(websocket, generation)
    context.operations[operation_id] = operation
    sessions = {websocket: claims}
    resolver = RegisteredSubjectTokenResolver(
        sessions=sessions,
        contexts={id(websocket): context},
        websocket=websocket,
        owner_id="user-1",
        connection_generation=generation,
        identity=session_identity(claims),
        operation_id=operation_id,
        execution_fence="fence-1",
        operation=operation,
        original_binding=subject_binding(claims),
        original_token="captured",
        original_exp=claims["exp"],
        renewable=False,
    )

    assert resolver.resolve() == "captured"
    sessions[websocket] = {**claims, "sid": "rotated-session"}

    assert resolver.resolve() is None


def test_auth_wait_uses_token_renewed_before_auth_required_is_sent():
    class Resolver:
        websocket = object()

        def __init__(self):
            self.current = False

        def is_current(self):
            return True

        def resolve(self):
            self.current = True
            return "already-renewed-token"

    async def run():
        from orchestrator.orchestrator import Orchestrator

        resolver = Resolver()
        runtime = object.__new__(Orchestrator)
        runtime.ui_sessions = {resolver.websocket: {"sub": "user-1"}}
        sent = []

        async def safe_send(_websocket, raw):
            sent.append(raw)

        runtime._safe_send = safe_send

        assert await Orchestrator._await_ui_auth_refresh(
            runtime, resolver.websocket, resolver
        ) == "already-renewed-token"
        assert sent == []
        assert "_auth_refresh_required" not in runtime.ui_sessions[resolver.websocket]

    asyncio.run(run())


def test_invalid_subject_exchange_waits_for_fresh_token_before_single_retry():
    async def run():
        from orchestrator.orchestrator import Orchestrator

        websocket = object()
        generation = str(uuid.uuid4())
        claims = _claims(
            token="old-token",
            _auth_connection_generation=generation,
            _auth_context_generation=generation,
            _auth_refresh_enabled=True,
        )
        session = dict(claims)
        context = _context(websocket, generation)
        orchestrator = Orchestrator.__new__(Orchestrator)
        orchestrator.ui_sessions = {websocket: session}
        orchestrator._connection_contexts = {id(websocket): context}
        orchestrator.agent_cards = {
            "agent": SimpleNamespace(skills=[SimpleNamespace(id="read")])
        }
        orchestrator.security_flags = {}

        class Permissions:
            @staticmethod
            def is_tool_allowed(*_args):
                return True

            @staticmethod
            def get_enabled_scope_names(*_args):
                return ["tools:read"]

        attempts = []

        class Delegation:
            async def exchange_token_for_agent(self, token, *_args):
                attempts.append(token)
                if len(attempts) == 1:
                    return {"error": "invalid_token"}
                assert token == "fresh-token"
                return {"access_token": "child-token"}

        orchestrator.tool_permissions = Permissions()
        orchestrator.delegation = Delegation()
        fresh = _claims(
            token="fresh-token",
            iat=claims["iat"] + 1,
            exp=claims["exp"] + 60,
        )

        async def verify(token):
            assert token == "fresh-token"
            return fresh

        renewal = UIAuthRenewal(orchestrator.ui_sessions, orchestrator._connection_contexts, verify)
        orchestrator._ui_auth_renewal = renewal

        async def safe_send(_websocket, raw):
            frame = json.loads(raw)
            assert frame["type"] == "auth_required"
            ack = await renewal.handle(websocket, _frame(generation, "fresh-token"))
            assert ack["accepted"] is True
            renewal.signal(websocket)

        orchestrator._safe_send = safe_send

        result = await Orchestrator._get_delegation_token(
            orchestrator, websocket, "agent", "user-1"
        )
        assert result == "child-token"
        assert attempts == ["old-token", "fresh-token"]

    asyncio.run(run())


def test_refresh_control_routes_through_connection_control_dispatch():
    async def run():
        from orchestrator.orchestrator import Orchestrator

        websocket = object()
        generation = str(uuid.uuid4())
        context = _context(websocket, generation)
        context.connection_scope_id = uuid.uuid4()
        context.preregistration = []
        context.registration_deadline = time.monotonic() + 10
        runtime = Orchestrator.__new__(Orchestrator)
        tasks = []
        delivered = []

        async def control(_context, raw):
            delivered.append(raw)

        def track(_context, coroutine, *, name):
            tasks.append((name, coroutine))

        runtime._run_ui_control = control
        runtime._track_connection_task = track
        frame = _frame(generation)
        raw = json.dumps(frame)

        assert await Orchestrator._route_ui_frame(runtime, context, raw) is True
        assert len(tasks) == 1
        name, coroutine = tasks.pop()
        assert name.startswith("connection-auth-refresh-")
        await coroutine
        assert delivered == [raw]

    asyncio.run(run())


def test_refresh_handler_ignores_malformed_frame_and_auth_requires_expired_refusal():
    async def run():
        from orchestrator.orchestrator import Orchestrator

        websocket = object()
        generation = str(uuid.uuid4())
        claims = _claims(
            exp=int(time.time()) - 1,
            _auth_connection_generation=generation,
            _auth_context_generation=generation,
            _auth_refresh_enabled=True,
        )
        sessions = {websocket: claims}
        contexts = {id(websocket): _context(websocket, generation)}
        verification_calls = []

        async def verify(_token):
            verification_calls.append(True)
            raise ValueError("private validation detail")

        runtime = Orchestrator.__new__(Orchestrator)
        runtime.ui_sessions = sessions
        runtime._connection_contexts = contexts
        runtime._ui_auth_renewal = UIAuthRenewal(sessions, contexts, verify)
        sent = []

        async def safe_send(_websocket, raw):
            sent.append(json.loads(raw))

        runtime._safe_send = safe_send
        malformed = json.dumps({**_frame(generation), "extra": True})
        await Orchestrator._handle_ui_auth_refresh(runtime, websocket, malformed)
        assert verification_calls == []
        assert sent == []

        await Orchestrator._handle_ui_auth_refresh(
            runtime, websocket, json.dumps(_frame(generation))
        )
        assert verification_calls == [True]
        assert [frame["type"] for frame in sent] == [
            "ui_auth_refreshed",
            "auth_required",
        ]
        assert sent[0]["accepted"] is False
        assert sent[0]["closed_code"] == "identity_changed"
        assert sent[1]["reason"] == "expired"
        assert "private validation detail" not in json.dumps(sent)
        assert claims["_auth_refresh_required"] is True

    asyncio.run(run())


def test_wait_for_refresh_is_bounded_and_wakes_only_on_same_socket_renewal():
    async def run():
        websocket = object()
        generation = str(uuid.uuid4())
        now = int(time.time())
        old = _claims(
            token="expired",
            iat=now - 60,
            exp=now - 1,
            _auth_connection_generation=generation,
            _auth_refresh_enabled=True,
        )
        operation_id = uuid.uuid4()
        operation = SimpleNamespace(
            lease_lost=False,
            fence="fence-1",
            owner=SimpleNamespace(owner_user_id="user-1"),
            subject_auth_claims=old,
        )
        context = _context(websocket, generation)
        context.operations[operation_id] = operation
        sessions = {websocket: old}
        contexts = {id(websocket): context}
        resolver = RegisteredSubjectTokenResolver(
            sessions=sessions,
            contexts=contexts,
            websocket=websocket,
            owner_id="user-1",
            connection_generation=generation,
            identity=session_identity(old),
            operation_id=operation_id,
            execution_fence="fence-1",
            operation=operation,
            original_binding=subject_binding(old),
            original_token="expired",
            original_exp=old["exp"],
            renewable=True,
        )
        fresh = _claims(token="renewed", iat=now, exp=now + 120)
        async def verify(_token):
            return fresh
        renewal = UIAuthRenewal(sessions, contexts, verify)
        old["_auth_refresh_required"] = True
        waiter = asyncio.create_task(renewal.wait_for_token(resolver, timeout=2))
        await asyncio.sleep(0)
        ack = await renewal.handle(websocket, _frame(generation, "renewed"))
        renewal.signal(websocket)
        assert ack["accepted"] is True
        assert await waiter == "renewed"
    asyncio.run(run())


def test_wait_for_refresh_stops_when_operation_fence_is_revoked():
    async def run():
        websocket = object()
        generation = str(uuid.uuid4())
        claims = _claims(
            exp=int(time.time()) - 1,
            _auth_connection_generation=generation,
            _auth_refresh_enabled=True,
        )
        operation_id = uuid.uuid4()
        operation = SimpleNamespace(
            lease_lost=False,
            fence="fence-1",
            owner=SimpleNamespace(owner_user_id="user-1"),
            subject_auth_claims=claims,
        )
        context = _context(websocket, generation)
        context.operations[operation_id] = operation
        resolver = RegisteredSubjectTokenResolver(
            sessions={websocket: claims},
            contexts={id(websocket): context},
            websocket=websocket,
            owner_id="user-1",
            connection_generation=generation,
            identity=session_identity(claims),
            operation_id=operation_id,
            execution_fence="fence-1",
            operation=operation,
            original_binding=subject_binding(claims),
            original_token="old-token",
            original_exp=claims["exp"],
            renewable=True,
        )
        renewal = UIAuthRenewal(
            {websocket: claims},
            {id(websocket): context},
            lambda _token: None,
        )
        claims["_auth_refresh_required"] = True
        waiter = asyncio.create_task(renewal.wait_for_token(resolver, timeout=30))
        await asyncio.sleep(0)
        operation.lease_lost = True
        assert await asyncio.wait_for(waiter, timeout=0.5) is None
    asyncio.run(run())


def test_wait_for_refresh_times_out_and_rejects_nonrenewable_bindings():
    async def run():
        websocket = object()
        generation = str(uuid.uuid4())
        claims = _claims(_auth_connection_generation=generation)
        context = _context(websocket, generation)
        resolver = RegisteredSubjectTokenResolver(
            sessions={websocket: claims},
            contexts={id(websocket): context},
            websocket=websocket,
            owner_id="user-1",
            connection_generation=generation,
            identity=session_identity(claims),
            original_binding=subject_binding(claims),
            original_token="old-token",
            original_exp=claims["exp"],
            renewable=False,
        )
        renewal = UIAuthRenewal(
            {websocket: claims}, {id(websocket): context}, lambda _token: None
        )
        assert await renewal.wait_for_token(resolver) is None
        resolver = RegisteredSubjectTokenResolver(
            **{
                **resolver.__dict__,
                "renewable": True,
            }
        )
        claims["_auth_refresh_required"] = True
        assert await renewal.wait_for_token(resolver, timeout=0) is None
    asyncio.run(run())
