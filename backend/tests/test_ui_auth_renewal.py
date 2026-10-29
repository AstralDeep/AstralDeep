"""Exercise same-session UI access-token renewal and fenced credential lookup."""

import asyncio
import time
import uuid
import json
from dataclasses import replace
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
    capture_subject_token_resolver_with_reason,
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


def _user_owner(user_id="user-1"):
    return SimpleNamespace(
        owner_scope=SimpleNamespace(value="user"),
        owner_user_id=user_id,
        connection_scope_id=None,
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
        owner=_user_owner(),
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


def test_capture_diagnostic_is_bounded_and_identifies_operation_fence_failure():
    websocket = object()
    generation = str(uuid.uuid4())
    claims = _claims(token="private-sentinel", _auth_context_generation=generation)
    operation_id = uuid.uuid4()
    operation = SimpleNamespace(
        owner=_user_owner(),
        fence="current-fence",
        lease_lost=False,
        subject_auth_claims=subject_auth_snapshot(claims),
    )
    context = _context(websocket, generation)
    context.operations[operation_id] = operation
    active = {
        "operation": SimpleNamespace(operation_id=operation_id),
        "execution_fence": "stale-fence",
        "owner": operation.owner,
        "connection_generation": generation,
    }
    resolver, reason = capture_subject_token_resolver_with_reason(
        {websocket: claims}, {id(websocket): context}, websocket, "user-1", active
    )
    assert resolver is None
    assert reason == "operation_fence_mismatch"
    assert "private-sentinel" not in repr(reason)

    resolver, reason = capture_subject_token_resolver_with_reason(
        {websocket: claims}, {id(websocket): context}, websocket, "different-user", active
    )
    assert resolver is None
    assert reason == "owner_mismatch"


def test_capture_diagnostic_explains_missing_generation_without_claim_values():
    websocket = object()
    claims = _claims(token="private-sentinel")
    claims.pop("_auth_context_generation")
    resolver, reason = capture_subject_token_resolver_with_reason(
        {websocket: claims},
        {id(websocket): _context(websocket, str(uuid.uuid4()))},
        websocket,
        "user-1",
    )
    assert resolver is None
    assert reason == "generation_missing"
    assert "private-sentinel" not in repr(reason)


def test_capture_diagnostic_classifies_registration_and_claim_failures():
    def diagnose(case):
        websocket = object()
        generation = str(uuid.uuid4())
        claims = _claims(_auth_context_generation=generation)
        context = _context(websocket, generation)
        sessions = {websocket: claims}
        contexts = {id(websocket): context}
        active = None
        if case == "session_missing":
            sessions.clear()
        elif case == "context_missing":
            contexts.clear()
        elif case == "socket_mismatch":
            context.websocket = object()
        elif case == "not_registered":
            context.registered = False
        elif case == "connection_closing":
            context.closing = True
        elif case == "owner_mismatch":
            pass
        elif case == "generation_missing":
            claims.pop("_auth_context_generation")
        elif case == "generation_mismatch":
            context.connection_generation = uuid.uuid4()
        elif case == "subject_binding_invalid":
            claims.pop("azp")
            claims.pop("client_id", None)
        elif case == "authorization_claims_invalid":
            claims["realm_access"] = []
        else:
            operation_id = uuid.uuid4()
            admitted_owner = _user_owner()
            if case == "operation_connection_scope_missing":
                admitted_owner = SimpleNamespace(
                    owner_scope=SimpleNamespace(value="connection"),
                    owner_user_id=None,
                    connection_scope_id=uuid.uuid4(),
                )
                context.connection_scope_id = None
            operation = SimpleNamespace(
                owner=admitted_owner,
                fence="fence-1",
                lease_lost=False,
                subject_auth_claims=subject_auth_snapshot(claims),
            )
            context.operations[operation_id] = operation
            active = {
                "operation": SimpleNamespace(operation_id=operation_id),
                "owner": operation.owner,
                "execution_fence": "fence-1",
                "connection_generation": generation,
            }
            if case == "operation_missing":
                active["operation"] = SimpleNamespace(operation_id=uuid.uuid4())
            elif case == "operation_owner_mismatch":
                operation.owner = _user_owner("different-user")
                active["owner"] = operation.owner
            elif case == "operation_lease_lost":
                operation.lease_lost = True
            elif case == "operation_fence_missing":
                operation.fence = None
                active["execution_fence"] = None
            elif case == "operation_fence_mismatch":
                active["execution_fence"] = "stale-fence"
            elif case == "operation_owner_binding_mismatch":
                active["owner"] = _user_owner("different-user")
            elif case == "operation_generation_mismatch":
                active["connection_generation"] = str(uuid.uuid4())
            elif case == "operation_subject_claims_missing":
                operation.subject_auth_claims = None
            elif case == "operation_subject_binding_mismatch":
                operation.subject_auth_claims = subject_auth_snapshot(
                    {**claims, "sub": "different-user"}
                )
            elif case == "operation_authorization_claims_invalid":
                baseline = dict(operation.subject_auth_claims)
                baseline["realm_access"] = []
                operation.subject_auth_claims = baseline
            elif case == "operation_generation_mismatch_baseline":
                baseline = dict(operation.subject_auth_claims)
                baseline["_auth_context_generation"] = str(uuid.uuid4())
                operation.subject_auth_claims = baseline
        owner_id = "different-user" if case == "owner_mismatch" else "user-1"
        return capture_subject_token_resolver_with_reason(
            sessions, contexts, websocket, owner_id, active
        )[1]

    cases = (
        "session_missing",
        "context_missing",
        "socket_mismatch",
        "not_registered",
        "connection_closing",
        "generation_missing",
        "generation_mismatch",
        "subject_binding_invalid",
        "authorization_claims_invalid",
        "operation_missing",
        "operation_owner_mismatch",
        "operation_connection_scope_missing",
        "operation_lease_lost",
        "operation_fence_missing",
        "operation_fence_mismatch",
        "operation_owner_binding_mismatch",
        "operation_generation_mismatch",
        "operation_subject_claims_missing",
        "operation_subject_binding_mismatch",
        "operation_authorization_claims_invalid",
        "operation_generation_mismatch_baseline",
    )
    expected = tuple(
        "operation_generation_mismatch"
        if case == "operation_generation_mismatch_baseline"
        else "operation_owner_mismatch"
        if case == "operation_connection_scope_missing"
        else case
        for case in cases
    )
    assert tuple(diagnose(case) for case in cases) == expected


def test_resolver_unavailable_reason_is_bounded_and_tracks_token_state():
    websocket = object()
    generation = str(uuid.uuid4())
    claims = _claims(_auth_context_generation=generation)
    claims["_auth_refresh_enabled"] = True
    contexts = {id(websocket): _context(websocket, generation)}

    def capture(session):
        return capture_subject_token_resolver_with_reason(
            {websocket: session}, contexts, websocket, "user-1"
        )[0]

    resolver = capture(claims)
    assert resolver is not None
    assert resolver.unavailable_reason() == "credential_available"
    claims["_auth_refresh_required"] = True
    assert resolver.unavailable_reason() == "refresh_required"
    claims.pop("_auth_refresh_required")
    claims["_raw_token"] = ""
    assert resolver.unavailable_reason() == "credential_unavailable"
    claims["_raw_token"] = "present"
    claims["exp"] = "invalid"
    resolver = capture(claims)
    assert resolver is not None
    assert resolver.unavailable_reason() == "expiry_unavailable"
    claims["exp"] = int(time.time()) - 1
    resolver = capture(claims)
    assert resolver is not None
    assert resolver.unavailable_reason() == "credential_expired"
    claims["exp"] = int(time.time()) + 30
    resolver = capture(claims)
    contexts[id(websocket)].closing = True
    assert resolver is not None
    assert resolver.unavailable_reason() == "binding_not_current"


def test_postgres_claim_identity_resolves_registered_chat_subject_token():
    from datetime import UTC, datetime

    from orchestrator.orchestrator import ConnectionContext, _ConnectionIngressFrame, _ConnectionOperation
    from orchestrator.work_admission import (
        AdmissionClass,
        OperationOwner,
        OperationRequest,
        OwnerScope,
        WorkAdmissionCoordinator,
    )
    from tests.helpers.voice_plane_runtime import isolated_plane_runtime

    websocket = object()
    generation = uuid.uuid4()
    claims = _claims(
        token="postgres-chat-token",
        _auth_connection_generation=str(generation),
        _auth_context_generation=str(generation),
    )
    assert bind_registered_session(
        claims,
        token="postgres-chat-token",
        capabilities=["ui_auth_refresh_v1"],
        connection_generation=str(generation),
        mock_auth=False,
    )
    context = ConnectionContext(
        websocket=websocket,
        connection_scope_id=uuid.uuid4(),
        registration_deadline=time.monotonic() + 30,
        connection_generation=generation,
        registered=True,
    )
    owner = OperationOwner(
        OwnerScope.CONNECTION, None, context.connection_scope_id
    )

    with isolated_plane_runtime("ui_auth_resolver") as plane:
        coordinator = WorkAdmissionCoordinator.from_plane(
            plane_runtime=plane,
            plane_repositories=plane.repositories,
            clock=lambda: datetime.now(UTC),
        )
        request = OperationRequest(
            operation_kind="chat_message",
            admission_class=AdmissionClass.INTERACTIVE,
            owner=owner,
            submission_id=uuid.uuid4(),
            idempotency_namespace=None,
            idempotency_key=None,
            normalized_input_digest=None,
            chat_id=str(uuid.uuid4()),
            parent_operation_id=None,
            connection_generation=generation,
            request_generation=uuid.uuid4(),
        )
        admitted = coordinator.submit(request)
        assert admitted.accepted
        claim = coordinator.claim_operation(AdmissionClass.INTERACTIVE, admitted.operation_id)
        assert claim is not None
        frame = _ConnectionIngressFrame(
            raw="{}",
            parsed={"type": "ui_event", "action": "chat_message"},
            action="chat_message",
            surface=None,
            chat_id=request.chat_id,
            submission_id=request.submission_id,
            request_generation=request.request_generation,
            normalized_digest="a" * 64,
            read_only=False,
            operation_kind="chat_message",
            deadline_at_monotonic=None,
            deadline_at_utc=None,
        )
        work = _ConnectionOperation(
            frame=frame,
            owner=owner,
            operation_id=admitted.operation_id,
            subject_auth_claims=subject_auth_snapshot(claims),
        )
        work.fence = claim.fence
        context.operations[work.operation_id] = work
        active = {
            "operation": claim.operation,
            "owner": owner,
            "execution_fence": claim.fence,
            "connection_generation": generation,
        }
        resolver, reason = capture_subject_token_resolver_with_reason(
            {websocket: claims},
            {id(websocket): context},
            websocket,
            "user-1",
            active,
        )
        assert reason is None
        assert resolver is not None
        assert resolver.operation is work
        assert resolver.resolve() == "postgres-chat-token"


def test_connection_owned_resolver_binds_scope_owner_fence_and_cancellation():
    async def run():
        from orchestrator.work_admission import OperationOwner, OwnerScope

        websocket = object()
        generation = str(uuid.uuid4())
        claims = _claims(
            token="connection-chat-token",
            _auth_connection_generation=generation,
            _auth_context_generation=generation,
        )
        context = _context(websocket, generation)
        owner = OperationOwner(
            OwnerScope.CONNECTION, None, uuid.uuid4()
        )
        context.connection_scope_id = owner.connection_scope_id
        operation_id = uuid.uuid4()
        operation = SimpleNamespace(
            owner=owner,
            fence="fence-current",
            lease_lost=False,
            subject_auth_claims=subject_auth_snapshot(claims),
        )
        context.operations[operation_id] = operation
        active = {
            "operation": SimpleNamespace(operation_id=operation_id),
            "owner": owner,
            "execution_fence": operation.fence,
            "connection_generation": generation,
        }
        sessions = {websocket: claims}
        contexts = {id(websocket): context}
        resolver, reason = capture_subject_token_resolver_with_reason(
            sessions, contexts, websocket, "user-1", active
        )
        assert reason is None
        assert resolver is not None
        assert resolver.resolve() == "connection-chat-token"

        operation.owner = OperationOwner(
            OwnerScope.CONNECTION, None, uuid.uuid4()
        )
        assert not resolver.is_current()
        operation.owner = owner
        sessions[websocket] = {**claims, "sub": "different-user"}
        assert not resolver.is_current()
        sessions[websocket] = claims
        operation.fence = "replacement-fence"
        assert not resolver.is_current()
        operation.fence = "fence-current"
        context.connection_scope_id = None
        assert not resolver.is_current()
        context.connection_scope_id = owner.connection_scope_id

        active["execution_fence"] = "other-fence"
        resolver, reason = capture_subject_token_resolver_with_reason(
            sessions, contexts, websocket, "user-1", active
        )
        assert resolver is None
        assert reason == "operation_fence_mismatch"
        active["execution_fence"] = "fence-current"

        async def cancelled_execution():
            task = asyncio.current_task()
            task.cancel()
            assert not resolver_current.is_current()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                return
            raise AssertionError("cancelled execution continued")

        resolver_current, reason = capture_subject_token_resolver_with_reason(
            sessions, contexts, websocket, "user-1", active
        )
        assert reason is None
        await asyncio.create_task(cancelled_execution())

    asyncio.run(run())


def test_resolver_rejects_replaced_completed_and_cancelling_parent_tasks():
    async def run():
        from orchestrator.work_admission import OperationOwner, OwnerScope

        def capture(parent_task):
            websocket = object()
            generation = str(uuid.uuid4())
            claims = _claims(
                token="parent-bound-token",
                _auth_connection_generation=generation,
                _auth_context_generation=generation,
            )
            context = _context(websocket, generation)
            context.connection_scope_id = uuid.uuid4()
            owner = OperationOwner(
                OwnerScope.CONNECTION, None, context.connection_scope_id
            )
            operation_id = uuid.uuid4()
            operation = SimpleNamespace(
                owner=owner,
                fence="parent-fence",
                lease_lost=False,
                task=parent_task,
                subject_auth_claims=subject_auth_snapshot(claims),
            )
            context.operations[operation_id] = operation
            active = {
                "operation": SimpleNamespace(operation_id=operation_id),
                "owner": owner,
                "execution_fence": operation.fence,
                "connection_generation": generation,
            }
            resolver, reason = capture_subject_token_resolver_with_reason(
                {websocket: claims}, {id(websocket): context}, websocket, "user-1", active
            )
            assert reason is None
            assert resolver is not None
            return resolver, operation

        release_first = asyncio.Event()
        first_parent = asyncio.create_task(release_first.wait())
        await asyncio.sleep(0)
        resolver, operation = capture(first_parent)
        assert resolver.resolve() == "parent-bound-token"
        replacement = asyncio.create_task(asyncio.sleep(10))
        operation.task = replacement
        assert resolver.resolve() is None
        operation.task = first_parent
        release_first.set()
        await first_parent
        assert resolver.resolve() is None
        replacement.cancel()
        try:
            await replacement
        except asyncio.CancelledError:
            pass

        second_parent = asyncio.create_task(asyncio.Event().wait())
        await asyncio.sleep(0)
        cancelling_resolver, _operation = capture(second_parent)
        second_parent.cancel()

        async def resolve_from_child():
            return cancelling_resolver.resolve()

        child_result = await asyncio.create_task(resolve_from_child())
        assert child_result is None
        try:
            await second_parent
        except asyncio.CancelledError:
            pass

    asyncio.run(run())


def test_missing_legacy_token_does_not_set_typed_refresh_fault(monkeypatch):
    monkeypatch.setenv("DELEGATION_REQUIRED", "false")

    async def run():
        from orchestrator.orchestrator import Orchestrator

        websocket = object()
        session = {"sub": "user-1"}
        runtime = Orchestrator.__new__(Orchestrator)
        runtime.ui_sessions = {websocket: session}
        runtime.agent_cards = {"agent": SimpleNamespace(skills=[])}
        runtime.security_flags = {}

        class Permissions:
            @staticmethod
            def get_enabled_scope_names(*_args):
                return []

        runtime.tool_permissions = Permissions()
        assert await Orchestrator._get_delegation_token(
            runtime, websocket, "agent", "user-1"
        ) is None
        assert "_delegation_fault" not in session

    asyncio.run(run())


def test_active_chat_auth_refusal_logs_only_bounded_capture_reason(caplog):
    async def run():
        from orchestrator.orchestrator import (
            Orchestrator,
            _CONNECTION_OPERATION_CONTEXT,
        )

        websocket = object()
        session = {"sub": "user-1", "_auth_refresh_enabled": True}
        runtime = Orchestrator.__new__(Orchestrator)
        runtime.ui_sessions = {websocket: session}
        runtime._connection_contexts = {}
        runtime.agent_cards = {"agent": SimpleNamespace(skills=[])}
        runtime.security_flags = {}

        class Permissions:
            @staticmethod
            def get_enabled_scope_names(*_args):
                return []

        runtime.tool_permissions = Permissions()
        token = _CONNECTION_OPERATION_CONTEXT.set({"operation": object()})
        try:
            assert await Orchestrator._get_delegation_token(
                runtime, websocket, "agent", "user-1"
            ) is None
        finally:
            _CONNECTION_OPERATION_CONTEXT.reset(token)
        assert session["_delegation_fault"] == "session_auth_unavailable"

    caplog.set_level("WARNING", logger="Orchestrator")
    asyncio.run(run())
    assert "reason=context_missing" in caplog.text
    assert "user-1" not in caplog.text


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
        owner=_user_owner(),
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
        owner=_user_owner(),
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


def test_authenticated_websocket_ingress_renews_expired_token_for_same_running_operation(caplog, monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
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
            owner=_user_owner(),
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
        owner=_user_owner(),
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
        owner=_user_owner(),
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


def test_normal_admitted_chat_claim_resolves_registered_subject_token():
    async def run():
        from datetime import UTC, datetime
        from orchestrator.orchestrator import (
            ConnectionContext,
            Orchestrator,
            _ConnectionOperation,
            _ConnectionIngressFrame,
        )
        from orchestrator.work_admission import (
            AdmissionClass,
            AdmissionClassConfig,
            InMemoryWorkAdmissionRepository,
            OperationOwner,
            OperationRequest,
            OwnerScope,
            WorkAdmissionCoordinator,
        )

        websocket = object()
        generation = uuid.uuid4()
        claims = _claims(
            token="normal-chat-token",
            _auth_connection_generation=str(generation),
            _auth_context_generation=str(generation),
        )
        bind_registered_session(
            claims,
            token="normal-chat-token",
            capabilities=["ui_auth_refresh_v1"],
            connection_generation=str(generation),
            mock_auth=False,
        )
        context = ConnectionContext(
            websocket=websocket,
            connection_scope_id=uuid.uuid4(),
            registration_deadline=time.monotonic() + 30,
            connection_generation=generation,
            registered=True,
        )
        owner = OperationOwner(
            OwnerScope.CONNECTION, None, context.connection_scope_id
        )
        coordinator = WorkAdmissionCoordinator(
            admission_classes=(
                AdmissionClassConfig(
                    class_name=AdmissionClass.GLOBAL,
                    parent_class_name=None,
                    active_limit=4,
                    queue_limit=0,
                    max_wait_ms=0,
                    config_revision="auth-resolver-normal-chat",
                ),
                AdmissionClassConfig(
                    class_name=AdmissionClass.INTERACTIVE,
                    parent_class_name=AdmissionClass.GLOBAL,
                    active_limit=4,
                    queue_limit=4,
                    max_wait_ms=1000,
                    config_revision="auth-resolver-normal-chat",
                ),
            ),
            repository=InMemoryWorkAdmissionRepository(),
            clock=lambda: datetime.now(UTC),
        )
        admitted = coordinator.submit(
            OperationRequest(
                operation_kind="chat_message",
                admission_class=AdmissionClass.INTERACTIVE,
                owner=owner,
                submission_id=uuid.uuid4(),
                idempotency_namespace=None,
                idempotency_key=None,
                normalized_input_digest=None,
                chat_id=str(uuid.uuid4()),
                parent_operation_id=None,
                connection_generation=generation,
                request_generation=uuid.uuid4(),
            )
        )
        accepted_id = admitted.operation_id
        frame = _ConnectionIngressFrame(
            raw="{}",
            parsed={"type": "ui_event", "action": "chat_message"},
            action="chat_message",
            surface=None,
            chat_id=str(uuid.uuid4()),
            submission_id=uuid.uuid4(),
            request_generation=uuid.uuid4(),
            normalized_digest="a" * 64,
            read_only=False,
            operation_kind="chat_message",
            deadline_at_monotonic=None,
            deadline_at_utc=None,
        )
        work = _ConnectionOperation(
            frame=frame,
            owner=owner,
            operation_id=accepted_id,
            subject_auth_claims=subject_auth_snapshot(claims),
        )
        context.operations[accepted_id] = work
        runtime = Orchestrator.__new__(Orchestrator)
        runtime.ui_sessions = {websocket: claims}
        runtime._connection_contexts = {id(websocket): context}
        runtime.work_admission = coordinator
        runtime._interactive_capacity_revision = 0
        runtime.agent_cards = {
            "agent": SimpleNamespace(skills=[SimpleNamespace(id="read")])
        }
        runtime.security_flags = {}

        class Permissions:
            @staticmethod
            def is_tool_allowed(*_args):
                return True

            @staticmethod
            def get_enabled_scope_names(*_args):
                return ["tools:read"]

        class Delegation:
            async def exchange_token_for_agent(self, token, *_args):
                assert token == "normal-chat-token"
                return {"access_token": "child-token"}

        runtime.tool_permissions = Permissions()
        runtime.delegation = Delegation()
        observed = []

        async def admission_call(method, *args, **kwargs):
            return method(*args, **kwargs)

        async def renewal(_context, _work, stop, _worker):
            await stop.wait()

        async def execute(_context, accepted_work, *, websocket):
            assert websocket is context.websocket
            observed.append(
                await Orchestrator._get_delegation_token(
                    runtime, websocket, "agent", "user-1"
                )
            )

        async def complete(_context, _work):
            return None

        runtime._call_work_admission = admission_call
        runtime._connection_admission_class = lambda _frame: AdmissionClass.INTERACTIVE
        runtime._emit_long_running_operation_phase = lambda *_args: asyncio.sleep(0)
        runtime._renew_connection_lease = renewal
        runtime._run_connection_ui_operation = execute
        runtime._complete_connection_operation = complete

        work.task = asyncio.current_task()
        await Orchestrator._run_connection_operation(runtime, context, work)

        assert observed == ["child-token"]

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
            owner=_user_owner(),
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
            owner=_user_owner(),
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
        resolver = replace(resolver, renewable=True)
        claims["_auth_refresh_required"] = True
        assert await renewal.wait_for_token(resolver, timeout=0) is None
    asyncio.run(run())
