"""Tests computer tool dispatch, workspace boundaries, and Kernel approval requirements using a registered macOS host."""

import base64
import io
import json
import time

import pytest

from orchestrator import computer_grants as cg
from orchestrator import hitl_confirmation as hc
from orchestrator.computer_use_policy import SCOPES
from tests.test_computer_use_076 import _descriptor, _FakeOrch
from tests.test_hitl_public_research import _dispatch, runtime as runtime  # noqa: F401
from tests.test_hitl_task_continuation import _registered_controller, task_runtime as task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator  # noqa: F401

OWNER, CHAT = "owner-a", "chat-a"
ROOT = "/Users/sam/Projects/x"
TEXTEDIT = {"hwnd": 42, "title": "Untitled", "process": "com.apple.TextEdit", "bundle_id": "com.apple.TextEdit",
            "rect": [0, 0, 800, 600], "focused": True}
NOTES = {"hwnd": 43, "title": "Groceries", "process": "com.apple.Notes", "bundle_id": "com.apple.Notes",
         "rect": [0, 0, 800, 600], "focused": False}
SAFARI = {"hwnd": 44, "title": "Bank", "process": "com.apple.Safari", "bundle_id": "com.apple.Safari",
          "rect": [0, 0, 800, 600], "focused": False}


async def _mac(task_runtime, windows=(TEXTEDIT, NOTES, SAFARI)):
    orch, ws, *_ = task_runtime
    if not hasattr(orch, "_connection_contexts") or id(ws) not in orch._connection_contexts:
        _registered_controller(orch, ws)
    fake = _FakeOrch()
    host, _ = fake.computer_hosts.register(OWNER, object(), _descriptor(
        platform="macos", name="Mac", workspace_roots=[{"name": "x", "path": ROOT}]))
    fake.ui_sessions[ws] = {"sub": OWNER}
    orch.computer_hosts = fake.computer_hosts
    orch.computer_sessions = fake.computer_sessions
    session = await fake.computer_sessions.start(OWNER, host, ws, CHAT, wait_for_ack=False)
    session.remember_windows([dict(w) for w in windows])
    orch.agents["computer-use-1"] = object()
    orch.tool_permissions.get_tool_scope_map.return_value = dict(SCOPES)
    return session, host


async def _call(task_runtime, tool, **args):
    orch, ws, *_ = task_runtime
    return await _dispatch((orch, ws), tool=tool, args=args, agent="computer-use-1")


def _jpeg(width=1280, height=720):
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), (20, 20, 20)).save(out, "JPEG")
    return base64.b64encode(out.getvalue()).decode("ascii")


def test_crop_refuses_off_screen_targets_corrupt_or_oversized_images():
    from orchestrator.computer_sessions import ComputerSession

    session = ComputerSession("s", OWNER, "h", "Mac", 1, "d", "Mac", CHAT)
    assert session.pointer_crop([(1, 1)]) is None
    session.remember_screenshot(_jpeg(64, 64), "image/jpeg", 64, 64, 0)
    assert session.pointer_crop([(500, 500)]) is None and session.pointer_crop([]) is None
    small = session.pointer_crop([(10, 10)])
    from PIL import Image

    assert Image.open(io.BytesIO(base64.b64decode(small))).size == (64, 64)
    session.remember_screenshot(base64.b64encode(b"not an image").decode(), "image/jpeg", 1, 1, 0)
    assert session.pointer_crop([(0, 0)]) is None
    session.remember_screenshot("@@@", "image/jpeg", 1, 1, 0)
    assert session.screenshot_cache is None
    session.remember_screenshot("A" * (5 * 1024 * 1024), "image/jpeg", 1, 1, 0)
    assert session.screenshot_cache is None


@pytest.mark.parametrize("cwd", ["/tmp", "/Users/riley/AstralTest", "~/Documents", "notes"])
async def test_a_command_folder_outside_every_shared_root_is_refused_before_a_card(task_runtime, cwd):
    orch, ws, effects, *_ = task_runtime
    await _mac(task_runtime)
    response = await _call(task_runtime, "run_command", argv=["wc", "a"], cwd=cwd)
    assert response.result is None and not response.ui_components
    assert response.error["retryable"] is False and "x = " + ROOT in response.error["message"]
    assert response.error["message"].startswith(("cwd_outside_workspace:", "out_of_range:"))
    assert not effects
    alerts = [c.args[1] for c in orch.send_ui_render.await_args_list]
    assert any(item.get("variant") == "error" for batch in alerts for item in batch)


async def test_browser_navigation_to_a_refused_url_never_gets_a_card(task_runtime):
    orch, *_ = task_runtime
    await _mac(task_runtime)
    response = await _call(task_runtime, "browser_navigate", url="javascript:alert(1)")
    assert response.error and "url_rejected" in response.error["message"]
    assert "confirmation_required" not in json.dumps(response.result)
    assert not [c for c in (response.ui_components or []) if c.get("type") == "card"]


def test_grant_events_use_the_store_audit_names():
    assert (cg.EVENT_CREATED, cg.EVENT_USED) == ("computer_grant.created", "computer_grant.used")


async def test_end_session_is_a_de_escalation_that_runs_without_a_card_and_is_audited(task_runtime):
    orch, ws, effects, *_ = task_runtime
    await _mac(task_runtime)
    orch._source_bound_de_escalations = {("computer-use-1", "end_session")}
    response = await _call(task_runtime, "end_session")
    assert response.error is None and response.result["_data"]["ok"]
    assert [tool for tool, _args in effects] == ["end_session"]
    events = [call.args[0] for call in orch.audit_recorder.record.await_args_list]
    assert [event.action_type for event in events] == ["hitl.de_escalation"]
    assert events[0].inputs_meta["risks"] == ["de_escalation"] and events[0].conversation_id == CHAT
    resume = await _call(task_runtime, "resume_session")
    assert resume.error["code"] == "source_bound_approval_required"
    assert not resume.ui_components and len(effects) == 1


async def test_end_session_without_the_host_declaration_keeps_its_review(task_runtime):
    orch, ws, effects, *_ = task_runtime
    await _mac(task_runtime)
    response = await _call(task_runtime, "end_session")
    assert response.error["code"] == "source_bound_approval_required"
    assert not response.ui_components and not effects


async def test_de_escalation_audit_failure_does_not_block_ending(task_runtime):
    orch, ws, effects, *_ = task_runtime
    await _mac(task_runtime)
    orch._source_bound_de_escalations = {("computer-use-1", "end_session")}
    orch.audit_recorder.record.side_effect = RuntimeError("audit down")
    response = await _call(task_runtime, "end_session")
    assert response.result["_data"]["ok"] and [tool for tool, _args in effects] == ["end_session"]


async def test_de_escalation_audit_records_unencodable_arguments_by_an_empty_digest(task_runtime):
    import hashlib

    orch, *_ = task_runtime
    await hc.audit_de_escalation(orch, OWNER, CHAT, "computer-use-1", "end_session", {"x": "y" * 70000})
    event = orch.audit_recorder.record.await_args.args[0]
    assert event.action_type == "hitl.de_escalation"
    assert event.inputs_meta["arguments_sha256"] == hashlib.sha256(b"{}").hexdigest()


@pytest.mark.parametrize("tool,args", [("type_text", {"text": "x", "hwnd": 42}), ("start_session", {}),
                                       ("run_command", {"argv": ["wc", "a"], "cwd": ROOT})])
async def test_an_unresolvable_computer_name_raises_no_card(task_runtime, tool, args):
    orch, *_ = task_runtime
    await _mac(task_runtime)
    response = await _call(task_runtime, tool, computer="Not My Mac", **args)
    text = json.dumps(response.error or response.result)
    assert "computer_unavailable" in text and "Not My Mac" in text
    assert not [c for c in (response.ui_components or []) if c.get("type") == "card"]


@pytest.mark.parametrize("bypass", [False, True])
@pytest.mark.parametrize("tool,args,code", [
    ("click", {"x": 10, "y": 5, "hwnd": 42}, "source_bound_approval_required"),
    ("type_text", {"text": "hello", "hwnd": 42}, "source_bound_approval_required"),
    ("run_command", {"argv": ["wc", "notes.md"], "cwd": ROOT}, "refused"),
    ("write_file", {"path": ROOT + "/notes.md", "content": "hello"}, "refused"),
])
async def test_a_host_switch_or_session_grant_cannot_authorize_an_effect(task_runtime, bypass, tool, args, code):
    orch, _, effects, *_ = task_runtime
    session, host = await _mac(task_runtime)
    await orch.computer_sessions.set_bypass(host, bypass)
    orch.computer_sessions.grants.create(OWNER, host.host_id, session.session_id, CHAT, "previous-task",
        {"kind": "command"}, ("command",), {"argv0": "wc", "root": ROOT}, now=time.time())
    response = await _call(task_runtime, tool, **args)
    assert response.error["code"] == code
    assert not response.ui_components
    assert not effects
