"""Holds the node to one turn engine: only the mesh bridge reaches the Kernel's answer_request, no production module carries an entry point of the retired Python loop, and model clients and protected tool dispatch are called only from the modules named here.
A new caller fails this file until someone lists it with its reason."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from orchestrator import node_mesh_admission
from orchestrator.node_mesh_chat import MeshChatBridge
from orchestrator.orchestrator import Orchestrator

BACKEND = Path(__file__).resolve().parents[1]
RETIRED = (
    "handle_chat_message",
    "_handle_chat_message_impl",
    "_handle_chat_message_with_guidance",
    "_serialized_chat",
    "_dispatch_async_chat",
    "_handle_voice_local_final",
    "_typesafe_set_turn_verdict",
    "_typesafe_turn_requires_confirmation",
    "run_scheduled_turn",
    "execute_parallel_tools",
    "orchestrator_runner",
    "continue_after_approval",
)
RETIRED_ADMISSION = ("turn", "Turn", "current_turn", "TURN_MARK")
TURN_STARTERS = {
    "orchestrator/node_mesh.py": "wraps the Kernel binding's answer_request",
    "orchestrator/node_mesh_chat.py": "answers a mesh text request on the Kernel engine",
}
MODEL_CALLERS = {
    "agents/general/mcp_tools.py": "a bundled agent's tool calls a model inside one gated tool call",
    "agents/summarizer/mcp_tools.py": "a bundled agent's tool calls a model inside one gated tool call",
    "agents/web_research/mcp_tools.py": "a bundled agent's tool calls a model inside one gated tool call",
    "answer_behavior/drivers/live.py": "an offline evaluation driver",
    "clinical_diagnostic/provider.py": "a bundled clinical tool's provider adapter",
    "llm_config/api.py": "the provider connection test",
    "llm_config/client_factory.py": "builds provider clients",
    "llm_config/evidence_transport.py": "the evidence transport's provider adapter",
    "llm_config/probe.py": "the provider capability probe",
    "orchestrator/agent_authoring.py": "one model call that drafts an agent, with no tool loop",
    "orchestrator/agent_generator.py": "one model call that generates agent code, with no tool loop",
    "orchestrator/evidence_context.py": "one model call that summarizes evidence, with no tool loop",
    "orchestrator/knowledge_synthesis.py": "background synthesis, with no tool loop",
    "orchestrator/memory_chat.py": "a memory meta tool's single model call",
    "orchestrator/orchestrator.py": "the single-call model helpers the modules above share",
}
DISPATCH_CALLERS = {
    "orchestrator/a2a_orchestrator_executor.py": "the A2A door",
    "orchestrator/api.py": "legacy standalone routes, absent from the personal node",
    "orchestrator/node_mesh_calls.py": "a Kernel-ticketed call from the mesh",
    "orchestrator/orchestrator.py": "the protected dispatch itself and the MCP door's helper",
    "orchestrator/projection_surfaces/agents.py": "an owner surface action",
    "verification/drivers/in_process.py": "the verification harness",
}
MODEL_CALL = re.compile(
    r"\._call_llm(?:_guarded|_streamed|_json)?\(|chat\.completions\.create|responses\.create|\.messages\.create\("
)


def production_sources() -> dict[str, str]:
    found = {}
    for path in BACKEND.rglob("*.py"):
        relative = path.relative_to(BACKEND)
        if ("tests" in relative.parts or relative.name.startswith("test_") or relative.name == "conftest.py"
                or ".venv" in relative.parts or "data" in relative.parts):
            continue
        found[relative.as_posix()] = path.read_text(encoding="utf-8", errors="ignore")
    return found


SOURCES = production_sources()


def callers(pattern: str | re.Pattern) -> set[str]:
    compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
    return {name for name, text in SOURCES.items() if compiled.search(text)}


def test_only_the_mesh_bridge_starts_a_turn():
    assert len(SOURCES) > 500
    assert callers(r"\.answer_request\(") == set(TURN_STARTERS)


@pytest.mark.parametrize("name", RETIRED)
def test_no_production_module_names_a_retired_loop_entry(name):
    word = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
    assert callers(word) == set()
    assert not hasattr(Orchestrator, name)


def test_the_bridge_takes_no_python_runner_and_admission_binds_calls_only():
    assert "runner" not in inspect.signature(MeshChatBridge.__init__).parameters
    assert not any(hasattr(node_mesh_admission, name) for name in RETIRED_ADMISSION)
    authority = inspect.signature(node_mesh_admission.personal_authority).parameters["agent_id"]
    assert authority.default is inspect.Parameter.empty and authority.kind is inspect.Parameter.KEYWORD_ONLY


def test_model_clients_are_called_only_from_named_modules():
    assert callers(MODEL_CALL) == set(MODEL_CALLERS)


def test_protected_dispatch_is_entered_only_from_named_modules():
    assert callers(r"execute_authorized_tool\(") == set(DISPATCH_CALLERS)
