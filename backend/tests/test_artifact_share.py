"""Feature 055 (US5) — share-grant store unit tests (T042).

Exercises ``orchestrator.artifact_share.ShareGrantStore`` against the live
Postgres ``share_grant`` table and the REAL ``audit.hooks`` recording path
(no monkeypatched hooks):

* mint — token returned exactly once, only its SHA-256 stored, snapshot
  html+json immutably captured, ``share.minted`` audited;
* PHI gate FAIL-CLOSED at mint — prefilter hit, analyzer hit, and
  analyzer-unavailable all refuse with ``share.refused_phi`` and write no row;
* list — owner-scoped metadata only, never token/snapshot material;
* revoke — immediate (a revoked grant never resolves again), idempotent,
  owner-scoped, ``share.revoked`` audited once;
* resolve — uniform None for unknown/revoked/expired tokens;
* record_open — increments ``open_count`` and audits ``share.opened`` with
  principal ``share:<id>``.

Store methods run inside ``asyncio.run`` from sync test bodies; all DB
asserts happen off-loop, keeping the feature-052 loop guard satisfied.
Every test uses uuid-unique user ids and purges its own rows on teardown
(audit_events is append-only behind a trigger — purge requires the
``audit.allow_purge`` GUC, mirroring the retention CLI).
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("AUDIT_HMAC_SECRET", "pytest-audit-secret")
os.environ.setdefault("AUDIT_HMAC_KEY_ID", "k1")

from audit.recorder import Recorder, get_recorder, set_recorder  # noqa: E402
from audit.repository import AuditRepository  # noqa: E402
from orchestrator.artifact_share import (  # noqa: E402
    ShareGrantStore,
    SharePHIRefusedError,
    SharingDisabledError,
    get_share_store,
    hash_token,
    set_share_store,
)
from personalization.phi_gate import PHIGate, set_phi_gate  # noqa: E402
from shared.feature_flags import flags  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _CleanAnalyzer:
    """Presidio stand-in that never reports entities."""

    def analyze(self, text, language, entities, score_threshold):
        return []


class _HitAnalyzer:
    """Presidio stand-in that always reports a PHI entity."""

    def analyze(self, text, language, entities, score_threshold):
        return [{"entity_type": "PERSON"}]


CLEAN_SNAPSHOT = [{"type": "card", "title": "Quarterly revenue", "content": "Up and to the right"}]
CLEAN_HTML = '<div class="astral-component" data-component-id="wc_demo">Quarterly revenue</div>'
PHI_SNAPSHOT = [{"type": "card", "title": "Patient record", "content": "SSN 123-45-6789"}]


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("artifact_share") as runtime:
        yield runtime


@pytest.fixture()
def store(plane_runtime):
    return ShareGrantStore(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )


@pytest.fixture()
def recorder(plane_runtime, tmp_path):
    """A REAL Recorder over the live audit_events table, wired into the
    process-global slot that audit.hooks reads."""
    prev = get_recorder()
    rec = Recorder(
        AuditRepository(
            plane_runtime=plane_runtime,
            plane_repositories=plane_runtime.repositories,
        ),
        retry_queue=tmp_path / "audit-retry.jsonl",
    )
    set_recorder(rec)
    yield rec
    set_recorder(prev)


@pytest.fixture(autouse=True)
def sharing_enabled():
    prior = flags._flags.get("artifact_sharing")
    flags._flags["artifact_sharing"] = True
    yield
    flags._flags["artifact_sharing"] = prior


@pytest.fixture(autouse=True)
def clean_phi_gate():
    """Default every test to a gate whose analyzer reports no entities;
    individual tests override for hit / fail-closed paths."""
    set_phi_gate(PHIGate(analyzer=_CleanAnalyzer(), build_if_missing=False))
    yield
    set_phi_gate(None)


@pytest.fixture()
def user(plane_runtime):
    uid = f"pytest-share-{uuid.uuid4().hex[:12]}"
    yield uid
    with plane_runtime.transaction() as transaction:
        transaction.execute("DELETE FROM share_grant WHERE user_id = %s", (uid,))
        transaction.execute("SET LOCAL audit.allow_purge = 'true'")
        transaction.execute(
            "DELETE FROM audit_events WHERE actor_user_id = %s",
            (uid,),
        )


def _audit_rows(plane_runtime, user_id, action_type=None):
    with plane_runtime.transaction() as transaction:
        if action_type is not None:
            rows = transaction.fetch_all(
                "SELECT * FROM audit_events "
                "WHERE actor_user_id = %s AND action_type = %s "
                "ORDER BY recorded_at ASC, event_id ASC",
                (user_id, action_type),
            )
        else:
            rows = transaction.fetch_all(
                "SELECT * FROM audit_events WHERE actor_user_id = %s "
                "ORDER BY recorded_at ASC, event_id ASC",
                (user_id,),
            )
    return [_plain(row) for row in rows]


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _grant_rows(plane_runtime, user_id):
    with plane_runtime.transaction() as transaction:
        rows = transaction.fetch_all(
            "SELECT * FROM share_grant WHERE user_id = %s ORDER BY id ASC",
            (user_id,),
        )
    return [_plain(row) for row in rows]


def _mint(store, user_id, **overrides):
    kwargs = dict(
        user_id=user_id, chat_id="chat-share-test", scope="canvas",
        snapshot_html=CLEAN_HTML, snapshot_json=CLEAN_SNAPSHOT,
    )
    kwargs.update(overrides)
    return asyncio.run(store.mint(**kwargs))


# ---------------------------------------------------------------------------
# Mint
# ---------------------------------------------------------------------------


def test_mint_stores_hash_only_and_snapshots(plane_runtime, store, recorder, user):
    res = _mint(store, user)

    assert res["token"]
    assert res["share_url"] == f"/share/{res['token']}"
    assert res["id"] and res["created_at"] is not None
    assert res["expires_at"] is None

    rows = _grant_rows(plane_runtime, user)
    assert len(rows) == 1
    row = rows[0]
    # Only the digest is persisted — the raw token appears in no column.
    assert row["token_sha256"] == hash_token(res["token"])
    assert all(res["token"] not in str(v) for v in row.values())
    # Snapshot captured at mint (html verbatim, json round-tripped by JSONB).
    assert row["snapshot_html"] == CLEAN_HTML
    assert row["snapshot_json"] == CLEAN_SNAPSHOT
    assert row["scope"] == "canvas"
    assert row["open_count"] == 0
    assert row["revoked_at"] is None

    minted = _audit_rows(plane_runtime, user, "share.minted")
    assert len(minted) == 1
    assert minted[0]["event_class"] == "conversation"
    assert minted[0]["outcome"] == "success"
    assert minted[0]["inputs_meta"]["share_id"] == res["id"]
    assert minted[0]["inputs_meta"]["scope"] == "canvas"


def test_mint_component_scope_carries_component_id(plane_runtime, store, recorder, user):
    res = _mint(store, user, scope="component", component_id="wc_abc123")
    row = _grant_rows(plane_runtime, user)[0]
    assert row["scope"] == "component"
    assert row["component_id"] == "wc_abc123"
    minted = _audit_rows(plane_runtime, user, "share.minted")
    assert minted[0]["inputs_meta"]["component_id"] == "wc_abc123"
    assert res["id"] == row["id"]


def test_mint_argument_validation(store, user):
    with pytest.raises(ValueError):
        _mint(store, user, scope="everything")
    with pytest.raises(ValueError):
        _mint(store, user, scope="component")  # no component_id
    with pytest.raises(ValueError):
        _mint(store, user, snapshot_html="")


def test_mint_refused_when_flag_off(plane_runtime, store, user):
    flags._flags["artifact_sharing"] = False
    with pytest.raises(SharingDisabledError):
        _mint(store, user)
    assert _grant_rows(plane_runtime, user) == []


# ---------------------------------------------------------------------------
# PHI gate — fail-closed at mint
# ---------------------------------------------------------------------------


def test_mint_refuses_phi_prefilter_hit(plane_runtime, store, recorder, user):
    """An SSN in the snapshot trips the regex prefilter: no row, audited refusal."""
    with pytest.raises(SharePHIRefusedError):
        _mint(store, user, snapshot_json=PHI_SNAPSHOT)

    assert _grant_rows(plane_runtime, user) == []
    refused = _audit_rows(plane_runtime, user, "share.refused_phi")
    assert len(refused) == 1
    assert refused[0]["event_class"] == "conversation"
    assert refused[0]["outcome"] == "failure"
    # Refusal rows carry scope metadata but never snapshot content.
    assert refused[0]["inputs_meta"]["scope"] == "canvas"
    assert "123-45-6789" not in str(refused[0])
    assert _audit_rows(plane_runtime, user, "share.minted") == []


def test_mint_refuses_phi_analyzer_hit(plane_runtime, store, recorder, user):
    """Content clean of prefilter patterns still refuses when Presidio flags it."""
    set_phi_gate(PHIGate(analyzer=_HitAnalyzer(), build_if_missing=False))
    with pytest.raises(SharePHIRefusedError):
        _mint(store, user)
    assert _grant_rows(plane_runtime, user) == []
    assert len(_audit_rows(plane_runtime, user, "share.refused_phi")) == 1


def test_mint_fail_closed_when_analyzer_unavailable(plane_runtime, store, recorder, user):
    """No analyzer ⇒ clean content is still refused — never mint blind."""
    set_phi_gate(PHIGate(analyzer=None, build_if_missing=False))
    with pytest.raises(SharePHIRefusedError):
        _mint(store, user)
    assert _grant_rows(plane_runtime, user) == []
    assert len(_audit_rows(plane_runtime, user, "share.refused_phi")) == 1


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def test_list_grants_owner_scoped_metadata_only(store, recorder, user):
    a = _mint(store, user)
    b = _mint(store, user, scope="component", component_id="wc_xyz")

    grants = asyncio.run(store.list_grants(user))
    assert [g["id"] for g in grants] == sorted([a["id"], b["id"]], reverse=True)
    for g in grants:
        # Never token material or snapshot payloads (contract: GET /api/share).
        assert "token_sha256" not in g
        assert "snapshot_html" not in g and "snapshot_json" not in g
        assert set(g) == {"id", "chat_id", "scope", "component_id",
                          "created_at", "expires_at", "revoked_at", "open_count"}

    other = f"pytest-share-{uuid.uuid4().hex[:12]}"
    assert asyncio.run(store.list_grants(other)) == []


# ---------------------------------------------------------------------------
# Resolve / revoke / open-count
# ---------------------------------------------------------------------------


def test_resolve_serves_snapshot_and_refuses_unknown(store, recorder, user):
    res = _mint(store, user)
    grant = asyncio.run(store.resolve(res["token"]))
    assert grant is not None
    assert grant["snapshot_html"] == CLEAN_HTML
    assert _plain(grant["snapshot_json"]) == CLEAN_SNAPSHOT
    assert grant["user_id"] == user

    assert asyncio.run(store.resolve("not-a-real-token")) is None
    assert asyncio.run(store.resolve("")) is None


def test_resolve_refuses_expired(store, recorder, user):
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    res = _mint(store, user, expires_at=past)
    assert asyncio.run(store.resolve(res["token"])) is None

    future = datetime.now(timezone.utc) + timedelta(hours=1)
    res2 = _mint(store, user, expires_at=future)
    assert asyncio.run(store.resolve(res2["token"])) is not None


def test_revoke_is_immediate_owner_scoped_and_idempotent(
    plane_runtime, store, recorder, user
):
    res = _mint(store, user)

    # A stranger cannot revoke; the grant keeps serving.
    stranger = f"pytest-share-{uuid.uuid4().hex[:12]}"
    assert asyncio.run(store.revoke(stranger, res["id"])) is False
    assert asyncio.run(store.resolve(res["token"])) is not None

    assert asyncio.run(store.revoke(user, res["id"])) is True
    # Revoked grants never serve again.
    assert asyncio.run(store.resolve(res["token"])) is None
    first_revoked_at = _grant_rows(plane_runtime, user)[0]["revoked_at"]
    assert first_revoked_at is not None

    # Idempotent: second revoke succeeds, keeps the original timestamp,
    # and does not audit a second transition.
    assert asyncio.run(store.revoke(user, res["id"])) is True
    assert _grant_rows(plane_runtime, user)[0]["revoked_at"] == first_revoked_at
    revoked = _audit_rows(plane_runtime, user, "share.revoked")
    assert len(revoked) == 1
    assert revoked[0]["inputs_meta"]["share_id"] == res["id"]

    # Unknown id → False.
    assert asyncio.run(store.revoke(user, 999_999_999)) is False


def test_record_open_increments_and_audits_share_principal(
    plane_runtime, store, recorder, user
):
    res = _mint(store, user)
    grant = asyncio.run(store.resolve(res["token"]))

    asyncio.run(store.record_open(grant))
    asyncio.run(store.record_open(grant))

    assert _grant_rows(plane_runtime, user)[0]["open_count"] == 2
    opened = _audit_rows(plane_runtime, user, "share.opened")
    assert len(opened) == 2
    for row in opened:
        # Actor = share owner; principal identifies the grant, not a visitor.
        assert row["actor_user_id"] == user
        assert row["auth_principal"] == f"share:{res['id']}"
        assert row["event_class"] == "conversation"


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


def test_get_share_store_singleton_and_override(store):
    set_share_store(None)
    with pytest.raises(RuntimeError, match="not been bound"):
        get_share_store()
    try:
        set_share_store(store)
        assert get_share_store() is store
    finally:
        set_share_store(None)
