"""Renders the shared mesh-enrollment surface: owner member inventory, invitation
states, and sanitized provenance consumed from the completed workflows in
orchestrator/mesh_api.py and orchestrator/mesh_enrollment.py.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

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
    "authority: a request must be confirmed through the mesh API while your "
    "session is live, and a revoked member cannot act. Enrollment payloads are "
    "one-time secrets; never share them beyond the joining device."
)

_ENROLL_STEPS = (
    "Generate a signing key on the joining device (astral-sdk mesh key-init).",
    "Create an invitation for that device key through POST /api/mesh/invitations "
    "with your owner session, or the astral-sdk owner client.",
    "Confirm the request before it expires; the one-time payload link is shown "
    "only at creation.",
    "The device redeems the payload and appears below as a member you can "
    "rename or revoke.",
)


def _store(orch) -> me.MeshEnrollmentStore:
    return me.MeshEnrollmentStore(plane_source_from_orchestrator(orch))


def _short_fingerprint(value) -> str:
    raw = str(value or "")
    if raw.startswith("sha256-"):
        raw = raw[len("sha256-"):]
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
    if status == "pending":
        return ("Expired", "error") if expired else ("Pending confirmation", "warning")
    if status == "confirmed":
        if record.get("redeemed_at") is not None:
            return ("Enrolled", "success")
        return ("Expired", "error") if expired else ("Confirmed — awaiting device", "info")
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
        "Enroll a device</h3><ol class=\"list-decimal ml-4 space-y-1 pt-2\">"
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
        f"{_guidance_html()}"
    )
    return f'<div class="space-y-4">{_view_tabs(view)}{body}</div>'


def _member_cards_html(members: list[dict]) -> list[str]:
    cards = []
    for member in members:
        label, variant = _member_badge(member)
        cards.append(
            f'<div class="{_CARD_CLS}">'
            f'<div class="flex items-center justify-between gap-3">'
            f'<span class="text-sm font-medium text-astral-text">{esc(member.get("label") or "")}</span>'
            f'<span class="astral-badge astral-badge--{variant} inline-flex items-center '
            f'px-2 py-0.5 rounded-full border text-xs font-medium">{esc(label)}</span>'
            f"</div>"
            f'<div class="pt-2">{_facts_html(_member_facts(member))}</div>'
            f"</div>"
        )
    return cards


def _invitation_cards_html(invitations: list[dict]) -> list[str]:
    cards = []
    for record in invitations:
        label, variant = _invitation_state(record)
        pending_note = (
            '<p class="text-xs text-astral-muted pt-1">Confirm or reject this '
            "request through the mesh API before it expires.</p>"
            if label == "Pending confirmation"
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
            for key, label in (("members", "Members"), ("invitations", "Enrollment requests"))
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
    out.append(_sdui.card("Enroll a device", [_sdui.bullet_list(list(_ENROLL_STEPS), True)]))
    return out


def _member_components(member: dict) -> list[dict]:
    from webrender.chrome.surfaces import _sdui

    label, variant = _member_badge(member)
    facts = _sdui.key_value(
        [{"label": name, "value": value} for name, value in _member_facts(member)]
    )
    return [_sdui.card(member.get("label") or "", [_sdui.badge(label, variant), facts])]


def _invitation_components(record: dict) -> list[dict]:
    from webrender.chrome.surfaces import _sdui

    label, variant = _invitation_state(record)
    facts = _sdui.key_value(
        [{"label": name, "value": value} for name, value in _invitation_facts(record)]
    )
    content = [_sdui.badge(label, variant), facts]
    if label == "Pending confirmation":
        content.append(
            _sdui.text(
                "Confirm or reject this request through the mesh API before it "
                "expires.",
                "caption",
            )
        )
    return [_sdui.card(record.get("label") or "", content)]
