"""Owner-authenticated Schedule assignment adapter and durable command contracts."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from orchestrator.projection_surfaces import personalization as surf
from persistent_agents.models import AssignmentError, AssignmentLimits

TOOL_OPTIONS = surf._assignment_tool_options


def run(value):
    return asyncio.run(value)


def record(**updates):
    result = {
        "assignment_id": str(uuid4()), "instruction_revision": 2, "control_epoch": 3,
        "state_version": 800, "lifecycle": "active", "phase": "waiting",
        "next_wake_at": "2026-09-06T12:00:00+00:00", "wake_reason": "cadence",
        "definition": {
            "name": "Releases", "instructions": "Notify me of meaningful releases.",
            "source": {"profile": "public_page", "agent_id": "web-research-1",
                       "tool_name": "fetch_page", "arguments": {"url": "https://example.org/releases"},
                       "linked_document_urls": []},
            "allowed_tools": ["web-research-1:fetch_page"], "consented_scopes": ["tools:read"],
            "limits": AssignmentLimits().to_plane(), "conversation_id": "chat-1",
        },
        "authority": {"grant_bound": True}, "usage": {"spent": {"tool_calls": 2}, "daily": {}, "outstanding": {}},
        "tasks": [], "cost_status": "unpriced", "latest_result": "A release was published.",
    }
    result.update(updates)
    return result


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(surf.flags, "is_enabled", lambda key: True)
    from persistent_agents import service
    monkeypatch.setattr(service, "public_record", copy.deepcopy)
    monkeypatch.setattr(service, "public_action", copy.deepcopy)
    monkeypatch.setattr(service, "thaw", copy.deepcopy)
    row = record()
    svc = SimpleNamespace(
        list=AsyncMock(return_value=[row]), get=AsyncMock(return_value=row),
        activity=AsyncMock(return_value=[]), proposals=AsyncMock(return_value=[]),
        create=AsyncMock(return_value=SimpleNamespace(assignment_id=row["assignment_id"])),
        revise=AsyncMock(return_value=SimpleNamespace()), control=AsyncMock(return_value=SimpleNamespace()),
        decide=AsyncMock(return_value=SimpleNamespace(state="completed")),
        tool_bound=lambda identity: {"tool_calls": 1},
    )
    # A real socket is hashable; SimpleNamespace is not.
    socket = type("Socket", (), {"closed": False})()
    claims = {"sub": "owner", "scope": "tools:read"}
    permissions = SimpleNamespace(list_disabled_agents=lambda owner: [],
        get_tool_scope=lambda agent, tool: "tools:read")
    orch = SimpleNamespace(persistent_assignments=svc, ui_sessions={socket: claims},
        tool_permissions=permissions, persistent_assignment_runner=SimpleNamespace(notify=lambda *a: None))
    monkeypatch.setattr(surf, "_assignment_tool_options", lambda *args: (["public_page"], ["web-research-1:fetch_page"]))
    return orch, socket, claims, row


def control_payload(row):
    return {"assignment_id": row["assignment_id"], "submission_id": str(uuid4()),
            "expected_instruction_revision": row["instruction_revision"],
            "expected_control_epoch": row["control_epoch"]}


def form_fields():
    limits = AssignmentLimits().model_dump()
    fields = {"name": "Releases", "instructions": "Notify me about new releases.",
              "source_key": "public_page", "source_url": "https://example.org/releases",
              "allowed_tools": ["web-research-1:fetch_page"], "conversation_id": "chat-1",
              "completion_condition": "", "currency_cap_enabled": False, "currency": "",
              "consent": True}
    for key, value in limits.items():
        if isinstance(value, dict):
            for metric, amount in value.items():
                if metric != "currency":
                    fields[f"limits.{key}.{metric}"] = "" if amount is None else str(amount)
        else:
            fields[f"limits.{key}"] = str(value)
    return fields


def test_eight_controls_are_registered_in_both_registries():
    from orchestrator.projection_controllers import PROJECTION_COMMAND_ACTIONS
    actions = {f"chrome_assignment_{part}" for part in ("create", "revise", "pause", "resume", "stop", "revoke", "run_now", "approval_decide")}
    assert actions <= surf.HANDLERS.keys()
    assert actions <= PROJECTION_COMMAND_ACTIONS["personalization"]


@pytest.mark.parametrize("command", ["pause", "resume", "stop", "revoke", "run_now"])
def test_controls_preserve_session_identity_and_owner_versions(host, command):
    orch, socket, claims, row = host
    payload = control_payload(row)
    result = run(surf.HANDLERS[f"chrome_assignment_{command}"](orch, socket, "owner", [], payload))
    args = orch.persistent_assignments.control.call_args.args
    assert args[:4] == ("owner", claims, row["assignment_id"], command.replace("_", "-"))
    assert args[4].model_dump() == {key: value for key, value in payload.items() if key != "assignment_id"}
    assert result[1] == {"tab": "schedule", "assignment_id": row["assignment_id"]}


@pytest.mark.parametrize("bad", [{"owner_id": "other"}, {"fields": {"owner_id": "other"}}, {"expected_control_epoch": True}, {"submission_id": "bad"}])
def test_controls_reject_forgery_and_malformed_bodies(host, bad):
    orch, socket, _, row = host
    result = run(surf.HANDLERS["chrome_assignment_pause"](orch, socket, "owner", [], {**control_payload(row), **bad}))
    orch.persistent_assignments.control.assert_not_awaited()
    assert "invalid" in result[2].lower()


@pytest.mark.parametrize("claims", [None, {"sub": "other"}, {"sub": "owner", "act": {"sub": "agent"}}, {"sub": "owner", "machine_turn_class": "assignment"}])
def test_no_fallback_identity_for_persistent_mutations(host, claims):
    orch, socket, _, row = host
    orch.ui_sessions = {} if claims is None else {socket: claims}
    run(surf.HANDLERS["chrome_assignment_stop"](orch, socket, "owner", [], control_payload(row)))
    orch.persistent_assignments.control.assert_not_awaited()


def test_create_and_revision_use_shared_strict_models_and_explicit_consent(host):
    orch, socket, claims, row = host
    payload = {"submission_id": str(uuid4()), "fields": form_fields()}
    run(surf.HANDLERS["chrome_assignment_create"](orch, socket, "owner", [], payload))
    args = orch.persistent_assignments.create.call_args.args
    assert args[:2] == ("owner", claims)
    assert args[2].limits.daily.spend_micro_units is None
    assert args[2].limits.daily.tool_calls == 200
    assert args[2].completion_condition is None
    run(surf.HANDLERS["chrome_assignment_revise"](orch, socket, "owner", [], {**control_payload(row), "fields": form_fields()}))
    assert orch.persistent_assignments.revise.call_args.args[3].expected_control_epoch == 3


@pytest.mark.parametrize("command", ["create", "revise", "pause", "resume", "stop", "revoke", "run_now", "approval_decide"])
def test_client_request_generation_is_transport_metadata_not_assignment_authority(host, command):
    orch, socket, _, row = host
    # The shipped web and native clients copy their outer request generation
    # into payload. chrome_events forwards this payload to the owner adapter.
    payload = ({"submission_id": str(uuid4())} if command == "create" else control_payload(row))
    payload["request_generation"] = str(uuid4())
    if command in ("create", "revise"):
        payload["fields"] = form_fields()
    if command == "approval_decide":
        payload.update(action_id=str(uuid4()), request_digest="a" * 64, decision="approve")
    response = run(surf.HANDLERS[f"chrome_assignment_{command}"](orch, socket, "owner", [], payload))
    method = "decide" if command == "approval_decide" else command if command in ("create", "revise") else "control"
    called = getattr(orch.persistent_assignments, method)
    called.assert_awaited_once()
    request = called.call_args.args[-1]
    assert "request_generation" not in request.model_dump()
    assert request.submission_id == payload["submission_id"]
    assert "recorded" in response[2] or "requested" in response[2]


@pytest.mark.parametrize("generation", ["bad", None, {}, "00000000-0000-0000-0000-000000000000"])
def test_malformed_transport_generation_cannot_reach_assignment_service(host, generation):
    orch, socket, _, row = host
    run(surf.HANDLERS["chrome_assignment_pause"](orch, socket, "owner", [],
        {**control_payload(row), "request_generation": generation}))
    orch.persistent_assignments.control.assert_not_awaited()


@pytest.fixture
def unconfigured_chrome(host, monkeypatch):
    from orchestrator import chrome_events, llm_gate
    orch, socket, claims, row = host
    orch.llm_configured_for = AsyncMock(return_value=False)
    orch._record_llm_unconfigured = AsyncMock()
    orch._llm_audit_principals = lambda ws: ("owner", "owner")
    orch.audit_recorder = object()
    setup = AsyncMock()
    rendered = AsyncMock()
    monkeypatch.setattr(llm_gate, "push_setup_dialog", setup)
    monkeypatch.setattr(chrome_events, "_render_surface", rendered)
    monkeypatch.setattr(chrome_events, "_handlers", lambda: {
        action: ("personalization", handler) for action, handler in surf.HANDLERS.items()})
    return orch, socket, claims, row, setup, rendered


@pytest.mark.parametrize("command", ["pause", "stop", "revoke"])
def test_owner_can_quiesce_assignments_without_personal_llm(unconfigured_chrome, command):
    from orchestrator import chrome_events
    orch, socket, _, row, setup, _ = unconfigured_chrome
    assert run(chrome_events.handle_chrome_event(orch, socket,
        f"chrome_assignment_{command}", control_payload(row), "owner"))
    orch.persistent_assignments.control.assert_awaited_once()
    setup.assert_not_awaited()


@pytest.mark.parametrize("mode", [None, "list", "detail"])
def test_owner_can_view_assignment_schedule_without_personal_llm(unconfigured_chrome, mode):
    from orchestrator import chrome_events
    orch, socket, _, row, setup, rendered = unconfigured_chrome
    params = {"tab": "schedule"}
    if mode is not None:
        params.update(assignment_mode=mode, assignment_id=row["assignment_id"])
    assert run(chrome_events.handle_chrome_event(orch, socket, "chrome_open",
        {"surface": "personalization", "params": params}, "owner"))
    rendered.assert_awaited_once()
    setup.assert_not_awaited()


@pytest.mark.parametrize("action,payload", [
    ("chrome_assignment_create", {}), ("chrome_assignment_revise", {}),
    ("chrome_assignment_approval_decide", {}), ("chrome_assignment_resume", {}),
    ("chrome_assignment_run_now", {}),
    ("chrome_open", {"surface": "personalization", "params": {"tab": "schedule", "assignment_mode": "create"}}),
    ("chrome_open", {"surface": "personalization", "params": {"tab": "schedule", "assignment_mode": "revise"}}),
    ("chrome_open", {"surface": "personalization", "params": {"tab": "memory"}}),
    ("chrome_open", {"surface": "agents", "params": {"tab": "schedule"}}),
    ("chrome_open", {"surface": "personalization", "params": "{\"tab\":\"schedule\"}"}),
])
def test_missing_llm_exception_does_not_enable_new_work_or_other_surfaces(unconfigured_chrome, action, payload):
    from orchestrator import chrome_events
    orch, socket, _, _, setup, rendered = unconfigured_chrome
    assert run(chrome_events.handle_chrome_event(orch, socket, action, payload, "owner"))
    setup.assert_awaited_once()
    rendered.assert_not_awaited()
    for method in ("create", "revise", "control", "decide"):
        getattr(orch.persistent_assignments, method).assert_not_awaited()


@pytest.mark.parametrize("claims", [None, {"sub": "other"}, {"sub": "owner", "act": {"sub": "agent"}},
                                   {"sub": "owner", "machine_turn_class": "persistent_assignment"}])
def test_missing_llm_exception_requires_the_actual_owner_session(unconfigured_chrome, claims):
    from orchestrator import chrome_events
    orch, socket, _, row, _, _ = unconfigured_chrome
    orch.ui_sessions[socket] = claims
    run(chrome_events.handle_chrome_event(orch, socket, "chrome_assignment_stop", control_payload(row), "owner"))
    orch.persistent_assignments.control.assert_not_awaited()


def test_missing_llm_exception_cannot_stop_a_foreign_owners_assignment(unconfigured_chrome):
    from orchestrator import chrome_events
    from persistent_agents.service import AssignmentService
    orch, socket, _, row, setup, rendered = unconfigured_chrome
    # The real service performs owner-scoped retrieval before every control.
    # A foreign identity is unavailable in that scope, so no mutation is issued.
    store = SimpleNamespace(call=AsyncMock(return_value=None))
    orch.persistent_assignments = AssignmentService(orch, store=store, enabled=True,
                                                    phi_gate=SimpleNamespace())
    run(chrome_events.handle_chrome_event(orch, socket, "chrome_assignment_stop", control_payload(row), "owner"))
    store.call.assert_awaited_once_with("get_assignment", owner_id="owner", assignment_id=row["assignment_id"])
    setup.assert_not_awaited()
    assert "assignment_not_found" in rendered.call_args.args[-1]


@pytest.mark.parametrize("update", [{"consent": False}, {"consent": "true"}, {"limits.daily.tool_calls": "1.5"}, {"limits.daily.tokens": True}, {"owner_id": "forged"}, {"limits.extra": 7}, {"currency_cap_enabled": True}])
def test_form_rejects_unreviewed_unknown_or_invalid_fields(host, update):
    orch, socket, _, _ = host
    run(surf.HANDLERS["chrome_assignment_create"](orch, socket, "owner", [], {"submission_id": str(uuid4()), "fields": {**form_fields(), **update}}))
    orch.persistent_assignments.create.assert_not_awaited()


def test_exact_approval_uses_actual_socket_and_never_replacement_arguments(host):
    orch, socket, claims, row = host
    payload = {**control_payload(row), "action_id": str(uuid4()), "request_digest": "a" * 64, "decision": "approve"}
    result = run(surf.HANDLERS["chrome_assignment_approval_decide"](orch, socket, "owner", [], payload))
    args = orch.persistent_assignments.decide.call_args
    assert args.args[:4] == ("owner", claims, row["assignment_id"], payload["action_id"])
    assert args.kwargs == {"interaction": socket}
    assert "recorded" in result[2].lower()
    orch.persistent_assignments.decide.reset_mock()
    run(surf.HANDLERS["chrome_assignment_approval_decide"](orch, socket, "owner", [], {**payload, "arguments": {"url": "https://evil.example"}}))
    orch.persistent_assignments.decide.assert_not_awaited()


def test_pending_attended_approval_does_not_claim_completion(host):
    orch, socket, _, row = host
    orch.persistent_assignments.decide.return_value = SimpleNamespace(state="approved")
    payload = {**control_payload(row), "action_id": str(uuid4()), "request_digest": "a" * 64, "decision": "approve"}
    result = run(surf.HANDLERS["chrome_assignment_approval_decide"](orch, socket, "owner", [], payload))
    assert "completed" not in result[2].lower()
    assert "review" in result[2].lower()


def test_conflict_and_unexpected_failure_have_safe_notices(host):
    orch, socket, _, row = host
    for error in (AssignmentError("assignment_revision_conflict"), RuntimeError("secret-token-do-not-render")):
        orch.persistent_assignments.control.side_effect = error
        result = run(surf.HANDLERS["chrome_assignment_pause"](orch, socket, "owner", [], control_payload(row)))
        assert "secret-token" not in result[2]
        assert "reload" in result[2].lower()


def test_shared_schedule_list_and_detail_keep_secrets_out(host, monkeypatch):
    orch, socket, _, row = host
    from orchestrator.chrome_events import current_surface_socket
    monkeypatch.setattr(surf, "_render_schedule", lambda *args: "legacy-jobs")
    monkeypatch.setattr(surf, "_components_schedule", lambda *args: [{"type": "text", "content": "legacy-jobs"}])
    token = current_surface_socket.set(socket)
    try:
        markup = run(surf.render(orch, "owner", [], {"tab": "schedule"}))
        native = run(surf.components(orch, "owner", [], {"tab": "schedule", "assignment_id": row["assignment_id"]}))
    finally:
        current_surface_socket.reset(token)
    assert "Ongoing agents" in markup and "Scheduled tasks" in markup and "legacy-jobs" in markup
    assert "Releases" in json.dumps(native) and "Unpriced/unknown" in json.dumps(native)
    assert "state_version" not in json.dumps(native)


def test_snapshot_pagination_form_draft_and_consented_tool_options(host):
    orch, socket, _, row = host
    state = run(surf._assignment_state(orch, socket, "owner", {"assignment_mode": "create", "assignment_draft": {"name": "Draft", "instructions": "Check releases.", "source_url": "https://example.org/new"}, "conversation_id": "chat-2"}))
    assert state["assignment"]["definition"]["name"] == "Draft"
    assert state["assignment"]["definition"]["conversation_id"] == "chat-2"
    assert state["assignment"]["definition"].get("consent") is None
    assert state["source_options"] == ["public_page"]
    assert len(state["limit_fields"]) == 16
    assert state["assignment"]["definition"]["currency_cap_enabled"] is False
    orch.persistent_assignments.list.return_value = [row] * 50
    listed = run(surf._assignment_state(orch, socket, "owner", {}))
    assert listed["next_cursor"] == row["assignment_id"]


def test_detail_approval_activity_and_task_mapping(host):
    orch, socket, _, row = host
    row["tasks"] = [{"title": "Read release", "state": "completed", "bounded_result": "Checked.", "incorporated_by": {"parent": "digest"}, "depends_on": ["first"], "provenance": {"source": "release page"}}]
    orch.persistent_assignments.activity.return_value = [{"title": "Finding", "summary": "New version.", "created_at": "today", "sequence": 1}] * 50
    action = {"action_id": str(uuid4()), "instruction_revision": 2, "control_epoch": 3, "state": "proposed", "intent": {"request_digest": "a" * 64, "request": {"kind": "tool", "agent_id": "outlook", "tool_name": "send_email", "arguments": {"recipient": "owner@example.org"}}, "approval_expires_at": "2000-01-01T00:00:00+00:00", "sensitivity": "sensitive", "interactive_only": True, "precondition_digest": "b" * 64}}
    orch.persistent_assignments.proposals.return_value = [action]
    state = run(surf._assignment_state(orch, socket, "owner", {"assignment_id": row["assignment_id"]}))
    assert state["assignment"]["approvals"][0]["expired"] is True
    assert state["assignment"]["activity_cursor"] == "1"
    assert state["assignment"]["tasks"][0]["incorporated"] is True


def test_missing_service_closed_socket_and_feature_off_are_fail_closed(host, monkeypatch):
    orch, socket, _, _row = host
    socket.closed = True
    assert "error" in run(surf._assignment_state(orch, socket, "owner", {}))
    socket.closed = False
    orch.persistent_assignments = None
    assert "error" in run(surf._assignment_state(orch, socket, "owner", {}))
    monkeypatch.setattr(surf.flags, "is_enabled", lambda key: False)
    assert run(surf._assignment_state(orch, socket, "owner", {}))["enabled"] is False
    run(surf.HANDLERS["chrome_assignment_pause"](orch, socket, "owner", [], {}))


def test_invalid_cursor_and_draft_never_echo_source_payload(host):
    orch, socket, _, row = host
    for params in ({"assignment_id": row["assignment_id"], "activity_cursor": "bad"},
                   {"assignment_mode": "create", "assignment_draft": {"owner_id": "secret-owner"}}):
        state = run(surf._assignment_state(orch, socket, "owner", params))
        assert "error" in state and "secret-owner" not in json.dumps(state)


def test_currency_cap_is_explicit_integer_money_and_no_cap_is_unknown(host):
    _orch, _socket, _, row = host
    fields = form_fields()
    fields.update(currency_cap_enabled=True, currency="USD")
    fields["limits.daily.spend_micro_units"] = 1000
    fields["limits.lifetime.spend_micro_units"] = 2000
    result = surf._assignment_form(fields)
    assert result["limits"]["daily"]["spend_micro_units"] == 1000
    row["definition"]["limits"].update(currency="USD", spend_micro_units=2000, daily_spend_micro_units=1000)
    mapped, _limits = surf._assignment_row(row)
    assert mapped["currency_cap_label"] == "2000 millionths of USD"
    assert mapped["monetary_cost_label"] == "0 millionths of USD"


def test_registered_reader_and_linked_scope_use_canonical_source_model(host):
    fields = form_fields()
    fields.update(source_key="inbox:read", source_arguments='{"folder":"inbox"}', allowed_tools=["inbox:read"])
    result = surf._assignment_form(fields)
    assert result["source"]["profile"] == "registered_reader"
    assert result["source"]["arguments"] == {"folder": "inbox"}
    fields = form_fields()
    fields["linked_document_urls"] = "https://example.org/notes\nhttps://example.org/changelog"
    assert len(surf._assignment_form(fields)["source"]["linked_document_urls"]) == 2


@pytest.mark.parametrize("update", [{"source_key": "inbox:read", "source_arguments": []}, {"linked_document_urls": []}, {"allowed_tools": "bad"}, {"currency_cap_enabled": "true"}])
def test_more_malformed_form_types_are_refused(host, update):
    with pytest.raises((TypeError, ValueError)):
        surf._assignment_form({**form_fields(), **update})


def test_tool_options_reuse_effective_permissions_and_require_resource_bound(host, monkeypatch):
    orch, _, claims, _ = host
    from orchestrator import tool_visibility
    pairs = [("web-research-1", SimpleNamespace(id="fetch_page")), ("mail", SimpleNamespace(id="read")), ("mail", SimpleNamespace(id="unbounded"))]
    monkeypatch.setattr(tool_visibility, "eligible_tool_pairs", lambda *args, **kw: pairs)
    def bound(identity):
        if identity.endswith("unbounded"):
            raise AssignmentError("assignment_tool_bound_unavailable")
        return {"tool_calls": 1}
    orch.persistent_assignments.tool_bound = bound
    sources, tools = TOOL_OPTIONS(orch, orch.persistent_assignments, "owner", claims)
    assert sources == ["public_page", "mail:read"]
    assert tools == ["web-research-1:fetch_page", "mail:read"]


@pytest.mark.parametrize("operation", [
    {"arguments": {"access_token": "SECRET"}},
    {"arguments": ["https://example.org/?token=SECRET"]},
    {"arguments": "https://name:SECRET@example.org"},
    {"arguments": {"value": float("nan")}},
    {"arguments": {"value": "a" * 9000}},
    {"arguments": [[[[[[[[[["deep"]]]]]]]]]]},
])
def test_credential_or_incomplete_action_review_is_not_displayable(operation):
    assert surf._assignment_review_text(operation) is None


def test_review_rejects_secret_fields_without_approvable_controls(host):
    action = {"action_id": str(uuid4()), "state": "proposed", "intent": {
        "request": {"kind": "tool", "arguments": {"token": "SECRET"}},
        "request_digest": "a" * 64, "precondition_digest": "b" * 64, "approval_expires_at": None}}
    review = surf._assignment_approval(action)
    assert review["expired"] is True
    assert "SECRET" not in json.dumps(review)
    assert "cannot be reviewed" in review["arguments_summary"]


@pytest.mark.parametrize("params", [
    {"assignment_mode": "invalid"},
    {"assignment_mode": "create", "assignment_draft": {"name": ""}},
    {"assignment_mode": "create", "conversation_id": ""},
])
def test_invalid_view_selection_and_prefill_are_not_rendered(host, params):
    orch, socket, _, _ = host
    assert "error" in run(surf._assignment_state(orch, socket, "owner", params))


def test_read_failure_terminal_row_revision_form_and_begun_actions(host):
    orch, socket, _, row = host
    state = run(surf._assignment_state(orch, socket, "owner", {"assignment_mode": "revise", "assignment_id": row["assignment_id"]}))
    assert state["assignment"]["definition"]["instructions"] == row["definition"]["instructions"]
    assert state["limit_fields"][6]["value"] == 50
    row["lifecycle"] = "stopped"
    assert surf._assignment_row(row)[0]["available_actions"] == []
    orch.persistent_assignments.list.side_effect = RuntimeError("SECRET")
    state = run(surf._assignment_state(orch, socket, "owner", {}))
    assert "error" in state and "SECRET" not in json.dumps(state)
    orch.persistent_assignments.control.return_value = SimpleNamespace(begun_action_ids=(str(uuid4()),))
    result = run(surf.HANDLERS["chrome_assignment_stop"](orch, socket, "owner", [], control_payload(row)))
    assert "in flight" in result[2]


@pytest.mark.parametrize("command,payload", [("pause", []), ("create", {"fields": form_fields(), "submission_id": str(uuid4()), "name": "unreviewed"}), ("revise", {"fields": form_fields(), "submission_id": str(uuid4()), "extra": True})])
def test_transport_payload_cannot_override_form_or_add_fields(host, command, payload):
    orch, socket, _, row = host
    if command == "revise":
        payload = {**control_payload(row), **payload}
    run(surf.HANDLERS[f"chrome_assignment_{command}"](orch, socket, "owner", [], payload))
    orch.persistent_assignments.create.assert_not_awaited()
    orch.persistent_assignments.revise.assert_not_awaited()
    orch.persistent_assignments.control.assert_not_awaited()
