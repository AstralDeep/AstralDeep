"""Runs every PHI destination vector through the reference and pins the contracted outcome of each named case: canonical destinations, subjects, proposals, approvals, the roster fold of the PHI entries, markers, the view, group duties and the chat PHI key.
It also proves the fold is independent of arrival order and that every phi epoch and wrap opens for each recipient it names.
A failure means phi-destinations.md, the committed vectors and phi_destinations.py disagree."""

import json
import random
import re
from pathlib import Path

import generate_phi_destination_vectors
import phi_destinations
import pytest
from astral_mesh_ref import attestation, crypto, keys, testkit

CONTRACTS = Path(__file__).resolve().parents[2]
VECTORS = CONTRACTS / "vectors"
DOC = json.loads((VECTORS / "phi-destination-v1.json").read_text(encoding="utf-8"))
CASES = {case["name"]: case for case in DOC["cases"]}
DEVICES = DOC["test_devices"]
ID = {name: device["id"] for name, device in DEVICES.items()}
NAME = {device["id"]: name for name, device in DEVICES.items()}
CONTACT = DOC["constants"]["contact_mesh"]
TEXT = (CONTRACTS / "phi-destinations.md").read_text(encoding="utf-8")

M = "https://api.model.example/v1"
S = "https://mcp.research.example/mcp"
LOOP = "http://127.0.0.1:8765/mcp"
MAL = "phi_destination.malformed"
WILD = "phi_destination.wildcard"

ENDPOINTS = {
    "endpoint_plain": (M, False),
    "endpoint_uppercase_scheme_and_host": (M, False),
    "endpoint_path_case_kept": ("https://api.model.example/V1", False),
    "endpoint_default_port_dropped": (M, False),
    "endpoint_other_port_kept": ("https://api.model.example:8443/v1", False),
    "endpoint_https_port_80_kept": ("https://api.model.example:80/v1", False),
    "endpoint_port_65535": ("https://api.model.example:65535/v1", False),
    "endpoint_trailing_slash_removed": (M, False),
    "endpoint_root_slash_removed": ("https://api.model.example", False),
    "endpoint_no_path": ("https://api.model.example", False),
    "endpoint_reserved_escape_kept": ("https://api.model.example/a%2Fb", False),
    "endpoint_at_sign_in_path": ("https://api.model.example/v1@x", False),
    "endpoint_punycode": ("https://xn--bcher-kva.example/v1", False),
    "endpoint_punycode_uppercase": ("https://xn--bcher-kva.example/v1", False),
    "endpoint_label_63": ("https://" + "a" * 63 + ".example", False),
    "endpoint_host_253": None,
    "endpoint_2048_characters": None,
    "endpoint_loopback_localhost_http": ("http://localhost:11434/v1", True),
    "endpoint_loopback_localhost_https": ("https://localhost:8443/v1", True),
    "endpoint_loopback_localhost_uppercase": ("http://localhost:11434/v1", True),
    "endpoint_loopback_subdomain": ("http://llm.localhost:8080/v1", True),
    "endpoint_loopback_ipv4": ("http://127.0.0.1:8080/v1", True),
    "endpoint_loopback_ipv4_other_address": ("http://127.2.3.4:8080/v1", True),
    "endpoint_loopback_ipv6": ("http://[::1]:8080/v1", True),
    "endpoint_http_default_port_dropped": ("http://localhost/v1", True),
    "endpoint_http_port_443_kept": ("http://localhost:443/v1", True),
    "endpoint_loopback_mcp": (LOOP, True),
    "endpoint_wildcard_label": WILD,
    "endpoint_wildcard_inside_label": WILD,
    "endpoint_wildcard_question_mark": WILD,
    "endpoint_wildcard_bracket_class": WILD,
    "endpoint_wildcard_leading_dot": WILD,
    "endpoint_wildcard_trailing_dot": WILD,
    "endpoint_wildcard_double_dot": WILD,
    "endpoint_wildcard_in_path": WILD,
    "endpoint_loopback_trailing_dot": WILD,
    "endpoint_ipv6_zone": WILD,
    "endpoint_query_without_path": WILD,
    "endpoint_order_wildcard_before_query": WILD,
    "endpoint_order_wildcard_before_userinfo": WILD,
    "endpoint_order_text_before_wildcard": MAL,
    "endpoint_order_userinfo_before_bracket": MAL,
    "endpoint_userinfo": MAL,
    "endpoint_query": MAL,
    "endpoint_fragment": MAL,
    "endpoint_fragment_without_path": MAL,
    "endpoint_list": MAL,
    "endpoint_empty": MAL,
    "endpoint_number": MAL,
    "endpoint_null": MAL,
    "endpoint_two_urls": MAL,
    "endpoint_non_ascii_host": MAL,
    "endpoint_2049_characters": MAL,
    "endpoint_http_remote": MAL,
    "endpoint_other_scheme": MAL,
    "endpoint_websocket_scheme": MAL,
    "endpoint_no_scheme": MAL,
    "endpoint_single_label": MAL,
    "endpoint_underscore": MAL,
    "endpoint_numeric_last_label": MAL,
    "endpoint_hex_last_label": MAL,
    "endpoint_label_64": MAL,
    "endpoint_host_254": MAL,
    "endpoint_empty_host": MAL,
    "endpoint_port_leading_zero": MAL,
    "endpoint_port_zero": MAL,
    "endpoint_port_65536": MAL,
    "endpoint_port_empty": MAL,
    "endpoint_port_not_digits": MAL,
    "endpoint_public_ipv4": MAL,
    "endpoint_private_ipv4": MAL,
    "endpoint_loopback_ipv4_short_form": MAL,
    "endpoint_loopback_ipv4_leading_zero": MAL,
    "endpoint_loopback_ipv4_octet_256": MAL,
    "endpoint_loopback_ipv6_long_form": MAL,
    "endpoint_public_ipv6": MAL,
    "endpoint_bracket_trailing_text": MAL,
    "endpoint_double_trailing_slash": MAL,
    "endpoint_empty_segment": MAL,
    "endpoint_dot_segment": MAL,
    "endpoint_single_dot_segment": MAL,
    "endpoint_escaped_dot_segment": MAL,
    "endpoint_escape_lowercase_hex": MAL,
    "endpoint_escape_unreserved": MAL,
    "endpoint_escape_incomplete": MAL,
    "endpoint_backslash_in_path": MAL,
    "endpoint_percent_in_host": MAL,
}


def local(device: str, endpoint: str = LOOP) -> str:
    return "local:" + ID[device] + ":" + endpoint


SUBJECTS = {
    "subject_endpoint": ("endpoint", None),
    "subject_endpoint_not_canonical": ("invalid", "phi_destination.not_canonical"),
    "subject_endpoint_trailing_slash": ("invalid", "phi_destination.not_canonical"),
    "subject_endpoint_scheme_uppercase": ("invalid", "phi_destination.not_canonical"),
    "subject_endpoint_wildcard": ("invalid", WILD),
    "subject_endpoint_query": ("invalid", MAL),
    "subject_loopback_unbound": ("invalid", "phi_destination.loopback_unbound"),
    "subject_local_bound": ("local", None),
    "subject_local_remote_endpoint": ("invalid", MAL),
    "subject_local_bad_device": ("invalid", MAL),
    "subject_local_missing_endpoint": ("invalid", MAL),
    "subject_local_not_canonical": ("invalid", "phi_destination.not_canonical"),
    "subject_contact": ("contact", None),
    "subject_contact_uppercase": ("invalid", MAL),
    "subject_contact_short": ("invalid", MAL),
    "subject_contact_without_prefix": ("invalid", MAL),
    "subject_device": ("device", None),
    "subject_unknown_form": ("unsupported", None),
    "subject_node_name": ("unsupported", None),
    "subject_mail_address": ("unsupported", None),
}

PROPOSALS = {
    "propose_provider_remote": ("model", M, True),
    "propose_provider_canonicalized": ("model", M, True),
    "propose_provider_loopback": ("model", "http://localhost:11434/v1", False),
    "propose_provider_wildcard": WILD,
    "propose_provider_list": MAL,
    "propose_provider_missing_url": MAL,
    "propose_connection_remote": ("service", S, True),
    "propose_connection_local": ("service", "local:bob", True),
    "propose_connection_local_scheme_none": ("service", "local:node", True),
    "propose_connection_local_without_device": MAL,
    "propose_connection_confined_refused": MAL,
    "propose_connection_confined_model_refused": MAL,
    "propose_connection_confined_not_boolean": MAL,
    "propose_connection_without_endpoint": "phi_destination.no_target",
    "propose_connection_model": ("model", M, True),
    "propose_connection_model_loopback": ("model", "http://127.0.0.1:1234/v1", False),
    "propose_connection_unknown_use": MAL,
    "propose_legacy_server_remote": ("service", S, True),
    "propose_legacy_server_loopback": ("service", "local:alice", True),
    "propose_contact": ("contact", CONTACT, True),
    "propose_contact_malformed": MAL,
    "propose_unknown_source": MAL,
    "propose_not_an_object": MAL,
    "propose_name_not_text": MAL,
}

APPROVALS = {
    "approve_endpoint": ("endpoint", M),
    "approve_service": ("endpoint", S),
    "approve_local_service": ("endpoint", "local:bob"),
    "approve_legacy_loopback_binds_this_device": ("endpoint", "local:alice"),
    "approve_on_device_model_refused": "phi_destination.on_device",
    "approve_confined_service_refused": MAL,
    "approve_contact_unavailable": "phi_destination.contact_unavailable",
    "approve_on_node_refused": "phi_group.node_device",
    "approve_wildcard_refused": WILD,
    "approve_list_refused": MAL,
    "approve_order_destination_before_node": WILD,
    "approve_order_node_before_on_device": "phi_group.node_device",
    "approve_order_node_before_contact": "phi_group.node_device",
    "approve_satellite_allowed": ("endpoint", M),
}

CHAT = {
    "chat_first_phi_write": ("rotate", "first_write", "phi"),
    "chat_phi_key_current": ("use", "ck:phi-current", None),
    "chat_phi_epoch_changed": ("rotate", "epoch_changed", "phi"),
    "chat_personal_create_key": ("use", "ck:create", None),
    "chat_personal_epoch_changed": ("rotate", "epoch_changed", "personal"),
    "chat_personal_rotated_key_used": ("use", "ck:personal-rotated", None),
    "chat_personal_without_key": ("rotate", "first_write", "personal"),
    "chat_concurrent_by_clock": ("use", "ck:later-ms", None),
    "chat_concurrent_same_ms_by_counter": ("use", "ck:later-n", None),
    "chat_concurrent_same_clock_by_feed": ("use", "ck:feed-b", None),
    "chat_same_feed_by_seq": ("use", "ck:seq-2", None),
    "chat_older_epoch_key_later_in_order_is_ignored": ("use", "ck:current-epoch", None),
    "chat_only_old_epoch_keys_rotate": ("rotate", "epoch_changed", "phi"),
    "chat_create_key_under_current_epoch_when_others_are_old": ("use", "ck:create", None),
    "chat_other_scope_ignored": ("rotate", "first_write", "phi"),
    "chat_other_group_ignored": ("rotate", "first_write", "phi"),
    "chat_create_key_of_other_group_ignored": ("rotate", "first_write", "phi"),
    "chat_phi_without_epoch": ("error", "phi.group_not_ready", None),
    "chat_phi_device_excluded": ("error", "phi.device_excluded", None),
    "chat_phi_device_not_ready": ("error", "phi.group_not_ready", None),
    "chat_personal_ignores_phi_state": ("use", "ck:create", None),
    "chat_phi_stale_epoch_first_write": ("rotate_epoch", "first_write", "phi"),
    "chat_phi_stale_epoch_with_a_key_under_it": ("rotate_epoch", "epoch_changed", "phi"),
    "chat_phi_stale_epoch_cannot_rotate": ("error", "phi.group_not_ready", None),
    "chat_phi_stale_epoch_for_an_excluded_device": ("error", "phi.device_excluded", None),
    "chat_phi_stale_without_epoch": ("error", "phi.group_not_ready", None),
    "chat_personal_ignores_a_stale_phi_epoch": ("use", "ck:create", None),
}


def names(ids):
    return sorted(NAME[i] for i in ids)


def step(case, index):
    return case["expect"]["steps"][index]


def step_by_name(case_name, step_name):
    case = CASES[case_name]
    for index, given in enumerate(case["input"]["steps"]):
        if given["name"] == step_name:
            return given, step(case, index)
    raise KeyError(step_name)


def wire(given):
    return [phi_destinations.read_wire(text) for text in given["entries"]]


def entry_names(case_name):
    out = {}
    for given in CASES[case_name]["input"]["steps"]:
        for name, entry in zip(given["entry_names"], wire(given), strict=True):
            out[phi_destinations.entry_id(entry)] = name
    return out


def test_every_case_runs_through_the_reference():
    for case in DOC["cases"]:
        assert phi_destinations.evaluate(case) == case["expect"], case["name"]


def test_vectors_regenerate_identically():
    rendered = generate_phi_destination_vectors.render()
    for name in ("phi-destination-v1", "labels-v2"):
        assert rendered[name] == (VECTORS / f"{name}.json").read_text(encoding="utf-8")


def test_case_names_are_unique():
    assert len(CASES) == len(DOC["cases"])


@pytest.mark.parametrize("name", sorted(ENDPOINTS))
def test_endpoint(name):
    case = CASES[name]
    want = ENDPOINTS[name]
    assert case["kind"] == "phi_endpoint"
    if isinstance(want, str):
        assert case["expect"] == {"error": want}
    elif want is None:
        assert set(case["expect"]["ok"]) == {"target", "loopback"}
    else:
        assert case["expect"] == {"ok": {"target": want[0], "loopback": want[1]}}


def test_every_endpoint_case_is_pinned():
    kinds = {n for n, c in CASES.items() if c["kind"] == "phi_endpoint"}
    assert kinds == set(ENDPOINTS)


def test_size_limits_sit_at_the_edge():
    at = CASES["endpoint_2048_characters"]["input"]["value"]
    over = CASES["endpoint_2049_characters"]["input"]["value"]
    assert len(at) == 2048 and len(over) == 2049
    host = CASES["endpoint_host_253"]["expect"]["ok"]["target"][len("https://") :]
    assert len(host) == 253
    assert CASES["endpoint_host_254"]["expect"] == {"error": MAL}


def test_canonical_form_is_a_fixed_point():
    for name, case in CASES.items():
        if case["kind"] == "phi_endpoint" and "ok" in case["expect"]:
            target = case["expect"]["ok"]["target"]
            assert phi_destinations.canonical_endpoint(target)["target"] == target, name


@pytest.mark.parametrize("name", sorted(SUBJECTS))
def test_subject(name):
    form, code = SUBJECTS[name]
    got = CASES[name]["expect"]["ok"]
    assert (got["form"], got["code"]) == (form, code)


def test_local_and_contact_subjects_round_trip():
    bound = CASES["subject_local_bound"]
    assert bound["input"]["subject"] == local("alice")
    assert bound["expect"]["ok"]["device"] == ID["alice"]
    assert bound["expect"]["ok"]["target"] == LOOP
    contact = CASES["subject_contact"]
    assert contact["input"]["subject"] == "contact:" + CONTACT
    assert contact["expect"]["ok"]["target"] == CONTACT
    trip = CASES["contact_round_trip"]["expect"]["ok"]
    assert trip == {"subject": "contact:" + CONTACT}


@pytest.mark.parametrize(
    "name",
    [
        "contact_uppercase_hex",
        "contact_short",
        "contact_without_prefix",
        "contact_not_text",
        "contact_own_form",
    ],
)
def test_malformed_contact_target(name):
    assert CASES[name]["expect"] == {"error": MAL}


@pytest.mark.parametrize("name", sorted(PROPOSALS))
def test_proposal(name):
    want = PROPOSALS[name]
    got = CASES[name]["expect"]
    if isinstance(want, str):
        assert got == {"error": want}
        return
    kind, target, needs = want
    ok = got["ok"]
    if target and target.startswith("local:"):
        target = local(target.split(":")[1], ok["target"].split(":", 3)[3])
    assert (ok["kind"], ok["target"], ok["needs_approval"]) == (kind, target, needs)
    if needs:
        assert ok["subject"] == ("contact:" + CONTACT if kind == "contact" else target)
        assert ok["facts"] == ["not_owner_run", "leaves_devices", "unseen_handling"]
    else:
        assert ok["subject"] is None and ok["facts"] == []


def test_local_connection_binds_its_record_device():
    ok = CASES["propose_connection_local"]["expect"]["ok"]
    assert ok["target"] == local("bob")
    ok = CASES["propose_connection_local_scheme_none"]["expect"]["ok"]
    assert ok["target"] == local("node", "http://127.0.0.1:8770/mcp")
    assert ok["needs_approval"] is True


@pytest.mark.parametrize("name", sorted(APPROVALS))
def test_approval(name):
    want = APPROVALS[name]
    got = CASES[name]["expect"]
    if isinstance(want, str):
        assert got == {"error": want}
        return
    subject = want[1]
    if subject.startswith("local:"):
        subject = local(subject.split(":")[1])
    assert got == {"ok": {"subject_kind": want[0], "subject": subject}}


def test_statements_use_the_approver_public_and_a_twenty_year_window():
    lifetime = DOC["constants"]["approval_lifetime_s"]
    assert lifetime == 631152000
    for name in ("statement_device", "statement_endpoint", "statement_contact"):
        case = CASES[name]
        statement = case["expect"]["ok"]["statement"]
        assert statement["not_after_s"] == statement["issued_s"] + lifetime
        device = DEVICES[case["input"]["approver"]]
        assert statement["institution"] == device["approver_public"]
        summary = attestation.verify_phi(statement, {device["approver_public"]})
        assert summary["subject"] == case["input"]["subject"]
        body = phi_destinations.read_wire(case["expect"]["ok"]["authority"])
        assert body == {
            "action": "add",
            "institution": {
                "key": device["approver_public"],
                "name": "astral-device:" + device["id"],
            },
        }


@pytest.mark.parametrize("name", sorted(CHAT))
def test_chat_key(name):
    kind, value, group = CHAT[name]
    got = CASES[name]["expect"]
    if kind == "error":
        assert got == {"error": value}
    elif kind == "use":
        assert got == {"ok": {"use": value}}
    elif kind == "rotate_epoch":
        assert got == {"ok": {"rotate_epoch": True, "rotate": value, "epoch": "new"}}
    else:
        current = CASES[name]["input"]["current"][group]
        assert got == {"ok": {"rotate": value, "epoch": current}}


def test_chat_reasons_are_a_closed_set():
    assert phi_destinations.ROTATION_REASONS == ("first_write", "epoch_changed", "revoked")
    for reason in phi_destinations.ROTATION_REASONS:
        assert f"`{reason}`" in TEXT


def sequence_steps():
    for case in DOC["cases"]:
        if case["kind"] == "phi_sequence":
            for index, given in enumerate(case["input"]["steps"]):
                yield case, index, given


def cumulative(case, index):
    entries, events = [], []
    for given in case["input"]["steps"][: index + 1]:
        entries += wire(given)
        events += given["events"]
    return entries, events


def test_fold_is_independent_of_arrival_order():
    rng = random.Random(7)
    for case, index, given in sequence_steps():
        entries, events = cumulative(case, index)
        for _ in range(3):
            shuffled = entries[:]
            rng.shuffle(shuffled)
            shuffled_events = events[:]
            rng.shuffle(shuffled_events)
            got = phi_destinations.run_step(
                case["input"], shuffled, shuffled_events, given
            )
            assert got == step(case, index), (case["name"], given["name"])


def test_every_phi_epoch_and_wrap_opens_for_its_recipients():
    private = {
        device["id"]: testkit.TestDevice(device["label"]).wrap_private
        for device in DEVICES.values()
    }
    opened: dict[str, set[bytes]] = {}
    checked = 0
    for case, _, given in sequence_steps():
        for entry in wire(given):
            body = entry["body"]
            if entry["type"] == "epoch" and body["group"] == "phi":
                assert body["recovery_wrap"] is None
                eid = phi_destinations.entry_id(entry)
                for item in body["wraps"]:
                    secret = keys.open_epoch_key(body, item["device"], private[item["device"]])
                    opened.setdefault(case["name"] + eid, set()).add(secret)
                    checked += 1
            if entry["type"] == "wrap" and body["group"] == "phi":
                for grant in body["grants"]:
                    secret = crypto.unwrap_key(
                        private[body["device"]],
                        grant["wrap"],
                        keys.wrap_aad("phi", body["device"]),
                    )
                    opened.setdefault(case["name"] + grant["epoch"], set()).add(secret)
                    checked += 1
    assert checked > 20
    assert all(len(secrets) == 1 for secrets in opened.values())


def test_entries_carry_presence_exactly_where_the_type_needs_it():
    presence = {"genesis", "add", "remove", "phi_authority", "phi_approval", "resume"}
    for case, _, given in sequence_steps():
        for name, entry in zip(given["entry_names"], wire(given), strict=True):
            if name.startswith("bad_") or entry["author"] == "recovery":
                continue
            assert ("presence_sig" in entry) == (entry["type"] in presence), (
                case["name"],
                name,
            )


def test_no_node_registers_an_approver_or_writes_a_grant():
    node = ID["node"]
    for case, _, given in sequence_steps():
        for entry in wire(given):
            if entry["type"] in ("phi_authority", "phi_approval"):
                assert entry["author"] != node, case["name"]


def test_writers_only_use_device_and_endpoint_subject_kinds():
    for case, _, given in sequence_steps():
        for name, entry in zip(given["entry_names"], wire(given), strict=True):
            if entry["type"] == "phi_approval" and entry["body"]["action"] == "grant":
                kind = entry["body"]["attestation"]["subject_kind"]
                if not name.startswith(("legacy_", "bad_")):
                    assert kind in ("device", "endpoint"), (case["name"], name)


def test_contact_and_endpoint_subjects_never_enter_the_group():
    for case, index, given in sequence_steps():
        members = step(case, index)["roster"]["phi_members"]
        assert all(m.startswith("sha256:") for m in members)
        assert not any(m.startswith(("contact:", "local:", "http")) for m in members)


ADOPT = "seq_adopt_group"


def test_adoption_grants_marked_members_then_writes_the_first_epoch():
    given, got = step_by_name(ADOPT, "existing_mesh")
    assert got["roster"]["phi_members"] == []
    assert got["roster"]["current_epoch"]["phi"] is None
    assert {NAME[r["device"]]: r["state"] for r in got["group"]} == {
        "alice": "not_upgraded",
        "bob": "not_upgraded",
        "node": "not_upgraded",
        "dave": "not_upgraded",
    }
    assert set(got["local"].values()) == {"phi.group_not_ready"}
    _, got = step_by_name(ADOPT, "markers")
    assert names(got["duties"][ID["alice"]]["grants"]) == ["alice", "bob", "node"]
    assert names(got["duties"][ID["bob"]]["grants"]) == ["alice", "bob", "node"]
    assert got["duties"][ID["node"]]["grants"] == []
    assert got["duties"][ID["dave"]] == {"grants": [], "epoch": False, "wraps": {}}
    assert {NAME[r["device"]]: r["state"] for r in got["group"]}["dave"] == "not_upgraded"
    assert got["plans"] == [
        {
            "ok": {
                "register_approver": True,
                "grants": sorted([ID["alice"], ID["bob"], ID["node"]]),
                "epoch": True,
                "wraps": {},
            }
        },
        {"error": "phi_group.node_device"},
        {"error": "phi_group.not_upgraded"},
    ]
    _, got = step_by_name(ADOPT, "adopt_grants")
    assert names(got["roster"]["phi_members"]) == ["alice", "bob", "node"]
    assert got["roster"]["current_epoch"]["phi"] is None
    assert got["duties"][ID["bob"]]["epoch"] is True
    assert got["duties"][ID["alice"]]["epoch"] is False
    assert set(got["local"].values()) == {"phi.group_not_ready"}
    _, got = step_by_name(ADOPT, "adopt_epoch")
    current = got["roster"]["current_epoch"]["phi"]
    assert names(got["roster"]["recipients"][current]) == ["alice", "bob", "node"]
    assert got["local"][ID["dave"]] == "phi.group_not_ready"
    assert all(got["local"][ID[n]] is None for n in ("alice", "bob", "node"))
    assert all(d == {"grants": [], "epoch": False, "wraps": {}} for d in got["duties"].values())


def test_group_plans_refuse_before_any_prompt():
    _, got = step_by_name(ADOPT, "adopt_epoch")
    assert got["plans"] == [
        {"error": "phi_group.unknown_device"},
        {"error": "phi_group.already_included"},
        {"error": "phi_group.unknown_device"},
        {"error": "phi_group.not_included"},
        {"error": "phi_group.unknown_device"},
        {"error": "phi_group.node_device"},
    ]


def test_every_documented_code_has_a_vector():
    seen = set()
    for case in DOC["cases"]:
        seen |= set(re.findall(r'"((?:phi_destination|phi_group|phi_marker|phi)\.[a-z_]+)"', json.dumps(case["expect"])))
        if case["kind"] == "phi_chat_key" and case["input"]["local"]:
            seen.add(case["input"]["local"])
    table = TEXT.split("## 9. Codes")[1].split("## 10.")[0]
    documented = set(re.findall(r"`((?:phi_destination|phi_group|phi_marker|phi)\.[a-z_]+)`", table))
    assert documented == seen


def test_pairing_waits_for_the_marker_and_the_grant_carries_a_wrap():
    _, got = step_by_name(ADOPT, "pair_carol")
    assert {NAME[r["device"]]: r["state"] for r in got["group"]}["carol"] == "not_upgraded"
    assert got["duties"][ID["alice"]]["grants"] == []
    _, got = step_by_name(ADOPT, "carol_marker")
    assert got["duties"][ID["alice"]]["grants"] == [ID["carol"]]
    assert got["duties"][ID["node"]]["grants"] == []
    epochs = list(step_by_name(ADOPT, "adopt_epoch")[1]["roster"]["recipients"])
    assert got["plans"] == [
        {
            "ok": {
                "register_approver": False,
                "grants": [ID["carol"]],
                "epoch": False,
                "wraps": {ID["carol"]: [e for e in epochs if e == got["roster"]["current_epoch"]["phi"]]},
            }
        }
    ]
    _, got = step_by_name(ADOPT, "grant_carol")
    assert names(got["roster"]["phi_members"]) == ["alice", "bob", "carol", "node"]
    current = got["roster"]["current_epoch"]["phi"]
    assert ID["carol"] in got["roster"]["recipients"][current]


def test_exclusion_rotates_and_a_revoked_device_is_not_granted_again():
    _, got = step_by_name(ADOPT, "grant_carol")
    assert got["plans"] == [
        {"ok": {"revoke": ID["bob"], "rotate": True, "rekey_needed": False}}
    ]
    before = got["roster"]["current_epoch"]["phi"]
    _, got = step_by_name(ADOPT, "exclude_bob")
    after = got["roster"]["current_epoch"]["phi"]
    assert after != before
    assert names(got["roster"]["recipients"][after]) == ["alice", "carol", "node"]
    assert {NAME[r["device"]]: r["state"] for r in got["group"]}["bob"] == "excluded"
    assert got["local"][ID["bob"]] == "phi.device_excluded"
    assert got["plans"] == [
        {"ok": {"revoke": ID["alice"], "rotate": False, "rekey_needed": True}}
    ]
    _, got = step_by_name(ADOPT, "bob_not_granted_again")
    assert all(d["grants"] == [] for d in got["duties"].values())
    assert {NAME[r["device"]]: r["state"] for r in got["group"]}["bob"] == "excluded"


def test_self_exclusion_leaves_the_rotation_to_the_lowest_upgraded_member():
    _, got = step_by_name(ADOPT, "self_exclude")
    assert names(got["roster"]["phi_members"]) == ["carol", "node"]
    keeper = min(ID["carol"], ID["node"])
    assert NAME[keeper] == "node"
    assert got["duties"][keeper]["epoch"] is True
    assert got["duties"][ID["carol"]]["epoch"] is False
    assert got["local"][ID["alice"]] == "phi.device_excluded"
    _, got = step_by_name(ADOPT, "rekey")
    current = got["roster"]["current_epoch"]["phi"]
    assert names(got["roster"]["recipients"][current]) == ["carol", "node"]
    assert got["duties"][keeper] == {"grants": [], "epoch": False, "wraps": {}}
    plan = got["plans"][0]["ok"]
    assert plan["register_approver"] is True
    assert plan["grants"] == [ID["bob"]]
    assert len(plan["wraps"][ID["bob"]]) == 2


def test_an_explicit_include_wraps_every_epoch_the_granter_holds():
    _, got = step_by_name(ADOPT, "include_bob")
    assert names(got["roster"]["phi_members"]) == ["bob", "carol", "node"]
    holds = [
        epoch
        for epoch, who in got["roster"]["recipients"].items()
        if got["roster"]["epochs"][epoch] == "phi" and ID["carol"] in who
    ]
    assert len(holds) == 3
    assert all(ID["bob"] in got["roster"]["recipients"][e] for e in holds)
    assert {NAME[r["device"]]: r["approved_by"] for r in got["group"]}["bob"] == ID["carol"]


def test_create_writes_the_self_grant_and_first_epoch():
    _, got = step_by_name("seq_create_and_pair", "create")
    assert names(got["roster"]["phi_members"]) == ["alice"]
    current = got["roster"]["current_epoch"]["phi"]
    assert names(got["roster"]["recipients"][current]) == ["alice"]
    assert got["local"][ID["alice"]] is None
    _, got = step_by_name("seq_create_and_pair", "bob_marker")
    assert got["duties"][ID["alice"]]["grants"] == [ID["bob"]]
    assert got["duties"][ID["bob"]]["grants"] == [ID["bob"]]
    assert got["plans"][1] == {
        "ok": {"register_approver": True, "grants": [ID["bob"]], "epoch": False, "wraps": {}}
    }
    _, got = step_by_name("seq_create_and_pair", "grant_bob")
    assert names(got["roster"]["phi_members"]) == ["alice", "bob"]
    assert names(got["roster"]["recipients"][current]) == ["alice", "bob"]


def test_removal_by_a_deployed_device_leaves_a_rekey_to_the_group():
    _, got = step_by_name("seq_removal_by_deployed_device", "deployed_removes_bob")
    assert names(got["roster"]["phi_members"]) == ["alice", "node"]
    assert got["duties"][ID["alice"]]["epoch"] is True
    assert got["duties"][ID["node"]]["epoch"] is False
    _, got = step_by_name("seq_removal_by_deployed_device", "rekey")
    current = got["roster"]["current_epoch"]["phi"]
    assert names(got["roster"]["recipients"][current]) == ["alice", "node"]


def test_removal_by_a_phi_member_rotates_both_groups():
    before = step_by_name("seq_removal_by_phi_member", "group_ready")[1]["roster"]
    _, got = step_by_name("seq_removal_by_phi_member", "member_removes_bob")
    roster = got["roster"]
    assert roster["current_epoch"]["personal"] != before["current_epoch"]["personal"]
    assert roster["current_epoch"]["phi"] != before["current_epoch"]["phi"]
    assert all(d["epoch"] is False for d in got["duties"].values())


def test_markers_count_only_from_the_named_current_member():
    _, got = step_by_name("seq_markers", "markers")
    assert names(got["markers"]["devices"]) == ["alice", "bob", "node"]
    given = CASES["seq_markers"]["input"]["steps"][0]
    labelled = {
        event["id"]: name
        for name, event in zip(given["event_names"], given["events"], strict=True)
    }
    assert {labelled[i]: code for i, code in got["markers"]["ignored"].items()} == {
        "foreign_feed": "phi_marker.foreign_feed",
        "removed_device": "phi_marker.not_member",
        "stranger": "phi_marker.not_member",
        "wrong_value": "phi_marker.malformed",
        "null_value": "phi_marker.malformed",
        "version_zero": "phi_marker.malformed",
        "version_text": "phi_marker.malformed",
        "version_true": "phi_marker.malformed",
        "chat_scope": "phi_marker.malformed",
        "wrong_labels": "phi_marker.malformed",
        "extra_body_field": "phi_marker.malformed",
        "uppercase_device": "phi_marker.malformed",
        "trailing_segment": "phi_marker.malformed",
        "order_malformed_before_member": "phi_marker.malformed",
        "order_member_before_feed": "phi_marker.not_member",
    }
    states = {NAME[r["device"]]: r["state"] for r in got["group"]}
    assert states["dave"] == "not_upgraded"
    assert states["node"] == "not_approved"


def test_revoke_beats_a_concurrent_grant_and_a_later_grant_stands():
    _, got = step_by_name("seq_concurrent_revoke_and_grant", "concurrent")
    assert names(got["roster"]["phi_members"]) == ["alice", "carol"]
    status = {r["subject"]: r["status"] for r in got["destinations"]}
    assert status[M] == "revoked"
    _, got = step_by_name("seq_concurrent_revoke_and_grant", "grant_after_revoke")
    assert names(got["roster"]["phi_members"]) == ["alice", "bob", "carol"]
    status = {r["subject"]: r["status"] for r in got["destinations"]}
    assert status[M] == "approved"


def test_destination_statuses_and_answers():
    given, got = step_by_name("seq_destinations", "approvals")
    status = {r["subject"]: (r["status"], r["code"]) for r in got["destinations"]}
    assert status == {
        M: ("approved", None),
        S: ("approved", None),
        local("alice"): ("approved", None),
        local("bob"): ("approved", None),
        "contact:" + CONTACT: ("contact_missing", None),
        "http://127.0.0.1:9000/v1": ("invalid", "phi_destination.loopback_unbound"),
        "https://*.model.example/v1": ("invalid", WILD),
        "https://API.model.example/v2": ("invalid", "phi_destination.not_canonical"),
        "urn:example:destination": ("unsupported", None),
        "node-7": ("unsupported", None),
        CONTACT: ("unsupported", None),
    }
    assert all(
        r["approved_by"] == ID["alice"]
        for r in got["destinations"]
        if r["status"] in ("approved", "contact_missing")
    )
    asked = [
        (q["device"], q["kind"], q["target"]) for q in given["queries"]
    ]
    answers = dict(zip(asked, got["queries"], strict=True))
    a, d = ID["alice"], ID["dave"]
    assert answers[(a, "model", M)] is True
    assert answers[(a, "model", "HTTPS://API.model.example/v1/")] is True
    assert answers[(a, "service", M)] is True
    assert answers[(a, "service", S)] is True
    assert answers[(a, "model", "https://other.model.example/v1")] is False
    assert answers[(a, "model", "http://localhost:11434/v1")] is True
    assert answers[(d, "model", "http://localhost:11434/v1")] is False
    assert answers[(d, "model", M)] is False
    assert answers[(a, "service", LOOP)] is False
    assert answers[(a, "service", local("alice"))] is True
    assert answers[(a, "service", local("bob"))] is False
    b = ID["bob"]
    assert answers[(b, "service", local("bob"))] is True
    assert answers[(b, "service", local("alice"))] is False
    assert answers[(a, "model", local("alice"))] is False
    assert answers[(a, "contact", CONTACT)] is False
    assert answers[(a, "model", "https://*.model.example/v1")] is False
    assert answers[(a, "device", ID["bob"])] is True
    assert answers[(a, "device", ID["dave"])] is False
    assert answers[(a, "printer", M)] is False
    assert answers[(a, "contact", "sha256:ab")] is False
    assert answers[(a, "service", "local:" + a + ":http://LOCALHOST:8765/mcp")] is False
    _, got = step_by_name("seq_destinations", "contact_verified")
    status = {r["subject"]: r["status"] for r in got["destinations"]}
    assert status["contact:" + CONTACT] == "approved"
    assert got["queries"] == [True]
    _, got = step_by_name("seq_destinations", "revoke_service")
    assert {r["subject"]: r["status"] for r in got["destinations"]}[S] == "revoked"
    assert got["queries"] == [False]
    _, got = step_by_name("seq_destinations", "grant_again")
    assert {r["subject"]: r["status"] for r in got["destinations"]}[S] == "approved"
    assert got["queries"] == [True]


def test_approvals_need_a_current_approver_device():
    _, got = step_by_name("seq_approver_removed", "remove_bob")
    status = {r["subject"]: r["status"] for r in got["destinations"]}
    assert status["https://api.bob.example/v1"] == "approver_removed"
    assert status[local("bob")] == "approver_removed"
    assert status[local("bob", "http://127.0.0.1:8766/mcp")] == "device_removed"
    assert status[M] == "approved"
    assert got["queries"] == [False, True]
    _, got = step_by_name("seq_approver_removed", "restored_device_approves")
    status = {r["subject"]: r["status"] for r in got["destinations"]}
    assert status["https://api.bob.example/v1"] == "approved"
    row = next(r for r in got["destinations"] if r["subject"] == "https://api.bob.example/v1")
    assert row["approved_by"] == ID["bob_restored"]
    assert got["queries"] == [True]


def test_legacy_authorities_approve_devices_but_no_destination():
    _, got = step_by_name("seq_legacy_authority", "legacy_authorities")
    status = {r["subject"]: r["status"] for r in got["destinations"]}
    assert status["https://api.clinic.example/v1"] == "legacy_authority"
    assert status["https://api.other.example/v1"] == "legacy_authority"
    assert status["https://api.shared.example/v1"] == "legacy_authority"
    assert names(got["roster"]["phi_members"]) == ["alice", "bob", "carol"]
    rows = {NAME[r["device"]]: r for r in got["group"]}
    assert rows["carol"]["state"] == "included" and rows["carol"]["approved_by"] is None
    assert got["duties"][ID["bob"]]["wraps"] == {
        ID["carol"]: [got["roster"]["current_epoch"]["phi"]]
    }
    assert got["queries"] == [False]


def test_the_newest_grant_decides_with_no_fallback():
    _, got = step_by_name("seq_legacy_authority", "authority_removed")
    status = {r["subject"]: r["status"] for r in got["destinations"]}
    assert status["https://api.clinic.example/v1"] == "authority_removed"
    assert status["https://api.shared.example/v1"] == "authority_removed"
    assert status["https://api.other.example/v1"] == "legacy_authority"
    assert names(got["roster"]["phi_members"]) == ["alice", "bob"]
    rows = {NAME[r["device"]]: r["state"] for r in got["group"]}
    assert rows["carol"] == "excluded"
    assert got["local"][ID["carol"]] == "phi.device_excluded"


FOLD_REJECTIONS = {
    "fold_epoch_concurrent_with_grants": "roster.epoch_wrong_members",
    "fold_phi_epoch_by_non_member": "roster.not_phi_member",
    "fold_wrap_to_non_member": "roster.epoch_wrong_members",
    "fold_wrap_by_non_member": "roster.not_phi_member",
    "fold_wrap_unknown_epoch": "roster.unknown_epoch",
    "fold_wrap_personal_epoch_as_phi": "roster.unknown_epoch",
    "fold_recovery_phi_epoch": "roster.recovery_not_allowed",
    "fold_grant_without_presence": "roster.malformed",
    "fold_grant_bad_presence": "roster.bad_presence_signature",
    "fold_grant_unknown_authority": "attestation.unknown_authority",
    "fold_grant_bad_statement_signature": "attestation.bad_signature",
    "fold_grant_malformed_statement": "attestation.malformed",
    "fold_grant_for_removed_device": "roster.unknown_device",
    "fold_authority_remove_unknown": "roster.unknown_authority",
    "fold_grant_by_recovery": "roster.recovery_not_allowed",
    "fold_authority_without_presence": "roster.malformed",
    "fold_grant_by_stranger": "roster.unauthorized_author",
}


@pytest.mark.parametrize("name", sorted(FOLD_REJECTIONS))
def test_fold_rejection(name):
    got = step(CASES[name], 0)
    labelled = entry_names(name)
    rejected = {labelled[i]: code for i, code in got["roster"]["rejected"].items()}
    assert rejected == {"bad_" + name[len("fold_") :]: FOLD_REJECTIONS[name]}


def test_void_entries_by_a_removed_device_have_no_effect():
    got = step(CASES["fold_void_after_removal"], 0)
    labelled = entry_names("fold_void_after_removal")
    assert [labelled[i] for i in got["roster"]["void"]] == ["bob_grant_concurrent"]
    status = {r["subject"]: r["status"] for r in got["destinations"]}
    assert "https://api.void.example/v1" not in status


def test_pending_entries_have_no_effect():
    got = step(CASES["fold_pending_entry"], 0)
    labelled = entry_names("fold_pending_entry")
    assert [labelled[i] for i in got["roster"]["pending"]] == ["grant_after_missing"]
    assert names(got["roster"]["phi_members"]) == ["alice"]


def test_an_expired_approval_counts_nowhere():
    got = step(CASES["fold_expired_approval"], 0)
    assert names(got["roster"]["phi_members"]) == ["alice"]
    status = {r["subject"]: r["status"] for r in got["destinations"]}
    assert status["https://api.short.example/v1"] == "expired"
    assert {NAME[r["device"]]: r["state"] for r in got["group"]}["bob"] == "excluded"


def test_an_endpoint_subject_naming_a_device_still_admits_it():
    got = step(CASES["fold_endpoint_subject_naming_a_device"], 0)
    assert names(got["roster"]["phi_members"]) == ["alice", "dave"]
    assert all(r["subject"] != ID["dave"] for r in got["destinations"])


def test_document_lists_every_code_and_status():
    codes = set(re.findall(r"`((?:phi_destination|phi_group|phi_marker|phi)\.[a-z_]+)`", TEXT))
    seen = set()
    for case in DOC["cases"]:
        seen |= set(re.findall(r'"((?:phi_destination|phi_group|phi_marker|phi)\.[a-z_]+)"', json.dumps(case["expect"])))
    assert seen <= codes
    for status in phi_destinations.STATUSES:
        assert f"`{status}`" in TEXT
    for state in phi_destinations.GROUP_STATES:
        assert f"`{state}`" in TEXT


def test_every_case_kind_is_documented():
    kinds = {case["kind"] for case in DOC["cases"]}
    assert kinds == {
        "phi_endpoint",
        "phi_subject",
        "phi_contact",
        "phi_propose",
        "phi_approve",
        "phi_statement",
        "phi_sequence",
        "phi_chat_key",
    }
    for kind in kinds:
        assert f"`{kind}`" in TEXT


def test_a_member_seals_only_after_rotating_a_stale_phi_epoch():
    for case_name, step_name, rotating, quiet in (
        (ADOPT, "self_exclude", ["carol", "node"], ["alice", "bob"]),
        ("seq_removal_by_deployed_device", "deployed_removes_bob", ["alice", "node"], ["bob", "dave"]),
        ("seq_concurrent_revoke_and_grant", "concurrent", ["alice", "carol"], ["bob"]),
    ):
        _, got = step_by_name(case_name, step_name)
        current = got["roster"]["current_epoch"]["phi"]
        outside = set(got["roster"]["recipients"][current]) - set(got["roster"]["phi_members"])
        assert outside, (case_name, step_name)
        flags = {NAME[d]: flag for d, flag in got["rotate_first"].items()}
        assert sorted(n for n, flag in flags.items() if flag) == sorted(rotating)
        assert all(not flags.get(n, False) for n in quiet)
        assert all(got["local"][ID[n]] is None for n in rotating)


def test_no_member_rotates_first_while_the_current_epoch_is_clean():
    for case in DOC["cases"]:
        if case["kind"] != "phi_sequence":
            continue
        for got in case["expect"]["steps"]:
            current = got["roster"]["current_epoch"]["phi"]
            phi = set(got["roster"]["phi_members"])
            stale = bool(current) and bool(set(got["roster"]["recipients"][current]) - phi)
            for device, flag in got["rotate_first"].items():
                assert flag == (stale and device in phi), (case["name"], device)


def test_a_rotated_epoch_ends_the_stale_window():
    _, got = step_by_name(ADOPT, "rekey")
    assert not any(got["rotate_first"].values())
    _, got = step_by_name("seq_removal_by_deployed_device", "rekey")
    assert not any(got["rotate_first"].values())


def test_registration_follows_the_local_key_and_a_current_device_authority():
    name = "seq_approver_registration"
    _, got = step_by_name(name, "registered")
    assert [p["ok"]["register_approver"] for p in got["plans"]] == [False, False, True, False, True]
    _, got = step_by_name(name, "authority_removed")
    assert [p["ok"]["register_approver"] for p in got["plans"][:2]] == [True, False]
    _, got = step_by_name(name, "foreign_authority")
    plans = got["plans"]
    assert [p["ok"]["register_approver"] for p in plans[:2]] == [False, True]


def test_an_include_plan_refuses_an_empty_list_and_drops_duplicates():
    _, got = step_by_name("seq_approver_registration", "registered")
    assert got["plans"][0]["ok"]["grants"] == [ID["bob"]]
    assert got["plans"][1]["ok"]["grants"] == [ID["bob"]]
    _, got = step_by_name("seq_approver_registration", "authority_removed")
    assert got["plans"][2] == {"error": "phi_group.no_target"}
    assert got["plans"][3] == {"error": "phi_group.node_device"}


def test_duplicate_copies_choose_the_verifying_one_then_the_smallest_bytes():
    got = step(CASES["fold_duplicate_verifying_copy_beats_smaller_bytes"], 0)
    assert got["roster"]["rejected"] == {}
    assert names(got["roster"]["phi_members"]) == ["alice", "bob"]
    case = CASES["fold_duplicate_all_copies_fail"]
    got = step(case, 0)
    given = case["input"]["steps"][0]
    copies = {
        name: text
        for name, text in zip(given["entry_names"], given["entries"], strict=True)
        if name.startswith("bad_duplicate_")
    }
    assert len(copies) == 2
    smallest = min(copies, key=lambda n: copies[n].encode())
    assert smallest == "bad_duplicate_presence_garbled"
    assert list(got["roster"]["rejected"].values()) == ["roster.bad_presence_signature"]
