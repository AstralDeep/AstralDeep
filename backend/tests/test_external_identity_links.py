"""Signed direct-ORCID link handoff and preference projection coverage."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.requests import Request

from orchestrator.api import (
    complete_external_identity_link,
    start_external_identity_link,
)

from orchestrator.external_identity_links import (
    ASSERTION_TYPE,
    IdentityLinkError,
    claims_with_identity_preferences,
    create_link_state,
    decode_signed_payload,
    encode_signed_payload,
    linked_identity_from_preferences,
    parse_link_secrets,
    store_verified_identity,
    verify_link_handoff,
)

AGENT_ID = "panatlas-1"
PROVIDER = "orcid"
USER_ID = "astral-user-123"
ORCID = "0009-0003-6606-0831"
SECRET = b"a-production-strength-link-secret-12345"
NOW = 2_000_000_000


def _handoff() -> tuple[str, str]:
    state = create_link_state(
        agent_id=AGENT_ID,
        provider=PROVIDER,
        user_id=USER_ID,
        secret=SECRET,
        now=NOW,
    )
    assertion = encode_signed_payload(
        {
            "v": 1,
            "type": ASSERTION_TYPE,
            "agent_id": AGENT_ID,
            "provider": PROVIDER,
            "subject": ORCID,
            "issuer": "https://orcid.org",
            "state": state,
            "jti": "assertion-1",
            "iat": NOW,
            "exp": NOW + 120,
        },
        SECRET,
    )
    return state, assertion


def test_signed_handoff_is_bound_to_agent_provider_and_astral_user():
    state, assertion = _handoff()
    verified = verify_link_handoff(
        agent_id=AGENT_ID,
        provider=PROVIDER,
        user_id=USER_ID,
        state_token=state,
        assertion_token=assertion,
        secret=SECRET,
        now=NOW,
    )
    assert verified["subject"] == ORCID
    assert verified["state_nonce"]

    with pytest.raises(IdentityLinkError, match="does not match"):
        verify_link_handoff(
            agent_id=AGENT_ID,
            provider=PROVIDER,
            user_id="another-user",
            state_token=state,
            assertion_token=assertion,
            secret=SECRET,
            now=NOW,
        )


def test_tampered_and_expired_tokens_fail_closed():
    state, _assertion = _handoff()
    with pytest.raises(IdentityLinkError):
        decode_signed_payload(
            state[:-1] + ("A" if state[-1] != "A" else "B"),
            SECRET,
            expected_type="identity-link-state",
            now=NOW,
        )
    with pytest.raises(IdentityLinkError, match="Expired"):
        decode_signed_payload(
            state,
            SECRET,
            expected_type="identity-link-state",
            now=NOW + 301,
        )


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not-json",
        "[]",
        json.dumps({AGENT_ID: "short"}),
        json.dumps({" bad ": "x" * 32}),
    ],
)
def test_link_secret_configuration_fails_closed(raw):
    assert parse_link_secrets(raw) == {}


def test_saved_link_projects_only_through_internal_verified_identity_bucket():
    preferences = {
        "theme": "dark",
        "verified_external_identities": {
            "orcid": {
                "subject": ORCID,
                "issuer": "https://orcid.org",
                "verified_by_agent": AGENT_ID,
                "verified_at": NOW,
            }
        },
    }
    claims = claims_with_identity_preferences(preferences, {"sub": USER_ID})
    assert claims["sub"] == USER_ID
    assert claims["_verified_external_identities"]["orcid"]["subject"] == ORCID
    assert linked_identity_from_preferences(
        preferences, agent_id=AGENT_ID, provider=PROVIDER
    )["subject"] == ORCID
    assert linked_identity_from_preferences(
        preferences, agent_id="another-agent", provider=PROVIDER
    ) is None


class _Cursor:
    def __init__(self, db):
        self.db = db

    def execute(self, query, params=()):
        if query.startswith("SELECT user_id"):
            self.db.result = [dict(row) for row in self.db.rows]
        elif query.startswith("INSERT INTO user_preferences"):
            user_id, preferences, updated_at = params
            replacement = {
                "user_id": user_id,
                "preferences": preferences,
                "updated_at": updated_at,
            }
            self.db.rows = [r for r in self.db.rows if r["user_id"] != user_id]
            self.db.rows.append(replacement)

    def fetchall(self):
        return list(self.db.result)


class _Connection:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return _Cursor(self.db)

    def commit(self):
        self.db.commits += 1

    def rollback(self):
        self.db.rollbacks += 1


class _Database:
    def __init__(self):
        self.rows = [{
            "user_id": USER_ID,
            "preferences": json.dumps({"theme": "dark"}),
        }]
        self.result = []
        self.commits = 0
        self.rollbacks = 0

    def _borrow(self):
        return _Connection(self), False

    def _release(self, _connection, _pooled):
        return None

    def get_user_preferences(self, user_id):
        for row in self.rows:
            if row["user_id"] == user_id:
                return json.loads(row["preferences"])
        return {}


def test_store_preserves_preferences_and_rejects_state_replay():
    db = _Database()
    store_verified_identity(
        db,
        user_id=USER_ID,
        agent_id=AGENT_ID,
        provider=PROVIDER,
        subject=ORCID,
        issuer="https://orcid.org",
        state_nonce="nonce-1",
        now=NOW,
    )
    saved = json.loads(db.rows[0]["preferences"])
    assert saved["theme"] == "dark"
    assert saved["verified_external_identities"]["orcid"]["subject"] == ORCID

    with pytest.raises(IdentityLinkError, match="already used"):
        store_verified_identity(
            db,
            user_id=USER_ID,
            agent_id=AGENT_ID,
            provider=PROVIDER,
            subject=ORCID,
            issuer="https://orcid.org",
            state_nonce="nonce-1",
            now=NOW + 1,
        )
    assert db.rollbacks == 1


def _request_for(orchestrator) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "scheme": "https",
        "path": "/",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("sandbox.ai.uky.edu", 443),
        "app": SimpleNamespace(state=SimpleNamespace(orchestrator=orchestrator)),
    })


def _link_orchestrator(db=None):
    card = SimpleNamespace(
        agent_id=AGENT_ID,
        metadata={
            "external_identity": {
                "provider": PROVIDER,
                "authorization_url": "https://panatlas.net/link?flow=astral",
            }
        },
    )
    return SimpleNamespace(
        agent_cards={AGENT_ID: card},
        history=SimpleNamespace(db=db or _Database()),
        ui_sessions={},
    )


def test_api_start_and_callback_complete_the_direct_orcid_link(monkeypatch):
    monkeypatch.setenv("IDENTITY_CLAIM_TRUSTED_AGENTS", AGENT_ID)
    monkeypatch.setenv(
        "EXTERNAL_IDENTITY_LINK_SECRETS",
        json.dumps({AGENT_ID: SECRET.decode("ascii")}),
    )
    orch = _link_orchestrator()
    websocket = object()
    orch.ui_sessions[websocket] = {"sub": USER_ID}
    request = _request_for(orch)

    start_response = asyncio.run(start_external_identity_link(
        request,
        AGENT_ID,
        PROVIDER,
        user_id=USER_ID,
    ))
    assert start_response.status_code == 302
    state = parse_qs(urlsplit(start_response.headers["location"]).query)["state"][0]
    state_payload = decode_signed_payload(
        state, SECRET, expected_type="identity-link-state"
    )
    issued = state_payload["iat"]
    assertion = encode_signed_payload(
        {
            "v": 1,
            "type": ASSERTION_TYPE,
            "agent_id": AGENT_ID,
            "provider": PROVIDER,
            "subject": ORCID,
            "issuer": "https://orcid.org",
            "state": state,
            "jti": "api-assertion",
            "iat": issued,
            "exp": issued + 120,
        },
        SECRET,
    )
    callback_response = asyncio.run(complete_external_identity_link(
        request,
        AGENT_ID,
        PROVIDER,
        state,
        assertion,
        user_id=USER_ID,
    ))
    assert callback_response.status_code == 303
    assert callback_response.headers["location"] == "/?external_identity=orcid-linked"
    stored = orch.history.db.get_user_preferences(USER_ID)
    assert stored["verified_external_identities"]["orcid"]["subject"] == ORCID
    assert orch.ui_sessions[websocket]["_verified_external_identities"]["orcid"][
        "subject"
    ] == ORCID
