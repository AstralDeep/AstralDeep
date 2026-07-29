"""Feature 063 — the remote-machines surface's native ``components()`` payload.

The credential inputs carry declarative ``visible_when`` markers (client-side
conditional visibility, additive): clients that support the attribute show only
the inputs matching the selected credential type; shipped clients that predate
it ignore the key and render every field, which ``chrome_machine_add`` already
tolerates by reading only the inputs matching ``cred_type``.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from webrender.chrome.surfaces import remote_machines as surface


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    # CI runs with FF_REMOTE_COMPUTE unset; these tests exercise the ENABLED
    # surface. T064's flag_off fixture re-patches False on top for its tests.
    monkeypatch.setattr(surface, "_enabled", lambda: True)


def _find_fields(node):
    """Depth-first hunt for the form's fields list inside the payload."""
    if isinstance(node, dict):
        fields = node.get("fields")
        if isinstance(fields, list) and any(
                isinstance(f, dict) and f.get("name") == "cred_type" for f in fields):
            return fields
        for value in node.values():
            found = _find_fields(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_fields(value)
            if found is not None:
                return found
    return None


@pytest.fixture()
def form_fields(monkeypatch):
    monkeypatch.setattr(surface.remote_machines, "list_machines",
                        lambda db, user_id: [])
    orch = SimpleNamespace(history=SimpleNamespace(db=MagicMock()))
    import asyncio
    components = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        surface.components(orch, "u1", ["user"], {}))
    fields = _find_fields(components)
    assert fields is not None, "components() must include the add-machine form"
    return {f["name"]: f for f in fields}


def test_credential_inputs_carry_visible_when(form_fields):
    for name, expected in (("private_key", "ssh_key"),
                           ("passphrase", "ssh_key"),
                           ("password", "password")):
        vw = form_fields[name].get("visible_when")
        assert vw == {"field": "cred_type", "equals": expected,
                      "default": "ssh_key"}, name


def test_non_credential_fields_are_unconditional(form_fields):
    for name in ("label", "address", "port", "username", "os_family", "role",
                 "cred_type"):
        assert "visible_when" not in form_fields[name], name


def test_visible_when_default_matches_cred_type_field_default(form_fields):
    # The client resolves the controller's value as typed-value-or-default; the
    # marker's embedded default must therefore equal the cred_type field's own.
    assert form_fields["cred_type"]["default"] == "ssh_key"
    for name in ("private_key", "passphrase", "password"):
        assert form_fields[name]["visible_when"]["default"] == "ssh_key"


def test_every_field_still_present_for_legacy_clients(form_fields):
    # visible_when must stay additive: the full field set (the pre-063.1 shape
    # the Windows/Android/Apple store builds render) is unchanged.
    assert set(form_fields) == {"label", "address", "port", "username",
                                "os_family", "role", "cred_type", "private_key",
                                "passphrase", "password"}


# ── FF_REMOTE_COMPUTE re-check at every surface entry point (T064) ────────────
#
# The surfaces registry maps the key unconditionally, so render/components and
# each chrome_* handler must re-check the flag themselves — otherwise a crafted
# ui_event could add/probe/delete machines while the feature is off, breaking
# the flag-off-byte-identical posture.

def _orch_with_tripwire():
    orch = SimpleNamespace(history=SimpleNamespace(db=MagicMock()),
                           credential_manager=MagicMock())
    return orch


@pytest.fixture()
def flag_off(monkeypatch):
    monkeypatch.setattr(surface, "_enabled", lambda: False)


def _run(coro):
    import asyncio
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_components_flag_off_returns_disabled_no_form(flag_off):
    components = _run(surface.components(_orch_with_tripwire(), "u1", ["user"], {}))
    assert _find_fields(components) is None, "no add-machine form while disabled"


def test_render_flag_off_returns_disabled_no_form(flag_off):
    html = _run(surface.render(_orch_with_tripwire(), "u1", ["user"], {}))
    assert "Add a machine" not in html


@pytest.mark.parametrize("handler,payload", [
    ("_h_machine_add", {"fields": {"label": "x", "address": "h", "username": "u",
                                   "cred_type": "password", "password": "p"}}),
    ("_h_machine_probe", {"machine_id": "m1"}),
    ("_h_machine_delete", {"machine_id": "m1"}),
    ("_h_credential_set", {"machine_id": "m1",
                           "fields": {"cred_type": "password", "password": "p"}}),
    ("_h_credential_delete", {"machine_id": "m1"}),
    ("_h_machine_retrust", {"machine_id": "m1"}),
])
def test_handlers_flag_off_refuse_without_touching_state(flag_off, monkeypatch,
                                                         handler, payload):
    def _trip(*a, **k):
        raise AssertionError("flag-off handler must not reach machine state")
    for fn in ("create_machine", "get_machine", "delete_machine", "list_machines",
               "retrust_host_key", "audit_machine_event"):
        monkeypatch.setattr(surface.remote_machines, fn, _trip)
    orch = _orch_with_tripwire()
    orch.credential_manager.set_machine_credential.side_effect = _trip
    orch.credential_manager.delete_machine_credential.side_effect = _trip
    key, params, notice = _run(getattr(surface, handler)(orch, None, "u1", ["user"], payload))
    assert key == surface.SURFACE_KEY
    assert "disabled" in notice.lower()


# ── per-machine controls (T026): credential replace/remove + re-trust ─────────
#
# Every registered machine's card carries the credential-scoped controls next to
# Probe/Delete; the Re-trust control appears ONLY after a host_key_mismatch
# verdict (retrust is the one deliberate path that accepts a changed host key,
# FR-020 — offering it on a healthy machine would invite blind re-trust).

_ROW_OK = {"machine_id": "m1", "label": "dgx", "address": "10.0.0.5", "port": 22,
           "os_family": "linux", "role": "cluster", "last_verdict": "ok"}
_ROW_MISMATCH = {**_ROW_OK, "machine_id": "m2", "label": "edge",
                 "last_verdict": "host_key_mismatch"}


def _with_rows(monkeypatch, rows):
    monkeypatch.setattr(surface.remote_machines, "list_machines",
                        lambda db, user_id: [dict(r) for r in rows])
    return SimpleNamespace(history=SimpleNamespace(db=MagicMock()))


def _dicts(node):
    """Yield every dict in a component tree depth-first."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _dicts(value)


def test_web_cards_carry_machine_scoped_credential_controls(monkeypatch):
    html = _run(surface.render(_with_rows(monkeypatch, [_ROW_OK]), "u1", ["user"], {}))
    assert 'data-ui-action="chrome_machine_credential_set"' in html
    assert 'data-ui-action="chrome_machine_credential_delete"' in html
    assert '"machine_id":"m1"' in html
    assert 'data-ui-action="chrome_machine_retrust"' not in html  # verdict is ok


def test_web_retrust_control_appears_only_on_host_key_mismatch(monkeypatch):
    html = _run(surface.render(_with_rows(monkeypatch, [_ROW_OK, _ROW_MISMATCH]),
                               "u1", ["user"], {}))
    assert html.count('data-ui-action="chrome_machine_retrust"') == 1
    assert '"machine_id":"m2"' in html


def test_native_cards_carry_machine_scoped_credential_controls(monkeypatch):
    comps = _run(surface.components(_with_rows(monkeypatch, [_ROW_OK]), "u1", ["user"], {}))
    dicts = list(_dicts(comps))
    actions = {d.get("action") for d in dicts if isinstance(d.get("action"), str)}
    assert "chrome_machine_credential_delete" in actions
    assert "chrome_machine_retrust" not in actions  # verdict is ok
    forms = [d for d in dicts if d.get("submit_action") == "chrome_machine_credential_set"]
    assert len(forms) == 1
    assert forms[0].get("submit_payload") == {"machine_id": "m1"}
    # Same credential field names + visible_when markers as the add form, so the
    # same handler parsing (and client-side visibility) applies.
    names = {f["name"] for f in forms[0]["fields"]}
    assert names == {"cred_type", "private_key", "passphrase", "password"}


def test_native_retrust_control_appears_only_on_host_key_mismatch(monkeypatch):
    comps = _run(surface.components(_with_rows(monkeypatch, [_ROW_OK, _ROW_MISMATCH]),
                                    "u1", ["user"], {}))
    retrust = [d for d in _dicts(comps) if d.get("action") == "chrome_machine_retrust"]
    assert len(retrust) == 1
    assert retrust[0].get("payload") == {"machine_id": "m2"}
