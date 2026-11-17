"""Runs every contacts vector through the reference module and pins the contracted outcome of each named case.
A failure means contacts.md, the committed vectors and the contacts reference disagree.
"""

import hashlib
import json
import re
from pathlib import Path

import contacts
import generate_contact_vectors as generator
import pytest
from astral_mesh_ref import cj, crypto, fold, testkit
from astral_mesh_ref.errors import MeshError

CONTRACTS = Path(__file__).resolve().parents[2]
DOC = json.loads((CONTRACTS / "vectors/contacts-v1.json").read_text(encoding="utf-8"))
CASES = {case["name"]: case for case in DOC["cases"]}
GID = {gid: f"g{n}" for n, gid in enumerate(generator.GIDS)}

KEY_OUTCOMES = {
    "key_contact": "contact",
    "key_mark": "mark",
    "key_grant": "grant",
    "key_revoked": "revoked",
    "key_other_setting": "other",
    "key_bare_contact_word": "other",
    "key_plural_prefix": "other",
    "key_shared_prefix": "other",
    "key_contact_uppercase_hex": "contact.malformed",
    "key_contact_63_hex": "contact.malformed",
    "key_contact_with_32_hex": "contact.malformed",
    "key_grant_with_64_hex": "share.malformed",
    "key_grant_uppercase_hex": "share.malformed",
    "key_revoked_trailing_slash": "share.malformed",
    "key_unknown_contact_family": "contact.malformed",
    "key_unknown_share_family": "share.malformed",
    "key_mark_with_32_hex": "contact.malformed",
}
RECORD_OUTCOMES = {
    "record_verified_valid": "ok",
    "record_unverified_valid": "ok",
    "record_name_64_valid": "ok",
    "record_name_with_space_valid": "ok",
    "record_heads_64_valid": "ok",
    "record_heads_65": "contact.malformed",
}
for _name in (
    "name_empty",
    "name_65",
    "name_bidi_override",
    "name_control",
    "name_not_text",
    "state_unknown",
    "version_2",
    "extra_field",
    "missing_field",
    "mesh_differs_from_key",
    "mesh_not_an_id",
    "heads_unsorted",
    "heads_duplicate",
    "heads_not_ids",
    "verified_without_heads",
    "verified_count_below_heads",
    "unverified_with_heads",
    "unverified_with_count",
    "count_negative",
    "count_not_integer",
    "ms_negative",
    "not_an_object",
):
    RECORD_OUTCOMES["record_" + _name] = "contact.malformed"

INVITE_OUTCOMES = {
    "invite_make_default_lifetime": "ok",
    "invite_make_lifetime_60_valid": "ok",
    "invite_make_lifetime_86400_valid": "ok",
    "invite_make_lifetime_59": "contact.invite_lifetime",
    "invite_make_lifetime_86401": "contact.invite_lifetime",
    "invite_valid_text": "ok",
    "invite_valid_link": "ok",
    "invite_valid_last_second": "ok",
    "invite_expired_at_expiry": "contact.invite_expired",
    "invite_expired_later": "contact.invite_expired",
    "invite_own_mesh": "contact.self",
    "invite_own_mesh_after_expiry_is_self": "contact.self",
    "invite_tampered_name_bad_signature": "contact.invite_bad_signature",
    "invite_tampered_signature": "contact.invite_bad_signature",
    "invite_high_s_signature": "contact.invite_bad_signature",
    "invite_signed_by_other_key_for_claimed_identity": "contact.invite_bad_signature",
}
for _name in (
    "device_not_matching_identity",
    "wrong_prefix",
    "link_wrong_host",
    "not_text",
    "empty_payload",
    "padded_base64",
    "not_base64",
    "spaced_json_not_canonical",
    "unsorted_json_not_canonical",
    "duplicate_key",
    "payload_not_an_object",
    "payload_not_utf8",
    "missing_addr",
    "missing_device",
    "missing_expires_s",
    "missing_identity",
    "missing_mesh",
    "missing_name",
    "missing_nonce",
    "missing_sig",
    "missing_v",
    "extra_field",
    "version_2",
    "nonce_15_bytes",
    "nonce_17_bytes",
    "name_empty",
    "name_65",
    "name_bidi_override",
    "addr_with_space",
    "addr_empty",
    "addr_1025",
    "expires_zero",
    "expires_not_integer",
    "mesh_not_an_id",
):
    INVITE_OUTCOMES["invite_" + _name] = "contact.invite_malformed"

CODE_OUTCOMES = {
    "code_same_nonce_twice": "contact.malformed",
    "code_same_mesh_both_sides": "contact.self",
    "code_nonce_15_bytes": "contact.malformed",
    "code_nonce_not_base64": "contact.malformed",
    "code_three_nonces": "contact.malformed",
}
ROSTER_OUTCOMES = {"roster_digest_no_genesis": "roster.no_genesis"}

V = "verified:Bea"
U = "unverified:Bea"


def folded(contacts_list: list[str], ignored: list[str] = ()) -> dict:
    return {"contacts": contacts_list, "ignored": sorted(ignored)}


CONTACT_OUTCOMES = {
    "fold_verified_with_consent": folded([V]),
    "fold_verified_by_pocket_device": folded([V]),
    "fold_verified_without_consent": folded([], ["contact.consent_missing"]),
    "fold_unverified_needs_no_consent": folded([U]),
    "fold_unverified_with_consent_is_malformed": folded([], ["contact.malformed"]),
    "fold_unverified_by_node_allowed": folded([U]),
    "fold_node_consent": folded([], ["contact.consent_node_device"]),
    "fold_satellite_consent_allowed": folded([V]),
    "fold_stale_window_600000_before": folded([V]),
    "fold_stale_window_600001_before": folded([], ["contact.consent_stale"]),
    "fold_stale_window_same_instant": folded([V]),
    "fold_stale_window_1_after": folded([], ["contact.consent_stale"]),
    "fold_consent_of_other_kind": folded([], ["contact.consent_wrong_action"]),
    "fold_consent_wrong_digest": folded([], ["contact.consent_wrong_action"]),
    "fold_consent_for_other_value": folded([], ["contact.consent_wrong_action"]),
    "fold_consent_tampered_signature": folded([], ["contact.consent_bad_signature"]),
    "fold_consent_high_s_signature": folded([], ["contact.consent_bad_signature"]),
    "fold_consent_unknown_device": folded([], ["contact.consent_unknown_device"]),
    "fold_consent_on_another_members_feed": folded([], ["contact.consent_foreign_feed"]),
    "fold_consent_malformed_object": folded([], ["contact.malformed"]),
    "fold_recarried_verified_put_on_another_feed": folded(
        [U], ["contact.consent_foreign_feed"] * 2
    ),
    "fold_recarried_on_same_feed_inside_window": folded([U], ["contact.consent_reused"]),
    "fold_recarried_after_window": folded([U], ["contact.consent_stale"]),
    "fold_downgrade_by_another_device": folded([U]),
    "fold_downgrade_then_new_verify": folded([V]),
    "fold_order_by_clock_not_arrival": folded(["verified:Newer"]),
    "fold_clock_tie_broken_by_feed": folded(["verified:Beta"]),
    "fold_removal": folded([]),
    "fold_put_after_removal": folded([U]),
    "fold_removal_by_removed_device_stands": folded([]),
    "fold_removal_by_stranger": folded([V], ["contact.writer_not_member"]),
    "fold_removal_with_consent_field": folded([V], ["contact.malformed"]),
    "fold_unverified_by_stranger": folded([], ["contact.writer_not_member"]),
    "fold_verifying_device_removed_older_put_stands": folded(
        [U], ["contact.consent_unknown_device"]
    ),
    "fold_own_mesh_refused": folded([], ["contact.self"]),
    "fold_three_contacts_sorted_by_mesh": folded([V, "verified:Cy", "unverified:Di"]),
    "fold_contact_cap_keeps_the_earliest": folded(
        [V, "verified:Cy"], ["contact.too_many"]
    ),
    "fold_scope_not_mesh": folded([], ["contact.malformed"]),
    "fold_labels_not_private_owner": folded([], ["contact.malformed"]),
    "fold_labels_public": folded([], ["contact.malformed"]),
    "fold_body_with_extra_field": folded([], ["contact.malformed"]),
    "fold_malformed_record_ignored": folded([], ["contact.malformed"]),
    "fold_malformed_key_ignored": folded([], ["contact.malformed"]),
    "fold_same_consent_twice": folded([V], ["contact.consent_reused"]),
    "fold_marks_merge_union_and_greatest_count": folded([]),
    "fold_marks_equal_count_latest_event_wins": folded([]),
    "fold_mark_from_node_refused": folded([], ["contact.writer_kind"]),
    "fold_mark_from_satellite_refused": folded([], ["contact.writer_kind"]),
    "fold_mark_from_stranger_refused": folded([], ["contact.writer_not_member"]),
    "fold_mark_from_removed_device_refused": folded([], ["contact.writer_not_member"]),
    "fold_mark_malformed_and_own_mesh": folded(
        [], ["contact.malformed", "contact.malformed", "contact.self"]
    ),
    "fold_mark_without_contact_record_kept": folded([V]),
}


def followed(follows: bool, reason: str | None = None, notices: list[str] = (), triggers: list[str] = (), dropped: list[str] = ()) -> dict:
    return {
        "follows": follows,
        "reason": reason,
        "notices": list(notices),
        "triggers": sorted(triggers),
        "dropped": sorted(dropped),
    }


REJECTED = "contact.roster_rejected"
FOLLOW_OUTCOMES = {
    "follow_unchanged": followed(True),
    "follow_entries_in_reverse_arrival_order": followed(True),
    "follow_valid_add_notice": followed(True, notices=["device_added"]),
    "follow_valid_removal_marks_removed": followed(True, notices=["device_removed"]),
    "follow_rename_is_silent": followed(True),
    "follow_recovery_key_add_notices": followed(True, notices=["device_added", "recovery_used"]),
    "follow_stop_and_resume_are_silent": followed(True),
    "follow_stranger_entry_changes_nothing": followed(True, dropped=["roster.unauthorized_author"]),
    "follow_stranger_entry_followed_by_a_valid_one_stays": followed(True),
    "follow_removal_built_on_a_stranger_entry_still_counts": followed(True, notices=["device_removed"]),
    "follow_chain_of_unfollowed_stranger_entries_is_dropped": followed(True, dropped=["roster.unauthorized_author"] * 2),
    "follow_unverifiable_bad_signature": followed(True, dropped=["roster.bad_signature"]),
    "follow_unverifiable_bad_presence_signature": followed(True, dropped=["roster.bad_presence_signature"]),
    "follow_unverifiable_author_removed": followed(True, dropped=["roster.author_removed"]),
    "follow_unverifiable_wrong_mesh": followed(True, dropped=["roster.wrong_mesh"]),
    "follow_unverifiable_extra_genesis": followed(True, dropped=["roster.extra_genesis"]),
    "follow_unverifiable_malformed_entry": followed(True, dropped=["roster.malformed"]),
    "follow_unverifiable_recovery_phi_epoch": followed(True, dropped=["roster.recovery_not_allowed"]),
    "follow_concurrent_adds_of_one_key_are_not_rejected": followed(True, notices=["device_added"] * 2),
    "follow_honest_rejection_after_a_voided_ancestor": followed(True, notices=["device_removed"]),
    "follow_trigger_with_later_valid_entries": followed(False, REJECTED, triggers=["roster.already_member"]),
    "follow_heads_missing_from_cache": followed(False, "contact.heads_missing"),
    "follow_no_genesis_in_cache": followed(False, "roster.no_genesis"),
    "follow_empty_cache": followed(False, "roster.no_genesis"),
    "follow_record_for_another_mesh": "contact.malformed",
    "follow_unverified_record": "contact.malformed",
}
for _code in (
    "already_member",
    "device_removed",
    "duplicate_key",
    "stop_not_rising",
    "attestation_bad_signature",
    "attestation_malformed",
):
    _full = ("attestation." if _code.startswith("attestation") else "roster.") + _code.removeprefix("attestation_")
    FOLLOW_OUTCOMES["follow_trigger_" + _code] = followed(False, REJECTED, triggers=[_full])
    FOLLOW_OUTCOMES[f"follow_trigger_{_code}_seen_at_verification_stays_verified"] = followed(True)
for _code in (
    "unknown_device",
    "cutoff_incomplete",
    "unknown_authority",
    "epoch_wrong_members",
    "not_phi_member",
    "unknown_epoch",
    "resume_not_stopped",
    "attestation_unknown_authority",
):
    FOLLOW_OUTCOMES["follow_honest_" + _code] = followed(True)

READER_OUTCOMES = {
    "reader_current_desktop": "ok",
    "reader_current_pocket": "ok",
    "reader_without_any_mark": "ok",
    "reader_satellite": "share.reader_kind",
    "reader_node": "share.reader_kind",
    "reader_removed_device": "share.device_removed",
    "reader_roster_lacks_a_named_head": "share.roster_stale",
    "reader_kind_checked_before_removal": "share.reader_kind",
    "reader_removed_checked_before_stale": "share.device_removed",
}
PROPOSE_OUTCOMES = {
    "share_propose_unverified_contact": "contact.unverified",
    "share_propose_unknown_contact": "contact.unverified",
    "share_propose_verified_contact": "ok",
}


def shared(
    effect: list[str] = (),
    waiting: int = 0,
    orphaned: int = 0,
    chat_missing: int = 0,
    revoked: list[str] = (),
    superseded: dict | None = None,
    ignored: list[str] = (),
) -> dict:
    return {
        "in_effect": list(effect),
        "waiting": waiting,
        "orphaned": orphaned,
        "chat_missing": chat_missing,
        "revoked": sorted(revoked),
        "superseded": superseded or {},
        "ignored": sorted(ignored),
    }


MALFORMED = ["share.malformed"]
SHARE_OUTCOMES = {
    "share_grant_in_effect": shared(["g0"]),
    "share_grant_by_pocket_device_with_phi_flag": shared(["g0"]),
    "share_grant_to_unverified_contact_waits": shared(waiting=1),
    "share_grant_to_unknown_contact_is_orphaned": shared(orphaned=1),
    "share_contact_downgraded_grant_waits": shared(waiting=1),
    "share_contact_reverified_grant_resumes": shared(["g0"]),
    "share_contact_removed_grant_orphaned": shared(orphaned=1),
    "share_chat_missing": shared(chat_missing=1),
    "share_revoked": shared(revoked=["g0"]),
    "share_revoked_then_put_again_stays_revoked": shared(revoked=["g0"], ignored=["share.id_reused"]),
    "share_revoke_ordered_before_the_put": shared(revoked=["g0"]),
    "share_revoke_by_removed_device_stands": shared(revoked=["g0"]),
    "share_revoke_by_node_needs_no_consent_in_the_fold": shared(revoked=["g0"]),
    "share_revoke_by_stranger": shared(["g0"], ignored=["share.writer_not_member"]),
    "share_revoke_of_an_id_never_granted": shared(["g0"], revoked=["g1"]),
    "share_revoke_malformed_values": shared(["g0"], ignored=MALFORMED * 3),
    "share_supersede": shared(["g1"], superseded={"g0": "g1"}),
    "share_revoking_the_newer_grant_keeps_the_older_dead": shared(revoked=["g1"], superseded={"g0": "g1"}),
    "share_supersede_chain_revoke_last": shared(revoked=["g2"], superseded={"g0": "g1", "g1": "g2"}),
    "share_superseder_without_consent_leaves_the_older_standing": shared(["g0"], ignored=["share.consent_missing"]),
    "share_supersedes_grant_for_other_contact": shared(["g0"], ignored=["share.supersedes_unknown"]),
    "share_supersedes_grant_for_other_chat": shared(["g0"], ignored=["share.supersedes_unknown"]),
    "share_supersedes_unknown_grant": shared(ignored=["share.supersedes_unknown"]),
    "share_supersedes_a_grant_ordered_after_it": shared(["g0"], ignored=["share.supersedes_unknown"]),
    "share_supersedes_itself": shared(ignored=MALFORMED),
    "share_supersede_a_revoked_grant_is_a_new_grant": shared(["g1"], revoked=["g0"], superseded={"g0": "g1"}),
    "share_grants_in_event_order": shared(["g0", "g1", "g2"]),
    "share_cap_keeps_the_earliest": shared(["g0", "g1"], ignored=["share.too_many"]),
    "share_grant_removal_is_malformed": shared(ignored=MALFORMED),
    "share_consent_missing": shared(ignored=["share.consent_missing"]),
    "share_consent_from_node": shared(ignored=["share.consent_node_device"]),
    "share_consent_of_contact_kind": shared(ignored=["share.consent_wrong_action"]),
    "share_consent_stale": shared(ignored=["share.consent_stale"]),
    "share_consent_at_window_edge": shared(["g0"]),
    "share_consent_on_another_members_feed": shared(ignored=["share.consent_foreign_feed"]),
    "share_consent_tampered_signature": shared(ignored=["share.consent_bad_signature"]),
    "share_consent_unknown_device": shared(ignored=["share.consent_unknown_device"]),
    "share_consent_from_removed_device": shared(ignored=["share.consent_unknown_device"]),
    "share_consent_for_other_value": shared(ignored=["share.consent_wrong_action"]),
    "share_consent_reused": shared(["g0"], ignored=["share.consent_reused", "share.consent_wrong_action"]),
    "share_recarried_grant_on_another_feed": shared(revoked=["g0"], ignored=["share.consent_foreign_feed"]),
    "share_same_id_first_valid_put_wins": shared(["g0"], ignored=["share.id_reused"]),
    "share_reput_of_a_superseded_id_is_ignored": shared(["g1"], superseded={"g0": "g1"}, ignored=["share.id_reused"]),
    "share_a_refused_put_does_not_take_the_id": shared(["g0"], ignored=["share.consent_missing"]),
    "share_contact_removed_and_verified_again_after_revocation": shared(revoked=["g0"]),
    "share_contact_removed_without_revocation_grant_returns": shared(["g0"]),
    "share_scope_not_mesh": shared(ignored=MALFORMED),
    "share_labels_public": shared(ignored=MALFORMED),
    "share_malformed_key": shared(ignored=MALFORMED),
    "share_item_kind_surface_reserved": shared(ignored=["share.item_kind_unsupported"]),
    "share_item_kind_artifact_reserved": shared(ignored=["share.item_kind_unsupported"]),
    "share_item_kind_file_reserved": shared(ignored=["share.item_kind_unsupported"]),
}
for _name in (
    "unknown",
    "chat_with_extra_field",
    "chat_not_an_id",
    "chat_missing_chat",
    "not_an_object",
):
    SHARE_OUTCOMES["share_item_" + _name] = shared(ignored=MALFORMED)
for _name in (
    "kind_edit",
    "phi_not_boolean",
    "extra_field",
    "version_2",
    "id_differs_from_key",
    "contact_not_an_id",
    "supersedes_not_hex",
    "ms_negative",
    "missing_field",
):
    SHARE_OUTCOMES["share_value_" + _name] = shared(ignored=MALFORMED)

CLASS_OUTCOMES = {
    "class_" + code.replace(".", "_"): kind
    for kind, codes in (
        ("unverifiable", contacts.UNVERIFIABLE_CODES),
        ("honest", contacts.HONEST_CODES),
        ("trigger", contacts.TRIGGER_CODES),
    )
    for code in codes
}
CLASS_OUTCOMES["class_roster_future_code"] = "honest"
CLASS_OUTCOMES["class_attestation_future_code"] = "honest"

OUTCOMES = {
    **{k: ("key", v) for k, v in KEY_OUTCOMES.items()},
    **{k: ("record", v) for k, v in RECORD_OUTCOMES.items()},
    **{k: ("invite", v) for k, v in INVITE_OUTCOMES.items()},
    **{k: ("code", v) for k, v in CODE_OUTCOMES.items()},
    **{k: ("roster", v) for k, v in ROSTER_OUTCOMES.items()},
    **{k: ("contact", v) for k, v in CONTACT_OUTCOMES.items()},
    **{k: ("follow", v) for k, v in FOLLOW_OUTCOMES.items()},
    **{k: ("reader", v) for k, v in READER_OUTCOMES.items()},
    **{k: ("propose", v) for k, v in PROPOSE_OUTCOMES.items()},
    **{k: ("share", v) for k, v in SHARE_OUTCOMES.items()},
    **{k: ("class", v) for k, v in CLASS_OUTCOMES.items()},
}
UNPINNED_HERE = {
    "code_base",
    "code_nonces_swapped_equal",
    "code_sides_swapped_equal",
    "code_changes_with_first_nonce",
    "code_changes_with_second_nonce",
    "code_changes_with_first_mesh",
    "code_changes_with_second_mesh",
    "code_changes_with_one_head",
    "code_changes_with_count",
    "code_head_order_irrelevant",
    "code_display_leading_zeros",
    "code_nonce_text_order_differs_from_byte_order",
    "roster_digest_plain",
    "roster_digest_drops_unverifiable_entry",
    "roster_digest_arrival_order_irrelevant",
    "roster_digest_duplicates_idempotent",
    "roster_digest_keeps_a_followed_unverifiable_entry",
    "coexistence_deployed_folds",
}


def summary(case: dict) -> object:
    expect = case["expect"]
    if "error" in expect:
        return expect["error"]
    ok = expect["ok"]
    return {
        "contact_fold": lambda: folded(
            [f"{c['state']}:{c['record']['name']}" for c in ok["contacts"]],
            list(ok["ignored"].values()),
        ),
        "share_fold": lambda: shared(
            [GID[i["id"]] for i in ok["in_effect"]],
            len(ok["waiting"]),
            len(ok["orphaned"]),
            len(ok["chat_missing"]),
            [GID[i] for i in ok["revoked"]],
            {GID[o]: GID[n] for o, n in ok["superseded"].items()},
            list(ok["ignored"].values()),
        ),
        "contact_follow": lambda: followed(
            ok["follows"],
            ok["reason"],
            [n["notice"] for n in ok["notices"]],
            list(ok["triggers"].values()),
            list(ok["dropped"].values()),
        ),
        "contact_key": lambda: ok["form"],
        "contact_classify": lambda: ok["class"],
    }.get(case["kind"], lambda: "ok")()


def test_documents_name_the_contract() -> None:
    assert DOC["format"] == "astral.vectors/v1"
    assert DOC["name"] == "contacts-v1"
    assert DOC["spec"] == "106/contracts/contacts.md"


def test_vectors_cover_exactly_the_named_cases() -> None:
    assert set(CASES) == set(OUTCOMES) | UNPINNED_HERE
    assert len(DOC["cases"]) == len(CASES)


@pytest.mark.parametrize("name", sorted(CASES))
def test_case_reproduces_its_committed_expectation(name: str) -> None:
    case = CASES[name]
    assert contacts.run(case["kind"], case["input"]) == case["expect"]


@pytest.mark.parametrize("name", sorted(OUTCOMES))
def test_case_has_the_contracted_outcome(name: str) -> None:
    assert summary(CASES[name]) == OUTCOMES[name][1]


@pytest.mark.parametrize(
    "name", sorted(n for n, c in CASES.items() if c["kind"] in ("contact_fold", "share_fold"))
)
def test_fold_ignores_the_order_events_arrive_in(name: str) -> None:
    case = CASES[name]
    events = case["input"]["events"]
    for order in (list(reversed(events)), events[1::2] + events[0::2]):
        assert contacts.run(case["kind"], {**case["input"], "events": order}) == case["expect"]


@pytest.mark.parametrize(
    "name", sorted(n for n, c in CASES.items() if c["kind"] == "contact_follow")
)
def test_follow_ignores_the_order_entries_arrive_in(name: str) -> None:
    case = CASES[name]
    entries = case["input"]["entries"]
    for order in (list(reversed(entries)), entries[1::2] + entries[0::2]):
        assert contacts.run("contact_follow", {**case["input"], "entries": order}) == case["expect"]


def test_generator_reproduces_the_committed_file() -> None:
    assert generator.render()["contacts-v1"] == (CONTRACTS / "vectors/contacts-v1.json").read_text(
        encoding="utf-8"
    )


def test_check_mode_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("check mode wrote a file")

    monkeypatch.setattr(Path, "write_text", refuse)
    generator.main(["--check"])


def _independent_code(nonces: list[str], sides: list[dict]) -> str:
    raw = sorted(cj.b64url_decode(n) for n in nonces)
    rows = sorted(
        (
            {
                "mesh": s["mesh"],
                "roster": crypto.sha256_id(
                    cj.canonical({"count": s["count"], "heads": sorted(s["heads"])})
                ),
            }
            for s in sides
        ),
        key=lambda r: r["mesh"],
    )
    document = {"nonces": [cj.b64url_encode(n) for n in raw], "sides": rows}
    data = b"astral.contact.code/v1\n" + cj.canonical(document)
    return hashlib.sha256(data).hexdigest()


def test_code_is_the_hash_of_the_contracted_bytes() -> None:
    for name, case in CASES.items():
        if case["kind"] != "contact_code" or "ok" not in case["expect"]:
            continue
        ok = case["expect"]["ok"]
        digest = _independent_code(case["input"]["nonces"], case["input"]["sides"])
        assert ok["code"] == digest, name
        number = int.from_bytes(bytes.fromhex(digest)[:8], "big") % 10**12
        assert ok["display"] == f"{number:012d}"[:4] + " " + f"{number:012d}"[4:8] + " " + f"{number:012d}"[8:]
        assert cj.b64url_decode(ok["qr"]) == bytes.fromhex(digest)
        assert re.fullmatch(r"[0-9]{4} [0-9]{4} [0-9]{4}", ok["display"])


def test_code_is_equal_on_both_sides_and_changes_with_each_input() -> None:
    base = CASES["code_base"]["expect"]["ok"]["code"]
    for same in ("code_nonces_swapped_equal", "code_sides_swapped_equal", "code_head_order_irrelevant"):
        assert CASES[same]["expect"]["ok"]["code"] == base
    differing = [
        "code_changes_with_first_nonce",
        "code_changes_with_second_nonce",
        "code_changes_with_first_mesh",
        "code_changes_with_second_mesh",
        "code_changes_with_one_head",
        "code_changes_with_count",
    ]
    codes = {CASES[n]["expect"]["ok"]["code"] for n in differing}
    assert len(codes) == len(differing) and base not in codes


def test_code_display_keeps_leading_zeros() -> None:
    ok = CASES["code_display_leading_zeros"]["expect"]["ok"]
    assert ok["display"].startswith("0000 ")
    assert len(ok["display"]) == 14


def test_nonce_order_is_the_byte_order_not_the_text_order() -> None:
    nonces = CASES["code_nonce_text_order_differs_from_byte_order"]["input"]["nonces"]
    assert sorted(nonces) != [cj.b64url_encode(n) for n in sorted(cj.b64url_decode(n) for n in nonces)]


def test_roster_digest_ignores_dropped_entries_and_order() -> None:
    plain = CASES["roster_digest_plain"]["expect"]["ok"]
    for name in (
        "roster_digest_drops_unverifiable_entry",
        "roster_digest_arrival_order_irrelevant",
        "roster_digest_duplicates_idempotent",
    ):
        ok = CASES[name]["expect"]["ok"]
        assert (ok["heads"], ok["count"], ok["digest"]) == (plain["heads"], plain["count"], plain["digest"])
    expected = crypto.sha256_id(cj.canonical({"count": plain["count"], "heads": plain["heads"]}))
    assert plain["digest"] == expected
    assert CASES["roster_digest_drops_unverifiable_entry"]["expect"]["ok"]["dropped"]


def test_a_followed_unverifiable_entry_stays_in_the_digest() -> None:
    kept = CASES["roster_digest_keeps_a_followed_unverifiable_entry"]["expect"]["ok"]
    plain = CASES["roster_digest_plain"]["expect"]["ok"]
    assert kept["dropped"] == {}
    assert kept["count"] == plain["count"] + 2
    assert kept["digest"] != plain["digest"]


def test_a_removal_built_on_a_stranger_entry_still_refuses_the_removed_device() -> None:
    ok = CASES["follow_removal_built_on_a_stranger_entry_still_counts"]["expect"]["ok"]
    bob = DOC["test_devices"]["bob"]["id"]
    assert ok["mark"]["removed"] == [bob]
    reader = {"device": bob, "kind": "pocket", "entries": ok["mark"]["heads"]}
    with pytest.raises(MeshError) as error:
        contacts.reader_check(ok["mark"], reader)
    assert error.value.code == "share.device_removed"


def test_a_grant_id_is_written_once() -> None:
    ok = CASES["share_reput_of_a_superseded_id_is_ignored"]["expect"]["ok"]
    assert [i["phi"] for i in ok["in_effect"]] == [True]
    assert list(ok["ignored"].values()) == ["share.id_reused"]


def test_invitation_forms_carry_the_same_canonical_bytes() -> None:
    ok = CASES["invite_valid_text"]["expect"]["ok"]
    assert ok["text"] == contacts.INVITE_TEXT + cj.b64url_encode(cj.canonical(ok["invite"]))
    assert ok["link"] == "https://astraldeep.github.io/contact/#" + ok["text"].removeprefix("astral-contact:v1:")
    assert CASES["invite_valid_link"]["expect"]["ok"] == ok
    assert ok["invite"]["expires_s"] - CASES["invite_valid_text"]["input"]["now_s"] > 0


def test_invitation_lifetime_is_the_creation_rule() -> None:
    made = CASES["invite_make_default_lifetime"]
    assert made["expect"]["ok"]["invite"]["expires_s"] == made["input"]["now_s"] + 1800
    low = CASES["invite_make_lifetime_60_valid"]
    assert low["expect"]["ok"]["invite"]["expires_s"] == low["input"]["now_s"] + 60


def test_marks_merge_the_union_of_removals_and_the_greatest_count() -> None:
    merged = next(iter(CASES["fold_marks_merge_union_and_greatest_count"]["expect"]["ok"]["marks"].values()))
    ids = DOC["test_devices"]
    assert merged["count"] == 5
    assert merged["removed"] == sorted([ids["dave"]["id"], ids["carol"]["id"]])
    tied = next(iter(CASES["fold_marks_equal_count_latest_event_wins"]["expect"]["ok"]["marks"].values()))
    assert (tied["count"], tied["removed"]) == (5, [ids["dave"]["id"]])
    assert len(tied["heads"]) == 2


def test_a_mark_survives_without_a_contact_record() -> None:
    ok = CASES["fold_mark_without_contact_record_kept"]["expect"]["ok"]
    assert len(ok["marks"]) == 1
    assert [c["record"]["name"] for c in ok["contacts"]] == ["Bea"]


def test_a_follow_result_carries_the_next_mark_and_digest() -> None:
    result = CASES["follow_valid_removal_marks_removed"]["expect"]["ok"]
    bob = DOC["test_devices"]["bob"]["id"]
    assert result["mark"]["removed"] == [bob]
    assert result["mark"]["heads"] == result["roster"]["heads"]
    assert result["roster"]["digest"] == crypto.sha256_id(
        cj.canonical({"count": result["roster"]["count"], "heads": result["roster"]["heads"]})
    )


def test_a_dropped_entry_leaves_heads_and_count_unchanged() -> None:
    for name in (
        "follow_stranger_entry_changes_nothing",
        "follow_unverifiable_bad_signature",
        "follow_unverifiable_wrong_mesh",
    ):
        case = CASES[name]
        assert case["expect"]["ok"]["roster"]["heads"] == case["input"]["record"]["heads"], name
        assert case["expect"]["ok"]["roster"]["count"] == case["input"]["record"]["count"], name


def test_the_three_classes_partition_the_live_roster_codes() -> None:
    live = {
        "roster.malformed",
        "roster.unauthorized_author",
        "roster.bad_signature",
        "roster.bad_presence_signature",
        "roster.recovery_not_allowed",
        "roster.author_removed",
        "roster.already_member",
        "roster.device_removed",
        "roster.duplicate_key",
        "roster.unknown_device",
        "roster.cutoff_incomplete",
        "roster.unknown_authority",
        "roster.epoch_wrong_members",
        "roster.not_phi_member",
        "roster.unknown_epoch",
        "roster.stop_not_rising",
        "roster.resume_not_stopped",
        "roster.extra_genesis",
        "roster.wrong_mesh",
        "attestation.malformed",
        "attestation.unknown_authority",
        "attestation.bad_signature",
        "attestation.expired",
    }
    classes = [
        set(contacts.UNVERIFIABLE_CODES),
        set(contacts.HONEST_CODES),
        set(contacts.TRIGGER_CODES),
    ]
    assert set().union(*classes) == live
    assert sum(len(c) for c in classes) == len(live)


def test_every_trigger_and_every_honest_code_has_a_follow_vector() -> None:
    for code in contacts.TRIGGER_CODES:
        slug = ("attestation_" if code.startswith("attestation") else "") + code.split(".")[1]
        assert "follow_trigger_" + slug in CASES, code
    for code in contacts.HONEST_CODES:
        slug = ("attestation_" if code.startswith("attestation") else "") + code.split(".")[1]
        if code == "attestation.expired":
            continue
        assert "follow_honest_" + slug in CASES, code


def test_a_trigger_downgrades_only_when_it_appeared_after_the_comparison() -> None:
    for code in ("already_member", "device_removed", "duplicate_key", "stop_not_rising"):
        after = CASES["follow_trigger_" + code]["expect"]["ok"]
        seen = CASES[f"follow_trigger_{code}_seen_at_verification_stays_verified"]["expect"]["ok"]
        assert after["downgrade"] == REJECTED and not after["follows"]
        assert seen["downgrade"] is None and seen["follows"]


def _codes_in(value: object) -> set[str]:
    if isinstance(value, str):
        return {value} if value.startswith(("contact.", "share.", "roster.")) else set()
    if isinstance(value, list):
        return set().union(set(), *(_codes_in(item) for item in value))
    if isinstance(value, dict):
        return set().union(set(), *(_codes_in(item) for item in value.values()))
    return set()


def test_the_document_code_table_matches_the_module() -> None:
    text = (CONTRACTS / "contacts.md").read_text()
    section = text.split("## 10. Codes", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| `")]
    listed = {
        code
        for row in rows
        for code in re.findall(r"`((?:contact|share|roster)\.[a-z0-9_.]+)`", row.split("|")[1])
    }
    assert listed == set(contacts.CODES)


def test_every_contract_code_is_exercised() -> None:
    seen = set()
    for case in DOC["cases"]:
        seen |= _codes_in(case["expect"])
    wanted = set(contacts.CODES) - {"share.contact_unverified"}
    assert wanted <= seen, sorted(wanted - seen)
    waiting = CASES["share_grant_to_unverified_contact_waits"]["expect"]["ok"]["waiting"]
    assert [w["code"] for w in waiting] == ["share.contact_unverified"]


def test_consent_checks_run_in_the_connection_order() -> None:
    ladder = [
        ("fold_consent_malformed_object", "malformed"),
        ("fold_consent_of_other_kind", "consent_wrong_action"),
        ("fold_consent_unknown_device", "consent_unknown_device"),
        ("fold_node_consent", "consent_node_device"),
        ("fold_consent_on_another_members_feed", "consent_foreign_feed"),
        ("fold_consent_tampered_signature", "consent_bad_signature"),
        ("fold_stale_window_600001_before", "consent_stale"),
        ("fold_same_consent_twice", "consent_reused"),
    ]
    for name, code in ladder:
        assert "contact." + code in CASES[name]["expect"]["ok"]["ignored"].values(), name


def test_the_consent_digest_is_the_object_id_of_the_value() -> None:
    case = CASES["fold_verified_with_consent"]
    event = case["input"]["events"][0]
    assert event["body"]["consent"]["digest"] == crypto.object_id(event["body"]["value"])
    assert event["body"]["consent"]["kind"] == "contact"
    grant = CASES["share_grant_in_effect"]["input"]["events"][-1]
    assert grant["body"]["consent"]["kind"] == "share"
    assert grant["body"]["consent"]["digest"] == crypto.object_id(grant["body"]["value"])


def test_equal_clock_goes_to_the_greater_feed() -> None:
    case = CASES["fold_clock_tie_broken_by_feed"]
    first, second = case["input"]["events"]
    assert first["clock"] == second["clock"]
    greater = max((first, second), key=fold.order_key)
    assert greater["body"]["value"]["name"] == "Beta"


def test_a_revoke_is_terminal_whatever_its_order() -> None:
    for name in (
        "share_revoked_then_put_again_stays_revoked",
        "share_revoke_ordered_before_the_put",
        "share_recarried_grant_on_another_feed",
    ):
        assert CASES[name]["expect"]["ok"]["in_effect"] == [], name


def test_a_superseded_grant_is_terminal_even_when_its_successor_is_revoked() -> None:
    for name in (
        "share_revoking_the_newer_grant_keeps_the_older_dead",
        "share_supersede_chain_revoke_last",
    ):
        ok = CASES[name]["expect"]["ok"]
        assert ok["in_effect"] == [] and ok["superseded"], name


def test_deployed_folds_agree_outside_the_reserved_keys() -> None:
    case = CASES["coexistence_deployed_folds"]
    events, keys = case["input"]["events"], case["input"]["presence_keys"]
    plain = [e for e in events if not contacts.is_event(e)]
    assert len(plain) < len(events)
    with_settings = fold.settings(events)
    outside = {k: v for k, v in with_settings.items() if not contacts.reserved_key(k)}
    assert outside == fold.settings(plain)
    assert any(contacts.reserved_key(k) for k in with_settings)
    assert fold.authority(events, keys) == fold.authority(plain, keys)
    assert case["expect"]["ok"] == {
        "authority": fold.authority(events, keys),
        "settings_with": with_settings,
        "settings_without": fold.settings(plain),
    }


def test_reserved_keys_cover_every_prefix() -> None:
    for key in ("contact/x", "contact.roster/x", "share/x", "share.revoked/x", "contact.other", "share.other"):
        assert contacts.reserved_key(key)
    for key in ("contacts/x", "shared/x", "theme", "contact"):
        assert not contacts.reserved_key(key)


def test_published_test_devices_are_the_public_labels() -> None:
    for name, device in DOC["test_devices"].items():
        label = device["label"]
        built = testkit.TestDevice(label)
        assert device["id"] == built.id, name
        assert int(device["presence_scalar_hex"], 16) == testkit.scalar(label + "/presence")
        assert int(device["identity_scalar_hex"], 16) == built.identity_scalar


def test_limits_default_to_the_contracted_caps() -> None:
    assert (contacts.MAX_CONTACTS, contacts.MAX_GRANTS) == (256, 1024)
    assert DOC["constants"]["max_contacts"] == 256
    assert DOC["constants"]["max_grants"] == 1024


def test_the_fold_reads_every_event_of_the_four_keys() -> None:
    assert contacts.RESERVED_PREFIXES == ("contact/", "contact.", "share/", "share.")
    assert contacts.parse_key("share.revoked/" + "0" * 32)["form"] == "revoked"
