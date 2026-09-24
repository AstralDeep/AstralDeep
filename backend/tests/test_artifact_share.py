"""Tests for orchestrator.artifact_share.py's ShareGrantStore against live Postgres
and audit.hooks, including contextual PHI decisions, public snapshot extraction,
analyzer failures, and owner-scoped link lifecycle.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

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
    _share_screen_text,
    get_share_store,
    hash_token,
    set_share_store,
)
from personalization.phi_gate import PHIGate, set_phi_gate  # noqa: E402
from shared.feature_flags import flags  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402


class _CleanAnalyzer:
    def analyze(self, text, language, entities, score_threshold):
        return []


class _HitAnalyzer:
    def analyze(self, text, language, entities, score_threshold):
        return [{"entity_type": "PERSON"}]


class _NameAnalyzer:
    def analyze(self, text, **kwargs):
        return [SimpleNamespace(entity_type="PERSON", start=match.start(), end=match.end(), score=0.85)
                for match in re.finditer("Jane Doe", text)]


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


def test_mint_stores_hash_only_and_snapshots(plane_runtime, store, recorder, user):
    res = _mint(store, user)

    assert res["token"]
    assert res["share_url"] == f"/share/{res['token']}"
    assert res["id"] and res["created_at"] is not None
    assert res["expires_at"] is None

    rows = _grant_rows(plane_runtime, user)
    assert len(rows) == 1
    row = rows[0]
    assert row["token_sha256"] == hash_token(res["token"])
    assert all(res["token"] not in str(v) for v in row.values())
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
        _mint(store, user, scope="component")
    with pytest.raises(ValueError):
        _mint(store, user, snapshot_html="")


def test_mint_refused_when_flag_off(plane_runtime, store, user):
    flags._flags["artifact_sharing"] = False
    with pytest.raises(SharingDisabledError):
        _mint(store, user)
    assert _grant_rows(plane_runtime, user) == []


def test_mint_refuses_phi_prefilter_hit(plane_runtime, store, recorder, user):
    with pytest.raises(SharePHIRefusedError):
        _mint(store, user, snapshot_json=PHI_SNAPSHOT)

    assert _grant_rows(plane_runtime, user) == []
    refused = _audit_rows(plane_runtime, user, "share.refused_phi")
    assert len(refused) == 1
    assert refused[0]["event_class"] == "conversation"
    assert refused[0]["outcome"] == "failure"
    assert refused[0]["inputs_meta"]["scope"] == "canvas"
    assert "123-45-6789" not in str(refused[0])
    assert _audit_rows(plane_runtime, user, "share.minted") == []


def test_mint_refuses_phi_analyzer_hit(plane_runtime, store, recorder, user):
    set_phi_gate(PHIGate(analyzer=_HitAnalyzer(), build_if_missing=False))
    with pytest.raises(SharePHIRefusedError):
        _mint(store, user)
    assert _grant_rows(plane_runtime, user) == []
    assert len(_audit_rows(plane_runtime, user, "share.refused_phi")) == 1


def test_mint_fail_closed_when_analyzer_unavailable(plane_runtime, store, recorder, user):
    set_phi_gate(PHIGate(analyzer=None, build_if_missing=False))
    with pytest.raises(SharePHIRefusedError):
        _mint(store, user)
    assert _grant_rows(plane_runtime, user) == []
    assert len(_audit_rows(plane_runtime, user, "share.refused_phi")) == 1


def test_list_grants_owner_scoped_metadata_only(store, recorder, user):
    a = _mint(store, user)
    b = _mint(store, user, scope="component", component_id="wc_xyz")

    grants = asyncio.run(store.list_grants(user))
    assert [g["id"] for g in grants] == sorted([a["id"], b["id"]], reverse=True)
    for g in grants:
        assert "token_sha256" not in g
        assert "snapshot_html" not in g and "snapshot_json" not in g
        assert set(g) == {"id", "chat_id", "scope", "component_id",
                          "created_at", "expires_at", "revoked_at", "open_count"}

    other = f"pytest-share-{uuid.uuid4().hex[:12]}"
    assert asyncio.run(store.list_grants(other)) == []


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

    stranger = f"pytest-share-{uuid.uuid4().hex[:12]}"
    assert asyncio.run(store.revoke(stranger, res["id"])) is False
    assert asyncio.run(store.resolve(res["token"])) is not None

    assert asyncio.run(store.revoke(user, res["id"])) is True
    assert asyncio.run(store.resolve(res["token"])) is None
    first_revoked_at = _grant_rows(plane_runtime, user)[0]["revoked_at"]
    assert first_revoked_at is not None

    assert asyncio.run(store.revoke(user, res["id"])) is True
    assert _grant_rows(plane_runtime, user)[0]["revoked_at"] == first_revoked_at
    revoked = _audit_rows(plane_runtime, user, "share.revoked")
    assert len(revoked) == 1
    assert revoked[0]["inputs_meta"]["share_id"] == res["id"]

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
        assert row["actor_user_id"] == user
        assert row["auth_principal"] == f"share:{res['id']}"
        assert row["event_class"] == "conversation"


def test_get_share_store_singleton_and_override(store):
    set_share_store(None)
    with pytest.raises(RuntimeError, match="not been bound"):
        get_share_store()
    try:
        set_share_store(store)
        assert get_share_store() is store
    finally:
        set_share_store(None)


def test_dashboard_dates_metrics_chart_configuration_and_metadata_are_shareable(
    plane_runtime, store, recorder, user
):
    snapshot = [{
        "type": "card", "title": "Lexington weather and research outlook",
        "component_id": "dash_1234567890", "css": {"color": "#12345678"},
        "_renderer": {"digest": "1234567890"}, "provenance": "grounded",
        "children": [{"type": "table", "columns": ["Date", "Population", "Rainfall"],
                      "rows": [["2026-09-23", 12345678, 72.1234567], ["09/24/2026", None, True]]}],
        "data": {"labels": ["Ada Lovelace", "Kentucky"], "values": [1234567890]},
    }]
    html = '<style>.card{color:#12345678}</style><h1>Lexington weather</h1><p>2026-09-23: 12345678</p>'
    minted = _mint(store, user, snapshot_json=snapshot, snapshot_html=html)
    row = _grant_rows(plane_runtime, user)[0]
    assert row["snapshot_json"] == snapshot
    assert row["snapshot_html"] == html
    assert asyncio.run(store.resolve(minted["token"]))["snapshot_html"] == html
    assert len(_audit_rows(plane_runtime, user, "share.minted")) == 1


@pytest.mark.parametrize("html", [
    "<div>SSN 123-<strong>45</strong>-6789</div>",
    '<a href="https://example.org/?mrn%3D0099123">Record</a>',
    '<img src="https://example.org/?contact=jane%40example.com" alt="Diagram">',
    '<div data-context="MRN: A00123">Summary</div>',
    '<div title="DOB: 1980-04-12">Summary</div>',
    '<div style="background-image:url(https://example.org/?mrn=0099123)">Summary</div>',
    '<style>.card{background:url(https://example.org/?mrn=0099123)}</style>Summary',
])
def test_public_markup_identifiers_are_refused_even_when_structured_content_is_clean(
    plane_runtime, store, recorder, user, html
):
    with pytest.raises(SharePHIRefusedError):
        _mint(store, user, snapshot_html=html)
    assert _grant_rows(plane_runtime, user) == []
    refusal = _audit_rows(plane_runtime, user, "share.refused_phi")
    assert len(refusal) == 1
    assert "0099123" not in str(refusal)
    assert "123-45-6789" not in str(refusal)
    assert "jane" not in str(refusal)


def test_screening_excludes_component_metadata_but_keeps_displayed_rows_and_attributes():
    snapshot = [{"type": "table", "component_id": "private-renderer-id", "css": {"color": "#fff"},
                 "_digest": "private-renderer-digest", "rows": [{"id": "MRN: A01234"}],
                 "content": "<b>Visible</b>", "values": (True, 5.25, None)}]
    text = _share_screen_text(snapshot, '<table class="layout" id="internal"><tr><td>Visible</td></tr></table>')
    assert "private-renderer" not in text
    assert "#fff" not in text
    assert "MRN: A01234" in text
    assert "Visible" in text
    assert "5.25" in text
    assert "layout" not in text
    assert "internal" not in text


@pytest.mark.parametrize("snapshot,html", [
    ([{"type": "card", "data": object()}], CLEAN_HTML),
    ([{"type": "card", 12: "invalid key"}], CLEAN_HTML),
    (CLEAN_SNAPSHOT, "<script>unsafe()</script>"),
    (CLEAN_SNAPSHOT, "<style>.card{display:none}"),
    (CLEAN_SNAPSHOT, 42),
    (CLEAN_SNAPSHOT, "x" * (8 * 1024 * 1024 + 1)),
])
def test_unscannable_snapshots_fail_closed_with_audit(
    plane_runtime, store, recorder, user, snapshot, html
):
    with pytest.raises(SharePHIRefusedError):
        _mint(store, user, snapshot_json=snapshot, snapshot_html=html, scope="component", component_id="wc_test")
    assert _grant_rows(plane_runtime, user) == []
    refusal = _audit_rows(plane_runtime, user, "share.refused_phi")
    assert len(refusal) == 1
    assert refusal[0]["inputs_meta"]["component_id"] == "wc_test"


def test_recursive_or_excessive_snapshot_data_is_refused():
    recursive = []
    recursive.append(recursive)
    for snapshot in (recursive, [None] * 50_001):
        with pytest.raises(ValueError, match="share_screen_limit"):
            _share_screen_text(snapshot, CLEAN_HTML)


def test_analyzer_error_refuses_mint_without_persistence(plane_runtime, store, recorder, user):
    class UnavailableAnalyzer:
        def analyze(self, **kwargs):
            raise RuntimeError("screen unavailable")

    set_phi_gate(PHIGate(analyzer=UnavailableAnalyzer()))
    with pytest.raises(SharePHIRefusedError):
        _mint(store, user)
    assert _grant_rows(plane_runtime, user) == []
    assert len(_audit_rows(plane_runtime, user, "share.refused_phi")) == 1


@pytest.mark.parametrize("snapshot,html", [
    ([{"type": "table", "headers": ["Patient name", "Age"], "rows": [["Jane Doe", 42]]}], CLEAN_HTML),
    ([{"type": "table", "headers": ["Name", "Diagnosis"], "rows": [["Jane Doe", "asthma"]]}], CLEAN_HTML),
    ([{"type": "table", "rows": [{"name": "Jane Doe", "medication": "insulin"}]}], CLEAN_HTML),
    (CLEAN_SNAPSHOT, "<table><tr><th>Patient name</th><th>Age</th></tr><tr><td>Jane <b>Doe</b></td><td>42</td></tr></table>"),
    (CLEAN_SNAPSHOT, "<table><tr><th>Name</th><th>Diagnosis</th></tr><tr><td>Jane Doe</td><td>asthma</td></tr></table>"),
])
def test_patient_tables_preserve_identity_context(plane_runtime, store, recorder, user, snapshot, html):
    set_phi_gate(PHIGate(analyzer=_NameAnalyzer()))
    with pytest.raises(SharePHIRefusedError):
        _mint(store, user, snapshot_json=snapshot, snapshot_html=html)
    assert _grant_rows(plane_runtime, user) == []
    assert len(_audit_rows(plane_runtime, user, "share.refused_phi")) == 1


def test_research_author_table_remains_shareable(store, recorder, user):
    set_phi_gate(PHIGate(analyzer=_NameAnalyzer()))
    snapshot = [{"type": "table", "headers": ["Author", "Diagnosis studied"],
                 "rows": [["Jane Doe", "asthma"]]}]
    html = "<table><tr><th>Author</th><th>Diagnosis studied</th></tr><tr><td>Jane Doe</td><td>asthma</td></tr></table>"
    assert _mint(store, user, snapshot_json=snapshot, snapshot_html=html)["share_url"].startswith("/share/")
