"""Tests for the web client's source contract (AstralProjection's client.js, shell.html,
web_auth.py via resources.py): auth-required recovery, timeline read-only guards, no
local-credential UI, and status reducers.
"""

import re
from pathlib import Path

import pytest
from astralprojection.resources import static_path, template_path

BACKEND = Path(__file__).resolve().parent.parent
CLIENT_JS = static_path("client.js")
SHELL_HTML = template_path("shell.html")
WEB_AUTH_PY = BACKEND / "orchestrator" / "web_auth.py"


def _read(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    assert len(src) > 500, f"{path} unexpectedly small — wrong file?"
    return src


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s)


def _case_block(src: str, name: str) -> str:
    marker = f'case "{name}":'
    assert marker in src, f"client.js has no {marker} handler"
    start = src.index(marker)
    nxt = src.find('case "', start + len(marker))
    assert nxt != -1, f"could not bound the {name} case"
    return src[start:nxt]


def _js_function(src: str, name: str) -> str:
    sig = f"function {name}("
    assert sig in src, f"client.js has no function {name}"
    start = src.index(sig)
    depth = 0
    for i in range(src.index("{", start), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unbalanced braces extracting function {name}")


def _py_function(src: str, name: str) -> str:
    sig = f"def {name}("
    assert sig in src, f"web_auth.py has no {sig}"
    start = src.index(sig)
    nxt = re.compile(r"\n(?:def |async def |class |@)").search(src, start + 1)
    return src[start : nxt.start()] if nxt else src[start:]


@pytest.fixture(scope="module")
def client_js() -> str:
    return _read(CLIENT_JS)


@pytest.fixture(scope="module")
def shell_html() -> str:
    return _read(SHELL_HTML)


@pytest.fixture(scope="module")
def web_auth_src() -> str:
    return _read(WEB_AUTH_PY)


def test_no_dev_token_literal_in_executable_code(client_js):
    for lineno, line in enumerate(client_js.splitlines(), 1):
        if "dev-token" in line:
            assert line.lstrip().startswith("//"), (
                f"client.js:{lineno} carries 'dev-token' outside a comment: {line.strip()!r}"
            )
    assert '"dev-token"' not in client_js, "dev-token JS string literal resurfaced"
    assert not re.search(r"""\|\|\s*['"]dev-token['"]""", client_js)


def test_auth_required_case_refreshes_then_falls_back_to_login(client_js):
    block = _norm(_case_block(client_js, "auth_required"))
    assert "refreshToken(" in block, "auth_required must try a silent session refresh"
    assert "gotoLogin()" in block, "auth_required must fall back to interactive login"
    assert "authRetried" in block
    assert "else { gotoLogin(); }" in block
    assert block.index("refreshToken(") < block.index("gotoLogin()")
    assert 'type: "register_ui"' in block and "token: token" in block


def test_gotologin_builds_next_from_current_location(client_js):
    fn = _norm(_js_function(client_js, "gotoLogin"))
    assert "encodeURIComponent(location.pathname + location.search)" in fn
    assert '"/auth/login?next=" + next' in fn
    assert "location.href" in fn


def _astral_action_handler(client_js: str) -> str:
    start = client_js.index('closest(".astral-action")')
    end = client_js.index("document.addEventListener", start)
    return client_js[start:end]


def test_astral_action_click_injects_component_identity(client_js):
    block = _norm(_astral_action_handler(client_js))
    assert 'closest("[data-component-id]")' in block, (
        "action clicks must resolve their enclosing workspace component"
    )
    assert 'payload.component_id = compHost.getAttribute("data-component-id")' in block
    assert "!payload.component_id" in block
    assert "payload.chat_id = activeChatId" in block


def test_apply_upsert_defers_dom_in_timeline_mode(client_js):
    fn = _norm(_js_function(client_js, "applyUpsert"))
    guard = fn.index("if (timelineMode)")
    indicator = fn.index("Live workspace updated")
    assert "Back to live" in fn, "indicator must point users back to the live view"
    ret = fn.index("return;", indicator)
    ops = fn.index("msg.ops")
    assert guard < indicator < ret < ops, (
        "applyUpsert must show the live-has-moved-on indicator and bail before "
        "touching the DOM while viewing history"
    )
    assert "setStatus(" in fn[guard:ret]


def test_canvas_actions_inert_in_timeline_mode(client_js):
    block = _norm(_astral_action_handler(client_js))
    guard = block.index("if (timelineMode && compHost && act")
    assert 'act.indexOf("chrome_") !== 0' in block
    assert 'setStatus("Read-only history view' in block
    ret = block.index("return;", guard)
    dispatch = block.index("if (act) action(act, payload)")
    assert guard < ret < dispatch, (
        "the timeline guard must return before dispatching the ui_event"
    )


def test_pagination_inert_in_timeline_mode(client_js):
    assert client_js.count("Read-only history view") >= 3
    for name in ("paginate", "paginateSize"):
        fn = _norm(_js_function(client_js, name))
        assert "if (timelineMode)" in fn, f"{name} lacks the timeline guard"
        assert "Read-only history view" in fn
        assert fn.index("Read-only history view") < fn.index('action("table_paginate"')
        assert "component_id: paginateComponentId(el)" in fn


def test_register_ui_resumed_semantics(client_js):
    assert "var serverResumed = (window.__ASTRAL_RESUMED__ !== false);" in client_js, (
        "serverResumed must derive from the shell-injected window.__ASTRAL_RESUMED__"
    )
    fn = _norm(_js_function(client_js, "connect"))
    assert "resumed: firstConnect ? serverResumed : true" in fn, (
        "only the first register_ui of a page load may report resumed=false"
    )
    assert fn.index("resumed: firstConnect ? serverResumed : true") < fn.index(
        "firstConnect = false;"
    )


def test_chat_deleted_clears_canvas_when_active(client_js):
    block = _norm(_case_block(client_js, "chat_deleted"))
    assert "data.chat_id === activeChatId" in block, (
        "chat_deleted must only act on the currently active chat"
    )
    assert "activeChatId = null" in block
    assert 'setHTML(canvas, "")' in block, "deleting the active chat must clear the canvas"
    assert "timelineMode = false" in block
    assert "This chat was deleted." in block


def test_reconnect_refetches_session_before_register(client_js):
    fn = _norm(_js_function(client_js, "connect"))
    onclose = fn[fn.index("ws.onclose") :]
    assert "refreshToken(false, function () { connect(); })" in onclose
    assert "attempts <= 10" in onclose
    rt = _norm(_js_function(client_js, "refreshToken"))
    assert 'fetch(API_URL + "/auth/session"' in rt
    assert '"same-origin"' in rt
    assert "j.authenticated && j.access_token" in rt
    assert "token = j.access_token" in rt


def test_full_shell_load_prefers_current_server_session_over_prior_tab_token(client_js):
    bootstrap = re.search(r"var token = ([^;]+);", client_js)
    assert bootstrap is not None
    expression = bootstrap.group(1)
    assert expression.index("window.__ASTRAL_TOKEN__") < expression.index(
        "sessionStorage.getItem(TOKEN_KEY)"
    )


def test_definitive_sign_out_clears_cached_token_and_account_marker(client_js):
    start = client_js.index("The local endpoint invalidates the server session")
    end = client_js.index("// ---- stacked-shell chrome", start)
    handler = client_js[start:end]
    assert "token = \"\"" in handler
    assert "sessionStorage.removeItem(TOKEN_KEY)" in handler
    assert "sessionStorage.removeItem(ACCOUNT_SESSION_KEY)" in handler
    assert 'clearActiveChatLocator("definitive_sign_out", activeChatId)' in handler


def test_shell_injects_token_and_resumed_placeholders(shell_html):
    assert 'window.__ASTRAL_TOKEN__ = "%%ASTRAL_TOKEN%%"' in shell_html
    assert "window.__ASTRAL_RESUMED__ = %%ASTRAL_RESUMED%%" in shell_html
    m = re.search(r"<script[^>]*>([^<]*__ASTRAL_TOKEN__[^<]*)</script>", shell_html)
    assert m is not None, "no inline bootstrap script found"
    assert "__ASTRAL_RESUMED__" in m.group(1)
    assert shell_html.index("%%ASTRAL_RESUMED%%") < shell_html.index(
        'src="/static/client.js'
    )


FORBIDDEN_FR010 = ("remember me", "remember-me", "biometric", "webauthn")


@pytest.mark.parametrize(
    "path", [CLIENT_JS, SHELL_HTML, WEB_AUTH_PY], ids=lambda p: p.name
)
def test_fr010_no_local_credential_ui(path):
    low = _read(path).lower()
    for term in FORBIDDEN_FR010:
        assert term not in low, f"{path.name} contains forbidden FR-010 term {term!r}"


def test_fr010_error_pages_offer_no_local_credential_capture(web_auth_src):
    no_access = _py_function(web_auth_src, "_no_access_page")
    error_page = _py_function(web_auth_src, "_error_page")
    assert "No access" in no_access
    assert "Sign-in problem" in error_page
    for name, fn in (("_no_access_page", no_access), ("_error_page", error_page)):
        low = fn.lower()
        for term in FORBIDDEN_FR010:
            assert term not in low, f"{name} contains forbidden FR-010 term {term!r}"
        assert "/auth/login" in fn, f"{name} must route recovery through /auth/login"
        assert "<input" not in low and "<form" not in low, (
            f"{name} must not render local credential inputs"
        )


def test_operation_status_reducer_is_scoped_monotonic_and_first_terminal(client_js):
    fn = _norm(_js_function(client_js, "reduceOperationStatus"))
    assert "scopedStatusMatches(frame)" in fn
    assert "operationStatusById[frame.operation_id]" in fn
    assert "frame.sequence <= current.sequence" in fn
    assert "current.terminal" in fn
    assert "frame.terminal !== expected[0]" in fn
    assert "frame.retryable !== expected[1]" in fn
    assert 'if (frame.state === "completed")' in fn
    assert "restoreActiveStatusOrClear([operationOwner, submissionOwner])" in fn
    assert 'setStatus(visible, false, "operation-error:" + frame.operation_id)' in fn
    active = _norm(_js_function(client_js, "newestActiveOperationStatus"))
    assert "candidate.terminal || !scopedStatusMatches(candidate)" in active
    assert "!operationStatusShowsActivity(candidate)" in active
    restore = _norm(_js_function(client_js, "restoreActiveStatusOrClear"))
    assert "newestVisibleLocalSubmission()" in restore
    assert '"operation-submission:" + local.request_generation' in restore
    block = _norm(_case_block(client_js, "operation_status"))
    assert "reduceOperationStatus(data)" in block


def test_late_legacy_chat_loaded_cannot_resurrect_completed_hydration(client_js):
    block = _norm(_case_block(client_js, "chat_loaded"))
    assert "requestState && !requestState.snapshotApplied" in block
    assert '"Restoring conversation…", true, "operation-submission:" + requestState.generation' in block


def test_generic_actions_create_local_operation_identity_before_send(client_js):
    begin = _norm(_js_function(client_js, "beginOperationSubmission"))
    assert "randomUuid4()" in begin
    assert "body.submission_id = submissionId" in begin
    assert "body.request_generation = requestGeneration" in begin
    assert 'state: "submitting"' in begin
    assert 'label: "Submitting…"' in begin
    assert "shows_status: exposeStatus !== false" in begin
    assert "operationSubmissionByGeneration[requestGeneration] = local" in begin
    assert "operationSubmissionById[submissionId] = local" in begin
    assert (
        'setStatus(local.label, true, "operation-submission:" + requestGeneration)'
        in begin
    )

    action = _norm(_js_function(client_js, "action"))
    assert "beginOperationSubmission(name, payload, suppliedGeneration, exposeStatus)" in action
    assert "submission_id: submission.submissionId" in action
    assert "request_generation: submission.requestGeneration" in action
    assert "payload: submission.payload" in action

    chat = _norm(_js_function(client_js, "sendChat"))
    assert "if (!isSocketReady())" in chat
    assert "queueChatSend(message, ready)" in chat
    assert "doSendChat(message, ready)" in chat

    dispatch = _norm(_js_function(client_js, "doSendChat"))
    assert 'beginOperationSubmission("chat_message", payload, requestState.generation)' in dispatch
    assert "submission_id: submission.submissionId" in dispatch

    connect = _norm(_js_function(client_js, "connect"))
    assert 'action("get_history", {}, false)' in connect
    assert 'action("watch_task", { task_id: tid }, false)' in connect


def test_surface_status_and_refusal_correlate_to_local_submission(client_js):
    scope = _norm(_js_function(client_js, "scopedStatusMatches"))
    assert "operationSubmissionByGeneration[frame.request_generation]" in scope
    assert "if (pending) return frame.chat_id == null || frame.chat_id === activeChatId" in scope

    refusal = _norm(_js_function(client_js, "reduceAdmissionRefusal"))
    assert "frame.accepted !== false" in refusal
    assert "operationSubmissionById[frame.submission_id]" in refusal
    assert "finishOperationSubmission(local.request_generation)" in refusal
    assert (
        'setStatus(errorMessage(frame), false, "operation-error:" + frame.submission_id)'
        in refusal
    )

    block = _norm(_case_block(client_js, "error"))
    assert "reduceAdmissionRefusal(data)" in block


def test_surface_operation_scope_does_not_require_an_active_chat(client_js):
    fn = _norm(_js_function(client_js, "scopedStatusMatches"))
    assert "frame.connection_generation !== connectionGeneration" in fn
    assert "frame.request_generation !== requestState.generation" in fn
    assert "frame.chat_id == null ||" in fn


def test_agent_lifecycle_reducer_uses_lexicographic_pair_and_visible_fallback(client_js):
    fn = _norm(_js_function(client_js, "reduceAgentLifecycle"))
    assert "agentLifecycleById[frame.agent_id]" in fn
    assert "frame.lifecycle_generation < current.lifecycle_generation" in fn
    assert "frame.lifecycle_generation === current.lifecycle_generation" in fn
    assert "frame.state_revision <= current.state_revision" in fn
    assert "renderAgentLifecycle(frame)" in fn
    render = _norm(_js_function(client_js, "renderAgentLifecycle"))
    assert 'document.querySelectorAll("[data-agent-id]")' in render
    assert 'badge.setAttribute("role", "status")' in render
    assert 'badge.setAttribute("aria-live", "polite")' in render
    assert "badge.textContent = frame.label" in render
    assert "if (!matched)" in render
    assert "setStatus(frame.label)" in render
    assert 'showToast(frame.label, frame.state === "failed" ? "error" : "info")' in render
    assert "isCanonicalUuid4(frame.revision_id)" in fn
    assert "isCanonicalUuid4(frame.runtime_instance_id)" in fn
    assert "reasonCodes[frame.reason_code]" in fn
    block = _norm(_case_block(client_js, "agent_lifecycle"))
    assert "reduceAgentLifecycle(data)" in block


def test_status_projections_are_cleared_on_account_change_or_sign_out(client_js):
    fn = _norm(_js_function(client_js, "clearCommittedConversationView"))
    assert "operationStatusById = Object.create(null)" in fn
    assert "operationSubmissionByGeneration = Object.create(null)" in fn
    assert "operationSubmissionById = Object.create(null)" in fn
    assert "agentLifecycleById = Object.create(null)" in fn


def test_author_only_browser_explicitly_ignores_host_control_frames(client_js):
    no_op_cases = re.compile(
        r'case "agent_host_inventory_reconciled":\s*'
        r'case "agent_host_registration_refused":\s*'
        r'case "agent_host_registered":\s*'
        r'// host-only; the browser is author-only\s*'
        r'case "system_config":'
    )
    assert no_op_cases.search(client_js)
