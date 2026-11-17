"""Runs every A2A card vector through the reference and pins the contracted outcome of each named case against the phase 0 fixture files.
A failure means connection.md section 13, the committed vectors and a2a_card.py disagree.
"""

import copy
import json
import re
from pathlib import Path

import a2a_card
import generate_a2a_vectors
import pytest

CONTRACTS = Path(__file__).resolve().parents[2]
VECTORS = CONTRACTS / "vectors"
FIXTURES = CONTRACTS.parent / "phase-0/a2a-reader/fixtures"
DOC = json.loads((VECTORS / "a2a-card-v1.json").read_text(encoding="utf-8"))
CASES = {case["name"]: case for case in DOC["cases"]}
ORIGIN_URL = "https://agent.example.org/.well-known/agent-card.json"


def load(*parts: str):
    return json.loads(FIXTURES.joinpath(*parts).read_text(encoding="utf-8"))


def stems(folder: str) -> list[str]:
    return sorted(p.stem for p in (FIXTURES / folder).glob("*.json"))


VERIFY = {
    "signed_by_signer_one_pinned": "verified_pinned",
    "signed_by_signer_two_new_signer": "verified_new_signer",
    "first_sight_reports_the_signer": "verified_new_signer",
    "changed_signed_card_invalid": "invalid",
    "no_matching_key_invalid": "invalid",
    "unsigned": "unsigned",
    "ed25519_pinned": "verified_pinned",
    "rsa_2048_pinned": "verified_pinned",
    "rsa_1024_refused": "invalid",
    "sdk_form_equal_to_strict_form": "verified_pinned",
    "sdk_form_differs_only_outside_requirements": "verified_pinned",
    "sdk_form_misses_empty_requirement_entries": "verified_pinned:sdk",
    "resigned_changed_card_pinned": "verified_pinned",
    "first_signature_bad_second_good": "verified_pinned",
    "header_without_kid_invalid": "invalid",
    "algorithm_hs256_refused": "invalid",
    "algorithm_none_refused": "invalid",
    "algorithm_does_not_fit_key": "invalid",
    "key_without_kid_still_verifies": "verified_pinned",
    "pin_is_the_thumbprint_not_the_kid": "verified_new_signer",
    "signature_not_object": "invalid",
    "signature_not_base64": "invalid",
}

PROPOSE = {
    "unsigned": "ok",
    "signed": "ok",
    "ed25519": "ok",
    "non_ascii_unsigned": "ok",
    "origin_is_lowercased_without_default_port": "ok",
    "origin_keeps_other_port": "ok",
    "tampered_signature_invalid": "a2a.signature_invalid",
    "sdk_only_signature_scope": "a2a.signature_scope",
    "sdk_form_differs_outside_requirements_accepted": "ok",
    "fetched_over_http": "a2a.insecure_url",
    "fetched_with_userinfo": "a2a.insecure_url",
    "fetched_without_scheme": "a2a.insecure_url",
    "card_refused_first": "a2a.missing_field",
}

CHECK = {
    "unsigned_requirements_emptied_breaks_the_pin": "a2a.card_changed",
    "signed_requirements_emptied_breaks_the_signature": "a2a.signature_invalid",
    "signed_requirements_emptied_resigned_is_a_changed_card": "a2a.card_changed",
    "unsigned_requirements_removed_breaks_the_pin": "a2a.card_changed",
    "signed_requirements_removed_breaks_the_signature": "a2a.signature_invalid",
    "signed_requirements_removed_resigned_is_a_changed_card": "a2a.card_changed",
    "unsigned_alternative_dropped_breaks_the_pin": "a2a.card_changed",
    "signed_alternative_dropped_breaks_the_signature": "a2a.signature_invalid",
    "signed_alternative_dropped_resigned_is_a_changed_card": "a2a.card_changed",
    "unsigned_alternative_added_breaks_the_pin": "a2a.card_changed",
    "signed_alternative_added_breaks_the_signature": "a2a.signature_invalid",
    "signed_alternative_added_resigned_is_a_changed_card": "a2a.card_changed",
    "unsigned_scope_added_breaks_the_pin": "a2a.card_changed",
    "signed_scope_added_breaks_the_signature": "a2a.signature_invalid",
    "signed_scope_added_resigned_is_a_changed_card": "a2a.card_changed",
    "unsigned_skill_requirements_emptied_breaks_the_pin": "a2a.card_changed",
    "signed_skill_requirements_emptied_breaks_the_signature": "a2a.signature_invalid",
    "signed_skill_requirements_emptied_resigned_is_a_changed_card": "a2a.card_changed",
    "unsigned_matches": "ok",
    "unsigned_key_order_and_origin_spelling_do_not_matter": "ok",
    "unsigned_version_changed": "a2a.card_changed",
    "unsigned_origin_host_changed": "a2a.origin_changed",
    "unsigned_origin_port_changed": "a2a.origin_changed",
    "unsigned_origin_http": "a2a.insecure_url",
    "unsigned_pin_card_now_signed_still_matches": "ok",
    "signed_pinned_matches": "ok",
    "signed_new_signer": "a2a.signer_changed",
    "signed_signature_removed": "a2a.signature_missing",
    "signed_tampered": "a2a.signature_invalid",
    "signed_sdk_form_misses_empty_entries": "a2a.signature_scope",
    "signed_by_pinned_signer_but_card_changed": "a2a.card_changed",
    "signed_order_origin_before_signature": "a2a.origin_changed",
    "signed_order_signature_before_card_change": "a2a.signer_changed",
    "signed_order_card_change_before_interface": "a2a.card_changed",
    "card_refused_before_origin": "a2a.missing_field",
    "interface_host_listed_with_port": "ok",
    "interface_host_not_in_egress": "a2a.interface_offsite",
    "interface_port_not_in_egress": "a2a.interface_offsite",
    "interface_ip_literal": "a2a.interface_offsite",
    "interface_second_host_not_in_egress": "a2a.interface_offsite",
    "pin_with_extra_field": "a2a.not_a_card_pin",
    "pin_hash_not_an_id": "a2a.not_a_card_pin",
    "pin_missing_signer": "a2a.not_a_card_pin",
    "pin_not_an_object": "a2a.not_a_card_pin",
}

EXTENDED = {
    "pinned_matches": "ok",
    "not_yet_reviewed": "a2a.extended_unreviewed",
    "changed_after_review": "a2a.extended_changed",
    "card_refused": "a2a.missing_field",
}

UNSELECTABLE = {
    "http_digest",
    "mutual_tls",
    "oauth2_client_credentials",
    "oauth2_implicit_deprecated",
}


def run(name: str):
    case = CASES[name]
    return a2a_card.run(case["kind"], case["input"])


def error_of(name: str) -> str | None:
    return run(name).get("error")


def test_the_fixture_set_is_the_nineteen_and_nineteen_of_phase_zero() -> None:
    assert len(stems("valid")) == len(stems("malformed")) == 19


@pytest.mark.parametrize("name", stems("valid"))
def test_every_valid_fixture_reads_and_matches_its_vector(name: str) -> None:
    case = CASES[f"card_valid_{name}"]
    assert case["input"]["card"] == load("valid", f"{name}.json")
    assert "ok" in case["expect"]
    assert case["expect"]["ok"]["digest"] == a2a_card.card_digest(case["input"]["card"])


@pytest.mark.parametrize("name", stems("malformed"))
def test_every_malformed_fixture_is_refused_with_its_code(name: str) -> None:
    stored = load("malformed", f"{name}.json")
    assert CASES[f"card_malformed_{name}"]["input"]["card"] == stored["card"]
    assert error_of(f"card_malformed_{name}") == "a2a." + stored["expectedRefusal"]


@pytest.mark.parametrize("name", sorted(CASES))
def test_case_reproduces_its_committed_expectation(name: str) -> None:
    assert run(name) == CASES[name]["expect"]


@pytest.mark.parametrize("name", stems("valid"))
def test_selection_follows_the_scheme_table(name: str) -> None:
    for node in (False, True):
        result = run(f"select_{name}_{'node' if node else 'device'}")
        unusable = name in UNSELECTABLE or (name == "oauth2_device_code" and not node)
        if unusable:
            assert result == {"error": "a2a.scheme_unsupported"}
        elif name == "no_requirements":
            assert result["ok"]["alternative"] is None
        else:
            assert result["ok"]["alternative"] == (1 if name == "alternatives" else 0)


def test_signed_alternatives_pick_the_first_usable_entry_in_card_order() -> None:
    chosen = run("select_alternatives_node")["ok"]
    assert [pair[0] for pair in chosen["schemes"]] == ["key", "tok"]


@pytest.mark.parametrize("name", sorted(VERIFY))
def test_verification_outcome(name: str) -> None:
    ok = run(f"verify_{name}")["ok"]
    want = VERIFY[name]
    assert ok["status"] == want.split(":")[0]
    assert ok["covers_requirements"] is (not want.endswith(":sdk"))
    assert ok["accepted"] is (
        ok["status"] == "verified_pinned" and ok["covers_requirements"]
    )


@pytest.mark.parametrize("name", sorted(PROPOSE))
def test_proposal_outcome(name: str) -> None:
    result = run(f"propose_{name}")
    assert result.get("error", "ok") == PROPOSE[name]


@pytest.mark.parametrize("name", sorted(CHECK))
def test_check_outcome(name: str) -> None:
    assert run(f"check_{name}").get("error", "ok") == CHECK[name]


@pytest.mark.parametrize("name", sorted(EXTENDED))
def test_extended_card_outcome(name: str) -> None:
    assert run(f"extended_{name}").get("error", "ok") == EXTENDED[name]


def test_every_vector_case_is_pinned_by_an_explicit_outcome() -> None:
    pinned = (
        {f"verify_{n}" for n in VERIFY}
        | {f"propose_{n}" for n in PROPOSE}
        | {f"check_{n}" for n in CHECK}
        | {f"extended_{n}" for n in EXTENDED}
    )
    grouped = ("verify_", "propose_", "check_", "extended_")
    assert {n for n in CASES if n.startswith(grouped)} == pinned


def test_requirement_only_changes_break_both_pins_and_resigning_does_not_help() -> None:
    names = [
        n
        for n in CASES
        if n.startswith("check_unsigned_") and n.endswith("_breaks_the_pin")
    ]
    assert len(names) == 6
    for name in names:
        assert error_of(name) == "a2a.card_changed"
        assert (
            error_of(name.replace("unsigned", "signed").replace("pin", "signature"))
            == "a2a.signature_invalid"
        )
        assert (
            error_of(
                name.replace("unsigned", "signed").replace(
                    "breaks_the_pin", "resigned_is_a_changed_card"
                )
            )
            == "a2a.card_changed"
        )


def test_a_signature_over_the_sdk_form_that_drops_empty_requirements_is_signature_scope() -> (
    None
):
    assert error_of("propose_sdk_only_signature_scope") == "a2a.signature_scope"
    assert (
        error_of("check_signed_sdk_form_misses_empty_entries") == "a2a.signature_scope"
    )
    assert not run("verify_sdk_form_misses_empty_requirement_entries")["ok"]["accepted"]
    assert run("verify_sdk_form_differs_only_outside_requirements")["ok"]["accepted"]


def test_the_reader_and_the_a2a_sdk_agree_on_the_signed_form() -> None:
    signing = pytest.importorskip("a2a.utils.signing")
    from a2a.types import AgentCard
    from google.protobuf.json_format import MessageToDict, ParseDict

    key = generate_a2a_vectors.derive_ec("sdk-interop")
    jwk = generate_a2a_vectors.ec_jwk(key, "sdk-kid")
    pin = a2a_card.jwk_thumbprint(jwk)
    card = load("valid", "oauth2_auth_code_pkce.json")
    signed = signing.create_agent_card_signer(
        key, {"kid": "sdk-kid", "alg": "ES256", "jku": None, "typ": "JOSE"}
    )(ParseDict(copy.deepcopy(card), AgentCard()))
    assert a2a_card.verify_signed(MessageToDict(signed), [jwk], pin).accepted
    empty = load("valid", "extended_card.json")
    signed = signing.create_agent_card_signer(
        key, {"kid": "sdk-kid", "alg": "ES256", "jku": None, "typ": "JOSE"}
    )(ParseDict(copy.deepcopy(empty), AgentCard()))
    result = a2a_card.verify_signed(MessageToDict(signed), [jwk], pin)
    assert result.status == "verified_pinned" and not result.accepted


def test_a_non_ascii_card_has_raw_utf8_canonical_bytes() -> None:
    canonical = run("canonical_non_ascii")["ok"]["canonical"]
    assert '"name":"Café Ágent 日本語 😀"' in canonical
    assert "\\u" not in canonical
    assert canonical.encode("utf-8").decode("utf-8") == canonical
    assert run("card_valid_non_ascii")["ok"]["digest"].startswith("sha256:")


def test_keys_sort_by_utf16_code_units_not_code_points() -> None:
    canonical = run("canonical_utf16_key_order")["ok"]["canonical"]
    assert canonical.index("😀") < canonical.index("￿")


def test_canonical_form_drops_signatures_empties_and_false_flags() -> None:
    assert (
        run("canonical_signatures_dropped")["ok"]
        == run("canonical_extended_card")["ok"]
    )
    assert (
        run("canonical_empty_values_dropped")["ok"]
        == run("canonical_extended_card")["ok"]
    )
    assert (
        run("canonical_key_order_does_not_matter")["ok"]
        == run("canonical_extended_card")["ok"]
    )
    assert (
        run("canonical_false_flags_dropped_presence_booleans_kept")["ok"]["canonical"]
        == '{"capabilities":{"streaming":false},"name":"n","x":{"y":1}}'
    )
    assert run("canonical_fractional_number_refused") == {"error": "a2a.float_in_card"}
    assert (
        run("canonical_integer_valued_float")["ok"]["canonical"]
        == '{"name":"n","weight":2}'
    )


def test_the_sdk_form_drops_empty_scope_lists_the_strict_form_keeps() -> None:
    ok = run("canonical_api_key_empty_scope_lists")["ok"]
    assert '"list":[]' in ok["canonical"]
    assert '"list"' not in ok["sdk"]


def test_a_card_hash_is_the_sha256_of_the_canonical_bytes() -> None:
    import hashlib

    ok = run("canonical_extended_card")["ok"]
    assert (
        ok["digest"] == "sha256:" + hashlib.sha256(ok["canonical"].encode()).hexdigest()
    )


def test_thumbprints_name_the_key_and_ignore_its_key_id() -> None:
    assert run("thumbprint_ignores_kid") == run("thumbprint_signer_one")
    thumbprints = {
        run(f"thumbprint_{n}")["ok"]["thumbprint"]
        for n in ("signer_one", "signer_two", "ed25519", "rsa")
    }
    assert len(thumbprints) == 4
    assert error_of("thumbprint_oct_refused") == "a2a.bad_key"


def test_auth_required_fails_the_call_and_sends_nothing() -> None:
    without = run("task_state_auth_required_without_credential")["ok"]
    assert without == {
        "action": "fail",
        "error": "connection.sign_in_needed",
        "state": "needs_attention",
        "reason": "not_signed_in",
        "sends": False,
    }
    assert run("task_state_auth_required_with_credential")["ok"]["reason"] == "refused"
    assert run("task_state_working_with_credential")["ok"] == {"action": "continue"}
    assert run("task_state_auth_required_with_credential")["ok"]["action"] == "fail"


def test_generator_reproduces_the_committed_file() -> None:
    assert generate_a2a_vectors.render()["a2a-card-v1"] == (
        VECTORS / "a2a-card-v1.json"
    ).read_text(encoding="utf-8")


def test_check_mode_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("check mode wrote a file")

    monkeypatch.setattr(Path, "write_text", refuse)
    generate_a2a_vectors.main(["--check"])


def test_the_document_code_table_matches_the_module() -> None:
    text = (CONTRACTS / "connection.md").read_text(encoding="utf-8")
    section = text.split("### 13.8 A2A codes", 1)[1]
    listed = set(re.findall(r"`((?:a2a|connection)\.[a-z0-9_]+)`", section))
    assert listed == set(a2a_card.CODES)


def test_every_a2a_code_is_exercised_by_a_vector() -> None:
    seen = {
        c["expect"].get("error") or c["expect"]["ok"].get("error") for c in DOC["cases"]
    }
    assert set(a2a_card.CODES) <= seen | {"a2a.scheme_unsupported"}, sorted(
        set(a2a_card.CODES) - seen
    )


def test_the_tool_hash_follows_the_card_name_description_and_skills() -> None:
    base = load("valid", "extended_card.json")
    renamed = {**base, "name": "Other"}
    reskilled = {**base, "skills": []}
    rekeyed = {**base, "version": "2.0.0"}
    assert a2a_card.tool_hash(base) != a2a_card.tool_hash(renamed)
    assert a2a_card.tool_hash(base) != a2a_card.tool_hash(reskilled)
    assert a2a_card.tool_hash(base) == a2a_card.tool_hash(rekeyed)
    assert run("card_valid_extended_card")["ok"]["tool_hash"] == a2a_card.tool_hash(
        base
    )


def test_unsigned_pin_survives_a_signature_added_later() -> None:
    assert error_of("check_unsigned_pin_card_now_signed_still_matches") is None


def test_signer_pin_is_the_key_not_the_key_id() -> None:
    assert (
        run("verify_pin_is_the_thumbprint_not_the_kid")["ok"]["status"]
        == "verified_new_signer"
    )
