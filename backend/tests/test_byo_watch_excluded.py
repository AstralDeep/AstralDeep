"""Feature 058 (US4, T023) — watchOS must NOT host or author BYO agents.

Hosting a BYO agent means supervising a CHILD PROCESS on a desktop — a
capability the watch does not have. Authoring is likewise excluded from the
wrist. This is enforced/observed server-side by two facts, both pinned here:

  1. The ONLY affordance that opens BYO authoring is the flag-gated "My agents"
     menu item (``menu_model``). It appears iff ``FF_BYO_AGENTS`` is on — there
     is no second authoring entry point.

  2. The watch is deliberately CHROME-FREE: it is absent from
     ``chrome_events._NATIVE_SDUI_DEVICE_TYPES`` — the device list that governs
     whether a client receives the SDUI settings menu (``chrome_menu``) and gets
     a settings surface RENDERED (``_render_surface`` → ``_render_surface_sdui``).
     Because the watch never receives the menu and never gets a surface rendered,
     the "My agents" item and the ``agent_authoring`` surface never reach a watch.
     (This is the exact "chrome_events device list" the authoring surface's own
     docstring cites for watch exclusion, FR-023.)

HONEST scope note (host marking): a socket is marked a desktop host purely by
its own ``register_ui agent_host`` declaration / by relaying stdio frames
(``orchestrator._agent_host_sockets``); there is no server-side device-class
gate on that marker. The watch is excluded from HOSTING on the CLIENT side — the
watch app ships no authoring/host UI and never declares ``agent_host``. The
assertable server-side half is the chrome device list above, which keeps the
authoring surface off the wrist in the first place.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from webrender.chrome import menu_model as mm  # noqa: E402
from orchestrator import chrome_events as ce  # noqa: E402


def _authoring_items(model):
    out = []
    for group in model.menu:
        for item in group.items:
            if item.surface == "agent_authoring":
                out.append(item)
    return out


# ---------------------------------------------------------------------------
# 1. The single, flag-gated authoring affordance.
# ---------------------------------------------------------------------------

def test_my_agents_item_present_only_when_flag_on():
    on = _authoring_items(mm.build_menu_model([], byo_enabled=True))
    off = _authoring_items(mm.build_menu_model([], byo_enabled=False))
    assert len(on) == 1, "exactly one BYO authoring entry point"
    assert on[0].key == "my-agents" and on[0].label == "My agents & skills"
    assert off == [], "flag off ⇒ no authoring affordance on any client"


def test_my_agents_is_the_only_authoring_surface_entry_point():
    """Guard against a second entry point sneaking in: across every group and
    role, ``agent_authoring`` is reachable ONLY via the one "My agents" item."""
    for roles in ([], ["user"], ["admin"], ["user", "admin"]):
        model = mm.build_menu_model(roles, byo_enabled=True)
        assert len(_authoring_items(model)) == 1


# ---------------------------------------------------------------------------
# 2. The watch is chrome-free — it never receives the menu or a surface.
# ---------------------------------------------------------------------------

def test_watch_absent_from_chrome_sdui_device_list():
    """The device list that gates SDUI chrome-menu delivery + surface rendering
    excludes the watch; the four chrome-bearing natives are present."""
    assert "watch" not in ce._NATIVE_SDUI_DEVICE_TYPES
    assert set(ce._NATIVE_SDUI_DEVICE_TYPES) == {"windows", "android", "ios", "macos"}


def test_watch_menu_and_authoring_surface_unreachable_end_to_end():
    """End-to-end for the assertable server half: even with the BYO flag ON, a
    watch device (a) is not among the clients that receive the settings menu
    carrying "My agents", and (b) is not among the clients a settings surface is
    rendered for — so ``agent_authoring`` is never opened on a watch."""
    # (a) The menu model itself contains "My agents" (flag on) …
    model = mm.build_menu_model(["user"], byo_enabled=True)
    assert len(_authoring_items(model)) == 1
    # … but the watch is not a chrome-menu / surface recipient.
    assert "watch" not in ce._NATIVE_SDUI_DEVICE_TYPES
    # And the four devices that DO author/manage BYO all receive chrome.
    for native in ("windows", "android", "ios", "macos"):
        assert native in ce._NATIVE_SDUI_DEVICE_TYPES
