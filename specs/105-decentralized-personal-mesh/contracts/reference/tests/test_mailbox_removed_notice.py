"""Checks authenticated self-removal notice retrieval without granting removed operations keys access to feeds or other notices.
The blind mailbox filters public notice targets; only the receiving roster can validate notice signatures and authority."""

import pytest

from astral_mesh_ref.cj import b64url_decode
from astral_mesh_ref.errors import MeshError
from astral_mesh_ref.mailbox import Mailbox
import generate_vectors as gv


@pytest.fixture
def box():
    mailbox = Mailbox(b64url_decode(gv.MBOX))
    statement = gv.mb_statement(gv.ALICE, gv.mb_members(gv.ALICE, gv.BOB, gv.CAROL), 1)
    mailbox.handle(gv.mb_request(gv.ALICE, "members", {"statement": statement}), gv.MBOX_T)
    return mailbox


def request(box, device, operation, body=None, *, elapsed=0, **kwargs):
    now = gv.MBOX_T + elapsed
    return box.handle(gv.mb_request(device, operation, body or {}, ts=now, **kwargs), now)


def notice(target, *, kind="remove", tag="own"):
    value = gv.mb_notice(gv.ALICE, kind=kind, tag=tag)
    value["body"] = {"device": target.id, "cutoff": {target.feed: 0}}
    return value


def remove_bob(box):
    return request(box, gv.ALICE, "members", {
        "statement": gv.mb_statement(gv.ALICE, gv.mb_members(gv.ALICE, gv.CAROL), 2),
    })


def test_removed_key_receives_only_its_own_remove_and_no_feed_or_epoch_data(box):
    own = notice(gv.BOB)
    for value in [own, notice(gv.CAROL, tag="other"), notice(gv.BOB, kind="stop", tag="stop"), notice(gv.BOB, kind="epoch", tag="epoch")]:
        request(box, gv.ALICE, "notice_put", {"notice": value})
    remove_bob(box)
    returned = request(box, gv.BOB, "notice_get")["notices"]
    assert len(returned) == 1 and returned[0]["notice"] == own


def test_own_notice_survives_collection_by_every_remaining_member(box):
    own = notice(gv.BOB)
    request(box, gv.ALICE, "notice_put", {"notice": own})
    remove_bob(box)
    assert request(box, gv.CAROL, "notice_get")["notices"][0]["notice"] == own
    assert request(box, gv.ALICE, "status")["notices"] == 1
    assert request(box, gv.BOB, "notice_get")["notices"][0]["notice"] == own
    assert request(box, gv.ALICE, "status")["notices"] == 0
    with pytest.raises(MeshError, match="mailbox.removed"):
        request(box, gv.BOB, "notice_get")


@pytest.mark.parametrize("operation,body", [
    ("fetch", {"after": 0, "limit": 1}),
    ("deposit", {"envelopes": [gv.mb_envelope(gv.BOB, 1, "denied")]}),
    ("notice_put", {"notice": notice(gv.BOB)}),
    ("status", {}),
])
def test_removed_key_stays_denied_for_every_other_operation(box, operation, body):
    remove_bob(box)
    with pytest.raises(MeshError, match="mailbox.removed"):
        request(box, gv.BOB, operation, body)


def test_unknown_key_cannot_fetch_notices_and_removed_key_cannot_change_the_target(box):
    remove_bob(box)
    with pytest.raises(MeshError, match="mailbox.not_a_member"):
        request(box, gv.MALLORY, "notice_get")
    with pytest.raises(MeshError, match="mailbox.malformed"):
        request(box, gv.BOB, "notice_get", {"target": gv.CAROL.feed})
    with pytest.raises(MeshError, match="mailbox.bad_signature"):
        request(box, gv.BOB, "notice_get", signer=gv.MALLORY)


def test_no_matching_notice_remains_removed_and_expiry_is_not_extended_by_reads(box):
    box.limits["notice_max_age_s"] = 5
    remove_bob(box)
    with pytest.raises(MeshError, match="mailbox.removed"):
        request(box, gv.BOB, "notice_get")
    request(box, gv.ALICE, "notice_put", {"notice": notice(gv.BOB)})
    assert len(request(box, gv.BOB, "notice_get", elapsed=1)["notices"]) == 1
    with pytest.raises(MeshError, match="mailbox.removed"):
        request(box, gv.BOB, "notice_get", elapsed=5)


def test_pending_removed_target_still_counts_against_notice_quota(box):
    box.limits["max_notices"] = 1
    request(box, gv.ALICE, "notice_put", {"notice": notice(gv.BOB)})
    remove_bob(box)
    request(box, gv.CAROL, "notice_get")
    with pytest.raises(MeshError, match="mailbox.quota_exceeded"):
        request(box, gv.ALICE, "notice_put", {"notice": notice(gv.CAROL, tag="next")})
