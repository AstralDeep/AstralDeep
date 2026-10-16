"""Executes Cresco contract conformance against the present default-off bridge runtime.
The suite checks admission, safe-seed exclusion and the verified control dialect while
retaining explicit missing real-fabric and effect-transport qualification.
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import subprocess
from io import BytesIO
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CASES_PATH = REPO / "specs" / "095-cresco-bridge-contract" / "contracts" / "conformance-cases.json"
CONTRACT_PATH = REPO / "specs" / "095-cresco-bridge-contract" / "contracts" / "bridge-contract.md"

KNOWN_KINDS = {
    "flag_default_off",
    "bridge_runtime_gated",
    "gaiakeep_dual_flag",
    "envelope_roundtrip",
    "transport_boundaries",
    "target_grammar",
    "fault_states",
    "secret_hygiene",
    "manifest_hygiene",
    "source_pins",
}

CASE_BINDINGS = {
    "C-01": "test_c01_flag_default_off",
    "C-02": "test_c02_present_runtime_has_a_flag_off_kill_switch",
    "C-03": "test_c03_bridge_registration_requires_the_flag",
    "C-04": "test_c04_bridge_is_not_automatically_safe",
    "C-05": "test_c05_gaiakeep_requires_both_flags",
    "C-06": "test_c06_dual_flag_truth_table_and_read_failure",
    "C-07": "test_c07_envelope_shape_and_deterministic_encoding",
    "C-08": "test_c08_runtime_transport_boundaries",
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
        "bounded topology implemented behind default-off flag; effect transports unavailable; real fabric acceptance pending"
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


def test_c02_present_runtime_has_a_flag_off_kill_switch(monkeypatch):
    from agents.cresco.mcp_server import MCPServer
    from agents.cresco.wsapi_client import WsapiClient

    case = _case_by_id(_load_cases(), "C-02")
    _set_flag(monkeypatch, "cresco", False)
    def never_dial(*args, **kwargs):
        pytest.fail("flag-off bridge attempted a connection")
    monkeypatch.setattr(WsapiClient, "_connect", never_dial)
    result = MCPServer().invoke(case["expect"]["tool"], user_id="synthetic-owner")
    assert result["_error"]["data"]["detail"] == case["expect"]["fault_detail"]

@pytest.mark.asyncio
async def test_c03_bridge_registration_requires_the_flag(monkeypatch):
    from types import SimpleNamespace
    from agents.cresco import cresco_agent
    from orchestrator import local_agents
    from shared import attachment_materializer, attachment_resolver

    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs", list)
    monkeypatch.setattr(attachment_resolver, "register_plane_runtime", lambda *a: True)
    monkeypatch.setattr(attachment_materializer, "register_materialization_service", lambda *a: None)
    monkeypatch.setattr(cresco_agent.BaseA2AAgent, "_init_crypto",
                        lambda self: setattr(self, "_public_key_jwk", {"test": "ephemeral"}))
    for enabled in (False, True):
        for name in ("gaiakeep", "remote_compute", "computer_use", "fhir"):
            _set_flag(monkeypatch, name, False)
        _set_flag(monkeypatch, "cresco", enabled)
        cards = []
        async def register_agent(socket, message):
            cards.append(message.agent_card.agent_id)
        plane = SimpleNamespace(runtime=object(), repositories=object(), blobs=object(),
                                attachment_materializer=object())
        orch = SimpleNamespace(runtime_composition=SimpleNamespace(plane=plane),
                               local_agents={}, register_agent=register_agent)
        assert await local_agents.register_built_ins(orch) == (["cresco-1"] if enabled else [])
        assert cards == (["cresco-1"] if enabled else [])


@pytest.mark.asyncio
async def test_c04_bridge_is_not_automatically_safe(monkeypatch):
    from orchestrator.local_agents import FIRST_PARTY_PUBLIC_AGENT_IDS
    from tests.test_remote_orchestrator_wiring_063 import _drive_start

    case = _case_by_id(_load_cases(), "C-04")
    member = case["expect"]["member"]
    assert member in FIRST_PARTY_PUBLIC_AGENT_IDS
    _, seeded = await _drive_start(monkeypatch, remote_compute=False, gaiakeep=True)
    assert member not in seeded[0][1]

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


@pytest.mark.parametrize("reverse_keys", [False, True])
def test_c07_envelope_shape_and_deterministic_encoding(reverse_keys):
    from agents.cresco.wsapi_client import build_request, decode_bulk, encode_bulk, parse_frame, response_matches

    expect = _case_by_id(_load_cases(), "C-07")["expect"]
    request = expect["canonical_request"]
    assert build_request("listregions", {}, message_id="case-7") == request
    assert all(isinstance(value, str) for part in request.values() for value in part.values())
    sample = expect["bulk_params_sample"]
    value = dict(reversed(sample.items())) if reverse_keys else sample
    json_bytes = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    buffer = BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, compresslevel=9, mtime=0) as stream:
        stream.write(json_bytes)
    compressed = buffer.getvalue()
    assert compressed[:10] == bytes.fromhex("1f8b08000000000002ff")
    assert base64.b64encode(compressed).decode("ascii") == expect["bulk_params_encoding"]
    assert gzip.decompress(compressed) == json_bytes
    assert encode_bulk(value) == expect["bulk_params_encoding"]
    assert decode_bulk(expect["bulk_params_encoding"]) == expect["bulk_params_sample"]
    reply = parse_frame(json.dumps(expect["canonical_reply"]))
    assert response_matches(reply, "case-7")
    assert decode_bulk(reply["regionslist"]) == {"regions": []}

def test_c08_runtime_transport_boundaries(monkeypatch):
    import ssl
    from agents.cresco.wsapi_client import FabricConfig, FabricFault, WsapiClient, build_ssl_context

    monkeypatch.delenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", raising=False)
    config = FabricConfig(wsapi_url="wss://127.0.0.1:8282", service_key="synthetic",
                          tls_fingerprint="ab" * 32)
    context = build_ssl_context(config)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert config.control_url == "wss://127.0.0.1:8282/api/apisocket"
    with pytest.raises(FabricFault) as denied:
        WsapiClient(config).validate_egress()
    assert denied.value.state == "tls"
    with pytest.raises(FabricFault) as endpoint:
        WsapiClient(FabricConfig(wsapi_url="wss://127.0.0.1/api/dataplane", service_key="synthetic")).validate_egress()
    assert endpoint.value.state == "protocol"

def test_c09_member_target_grammar():
    case = _case_by_id(_load_cases(), "C-09")
    expect = case["expect"]
    from agents.cresco.targets import TargetRefused, parse_target, require_approved

    grammar = re.compile(expect["grammar"])
    env = {"CRESCO_APPROVED_TARGETS": ",".join(expect["valid"])}
    for target in expect["valid"]:
        assert grammar.fullmatch(target), target
        assert "_".join(require_approved(target, env)) == target
    for target in expect["invalid"]:
        assert grammar.fullmatch(target) is None, target
        with pytest.raises(TargetRefused):
            parse_target(target)
    with pytest.raises(TargetRefused):
        require_approved("r9_unadmitted", env)

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
    profile = json.loads((CONTRACT_PATH.parent / "wsapi-wire-profile.json").read_text())
    assert profile["status"] == _load_cases()["status"]
    assert profile["requests"]["list_regions"] == _case_by_id(_load_cases(), "C-07")["expect"]["canonical_request"]
    from agents.cresco.wsapi_client import build_request
    assert build_request("agentinfo", {"region_agent": "r1_a1"}, message_id="case-agent") == profile["requests"]["agent_info"]
    assert build_request("getsysinfo", {"region_agent": "r1_a1"}, message_id="case-sys") == profile["requests"]["sysinfo"]
    for source in profile["sources"]:
        assert re.fullmatch(r"[a-f0-9]{40}", source["commit"])
        assert re.fullmatch(r"[a-f0-9]{64}", source["sha256"])
        assert source["repository"].startswith("https://github.com/CrescoEdge/")
        assert source["path"] and source["status"] == "source-verified"
        assert source["retrieved"] == "2026-10-13"
    unreverified = [s for s in expect["sources"] if s["status"] == "pinned-unreverified"]
    assert unreverified, "the contract must carry its unreverified external pins honestly"


@pytest.mark.parametrize("case_id", sorted(CASE_BINDINGS))
def test_contract_case_registry_stays_complete(case_id):
    cases = _load_cases()
    _case_by_id(cases, case_id)
    assert f"**{case_id}**" in _contract_text()
