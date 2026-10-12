"""Exercises shared mesh controls through the ordinary authenticated chrome dispatcher.
Real PostgreSQL, current signed sessions, credential encryption and audit fences are
used with the deterministic Keycloak boundary supplied by MeshRuntime.
"""

import asyncio
import base64
from dataclasses import dataclass
from io import BytesIO
import json
import re
import time
from types import SimpleNamespace
from uuid import uuid4

from PIL import Image
import pytest
from jose import jwt

from orchestrator import chrome_events, human_request_authority as human, web_auth
from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
from orchestrator.orchestrator import ConnectionContext
from orchestrator.projection_surfaces import mesh as surface
from orchestrator import mesh_enrollment as me
from persistent_agents.models import AssignmentError
from tests.test_mesh_admission import mesh as mesh, plane_runtime as plane_runtime


@dataclass(eq=False)
class Socket:
    scope: dict
    closed: bool = False


@pytest.fixture
def registered(mesh):
    host = mesh.host
    host.runtime_composition = SimpleNamespace(
        plane=SimpleNamespace(
            runtime=mesh.runtime, repositories=mesh.runtime.repositories
        )
    )
    boundary = human.HumanRequestBoundary(host)
    host.human_request_boundary = boundary
    host.rote = SimpleNamespace(
        get_profile=lambda _: SimpleNamespace(
            device_type=SimpleNamespace(value="browser")
        )
    )
    host.llm_configured_for = lambda _: asyncio.sleep(0, result=False)
    frames = []

    async def send(socket, frame):
        frames.append(json.loads(frame))
        return True

    host._safe_send = send
    socket = Socket(
        {
            "type": "websocket",
            "query_string": b"",
            "headers": [
                (
                    b"cookie",
                    (web_auth.COOKIE_NAME + "=" + web_auth._sign(mesh.sid)).encode(),
                ),
                (b"origin", b"https://mesh.invalid"),
            ],
        }
    )
    context = ConnectionContext(
        socket,
        uuid4(),
        time.monotonic() + 30,
        registered=True,
        connection_generation=uuid4(),
    )
    host._connection_contexts = {id(socket): context}
    host.ui_sessions = {
        socket: {
            **jwt.get_unverified_claims(mesh.owner_token),
            "_raw_token": mesh.owner_token,
        }
    }
    host._open_chrome_surface = {id(socket): "mesh"}

    async def dispatch(action, payload, *, before=None):
        frame = {
            "type": "ui_event",
            "action": action,
            "payload": payload,
            "submission_id": str(uuid4()),
            "request_generation": str(uuid4()),
            "connection_generation": str(context.connection_generation),
        }
        pending = human.capture_human_socket_request(
            boundary, websocket=socket, context=context, message=frame
        )
        token = _CONNECTION_OPERATION_CONTEXT.set({"human_request": pending})
        try:
            if before is not None:
                await pending.authenticate()
                before()
            frames.clear()
            await chrome_events.handle_chrome_event(
                host,
                socket,
                action,
                payload,
                mesh.owner,
                request_generation=frame["request_generation"],
            )
            return frames
        finally:
            _CONNECTION_OPERATION_CONTEXT.reset(token)
            pending.close()

    try:
        yield mesh, dispatch, socket, frames
    finally:
        boundary.close()


def creation(device, **overrides):
    values = {
        "label": "Synthetic joining device",
        "device_key": json.dumps(device.public_key),
        "scopes": "tools:read",
        "ttl_seconds": "300",
    }
    values.update(overrides)
    return {"fields": values}


@pytest.mark.asyncio
async def test_dispatcher_create_confirm_redeem_rename_revoke_and_remove(registered):
    mesh, dispatch, _, _ = registered
    device = mesh.sdk.MeshDevice.generate()
    frames = await dispatch("chrome_mesh_create", creation(device))
    html = frames[-1]["html"]
    assert "One-time enrollment" in html
    assert "data:image/png;base64," in html
    payload = re.search(r"<code[^>]*>([A-Za-z0-9_-]+)</code>", html).group(1)
    png = re.search(r'data:image/png;base64,([^"\s]+)', html).group(1)
    assert Image.open(BytesIO(base64.b64decode(png))).format == "PNG"
    invitation = mesh.store.list_invitations(mesh.owner)[0]
    invite_id = invitation["invite_id"]
    assert invitation["status"] == "pending"
    assert payload not in json.dumps(invitation)
    assert not mesh.store.list_members(mesh.owner)
    frames = await dispatch("chrome_mesh_confirm", {"invite_id": invite_id})
    assert mesh.store.get_invitation(mesh.owner, invite_id)["status"] == "confirmed"
    client = mesh.sdk.MeshClient("https://mesh.invalid", device, client=mesh.client)
    member = client.redeem(payload, agent_id="reader")
    frames = await dispatch(
        "chrome_mesh_rename",
        {
            "member_id": member["member_id"],
            "fields": {"label": "Renamed synthetic device"},
        },
    )
    assert (
        mesh.store.get_member(mesh.owner, member["member_id"])["label"]
        == "Renamed synthetic device"
    )
    await dispatch("chrome_mesh_revoke", {"member_id": member["member_id"]})
    assert mesh.store.get_member(mesh.owner, member["member_id"])["status"] == "revoked"
    await dispatch("chrome_mesh_remove_invitation", {"invite_id": invite_id})
    assert mesh.store.list_invitations(mesh.owner) == []
    refused = mesh.client.get(
        "/api/mesh/me", headers={"Authorization": "DPoP " + client._token}
    )
    assert refused.status_code == 403


@pytest.mark.asyncio
async def test_dispatcher_reject_never_creates_device_authority(registered):
    mesh, dispatch, _, _ = registered
    device, invitation = mesh.invitation()
    invite_id = invitation["invitation"]["invite_id"]
    frames = await dispatch("chrome_mesh_reject", {"invite_id": invite_id})
    assert "Mesh inventory updated" in frames[-1]["html"]
    assert mesh.store.get_invitation(mesh.owner, invite_id)["status"] == "rejected"
    response = mesh.client.post(
        "/api/mesh/enrollment/redeem", json=device.redemption(invitation["payload"])
    )
    assert response.status_code == 409
    assert mesh.store.list_members(mesh.owner) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["owner", "closed", "session", "navigation"])
async def test_stale_owner_session_or_surface_cannot_mutate(registered, loss):
    mesh, dispatch, socket, _ = registered
    device = mesh.sdk.MeshDevice.generate()

    def expire():
        if loss == "owner":
            mesh.host.ui_sessions[socket]["sub"] = "foreign-owner"
        elif loss == "closed":
            socket.closed = True
        elif loss == "session":
            mesh.sessions.delete(mesh.sid)
        else:
            mesh.host._open_chrome_surface[id(socket)] = "agents"

    with pytest.raises(AssignmentError):
        await dispatch("chrome_mesh_create", creation(device), before=expire)
    assert mesh.store.list_invitations(mesh.owner) == []


@pytest.mark.asyncio
async def test_mutation_rolls_back_when_final_caller_is_lost(registered, monkeypatch):
    mesh, dispatch, socket, _ = registered
    original = me.MeshEnrollmentStore.create_invitation

    def lose(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        socket.closed = True
        return value

    monkeypatch.setattr(me.MeshEnrollmentStore, "create_invitation", lose)
    with pytest.raises(AssignmentError):
        await dispatch("chrome_mesh_create", creation(mesh.sdk.MeshDevice.generate()))
    assert mesh.store.list_invitations(mesh.owner) == []


@pytest.mark.asyncio
async def test_unknown_foreign_target_is_safe_refusal_with_audit(registered):
    mesh, dispatch, _, _ = registered
    frames = await dispatch("chrome_mesh_revoke", {"member_id": "foreignmember"})
    assert "mesh change was refused" in frames[-1]["html"]
    assert "foreignmember" not in frames[-1]["html"]
    events, _ = mesh.audit.list_for_user(mesh.owner)
    assert any(
        event.action_type == "mesh.surface.denied" and event.outcome == "failure"
        for event in events
    )


@pytest.mark.asyncio
async def test_missing_audit_rolls_back_without_success(registered, monkeypatch):
    mesh, dispatch, _, _ = registered
    monkeypatch.setattr(
        surface,
        "_store",
        lambda _: me.MeshEnrollmentStore(mesh.host.plane_repository_source),
    )
    with pytest.raises(AssignmentError, match="human_request_unavailable") as refusal:
        await dispatch("chrome_mesh_create", creation(mesh.sdk.MeshDevice.generate()))
    assert refusal.value.status_code == 503
    assert mesh.store.list_invitations(mesh.owner) == []


@pytest.mark.parametrize(
    "action,payload",
    [
        ("create", {"fields": {"device_key": "private secret"}}),
        ("create", {"fields": {"device_key": "{}"}}),
        ("create", {"fields": {"device_key": "[]"}}),
        (
            "create",
            {
                "fields": {
                    "device_key": '{"kty":"OKP","crv":"Ed25519","x":"public","d":"private"}'
                }
            },
        ),
        ("create", {"fields": {"device_key": ""}, "extra": "ignored"}),
        ("rename", {"member_id": "bad/id", "fields": {"label": "name"}}),
        ("rename", {"member_id": "ok", "fields": {"label": 123}}),
        ("rename", {"member_id": "ok", "fields": {"unexpected": "name"}}),
        ("revoke", {"member_id": "x" * 129}),
        ("confirm", {"invite_id": None}),
        ("reject", {"invite_id": "ok", "fields": []}),
        ("create", {"fields": {"device_key": "x" * 2049}}),
        ("create", []),
        ("create", {"surface": "foreign"}),
        ("create", {"fields": {"device_key": "[" * 1000 + "]" * 1000}}),
    ],
)
def test_invalid_action_payload_refuses_without_sensitive_detail(action, payload):
    with pytest.raises(AssignmentError, match="mesh_control_invalid"):
        surface._action_input(payload, action)


@pytest.mark.asyncio
async def test_invalid_label_is_safe_audited_refusal(registered):
    mesh, dispatch, _, _ = registered
    frames = await dispatch(
        "chrome_mesh_create", creation(mesh.sdk.MeshDevice.generate(), label="")
    )
    assert "mesh change was refused" in frames[-1]["html"]
    assert mesh.store.list_invitations(mesh.owner) == []


def test_one_time_presentation_requires_same_owner_and_session(monkeypatch):
    host = object()
    created = surface._CreatedEnrollment(
        "owner", "incarnation", "opaque-one-time", "data:image/png;base64,AAA="
    )
    monkeypatch.setattr(human, "current_human_caller", lambda **_: None)
    assert surface._created_enrollment(host, "owner", {"created": created}) is None
    assert surface._created_enrollment(host, "foreign", {"created": created}) is None
    consent = SimpleNamespace(credential=SimpleNamespace(incarnation_id="other"))
    caller = SimpleNamespace(owner_id="owner", require_session=lambda: consent)
    monkeypatch.setattr(human, "current_human_caller", lambda **_: caller)
    assert surface._created_enrollment(host, "owner", {"created": created}) is None
    consent.credential.incarnation_id = "incarnation"
    assert surface._created_enrollment(host, "owner", {"created": created}) is created
    caller.owner_id = "foreign"
    assert surface._created_enrollment(host, "owner", {"created": created}) is None
    assert "opaque-one-time" not in repr(created)


@pytest.mark.asyncio
async def test_native_created_components_preserve_one_time_presentation(
    mesh, monkeypatch
):
    created = surface._CreatedEnrollment(
        mesh.owner,
        "incarnation",
        "opaque-one-time",
        surface._qr_image("opaque-one-time"),
    )
    caller = SimpleNamespace(
        owner_id=mesh.owner,
        require_session=lambda: SimpleNamespace(
            credential=SimpleNamespace(incarnation_id="incarnation")
        ),
    )
    monkeypatch.setattr(human, "current_human_caller", lambda **_: caller)
    values = await surface.components(
        mesh.host, mesh.owner, ["user"], {"created": created}
    )
    serialized = json.dumps(values)
    assert "One-time enrollment" in serialized
    assert "opaque-one-time" in serialized
    assert "data:image/png;base64," in serialized
    assert "chrome_mesh_create" in serialized


@pytest.mark.asyncio
async def test_control_refuses_divergent_repository_binding(registered, monkeypatch):
    mesh, dispatch, _, _ = registered
    store = surface._store(mesh.host)
    store._membership = object()
    monkeypatch.setattr(surface, "_store", lambda _: store)
    with pytest.raises(AssignmentError) as error:
        await dispatch("chrome_mesh_create", creation(mesh.sdk.MeshDevice.generate()))
    assert error.value.code == "human_request_unavailable"
    assert not mesh.store.list_invitations(mesh.owner)
