"""Signed direct-ORCID link handoff and preference projection coverage."""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from astralplane.repositories.identity import (
    ExternalIdentityLinkRecord,
    ExternalIdentityNonceReplayError,
)
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
    public_user_preferences,
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
    assert public_user_preferences(preferences) == {"theme": "dark"}


class _IdentityRepository:
    def __init__(self):
        self.records = {}
        self.nonces = set()

    def store_verified_external_identity(self, _transaction, **values):
        nonce = (values["owner_id"], values["provider"], values["state_nonce"])
        if nonce in self.nonces:
            raise ExternalIdentityNonceReplayError("state nonce was already used")
        self.nonces.add(nonce)
        record = ExternalIdentityLinkRecord(
            owner_id=values["owner_id"],
            agent_id=values["agent_id"],
            provider=values["provider"],
            subject=values["subject"],
            issuer=values["issuer"],
            verified_at=values["observed_at"],
        )
        self.records[(record.owner_id, record.provider)] = record
        return record

    def list_external_identities(self, _transaction, *, owner_id, limit):
        return tuple(
            record
            for (record_owner, _), record in sorted(self.records.items())
            if record_owner == owner_id
        )[:limit]


class _Runtime:
    @contextmanager
    def transaction(self):
        yield object()


def _plane_boundary():
    repository = _IdentityRepository()
    runtime = _Runtime()
    repositories = SimpleNamespace(identity=repository)
    return runtime, repositories


def test_store_uses_typed_plane_boundary_and_rejects_state_replay():
    runtime, repositories = _plane_boundary()
    store_verified_identity(
        None,
        user_id=USER_ID,
        agent_id=AGENT_ID,
        provider=PROVIDER,
        subject=ORCID,
        issuer="https://orcid.org",
        state_nonce="nonce-1",
        now=NOW,
        plane_runtime=runtime,
        plane_repositories=repositories,
    )
    assert repositories.identity.records[(USER_ID, PROVIDER)].subject == ORCID

    with pytest.raises(IdentityLinkError, match="already used"):
        store_verified_identity(
            None,
            user_id=USER_ID,
            agent_id=AGENT_ID,
            provider=PROVIDER,
            subject=ORCID,
            issuer="https://orcid.org",
            state_nonce="nonce-1",
            now=NOW + 1,
            plane_runtime=runtime,
            plane_repositories=repositories,
        )


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


def _link_orchestrator():
    card = SimpleNamespace(
        agent_id=AGENT_ID,
        metadata={
            "external_identity": {
                "provider": PROVIDER,
                "authorization_url": (
                    "https://panatlas.net/?panatlas_astral_orcid_link=1"
                ),
            }
        },
    )
    runtime, repositories = _plane_boundary()
    return SimpleNamespace(
        agent_cards={AGENT_ID: card},
        runtime_composition=SimpleNamespace(
            plane=SimpleNamespace(runtime=runtime, repositories=repositories)
        ),
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
    destination = urlsplit(start_response.headers["location"])
    query = parse_qs(destination.query)
    assert destination.scheme == "https"
    assert destination.netloc == "panatlas.net"
    assert destination.path == "/"
    assert query["panatlas_astral_orcid_link"] == ["1"]
    state = query["state"][0]
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
    stored = orch.runtime_composition.plane.repositories.identity.records[
        (USER_ID, PROVIDER)
    ]
    assert stored.subject == ORCID
    assert orch.ui_sessions[websocket]["_verified_external_identities"]["orcid"][
        "subject"
    ] == ORCID
