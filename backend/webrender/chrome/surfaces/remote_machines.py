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

_DISABLED_MSG = "Remote compute is disabled on this server."


def _enabled() -> bool:
    """FF_REMOTE_COMPUTE re-check (T064). The surfaces registry maps this key
    unconditionally, so every entry point — render, components, and each
    chrome_* handler — re-checks the flag itself; otherwise a crafted ui_event
    could mutate machine state while the feature is off (the flag must keep
    flag-off byte-identical, mirroring the BYO ``byo_enabled()`` posture)."""
    from shared.feature_flags import flags
    return flags.is_enabled("remote_compute")

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


def _cred_inputs_html() -> str:
    """The credential inputs shared by the add-machine form and each machine's
    replace-credential form. SSH-key fields and the password field are BOTH in
    the DOM; the astral-cred-type change handler in client.js shows only the
    group that matches the selected credential type (the chrome modal has no
    reactive re-render — same pattern as the LLM provider/endpoint toggle). The
    server handlers already read private_key OR password by cred_type, so a
    hidden field being submitted is inert."""
    return (
        f'<label class="{_LABEL_CLS}"><span class="{_LABEL_TEXT_CLS}">Credential type</span>'
        f'{_select("cred_type", _CRED, "ssh_key", extra_cls="astral-cred-type")}</label>'
        f'<div class="astral-cred-group astral-cred-ssh_key space-y-3">'
        f'<label class="{_LABEL_CLS}"><span class="{_LABEL_TEXT_CLS}">Private key (paste the full PEM)</span>'
        f'<textarea name="private_key" rows="8" spellcheck="false" class="{_INPUT_CLS}" '
        f'placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"></textarea></label>'
        f'{_text_field("passphrase", "Key passphrase (optional)", input_type="password")}'
        f'</div>'
        f'<div class="astral-cred-group astral-cred-password space-y-3" style="display:none">'
        f'{_text_field("password", "Password", input_type="password")}'
        f'</div>')


def _machines_html(orch, user_id: str) -> str:
    rows = remote_machines.list_machines(orch.history.db, user_id)
    if not rows:
        return ('<p class="text-sm text-astral-muted">No machines yet. Add one below — the '
                'inventory starts empty and nothing is pre-filled.</p>')
    cards = []
    for r in rows:
        mid = esc(r["machine_id"])
        verdict = esc(r.get("last_verdict") or "not yet probed")
        # Re-trust is offered ONLY after a host_key_mismatch verdict — the one
        # deliberate path that accepts a changed host identity (FR-020).
        retrust = ""
        if r.get("last_verdict") == "host_key_mismatch":
            retrust = (f'<button type="button" class="{_BTN_PRIMARY}" '
                       f'data-ui-action="chrome_machine_retrust" '
                       f"data-ui-payload='{{\"machine_id\":\"{mid}\"}}'>Re-trust</button>")
        cred_form = (
            '<details class="pt-2"><summary class="text-sm text-astral-muted cursor-pointer">'
            'Credential…</summary>'
            f'<div data-ui-form class="space-y-3 pt-2">{_cred_inputs_html()}'
            f'<div class="flex justify-end gap-2">'
            f'<button type="button" class="{_BTN}" data-ui-action="chrome_machine_credential_delete" '
            f"data-ui-payload='{{\"machine_id\":\"{mid}\"}}'>Remove credential</button>"
            f'<button type="button" class="{_BTN_PRIMARY}" data-ui-action="chrome_machine_credential_set" '
            f"data-ui-payload='{{\"machine_id\":\"{mid}\"}}' data-ui-collect=\"true\">"
            'Replace &amp; probe</button>'
            f'</div></div></details>')
        cards.append(
            '<div class="bg-white/5 border border-white/10 rounded-lg px-3 py-2">'
            '<div class="flex items-center justify-between gap-3">'
            f'<div class="text-sm"><div class="text-astral-text font-medium">{esc(r["label"])}</div>'
            f'<div class="text-astral-muted">{esc(r["address"])}:{esc(str(r["port"]))} · '
            f'{esc(r["os_family"])} · {esc(r["role"])} · last: {verdict}</div></div>'
            '<div class="flex gap-2 shrink-0">'
            f'{retrust}'
            f'<button type="button" class="{_BTN}" data-ui-action="chrome_machine_probe" '
            f"data-ui-payload='{{\"machine_id\":\"{mid}\"}}'>Probe</button>"
            f'<button type="button" class="{_BTN}" data-ui-action="chrome_machine_delete" '
            f"data-ui-payload='{{\"machine_id\":\"{mid}\"}}'>Delete</button>"
            '</div></div>'
            f'{cred_form}'
            '</div>')
    return '<div class="space-y-2">' + "".join(cards) + '</div>'


async def render(orch: Any, user_id: str, roles: Any, params: Any) -> str:
    if not _enabled():
        return f'<p class="text-sm text-astral-muted">{esc(_DISABLED_MSG)}</p>'
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
        f'{_cred_inputs_html()}'
        f'<div class="flex justify-end gap-2">'
        f'<button type="button" class="{_BTN_PRIMARY}" data-ui-action="chrome_machine_add" '
        f'data-ui-collect="true">Add &amp; probe</button></div>'
        '</div>')
    return (f'<div class="space-y-4"><div class="text-sm text-astral-muted">'
            'Register your own machines and clusters. On save the product makes a real '
            'connection and reports what happened. Your machines are private to you.</div>'
            f'{machines}{form}</div>')


async def components(orch: Any, user_id: str, roles: Any, params: Any):
    """Feature 063 (T025) — the surface as native SDUI components.

    BOTH credential inputs — the private-key textarea and the password field —
    are always present in the payload; the ``chrome_machine_add`` handler
    already reads whichever matches ``cred_type`` and ignores the other. The
    credential inputs additionally carry ``visible_when`` markers so clients
    that support declarative visibility show only the inputs matching the
    selected credential type; older shipped clients ignore the attribute and
    render every field (the pre-063.1 behavior). Same handler keys + same
    ``fields`` payload shape as the web ``render()`` form, so HANDLERS are
    unchanged. Template: webrender/chrome/surfaces/llm.py.
    """
    import asyncio

    from webrender.chrome.surfaces import _sdui

    def _cred_fields_sdui():
        """The credential fields shared by the add-machine form and each
        machine's replace-credential form (same names + visible_when markers,
        so the same handler parsing applies)."""
        return [
            _sdui.field("cred_type", "Credential type", "select", default="ssh_key",
                        options=list(_CRED),
                        help="Pick ssh_key to paste a private key below, or password."),
            _sdui.field("private_key", "Private key (paste the full PEM)",
                        "textarea", help="Used only when the credential type is ssh_key.",
                        visible_when={"field": "cred_type", "equals": "ssh_key",
                                      "default": "ssh_key"}),
            _sdui.field("passphrase", "Key passphrase (optional)", "password",
                        visible_when={"field": "cred_type", "equals": "ssh_key",
                                      "default": "ssh_key"}),
            _sdui.field("password", "Password", "password",
                        help="Used only when the credential type is password.",
                        visible_when={"field": "cred_type", "equals": "password",
                                      "default": "ssh_key"}),
        ]

    if not _enabled():
        return [_sdui.text(_DISABLED_MSG, "caption")]
    rows = await asyncio.to_thread(remote_machines.list_machines, orch.history.db, user_id)
    out = [_sdui.text("Register your own machines and clusters. On save the product makes a "
                      "real connection and reports what happened. Your machines are private "
                      "to you.", "caption")]
    if not rows:
        out.append(_sdui.text("No machines yet — add one below.", "caption"))
    else:
        for r in rows:
            mid = r["machine_id"]
            facts = _sdui.key_value([
                {"label": "Address", "value": f'{r["address"]}:{r["port"]}'},
                {"label": "OS", "value": r["os_family"]},
                {"label": "Role", "value": r["role"]},
                {"label": "Last check", "value": r.get("last_verdict") or "not yet probed"},
            ], columns=2)
            buttons = [_sdui.button("Probe", "chrome_machine_probe", payload={"machine_id": mid})]
            # Re-trust appears ONLY after a host_key_mismatch verdict — the one
            # deliberate path that accepts a changed host identity (FR-020).
            if r.get("last_verdict") == "host_key_mismatch":
                buttons.append(_sdui.button("Re-trust", "chrome_machine_retrust",
                                            payload={"machine_id": mid}))
            buttons.append(_sdui.button("Remove credential", "chrome_machine_credential_delete",
                                        payload={"machine_id": mid}, variant="secondary"))
            buttons.append(_sdui.button("Delete", "chrome_machine_delete",
                                        payload={"machine_id": mid}, variant="secondary"))
            cred_form = _sdui.form(_cred_fields_sdui(),
                                   submit_action="chrome_machine_credential_set",
                                   submit_label="Replace & probe",
                                   submit_payload={"machine_id": mid},
                                   title="Replace credential")
            out.append(_sdui.card(r["label"], [facts,
                                               _sdui.container(buttons, direction="row"),
                                               cred_form]))

    fields = [
        _sdui.field("label", "Label", "text", help="A short name, e.g. my-dgx."),
        _sdui.field("address", "Address", "text", help="Hostname or IP, e.g. dgx.ai.uky.edu."),
        _sdui.field("port", "Port", "number", default="22"),
        _sdui.field("username", "Username", "text"),
        _sdui.field("os_family", "Operating system", "select", default="linux", options=list(_OS)),
        _sdui.field("role", "Role", "select", default="cluster", options=list(_ROLE)),
    ] + _cred_fields_sdui()
    out.append(_sdui.form(fields, submit_action="chrome_machine_add",
                          submit_label="Add & probe", title="Add a machine"))
    return out


# ── handlers ─────────────────────────────────────────────────────────────────

def _credential_from_fields(f: Dict[str, str]):
    """Validate the credential portion of a submitted form. Returns
    ``(cred_type, secret, passphrase)``, or an error-message string. Reads ONLY
    the inputs matching ``cred_type`` — legacy clients submit every field."""
    cred_type = f.get("cred_type") if f.get("cred_type") in _CRED else "ssh_key"
    if cred_type == "ssh_key":
        secret = f.get("private_key") or ""
        if not secret:
            return "Paste a private key for an SSH-key credential."
        if not secret.endswith("\n"):
            secret += "\n"  # PEMs want a trailing newline
        return (cred_type, secret, f.get("passphrase") or None)
    secret = f.get("password") or ""
    if not secret:
        return "Enter a password for a password credential."
    return (cred_type, secret, None)


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
    if not _enabled():
        return (SURFACE_KEY, {}, notice_block("error", _DISABLED_MSG))
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
    cred = _credential_from_fields(f)
    if isinstance(cred, str):
        return (SURFACE_KEY, {}, notice_block("error", cred))
    cred_type, secret, passphrase = cred

    db = orch.history.db
    machine_id = remote_machines.create_machine(db, user_id, label, address, port, username, os_family, role)
    orch.credential_manager.set_machine_credential(machine_id, user_id, cred_type, secret, passphrase)
    remote_machines.audit_machine_event(
        user_id, "remote_machine.registered", f"registered machine {label}",
        machine_id=machine_id, label=label, cred_type=cred_type)
    notice = _probe_notice(orch, user_id, machine_id, label)
    return (SURFACE_KEY, {}, notice)


async def _h_machine_probe(orch, websocket, user_id, roles, payload):
    if not _enabled():
        return (SURFACE_KEY, {}, notice_block("error", _DISABLED_MSG))
    machine_id = (payload or {}).get("machine_id")
    if not machine_id:
        return (SURFACE_KEY, {}, notice_block("error", "No machine specified."))
    row = remote_machines.get_machine(orch.history.db, user_id, machine_id)
    if row is None:
        return (SURFACE_KEY, {}, notice_block("error", "That machine is not in your inventory."))
    return (SURFACE_KEY, {}, _probe_notice(orch, user_id, machine_id, row["label"]))


async def _h_machine_delete(orch, websocket, user_id, roles, payload):
    if not _enabled():
        return (SURFACE_KEY, {}, notice_block("error", _DISABLED_MSG))
    machine_id = (payload or {}).get("machine_id")
    if not machine_id:
        return (SURFACE_KEY, {}, notice_block("error", "No machine specified."))
    row = remote_machines.get_machine(orch.history.db, user_id, machine_id)
    label = row["label"] if row else machine_id
    ok = remote_machines.delete_machine(orch.history.db, user_id, machine_id)
    if ok:
        # Belt-and-suspenders credential destroy (FK also cascades) — ONLY after
        # the owner-scoped delete succeeded: the credential-manager delete is
        # keyed by machine_id alone, so running it on a refused delete would let
        # a non-owner destroy another user's credential.
        try:
            orch.credential_manager.delete_machine_credential(machine_id)
        except Exception:
            pass
        remote_machines.audit_machine_event(
            user_id, "remote_machine.removed",
            f"removed machine {label} and its credential",
            machine_id=machine_id, label=label)
        return (SURFACE_KEY, {}, notice_block("success", f"Removed {esc(label)} and its credential."))
    return (SURFACE_KEY, {}, notice_block("error", "That machine is not in your inventory."))


async def _h_credential_set(orch, websocket, user_id, roles, payload):
    """Machine-scoped credential replace: owner check, encrypt + upsert via the
    credential manager, then an immediate probe verdict like add (FR-013)."""
    if not _enabled():
        return (SURFACE_KEY, {}, notice_block("error", _DISABLED_MSG))
    machine_id = (payload or {}).get("machine_id")
    if not machine_id:
        return (SURFACE_KEY, {}, notice_block("error", "No machine specified."))
    row = remote_machines.get_machine(orch.history.db, user_id, machine_id)
    if row is None:
        return (SURFACE_KEY, {}, notice_block("error", "That machine is not in your inventory."))
    cred = _credential_from_fields(_fields(payload))
    if isinstance(cred, str):
        return (SURFACE_KEY, {}, notice_block("error", cred))
    cred_type, secret, passphrase = cred
    orch.credential_manager.set_machine_credential(machine_id, user_id, cred_type, secret, passphrase)
    remote_machines.audit_machine_event(
        user_id, "remote_machine.credential_set",
        f"replaced credential for {row['label']} ({cred_type})",
        machine_id=machine_id, label=row["label"], cred_type=cred_type)
    return (SURFACE_KEY, {}, _probe_notice(orch, user_id, machine_id, row["label"]))


async def _h_credential_delete(orch, websocket, user_id, roles, payload):
    """Machine-scoped credential removal (FR-015). Owner check FIRST — the
    credential-manager delete itself is keyed by machine_id only."""
    if not _enabled():
        return (SURFACE_KEY, {}, notice_block("error", _DISABLED_MSG))
    machine_id = (payload or {}).get("machine_id")
    if not machine_id:
        return (SURFACE_KEY, {}, notice_block("error", "No machine specified."))
    row = remote_machines.get_machine(orch.history.db, user_id, machine_id)
    if row is None:
        return (SURFACE_KEY, {}, notice_block("error", "That machine is not in your inventory."))
    try:
        orch.credential_manager.delete_machine_credential(machine_id)
    except Exception:
        return (SURFACE_KEY, {}, notice_block("error", "Could not remove the credential."))
    remote_machines.audit_machine_event(
        user_id, "remote_machine.credential_deleted",
        f"deleted credential for {row['label']}",
        machine_id=machine_id, label=row["label"])
    return (SURFACE_KEY, {}, notice_block(
        "success", f"Removed the credential for {esc(row['label'])} — add a new one to reconnect."))


async def _h_machine_retrust(orch, websocket, user_id, roles, payload):
    """Deliberate re-trust after a host_key_mismatch — clears the pinned key via
    ``remote_machines.retrust_host_key`` (the ONLY path that accepts a changed
    host identity, FR-020), then re-probes so the new key is recorded and an
    honest verdict shown."""
    if not _enabled():
        return (SURFACE_KEY, {}, notice_block("error", _DISABLED_MSG))
    machine_id = (payload or {}).get("machine_id")
    if not machine_id:
        return (SURFACE_KEY, {}, notice_block("error", "No machine specified."))
    row = remote_machines.get_machine(orch.history.db, user_id, machine_id)
    if row is None:
        return (SURFACE_KEY, {}, notice_block("error", "That machine is not in your inventory."))
    remote_machines.retrust_host_key(orch.history.db, user_id, machine_id)
    remote_machines.audit_machine_event(
        user_id, "remote_machine.retrusted",
        f"cleared pinned host key for {row['label']} — next connection re-records it",
        machine_id=machine_id, label=row["label"])
    return (SURFACE_KEY, {}, _probe_notice(orch, user_id, machine_id, row["label"]))


# The credential/retrust actions are machine-namespaced (chrome_machine_*):
# chrome handlers aggregate into ONE flat action map (surfaces/__init__.py
# collect_handlers), and the agents surface already owns plain
# chrome_credential_delete for per-agent credentials.
HANDLERS = {
    "chrome_machine_add": _h_machine_add,
    "chrome_machine_probe": _h_machine_probe,
    "chrome_machine_delete": _h_machine_delete,
    "chrome_machine_credential_set": _h_credential_set,
    "chrome_machine_credential_delete": _h_credential_delete,
    "chrome_machine_retrust": _h_machine_retrust,
}
