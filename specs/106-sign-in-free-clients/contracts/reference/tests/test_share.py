"""Runs every share session vector through the reference module and pins the contracted outcome of each named case.
A failure means share.md, the committed vectors and the share reference disagree.
"""

import hashlib
import hmac
import json
import re
from pathlib import Path

import contacts
import generate_share_vectors as generator
import pytest
import share
from astral_mesh_ref import cj, crypto, event
from astral_mesh_ref.errors import MeshError

CONTRACTS = Path(__file__).resolve().parents[2]
DOC = json.loads((CONTRACTS / "vectors/share-v1.json").read_text(encoding="utf-8"))
CASES = {case["name"]: case for case in DOC["cases"]}
TEXT = (CONTRACTS / "share.md").read_text(encoding="utf-8")
SERVED = (
    "chat.create",
    "chat.user_message",
    "chat.assistant_message",
    "chat.spoken",
    "chat.attachment",
    "chat.turn_start",
    "chat.turn_end",
    "chat.turn_outcome_unknown",
    "ui.surface_put",
    "ui.patch",
    "chat.tool_result",
)
NAMES = {generator.eid(v): k for k, v in generator.EV.items() if k != "never"}
NAMES.update({generator.eid(v): "never/" + k for k, v in generator.EV["never"].items()})
NAMES[generator.CHAT] = "create"
ALWAYS_SERVED = {
    "create", "user", "turn_start", "tr_bound", "tr_patch", "tr_source", "surface", "patch",
    "surface_source", "assistant", "spoken", "attachment", "turn_end", "unknown_outcome",
}
PHI_NAMES = {"phi_message", "phi_attachment"}


def result(name: str) -> dict:
    return CASES[name]["expect"]


def code_of(name: str) -> str:
    expect = result(name)
    return expect["error"] if "error" in expect else "ok"


def served(name: str) -> set[str]:
    return {NAMES[i] for i in result(name)["ok"]["served"]}


def refused(name: str) -> dict[str, str]:
    return {NAMES[i]: c for i, c in result(name)["ok"]["refused"].items()}


MESSAGE_CODES = {"roster_want_257_ids": "share.too_large", "roster_257_entries": "share.too_large"}
for _n in ("grants_want_257", "want_65_ranges", "want_span_513", "chunks_want_257"):
    MESSAGE_CODES[_n] = "share.too_large"
MESSAGE_CODES["grants_65_wraps_too_many_keys"] = "share.too_many_keys"


def message_outcome(name: str) -> str:
    bare = name.removeprefix("message_")
    if bare.startswith("valid_") or bare.endswith("_valid"):
        return "ok"
    return MESSAGE_CODES.get(bare, "share.malformed")


FRAME = {
    "frame_roundtrip": "ok",
    "unframe_valid": "ok",
    "unframe_over_limit": "share.too_large",
}


EXPLICIT_ALLOWED = {
    "allowed_read_admitted_grants_from_reader",
    "allowed_read_admitted_want_from_server",
    "allowed_read_admitted_chunks_from_reader",
}


def allowed_outcome(name: str) -> str:
    match = re.fullmatch(r"allowed_(\w+?)_(open|admitted)_(\w+?)_from_(reader|server)", name)
    if match is None or name in EXPLICIT_ALLOWED:
        return {
            "allowed_read_admitted_grants_from_reader": "share.unexpected_message",
            "allowed_read_admitted_want_from_server": "share.unexpected_message",
            "allowed_read_admitted_chunks_from_reader": "share.unexpected_message",
            "allowed_read_open_bye": "ok",
            "allowed_unknown_session_kind": "share.malformed",
        }[name]
    kind, state, t, sender = match.groups()
    if t in ("hello", "roster"):
        return "ok"
    return "ok" if kind == "read" and state == "admitted" else "share.unexpected_message"


PRE = {
    "pre_admission_four_rounds_at_the_limit": "ok",
    "pre_admission_five_rounds": "share.too_large",
    "pre_admission_seventeen_frames": "share.too_large",
    "pre_admission_one_byte_over": "share.too_large",
}
ADMIT = {
    "admit_contact_open_invitation": "ok",
    "admit_contact_expired_at_its_second": "share.invite_unknown",
    "admit_contact_last_second": "ok",
    "admit_contact_unknown_invitation": "share.invite_unknown",
    "admit_contact_other_nonce": "share.invite_unknown",
    "admit_contact_own_mesh": "contact.self",
    "admit_contact_hello_kind_differs": "share.malformed",
    "admit_contact_needs_no_contact_record": "ok",
    "admit_read_pocket": "ok",
    "admit_read_desktop": "ok",
    "admit_read_satellite": "share.reader_kind",
    "admit_read_node": "share.reader_kind",
    "admit_read_unverified_contact": "share.contact_unverified",
    "admit_read_no_contact_record": "share.contact_unknown",
    "admit_read_own_mesh": "contact.self",
    "admit_read_unlisted_feed": "share.peer_unknown",
    "admit_read_device_not_in_roster": "share.peer_unknown",
    "admit_read_heads_not_presented": "share.roster_incomplete",
    "admit_read_no_genesis": "roster.no_genesis",
    "admit_read_contact_heads_not_cached": "contact.heads_missing",
    "admit_read_current_mark": "ok",
    "admit_read_removed_device": "share.device_removed",
    "admit_read_roster_stale": "share.roster_stale",
    "admit_read_removed_checked_before_stale": "share.device_removed",
    "admit_read_satellite_checked_before_removal": "share.reader_kind",
    "admit_read_phi_claimed_by_a_phi_member": "ok",
    "admit_read_phi_claimed_by_a_non_member": "ok",
    "admit_read_phi_not_claimed_by_a_member": "ok",
    "admit_read_phi_member_pocket": "ok",
    "admit_read_trigger_seen_at_verification_stays_verified": "ok",
    "admit_read_roster_rejected_after_verification": "contact.roster_rejected",
    "admit_verify_unverified_contact": "ok",
    "admit_verify_verified_contact": "ok",
    "admit_verify_node": "ok",
    "admit_verify_recovers_a_downgraded_roster": "ok",
    "admit_verify_no_contact_record": "share.contact_unknown",
    "admit_verify_unlisted_feed": "share.peer_unknown",
    "admit_verify_roster_without_genesis": "roster.no_genesis",
    "admit_verify_genesis_not_the_mesh": "roster.no_genesis",
    "admit_verify_ignores_the_mark": "ok",
    "admit_nudge_pocket": "ok",
    "admit_nudge_node": "ok",
    "admit_nudge_satellite": "share.server_kind",
    "admit_nudge_unverified_contact": "share.contact_unverified",
    "admit_nudge_removed_device": "share.device_removed",
    "admit_nudge_roster_stale": "share.roster_stale",
    "admit_nudge_roster_rejected": "contact.roster_rejected",
}
RECEIVE = {
    "receive_valid_assistant_message": "ok",
    "receive_valid_under_the_other_grant": "ok",
    "receive_defect_shape": "share.malformed",
    "receive_defect_grant": "share.grant_unknown",
    "receive_defect_key": "share.key_unknown",
    "receive_defect_wrong_salt": "share.decrypt_failed",
    "receive_defect_parse": "event.malformed",
    "receive_defect_mismatch": "share.envelope_mismatch",
    "receive_defect_badsig": "share.bad_signature",
    "receive_defect_feed": "share.feed_unknown",
    "receive_defect_scope": "share.scope_mismatch",
    "receive_defect_type": "share.type_not_allowed",
    "receive_defect_label": "share.label_unknown",
    "receive_defect_bodyhash": "event.body_mismatch",
    "receive_defect_bodydec": "event.decrypt_failed",
    "receive_defect_fields": "share.body_field_refused",
    "receive_secret_label": "share.secret_refused",
    "receive_unknown_integrity": "share.label_unknown",
    "receive_unknown_label_before_secret": "share.label_unknown",
    "receive_header_key_differs": "share.envelope_mismatch",
    "receive_phi_event_with_the_phi_key": "ok",
    "receive_phi_event_when_the_grant_has_no_phi_wrap": "share.key_unknown",
    "receive_removed_device_at_its_cutoff": "ok",
    "receive_removed_device_above_its_cutoff": "share.feed_unknown",
    "receive_node_feed": "ok",
    "receive_chat_create_of_the_granted_chat": "ok",
    "receive_chat_create_of_another_chat": "share.scope_mismatch",
    "receive_tool_result_any_fields": "ok",
    "receive_turn_start_naming_a_request": "share.body_field_refused",
    "receive_header_resealed_under_another_grant": "share.decrypt_failed",
    "receive_body_not_an_object": "share.body_field_refused",
    "receive_envelope_regranted_after_sealing": "share.decrypt_failed",
    "receive_envelope_seq_edited": "share.decrypt_failed",
    "receive_header_ct_too_short": "share.decrypt_failed",
    "receive_wrap_for_another_device": "wrap.decrypt_failed",
    "receive_wrap_edited": "wrap.decrypt_failed",
}
STEP_FLAGS = (
    "shape", "grant", "key", "wrong_salt", "parse", "mismatch", "badsig", "feed", "scope",
    "type", "label", "bodyhash", "bodydec", "fields",
)
for _a, _b in zip(STEP_FLAGS, STEP_FLAGS[1:]):
    RECEIVE[f"receive_defects_{_a}_and_{_b}"] = RECEIVE[f"receive_defect_{_a}"]
OTHER = {
    "wrap_opens_for_the_reader": "ok",
    "wrap_wrong_grant": "wrap.decrypt_failed",
    "wrap_wrong_key": "wrap.decrypt_failed",
    "wrap_wrong_recipient": "wrap.decrypt_failed",
    "wrap_wrong_device_key": "wrap.decrypt_failed",
    "wrap_purpose_epoch_not_share": "wrap.decrypt_failed",
    "want_range_too_long": "share.too_large",
    "revoked_bad_id": "share.malformed",
    "chunks_257_hashes": "share.too_large",
    "chunks_receive_unnamed_chunk": "share.chunk_not_named",
    "chunks_receive_unrequested_chunk": "share.chunk_not_named",
    "chunks_receive_edited_bytes": "attachment.chunk_hash",
}
KINDS = {
    "share_message": message_outcome,
    "share_frame": lambda n: FRAME.get(n, "ok"),
    "share_unframe": lambda n: FRAME.get(n, "share.too_large" if n.endswith("over_limit") else "share.malformed"),
    "share_allowed": allowed_outcome,
    "share_pre_admission": lambda n: PRE[n],
    "share_admit": lambda n: ADMIT[n],
    "share_receive": lambda n: RECEIVE[n],
}


def expected_code(name: str) -> str:
    kind = CASES[name]["kind"]
    if kind in KINDS:
        return KINDS[kind](name)
    return OTHER.get(name, "ok")


def test_documents_name_the_contract() -> None:
    assert DOC["format"] == "astral.vectors/v1"
    assert DOC["name"] == "share-v1"
    assert DOC["spec"] == "106/contracts/share.md"
    assert len(DOC["cases"]) == len(CASES)


@pytest.mark.parametrize("name", sorted(CASES))
def test_case_reproduces_its_committed_expectation(name: str) -> None:
    case = CASES[name]
    assert share.run(case["kind"], case["input"]) == case["expect"]


@pytest.mark.parametrize("name", sorted(CASES))
def test_case_has_the_contracted_outcome(name: str) -> None:
    assert code_of(name) == expected_code(name)


def test_generator_reproduces_the_committed_file() -> None:
    assert generator.render()["share-v1"] == (CONTRACTS / "vectors/share-v1.json").read_text(
        encoding="utf-8"
    )


def test_check_mode_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("check mode wrote a file")

    monkeypatch.setattr(Path, "write_text", refuse)
    generator.main(["--check"])


def test_check_mode_fails_on_a_differing_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(generator, "VECTORS", tmp_path)
    with pytest.raises(SystemExit):
        generator.main(["--check"])
    assert not list(tmp_path.iterdir())
    generator.main([])
    assert (tmp_path / "share-v1.json").exists()


def test_the_served_list_is_exactly_the_contracted_one() -> None:
    assert set(share.SERVED_FIELDS) == set(SERVED)
    every = {t for t in event.EVENT_TYPES}
    assert set(SERVED) < every
    assert DOC["constants"]["served_types"] == sorted(SERVED)


def test_selection_over_a_chat_with_every_type_serves_exactly_the_list() -> None:
    names = served("select_every_type_without_phi")
    assert names == ALWAYS_SERVED
    types = {
        generator.EV[n]["header"]["type"] if n != "create" else "chat.create"
        for n in names
    }
    assert types == set(SERVED)
    for type_, item in generator.EV["never"].items():
        assert "never/" + type_ not in names
    chat_scoped = {
        t for t, i in generator.EV["never"].items() if i["header"]["scope"] == generator.CHAT
    }
    assert {"auth.grant", "run.lease", "request.send", "ui.action", "chat.model_request"} <= chat_scoped
    for type_ in chat_scoped:
        assert refused("select_every_type_without_phi")["never/" + type_] == "share.type_not_allowed"


@pytest.mark.parametrize("type_", sorted(event.EVENT_TYPES))
def test_every_non_served_type_has_its_own_refusal_vector(type_: str) -> None:
    if type_ in SERVED:
        return
    name = "select_never_" + type_.replace(".", "_")
    assert code_of(name) == "ok"
    assert result(name)["ok"]["served"] == []
    assert list(result(name)["ok"]["refused"].values())[0] in (
        "share.type_not_allowed",
        "share.scope_mismatch",
    )


def test_scope_mesh_events_are_never_served() -> None:
    for type_, item in generator.EV["never"].items():
        if item["header"]["scope"] == "mesh":
            assert result("select_never_" + type_.replace(".", "_"))["ok"]["served"] == []


def test_a_tool_result_is_served_only_when_a_served_surface_binds_it() -> None:
    assert served("select_tool_results_without_any_surface") == set()
    assert served("select_tool_result_bound_by_a_put") == {"tr_bound", "surface"}
    assert served("select_tool_result_bound_by_a_patch") == {"tr_patch", "patch"}
    assert served("select_tool_result_named_by_source_result") == {"tr_bound", "tr_source", "surface_source"}
    assert refused("select_tool_result_bound_by_a_put")["tr_unbound"] == "share.type_not_allowed"
    assert "tr_bound" not in served("select_binding_from_a_surface_that_is_not_served")
    assert "tr_bound" not in served("select_binding_from_a_phi_surface_without_phi")


def test_a_bound_tool_result_discloses_its_whole_body() -> None:
    item = generator.EV["tr_bound"]
    assert share.SERVED_FIELDS["chat.tool_result"] is None
    assert set(item["body"]) == {"turn", "call", "status", "content", "code"}


def test_create_of_the_granted_chat_is_served_and_another_is_not() -> None:
    assert served("select_create_of_the_granted_chat") == {"create"}
    assert list(refused("select_create_of_another_chat").values()) == ["share.scope_mismatch"]
    assert code_of("receive_chat_create_of_the_granted_chat") == "ok"
    assert code_of("receive_chat_create_of_another_chat") == "share.scope_mismatch"
    assert result("receive_chat_create_of_the_granted_chat")["ok"]["id"] == generator.CHAT


def test_secret_and_unknown_labels_are_refused_at_both_ends() -> None:
    assert refused("select_secret_event") == {"secret_message": "share.secret_refused"}
    assert refused("select_unknown_confidentiality") == {"unknown_label": "share.label_unknown"}
    assert refused("select_unknown_integrity") == {"unknown_integrity": "share.label_unknown"}
    assert code_of("receive_secret_label") == "share.secret_refused"
    assert code_of("receive_defect_label") == "share.label_unknown"


def test_a_reader_without_phi_gets_no_phi_event_and_a_count() -> None:
    names = served("select_every_type_without_phi")
    assert not names & PHI_NAMES
    assert result("select_every_type_without_phi")["ok"]["withheld_phi"] == 2
    assert PHI_NAMES <= served("select_every_type_with_phi")
    assert result("select_every_type_with_phi")["ok"]["withheld_phi"] == 0
    assert not (PHI_NAMES & served("select_phi_off_even_with_the_phi_key_wrapped"))
    assert not (PHI_NAMES & served("select_phi_reader_without_the_phi_key"))


def _entry(name: str) -> dict:
    return result(name)["ok"]["message"]["grants"][0]


def test_a_non_phi_grant_carries_no_wrap_of_a_phi_key() -> None:
    for name in (
        "grants_plain_grant_for_a_non_phi_reader",
        "grants_phi_grant_for_a_non_phi_reader",
        "grants_non_phi_grant_for_a_phi_reader",
        "grants_phi_grant_with_a_withdrawn_destination",
    ):
        entry = _entry(name)
        assert not entry["phi"]
        assert generator.CK_H not in {w["key"] for w in entry["wraps"]}
        assert entry["withheld_phi"] == 2
    for name in ("grants_phi_grant_for_a_phi_reader", "grants_a_superseding_grant_names_the_old_one"):
        entry = _entry(name)
        assert entry["phi"] and entry["withheld_phi"] == 0
        assert generator.CK_H in {w["key"] for w in entry["wraps"]}
    assert _entry("grants_a_superseding_grant_names_the_old_one")["supersedes"] == generator.GA


def test_the_grants_message_reports_served_heads_and_withheld_fields() -> None:
    entry = _entry("grants_plain_grant_for_a_non_phi_reader")
    assert entry["withheld_fields"] == 3
    assert entry["heads"] == {generator.OA.feed: entry["heads"][generator.OA.feed]}
    assert entry["title"] == "Lab results"
    assert result("grants_only_the_readers_mesh")["ok"]["served"].keys() == {generator.GA}
    assert result("grants_none_in_effect")["ok"]["message"]["grants"] == []


def test_the_key_cap_wraps_the_newest_and_reports_the_rest() -> None:
    capped = result("keys_cap_keeps_the_newest_64")["ok"]
    assert len(capped["wrapped"]) == 64 and capped["withheld_keys"] == 2
    keys = {k["ref"] for k in _key_events("keys_cap_keeps_the_newest_64")}
    assert set(capped["wrapped"]) < keys
    assert result("keys_exactly_64_withhold_none")["ok"]["withheld_keys"] == 0
    assert result("keys_cap_with_phi_counts_the_phi_key")["ok"]["withheld_keys"] == 1
    assert _entry("grants_key_cap_reports_withheld_keys")["withheld_keys"] == 3
    assert len(_entry("grants_key_cap_reports_withheld_keys")["wraps"]) == 64
    assert code_of("message_grants_65_wraps_too_many_keys") == "share.too_many_keys"
    assert code_of("message_grants_64_wraps_valid") == "ok"


def _key_events(name: str) -> list[dict]:
    return [
        {"ref": e["body"]["id"]} for e in CASES[name]["input"]["key_events"]
    ]


def test_a_key_made_for_another_chat_is_not_in_the_set() -> None:
    assert len(result("keys_exclude_a_key_made_for_another_chat")["ok"]["keys"]) == 1
    full = result("keys_chat_key_and_chat_scoped_keys")["ok"]
    assert generator.CK_X not in full["keys"]
    assert generator.CK_H not in full["wrapped"]


def test_an_event_whose_key_is_not_wrapped_is_not_served() -> None:
    assert refused("select_event_under_an_unwrapped_key") == {"no_wrap_key": "share.key_unknown"}
    assert code_of("receive_defect_key") == "share.key_unknown"


def test_a_chunk_not_named_by_a_served_attachment_is_refused() -> None:
    sent = result("chunks_named_by_a_served_attachment")["ok"]
    stray = crypto.sha256_id(b"stray")
    assert stray in {w["hash"] for w in sent["withheld"]}
    assert stray not in sent["send"]
    assert {w["reason"] for w in sent["withheld"]} == {"unknown"}
    nothing = result("chunks_unserved_attachment_names_nothing")["ok"]
    assert nothing["send"] == [] and {w["reason"] for w in nothing["withheld"]} == {"unknown"}
    assert code_of("chunks_receive_unnamed_chunk") == "share.chunk_not_named"


def test_a_phi_attachment_chunk_needs_phi() -> None:
    plain = result("chunks_phi_attachment_without_phi")["ok"]
    phi = generator.CHUNKS["p"][1]
    assert phi in {w["hash"] for w in plain["withheld"] if w["reason"] == "phi"}
    assert phi in result("chunks_phi_attachment_with_phi")["ok"]["send"]


def _hkdf(secret: bytes, salt: bytes, info: bytes) -> bytes:
    extracted = hmac.new(salt, secret, hashlib.sha256).digest()
    return hmac.new(extracted, info + b"\x01", hashlib.sha256).digest()


def test_the_header_key_is_hkdf_over_the_content_key_with_the_grant_as_salt() -> None:
    expected = _hkdf(generator.K_P, bytes.fromhex(generator.GA), b"astral.share.header/v1")
    assert result("header_key_derivation")["ok"]["key"] == expected.hex()
    assert result("header_key_changes_with_the_grant")["ok"]["key"] != expected.hex()


def test_a_header_resealed_under_another_grant_fails() -> None:
    assert code_of("receive_header_resealed_under_another_grant") == "share.decrypt_failed"
    assert code_of("receive_envelope_regranted_after_sealing") == "share.decrypt_failed"
    assert code_of("receive_valid_under_the_other_grant") == "ok"


def test_a_removed_device_is_read_up_to_its_cutoff_only() -> None:
    assert code_of("receive_removed_device_at_its_cutoff") == "ok"
    assert code_of("receive_removed_device_above_its_cutoff") == "share.feed_unknown"
    context = {**generator.CTX, "removed": {generator.OD.id: {"feeds": [generator.OD.feed]}}}
    forged = generator.forge(set(), device=generator.OD, seq=2)
    with pytest.raises(MeshError) as error:
        share.receive(forged["envelope"], context)
    assert error.value.code == "share.feed_unknown"


def test_each_receiver_step_has_a_vector_and_adjacent_defects_give_the_earlier_code() -> None:
    codes = [RECEIVE[f"receive_defect_{f}"] for f in STEP_FLAGS]
    assert codes == list(share.RECEIVER_STEPS)
    for first, second in zip(STEP_FLAGS, STEP_FLAGS[1:]):
        assert code_of(f"receive_defects_{first}_and_{second}") == RECEIVE[f"receive_defect_{first}"]


def test_a_reader_marks_shared_events_untrusted_and_keeps_header_labels_for_display() -> None:
    opened = result("receive_valid_assistant_message")["ok"]
    assert opened["labels"] == {"confidentiality": "private", "integrity": "untrusted"}
    assert opened["header_labels"] == generator.PRIVATE
    phi = result("receive_phi_event_with_the_phi_key")["ok"]
    assert phi["labels"]["confidentiality"] == "phi"


def test_phi_reader_in_a_phi_group_reads_a_phi_event() -> None:
    assert result("admit_read_phi_claimed_by_a_phi_member")["ok"]["phi"] is True
    assert result("admit_read_phi_member_pocket")["ok"]["phi"] is True
    assert result("admit_read_phi_claimed_by_a_non_member")["ok"]["phi"] is False
    assert result("admit_read_phi_not_claimed_by_a_member")["ok"]["phi"] is False
    assert code_of("receive_phi_event_with_the_phi_key") == "ok"
    assert code_of("receive_phi_event_when_the_grant_has_no_phi_wrap") == "share.key_unknown"


def test_verify_needs_only_a_fold_valid_roster() -> None:
    assert code_of("admit_verify_recovers_a_downgraded_roster") == "ok"
    assert code_of("admit_read_roster_rejected_after_verification") == "contact.roster_rejected"
    assert code_of("admit_nudge_roster_rejected") == "contact.roster_rejected"
    assert code_of("admit_verify_ignores_the_mark") == "ok"
    assert code_of("admit_read_removed_device") == "share.device_removed"


def test_the_mark_is_read_with_the_contacts_reader_check() -> None:
    mark = {"heads": ["x"], "count": 1, "removed": []}
    with pytest.raises(MeshError) as error:
        share._mark_check(mark, {"device": "d", "entries": set()})
    assert error.value.code == "share.roster_stale"
    share._mark_check(None, {"device": "d", "entries": set()})
    with pytest.raises(MeshError) as error:
        contacts.reader_check(mark, {"device": "d", "kind": "satellite", "entries": []})
    assert error.value.code == "share.reader_kind"


def test_the_frame_limit_and_list_limits() -> None:
    chunk = {"hash": generator.CHUNKS["a"][1], "ct": cj.b64url_encode(bytes(40000))}
    big = {"t": "chunks", "grant": generator.GA, "chunks": [chunk] * 250, "withheld": []}
    with pytest.raises(MeshError) as error:
        share.frame(big)
    assert error.value.code == "share.too_large"
    envelope = generator.sample_envelope()
    for message, field in (
        ({"t": "events", "grant": generator.GA, "events": [envelope] * 257}, "events"),
        ({"t": "chunks", "grant": generator.GA, "chunks": [chunk] * 257, "withheld": []}, "chunks"),
        ({"t": "grants", "grants": [generator.sample_entry(id=f"{i:032x}") for i in range(257)]}, "grants"),
    ):
        with pytest.raises(MeshError) as error:
            share.check_message(message)
        assert error.value.code == "share.too_large", field
    for count in (256,):
        ok = {"t": "events", "grant": generator.GA, "events": [envelope] * count}
        assert share.check_message(ok) == "events"


def test_a_message_with_an_unknown_kind_or_extra_field_is_malformed() -> None:
    for bad in ({"t": "mailbox"}, {"t": "bye", "x": 1}, {"t": "revoked"}, []):
        with pytest.raises(MeshError) as error:
            share.check_message(bad)
        assert error.value.code == "share.malformed"


def test_hello_fields_follow_the_session_kind() -> None:
    assert share.HELLO_FIELDS["read"] - share.HELLO_BASE == {"phi"}
    assert share.HELLO_FIELDS["contact"] - share.HELLO_BASE == {"invite", "name"}
    assert share.HELLO_FIELDS["verify"] - share.HELLO_BASE == {"name"}
    assert share.HELLO_FIELDS["nudge"] == share.HELLO_BASE


def test_a_server_never_answers_a_want_beyond_the_envelope_limit() -> None:
    answer = result("want_caps_at_256_and_says_more")["ok"]
    assert len(answer["ids"]) == 256 and answer["more"] is True
    assert result("want_unknown_feed")["ok"] == {"ids": [], "more": False}


def test_revoked_names_only_the_grants_the_reader_named() -> None:
    assert result("revoked_names_only_revoked")["ok"]["revoked"] == [generator.GA]
    assert result("revoked_none_named")["ok"]["revoked"] == []


def test_served_field_lists_come_from_the_live_writers() -> None:
    fields = share.SERVED_FIELDS
    assert "request" not in fields["chat.turn_start"] and "action" not in fields["chat.turn_start"]
    assert fields["chat.turn_end"] == {"turn", "reason", "code"}
    assert fields["chat.user_message"] == {"text", "base", "attachments", "steered"}
    assert DOC["constants"]["served_fields"]["chat.tool_result"] is None


def test_the_document_code_table_matches_the_module() -> None:
    for code in share.CODES:
        assert f"`{code}`" in TEXT, code


def test_every_share_code_is_exercised() -> None:
    seen: set[str] = set()
    for case in DOC["cases"]:
        expect = case["expect"]
        if "error" in expect:
            seen.add(expect["error"])
        refusals = expect.get("ok", {})
        if isinstance(refusals, dict):
            for value in refusals.get("refused", {}).values():
                seen.update(value.values() if isinstance(value, dict) else [value])
    owned_elsewhere = {"contact.roster_too_large"}
    assert set(share.CODES) - seen == owned_elsewhere


def test_the_document_names_the_constants() -> None:
    constants = DOC["constants"]
    assert constants["alpn"] == "astral/share/1" and "astral/share/1" in TEXT
    assert str(share.FRAME_LIMIT) in TEXT or "8 MiB" in TEXT
    assert constants["max_keys"] == share.MAX_KEYS
    assert constants["pre_admission"] == share.PRE_ADMISSION
    assert "astral.share.header/v1" in TEXT
    for field in ("chat.tool_result", "ui.patch", "chat.turn_start"):
        assert f"`{field}`" in TEXT


def test_the_text_obeys_the_house_style() -> None:
    assert "—" not in TEXT and ";" not in TEXT
    for word in ("delve", "leverage", "robust", "seamless", "comprehensive", "crucial", "utilize", "streamline"):
        assert word not in TEXT.lower()
