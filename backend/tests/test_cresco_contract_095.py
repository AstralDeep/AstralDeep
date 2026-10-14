"""Executes the bounded general Cresco bridge contract: every conformance case in
specs/095-cresco-bridge-contract/contracts/conformance-cases.json runs against the live
tree, and the repository fails closed if it drifts from the contract's bounded posture.
The suite is deterministic and offline: it verifies contract and posture, not bridge
runtime, which does not exist in this snapshot.
"""

from __future__ import annotations

import base64
import gzip
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CASES_PATH = REPO / "specs" / "095-cresco-bridge-contract" / "contracts" / "conformance-cases.json"
CONTRACT_PATH = REPO / "specs" / "095-cresco-bridge-contract" / "contracts" / "bridge-contract.md"

KNOWN_KINDS = {
    "flag_default_off",
    "bridge_runtime_absent",
    "gaiakeep_dual_flag",
    "envelope_roundtrip",
    "profile_boundary_text",
    "target_grammar",
    "fault_states",
    "secret_hygiene",
    "manifest_hygiene",
    "source_pins",
}

CASE_BINDINGS = {
    "C-01": "test_c01_flag_default_off",
    "C-02": "test_c02_bridge_package_absent",
    "C-03": "test_c03_bridge_not_registered",
    "C-04": "test_c04_bridge_absent_from_public_registry",
    "C-05": "test_c05_gaiakeep_requires_both_flags",
    "C-06": "test_c06_dual_flag_truth_table_and_read_failure",
    "C-07": "test_c07_envelope_shape_and_deterministic_encoding",
    "C-08": "test_c08_profile_boundary_text",
    "C-09": "test_c09_member_target_grammar",
    "C-10": "test_c10_fault_state_table",
    "C-11": "test_c11_no_committed_credential_material",
    "C-12": "test_c12_no_bridge_artifacts_in_manifests",
    "C-13": "test_c13_source_pins_carry_identifier_status_and_date",
}


def _load_cases() -> dict:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _case_by_id(cases: dict, case_id: str) -> dict:
    for case in cases["cases"]:
        if case["id"] == case_id:
            return case
    raise AssertionError(f"missing conformance case {case_id}")


def _contract_text() -> str:
    return CONTRACT_PATH.read_text(encoding="utf-8")


def _tracked_files(scan_paths: list[str]) -> list[Path]:
    listing = subprocess.run(
        ["git", "ls-files", "--", *scan_paths],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO / line for line in listing.stdout.splitlines() if line]


def test_case_inventory_is_wellformed():
    cases = _load_cases()
    assert cases["status"] == (
        "contract ratified and bounded; bridge implementation not started; no runtime activation"
    )
    assert cases["contract"] == "specs/095-cresco-bridge-contract/contracts/bridge-contract.md"
    ids = [case["id"] for case in cases["cases"]]
    assert len(ids) == len(set(ids))
    for case in cases["cases"]:
        assert re.fullmatch(r"C-\d{2}", case["id"])
        assert case["kind"] in KNOWN_KINDS
        assert case["title"]
        assert isinstance(case["expect"], dict)


def test_every_case_is_bound_to_contract_and_test():
    cases = _load_cases()
    text = _contract_text()
    json_ids = {case["id"] for case in cases["cases"]}
    assert json_ids == set(CASE_BINDINGS)
    for case_id, test_name in CASE_BINDINGS.items():
        assert f"**{case_id}**" in text, f"{case_id} missing from the contract document"
        bound = globals().get(test_name)
        assert callable(bound), f"{case_id} bound to missing test {test_name}"


def test_c01_flag_default_off(monkeypatch):
    case = _case_by_id(_load_cases(), "C-01")
    expect = case["expect"]
    monkeypatch.delenv(expect["env_var"], raising=False)
    from shared.feature_flags import FeatureFlags

    assert FeatureFlags().is_enabled(expect["flag"]) is expect["expected_default"]


def test_c02_bridge_package_absent():
    case = _case_by_id(_load_cases(), "C-02")
    import agents  # noqa: F401

    assert importlib.util.find_spec(case["expect"]["module"]) is None


def test_c03_bridge_not_registered():
    case = _case_by_id(_load_cases(), "C-03")
    from orchestrator.local_agents import BUILT_IN_AGENT_DIRS, discover_built_in_agent_dirs

    assert case["expect"]["member"] not in BUILT_IN_AGENT_DIRS
    assert case["expect"]["member"] not in discover_built_in_agent_dirs()


def test_c04_bridge_absent_from_public_registry():
    case = _case_by_id(_load_cases(), "C-04")
    from orchestrator.local_agents import FIRST_PARTY_PUBLIC_AGENT_IDS

    assert case["expect"]["member"] not in FIRST_PARTY_PUBLIC_AGENT_IDS


def _set_flag(monkeypatch, name: str, value: bool) -> None:
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, name, value)


def test_c05_gaiakeep_requires_both_flags(monkeypatch):
    case = _case_by_id(_load_cases(), "C-05")
    import start

    probe = case["expect"]["probe"]
    _set_flag(monkeypatch, "gaiakeep", probe["gaiakeep"])
    _set_flag(monkeypatch, "cresco", probe["cresco"])
    assert start._gaiakeep_enabled() is probe["expected"]


def test_c06_dual_flag_truth_table_and_read_failure(monkeypatch):
    case = _case_by_id(_load_cases(), "C-06")
    import start
    from shared.feature_flags import flags

    for row in case["expect"]["truth_table"]:
        _set_flag(monkeypatch, "gaiakeep", row["gaiakeep"])
        _set_flag(monkeypatch, "cresco", row["cresco"])
        assert start._gaiakeep_enabled() is row["expected"], row

    def unavailable(name):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(flags, "is_enabled", unavailable)
    assert start._gaiakeep_enabled() is case["expect"]["flag_read_failure_result"]


def test_c07_envelope_shape_and_deterministic_encoding():
    case = _case_by_id(_load_cases(), "C-07")
    expect = case["expect"]
    request = expect["canonical_request"]
    assert set(request) == {"message_info", "message_payload"}
    assert request["message_info"]["is_rpc"] is True
    assert isinstance(request["message_payload"]["action"], str)

    sample = expect["bulk_params_sample"]
    json_bytes = json.dumps(sample, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = base64.b64encode(gzip.compress(json_bytes, compresslevel=9, mtime=0)).decode("ascii")
    assert encoded == expect["bulk_params_encoding"]
    assert json.loads(gzip.decompress(base64.b64decode(encoded))) == sample


def test_c08_profile_boundary_text():
    case = _case_by_id(_load_cases(), "C-08")
    text = _contract_text()
    for phrase in case["expect"]["required_phrases"]:
        assert phrase in text, f"contract lost profile statement: {phrase}"


def test_c09_member_target_grammar():
    case = _case_by_id(_load_cases(), "C-09")
    expect = case["expect"]
    grammar = re.compile(expect["grammar"])
    for target in expect["valid"]:
        assert grammar.fullmatch(target), target
    for target in expect["invalid"]:
        assert grammar.fullmatch(target) is None, target

    widest = "_".join(["a" * 63] * 3)
    assert len(widest) == 191 and grammar.fullmatch(widest)
    assert expect["max_length"] >= len(widest)


def test_c10_fault_state_table():
    case = _case_by_id(_load_cases(), "C-10")
    states = case["expect"]["states"]
    assert set(states) == {"unavailable", "unreachable", "auth", "protocol", "tls"}
    retryable = {name: entry["retryable"] for name, entry in states.items()}
    assert retryable == {
        "unavailable": False,
        "unreachable": True,
        "auth": False,
        "protocol": False,
        "tls": False,
    }
    results = [entry["result"] for entry in states.values()]
    assert len(results) == len(set(results))
    for entry in states.values():
        assert entry["trigger"]
        assert "cresco_service_key" not in entry["result"].lower()
        assert "CRESCO_SERVICE_KEY" not in entry["result"]


def test_c11_no_committed_credential_material():
    case = _case_by_id(_load_cases(), "C-11")
    expect = case["expect"]
    pattern = re.compile(expect["literal_pattern"])
    for path in _tracked_files(expect["scan_paths"]):
        text = path.read_text(encoding="utf-8", errors="replace")
        match = pattern.search(text)
        assert match is None, f"credential-like literal in {path.relative_to(REPO)}: {match!r}"


def test_c12_no_bridge_artifacts_in_manifests():
    case = _case_by_id(_load_cases(), "C-12")
    expect = case["expect"]
    for manifest in expect["manifests"]:
        text = (REPO / manifest).read_text(encoding="utf-8", errors="replace").lower()
        for term in expect["forbidden_terms"]:
            assert term not in text, f"forbidden term {term!r} in {manifest}"


def test_c13_source_pins_carry_identifier_status_and_date():
    case = _case_by_id(_load_cases(), "C-13")
    expect = case["expect"]
    assert expect["sources"]
    text = _contract_text()
    for source in expect["sources"]:
        assert source["name"] and source["identifier"]
        assert source["status"] in expect["required_status_values"]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", source["retrieved"])
        assert source["name"] in text or source["identifier"].split(":")[-1] in text
    unreverified = [s for s in expect["sources"] if s["status"] == "pinned-unreverified"]
    assert unreverified, "the contract must carry its unreverified external pins honestly"


@pytest.mark.parametrize("case_id", sorted(CASE_BINDINGS))
def test_contract_case_registry_stays_complete(case_id):
    cases = _load_cases()
    _case_by_id(cases, case_id)
    assert f"**{case_id}**" in _contract_text()
