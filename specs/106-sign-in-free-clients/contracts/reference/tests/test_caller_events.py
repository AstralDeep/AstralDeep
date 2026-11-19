"""Runs the caller event vectors through caller_events.py and holds the node's refusal table in backend/orchestrator/caller_tickets.py to the same codes and classes.
It reads the node's source as syntax, so the check needs none of the node's packages."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import caller_events

ROOT = Path(__file__).resolve().parents[5]
VECTORS = json.loads((Path(__file__).resolve().parents[2] / "vectors" / "caller-events-v1.json").read_text())
ORCHESTRATOR = ROOT / "backend" / "orchestrator"


def strings(path: Path) -> dict[str, str]:
    found = {}
    for statement in ast.parse(path.read_text()).body:
        if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
            for target in statement.targets:
                found[target.id] = statement.value.value
    return found


def node_tables() -> dict[str, set[str]]:
    names = strings(ORCHESTRATOR / "caller_tickets.py")
    mesh = strings(ORCHESTRATOR / "node_mesh.py")

    def value(node: ast.expr) -> str:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return names[node.id]
        assert isinstance(node, ast.Attribute) and node.value.id == "node_mesh"
        return mesh[node.attr]

    tables = {}
    for statement in ast.parse((ORCHESTRATOR / "caller_tickets.py").read_text()).body:
        if not isinstance(statement, ast.Assign):
            continue
        target = statement.targets[0].id
        if target in ("UNAUTHORIZED", "FORBIDDEN", "UNAVAILABLE"):
            tables[target] = {value(item) for item in statement.value.args[0].elts}
        if target == "REASONS":
            tables[target] = {value(key) for key in statement.value.keys}
    tables["names"] = names
    return tables


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
def test_the_reference_answers_every_vector(case):
    assert caller_events.run(case["kind"], case["input"]) == case["expect"]


def test_the_vector_header_matches_the_reference():
    assert VECTORS["format"] == caller_events.FORMAT
    assert VECTORS["notification_types"] == []
    assert [(entry["class"], entry["status"], tuple(entry["codes"])) for entry in VECTORS["classes"]] == list(caller_events.CLASSES)


def test_the_node_refuses_with_exactly_the_contract_codes():
    tables = node_tables()
    names = tables["names"]
    assert tables["UNAUTHORIZED"] | {names["BEARER_REFUSED"]} == set(caller_events.UNAUTHORIZED)
    assert tables["FORBIDDEN"] == set(caller_events.FORBIDDEN)
    assert tables["UNAVAILABLE"] | {names["MESH_UNAVAILABLE"], names["OWNER_UNBOUND"]} == set(caller_events.UNAVAILABLE)
    every = set(caller_events.UNAUTHORIZED) | set(caller_events.FORBIDDEN) | set(caller_events.UNAVAILABLE)
    assert tables["REASONS"] == every
    assert len(every) == len(caller_events.UNAUTHORIZED) + len(caller_events.FORBIDDEN) + len(caller_events.UNAVAILABLE)


def test_the_node_door_serves_the_contract_notification_types():
    manifest = ast.parse((ORCHESTRATOR / "assembly_manifest.py").read_text())
    declared = [
        statement.value for statement in manifest.body
        if isinstance(statement, ast.AnnAssign) and statement.target.id == "CALLER_NOTIFICATION_TYPES"
    ]
    assert len(declared) == 1 and ast.literal_eval(declared[0]) == caller_events.NOTIFICATION_TYPES
    door = (ORCHESTRATOR / "mcp_server_endpoint.py").read_text()
    assert door.count('"notificationTypes": list(CALLER_NOTIFICATION_TYPES)') == 1
    assert door.count('"notificationTypes"') == 1
