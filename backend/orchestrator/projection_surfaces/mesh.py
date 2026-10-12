"""Renders the shared mesh-enrollment surface: owner member inventory, invitation
states, and sanitized provenance consumed from the completed workflows in
orchestrator/mesh_api.py and orchestrator/mesh_enrollment.py.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO

from webrender.chrome import esc, notice_block

from orchestrator import mesh_enrollment as me
from orchestrator.plane_repository_context import plane_source_from_orchestrator

TITLE = "Mesh devices"
SURFACE_KEY = "mesh"

_VIEWS = ("members", "invitations")

_BTN_PRIMARY = (
    "px-3 py-1.5 rounded-lg text-xs font-medium bg-astral-primary/20 "
    "text-astral-primary border border-astral-primary/30 hover:bg-astral-primary/30"
)
_BTN_GHOST = (
    "px-3 py-1.5 rounded-lg text-xs font-medium text-astral-muted "
    "hover:text-astral-text hover:bg-white/5"
)
_CARD_CLS = "bg-white/5 border border-white/10 rounded-lg p-4"
_MUTED = "text-astral-muted"

_MEMBER_BADGES = {
    "active": ("Active", "success"),
    "revoked": ("Revoked", "error"),
}

_INTRO_TEXT = (
    "Your personal mesh gives each of your devices and agents its own identity "
    "with attenuated tool authority. A link or QR code alone never grants "
    "authority: a request must be confirmed while your "
    "session is live, and a revoked member cannot act. Enrollment payloads are "
    "one-time secrets; never share them beyond the joining device."
)

_ENROLL_STEPS = (
    "On the joining device, leave public identity blank and create a request. Its private key stays on that device.",
    "To create the request elsewhere, share the joining device's public identity and paste it on your owner device.",
    "From another device signed in to your account, check the fingerprint and confirm the pending request before it expires.",
    "Copy the one-time secure link back to the original joining device and complete enrollment. You can then rename or revoke it.",
)


def _store(orch) -> me.MeshEnrollmentStore:
    return me.MeshEnrollmentStore(
        plane_source_from_orchestrator(orch),
        audit_repository=getattr(orch, "audit_repo", None),
    )


@dataclass(frozen=True, slots=True, repr=False)
class _CreatedEnrollment:
    owner_id: str
    incarnation_id: str
    payload: str
    qr_image: str
    link: str | None = None


def _qr_image(payload):
    import qrcode

    image = qrcode.make(payload, box_size=4, border=4)
    output = BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode(
        "ascii"
    )


def _created_enrollment(orch, user_id, params):
    from orchestrator.human_request_authority import current_human_caller

    created = params.get("created")
    if not isinstance(created, _CreatedEnrollment) or created.owner_id != user_id:
        return None
    caller = current_human_caller(expected_orchestrator=orch)
    if caller is None or caller.owner_id != user_id:
        return None
    consent = caller.require_session()
    return (
        created if consent.credential.incarnation_id == created.incarnation_id else None
    )


def _button_html(label, action, payload, *, collect=False):
    collection = ' data-ui-collect="true"' if collect else ""
    return (
        f'<button type="button" class="{_BTN_PRIMARY}" '
        f"data-ui-action=\"{esc(action)}\" data-ui-payload='{esc(json.dumps(payload))}'"
        f"{collection}>{esc(label)}</button>"
    )


def _field_html(name, label, value=""):
    return (
        f'<label class="flex flex-col gap-1 text-sm"><span>{esc(label)}</span>'
        f'<input name="{esc(name)}" value="{esc(value)}" autocomplete="off" '
        'class="rounded-lg bg-white/10 border border-white/10 px-3 py-2 text-astral-text"></label>'
    )


def _enrollment_fields():
    from webrender.chrome.surfaces import _sdui

    return [
        _sdui.field("label", "Device name"),
        _sdui.field(
            "device_key",
            "Other device public identity (optional JSON)",
            "textarea",
            help="Leave blank to enroll this device. To enroll another device, paste its shared public identity.",
        ),
        _sdui.field("scopes", "Allowed access (comma separated)", default="tools:read"),
        _sdui.field("ttl_seconds", "Expires after (seconds)", default="3600"),
    ]


def _enrollment_form_html():
    fields = "".join(
        _field_html(field["name"], field["label"], str(field.get("default", "")))
        for field in _enrollment_fields()
    )
    return (
        f'<div data-ui-form class="{_CARD_CLS} space-y-3">'
        '<h3 class="text-sm font-semibold">Enroll a device</h3>'
        '<p class="text-xs text-astral-muted">Leave public identity blank to enroll this '
        "device. To enroll another device, paste only its shared public identity. "
        "Confirm the request from another owner device before completing enrollment.</p>"
        f"{fields}{_button_html('Create secure enrollment', 'chrome_mesh_create', {}, collect=True)}</div>"
        f"{_button_html('Share this device public identity', 'chrome_mesh_identity', {})}"
        f'<div data-ui-form class="{_CARD_CLS} space-y-3">'
        f"{_field_html('link', 'Secure enrollment link')}"
        f"{_button_html('Complete enrollment on this device', 'chrome_mesh_redeem', {}, collect=True)}</div>"
    )


def _created_components(created):
    from astralprims import CodeBlock, Image
    from webrender.chrome.surfaces import _sdui

    return [
        _sdui.card(
            "One-time enrollment",
            [
                _sdui.text(
                    "Scan this QR or copy the secure link to the original joining device. "
                    "The request still requires confirmation before the device can redeem it. "
                    "This secret is shown once and must not be shared with other devices.",
                    "caption",
                ),
                Image(
                    url=created.qr_image, alt="One-time mesh enrollment QR"
                ).to_dict(),
                CodeBlock(
                    code=created.link or created.payload, language="text"
                ).to_dict(),
            ],
        )
    ]


def _short_fingerprint(value) -> str:
    raw = str(value or "")
    if raw.startswith("sha256-"):
        raw = raw[len("sha256-") :]
    return raw[:12] + "…" if len(raw) > 12 else raw


def _short_id(value) -> str:
    raw = str(value or "")
    return raw[:8] + "…" if len(raw) > 8 else raw


def _date(ms) -> str:
    try:
        return datetime.fromtimestamp(ms / 1000, UTC).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown"


def _scopes_text(scopes) -> str:
    if not isinstance(scopes, list) or not scopes:
        return "none"
    return ", ".join(str(scope) for scope in scopes)


def _confirmer_text(record) -> str:
    confirmer = record.get("confirmed_by")
    if not isinstance(confirmer, dict):
        return "—"
    kind = str(confirmer.get("kind") or "unknown")
    identifier = confirmer.get("id")
    if kind == "owner":
        return "owner"
    return f"member {_short_id(identifier)}" if identifier else "member"


def _creator_text(record) -> str:
    creator = record.get("created_by")
    if not isinstance(creator, dict):
        return "—"
    kind = str(creator.get("kind") or "unknown")
    identifier = creator.get("id")
    if kind == "owner":
        return "owner"
    return f"member {_short_id(identifier)}" if identifier else "member"


def _invitation_state(record) -> tuple[str, str]:
    status = str(record.get("status") or "")
    expired = _is_expired(record)
    if status == "redeemed":
        return ("Enrolled", "success")
    if status == "pending":
        return ("Expired", "error") if expired else ("Pending confirmation", "warning")
    if status == "confirmed":
        if record.get("redeemed_at") is not None:
            return ("Enrolled", "success")
        return (
            ("Expired", "error") if expired else ("Confirmed — awaiting device", "info")
        )
    if status == "rejected":
        return ("Rejected", "default")
    return ("Unknown", "default")


def _is_expired(record) -> bool:
    expires = record.get("expires_at")
    return isinstance(expires, int) and me._now_ms() >= expires


def _member_badge(record) -> tuple[str, str]:
    return _MEMBER_BADGES.get(str(record.get("status")), ("Unknown", "default"))


def _member_facts(member: dict) -> list[tuple[str, str]]:
    facts = [("Status", _member_badge(member)[0])]
    if member.get("status") == "revoked":
        facts.append(("Revoked", _date(member.get("revoked_at"))))
    facts.extend(
        [
            ("Device key", _short_fingerprint(member.get("device_key_fingerprint"))),
            ("Tool access", _scopes_text(member.get("scopes"))),
            ("Enrolled", _date(member.get("enrolled_at"))),
            ("Confirmed by", _confirmer_text(member)),
        ]
    )
    return facts


def _invitation_facts(record: dict) -> list[tuple[str, str]]:
    label, _ = _invitation_state(record)
    facts = [("State", label)]
    facts.extend(
        [
            ("Device key", _short_fingerprint(record.get("device_key_fingerprint"))),
            ("Requested access", _scopes_text(record.get("requested_scopes"))),
        ]
    )
    confirmed = record.get("confirmed_scopes")
    if isinstance(confirmed, list):
        facts.append(("Confirmed access", _scopes_text(confirmed)))
    facts.extend(
        [
            ("Created by", _creator_text(record)),
            ("Confirmed by", _confirmer_text(record)),
            ("Created", _date(record.get("created_at"))),
            ("Expires", _date(record.get("expires_at"))),
        ]
    )
    if record.get("decided_at") is not None:
        facts.append(("Decided", _date(record.get("decided_at"))))
    if record.get("redeemed_at") is not None:
        facts.append(("Redeemed", _date(record.get("redeemed_at"))))
    return facts


def _open_payload(surface: str, params: dict) -> str:
    return esc(json.dumps({"surface": surface, "params": params}))


def _view_tabs(view: str) -> str:
    parts = ['<div class="flex items-center gap-1.5" role="tablist">']
    for key, label in (("members", "Members"), ("invitations", "Enrollment requests")):
        active = key == view
        cls = _BTN_PRIMARY if active else _BTN_GHOST
        selected = "true" if active else "false"
        payload = _open_payload(SURFACE_KEY, {"view": key})
        parts.append(
            f'<button type="button" role="tab" aria-selected="{selected}" class="{cls}" '
            f'data-ui-action="chrome_open" '
            f"data-ui-payload='{payload}'>{esc(label)}</button>"
        )
    parts.append("</div>")
    return "".join(parts)


def _facts_html(facts: list[tuple[str, str]]) -> str:
    rows = "".join(
        f'<div class="flex justify-between gap-3 text-xs">'
        f'<span class="{_MUTED}">{esc(name)}</span>'
        f'<span class="text-astral-text text-right">{esc(value)}</span></div>'
        for name, value in facts
    )
    return f'<div class="space-y-1">{rows}</div>'


def _guidance_html() -> str:
    steps = "".join(
        f'<li class="text-sm {_MUTED}">{esc(step)}</li>' for step in _ENROLL_STEPS
    )
    return (
        f'<div class="{_CARD_CLS}"><h3 class="text-sm font-semibold text-astral-text">'
        'Enroll a device</h3><ol class="list-decimal ml-4 space-y-1 pt-2">'
        f"{steps}</ol></div>"
    )


async def _load_inventory(orch, user_id) -> tuple[list[dict], list[dict]] | None:
    store = _store(orch)
    try:
        members, invitations = await asyncio.gather(
            asyncio.to_thread(store.list_members, user_id),
            asyncio.to_thread(store.list_invitations, user_id),
        )
    except Exception:
        return None
    return members, invitations


async def render(orch, user_id, roles, params) -> str:
    params = params if isinstance(params, dict) else {}
    view = params.get("view") if params.get("view") in _VIEWS else "members"
    loaded = await _load_inventory(orch, user_id)
    if loaded is None:
        body = notice_block("error", "The mesh inventory is unavailable. Please retry.")
        return f'<div class="space-y-4">{_view_tabs(view)}{body}</div>'
    members, invitations = loaded
    if view == "invitations":
        cards = _invitation_cards_html(invitations)
        empty = (
            "No enrollment requests yet. A device asks to join by creating an "
            "invitation through the mesh API with its own new key."
        )
    else:
        cards = _member_cards_html(members)
        empty = "No devices are enrolled yet. Your mesh inventory starts empty."
    listing = (
        "".join(cards)
        if cards
        else f'<div class="text-sm {_MUTED} py-6 text-center">{esc(empty)}</div>'
    )
    body = (
        f'<p class="text-sm {_MUTED}">{esc(_INTRO_TEXT)}</p>'
        f'<div class="space-y-2">{listing}</div>'
        f"{_enrollment_form_html()}"
        f"{_guidance_html()}"
    )
    created = _created_enrollment(orch, user_id, params)
    if created is not None:
        from webrender import render_component_fragment

        body = (
            "".join(
                render_component_fragment(component)
                for component in _created_components(created)
            )
            + body
        )
    return f'<div class="space-y-4">{_view_tabs(view)}{body}</div>'


def _member_cards_html(members: list[dict]) -> list[str]:
    cards = []
    for member in members:
        label, variant = _member_badge(member)
        controls = ""
        if member.get("status") == "active":
            identity = {"member_id": member["member_id"]}
            controls = (
                '<div data-ui-form class="space-y-2 pt-3">'
                f"{_field_html('label', 'Device name', member.get('label') or '')}"
                f"{_button_html('Rename', 'chrome_mesh_rename', identity, collect=True)} "
                f"{_button_html('Revoke device', 'chrome_mesh_revoke', identity)}</div>"
            )
        cards.append(
            f'<div class="{_CARD_CLS}">'
            f'<div class="flex items-center justify-between gap-3">'
            f'<span class="text-sm font-medium text-astral-text">{esc(member.get("label") or "")}</span>'
            f'<span class="astral-badge astral-badge--{variant} inline-flex items-center '
            f'px-2 py-0.5 rounded-full border text-xs font-medium">{esc(label)}</span>'
            f"</div>"
            f'<div class="pt-2">{_facts_html(_member_facts(member))}</div>'
            f"{controls}"
            f"</div>"
        )
    return cards


def _invitation_cards_html(invitations: list[dict]) -> list[str]:
    cards = []
    for record in invitations:
        label, variant = _invitation_state(record)
        pending_note = (
            '<div class="flex gap-2 pt-3">'
            f"{_button_html('Confirm enrollment', 'chrome_mesh_confirm', {'invite_id': record['invite_id']})}"
            f"{_button_html('Reject', 'chrome_mesh_reject', {'invite_id': record['invite_id']})}</div>"
            if label == "Pending confirmation"
            else _button_html(
                "Remove request",
                "chrome_mesh_remove_invitation",
                {"invite_id": record["invite_id"]},
            )
            if record.get("status") != "pending"
            else ""
        )
        cards.append(
            f'<div class="{_CARD_CLS}">'
            f'<div class="flex items-center justify-between gap-3">'
            f'<span class="text-sm font-medium text-astral-text">{esc(record.get("label") or "")}</span>'
            f'<span class="astral-badge astral-badge--{variant} inline-flex items-center '
            f'px-2 py-0.5 rounded-full border text-xs font-medium">{esc(label)}</span>'
            f"</div>"
            f'<div class="pt-2">{_facts_html(_invitation_facts(record))}</div>'
            f"{pending_note}"
            f"</div>"
        )
    return cards


async def components(orch, user_id, roles, params):
    from webrender.chrome.surfaces import _sdui

    params = params if isinstance(params, dict) else {}
    view = params.get("view") if params.get("view") in _VIEWS else "members"
    tab_bar = _sdui.container(
        [
            _sdui.button(
                label,
                "chrome_open",
                {"surface": SURFACE_KEY, "params": {"view": key}},
                variant="primary" if key == view else "secondary",
            )
            for key, label in (
                ("members", "Members"),
                ("invitations", "Enrollment requests"),
            )
        ],
        direction="row",
    )
    loaded = await _load_inventory(orch, user_id)
    if loaded is None:
        return [
            tab_bar,
            _sdui.alert("The mesh inventory is unavailable. Please retry.", "error"),
        ]
    members, invitations = loaded
    out = [tab_bar, _sdui.text(_INTRO_TEXT, "caption")]
    created = _created_enrollment(orch, user_id, params)
    if created is not None:
        out.extend(_created_components(created))
    if view == "invitations":
        records = invitations
        empty = "No enrollment requests yet."
        builders = _invitation_components
    else:
        records = members
        empty = "No devices are enrolled yet. Your mesh inventory starts empty."
        builders = _member_components
    if records:
        for record in records:
            out.extend(builders(record))
    else:
        out.append(_sdui.text(empty, "caption"))
    out.append(
        _sdui.card("Enroll a device", [_sdui.bullet_list(list(_ENROLL_STEPS), True)])
    )
    out.append(
        _sdui.form(
            _enrollment_fields(),
            "chrome_mesh_create",
            "Create secure enrollment",
            title="Enroll a device",
        )
    )
    out.append(
        _sdui.button("Share this device public identity", "chrome_mesh_identity")
    )
    out.append(
        _sdui.form(
            [_sdui.field("link", "Secure enrollment link")],
            "chrome_mesh_redeem",
            "Complete enrollment on this device",
            title="Complete enrollment",
        )
    )
    return out


def _member_components(member: dict) -> list[dict]:
    from webrender.chrome.surfaces import _sdui

    label, variant = _member_badge(member)
    facts = _sdui.key_value(
        [{"label": name, "value": value} for name, value in _member_facts(member)]
    )
    content = [_sdui.badge(label, variant), facts]
    if member.get("status") == "active":
        content.extend(
            [
                _sdui.form(
                    [
                        _sdui.field(
                            "label", "Device name", default=member.get("label") or ""
                        )
                    ],
                    "chrome_mesh_rename",
                    "Rename",
                    {"member_id": member["member_id"]},
                ),
                _sdui.button(
                    "Revoke device",
                    "chrome_mesh_revoke",
                    {"member_id": member["member_id"]},
                ),
            ]
        )
    return [_sdui.card(member.get("label") or "", content)]


def _invitation_components(record: dict) -> list[dict]:
    from webrender.chrome.surfaces import _sdui

    label, variant = _invitation_state(record)
    facts = _sdui.key_value(
        [{"label": name, "value": value} for name, value in _invitation_facts(record)]
    )
    content = [_sdui.badge(label, variant), facts]
    if label == "Pending confirmation":
        content.extend(
            [
                _sdui.button(
                    "Confirm enrollment",
                    "chrome_mesh_confirm",
                    {"invite_id": record["invite_id"]},
                ),
                _sdui.button(
                    "Reject", "chrome_mesh_reject", {"invite_id": record["invite_id"]}
                ),
            ]
        )
    elif record.get("status") != "pending":
        content.append(
            _sdui.button(
                "Remove request",
                "chrome_mesh_remove_invitation",
                {"invite_id": record["invite_id"]},
            )
        )
    return [_sdui.card(record.get("label") or "", content)]


def _action_input(payload, action):
    from persistent_agents.models import AssignmentError

    top_level = {"surface"}
    field_names = set()
    if action == "create":
        top_level.add("fields")
        field_names = {"label", "device_key", "scopes", "ttl_seconds"}
    elif action == "rename":
        top_level.update({"fields", "member_id"})
        field_names = {"label"}
    elif action == "revoke":
        top_level.add("member_id")
    else:
        top_level.add("invite_id")
    if (
        type(payload) is not dict
        or set(payload) - top_level
        or payload.get("surface", SURFACE_KEY) != SURFACE_KEY
    ):
        raise AssignmentError("mesh_control_invalid", 422)
    fields = payload.get("fields", {})
    if (
        type(fields) is not dict
        or set(fields) - field_names
        or any(type(value) is not str or len(value) > 2048 for value in fields.values())
    ):
        raise AssignmentError("mesh_control_invalid", 422)
    identifier = payload.get(
        "member_id" if action in {"rename", "revoke"} else "invite_id"
    )
    if action != "create" and (
        type(identifier) is not str
        or not 1 <= len(identifier) <= 128
        or not identifier.isalnum()
    ):
        raise AssignmentError("mesh_control_invalid", 422)
    try:
        if action == "create":
            key = json.loads(fields.get("device_key", ""))
            if type(key) is not dict or set(key) != {"kty", "crv", "x"}:
                raise ValueError
            return {
                "label": fields.get("label"),
                "device_key": me.normalize_device_key(key),
                "scopes": [
                    scope.strip()
                    for scope in fields.get("scopes", "").split(",")
                    if scope.strip()
                ],
                "ttl_seconds": int(fields.get("ttl_seconds", "3600")),
            }
        return {"identifier": identifier, "label": fields.get("label")}
    except (ValueError, TypeError, RecursionError):
        raise AssignmentError("mesh_control_invalid", 422) from None


async def _control(orch, websocket, user_id, payload, action):
    from orchestrator.chrome_events import open_surface_for
    from orchestrator.human_request_authority import current_human_caller
    from persistent_agents.models import AssignmentError

    caller = current_human_caller(expected_orchestrator=orch)
    if (
        caller is None
        or caller.owner_id != user_id
        or caller._binding.socket_request is None
        or caller._binding.socket_request.websocket is not websocket
        or open_surface_for(orch, websocket) != SURFACE_KEY
    ):
        raise AssignmentError("human_authentication_required", 401)
    caller.require_write()
    consent = caller.require_session()
    values = _action_input(payload, action)
    store = _store(orch)
    if (
        store._credentials.plane_runtime is not caller.runtime
        or store._credentials.repository is not caller.repositories.credentials
        or store._membership is not caller.repositories.mesh_enrollment
        or store._audit is not caller.audit_repo
    ):
        raise AssignmentError("human_request_unavailable", 503)
    from orchestrator.mesh_links import enrollment_link

    if action == "create":
        enrollment_link("x")
    credential = consent.credential
    custody = {
        "incarnation_id": credential.incarnation_id,
        "issuer": credential.issuing_issuer,
        "client_id": credential.issuing_client_id,
    }

    def mutate(transaction, repositories):
        try:
            if action == "create":
                result = store.create_invitation(
                    user_id,
                    creator_kind="owner",
                    creator_id=user_id,
                    transaction=transaction,
                    **values,
                )
                created = _CreatedEnrollment(
                    user_id,
                    credential.incarnation_id,
                    result["payload"],
                    _qr_image(enrollment_link(result["payload"])),
                    enrollment_link(result["payload"]),
                )
                return created, None
            if action in {"confirm", "reject"}:
                store.decide_invitation(
                    user_id,
                    values["identifier"],
                    decision="confirmed" if action == "confirm" else "rejected",
                    decider_kind="owner",
                    decider_id=user_id,
                    custody=custody,
                    consent_observation=consent,
                    transaction=transaction,
                )
            elif action == "rename":
                store.rename_member(
                    user_id,
                    values["identifier"],
                    label=values["label"],
                    transaction=transaction,
                )
            elif action == "revoke":
                store.revoke_member(
                    user_id, values["identifier"], transaction=transaction
                )
            else:
                store.remove_invitation(
                    user_id, values["identifier"], transaction=transaction
                )
            return None, None
        except (me.MeshEnrollmentError, ValueError) as error:
            code = (
                error.code
                if isinstance(error, me.MeshEnrollmentError)
                else "mesh_control_invalid"
            )
            store._audit_transition(
                transaction,
                user_id,
                "mesh.surface.denied",
                outcome="failure",
                meta={"action": action, "reason": code},
            )
            return None, code

    created, refusal = await caller.transaction(mutate, expected_orchestrator=orch)
    await caller.verify_delivery()
    if refusal is not None:
        return (
            SURFACE_KEY,
            {},
            notice_block(
                "error", "The mesh change was refused. Refresh the inventory and retry."
            ),
        )
    params = {"created": created, "view": "invitations"} if created else {}
    return SURFACE_KEY, params, notice_block("success", "Mesh inventory updated.")


async def _create(orch, websocket, user_id, roles, payload):
    return await _control(orch, websocket, user_id, payload, "create")


async def _confirm(orch, websocket, user_id, roles, payload):
    return await _control(orch, websocket, user_id, payload, "confirm")


async def _reject(orch, websocket, user_id, roles, payload):
    return await _control(orch, websocket, user_id, payload, "reject")


async def _rename(orch, websocket, user_id, roles, payload):
    return await _control(orch, websocket, user_id, payload, "rename")


async def _revoke(orch, websocket, user_id, roles, payload):
    return await _control(orch, websocket, user_id, payload, "revoke")


async def _remove_invitation(orch, websocket, user_id, roles, payload):
    return await _control(orch, websocket, user_id, payload, "remove_invitation")


HANDLERS = {
    "chrome_mesh_create": _create,
    "chrome_mesh_confirm": _confirm,
    "chrome_mesh_reject": _reject,
    "chrome_mesh_rename": _rename,
    "chrome_mesh_revoke": _revoke,
    "chrome_mesh_remove_invitation": _remove_invitation,
}
