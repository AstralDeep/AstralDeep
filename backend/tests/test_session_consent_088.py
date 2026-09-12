"""Server-selected consent identity, including refusal and await replacement."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import Request, WebSocket

from orchestrator import session_consent as sc, web_auth
from tests.helpers.session_consent_088 import synthetic_consent


@pytest.fixture
def fixture(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("WEB_SESSION_SECRET", "synthetic-consent-cookie-key")
    selected = synthetic_consent()
    state = SimpleNamespace(credential=selected.observation.credential,
                            observed_at=selected.observation.started_at)
    store = SimpleNamespace(capture_execution_reference=Mock(return_value=SimpleNamespace(state=state)))
    return selected, store


def connection(*, cookie=True, extra=(), socket=False, sid="consent-session"):
    headers = list(extra)
    if cookie:
        headers.append((b"cookie", f"astral_session={web_auth._sign(sid)}".encode()))
    scope = {"type": "websocket" if socket else "http", "method": "POST", "path": "/consent",
             "headers": headers, "query_string": b"", "scheme": "https", "state": {"kept": True}}
    return WebSocket(scope, receive=None, send=None) if socket else Request(scope)


def run(store, req=None, principal=None):
    return asyncio.run(sc.select_consent_session(
        req or connection(), store=store,
        principal=principal if principal is not None else {"sub": "owner", "exp": time.time()+300}))


@pytest.mark.parametrize("socket", [False, True])
@pytest.mark.parametrize("bearer", [False, True])
def test_cookie_selects_exact_session_after_normal_iam(fixture, socket, bearer):
    original, store = fixture
    req = connection(socket=socket, extra=[(b"authorization", b"Bearer synthetic-current")] if bearer else [])
    result = run(store, req)
    assert result.reference("owner") == original.reference("owner")
    store.capture_execution_reference.assert_called_once_with(owner_id="owner", session_id="consent-session")
    assert req.state.kept is True and len(req.scope["state"]) == 1
    assert not isinstance(result, type(result.observation))


@pytest.mark.parametrize("principal", [None, {}, {"sub":""}, {"sub":"x"*257},
    {"sub":"owner","exp":True}, {"sub":"owner","exp":float("nan")},
    {"sub":"owner","exp":float("inf")}, {"sub":"owner","exp":0},
    {"sub":"owner","exp":1e12,"act":{"sub":"agent"}},
    {"sub":"owner","exp":1e12,"machine_turn_class":"scheduled_job"}])
def test_unqualified_principal_never_selects_session(fixture, principal):
    _, store = fixture
    assert asyncio.run(sc.select_consent_session(connection(), principal=principal, store=store)) is None
    store.capture_execution_reference.assert_not_called()


@pytest.mark.parametrize("case", ["missing", "duplicate", "bad-signature", "invalid-sid", "oversized"])
def test_cookie_selection_is_closed(fixture, case):
    _, store = fixture
    req = connection(cookie=case != "missing",
                     sid="bad/path" if case == "invalid-sid" else "x"*257 if case == "oversized" else "consent-session",
                     extra=[(b"cookie", b"astral_session=invalid")] if case == "duplicate" else [])
    if case == "bad-signature":
        req = connection(cookie=False, extra=[(b"cookie", b"astral_session=consent-session.invalid")])
    assert run(store, req) is None
    store.capture_execution_reference.assert_not_called()


@pytest.mark.parametrize("change", [{"owner_id":"other"}, {"session_id":"other"}, {"version":1}])
def test_store_mismatch_never_adopted(fixture, change):
    original, store = fixture
    state = store.capture_execution_reference.return_value.state
    state.credential = replace(original.observation.credential, **change)
    assert run(store) is None


def test_selection_failure_and_cancellation(fixture, monkeypatch):
    _, store = fixture
    store.capture_execution_reference.side_effect = RuntimeError("sensitive diagnostic")
    assert run(store) is None
    store.capture_execution_reference.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        run(store)
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    store.capture_execution_reference.reset_mock()
    assert run(store) is None
    store.capture_execution_reference.assert_not_called()
    assert asyncio.run(sc.select_consent_session(object(), principal={}, store=store)) is None


def test_original_clock_and_principal_expiry_cap(fixture):
    original, store = fixture
    expiry = int(time.time())+4
    selected = run(store, principal={"sub":"owner","exp":expiry})
    assert selected.observation.started_at == original.observation.started_at
    assert selected.observation.valid_until.timestamp() == expiry
    store.capture_execution_reference.return_value.state.observed_at -= timedelta(seconds=16)
    assert run(store) is None


@pytest.mark.parametrize("change", [{"incarnation_id":"not-uuid"}, {"incarnation_id":None},
    {"incarnation_id":"AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"},
    {"incarnation_id":"aaaaaaaa-aaaa-1aaa-8aaa-aaaaaaaaaaaa"},
    {"created_at":True}, {"interactive_anchor":-1}, {"session_id":"/invalid"}])
def test_private_reference_has_closed_identity(fixture, change):
    original, _ = fixture
    selected = sc.ConsentSession(replace(original.observation,
        credential=replace(original.observation.credential, **change)))
    with pytest.raises(ValueError, match="consenting_session_unavailable"):
        selected.reference("owner")
    with pytest.raises(ValueError):
        original.reference("other")
    with pytest.raises(ValueError):
        sc.ConsentSession(object()).reference("owner")
