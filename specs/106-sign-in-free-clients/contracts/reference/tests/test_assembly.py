"""Runs the activation manifest vectors through assembly.py and through the node's backend/orchestrator/assembly_manifest.py, and holds config/astral-assembly.json to the contract, the pinned contract digests and the node's capability names.
The node's validator uses only the standard library, so it loads here by file path."""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
from pathlib import Path

import pytest

import assembly
import caller_events

ROOT = Path(__file__).resolve().parents[5]
VECTORS = json.loads((Path(__file__).resolve().parents[2] / "vectors" / "assembly-v0.json").read_text())
ORCHESTRATOR = ROOT / "backend" / "orchestrator"
MANIFEST = ROOT / "config" / "astral-assembly.json"


def load_node_validator():
    spec = importlib.util.spec_from_file_location("node_assembly_manifest", ORCHESTRATOR / "assembly_manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NODE = load_node_validator()


def node_run(kind: str, case: dict) -> dict:
    try:
        document = NODE.validate(copy.deepcopy(case["document"]))
        if kind == "runtime":
            NODE.check_runtime(document, case["capabilities"], case["notification_types"])
    except NODE.AssemblyError as refused:
        return {"code": refused.code}
    return {"ok": True}


def node_capabilities() -> list[str]:
    found = {}
    for statement in ast.parse((ORCHESTRATOR / "node_api.py").read_text()).body:
        if isinstance(statement, ast.Assign) and statement.targets[0].id in ("CAPABILITIES", "MESH_CAPABILITIES"):
            found[statement.targets[0].id] = list(ast.literal_eval(statement.value))
    return found["CAPABILITIES"] + found["MESH_CAPABILITIES"]


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
def test_the_reference_answers_every_vector(case):
    assert assembly.run(case["kind"], case["input"]) == case["expect"]


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
def test_the_node_validator_answers_every_vector(case):
    assert node_run(case["kind"], case["input"]) == case["expect"]


def test_the_node_manifest_is_valid_and_true():
    document = NODE.load(MANIFEST)
    capabilities = node_capabilities()
    case = {"document": document, "capabilities": capabilities, "notification_types": list(NODE.CALLER_NOTIFICATION_TYPES)}
    assert assembly.run("runtime", case) == {"ok": True}
    assert node_run("runtime", case) == {"ok": True}
    assert NODE.require(MANIFEST, capabilities, NODE.CALLER_NOTIFICATION_TYPES) == document


def test_the_node_manifest_pins_the_current_contract_documents():
    document = NODE.load(MANIFEST)
    NODE.check_contracts(document, ROOT)
    pinned = {entry["path"] for port in document["ports"].values() for entry in port["contracts"]}
    assert "specs/106-sign-in-free-clients/contracts/caller-events.md" in pinned
    assert all(path.startswith("specs/") for path in pinned)


def test_a_changed_or_absent_contract_document_is_refused(tmp_path):
    document = NODE.load(MANIFEST)
    with pytest.raises(NODE.AssemblyError) as absent:
        NODE.check_contracts(document, tmp_path)
    assert absent.value.code == "assembly.contract_missing"
    for port in document["ports"].values():
        for entry in port["contracts"]:
            target = tmp_path / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / entry["path"]).read_bytes() + b"\n")
    with pytest.raises(NODE.AssemblyError) as changed:
        NODE.check_contracts(document, tmp_path)
    assert changed.value.code == "assembly.contract_digest"


def test_every_door_gives_its_principal_the_contract_consent_class():
    doors = NODE.load(MANIFEST)["doors"]
    assert set(doors) == {"mesh", "/mcp", "/a2a"}
    for name, door in doors.items():
        assert door["consent"] == caller_events.consent_class(door["principal"], door["turns"]), name
    assert [door["turns"] for door in doors.values() if door["principal"] == "caller-ticket"] == [False, False]


def test_the_two_validators_agree_on_the_fixed_vocabulary():
    assert NODE.FORMAT == assembly.FORMAT == VECTORS["format"]
    assert set(NODE.SECTIONS) == assembly.SECTIONS
    assert NODE.PORTS == assembly.PORTS
    assert NODE.MULTI_ADAPTER_PORTS == assembly.SEVERAL_ADAPTERS
    assert NODE.CONTROLS == assembly.CONTROLS
