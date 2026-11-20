"""Property checks that the merge functions give one answer whatever order entries and events arrive in."""

from __future__ import annotations

import random

import scenarios as sc
from astral_mesh_ref import event, fold, roster
from astral_mesh_ref.testkit import TestDevice

ALICE, BOB, CAROL, DAVE = (
    TestDevice(n, k)
    for n, k in (
        ("alice", "desktop"),
        ("bob", "pocket"),
        ("carol", "desktop"),
        ("dave", "satellite"),
    )
)


def _summary(state: roster.RosterState) -> dict:
    keep = (
        "members",
        "removed",
        "void",
        "rejected",
        "stop",
        "epochs",
        "current_epoch",
        "issuers",
        "phi_members",
        "heads",
        "pending",
    )
    return {k: state[k] for k in keep}


def _busy_mesh() -> sc.Mesh:
    m = sc.Mesh("conv", ALICE)
    ab = m.add(ALICE, BOB, [m.mesh])
    dv = m.add(BOB, DAVE, [ab])
    m.remove(ALICE, BOB, [ab, dv], {BOB.feed: 1})
    m.add(BOB, CAROL, [ab])
    m.stop(ALICE, [ab], 1)
    m.epoch(ALICE, [ab], [ALICE, BOB], "x")
    return m


def test_roster_fold_is_independent_of_arrival_order() -> None:
    m = _busy_mesh()
    expected = _summary(roster.fold(m.entries, m.mesh))
    rng = random.Random(7)
    for _ in range(25):
        shuffled = list(m.entries)
        rng.shuffle(shuffled)
        assert _summary(roster.fold(shuffled, m.mesh)) == expected


def test_roster_fold_ignores_duplicates() -> None:
    m = _busy_mesh()
    assert _summary(roster.fold(m.entries + m.entries, m.mesh)) == _summary(
        roster.fold(m.entries, m.mesh)
    )


def test_roster_fold_grows_monotonically_with_known_ancestors() -> None:
    m = _busy_mesh()
    for cut in range(1, len(m.entries) + 1):
        state = roster.fold(m.entries[:cut], m.mesh)
        assert state["pending"] == []


def test_fold_authority_and_settings_are_order_independent() -> None:
    def ev(
        name: str,
        feed: str,
        seq: int,
        ms: int,
        type_: str,
        body: dict,
        after: list[str] | None = None,
    ) -> dict:
        from astral_mesh_ref import crypto

        return {
            "after": after or [],
            "body": body,
            "clock": {"ms": ms, "n": 0},
            "feed": feed,
            "id": crypto.sha256_id(name.encode()),
            "seq": seq,
            "type": type_,
        }

    key = {"agent": "a", "scope": "s", "tool": "t"}
    events = [
        ev("g", "aaa", 1, 10, "auth.grant", {**key, "widening": False}),
        ev("r", "bbb", 1, 11, "auth.revoke", key),
        ev("s1", "aaa", 2, 12, "settings.set", {"key": "k", "value": 1}),
        ev("s2", "bbb", 2, 12, "settings.set", {"key": "k", "value": 2}),
    ]
    rng = random.Random(3)
    expected = (fold.authority(events, {}), fold.settings(events))
    for _ in range(20):
        shuffled = list(events)
        rng.shuffle(shuffled)
        assert (fold.authority(shuffled, {}), fold.settings(shuffled)) == expected


def test_a_sealed_event_round_trips_with_random_nonces() -> None:
    import os

    from astral_mesh_ref import crypto

    epoch_id = crypto.sha256_id(b"epoch")
    epoch_key, content = os.urandom(32), os.urandom(32)
    ck = event.new_content_key_ref(os.urandom(16))
    envelope = event.seal_event(
        ALICE.ops_seed,
        seq=1,
        prev=None,
        after=[],
        clock={"ms": 5, "n": 0},
        scope="new",
        type="chat.create",
        labels={"confidentiality": "private", "integrity": "owner"},
        key=ck,
        content_key=content,
        body={"title": "t"},
        epoch_id=epoch_id,
        epoch_key=epoch_key,
        body_nonce=os.urandom(24),
        header_nonce=os.urandom(24),
    )
    header, body = event.open_event(envelope, {epoch_id: epoch_key}, {ck: content})
    assert body == {"title": "t"} and header["seq"] == 1


def test_feed_status_reports_member_removed_and_unknown() -> None:
    m = sc.Mesh("status", ALICE)
    ab = m.add(ALICE, BOB, [m.mesh])
    m.remove(ALICE, BOB, [ab], {BOB.feed: 4})
    state = roster.fold(m.entries, m.mesh)
    assert state.feed_status(ALICE.feed) == {"device": ALICE.id, "state": "member"}
    assert state.feed_status(BOB.feed) == {
        "cutoff": 4,
        "device": BOB.id,
        "state": "removed",
    }
    assert state.feed_status(CAROL.feed) == {"state": "unknown"}


def test_store_lists_after_events_it_has_not_seen() -> None:
    from astral_mesh_ref import crypto, sync

    missing = crypto.sha256_id(b"unseen")
    store = sync.EventStore()
    store.accept(
        sc.header(ALICE, 1, None, sc.BASE_MS + 1000, after=[missing]),
        {"state": "member"},
    )
    assert store.missing_after() == [missing]
