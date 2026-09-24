"""Tests that retained legacy agent families (agents/medical/mcp_tools.py,
orchestrator/local_agents.py) stay importable and registration-safe, with a routed
behavior suite each; drift guards only, not integrated acceptance.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "specs/088-rewrite-integration/source-inventory.json"

BEHAVIOR_SUITES = {
    "conversation_and_research": ("tests/test_inprocess_dispatch.py",
                                  "agents/web_research/tests/test_registry_contract.py",
                                  "tests/test_workspace_export_088.py"),
    "voice_and_local_speech": ("tests/test_voice_local_protocol_075.py",
                               "tests/test_voice_backend_rollback_075.py"),
    "uploads_and_parsing": ("tests/test_chat_attachments_ownership.py",
                            "shared/tests/test_attachment_resolver.py"),
    "authored_skills_and_commands": ("tests/test_skill_packs.py",
                                     "tests/test_skill_toggle_resolution.py"),
    "memory_and_personalization": ("orchestrator/tests/test_memory_chat.py",
                                   "personalization/tests/test_memory_guard.py"),
    "persistent_and_scheduled_work": ("persistent_agents/tests/test_runner.py",
                                      "persistent_agents/tests/test_dispatch.py"),
    "declarative_and_executable_agents": ("tests/test_user_agent_registry_plane_074.py",),
    "framework_transports": ("tests/test_a2a_bridge_agent_id.py",),
    "office_developer_creative_connectors": ("tests/test_connectors.py",),
    "remote_compute_and_computer_control": ("agents/tests/test_remote_verbs_contract.py",
                                            "tests/test_remote_confirmation_063.py"),
    "specialist_services": ("agents/ml_services/tests/test_union_registry.py",
                            "agents/summarizer/tests/test_registry_contract.py"),
    "identity_and_client_delivery": ("tests/test_native_work_compat_088.py",),
}


def retained_rows():
    return json.loads(INVENTORY.read_text())["retained_existing_capabilities"]


def test_every_retained_family_has_a_behavior_suite_route():
    rows = retained_rows()
    identities = [row["id"] for row in rows]
    assert len(identities) == len(set(identities))
    assert set(identities) == set(BEHAVIOR_SUITES)
    for suites in BEHAVIOR_SUITES.values():
        assert suites
        for relative in suites:
            path = ROOT / "backend" / relative
            assert path.is_file()
            tree = ast.parse(path.read_text())
            assert any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and node.name.startswith("test_") for node in ast.walk(tree)), relative


def inventoried_agents():
    return sorted({Path(source["path"]).parent.name
                   for row in retained_rows() for source in row["source_files"]
                   if source["repository"] == "astraldeep"
                   and source["path"].startswith("backend/agents/")
                   and source["path"].endswith("_agent.py")})


def test_retained_agents_keep_default_and_opt_in_registration_boundaries():
    from orchestrator.local_agents import (
        BUILT_IN_AGENT_DIRS, _COMPUTER_USE_AGENT_DIRS, _REMOTE_COMPUTE_AGENT_DIRS,
        discover_built_in_agent_dirs,
    )

    default = set(BUILT_IN_AGENT_DIRS)
    remote, computer = set(_REMOTE_COMPUTE_AGENT_DIRS), set(_COMPUTER_USE_AGENT_DIRS)
    assert remote == {"remote_compute"} and computer == {"computer_use"}
    assert not default.intersection(remote | computer)
    assert default | remote | computer == set(inventoried_agents())
    assert set(discover_built_in_agent_dirs()) == default


@pytest.mark.parametrize("directory", inventoried_agents())
def test_each_retained_agent_remains_importable_without_starting_a_service(directory):
    from orchestrator.local_agents import _load_agent_class
    from shared.base_agent import BaseA2AAgent

    selected = _load_agent_class(directory)
    assert selected is not None and issubclass(selected, BaseA2AAgent)
    assert selected.__module__ == f"agents.{directory}.{directory}_agent"


def test_clinical_example_search_stays_synthetic_and_read_only():
    from agents.medical.mcp_tools import MOCK_PATIENTS, TOOL_REGISTRY

    before = json.loads(json.dumps(MOCK_PATIENTS))
    tool = TOOL_REGISTRY["search_patients"]
    assert tool["scope"] == "tools:read"
    result = tool["function"](min_age=41, max_age=45, condition="degenerative")
    assert result["_data"]["total"] == 1
    assert [patient["id"] for patient in result["_data"]["patients"]] == ["P-1024"]
    assert result["_ui_components"] and all(isinstance(item, dict)
                                            for item in result["_ui_components"])
    assert MOCK_PATIENTS == before
    assert TOOL_REGISTRY["generate_synthetic_patients"]["scope"] == "tools:write"


def test_empty_clinical_example_search_returns_an_honest_empty_result():
    from agents.medical.mcp_tools import TOOL_REGISTRY

    result = TOOL_REGISTRY["search_patients"]["function"](condition="no-fixture-match")
    assert result["_data"] is None
    assert result["_ui_components"][0]["type"] == "alert"
    assert "No patients found" in result["_ui_components"][0]["message"]
