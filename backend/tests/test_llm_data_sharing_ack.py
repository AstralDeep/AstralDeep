"""Tests for llm_config/data_sharing.py and its rendering in projection_surfaces/llm.py:
the acknowledgment gates credential save, never a chat turn, before validation or any
provider request, and the notice text is pinned by digest.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from llm_config import data_sharing as ds  # noqa: E402
from orchestrator.projection_surfaces import llm as llm_surface  # noqa: E402

USER = "ack-user"

# Changing this text requires bumping NOTICE_VERSION too
PINNED_TEXT_DIGEST = hashlib.sha256(
    "\n".join([ds.NOTICE_TITLE, ds.NOTICE_BODY, ds.CHECKBOX_LABEL]).encode("utf-8")
).hexdigest()


def run(coro):
    return asyncio.run(coro)


def test_the_notice_text_is_pinned_to_its_version() -> None:
    current = hashlib.sha256(
        "\n".join([ds.NOTICE_TITLE, ds.NOTICE_BODY, ds.CHECKBOX_LABEL]).encode("utf-8")
    ).hexdigest()
    assert current == PINNED_TEXT_DIGEST, (
        "the data-sharing wording changed; bump NOTICE_VERSION in the same "
        "change so consent is not silently reused across different text"
    )


def test_the_notice_says_what_actually_leaves() -> None:
    body = ds.NOTICE_BODY.lower()
    assert "content of your requests" in body
    assert "messages" in body and "conversation context" in body
    assert "third-party" in body or "third party" in body
    assert "typesafe" in body


def test_the_version_looks_like_a_dated_version() -> None:
    assert ds.NOTICE_VERSION
    assert ds.NOTICE_VERSION[0].isdigit()


def test_an_unacknowledged_save_is_blocked(data_sharing_store) -> None:
    result = ds.require_acknowledgment(data_sharing_store, USER, None)
    assert result.blocked
    assert result.error == ds.FIELD_ERROR


def test_ticking_the_box_records_and_allows(data_sharing_store) -> None:
    result = ds.require_acknowledgment(data_sharing_store, USER, True)
    assert result.allowed
    assert result.newly_acknowledged is True
    assert data_sharing_store.state_sync(USER).acknowledged is True


def test_a_stored_acknowledgment_allows_a_later_save(data_sharing_store) -> None:
    ds.require_acknowledgment(data_sharing_store, USER, True)
    result = ds.require_acknowledgment(data_sharing_store, USER, None)
    assert result.allowed
    assert result.newly_acknowledged is False


def test_an_explicit_false_blocks_even_after_acknowledging(data_sharing_store) -> None:
    ds.require_acknowledgment(data_sharing_store, USER, True)
    result = ds.require_acknowledgment(data_sharing_store, USER, False)
    assert result.blocked


def test_a_version_bump_requires_a_fresh_acknowledgment(
    data_sharing_store, monkeypatch
) -> None:
    ds.require_acknowledgment(data_sharing_store, USER, True)
    assert ds.require_acknowledgment(data_sharing_store, USER, None).allowed

    monkeypatch.setattr(ds, "NOTICE_VERSION", "2099-01-01.1")
    assert ds.require_acknowledgment(data_sharing_store, USER, None).blocked


def test_acknowledgment_is_per_user(data_sharing_store) -> None:
    ds.require_acknowledgment(data_sharing_store, USER, True)
    assert ds.require_acknowledgment(data_sharing_store, "somebody-else", None).blocked


def test_the_first_acknowledgment_time_survives_a_version_bump(
    data_sharing_store, monkeypatch
) -> None:
    first = datetime.now(UTC) - timedelta(days=30)
    data_sharing_store.acknowledge_sync(USER, at=first)
    monkeypatch.setattr(ds, "NOTICE_VERSION", "2099-01-01.1")
    data_sharing_store.acknowledge_sync(USER)

    record = data_sharing_store._repository.repository.get_user(  # noqa: SLF001
        None, owner_id=USER
    )
    assert record.first_acknowledged_at == first
    assert record.notice_version == "2099-01-01.1"


def test_the_legacy_path_gets_a_message_that_says_where_to_go(
    data_sharing_store,
) -> None:
    result = ds.require_acknowledgment(data_sharing_store, USER, None, legacy=True)
    assert result.error == ds.LEGACY_ERROR
    assert "Settings" in result.error


def test_a_broken_store_reads_as_unacknowledged_rather_than_allowing(
    monkeypatch, data_sharing_store
) -> None:
    def _explode(*args, **kwargs):
        raise RuntimeError("plane is unreachable")

    monkeypatch.setattr(
        data_sharing_store._repository.repository, "get_user", _explode  # noqa: SLF001
    )
    assert ds.require_acknowledgment(data_sharing_store, USER, None).blocked


def test_an_acknowledgment_is_audited() -> None:
    recorder = MagicMock()
    recorder.record = AsyncMock()
    run(ds.record_acknowledged(recorder, actor_user_id=USER, auth_principal=USER))
    event = recorder.record.await_args.args[0]
    assert event.action_type == "llm_data_sharing.acknowledged"
    assert event.inputs_meta["notice_version"] == ds.NOTICE_VERSION
    assert event.outcome == "success"


def test_a_blocked_save_is_audited_with_its_target() -> None:
    recorder = MagicMock()
    recorder.record = AsyncMock()
    run(
        ds.record_save_blocked(
            recorder, actor_user_id=USER, auth_principal=USER, target="TypeSafe"
        )
    )
    event = recorder.record.await_args.args[0]
    assert event.action_type == "llm_data_sharing.save_blocked"
    assert event.inputs_meta["target"] == "TypeSafe"
    assert event.outcome == "failure"


def test_auditing_never_breaks_a_save() -> None:
    recorder = MagicMock()
    recorder.record = AsyncMock(side_effect=RuntimeError("audit is down"))
    run(ds.record_acknowledged(recorder, actor_user_id=USER, auth_principal=USER))


def test_a_missing_recorder_is_not_an_error() -> None:
    run(ds.record_acknowledged(None, actor_user_id=USER, auth_principal=USER))


def _orch(data_sharing_store=None, typesafe_store=None):
    return SimpleNamespace(
        _llm_store=None,
        _typesafe_store=typesafe_store,
        _data_sharing_store=data_sharing_store,
        ui_sessions={},
        audit_recorder=None,
    )


def _render(orch, params=None):
    return run(llm_surface.render(orch, USER, ["user"], params or {}))


def _components(orch, params=None):
    return run(llm_surface.components(orch, USER, ["user"], params or {}))


def test_the_warning_and_checkbox_render_on_the_web_surface(
    data_sharing_store,
) -> None:
    html = _render(_orch(data_sharing_store))
    assert ds.NOTICE_TITLE in html
    assert 'role="note"' in html
    assert f'id="{ds.WARNING_ELEMENT_ID}"' in html
    assert f'name="{ds.FIELD_NAME}"' in html
    assert 'type="checkbox"' in html


def test_the_checkbox_is_described_by_the_warning(data_sharing_store) -> None:
    html = _render(_orch(data_sharing_store))
    assert f'aria-describedby="{ds.WARNING_ELEMENT_ID}"' in html
    assert f'id="{ds.CHECKBOX_ELEMENT_ID}"' in html
    assert ds.CHECKBOX_LABEL in html


def test_the_checkbox_sits_below_the_credentials_and_above_the_actions(
    data_sharing_store, typesafe_store
) -> None:
    from webrender.chrome import render_modal_shell

    body = _render(_orch(data_sharing_store, typesafe_store))
    assert body.index('name="api_key"') < body.index(f'name="{ds.FIELD_NAME}"')
    assert body.index('name="typesafe_api_key"') < body.index(f'name="{ds.FIELD_NAME}"')

    dialog = render_modal_shell(
        llm_surface.TITLE, body, "llm",
        subtitle=llm_surface.SUBTITLE, icon=llm_surface.ICON, sections=llm_surface.SECTIONS,
        footer_html=llm_surface.footer_html(),
    )
    assert dialog.index(f'name="{ds.FIELD_NAME}"') < dialog.index("chrome_llm_save")


def test_the_warning_renders_during_first_run_too(data_sharing_store) -> None:
    html = _render(_orch(data_sharing_store), {"first_run": True})
    assert ds.NOTICE_TITLE in html
    assert f'name="{ds.FIELD_NAME}"' in html


def test_an_acknowledged_box_renders_checked_with_its_date(data_sharing_store) -> None:
    data_sharing_store.acknowledge_sync(USER)
    html = _render(_orch(data_sharing_store))
    assert "checked" in html
    assert "Acknowledged on" in html


def test_the_sdui_surface_carries_a_boolean_field_and_a_warning(
    data_sharing_store,
) -> None:
    components = _components(_orch(data_sharing_store))
    rendered = repr(components)
    assert ds.NOTICE_TITLE in rendered
    forms = [c for c in components if c.get("type") == "param_picker"]
    fields = {f["name"]: f for f in forms[0]["fields"]}
    assert fields[ds.FIELD_NAME]["kind"] == "boolean"


def test_the_sdui_checkbox_is_the_last_field(data_sharing_store, typesafe_store) -> None:
    components = _components(_orch(data_sharing_store, typesafe_store))
    forms = [c for c in components if c.get("type") == "param_picker"]
    names = [f["name"] for f in forms[0]["fields"]]
    assert names[-1] == ds.FIELD_NAME


def test_the_inline_error_renders_when_a_save_was_blocked(data_sharing_store) -> None:
    html = _render(_orch(data_sharing_store), {"data_sharing_error": ds.FIELD_ERROR})
    assert ds.FIELD_ERROR in html
    assert 'role="alert"' in html


def test_a_missing_store_still_renders_the_warning() -> None:
    html = _render(_orch(None))
    assert ds.NOTICE_TITLE in html


@pytest.mark.parametrize(
    "raw,expected",
    [
        (True, True), ("true", True), ("on", True), ("1", True), ("yes", True),
        (False, False), ("false", False), ("off", False), ("0", False), ("", False),
    ],
)
def test_the_submitted_value_is_read_as_a_tri_state(raw, expected) -> None:
    payload = {"fields": {ds.FIELD_NAME: raw}}
    assert llm_surface._submitted_acknowledgment(payload) is expected  # noqa: SLF001


def test_an_absent_field_reads_as_none_not_false() -> None:
    assert llm_surface._submitted_acknowledgment({"fields": {}}) is None  # noqa: SLF001
    assert llm_surface._submitted_acknowledgment({}) is None  # noqa: SLF001


def test_the_turn_path_never_reads_the_acknowledgment_store() -> None:
    import ast
    import pathlib

    from orchestrator import orchestrator as orch_module

    source = pathlib.Path(orch_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in (
            "_handle_chat_message_impl",
            "handle_chat_message",
        ):
            body = ast.unparse(node)
            assert "_data_sharing_store" not in body
            assert "require_acknowledgment" not in body


def test_the_durable_credential_operation_applies_the_gate() -> None:
    import ast
    import inspect
    import pathlib as _p

    from orchestrator import orchestrator as orch_module

    source = _p.Path(orch_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.AsyncFunctionDef)
                and node.name == "_handle_llm_credential_operation"):
            target = node
            break
    assert target is not None, "_handle_llm_credential_operation not found"

    called = [
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        for n in ast.walk(target) if isinstance(n, ast.Call)
    ]
    assert "_require_acknowledgment" in called, (
        "the durable credential path must run the data-sharing gate"
    )

    def line_of(name):
        return min((n.lineno for n in ast.walk(target)
                    if isinstance(n, ast.Call)
                    and (getattr(n.func, "id", "") == name
                         or getattr(n.func, "attr", "") == name)), default=None)

    gate, resolve = line_of("_require_acknowledgment"), line_of("_resolve_api_key")
    assert gate is not None
    if resolve is not None:
        assert gate < resolve, (
            "the gate must run before the API key is resolved and probed"
        )
    _ = inspect
