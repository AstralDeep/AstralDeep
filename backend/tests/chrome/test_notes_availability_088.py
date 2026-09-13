"""Private notes are advertised only on channels with response correlation.

The registration branch fixture executes the real delivery code; institutional
registration authentication and notes access are independently ingress-tested.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import chrome_availability
from orchestrator.api import get_chrome_menu
from tests.chrome.test_watch_workspace_disposition_088 import (
    register_chrome_branch as register_chrome_branch,
)
from webrender.chrome import render_topbar


@pytest.mark.parametrize("capabilities", [None, "guidance_notes_v1", [], ["work_read_v1"]])
def test_unnegotiated_native_notes_are_hidden(capabilities):
    values = chrome_availability.projection_native_chrome_availability(
        {"_client_capabilities": capabilities})
    assert values["notes_enabled"] is False


def test_web_notes_do_not_require_a_model_or_work_feature(monkeypatch):
    from shared.feature_flags import flags
    monkeypatch.setattr(flags, "is_enabled", lambda _: False)
    values = chrome_availability.projection_chrome_availability()
    html = render_topbar(roles=["user"], **values)
    assert "Private notes" in html and values["work_enabled"] is False


@pytest.mark.asyncio
async def test_rest_claim_hint_cannot_negotiate_notes_response_correlation():
    model = await get_chrome_menu({"realm_access": {"roles": ["user"]},
                                  "_client_capabilities": ["guidance_notes_v1"]})
    assert "guidance" not in json.dumps(model)


@pytest.mark.parametrize("device", ["watch", "ios", "macos", "android"])
@pytest.mark.asyncio
async def test_registered_native_receives_the_shared_notes_entry(device, register_chrome_branch):
    host, socket = SimpleNamespace(_safe_send=AsyncMock()), object()
    await register_chrome_branch(device, host, socket,
        {"realm_access": {"roles": ["user"]}, "_client_capabilities": ["guidance_notes_v1"]})
    host._safe_send.assert_awaited_once()
    frame = json.loads(host._safe_send.await_args.args[1])
    model = frame["model"]
    entries = (model["topbar"] if device == "watch" else
               [item for group in model["menu"] for item in group["items"]])
    notes = [item for item in entries if item["key"] == "guidance"]
    assert len(notes) == 1
    assert notes[0]["label"] == "Private notes"
    action = notes[0]["action"] if device == "watch" else {
        key: notes[0][key] for key in ("surface", "params")}
    assert action == {"surface": "guidance", "params": {"mode": "list"}}
    if device == "watch":
        assert model["menu"] == [] and [item["key"] for item in entries] == ["guidance"]
