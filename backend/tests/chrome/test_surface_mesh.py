"""Tests for orchestrator/projection_surfaces/mesh.py: read-only shared-UI
inventory, invitation/member state derivation, sanitized provenance, manifested
component vocabulary, and the cross-client entry points that open the surface.
"""

import asyncio
import json
import time
from pathlib import Path

import pytest

from orchestrator.projection_surfaces import get_surface
from orchestrator.projection_surfaces import agents as agents_surface
from orchestrator.projection_surfaces import mesh as mesh_surface
from orchestrator.projection_surfaces import personalization as personalization_surface

_ROOT = Path(__file__).resolve().parents[3]
_NOW_MS = int(time.time() * 1000)


def run(coro):
    return asyncio.run(coro)


def _payloads(html: str) -> list[str]:
    import html as html_module

    return [
        html_module.unescape(fragment.split("'")[0])
        for fragment in html.split("data-ui-payload='")[1:]
    ]


def _fingerprint(seed: str) -> str:
    return "sha256-" + seed * 32


def _member(**overrides):
    record = {
        "member_id": "a1b2c3d4" * 4,
        "owner_id": "owner-1",
        "mesh_id": "mesh-owner-1",
        "label": "Kitchen tablet",
        "device_key_fingerprint": _fingerprint("ab"),
        "device_key": {"kty": "OKP", "crv": "Ed25519", "x": "public-material"},
        "membership_epoch": 2,
        "status": "active",
        "scopes": ["tools:read", "mesh:confirm"],
        "enrolled_at": _NOW_MS - 86_400_000,
        "confirmed_by": {"kind": "owner", "id": "owner-1"},
        "revoked_at": None,
    }
    record.update(overrides)
    return record


def _invitation(**overrides):
    record = {
        "invite_id": "e4f5a6b7" * 4,
        "mesh_id": "mesh-owner-1",
        "label": "Desk speaker",
        "device_key_fingerprint": _fingerprint("cd"),
        "requested_scopes": ["tools:read", "mesh:confirm"],
        "confirmed_scopes": None,
        "status": "pending",
        "created_by": {"kind": "member", "id": "a1b2c3d4" * 4},
        "confirmed_by": None,
        "created_at": _NOW_MS - 3_600_000,
        "expires_at": _NOW_MS + 3_600_000,
        "decided_at": None,
        "redeemed_at": None,
        "member_id": None,
    }
    record.update(overrides)
    return record


class FakeStore:
    def __init__(self, members=(), invitations=(), error=None):
        self._members, self._invitations, self._error = members, invitations, error

    def list_members(self, owner_id):
        if self._error:
            raise RuntimeError("store unavailable")
        return list(self._members)

    def list_invitations(self, owner_id):
        if self._error:
            raise RuntimeError("store unavailable")
        return list(self._invitations)


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(mesh_surface, "_store", lambda orch: fake)
    return fake


def _orch():
    from types import SimpleNamespace

    return SimpleNamespace(plane_repository_source=object())


def test_registry_resolves_surface():
    mod = get_surface("mesh")
    assert mod is mesh_surface
    assert mod.TITLE == "Mesh devices"


def test_surface_dispatches_no_new_actions(store):
    assert not hasattr(mesh_surface, "HANDLERS")
    html = run(mesh_surface.render(_orch(), "owner-1", ["user"], {}))
    actions = {part.split('"')[0] for part in html.split('data-ui-action="')[1:]}
    assert actions == {"chrome_open"}

def test_render_member_inventory_states_and_sanitized_provenance(store):
    store._members = [
        _member(),
        _member(
            member_id="b2c3d4e5" * 4,
            label="Old phone",
            status="revoked",
            revoked_at=_NOW_MS - 60_000,
            confirmed_by={"kind": "member", "id": "a1b2c3d4" * 4},
        ),
    ]
    html = run(mesh_surface.render(_orch(), "owner-1", ["user"], {}))
    assert "Kitchen tablet" in html and "Old phone" in html
    assert ">Active<" in html and ">Revoked<" in html
    assert "ababababababab…" not in html
    assert "abababababab" + "…" in html
    assert "public-material" not in html and '"kty"' not in html
    assert "sha256-" not in html
    assert "custody" not in html and "device_key" not in html
    confirmed = html.split("Old phone")[0]
    assert "owner" in confirmed
    revoked_section = html.split("Old phone", 1)[1]
    assert "Revoked" in revoked_section
    assert "member a1b2c3d4…" in revoked_section


def test_render_invitation_states(store):
    store._invitations = [
        _invitation(),
        _invitation(
            invite_id="1" * 32,
            label="Expired request",
            status="pending",
            expires_at=_NOW_MS - 1_000,
        ),
        _invitation(
            invite_id="2" * 32,
            label="Waiting for device",
            status="confirmed",
            confirmed_scopes=["tools:read"],
            confirmed_by={"kind": "owner", "id": "owner-1"},
        ),
        _invitation(
            invite_id="3" * 32,
            label="Enrolled",
            status="confirmed",
            confirmed_by={"kind": "owner", "id": "owner-1"},
            redeemed_at=_NOW_MS - 500,
            member_id="a1b2c3d4" * 4,
        ),
        _invitation(
            invite_id="4" * 32,
            label="Declined",
            status="rejected",
            expires_at=_NOW_MS - 1_000,
        ),
    ]
    html = run(mesh_surface.render(_orch(), "owner-1", ["user"], {"view": "invitations"}))
    for label in (
        "Pending confirmation",
        "Expired",
        "Confirmed — awaiting device",
        "Enrolled",
        "Rejected",
    ):
        assert label in html, label
    assert "member a1b2c3d4…" in html
    assert "mesh:confirm" in html
    assert "Confirm or reject this request through the mesh API" in html


def test_render_inventory_failure_is_visible(store):
    store._error = RuntimeError("down")
    html = run(mesh_surface.render(_orch(), "owner-1", ["user"], {}))
    assert "mesh inventory is unavailable" in html
    components = run(mesh_surface.components(_orch(), "owner-1", ["user"], {}))
    kinds = [c.get("variant") for c in components if c.get("type") == "alert"]
    assert "error" in kinds


def test_inventory_helper_edges(monkeypatch):
    assert mesh_surface._date("not-a-number") == "unknown"
    assert mesh_surface._date(None) == "unknown"
    assert mesh_surface._scopes_text(None) == "none"
    assert mesh_surface._scopes_text("tools:read") == "none"
    assert mesh_surface._creator_text({"created_by": None}) == "—"
    assert mesh_surface._creator_text({"created_by": "junk"}) == "—"
    assert mesh_surface._creator_text({"created_by": {"kind": "member"}}) == "member"
    assert mesh_surface._creator_text({"created_by": {"kind": "owner", "id": "o"}}) == "owner"
    assert mesh_surface._confirmer_text({}) == "—"
    assert mesh_surface._invitation_state(_invitation(status="weird")) == (
        "Unknown",
        "default",
    )
    decided = _invitation(
        status="rejected",
        decided_at=_NOW_MS - 500,
        confirmed_by={"kind": "member", "id": "a1b2c3d4" * 4},
    )
    facts = dict(mesh_surface._invitation_facts(decided))
    assert facts["Decided"]
    assert facts["Confirmed by"] == "member a1b2c3d4…"


def test_unavailable_inventory_renders_error_notices(monkeypatch):
    async def unavailable(orch, user_id):
        return None

    monkeypatch.setattr(mesh_surface, "_load_inventory", unavailable)
    html = run(mesh_surface.render(_orch(), "owner-1", ["user"], {"view": "members"}))
    assert "mesh inventory is unavailable" in html
    components = run(mesh_surface.components(_orch(), "owner-1", ["user"], {}))
    assert any(
        c.get("type") == "alert" and c.get("variant") == "error" for c in components
    )


def test_components_use_only_manifested_vocabulary(store):
    store._members = [_member()]
    store._invitations = [_invitation()]
    manifest = json.loads(
        (_ROOT / "components/AstralProjection/contracts/ui_protocol.json").read_text()
    )
    declared = set(manifest["component_types"])
    for params in ({}, {"view": "invitations"}):
        components = run(mesh_surface.components(_orch(), "owner-1", ["user"], params))
        assert components
        assert {c["type"] for c in components} <= declared
        for component in components:
            for child in component.get("content") or []:
                if isinstance(child, dict):
                    assert child["type"] in declared


def test_view_tabs_navigate_within_surface(store):
    html = run(mesh_surface.render(_orch(), "owner-1", ["user"], {}))
    assert '{"surface": "mesh", "params": {"view": "members"}}' in _payloads(html)
    assert '{"surface": "mesh", "params": {"view": "invitations"}}' in _payloads(html)
    components = run(mesh_surface.components(_orch(), "owner-1", ["user"], {}))
    bar = components[0]
    assert bar["type"] == "container"
    payloads = [child["payload"] for child in bar["children"]]
    assert {"surface": "mesh", "params": {"view": "members"}} in payloads
    assert {"surface": "mesh", "params": {"view": "invitations"}} in payloads


def test_agents_surface_opens_mesh_devices():
    html = agents_surface._render_tabs("mine")
    assert "Mesh devices" in html
    assert '{"surface": "mesh", "params": {}}' in _payloads(html)


def test_personalization_surfaces_open_mesh_devices(monkeypatch):
    def empty_soul(orch, user_id, params):
        return []

    monkeypatch.setattr(personalization_surface, "_components_soul", empty_soul)
    html = personalization_surface._tab_bar("soul")
    assert "Mesh devices" in html
    assert '{"surface": "mesh", "params": {}}' in _payloads(html)
    components = run(
        personalization_surface.components(_orch(), "owner-1", ["user"], {})
    )
    bar = next(c for c in components if c.get("type") == "container")
    payloads = [child["payload"] for child in bar["children"]]
    assert {"surface": "mesh", "params": {}} in payloads
