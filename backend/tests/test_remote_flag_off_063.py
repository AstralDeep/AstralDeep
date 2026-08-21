"""Feature 063 US7 — FF_REMOTE_COMPUTE off is byte-identical to pre-063 (SC-013).

With the flag off the remote-compute agent must never register in-process, so
no verb can be listed or invoked (both are derived purely from registration),
and the remote-machines surface + its settings-menu item must be absent. Each
scenario also runs flag-ON as a contrast, proving the absence is the flag's
doing rather than a missing directory/module. Hermetic: the flag helper is
monkeypatched on the singleton; no DB, no network, no agent instantiation.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import local_agents
from orchestrator.chrome_availability import projection_chrome_availability
from shared.protocol import AgentCard
from webrender.chrome.menu_model import build_menu_model
from orchestrator.projection_surfaces import remote_machines as surface


def _set_flag(monkeypatch, enabled: bool) -> None:
    """Pin the singleton's remote_compute answer (every 063 entry point resolves
    through ``flags.is_enabled``); all other flags read as off, which none of
    the code under test consults."""
    from shared.feature_flags import flags
    monkeypatch.setattr(
        flags, "is_enabled", lambda name: enabled and name == "remote_compute")


def test_deep_resolves_every_projection_chrome_input(monkeypatch):
    from dreaming import pulse
    from shared.feature_flags import flags

    monkeypatch.setattr(pulse, "pulse_enabled", lambda: True)
    monkeypatch.setattr(
        flags,
        "is_enabled",
        lambda name: name in {"byo_agents", "remote_compute"},
    )
    assert projection_chrome_availability() == {
        "pulse_enabled": True,
        "byo_enabled": True,
        "remote_enabled": True,
    }


def test_deep_chrome_availability_fails_closed(monkeypatch):
    from dreaming import pulse
    from shared.feature_flags import flags

    def _fail(*_args, **_kwargs):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(pulse, "pulse_enabled", _fail)
    monkeypatch.setattr(flags, "is_enabled", _fail)
    assert projection_chrome_availability() == {
        "pulse_enabled": False,
        "byo_enabled": False,
        "remote_enabled": False,
    }


class _FakeRemoteAgent:
    binding = None

    def __init__(
        self,
        *,
        plane_runtime,
        plane_repositories=None,
        plane_blobs=None,
    ):
        type(self).binding = (plane_runtime, plane_repositories, plane_blobs)
        self.card = AgentCard(name="Remote Compute", description="fake",
                              agent_id="remote-compute-1")


@pytest.fixture
def registration(monkeypatch):
    """Isolate register_built_ins to the 063 branch: no bundled dirs discovered,
    and loading a class records the attempt instead of instantiating an agent."""
    attempted: list[str] = []
    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs",
                        lambda *a, **k: [])
    monkeypatch.setattr(local_agents, "_load_agent_class",
                        lambda d: attempted.append(d) or _FakeRemoteAgent)
    from shared import attachment_materializer, attachment_resolver

    bindings = []

    def _bind_resolver(runtime, repositories, blobs):
        bindings.append(("resolver", runtime, repositories, blobs))
        return True

    def _bind_materializer(service):
        bindings.append(("materializer", service))
        return True

    monkeypatch.setattr(
        attachment_resolver,
        "register_plane_runtime",
        _bind_resolver,
    )
    monkeypatch.setattr(
        attachment_materializer,
        "register_materialization_service",
        _bind_materializer,
    )
    registered = []

    async def _register(ws, msg):
        registered.append(msg.agent_card.agent_id)

    plane = SimpleNamespace(
        runtime=object(),
        repositories=object(),
        blobs=object(),
        attachment_materializer=object(),
    )
    orch = SimpleNamespace(
        local_agents={},
        register_agent=_register,
        runtime_composition=SimpleNamespace(plane=plane),
    )
    return orch, attempted, registered, bindings, plane


# ── neither agent registers, so no verb is listed or invocable ────────────────

async def test_flag_off_remote_agent_never_loads_or_registers(monkeypatch, registration):
    _set_flag(monkeypatch, False)
    orch, attempted, registered, bindings, plane = registration
    assert await local_agents.register_built_ins(orch) == []
    assert attempted == []          # the module is never even imported
    assert registered == []         # nothing enters the fleet
    assert orch.local_agents == {}  # so no verb can be listed or dispatched
    assert bindings == [
        ("resolver", plane.runtime, plane.repositories, plane.blobs),
        ("materializer", plane.attachment_materializer),
    ]


async def test_flag_on_contrast_remote_agent_registers(monkeypatch, registration):
    _set_flag(monkeypatch, True)
    orch, attempted, registered, bindings, plane = registration
    assert await local_agents.register_built_ins(orch) == ["remote-compute-1"]
    assert attempted == ["remote_compute"]
    assert registered == ["remote-compute-1"]
    assert "remote-compute-1" in orch.local_agents
    assert _FakeRemoteAgent.binding == (
        plane.runtime,
        plane.repositories,
        plane.blobs,
    )
    assert bindings == [
        ("resolver", plane.runtime, plane.repositories, plane.blobs),
        ("materializer", plane.attachment_materializer),
    ]


async def test_registration_refuses_missing_application_plane(monkeypatch):
    monkeypatch.setattr(
        local_agents,
        "discover_built_in_agent_dirs",
        lambda *args, **kwargs: ["remote_compute"],
    )
    monkeypatch.setattr(
        local_agents,
        "_load_agent_class",
        lambda _name: pytest.fail("loaded an agent without the application Plane"),
    )
    orch = SimpleNamespace(local_agents={}, register_agent=None)
    assert await local_agents.register_built_ins(orch) == []


# ── the settings-menu item is absent ─────────────────────────────────────────

def _menu_item_keys(model):
    return [item.key for group in model.menu for item in group.items]


def test_flag_off_menu_has_no_remote_machines_item(monkeypatch):
    _set_flag(monkeypatch, False)
    assert "remote-machines" not in _menu_item_keys(
        build_menu_model(["user"], **projection_chrome_availability())
    )


def test_flag_on_contrast_menu_carries_remote_machines_item(monkeypatch):
    _set_flag(monkeypatch, True)
    assert "remote-machines" in _menu_item_keys(
        build_menu_model(["user"], **projection_chrome_availability())
    )


# ── the remote-machines surface is absent (disabled notice, no form) ─────────

def _run(coro):
    import asyncio
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _tripwired_orch():
    def _trip(*a, **k):
        raise AssertionError("flag-off surface must not touch machine state")
    return SimpleNamespace(
        history=SimpleNamespace(db=SimpleNamespace(fetch_all=_trip, fetch_one=_trip)))


def test_flag_off_surface_render_is_disabled(monkeypatch):
    _set_flag(monkeypatch, False)
    html = _run(surface.render(_tripwired_orch(), "u1", ["user"], {}))
    assert "Add a machine" not in html
    assert "disabled" in html.lower()


def test_flag_off_surface_components_is_disabled(monkeypatch):
    _set_flag(monkeypatch, False)
    components = _run(surface.components(_tripwired_orch(), "u1", ["user"], {}))
    flat = str(components).lower()
    assert "disabled" in flat
    assert "cred_type" not in flat  # no add-machine form is offered
