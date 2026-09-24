"""Tests for owner-issued framework credentials (orchestrator/framework_credentials.py)
over real Postgres: session-consent-gated issuance, one-time secrets,
scope/TTL/admission bounds, and resolve_bearer's tamper/expiry/ownership refusals.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

from audit.repository import AuditRepository
from orchestrator.framework_credentials import FrameworkCaller, FrameworkCredentialService
from orchestrator.session_store import WebSessionStore
from persistent_agents.models import AssignmentError
from persistent_agents.tests.test_engine_postgres import plane as plane
from tests.helpers.session_consent_088 import consent_from_store

runtime = plane


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-framework-credential-audit-key")


@pytest.fixture
def session(runtime):
    owner = str(uuid.uuid4())
    sid = uuid.uuid4().hex
    store = WebSessionStore(plane_runtime=runtime, plane_repositories=runtime.repositories)
    store.create(sid, user_id=owner, access_token="synthetic-access",
                 refresh_token="synthetic-refresh", hard_max_seconds=3600)
    yield store, owner, sid
    try:
        store.delete(sid)
    except Exception:
        pass


@pytest.fixture
def service(runtime):
    return FrameworkCredentialService(plane_runtime=runtime, audit=AuditRepository(plane_runtime=runtime))


def _caller(store, owner, sid):
    return consent_from_store(store, owner, sid).observation


def _issue(service, store, owner, sid, **overrides):
    fields = {
        "owner_id": owner,
        "caller": _caller(store, owner, sid),
        "name": "My SDK key",
        "scopes": ("operations.submit", "operations.read"),
        "expires_in_seconds": 3600,
        "max_admissions": 100,
    }
    fields.update(overrides)
    return service.issue(**fields)


def test_issue_requires_a_typed_session_consent_observation(service, session):
    store, owner, sid = session
    with pytest.raises(AssignmentError, match="framework_credential_authority_required"):
        service.issue(owner_id=owner, caller=None, name="x", scopes=("operations.read",),
                      expires_in_seconds=3600, max_admissions=1)


def test_bare_owner_id_with_a_foreign_sessions_observation_is_refused(service, session):
    store, owner, sid = session
    other_owner = str(uuid.uuid4())
    other_sid = uuid.uuid4().hex
    store.create(other_sid, user_id=other_owner, access_token="a", refresh_token="b",
                 hard_max_seconds=3600)
    foreign = _caller(store, other_owner, other_sid)
    with pytest.raises(AssignmentError, match="framework_credential_authority_required"):
        service.issue(owner_id=owner, caller=foreign, name="x", scopes=("operations.read",),
                      expires_in_seconds=3600, max_admissions=1)


def test_a_session_deleted_before_mint_refuses_issuance_and_persists_nothing(service, session, runtime):
    store, owner, sid = session
    caller = _caller(store, owner, sid)
    store.delete(sid)
    with pytest.raises(AssignmentError, match="framework_credential_authority_unavailable"):
        service.issue(owner_id=owner, caller=caller, name="x", scopes=("operations.read",),
                      expires_in_seconds=3600, max_admissions=1)
    assert service.list(owner_id=owner) == []
    with runtime.transaction() as tx:
        assert tx.fetch_one(
            "SELECT count(*) AS n FROM framework_credential WHERE owner_id=%s", (owner,))["n"] == 0


def test_issue_returns_a_view_row_and_a_matching_secret_never_persisted(service, session, runtime):
    store, owner, sid = session
    view, secret = _issue(service, store, owner, sid)
    assert secret.startswith("afk_")
    random_part = secret.rsplit(".", 1)[-1]
    assert view["prefix"] == f"afk_{random_part[:10]}"
    assert view["name"] == "My SDK key"
    assert view["scopes"] == ["operations.read", "operations.submit"]
    assert view["max_admissions"] == 100 and view["consumed_admissions"] == 0
    assert view["revoked"] is False and view["expired"] is False
    assert set(view) == {
        "credential_id", "revision", "name", "prefix", "scopes", "created_at", "expires_at",
        "consumed_admissions", "max_admissions", "revoked", "expired",
    }
    with runtime.transaction() as tx:
        row = tx.fetch_one("SELECT token_hash FROM framework_credential WHERE id=%s",
                           (view["credential_id"],))
        assert secret not in repr(row) and row["token_hash"] != secret


def test_issued_secret_never_reappears_in_list(service, session):
    store, owner, sid = session
    view, secret = _issue(service, store, owner, sid)
    rows = service.list(owner_id=owner)
    assert len(rows) == 1 and rows[0]["credential_id"] == view["credential_id"]
    assert secret not in repr(rows)


def test_issue_writes_exactly_one_auth_framework_issue_audit_row(service, session, runtime):
    store, owner, sid = session
    view, secret = _issue(service, store, owner, sid)
    with runtime.transaction() as tx:
        rows = tx.fetch_all(
            "SELECT action_type, actor_user_id FROM audit_events "
            "WHERE actor_user_id=%s AND action_type='auth.framework_issue'", (owner,))
    assert len(rows) == 1
    assert secret not in repr(rows)


def test_scopes_are_restricted_to_the_closed_framework_vocabulary(service, session):
    store, owner, sid = session
    with pytest.raises(AssignmentError, match="framework_credential_invalid"):
        _issue(service, store, owner, sid, scopes=("operations.submit", "sudo"))


@pytest.mark.parametrize("expires_in_seconds", [0, -1, 299, 90 * 86400 + 1])
def test_ttl_is_bounded_five_minutes_to_ninety_days(service, session, expires_in_seconds):
    store, owner, sid = session
    with pytest.raises(AssignmentError, match="framework_credential_invalid"):
        _issue(service, store, owner, sid, expires_in_seconds=expires_in_seconds)


@pytest.mark.parametrize("max_admissions", [0, -1, 10001])
def test_max_admissions_is_bounded_one_to_ten_thousand(service, session, max_admissions):
    store, owner, sid = session
    with pytest.raises(AssignmentError, match="framework_credential_invalid"):
        _issue(service, store, owner, sid, max_admissions=max_admissions)


def test_revoke_is_idempotent_and_owner_scoped(service, session):
    store, owner, sid = session
    view, _secret = _issue(service, store, owner, sid)
    revoked = service.revoke(owner_id=owner, credential_id=view["credential_id"])
    assert revoked["revoked"] is True
    again = service.revoke(owner_id=owner, credential_id=view["credential_id"])
    assert again["revoked"] is True

    foreign_owner = str(uuid.uuid4())
    with pytest.raises(AssignmentError, match="framework_credential_not_found"):
        service.revoke(owner_id=foreign_owner, credential_id=view["credential_id"])


def test_revoke_unknown_credential_is_not_found(service, session):
    store, owner, sid = session
    with pytest.raises(AssignmentError, match="framework_credential_not_found"):
        service.revoke(owner_id=owner, credential_id=str(uuid.uuid4()))


def test_resolve_bearer_round_trips_a_freshly_issued_token(service, session):
    store, owner, sid = session
    view, secret = _issue(service, store, owner, sid, scopes=("operations.submit", "operations.read"))
    caller = service.resolve_bearer(secret)
    assert isinstance(caller, FrameworkCaller)
    assert caller.owner_id == owner and caller.credential_id == view["credential_id"]
    assert caller.scopes == frozenset({"operations.submit", "operations.read"})
    assert caller.has_scope("operations.submit") and not caller.has_scope("agents.read")


@pytest.mark.parametrize("bad", [
    None, "", "not-a-token", "afk_", "afk_missing.dots", "afk_" + "x" * 3000,
    "afk_" + "a" * 20 + "." + "b" * 20 + ".short",
])
def test_resolve_bearer_refuses_malformed_or_implausible_tokens(service, bad):
    assert service.resolve_bearer(bad) is None


def test_resolve_bearer_refuses_a_revoked_credential(service, session):
    store, owner, sid = session
    view, secret = _issue(service, store, owner, sid)
    service.revoke(owner_id=owner, credential_id=view["credential_id"])
    assert service.resolve_bearer(secret) is None


def test_resolve_bearer_refuses_an_expired_credential(service, session, runtime):
    store, owner, sid = session
    view, secret = _issue(service, store, owner, sid, expires_in_seconds=300)
    with runtime.transaction() as tx:
        tx.execute(
            "UPDATE framework_credential SET "
            "created_at = clock_timestamp() - interval '2 hours', "
            "expires_at = clock_timestamp() - interval '1 hour' WHERE id=%s",
            (view["credential_id"],))
    assert service.resolve_bearer(secret) is None


def test_resolve_bearer_refuses_a_tampered_secret(service, session):
    store, owner, sid = session
    view, secret = _issue(service, store, owner, sid)
    tampered = secret[:-1] + ("a" if secret[-1] != "a" else "b")
    assert service.resolve_bearer(tampered) is None


def test_resolve_bearer_refuses_a_credential_belonging_to_a_different_owner_segment(service, session):
    store, owner, sid = session
    _view, secret = _issue(service, store, owner, sid)
    prefix, _cred, tail = secret[len("afk_"):].split(".", 2)
    forged = f"afk_{prefix}.{uuid.uuid4()}.{tail}"
    assert service.resolve_bearer(forged) is None


def test_consume_admission_charges_exactly_once_and_refuses_when_exhausted(service, session, runtime):
    store, owner, sid = session
    view, _secret = _issue(service, store, owner, sid, max_admissions=1)
    with runtime.transaction() as tx:
        record = service.consume_admission(tx, owner_id=owner, credential_id=view["credential_id"])
        assert record.consumed_admissions == 1
    with runtime.transaction() as tx:
        with pytest.raises(AssignmentError, match="credential_allowance_exhausted"):
            service.consume_admission(tx, owner_id=owner, credential_id=view["credential_id"])


def test_service_refuses_to_construct_without_a_real_audit_repository(runtime):
    with pytest.raises(ValueError):
        FrameworkCredentialService(plane_runtime=runtime, audit=None)
    with pytest.raises(ValueError):
        FrameworkCredentialService(plane_runtime=runtime, audit=object())
