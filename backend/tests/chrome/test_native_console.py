"""Verifies negotiated native console catalogs retain authenticated owner filtering.
The delivery helper extends negotiated chrome without changing legacy output.
"""

from types import SimpleNamespace

import pytest

from orchestrator.native_console import attach_native_console
from orchestrator import web_landing
from rote.capabilities import DeviceProfile
from tests.chrome.test_surface_agents import make_orch


def profile(kind="ios", contract="console/v2"):
    return SimpleNamespace(device_type=SimpleNamespace(value=kind), console_contract=contract)


@pytest.mark.parametrize("kind", ["ios", "macos", "android", "watch"])
async def test_native_console_uses_owner_filtered_shared_landing(kind):
    orch = make_orch()
    menu = {"version": 2, "topbar": [], "menu": []}
    claims = {"sub": "u1", "name": "Alice", "realm_access": {"roles": ["user"]}}
    result = await attach_native_console(orch, menu, claims, profile(kind))
    assert "console" not in menu
    catalog = await web_landing.payload(orch, "u1")
    if kind == "watch":
        for agent in catalog["agents"]:
            agent["availability"] = {"mode": "handoff", "message": "Continue on your phone or desktop."}
    assert result["console"]["catalog"] == catalog
    agents = result["console"]["catalog"]["agents"]
    assert {row["id"] for row in agents} == {"alpha", "beta"}
    assert next(row for row in agents if row["id"] == "alpha")["owned"]
    assert result["console"]["identity"] == {"name": "Alice", "role": "Member", "initials": "A"}
    assert "owner_email" not in str(result)


@pytest.mark.parametrize("kind,contract", [("windows", "console/v2"), ("watch", ""),
                                          ("ios", "console/v3"), ("ios", None)])
async def test_legacy_and_deferred_clients_keep_exact_menu(kind, contract):
    menu = {"version": 2, "topbar": [], "menu": []}
    assert await attach_native_console(None, menu, {"sub": "u1"}, profile(kind, contract)) is menu


@pytest.mark.parametrize("claims", [{}, {"sub": ""}, {"sub": None}])
async def test_missing_authenticated_subject_cannot_receive_catalog(claims):
    menu = {}
    assert await attach_native_console(None, menu, claims, profile()) is menu


async def test_catalog_failure_does_not_publish_foreign_or_cached_agents(monkeypatch):
    async def fail(*args):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(web_landing, "payload", fail)
    with pytest.raises(RuntimeError, match="unavailable"):
        await attach_native_console(None, {}, {"sub": "u1"}, profile())


async def test_empty_profile_keeps_legacy_menu():
    menu = {}
    assert await attach_native_console(None, menu, {"sub": "u1"}, SimpleNamespace()) is menu


async def test_watch_console_actions_use_qualified_surface_capabilities():
    device = DeviceProfile.from_dict({"device_type": "watch", "console_contract": "console/v2",
        "supported_types": ["text", "alert", "badge", "card", "container", "button", "keyvalue"]})
    claims = {"sub": "u1", "_client_capabilities": ["guidance_selection_v1", "work_read_v1"]}
    menu = {"topbar": [{"key": "work", "kind": "action", "label": "Recent work", "icon": "briefcase",
                        "action": {"surface": "work", "params": {"mode": "list"}}}], "menu": []}
    result = await attach_native_console(make_orch(), menu, claims, device)
    actions = {item["key"]: item for item in result["console"]["composer_actions"]}
    assert actions["advanced"]["availability"] == actions["work"]["availability"] == {"mode": "native"}
    assert actions["background"]["availability"]["mode"] == "handoff"
    assert all(agent["availability"] == {"mode": "native"} for agent in result["console"]["catalog"]["agents"])
