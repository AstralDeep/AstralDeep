"""Covers the activation manifest validator in orchestrator/assembly_manifest.py and the boot refusal in orchestrator/boot_posture.py: the shipped manifest, every refusal code, the runtime comparison and the contract digests.
The contract vectors under specs/106-sign-in-free-clients/contracts run the same validator in the contract job."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from orchestrator import assembly_manifest, boot_posture, mcp_server_endpoint, node_api

CAPABILITIES = (*node_api.CAPABILITIES, *node_api.MESH_CAPABILITIES)
CONTRACT = {"format": "astral.caller/v1", "path": "contracts/caller-ticket.md", "sha256": "0f" * 32}
EXCEPTION = {"id": "arm64-pack", "date": "2026-11-18", "review": "owner", "summary": "A known gap."}


def base():
    return {
        "format": assembly_manifest.FORMAT,
        "assembly": "example.personal-node",
        "ports": {
            "identity": {"adapters": ["mesh-caller"], "contracts": [dict(CONTRACT)]},
            "authority": {"adapters": ["kernel-authz"], "contracts": []},
            "persistence": {"adapters": ["plane"], "contracts": []},
            "tool-provider": {"adapters": ["executor", "mcp-servers"], "contracts": []},
            "ui-adaptation": {"adapters": ["rote"], "contracts": []},
        },
        "capabilities": {"offered": sorted(CAPABILITIES), "required_controls": ["consent-gate", "egress-allowlist"]},
        "doors": {
            "mesh": {"principal": "owner-device", "turns": True, "notification_types": [], "consent": "full"},
            "/mcp": {"principal": "caller-ticket", "turns": False, "notification_types": [], "consent": "none"},
        },
        "exceptions": [dict(EXCEPTION)],
    }


def put(document, path, value):
    target = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return document


def drop(document, path):
    target = document
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    return document


def code_of(document):
    with pytest.raises(assembly_manifest.AssemblyError) as refused:
        assembly_manifest.validate(document)
    return refused.value.code


CONTRACTS = ("ports", "identity", "contracts")
REFUSALS = [
    ("assembly.malformed", lambda d: [d]),
    ("assembly.unknown_format", lambda d: drop(d, ("format",))),
    ("assembly.unknown_format", lambda d: put(d, ("format",), "astral.assembly/v1")),
    ("assembly.unknown_section", lambda d: put(d, ("flags",), {})),
    ("assembly.section_missing", lambda d: drop(d, ("doors",))),
    ("assembly.malformed", lambda d: put(d, ("assembly",), "Example Node")),
    ("assembly.malformed", lambda d: put(d, ("ports",), [])),
    ("assembly.unknown_port", lambda d: put(d, ("ports", "transport"), {"adapters": ["iroh"], "contracts": []})),
    ("assembly.port_missing", lambda d: drop(d, ("ports", "authority"))),
    ("assembly.unknown_key", lambda d: put(d, ("ports", "identity", "flags"), [])),
    ("assembly.malformed", lambda d: drop(d, CONTRACTS)),
    ("assembly.malformed", lambda d: put(d, ("ports", "identity", "adapters"), [])),
    ("assembly.malformed", lambda d: put(d, ("ports", "identity", "adapters"), "mesh-caller")),
    ("assembly.malformed", lambda d: put(d, ("ports", "tool-provider", "adapters"), ["executor", "executor"])),
    ("assembly.ambiguous", lambda d: put(d, ("ports", "identity", "adapters"), ["mesh-caller", "institutional"])),
    ("assembly.contract_malformed", lambda d: put(d, CONTRACTS, {})),
    ("assembly.contract_malformed", lambda d: put(d, CONTRACTS, ["astral.caller/v1"])),
    ("assembly.contract_malformed", lambda d: put(d, CONTRACTS, [dict(CONTRACT, version=1)])),
    ("assembly.contract_malformed", lambda d: put(d, CONTRACTS, [dict(CONTRACT, format="")])),
    ("assembly.contract_malformed", lambda d: put(d, CONTRACTS, [dict(CONTRACT, path="/etc/passwd")])),
    ("assembly.contract_malformed", lambda d: put(d, CONTRACTS, [dict(CONTRACT, path="specs/../secret.md")])),
    ("assembly.contract_malformed", lambda d: put(d, CONTRACTS, [dict(CONTRACT, sha256="0F" * 32)])),
    ("assembly.unknown_key", lambda d: put(d, ("capabilities", "optional"), [])),
    ("assembly.malformed", lambda d: put(d, ("capabilities", "offered"), ["tool-dispatch", "chat"])),
    ("assembly.unknown_control", lambda d: put(d, ("capabilities", "required_controls"), ["consent-gate", "firewall"])),
    ("assembly.malformed", lambda d: put(d, ("doors",), {})),
    ("assembly.unknown_key", lambda d: put(d, ("doors", "/mcp", "events"), [])),
    ("assembly.unknown_principal", lambda d: put(d, ("doors", "/mcp", "principal"), "contact")),
    ("assembly.malformed", lambda d: put(d, ("doors", "/mcp", "turns"), 0)),
    ("assembly.malformed", lambda d: put(d, ("doors", "/mcp", "consent"), "partial")),
    ("assembly.consent_visibility", lambda d: put(d, ("doors", "/mcp", "consent"), "full")),
    ("assembly.consent_visibility", lambda d: put(d, ("doors", "/mcp", "consent"), "own-redacted")),
    ("assembly.consent_visibility", lambda d: put(d, ("doors", "mesh", "consent"), "own-redacted")),
    ("assembly.exception_malformed", lambda d: put(d, ("exceptions",), {})),
    ("assembly.exception_malformed", lambda d: put(d, ("exceptions",), ["arm64-pack"])),
    ("assembly.exception_malformed", lambda d: put(d, ("exceptions",), [dict(EXCEPTION, review="")])),
    ("assembly.exception_malformed", lambda d: put(d, ("exceptions",), [dict(EXCEPTION, date="18 Nov 2026")])),
]


def test_the_shipped_manifest_is_valid_and_matches_what_the_doors_serve():
    document = assembly_manifest.require(
        boot_posture.assembly_path(), CAPABILITIES, assembly_manifest.CALLER_NOTIFICATION_TYPES,
    )
    assert document["format"] == assembly_manifest.FORMAT
    assert document["capabilities"]["offered"] == sorted(CAPABILITIES)
    callers = [door for door in document["doors"].values() if door["principal"] == "caller-ticket"]
    assert callers and all(door["turns"] is False and door["consent"] == "none" for door in callers)
    assert mcp_server_endpoint.CALLER_NOTIFICATION_TYPES is assembly_manifest.CALLER_NOTIFICATION_TYPES == ()


def test_a_valid_document_passes_and_a_caller_turn_door_may_only_be_redacted():
    assert assembly_manifest.validate(base())["assembly"] == "example.personal-node"
    redacted = put(base(), ("doors", "/turns"), {
        "principal": "caller-ticket", "turns": True, "notification_types": ["consent.blocked"], "consent": "own-redacted",
    })
    assert assembly_manifest.validate(redacted) is redacted
    assert code_of(put(redacted, ("doors", "/turns", "consent"), "full")) == "assembly.consent_visibility"


@pytest.mark.parametrize("expected,change", REFUSALS, ids=[f"{index}-{code}" for index, (code, _) in enumerate(REFUSALS)])
def test_each_broken_document_is_refused_with_its_code(expected, change):
    assert code_of(change(copy.deepcopy(base()))) == expected


def test_the_runtime_check_compares_capabilities_and_caller_notifications():
    document = assembly_manifest.validate(base())
    assembly_manifest.check_runtime(document, list(reversed(CAPABILITIES)), ())
    with pytest.raises(assembly_manifest.AssemblyError) as missing:
        assembly_manifest.check_runtime(document, CAPABILITIES + ("voice",), ())
    assert missing.value.code == "assembly.capability_mismatch"
    with pytest.raises(assembly_manifest.AssemblyError) as undeclared:
        assembly_manifest.check_runtime(document, CAPABILITIES, ("notifications/progress",))
    assert undeclared.value.code == "assembly.notification_mismatch"


def test_contract_digests_are_checked_against_the_files(tmp_path):
    document = assembly_manifest.validate(base())
    with pytest.raises(assembly_manifest.AssemblyError) as absent:
        assembly_manifest.check_contracts(document, tmp_path)
    assert absent.value.code == "assembly.contract_missing"
    target = tmp_path / CONTRACT["path"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b"contract")
    with pytest.raises(assembly_manifest.AssemblyError) as changed:
        assembly_manifest.check_contracts(document, tmp_path)
    assert changed.value.code == "assembly.contract_digest"
    document["ports"]["identity"]["contracts"][0]["sha256"] = hashlib.sha256(b"contract").hexdigest()
    assembly_manifest.check_contracts(document, tmp_path)


def test_load_refuses_a_missing_file_bad_json_and_a_duplicate_key(tmp_path):
    with pytest.raises(assembly_manifest.AssemblyError) as missing:
        assembly_manifest.load(tmp_path / "absent.json")
    assert missing.value.code == "assembly.missing"
    for text in ("{", '{"format": "a", "format": "b"}'):
        path = tmp_path / "manifest.json"
        path.write_text(text)
        with pytest.raises(assembly_manifest.AssemblyError) as malformed:
            assembly_manifest.load(path)
        assert malformed.value.code == "assembly.malformed"


def test_boot_accepts_the_shipped_manifest_and_refuses_a_missing_or_untrue_one(tmp_path, monkeypatch, caplog):
    boot_posture.refuse_invalid_assembly()
    untrue = put(base(), ("capabilities", "offered"), ["chat"])
    path = tmp_path / assembly_manifest.FILE
    for content, code in ((None, "assembly.missing"), (untrue, "assembly.capability_mismatch")):
        if content is not None:
            path.write_text(json.dumps(content))
        monkeypatch.setattr(boot_posture, "assembly_path", lambda: path)
        caplog.clear()
        with pytest.raises(SystemExit) as stopped:
            boot_posture.refuse_invalid_assembly()
        assert stopped.value.code == 78
        assert boot_posture.ASSEMBLY_REFUSED in caplog.text and code in caplog.text
