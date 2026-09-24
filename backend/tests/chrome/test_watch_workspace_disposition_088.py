"""Tests the production device gate in orchestrator/chrome_availability.py in isolation,
without an authenticated register_ui lifecycle around it.
"""

import ast
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import chrome_availability
from rote.capabilities import DeviceProfile


@pytest.fixture(scope="module")
def register_chrome_branch():
    source = Path(__file__).resolve().parents[2] / "orchestrator" / "orchestrator.py"
    tree = ast.parse(source.read_text())
    gates = [node for node in ast.walk(tree) if isinstance(node, ast.If)
             and any(isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                     and test.left.id == "_dt" for test in ast.walk(node.test))
             and any(isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                     and child.func.id == "ChromeMenu" for child in ast.walk(node))]
    assert len(gates) == 1, "registration must have one shared native chrome delivery gate"
    module = ast.parse("async def deliver(_dt, self, websocket, user_data):\n    pass\n")
    module.body[0].body = [copy.deepcopy(gates[0])]
    namespace = {}
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace["deliver"]


@pytest.mark.parametrize("device_type", ["watch", "ios", "android", "macos", "windows"])
@pytest.mark.parametrize("export,share", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.asyncio
async def test_register_chrome_omission_is_watch_specific(
    monkeypatch, register_chrome_branch, device_type, export, share,
):
    monkeypatch.delenv("ROTE_HOST_CONFIG", raising=False)
    monkeypatch.setattr(chrome_availability, "projection_chrome_availability", lambda: {
        "export_enabled": export, "share_enabled": share,
    })
    profile = DeviceProfile.from_dict({"device_type": device_type, "viewport_width": 390})
    host = SimpleNamespace(_safe_send=AsyncMock())
    socket = object()
    await register_chrome_branch(profile.device_type.value, host, socket,
                                 {"realm_access": {"roles": ["user"]}})
    if device_type == "watch":
        host._safe_send.assert_not_awaited()
        assert profile.supports_file_io is False
        return
    host._safe_send.assert_awaited_once()
    assert host._safe_send.await_args.args[0] is socket
    frame = json.loads(host._safe_send.await_args.args[1])
    assert frame["type"] == "chrome_menu" and frame["model"]["version"] == 2
    actions = [control["operation"] for control in frame["model"]["topbar"]
               if control["kind"] == "workspace_action"]
    assert actions == (["export_canvas"] if export else []) + (["share_canvas"] if share else [])
    assert profile.supports_file_io is True
