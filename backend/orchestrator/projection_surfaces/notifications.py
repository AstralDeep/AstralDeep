"""Owner-authorized notification chrome state and actions use Plane-backed routing
and inbox services. Projection owns the shared view, rendering and menu definition;
source links are exposed only after current owner-scoped history authorization.
"""

import asyncio

from orchestrator.notification_api import AcknowledgeRequest, PreferenceUpdateRequest, QuietHoursUpdateRequest, _entry_id
from orchestrator.notification_routing import NotificationPolicyInvalid, NotificationRoutingError, NotificationStoreUnavailable
from shared.feature_flags import flags


TITLE = "Notifications"
SURFACE_KEY = "notifications"


def _service(orch):
    service = getattr(orch, "notification_routing", None)
    if not flags.is_enabled("notification_routing") or service is None:
        raise NotificationStoreUnavailable("notifications are unavailable")
    return service


def _owner(orch, websocket, user_id):
    claims = orch.ui_sessions.get(websocket) or {}
    if not user_id or claims.get("sub") != user_id or claims.get("machine_class") == "scheduled":
        raise NotificationPolicyInvalid("notification action requires the current owner session")
    return user_id


def _fields(payload):
    if not isinstance(payload, dict):
        raise NotificationPolicyInvalid("notification action must be an object")
    if "fields" not in payload:
        return payload
    fields = payload["fields"]
    if not isinstance(fields, dict) or set(payload) - {"fields", "expected_revision"}:
        raise NotificationPolicyInvalid("notification form envelope is invalid")
    if "expected_revision" in payload:
        if "expected_revision" in fields:
            raise NotificationPolicyInvalid("notification form revision is ambiguous")
        return {**fields, "expected_revision": payload["expected_revision"]}
    return fields


def _state(orch, user_id, params):
    service = _service(orch)
    state = {"routing": service.preferences(user_id), "quiet_hours": service.quiet_hours(user_id),
             "entries": service.list_inbox(user_id), "selected_entry": None, "source": None}
    if isinstance(params, dict) and params.get("entry_id"):
        entry = service.get_entry(user_id, _entry_id(params["entry_id"]))
        state["selected_entry"] = entry
        chat_id = entry["content"].get("chat_id")
        if isinstance(chat_id, str) and chat_id:
            chat = orch.history.get_chat(chat_id, user_id=user_id)
            if chat is not None:
                state["source"] = {"chat_id": chat_id}
    return state


async def _view(orch, user_id, params):
    from astralprojection.chrome.notifications import build_notifications_view

    try:
        state = await asyncio.to_thread(_state, orch, user_id, params)
    except NotificationRoutingError as exc:
        return build_notifications_view(None, error=exc.code)
    return build_notifications_view(state)


async def render(orch, user_id, roles, params):
    from astralprojection.chrome import render_html

    return render_html(await _view(orch, user_id, params))


async def components(orch, user_id, roles, params):
    return [item.to_dict() for item in (await _view(orch, user_id, params)).components]


async def _preferences(orch, websocket, user_id, roles, payload):
    owner = _owner(orch, websocket, user_id)
    body = PreferenceUpdateRequest.model_validate(_fields(payload))
    await asyncio.to_thread(_service(orch).update_preferences, owner, **body.model_dump())
    return SURFACE_KEY, {}, ""


async def _quiet_hours(orch, websocket, user_id, roles, payload):
    owner = _owner(orch, websocket, user_id)
    body = QuietHoursUpdateRequest.model_validate(_fields(payload))
    await asyncio.to_thread(_service(orch).update_quiet_hours, owner, **body.model_dump())
    return SURFACE_KEY, {}, ""


async def _mutate_entry(orch, websocket, user_id, payload, operation):
    owner = _owner(orch, websocket, user_id)
    fields = _fields(payload)
    if set(fields) != {"entry_id", "expected_revision"}:
        raise NotificationPolicyInvalid("notification entry action fields are invalid")
    entry_id = _entry_id(fields["entry_id"])
    body = AcknowledgeRequest.model_validate({"expected_revision": fields["expected_revision"]})
    await asyncio.to_thread(getattr(_service(orch), operation), owner, entry_id, expected_revision=body.expected_revision)
    return SURFACE_KEY, {}, ""


async def _acknowledge(orch, websocket, user_id, roles, payload):
    return await _mutate_entry(orch, websocket, user_id, payload, "acknowledge")


async def _delete(orch, websocket, user_id, roles, payload):
    return await _mutate_entry(orch, websocket, user_id, payload, "delete_entry")


async def _open(orch, websocket, user_id, roles, payload):
    owner = _owner(orch, websocket, user_id)
    fields = _fields(payload)
    if set(fields) != {"entry_id"}:
        raise NotificationPolicyInvalid("notification open fields are invalid")
    entry_id = _entry_id(fields["entry_id"])
    await asyncio.to_thread(_service(orch).get_entry, owner, entry_id)
    return SURFACE_KEY, {"entry_id": entry_id}, ""


HANDLERS = {
    "chrome_notification_preferences": _preferences,
    "chrome_notification_quiet_hours": _quiet_hours,
    "chrome_notification_acknowledge": _acknowledge,
    "chrome_notification_delete": _delete,
    "chrome_notification_open": _open,
}
