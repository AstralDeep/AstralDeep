"""Tests for saving, showing, and clearing a TypeSafe key in
llm_config/typesafe_store.py: a rejected save never destroys a working stored key,
the key stays write-only, and its handlers stay isolated from the LLM first-run gate.
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from llm_config import typesafe_handlers  # noqa: E402
from llm_config.typesafe_handlers import (  # noqa: E402
    MAX_KEY_CHARS,
    PROBE_RATE_LIMIT,
    TypeSafeSaveError,
    clear_key,
    reset_probe_rate,
    save_key,
    validate_key,
)
from llm_config.typesafe_store import key_fingerprint  # noqa: E402
from orchestrator.projection_surfaces import llm as llm_surface  # noqa: E402

KEY = "ts_live_CANARY0000NOTAREALKEY000000"
OTHER_KEY = "ts_live_CANARY1111NOTAREALKEY111111"
USER = "settings-user"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_rate_limit():
    reset_probe_rate()
    yield
    reset_probe_rate()


async def _probe_ok(key: str) -> None:
    return None


def _probe_rejects(message: str = "TypeSafe rejected that key."):
    async def _probe(key: str) -> None:
        raise TypeSafeSaveError(message)

    return _probe


@pytest.mark.parametrize("bad", ["", "   ", None, 7, b"bytes"])
def test_an_empty_or_non_string_key_is_refused(bad) -> None:
    with pytest.raises(TypeSafeSaveError):
        validate_key(bad)


def test_an_oversized_key_is_refused() -> None:
    with pytest.raises(TypeSafeSaveError) as caught:
        validate_key("t" * (MAX_KEY_CHARS + 1))
    assert str(MAX_KEY_CHARS) in str(caught.value)


def test_a_key_with_a_space_is_refused_with_a_useful_message() -> None:
    with pytest.raises(TypeSafeSaveError) as caught:
        validate_key("ts_live_AAA BBB")
    assert "space" in str(caught.value).lower()


def test_surrounding_whitespace_is_stripped() -> None:
    assert validate_key("  " + KEY + "\n") == KEY


def test_a_validation_message_never_contains_the_key() -> None:
    for bad in ("ts_live_AAA BBB", "t" * (MAX_KEY_CHARS + 1)):
        try:
            validate_key(bad)
        except TypeSafeSaveError as error:
            assert bad not in str(error)


def test_a_successful_save_stores_the_key(typesafe_store) -> None:
    status, fingerprint = run(
        save_key(typesafe_store, USER, KEY, probe=_probe_ok)
    )
    assert status.name == "active"
    assert fingerprint == key_fingerprint(KEY)
    assert typesafe_store.get_key_sync(USER).api_key == KEY


def test_a_rejected_save_leaves_the_stored_key_untouched(typesafe_store) -> None:
    run(save_key(typesafe_store, USER, KEY, probe=_probe_ok))

    with pytest.raises(TypeSafeSaveError):
        run(save_key(typesafe_store, USER, OTHER_KEY, probe=_probe_rejects()))

    assert typesafe_store.get_key_sync(USER).api_key == KEY
    assert typesafe_store.status_sync(USER).name == "active"


def test_a_rejected_first_save_stores_nothing(typesafe_store) -> None:
    with pytest.raises(TypeSafeSaveError):
        run(save_key(typesafe_store, USER, KEY, probe=_probe_rejects()))
    assert typesafe_store.get_key_sync(USER) is None


def test_an_invalid_key_never_reaches_the_probe(typesafe_store) -> None:
    probed: list = []

    async def _probe(key: str) -> None:
        probed.append(key)

    with pytest.raises(TypeSafeSaveError):
        run(save_key(typesafe_store, USER, "", probe=_probe))
    assert probed == []


def test_a_successful_save_resets_the_users_circuit(typesafe_store) -> None:
    from orchestrator.typesafe_routing.budget import UserCircuit, circuit, set_circuit

    set_circuit(UserCircuit())
    circuit().record_auth_failure(USER, "0123456789ab")
    assert circuit().allows(USER, fingerprint="0123456789ab") is False

    run(save_key(typesafe_store, USER, KEY, probe=_probe_ok))

    assert circuit().allows(USER, fingerprint=key_fingerprint(KEY)) is True


def test_the_probe_is_rate_limited_per_user(typesafe_store) -> None:
    for _ in range(PROBE_RATE_LIMIT):
        run(save_key(typesafe_store, USER, KEY, probe=_probe_ok))
    with pytest.raises(TypeSafeSaveError) as caught:
        run(save_key(typesafe_store, USER, KEY, probe=_probe_ok))
    assert "wait" in str(caught.value).lower()


def test_the_rate_limit_is_per_user_not_global(typesafe_store) -> None:
    for _ in range(PROBE_RATE_LIMIT):
        run(save_key(typesafe_store, USER, KEY, probe=_probe_ok))
    run(save_key(typesafe_store, "another-user", KEY, probe=_probe_ok))


def test_clear_removes_the_key_and_is_idempotent(typesafe_store) -> None:
    run(save_key(typesafe_store, USER, KEY, probe=_probe_ok))
    assert run(clear_key(typesafe_store, USER)) is True
    assert typesafe_store.get_key_sync(USER) is None
    assert run(clear_key(typesafe_store, USER)) is False


def test_clear_frees_the_rate_limit(typesafe_store) -> None:
    for _ in range(PROBE_RATE_LIMIT):
        run(save_key(typesafe_store, USER, KEY, probe=_probe_ok))
    run(clear_key(typesafe_store, USER))
    run(save_key(typesafe_store, USER, KEY, probe=_probe_ok))


def test_a_save_is_audited_without_the_key(typesafe_store) -> None:
    recorder = MagicMock()
    recorder.record = AsyncMock()
    run(save_key(typesafe_store, USER, KEY, recorder=recorder, probe=_probe_ok))

    recorder.record.assert_awaited()
    event = recorder.record.await_args.args[0]
    rendered = repr(event)
    assert KEY not in rendered
    assert event.action_type == "typesafe_credential.saved"
    assert event.inputs_meta["key_fingerprint"] == key_fingerprint(KEY)


def test_a_clear_is_audited(typesafe_store) -> None:
    recorder = MagicMock()
    recorder.record = AsyncMock()
    run(save_key(typesafe_store, USER, KEY, probe=_probe_ok))
    run(clear_key(typesafe_store, USER, recorder=recorder))
    assert recorder.record.await_args.args[0].action_type == "typesafe_credential.cleared"


def test_clearing_nothing_records_nothing(typesafe_store) -> None:
    recorder = MagicMock()
    recorder.record = AsyncMock()
    run(clear_key(typesafe_store, USER, recorder=recorder))
    recorder.record.assert_not_awaited()


def test_an_audit_failure_never_breaks_a_save(typesafe_store) -> None:
    recorder = MagicMock()
    recorder.record = AsyncMock(side_effect=RuntimeError("audit is down"))
    status, _fingerprint = run(
        save_key(typesafe_store, USER, KEY, recorder=recorder, probe=_probe_ok)
    )
    assert status.name == "active"
    assert typesafe_store.get_key_sync(USER).api_key == KEY


def _orch(typesafe_store=None, data_sharing_store=None, llm_store=None):
    return SimpleNamespace(
        _llm_store=llm_store,
        _typesafe_store=typesafe_store,
        _data_sharing_store=data_sharing_store,
        ui_sessions={},
        audit_recorder=None,
    )


def _render(orch, params=None):
    return run(llm_surface.render(orch, USER, ["user"], params or {}))


def _components(orch, params=None):
    return run(llm_surface.components(orch, USER, ["user"], params or {}))


def test_the_section_renders_with_a_write_only_field(typesafe_store) -> None:
    html = _render(_orch(typesafe_store))
    assert "TypeSafe routing (optional)" in html
    assert 'type="password"' in html and 'name="typesafe_api_key"' in html
    assert 'value=""' in html
    assert 'data-ui-action="chrome_typesafe_save"' in html


def test_the_section_is_hidden_during_first_run(typesafe_store) -> None:
    html = _render(_orch(typesafe_store), {"first_run": True})
    assert "TypeSafe routing (optional)" not in html
    assert "chrome_typesafe_save" not in html


def test_remove_appears_only_when_a_key_is_stored(typesafe_store) -> None:
    assert "chrome_typesafe_clear" not in _render(_orch(typesafe_store))
    run(save_key(typesafe_store, USER, KEY, probe=_probe_ok))
    assert "chrome_typesafe_clear" in _render(_orch(typesafe_store))


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (None, "Not set"),
        ("valid", "Active"),
        ("rejected", "Key rejected on"),
        ("unavailable", "Temporarily unavailable"),
    ],
)
def test_the_status_line_reflects_the_stored_outcome(
    typesafe_store, outcome, expected
) -> None:
    if outcome is not None:
        run(save_key(typesafe_store, USER, KEY, probe=_probe_ok))
        if outcome != "valid":
            typesafe_store.record_outcome_sync(USER, outcome, key_fingerprint(KEY))
    html = _render(_orch(typesafe_store))
    assert expected in html


def test_the_key_never_appears_in_the_rendered_html(typesafe_store) -> None:
    run(save_key(typesafe_store, USER, KEY, probe=_probe_ok))
    html = _render(_orch(typesafe_store))
    assert KEY not in html
    assert key_fingerprint(KEY) not in html
    assert "Saved key hidden" in html


def test_the_key_never_appears_in_the_sdui_components(typesafe_store) -> None:
    run(save_key(typesafe_store, USER, KEY, probe=_probe_ok))
    rendered = repr(_components(_orch(typesafe_store)))
    assert KEY not in rendered
    assert key_fingerprint(KEY) not in rendered


def test_the_sdui_field_is_a_password_field(typesafe_store) -> None:
    components = _components(_orch(typesafe_store))
    forms = [c for c in components if c.get("type") == "param_picker"]
    fields = {f["name"]: f for f in forms[0]["fields"]}
    assert fields["typesafe_api_key"]["kind"] == "password"
    assert not fields["typesafe_api_key"].get("default")


def test_a_missing_store_renders_the_unset_state_rather_than_failing() -> None:
    html = _render(_orch(None))
    assert "Not set" in html


def test_the_surface_survives_a_store_that_raises() -> None:
    class Broken:
        async def status(self, user_id):
            raise RuntimeError("plane is unreachable")

    html = _render(_orch(Broken()))
    assert "Not set" in html


def test_the_handlers_are_not_allowed_while_the_llm_gate_is_closed() -> None:
    from orchestrator.chrome_events import _LLM_GATE_ALLOWED_ACTIONS

    assert "chrome_typesafe_save" not in _LLM_GATE_ALLOWED_ACTIONS
    assert "chrome_typesafe_clear" not in _LLM_GATE_ALLOWED_ACTIONS


def _handler_source(name: str) -> str:
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(llm_surface.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"handler {name} not found")


def test_the_save_handler_never_unlocks_the_first_run_gate() -> None:
    assert "unlock_after_save" not in _handler_source("_handle_typesafe_save")


def test_the_clear_handler_never_regates_the_user() -> None:
    assert "regate_after_clear" not in _handler_source("_handle_typesafe_clear")


def test_the_llm_save_handler_still_unlocks_the_gate() -> None:
    assert "unlock_after_save" in _handler_source("_handle_save")


def test_the_save_is_not_routed_to_an_executor_that_cannot_perform_it() -> None:
    from orchestrator.orchestrator import _LLM_CREDENTIAL_SAVE_ACTIONS

    assert "chrome_typesafe_save" not in _LLM_CREDENTIAL_SAVE_ACTIONS
    assert "save_key" in _handler_source("_handle_typesafe_save")


def test_the_probe_budget_is_generous_compared_with_the_turn_budget() -> None:
    from orchestrator.typesafe_routing.budget import TURN_BUDGET_SECONDS

    assert typesafe_handlers.PROBE_TIMEOUT_SECONDS > TURN_BUDGET_SECONDS
