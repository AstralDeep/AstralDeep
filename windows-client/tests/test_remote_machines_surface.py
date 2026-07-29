"""Feature 063 — the Windows native renderer draws the remote_machines settings
surface (server ``components()``) correctly, with zero surface-specific client
code. The one native risk the parity map flagged is the ``textarea`` field kind
(the pasted PEM) — no other shipping surface uses it — so this locks it: a
multi-line PEM survives the QPlainTextEdit and the form's action-submit posts the
exact ``chrome_machine_add {fields}`` payload the backend handler parses.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QPlainTextEdit, QPushButton  # noqa: E402

from astral_client.renderer import RenderContext, render  # noqa: E402

# The ParamPicker the server's remote_machines.components() emits (all credential
# fields always present — native forms can't reactively show/hide on cred_type).
FORM = {
    "type": "param_picker",
    "title": "Add a machine",
    "submit_label": "Add & probe",
    "submit_action": "chrome_machine_add",
    "fields": [
        {"name": "label", "label": "Label", "kind": "text"},
        {"name": "address", "label": "Address", "kind": "text"},
        {"name": "port", "label": "Port", "kind": "number", "default": "22"},
        {"name": "username", "label": "Username", "kind": "text"},
        {"name": "os_family", "label": "OS", "kind": "select",
         "options": ["linux", "windows", "macos"], "default": "linux"},
        {"name": "role", "label": "Role", "kind": "select",
         "options": ["cluster", "plain"], "default": "cluster"},
        {"name": "cred_type", "label": "Credential type", "kind": "select",
         "options": ["ssh_key", "password"], "default": "ssh_key"},
        {"name": "private_key", "label": "Private key", "kind": "textarea"},
        {"name": "passphrase", "label": "Key passphrase", "kind": "password"},
        {"name": "password", "label": "Password", "kind": "password"},
    ],
}

PEM = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
       "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ\n"
       "AAAAtzc2gtZWQyNTUxOQAAACD0line3line4pad==\n"
       "-----END OPENSSH PRIVATE KEY-----")


def _ctx(sink):
    return RenderContext(emit=sink)


def test_pem_textarea_round_trips_through_action_submit(qapp):
    seen = []
    w = render(FORM, _ctx(lambda a, p: seen.append((a, p))))

    # The private_key field is the only textarea (QPlainTextEdit).
    areas = w.findChildren(QPlainTextEdit)
    assert len(areas) == 1, "expected exactly one textarea (private_key)"
    areas[0].setPlainText(PEM)

    # A single-submit_action form renders exactly one action button.
    btns = w.findChildren(QPushButton)
    assert len(btns) == 1
    btns[0].click()

    assert len(seen) == 1
    action, payload = seen[0]
    assert action == "chrome_machine_add"
    fields = payload["fields"]
    # Newlines survive the Qt textarea → the handler stores a valid multi-line PEM.
    assert fields["private_key"] == PEM
    assert fields["private_key"].count("\n") == PEM.count("\n") >= 3
    # All fields are present (native no-toggle) and select defaults flow through.
    assert fields["cred_type"] == "ssh_key" and fields["os_family"] == "linux"
    assert set(fields) >= {"label", "address", "port", "username", "os_family",
                           "role", "cred_type", "private_key", "passphrase", "password"}


def test_machine_row_probe_and_delete_emit_with_machine_id(qapp):
    seen = []
    ctx = _ctx(lambda a, p: seen.append((a, p)))
    card = {
        "type": "card", "title": "dgx",
        "content": [
            {"type": "keyvalue", "items": [{"label": "Address", "value": "dgx.ai.uky.edu:22"}]},
            {"type": "container", "direction": "row", "children": [
                {"type": "button", "label": "Probe", "action": "chrome_machine_probe",
                 "payload": {"machine_id": "m1"}},
                {"type": "button", "label": "Delete", "action": "chrome_machine_delete",
                 "payload": {"machine_id": "m1"}, "variant": "secondary"},
            ]},
        ],
    }
    w = render(card, ctx)
    buttons = {b.text().replace("&", ""): b for b in w.findChildren(QPushButton)}
    next(b for t, b in buttons.items() if "Probe" in t).click()
    next(b for t, b in buttons.items() if "Delete" in t).click()
    assert ("chrome_machine_probe", {"machine_id": "m1"}) in seen
    assert ("chrome_machine_delete", {"machine_id": "m1"}) in seen
