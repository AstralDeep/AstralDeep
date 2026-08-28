"""Composition-level drift guards against Projection's UI protocol owner."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
COMPOSITION_PATH = ROOT / "config" / "astral-composition.json"
VOICE_LOCAL_SCHEMA_PATH = (
    ROOT
    / "specs"
    / "075-client-local-speech"
    / "contracts"
    / "voice-local.schema.json"
)

UI_SEND_MODULES = (
    "orchestrator/orchestrator.py",
    "orchestrator/chrome_events.py",
    "orchestrator/async_tasks.py",
    "orchestrator/chat_steps.py",
    "orchestrator/stream_manager.py",
    "orchestrator/api.py",
    "orchestrator/agentic_creation.py",
    "orchestrator/agent_lifecycle.py",
    "scheduler/runner.py",
    "audit/ws_publisher.py",
    "llm_config/ws_handlers.py",
    "shared/protocol.py",
)

NON_PUSH_TYPES = {
    "ui_event",
    "register_ui",
    "register_agent",
    "mcp_request",
    "mcp_response",
    "llm_config_set",
    "llm_config_clear",
    "tool_stream_data",
    "tool_stream_end",
    "tool_stream_cancel",
    "voice_playout_event",
    "ping",
    "pong",
    "close",
    "cancel",
    "cancel_task",
    "agent_hop_request",
    "agent_hop_response",
    "string",
    "object",
    "array",
    "function",
    "json_object",
    "json_schema",
    "raw",
}

_TYPE_LITERAL = re.compile(r'"type": "([a-z_]+)"')
_DATACLASS_DEFAULT = re.compile(r'type: str = "([a-z_]+)"')
_ACTION_LITERAL = re.compile(r'action == "([a-z_]+)"')
_CHROME_KEY = re.compile(r'"((?:chrome|draft|revision)_[a-z_]+)"\s*:')


def _voice_local_types() -> set[str]:
    """Read the separately owned speech sideband vocabulary."""

    document = json.loads(VOICE_LOCAL_SCHEMA_PATH.read_text(encoding="utf-8"))
    discovered: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            constant = value.get("const")
            if isinstance(constant, str) and constant.startswith("voice_local_"):
                discovered.add(constant)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(document)
    return discovered


def _canonical_json_sha256(document: object) -> str:
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _composition() -> dict[str, object]:
    document = json.loads(COMPOSITION_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _projection_manifest() -> tuple[Path, dict[str, object]]:
    composition = _composition()
    component = composition["components"]["astral-projection"]  # type: ignore[index]
    component_path = ROOT / component["path"]  # type: ignore[index]
    manifest_path = component_path / "contracts" / "ui_protocol.json"
    assert manifest_path.is_file(), (
        "AstralProjection is missing or uninitialized; run "
        "git submodule update --init components/AstralProjection"
    )
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return manifest_path, document


def test_projection_manifest_matches_composition_and_parent_gitlink() -> None:
    composition = _composition()
    component = composition["components"]["astral-projection"]  # type: ignore[index]
    compatibility = composition["compatibility"]["ui_protocol"]  # type: ignore[index]
    manifest_path, manifest = _projection_manifest()

    assert str(manifest["version"]) == compatibility["version"]  # type: ignore[index]
    assert _canonical_json_sha256(manifest) == compatibility["sha256"]  # type: ignore[index]

    index_entry = subprocess.run(
        ["git", "ls-files", "-s", "--", component["path"]],  # type: ignore[index]
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert index_entry[0] == "160000"
    assert index_entry[1] == component["commit"]  # type: ignore[index]
    assert manifest_path.is_relative_to(ROOT / component["path"])  # type: ignore[index]

    assert not (BACKEND / "shared" / "ui_protocol.json").exists(), (
        "AstralDeep must consume Projection's protocol owner, not retain a mirror"
    )


def test_server_push_literals_are_declared_by_projection() -> None:
    _, manifest = _projection_manifest()
    declared = {
        entry["name"]
        for entry in manifest["push_types"]  # type: ignore[index]
    }
    declared.update(manifest["component_types"])  # type: ignore[arg-type]
    declared.update(NON_PUSH_TYPES)
    # Feature 075 speech sideband frames are not Projection UI primitives or
    # chrome pushes. Their exact vocabulary is owned by the separately pinned
    # voice-local schema and is still drift-checked here rather than waived.
    declared.update(_voice_local_types())

    missing: dict[str, list[str]] = {}
    for relative in UI_SEND_MODULES:
        source = (BACKEND / relative).read_text(encoding="utf-8")
        literals = set(_TYPE_LITERAL.findall(source)) | set(
            _DATACLASS_DEFAULT.findall(source)
        )
        for name in sorted(literals - declared):
            missing.setdefault(name, []).append(relative)

    assert not missing, f"server frame types missing from Projection: {missing}"


def test_server_ui_actions_are_declared_by_projection() -> None:
    _, manifest = _projection_manifest()
    declared = set(manifest["accept_actions"])  # type: ignore[arg-type]
    source = (BACKEND / "orchestrator" / "orchestrator.py").read_text(encoding="utf-8")
    actions = set(_ACTION_LITERAL.findall(source))
    actions -= {"block", "modify", "session_resumed"}

    for relative in (
        "orchestrator/chrome_events.py",
        "orchestrator/agentic_creation.py",
    ):
        actions.update(
            _CHROME_KEY.findall((BACKEND / relative).read_text(encoding="utf-8"))
        )
    actions -= {"draft_id", "draft_status", "revision_staged"}

    assert not (actions - declared), (
        f"server UI actions missing from Projection: {sorted(actions - declared)}"
    )
