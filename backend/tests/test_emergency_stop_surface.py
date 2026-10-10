"""Exercise shared emergency stop presentation and current-owner action controls.
Work mirrors stop status; Safety uses the same durable coordinator as owner REST.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.emergency_stop import EmergencyStopCoordinator  # noqa: E402
from orchestrator.projection_surfaces import safety as safety_surface  # noqa: E402
from orchestrator.projection_surfaces import work as work_surface  # noqa: E402


OWNER = "surface-owner-1"
CLAIMS = {"sub": OWNER, "realm_access": {"roles": ["user"]}}
WS = object()


def _orch(coordinator) -> SimpleNamespace:
    return SimpleNamespace(emergency_stop=coordinator, ui_sessions={WS: CLAIMS})


def _work_params():
    return {"mode": "list", "status": "ready"}


def test_safety_actions_are_registered_under_their_own_surface():
    from orchestrator.projection_surfaces import collect_handlers

    handlers = collect_handlers()
    for action in ("chrome_safety_stop", "chrome_safety_resume", "chrome_safety_verify"):
        assert handlers[action][0] == "safety"


def test_work_mirror_omits_the_card_without_a_coordinator():
    orch = SimpleNamespace()
    html = asyncio.run(work_surface.render(orch, OWNER, [], _work_params()))
    components = asyncio.run(work_surface.components(orch, OWNER, [], _work_params()))
    assert "Emergency stop" not in html
    assert all("Emergency stop" not in str(item.get("title", "")) for item in components)


def test_work_mirror_html_links_to_the_safety_controls():
    orch = _orch(EmergencyStopCoordinator())
    html = asyncio.run(work_surface.render(orch, OWNER, [], _work_params()))
    assert "Emergency stop" in html
    assert "Open emergency stop controls" in html
    assert "chrome_open" in html
    assert "Stop everything now" not in html


def test_work_mirror_native_card_is_status_only_and_validates():
    from rote.work import validate_work_components

    orch = _orch(EmergencyStopCoordinator())
    asyncio.run(orch.emergency_stop.engage(OWNER, sweep=False))
    components = asyncio.run(work_surface.components(orch, OWNER, [], _work_params()))
    card = next(item for item in components if item["type"] == "card"
                and item["title"].startswith("Emergency stop"))
    assert not [child for child in card["content"] if child["type"] == "button"]
    validated = validate_work_components(components, None)
    assert validated, "emergency stop mirror must survive the native vocabulary validator"


def test_work_mirror_shows_unreachable_responder_truth():
    async def probe(owner_id, responder):
        return "unreachable"

    coordinator = EmergencyStopCoordinator(remote_responders=lambda owner: ["machine-9"],
                                           probe_responder=probe)
    orch = _orch(coordinator)
    asyncio.run(coordinator.engage(OWNER, sweep=False))
    asyncio.run(coordinator.verify(OWNER))
    html = asyncio.run(work_surface.render(orch, OWNER, [], _work_params()))
    assert "Unreachable responders" in html
    assert "remote:machine-9: unreachable" in html


def test_safety_surface_shows_actionable_owner_controls_and_current_resume_revision():
    orch = _orch(EmergencyStopCoordinator())
    html = asyncio.run(safety_surface.render(orch, OWNER, [], {}))
    assert "Emergency stop" in html
    assert "Running" in html
    assert "Stop everything now" in html and "chrome_safety_stop" in html
    components = asyncio.run(safety_surface.components(orch, OWNER, [], {}))
    card = next(item for item in components if item["type"] == "card"
                and item["title"].startswith("Emergency stop"))
    buttons = [child for child in card["content"] if child["type"] == "button"]
    assert len(buttons) == 2 and all(button["variant"] == "primary" for button in buttons)
    assert all(button["disabled"] is False and button["local"] is False for button in buttons)
    assert buttons[0]["action"] == "chrome_safety_stop"

    asyncio.run(orch.emergency_stop.engage(OWNER, reason="drill", sweep=False))
    stopped = asyncio.run(safety_surface.render(orch, OWNER, [], {}))
    assert "Stopped" in stopped
    assert "Current revision for resume: 1" in stopped
    assert "drill" in stopped
    assert "chrome_safety_resume" in stopped and "chrome_safety_stop" not in stopped


def test_safety_surface_reports_unavailability_without_a_coordinator():
    orch = SimpleNamespace()
    html = asyncio.run(safety_surface.render(orch, OWNER, [], {}))
    assert "unavailable" in html.lower()
    components = asyncio.run(safety_surface.components(orch, OWNER, [], {}))
    assert components[0]["type"] == "alert"


def test_safety_surface_is_registered_for_chrome_open():
    from orchestrator.projection_surfaces import SURFACE_MODULES, get_surface

    assert SURFACE_MODULES.get("safety") == "orchestrator.projection_surfaces.safety"
    assert get_surface("safety") is safety_surface


def test_nonportable_resume_revision_suppresses_control_and_explains_unavailability():
    value = {"state": "stopped", "engaged": True, "revision": 9007199254740992}
    assert [row["action"] for row in safety_surface._controls(value)] == ["chrome_safety_verify"]
    assert "Resume is unavailable" in " ".join(safety_surface._rows(value))
