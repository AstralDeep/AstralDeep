"""Feature 063 — Remote machines inventory & per-machine credential surface.

Surface key ``remote_machines``. Web ``render()`` (this file); native parity
(``components()``) is a follow-up (spec task T025). The user registers their own
SSH machines/clusters here and pastes a multi-line key; on save the product opens
a real connection and shows an honest verdict (FR-013). Owner-scoped throughout
(FR-010/FR-018). Template: webrender/chrome/surfaces/llm.py.
"""
from __future__ import annotations

from typing import Any, Dict

from webrender.chrome import esc, notice_block
from orchestrator import remote_machines
from orchestrator.credential_manager import CredentialUndecryptable
from orchestrator.remote_transport import get_transport

TITLE = "Remote machines"
SURFACE_KEY = "remote_machines"

_INPUT_CLS = ("rounded-lg bg-white/10 border border-white/10 px-3 py-2 text-sm "
              "text-astral-text w-full focus:outline-none focus:border-astral-primary/50")
_LABEL_CLS = "flex flex-col gap-1 text-sm"
_LABEL_TEXT_CLS = "text-astral-text font-medium"
_BTN = "px-3 py-2 rounded-lg text-sm font-medium bg-white/5 text-astral-text border border-white/10 hover:bg-white/10"
_BTN_PRIMARY = ("px-3 py-2 rounded-lg text-sm font-medium bg-astral-primary/20 "
                "text-astral-primary border border-astral-primary/30 hover:bg-astral-primary/30")
_OS = ("linux", "windows", "macos")
_ROLE = ("cluster", "plain")
_CRED = ("ssh_key", "password")


def _fields(payload: Any) -> Dict[str, str]:
    raw = payload.get("fields") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(k, str) and v is not None and not isinstance(v, (dict, list)):
            out[k] = str(v).strip()
    return out


def _select(name: str, options, current: str, *, extra_cls: str = "") -> str:
    opts = "".join(
        f'<option value="{esc(v)}"{" selected" if v == current else ""}>{esc(v)}</option>'
        for v in options)
    cls = f"{_INPUT_CLS} {extra_cls}".strip()
    return f'<select name="{esc(name)}" class="{cls}">{opts}</select>'


def _text_field(name: str, label: str, *, value: str = "", placeholder: str = "",
                input_type: str = "text") -> str:
    return (f'<label class="{_LABEL_CLS}"><span class="{_LABEL_TEXT_CLS}">{esc(label)}</span>'
            f'<input type="{input_type}" name="{esc(name)}" value="{esc(value)}" '
            f'autocomplete="off" placeholder="{esc(placeholder)}" class="{_INPUT_CLS}"></label>')


def _machines_html(orch, user_id: str) -> str:
    rows = remote_machines.list_machines(orch.history.db, user_id)
    if not rows:
        return ('<p class="text-sm text-astral-muted">No machines yet. Add one below — the '
                'inventory starts empty and nothing is pre-filled.</p>')
    cards = []
    for r in rows:
        mid = esc(r["machine_id"])
        verdict = esc(r.get("last_verdict") or "not yet probed")
        cards.append(
            '<div class="flex items-center justify-between gap-3 bg-white/5 border '
            'border-white/10 rounded-lg px-3 py-2">'
            f'<div class="text-sm"><div class="text-astral-text font-medium">{esc(r["label"])}</div>'
            f'<div class="text-astral-muted">{esc(r["address"])}:{esc(str(r["port"]))} · '
            f'{esc(r["os_family"])} · {esc(r["role"])} · last: {verdict}</div></div>'
            '<div class="flex gap-2 shrink-0">'
            f'<button type="button" class="{_BTN}" data-ui-action="chrome_machine_probe" '
            f"data-ui-payload='{{\"machine_id\":\"{mid}\"}}'>Probe</button>"
            f'<button type="button" class="{_BTN}" data-ui-action="chrome_machine_delete" '
            f"data-ui-payload='{{\"machine_id\":\"{mid}\"}}'>Delete</button>"
            '</div></div>')
    return '<div class="space-y-2">' + "".join(cards) + '</div>'


async def render(orch: Any, user_id: str, roles: Any, params: Any) -> str:
    params = params if isinstance(params, dict) else {}
    machines = _machines_html(orch, user_id)
    form = (
        '<div data-ui-form class="space-y-3 bg-white/5 border border-white/10 rounded-lg p-4">'
        f'<div class="text-astral-text font-medium">Add a machine</div>'
        f'{_text_field("label", "Label", placeholder="my-dgx")}'
        f'{_text_field("address", "Address", placeholder="dgx.ai.uky.edu")}'
        f'{_text_field("port", "Port", value="22")}'
        f'{_text_field("username", "Username", placeholder="you")}'
        f'<label class="{_LABEL_CLS}"><span class="{_LABEL_TEXT_CLS}">Operating system</span>'
        f'{_select("os_family", _OS, "linux")}</label>'
        f'<label class="{_LABEL_CLS}"><span class="{_LABEL_TEXT_CLS}">Role</span>'
        f'{_select("role", _ROLE, "cluster")}</label>'
        f'<label class="{_LABEL_CLS}"><span class="{_LABEL_TEXT_CLS}">Credential type</span>'
        f'{_select("cred_type", _CRED, "ssh_key", extra_cls="astral-cred-type")}</label>'
        # SSH-key fields and the password field are BOTH in the DOM; the
        # astral-cred-type change handler in client.js shows only the group that
        # matches the selected credential type (the chrome modal has no reactive
        # re-render — same pattern as the LLM provider/endpoint toggle). The server
        # handler already reads private_key OR password by cred_type, so a hidden
        # field being submitted is inert.
        f'<div class="astral-cred-group astral-cred-ssh_key space-y-3">'
        f'<label class="{_LABEL_CLS}"><span class="{_LABEL_TEXT_CLS}">Private key (paste the full PEM)</span>'
        f'<textarea name="private_key" rows="8" spellcheck="false" class="{_INPUT_CLS}" '
        f'placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"></textarea></label>'
        f'{_text_field("passphrase", "Key passphrase (optional)", input_type="password")}'
        f'</div>'
        f'<div class="astral-cred-group astral-cred-password space-y-3" style="display:none">'
        f'{_text_field("password", "Password", input_type="password")}'
        f'</div>'
        f'<div class="flex justify-end gap-2">'
        f'<button type="button" class="{_BTN_PRIMARY}" data-ui-action="chrome_machine_add" '
        f'data-ui-collect="true">Add &amp; probe</button></div>'
        '</div>')
    return (f'<div class="space-y-4"><div class="text-sm text-astral-muted">'
            'Register your own machines and clusters. On save the product makes a real '
            'connection and reports what happened. Your machines are private to you.</div>'
            f'{machines}{form}</div>')


# ── handlers ─────────────────────────────────────────────────────────────────

def _probe_notice(orch, user_id: str, machine_id: str, label: str) -> str:
    """Probe a machine, persist the verdict + first host key, return a notice."""
    db = orch.history.db
    try:
        target = remote_machines.build_target(db, orch.credential_manager, user_id, machine_id)
    except CredentialUndecryptable:
        return notice_block("error", f"{label}: stored credential can't be decrypted — re-enter it.")
    except Exception:
        return notice_block("error", f"{label}: could not load machine or credential.")
    res = get_transport().probe(target, timeout=20.0)
    try:
        remote_machines.record_probe(db, user_id, machine_id, res.verdict.value, host_key=res.host_key)
    except Exception:
        pass
    if res.ok:
        fp = (res.host_key or {}).get("fingerprint") or ""
        extra = f" Host key {esc(fp)}." if fp else ""
        return notice_block("success", f"{esc(label)} is reachable and authenticated.{extra}")
    return notice_block("error", f"{esc(label)}: {esc(res.verdict.value)} — {esc(res.next_action)}")


async def _h_machine_add(orch, websocket, user_id, roles, payload):
    f = _fields(payload)
    label, address, username = f.get("label"), f.get("address"), f.get("username")
    if not (label and address and username):
        return (SURFACE_KEY, {}, notice_block("error", "Label, address and username are required."))
    try:
        port = int(f.get("port") or "22")
        if not (1 <= port <= 65535):
            raise ValueError
    except ValueError:
        return (SURFACE_KEY, {}, notice_block("error", "Port must be a number between 1 and 65535."))
    os_family = f.get("os_family") if f.get("os_family") in _OS else "linux"
    role = f.get("role") if f.get("role") in _ROLE else "plain"
    cred_type = f.get("cred_type") if f.get("cred_type") in _CRED else "ssh_key"

    if cred_type == "ssh_key":
        secret = f.get("private_key") or ""
        if not secret:
            return (SURFACE_KEY, {}, notice_block("error", "Paste a private key for an SSH-key credential."))
        if not secret.endswith("\n"):
            secret += "\n"  # PEMs want a trailing newline
        passphrase = f.get("passphrase") or None
    else:
        secret = f.get("password") or ""
        if not secret:
            return (SURFACE_KEY, {}, notice_block("error", "Enter a password for a password credential."))
        passphrase = None

    db = orch.history.db
    machine_id = remote_machines.create_machine(db, user_id, label, address, port, username, os_family, role)
    orch.credential_manager.set_machine_credential(machine_id, user_id, cred_type, secret, passphrase)
    notice = _probe_notice(orch, user_id, machine_id, label)
    return (SURFACE_KEY, {}, notice)


async def _h_machine_probe(orch, websocket, user_id, roles, payload):
    machine_id = (payload or {}).get("machine_id")
    if not machine_id:
        return (SURFACE_KEY, {}, notice_block("error", "No machine specified."))
    row = remote_machines.get_machine(orch.history.db, user_id, machine_id)
    if row is None:
        return (SURFACE_KEY, {}, notice_block("error", "That machine is not in your inventory."))
    return (SURFACE_KEY, {}, _probe_notice(orch, user_id, machine_id, row["label"]))


async def _h_machine_delete(orch, websocket, user_id, roles, payload):
    machine_id = (payload or {}).get("machine_id")
    if not machine_id:
        return (SURFACE_KEY, {}, notice_block("error", "No machine specified."))
    row = remote_machines.get_machine(orch.history.db, user_id, machine_id)
    label = row["label"] if row else machine_id
    ok = remote_machines.delete_machine(orch.history.db, user_id, machine_id)
    try:
        orch.credential_manager.delete_machine_credential(machine_id)  # belt-and-suspenders (FK also cascades)
    except Exception:
        pass
    if ok:
        return (SURFACE_KEY, {}, notice_block("success", f"Removed {esc(label)} and its credential."))
    return (SURFACE_KEY, {}, notice_block("error", "That machine is not in your inventory."))


HANDLERS = {
    "chrome_machine_add": _h_machine_add,
    "chrome_machine_probe": _h_machine_probe,
    "chrome_machine_delete": _h_machine_delete,
}
