"""Spec 060 browser role/name/state/focus contracts for changed controls."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from webrender.chrome.surfaces import agents, authoring


CLIENT_JS = Path(__file__).resolve().parents[2] / "webrender" / "static" / "client.js"


def _js_function(source: str, name: str) -> str:
    signature = f"function {name}("
    start = source.index(signature)
    depth = 0
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unbalanced JavaScript function {name}")


class _Controls(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.controls: list[dict[str, str | None]] = []
        self._button: dict[str, str | None] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag in {"button", "input"}:
            control = {"tag": tag, "text": "", **attributes}
            self.controls.append(control)
            if tag == "button":
                self._button = control

    def handle_data(self, data: str) -> None:
        if self._button is not None:
            self._button["text"] = str(self._button["text"] or "") + data

    def handle_endtag(self, tag: str) -> None:
        if tag == "button":
            self._button = None


def _controls(html: str) -> list[dict[str, str | None]]:
    parser = _Controls()
    parser.feed(html)
    return parser.controls


def test_application_status_exposes_role_name_live_state_and_atomic_updates() -> None:
    source = CLIENT_JS.read_text(encoding="utf-8")
    configure = _js_function(source, "configureStatusElement")
    update = _js_function(source, "setStatus")

    assert 'node.setAttribute("role", "status")' in configure
    assert 'node.setAttribute("aria-label", "Application status")' in configure
    assert 'node.setAttribute("aria-live", "polite")' in configure
    assert 'node.setAttribute("aria-atomic", "true")' in configure
    assert 'node.setAttribute("aria-busy", "false")' in configure
    assert 'statusEl.setAttribute("aria-busy", busy === true ? "true" : "false")' in update
    assert '"data-status-state"' in update

    operation = _js_function(source, "reduceOperationStatus")
    assert 'if (frame.state === "completed")' in operation
    assert 'setStatus(visible, false, "operation-error:" + frame.operation_id)' in operation
    assert "restoreActiveStatusOrClear([operationOwner, submissionOwner])" in operation
    active = _js_function(source, "newestActiveOperationStatus")
    assert "candidate.terminal || !scopedStatusMatches(candidate)" in active
    assert "!operationStatusShowsActivity(candidate)" in active
    restore = _js_function(source, "restoreActiveStatusOrClear")
    assert "newestVisibleLocalSubmission()" in restore
    assert '"operation-submission:" + local.request_generation' in restore


def test_lifecycle_status_exposes_a_stable_name_role_and_busy_state() -> None:
    source = CLIENT_JS.read_text(encoding="utf-8")
    render = _js_function(source, "renderAgentLifecycle")

    assert 'badge.setAttribute("role", "status")' in render
    assert 'frame.agent_id + " lifecycle status"' in render
    assert 'badge.setAttribute("aria-live", "polite")' in render
    assert 'badge.setAttribute("aria-atomic", "true")' in render
    assert 'frame.state === "starting" || frame.state === "updating"' in render
    assert "badge.textContent = frame.label" in render


def test_changed_authoring_actions_are_named_native_buttons_with_focus_behavior() -> None:
    html = authoring._phase_actions("draft-1", "analyze", 7)
    buttons = _controls(html)

    assert buttons
    for button in buttons:
        assert button["tag"] == "button"
        assert button.get("type") == "button"
        assert str(button.get("text") or "").strip()
        assert button.get("data-ui-action")
        assert button.get("disabled") is None
        assert button.get("tabindex") != "-1"


def test_authoring_submission_state_stays_named_and_focusable_while_guarded() -> None:
    source = CLIENT_JS.read_text(encoding="utf-8")
    pending = _js_function(source, "beginAuthoringControlPending")
    clear = _js_function(source, "clearAuthoringControlPending")

    assert 'el.setAttribute("aria-busy", "true")' in pending
    assert 'el.setAttribute("aria-disabled", "true")' in pending
    assert 'el.setAttribute("data-control-state", "submitting")' in pending
    assert ".disabled" not in pending
    assert "pendingAuthoringControl ||" in pending
    assert "setTimeout(clearAuthoringControlPending, 10000)" in pending
    assert 'setAttribute("aria-busy", "false")' in clear
    assert 'setAttribute("aria-disabled", "false")' in clear


def test_permission_switch_has_native_role_stable_name_state_and_tab_focus() -> None:
    html = agents._switch(
        "tools:read",
        checked=True,
        extra_cls="astral-perm-master",
        aria="Enable all Read tools",
    )
    [checkbox] = _controls(html)

    assert checkbox["tag"] == "input"
    assert checkbox.get("type") == "checkbox"
    assert checkbox.get("aria-label") == "Enable all Read tools"
    assert "checked" in checkbox
    assert checkbox.get("disabled") is None
    assert checkbox.get("tabindex") != "-1"
