"""Checks that each removed credential door stays refused: no session cookie, refresh exchange, member token, durable consent or session store write path remains.
It drives the surviving authority seams directly, without a database, and pairs with the route-level denials in test_node_route_census.py and test_node_bearer_refusal.py.
"""

from __future__ import annotations

import asyncio
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import HTTPConnection

from orchestrator import identity_display, projects
from orchestrator.chain_authority import MACHINE_TURN_CLASSES, AuthoritySkip, MachineTurnAuthority
from orchestrator.orchestrator import Orchestrator
from orchestrator.remote_effect_execution import run_qualified, select_effect
from orchestrator.session_store import WebSessionStore
from orchestrator.work_reconciliation import ReconciliationRefused
from orchestrator.work_submit_authority import SESSION_COOKIE, refuse_session_cookie
from persistent_agents.models import AssignmentError


def _connection(cookie: str | None) -> HTTPConnection:
    headers = [] if cookie is None else [(b"cookie", cookie.encode())]
    return HTTPConnection({"type": "http", "headers": headers})


def test_a_session_cookie_is_refused_and_no_cookie_passes():
    with pytest.raises(AssignmentError) as refused:
        refuse_session_cookie(_connection(f"theme=dark; {SESSION_COOKIE}=value.signature"))
    assert refused.value.status_code == 401
    refuse_session_cookie(_connection(None))
    refuse_session_cookie(_connection("theme=dark"))


def _host(session):
    websocket = object()
    host = SimpleNamespace(
        agent_cards={"agent": SimpleNamespace(skills=[])},
        ui_sessions={websocket: session},
        delegation=SimpleNamespace(exchange_token_for_agent=AsyncMock(side_effect=AssertionError("no RFC 8693 exchange"))),
        _mint_mcp_delegation_token=AsyncMock(side_effect=AssertionError("no member token mint")),
    )
    method = types.MethodType(Orchestrator._get_delegation_token, host)
    return host, websocket, method


def test_a_session_carrying_an_identity_server_token_gets_no_exchange_and_no_delegation(monkeypatch):
    monkeypatch.delenv("ASTRAL_NODE_PROFILE", raising=False)
    session = {"sub": "user", "_raw_token": "bearer-from-an-identity-server"}
    host, websocket, method = _host(session)
    assert asyncio.run(method(websocket, "agent", "user")) is None
    assert session["_delegation_fault"] == "session_auth_unavailable"
    host.delegation.exchange_token_for_agent.assert_not_awaited()


def test_a_session_with_a_mesh_member_claim_gets_no_delegation():
    session = {"sub": "user", "astral_mesh": {"member_id": "m"}}
    host, websocket, method = _host(session)
    assert asyncio.run(method(websocket, "agent", "user")) is None
    host._mint_mcp_delegation_token.assert_not_awaited()


def test_the_refresh_frame_is_not_a_control_and_no_renewal_handler_remains():
    assert Orchestrator._ui_control_kind({"type": "refresh_ui_auth"}) is None
    for name in ("_handle_ui_auth_refresh", "_ui_auth_renewal_service", "_await_ui_auth_refresh"):
        assert not hasattr(Orchestrator, name)


@pytest.mark.parametrize("turn_class", MACHINE_TURN_CLASSES)
def test_no_machine_turn_class_can_derive_authority_without_a_durable_consent(turn_class):
    result = asyncio.run(MachineTurnAuthority(SimpleNamespace()).derive(
        user_id="user", agent_id="agent", consented_scopes=["tools:read"], grant_id="grant",
        turn_class=turn_class))
    assert isinstance(result, AuthoritySkip) and result.reason == "missing_consent"


def test_remote_mutators_are_refused_without_a_session_authority_and_other_tools_pass():
    with pytest.raises(ReconciliationRefused) as refused:
        asyncio.run(select_effect(None, None, "remote-compute-1", "make_directory", {}, "owner"))
    assert refused.value.code == "effect_session_required"
    assert asyncio.run(select_effect(None, None, "weather-1", "get_weather", {}, "owner")) is None
    target = SimpleNamespace(label="machine")
    result = run_qualified(None, target, ["rm", "-rf", "/"], tool="remove_path", owner_id="owner", timeout=1.0)
    assert result.verdict.name == "UNATTENDED_REFUSED" and result.retryable is False


def test_the_session_store_can_only_read_fences_and_never_issue_or_refresh_a_credential():
    for name in ("create", "acreate", "get", "update_tokens", "refresh_credential", "refresh_for_execution",
                 "latest_refresh_token_for", "enqueue_revocation", "mark_resumed", "verify_native_custody"):
        assert not hasattr(WebSessionStore, name), name


def test_chrome_identity_names_a_guest_without_roles_and_never_an_email():
    assert identity_display.identity_from_claims({"preferred_username": "a@example.org"}) == {
        "name": "Signed in", "role": "Guest", "initials": "SI"}
    assert identity_display.identity_from_claims(
        {"given_name": "Ada", "family_name": "Lovelace"}, ["admin"]) == {
        "name": "Ada Lovelace", "role": "Administrator", "initials": "AL"}
    assert identity_display.identity_from_claims(None)["role"] == "Guest"


def test_project_cursors_are_signed_with_a_fresh_random_process_key():
    assert isinstance(projects._CURSOR_KEY, bytes) and len(projects._CURSOR_KEY) == 32
