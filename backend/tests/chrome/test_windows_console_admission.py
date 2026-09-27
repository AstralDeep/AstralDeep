"""Checks the native resize producer against the real connection admission boundary.
The extracted producer needs no Qt runtime in backend CI.
"""

import ast
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
import uuid

import pytest

from orchestrator.orchestrator import Orchestrator


def resize_frame(connection):
    source = Path(__file__).resolve().parents[3] / "components/AstralProjection/windows-client/astral_client/protocol.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    client = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OrchestratorClient")
    method = next(node for node in client.body if isinstance(node, ast.FunctionDef) and node.name == "update_device")
    namespace = {"copy": copy, "json": json, "uuid": uuid, "Optional": Optional, "WindowsProtocolError": ValueError}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
    frames = []

    def send(frame, **kwargs):
        assert kwargs["is_current"]()
        frames.append({**frame, "connection_generation": connection})
        return True

    producer = SimpleNamespace(_device_update_generation=0, session_id="win-client", _send_current_frame=send)
    assert namespace["update_device"](producer, {
        "device_type": "windows", "console_contract": "console/v2", "viewport_width": 390,
        "viewport_height": 844, "supported_types": ["text"],
    })
    return frames[0]


@pytest.mark.parametrize("mutation", [None, "missing_submission", "missing_request", "mismatched_request", "stale_connection"])
def test_windows_resize_uses_existing_admission_and_retains_refusals(mutation):
    connection = str(uuid.uuid4())
    frame = resize_frame(connection)
    if mutation in {"missing_submission", "missing_request"}:
        key = "submission_id" if mutation == "missing_submission" else "request_generation"
        frame.pop(key)
        frame["payload"].pop(key)
    elif mutation == "mismatched_request":
        frame["payload"]["request_generation"] = str(uuid.uuid4())
    elif mutation == "stale_connection":
        frame["connection_generation"] = str(uuid.uuid4())
    host = Orchestrator.__new__(Orchestrator)
    context = SimpleNamespace(connection_generation=uuid.UUID(connection))
    admitted = host._connection_frame(context, json.dumps(frame), frame)
    if mutation is not None:
        assert admitted is None
    else:
        assert admitted.action == "update_device"
        assert admitted.chat_id is None
        assert admitted.parsed["payload"]["device"]["viewport_width"] == 390
