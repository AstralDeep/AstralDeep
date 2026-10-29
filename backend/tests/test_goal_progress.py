"""Tests the request-scoped completion controller that guards verifier decisions."""

import json
from types import SimpleNamespace

import pytest

from orchestrator.goal_progress import CompletionDecision, GoalProgress, parse_completion
from orchestrator.computer_hosts import ComputerHostError
from shared.protocol import MCPResponse


def _decision(status, remaining=(), reason=""):
    return CompletionDecision(status=status, remaining=tuple(remaining), reason=reason)


def _completion():
    return _decision("complete")


def _failed(error="command failed"):
    return SimpleNamespace(error=error, result={"_data": {"status": "error"}})


def test_parse_completion_accepts_exact_complete_json():
    decision = parse_completion('{"status":"complete","remaining":[],"reason":""}')
    assert decision == _completion()


@pytest.mark.parametrize("text", [
    '```json\n{"status":"complete","remaining":[],"reason":""}\n```',
    '{"status":"complete","remaining":[],"reason":"","extra":1}',
    '{"status":"complete","remaining":[],"reason":""} trailing',
    '{"status":"unknown","remaining":[],"reason":""}',
    '{"status":"complete","remaining":["work"],"reason":""}',
    '{"status":"continue","remaining":[],"reason":""}',
    '{"status":"blocked","remaining":[],"reason":""}',
    '{"status":"complete","remaining":"none","reason":""}',
    '{"status":"complete","remaining":[],"reason":3}',
    '[]',
])
def test_parse_completion_rejects_non_contract_payloads(text):
    with pytest.raises(ValueError):
        parse_completion(text)


def test_parse_completion_bounds_text_and_list_lengths():
    with pytest.raises(ValueError):
        parse_completion(json.dumps({"status": "continue", "remaining": ["x" * 5000], "reason": ""}))
    with pytest.raises(ValueError):
        parse_completion(json.dumps({"status": "continue", "remaining": ["x"] * 100, "reason": ""}))


def test_informational_request_can_complete_without_tool_calls():
    progress = GoalProgress("What is 2 + 2?")
    assert progress.assess(_completion()) == _completion()


def test_only_actual_computer_run_command_jobs_remain_pending():
    progress = GoalProgress("Complete a computation")
    progress.record("c1", "run_command", {"argv": ["sleep", "1"]},
                    SimpleNamespace(error=None, result={"_data": {"job_id": "job-1", "background": True}}))
    progress.record("c2", "other_tool", {},
                    SimpleNamespace(error=None, result={"_data": {"job_id": "job-2", "background": True}}))
    assert progress.pending_jobs == {"job-1"}
    assert progress.assess(_completion()).status == "continue"
    assert "job-1" in progress.assess(_completion()).remaining[0]


def test_known_running_job_clears_when_poll_exits():
    progress = GoalProgress("Complete a computation")
    progress.record("c1", "run_command", {}, SimpleNamespace(error=None, result={"_data": {"job_id": "j", "background": True}}))
    progress.record("c2", "read_output", '{"job_id":"j"}', SimpleNamespace(error=None, result={"_data": {"job_id": "j", "state": "exited", "exit_code": 0}}))
    assert progress.pending_jobs == set()
    assert progress.assess(_completion()).status == "complete"


def test_only_an_explicit_same_job_observation_resolves_job_uncertainty():
    progress = GoalProgress("Complete a computation")
    progress.record("c1", "run_command", {}, SimpleNamespace(error=None, result={"_data": {"job_id": "j", "background": True}}))
    progress.record("c2", "read_output", '{"job_id":"j"}', SimpleNamespace(error=None, result={"_data": {"job_id": "j", "state": "uncertain"}}))
    assert progress.assess(_completion()).status == "continue"
    progress.record("c3", "read_output", '{"job_id":"j"}', SimpleNamespace(error=None, result={"_data": {"job_id": "j", "state": "running"}}))
    assert progress.evidence()["uncertain_jobs"] == []
    assert progress.pending_jobs == {"j"}
    progress.record("c4", "read_output", '{"job_id":"j"}', SimpleNamespace(error=None, result={"_data": {"job_id": "j", "state": "exited", "exit_code": 0}}))
    assert progress.assess(_completion()).status == "complete"


@pytest.mark.parametrize("state", ["timed_out", "interrupted"])
def test_job_terminating_states_clear_pending_jobs(state):
    progress = GoalProgress("Complete a computation")
    progress.record("c1", "run_command", {}, SimpleNamespace(error=None, result={"_data": {"job_id": "j", "background": True}}))
    progress.record("c2", "interrupt_command", '{"job_id":"j"}', SimpleNamespace(error=None, result={"_data": {"job_id": "j", "state": state}}))
    assert progress.pending_jobs == set()


def test_failed_tool_call_then_success_resets_stalled_state():
    progress = GoalProgress("Repair a file")
    progress.record("f1", "write_file", {"path": "a"}, _failed())
    progress.record("p1", "write_file", {"path": "a"}, SimpleNamespace(error=None, result={"_data": {"ok": True}}))
    assert progress.stalled is False
    assert progress.assess(_completion()).status == "complete"


def test_failed_call_cannot_be_declared_complete_without_recovery():
    progress = GoalProgress("Run a process")
    progress.record("failed-call", "run_command", {"argv": ["false"]}, SimpleNamespace(
        error=None, result={"_data": {"exit_code": 1, "timed_out": False}}))
    decision = progress.assess(_completion())
    assert decision.status == "continue"
    assert decision.remaining
    assert "failed-call" in progress.evidence()["unresolved_failures"]


def test_successful_retry_of_the_same_parsed_invocation_resolves_failure():
    progress = GoalProgress("Run a process")
    progress.record("failed-call", "run_command", '{"argv":["cmd","x"]}', SimpleNamespace(
        error=None, result={"_data": {"exit_code": 2, "timed_out": False}}))
    progress.record("retry-call", "run_command", {"argv": ["cmd", "x"]}, SimpleNamespace(
        error=None, result={"_data": {"exit_code": 0, "timed_out": False}}))
    assert progress.evidence()["unresolved_failures"] == []
    assert progress.assess(_completion()).status == "complete"


def test_unrelated_success_does_not_resolve_failed_call():
    progress = GoalProgress("Look up a source")
    progress.record("failed-call", "search", {"query": "first source"}, _failed())
    progress.record("success-call", "search", {"query": "different source"}, SimpleNamespace(
        error=None, result={"_data": {"items": ["source"]}}))
    assert progress.assess(_completion()).status == "continue"


def test_explicit_resolution_accepts_later_successful_alternate_evidence():
    progress = GoalProgress("Look up a source")
    progress.record("failed-call", "search", {"query": "first source"}, _failed())
    progress.record("success-call", "search", {"query": "alternate source"}, SimpleNamespace(
        error=None, result={"_data": {"items": ["source"]}}))
    decision = CompletionDecision("complete", resolutions=(("failed-call", "success-call"),))
    assert progress.assess(decision).status == "complete"


def test_resolution_metadata_survives_tool_evidence_tail_truncation():
    progress = GoalProgress("Look up a source")
    progress.record("failed-call", "search", {"query": "first source"}, _failed())
    progress.record("success-call", "search", {"query": "alternate source"}, SimpleNamespace(
        error=None, result={"_data": {"items": ["source"]}}))
    for index in range(70):
        progress.record(f"call-{index}", "search", {"query": f"other-{index}"}, SimpleNamespace(
            error=None, result={"_data": {"items": []}}))
    evidence = progress.evidence()
    assert len(evidence["tools"]) == 64
    assert "success-call" in {item["call_id"] for item in evidence["completed_successes"]}
    assert "failed-call" in evidence["unresolved_failures"]
    assert progress.assess(CompletionDecision("complete", resolutions=(("failed-call", "success-call"),))).status == "complete"


def test_failed_call_id_cannot_resolve_itself_after_later_reuse():
    progress = GoalProgress("Look up a source")
    progress.record("same-id", "search", {"query": "first"}, _failed())
    progress.record("same-id", "search", {"query": "alternate"}, SimpleNamespace(
        error=None, result={"_data": {"items": ["source"]}}))
    decision = CompletionDecision("complete", resolutions=(("same-id", "same-id"),))
    assert progress.assess(decision).status == "continue"


@pytest.mark.parametrize("resolutions", [
    (("failed-call", "missing-call"),),
    (("failed-call", "failed-call"),),
    (("failed-call", "earlier-success"),),
    (("failed-call", "later-failure"),),
    (("missing-failure", "later-success"),),
])
def test_invalid_resolution_references_do_not_clear_failure(resolutions):
    progress = GoalProgress("Run a process")
    progress.record("failed-call", "write_file", {"path": "a"}, _failed())
    progress.record("earlier-success", "read_file", {"path": "a"}, SimpleNamespace(error=None, result={"_data": {"text": "old"}}))
    progress.record("later-failure", "write_file", {"path": "a"}, _failed("still failed"))
    progress.record("later-success", "read_file", {"path": "b"}, SimpleNamespace(error=None, result={"_data": {"text": "new"}}))
    decision = CompletionDecision("complete", resolutions=resolutions)
    assert progress.assess(decision).status == "continue"


def test_timeout_uncertainty_and_background_start_cannot_resolve_failure():
    progress = GoalProgress("Run a process")
    progress.record("failed-call", "run_command", {"argv": ["cmd"], "background": True}, SimpleNamespace(error=None, result={"_data": {"exit_code": 1}}))
    progress.record("timeout-call", "run_command", {"argv": ["cmd"], "background": True}, SimpleNamespace(error=None, result={"_data": {"timed_out": True, "exit_code": None}}))
    progress.record("uncertain-call", "run_command", {"argv": ["cmd"], "background": True}, {"_data": {"code": "uncertain", "message": "unknown", "next_action": "inspect"}})
    progress.record("background-call", "run_command", {"argv": ["cmd"], "background": True}, SimpleNamespace(error=None, result={"_data": {"job_id": "j", "background": True}}))
    assert "failed-call" in progress.evidence()["unresolved_failures"]
    assert progress.assess(_completion()).status != "complete"


def test_completion_parser_accepts_optional_bounded_resolution_mapping():
    parsed = parse_completion('{"status":"complete","remaining":[],"reason":"","resolutions":{"failed":"success"}}')
    assert parsed.resolutions == (("failed", "success"),)


@pytest.mark.parametrize("text", [
    '{"status":"complete","remaining":[],"reason":"","resolutions":[]}',
    '{"status":"complete","remaining":[],"reason":"","resolutions":{"":"success"}}',
    '{"status":"complete","remaining":[],"reason":"","resolutions":{"failed":""}}',
    '{"status":"complete","remaining":[],"reason":"","resolutions":{"failed":"x"' + '}' * 32 + '}',
])
def test_completion_parser_rejects_malformed_or_oversized_resolution_mapping(text):
    with pytest.raises(ValueError):
        parse_completion(text)


def test_completion_parser_rejects_more_than_32_resolution_pairs():
    mapping = {f"failed-{index}": f"success-{index}" for index in range(33)}
    text = json.dumps({"status": "complete", "remaining": [], "reason": "", "resolutions": mapping})
    with pytest.raises(ValueError):
        parse_completion(text)


@pytest.mark.parametrize("field", ["failed", "success"])
def test_completion_parser_rejects_resolution_ids_over_128_characters(field):
    source, target = "failed", "success"
    if field == "failed":
        source = "f" * 129
    else:
        target = "s" * 129
    text = json.dumps({"status": "complete", "remaining": [], "reason": "", "resolutions": {source: target}})
    with pytest.raises(ValueError):
        parse_completion(text)


def test_uncertain_result_blocks_completion_with_inspection_guidance():
    progress = GoalProgress("Run a process")
    progress.record("c1", "run_command", {"argv": ["python", "work.py"]}, {
        "_ui_components": [],
        "_data": {"code": "uncertain", "message": "Host lost contact", "next_action": "inspect the folder before retrying"},
    })
    decision = progress.assess(_completion())
    assert decision.status == "blocked"
    assert "inspect" in decision.reason.lower()
    assert progress.evidence()["tools"][0]["error"]["code"] == "uncertain"


@pytest.mark.parametrize("code", ["outcome_uncertain", "effect_uncertain"])
def test_real_uncertain_effect_envelopes_remain_uncertain_and_exposed(code):
    progress = GoalProgress("Run a process")
    message = "Delivery may have completed; inspect before retrying."
    if code == "outcome_uncertain":
        source = ComputerHostError(code, message)
        response = MCPResponse(error={"code": source.code, "message": source.message})
    else:
        response = MCPResponse(error={"code": code, "message": message})
    progress.record("uncertain-call", "run_command", {"argv": ["cmd"]}, response)

    assert progress.evidence()["tools"][0]["outcome"] == "uncertain"
    assert progress.requires_inspection is True
    assert progress.assess(_completion()).status == "blocked"


def test_replaying_identical_call_does_not_clear_uncertain_effect():
    progress = GoalProgress("Run a process")
    progress.record("uncertain-call", "run_command", {"argv": ["cmd"]}, SimpleNamespace(
        error={"code": "outcome_uncertain", "message": "Delivery may have completed."},
        result=None,
    ))
    progress.record("retry-call", "run_command", {"argv": ["cmd"]}, SimpleNamespace(
        error=None, result={"_data": {"exit_code": 0, "timed_out": False}},
    ))

    assert progress.requires_inspection is True
    assert progress.assess(_completion()).status == "blocked"


def test_interrupt_result_cannot_resolve_uncertain_computer_job():
    progress = GoalProgress("Complete a computation")
    progress.record("start", "run_command", {}, SimpleNamespace(error=None, result={
        "_data": {"job_id": "j", "background": True},
    }))
    progress.record("uncertain-poll", "read_output", '{"job_id":"j"}', SimpleNamespace(
        error=None, result={"_data": {"job_id": "j", "state": "uncertain"}},
    ))
    progress.record("interrupt", "interrupt_command", '{"job_id":"j"}', SimpleNamespace(
        error=None, result={"_data": {"job_id": "j", "state": "exited", "exit_code": 0}},
    ))

    assert progress.evidence()["uncertain_jobs"] == ["j"]
    assert progress.pending_jobs == {"j"}
    assert progress.requires_inspection is False
    assert progress.assess(_completion()).status == "continue"


def test_three_identical_hard_failures_mark_request_stalled():
    progress = GoalProgress("Repair a file")
    for call_id in ("f1", "f2", "f3"):
        progress.record(call_id, "write_file", {"path": "a"}, _failed())
    assert progress.stalled is True
    assert progress.assess(_decision("continue", ("retry the operation",))).status == "blocked"


def test_nonzero_exit_and_timeout_are_not_successful_results():
    progress = GoalProgress("Run a process")
    progress.record("c1", "run_command", {}, SimpleNamespace(error=None, result={"_data": {"exit_code": 2}}))
    progress.record("c2", "run_command", {}, SimpleNamespace(error=None, result={"_data": {"timed_out": True, "exit_code": None}}))
    assert progress.evidence()["tools"][-1]["outcome"] == "failure"
    assert progress.evidence()["tools"][-2]["outcome"] == "failure"


def test_three_continue_decisions_without_tool_calls_block():
    progress = GoalProgress("Research a topic")
    decision = _decision("continue", ("find another source",))
    assert [progress.assess(decision).status for _ in range(3)] == ["continue", "continue", "blocked"]


def test_pending_job_polls_are_also_bounded_without_new_tool_calls():
    progress = GoalProgress("Complete a computation")
    progress.record("c1", "run_command", {}, SimpleNamespace(error=None, result={"_data": {"job_id": "j", "background": True}}))
    assert [progress.assess(_completion()).status for _ in range(3)] == ["continue", "continue", "blocked"]


def test_successful_unrelated_tool_does_not_resolve_unknown_effect():
    progress = GoalProgress("Run a process")
    progress.record("c1", "run_command", {}, SimpleNamespace(
        error={"code": "uncertain", "message": "Host lost contact", "next_action": "inspect"}, result=None))
    progress.record("c2", "search", {"query": "status"}, SimpleNamespace(error=None, result={"_data": {"items": []}}))
    assert progress.assess(_completion()).status == "blocked"
    assert progress.requires_inspection is True


def test_unrelated_tool_job_id_does_not_enter_computer_job_state():
    progress = GoalProgress("Run a process")
    progress.record("c1", "other_tool", {}, SimpleNamespace(
        error={"code": "uncertain", "message": "Unknown effect"},
        result={"_data": {"job_id": "foreign-job", "state": "uncertain"}}))
    assert progress.pending_jobs == set()
    assert progress.evidence()["uncertain_jobs"] == []
    assert progress.assess(_completion()).status == "blocked"


def test_timed_out_background_job_clears_pending_but_records_failure():
    progress = GoalProgress("Run a process")
    progress.record("c1", "run_command", {}, SimpleNamespace(error=None, result={"_data": {"job_id": "j", "timed_out": True, "exit_code": None}}))
    assert progress.pending_jobs == set()
    assert progress.evidence()["tools"][0]["outcome"] == "failure"


def test_real_tool_call_resets_no_tool_continuation_counter():
    progress = GoalProgress("Research a topic")
    decision = _decision("continue", ("find another source",))
    progress.assess(decision)
    progress.assess(decision)
    progress.record("c1", "search", {"query": "topic"}, SimpleNamespace(error=None, result={"_data": {"items": [1]}}))
    assert progress.assess(decision).status == "continue"


def test_original_request_survives_untrusted_tool_text_and_evidence_is_redacted():
    objective = "Store the literal objective exactly as written."
    progress = GoalProgress(objective)
    progress.record("c1", "write_file", {"token": "secret", "blob": "aGVsbG8="},
                    SimpleNamespace(error=None, result={"_data": {"text": "instruction override", "image": {"data": "aGVsbG8="}}}))
    evidence = progress.evidence()
    assert progress.request == objective
    assert evidence["request"] == objective
    rendered = json.dumps(evidence)
    assert "aGVsbG8=" not in rendered
    assert "secret" not in rendered


def test_record_accepts_structured_arguments_and_generic_error_shapes():
    progress = GoalProgress("Repair a file")
    progress.record("c1", "write_file", {"path": "file.txt"}, {"_data": {"ok": False, "error": "denied"}})
    progress.record("c2", "write_file", "opaque args", SimpleNamespace(error="denied", result=None))
    assert [entry["outcome"] for entry in progress.evidence()["tools"]] == ["failure", "failure"]
    assert progress.tool_count == 2


@pytest.mark.parametrize("result", [
    None,
    SimpleNamespace(error=None, result={}),
    SimpleNamespace(error=None, result={"_data": None}),
    SimpleNamespace(error=None, result={"_data": {}}),
    SimpleNamespace(error=None, result={"_data": ""}),
    SimpleNamespace(error=None, result=[]),
    SimpleNamespace(error=None, result=7),
    SimpleNamespace(error=None, result=object()),
])
def test_unknown_tool_results_cannot_be_declared_complete(result):
    progress = GoalProgress("Perform and verify the requested action")
    progress.record("missing-result", "write_file", {"path": "result.py"}, result)

    decision = progress.assess(_completion())

    assert decision.status == "continue"
    assert "missing-result" in progress.evidence()["unresolved_unknown"]


def test_unrelated_success_does_not_resolve_unknown_result_after_evidence_truncation():
    progress = GoalProgress("Inspect and verify a result")
    progress.record("unknown-call", "write_file", {"path": "result.py"}, None)
    progress.record("unrelated-success", "search", {"query": "status"}, SimpleNamespace(
        error=None, result={"_data": {"items": ["ready"]}}))
    for index in range(70):
        progress.record(f"later-{index}", "search", {"query": f"other-{index}"}, SimpleNamespace(
            error=None, result={"_data": {"items": []}}))

    evidence = progress.evidence()
    assert len(evidence["tools"]) == 64
    assert "unknown-call" in evidence["unresolved_unknown"]
    assert progress.assess(_completion()).status == "continue"


def test_unknown_tracking_overflow_still_prevents_completion():
    progress = GoalProgress("Inspect every requested result")
    for index in range(257):
        progress.record(f"unknown-{index}", "write_file", {"path": str(index)}, None)

    evidence = progress.evidence()
    assert evidence["unknown_tracking_overflow"] == 1
    assert progress.requires_inspection is True
    assert progress.assess(_completion()).status == "continue"


def test_unknown_result_requires_explicit_later_verification_to_resolve():
    progress = GoalProgress("Inspect and verify a result")
    progress.record("unknown-call", "write_file", {"path": "result.py"}, None)
    assert progress.requires_inspection is True
    progress.record("inspection-call", "read_file", {"path": "result.py"}, SimpleNamespace(
        error=None, result={"_data": {"source": "print('ready')"}}))

    decision = CompletionDecision("complete", resolutions=(("unknown-call", "inspection-call"),))

    assert progress.assess(decision).status == "complete"
    assert progress.evidence()["unresolved_unknown"] == []
    assert progress.requires_inspection is False


@pytest.mark.parametrize("payload", [
    {"_data": {"path": "module.py", "source": "print('ready')"}},
    {"_data": ["print('ready')", "return 0"]},
    {"_data": "print('ready')"},
])
def test_normal_structured_source_payloads_remain_successful(payload):
    progress = GoalProgress("Review the source")
    progress.record("source-call", "read_file", {"path": "module.py"}, SimpleNamespace(
        error=None, result=payload))

    assert progress.evidence()["tools"][0]["outcome"] == "success"
    assert progress.assess(_completion()).status == "complete"


def test_unknown_or_absent_results_do_not_count_as_success_or_failure():
    progress = GoalProgress("Repair a file")
    progress.record("c1", "write_file", {}, SimpleNamespace(error=None, result=None))
    progress.record("c2", "write_file", {}, {"_data": {}})
    assert [entry["outcome"] for entry in progress.evidence()["tools"]] == ["unknown", "unknown"]
    assert progress.stalled is False


@pytest.mark.parametrize("state", [{"phase": "complete"}, ["complete"]])
def test_arbitrary_state_values_remain_payload_without_crashing(state):
    progress = GoalProgress("Inspect structured output")
    progress.record("c1", "search", {}, {"_data": {"state": state, "code": "source snippet"}})
    assert progress.evidence()["tools"][0]["outcome"] == "success"
    assert progress.evidence()["tools"][0]["status"] == "unknown"


def test_successful_code_payload_is_not_mistaken_for_error_envelope():
    progress = GoalProgress("Read a source")
    progress.record("c1", "search", {}, {"_data": {"code": "def answer(): return 1", "source": "module"}})
    assert progress.evidence()["tools"][0]["outcome"] == "success"


@pytest.mark.parametrize("code", [{"source": "def answer(): return 1"}, ["def answer(): return 1"]])
def test_structured_code_payload_is_a_successful_source_result(code):
    progress = GoalProgress("Read a source")
    progress.record("c1", "read_file", {}, {"_data": {"code": code, "source": "module"}})
    assert progress.evidence()["tools"][0]["outcome"] == "success"


@pytest.mark.parametrize("code", [{"details": "invalid"}, ["invalid"]])
def test_unhashable_error_code_payload_is_a_failure_without_crashing(code):
    progress = GoalProgress("Run a command")
    progress.record("c1", "run_command", {}, SimpleNamespace(
        error={"code": code, "message": "The command failed"}, result=None,
    ))
    assert progress.evidence()["tools"][0]["outcome"] == "failure"


@pytest.mark.parametrize("state", [{"phase": "complete"}, ["complete"]])
def test_run_command_unrecognized_structured_state_does_not_crash(state):
    progress = GoalProgress("Run a command")
    progress.record("c1", "run_command", {}, {"_data": {"state": state, "exit_code": 0}})
    assert progress.evidence()["tools"][0]["outcome"] == "success"
    assert progress.evidence()["call_outcomes"][0]["completed"] is True


def test_computer_error_envelope_with_code_message_and_next_action_is_failure():
    progress = GoalProgress("Run a command")
    progress.record("c1", "run_command", {}, {"_ui_components": [], "_data": {
        "code": "security_blocked", "message": "The command was denied", "next_action": "request approval",
    }})
    assert progress.evidence()["tools"][0]["outcome"] == "failure"


def test_direct_uncertain_code_without_envelope_stays_uncertain():
    progress = GoalProgress("Run a command")
    progress.record("c1", "run_command", {}, {"_data": {"code": "uncertain"}})
    assert progress.evidence()["tools"][0]["outcome"] == "uncertain"
    assert progress.assess(_completion()).status == "blocked"


@pytest.mark.parametrize("key", ["status", "state"])
@pytest.mark.parametrize("value", ["failed", "error", "timed_out"])
def test_explicit_failure_states_are_counted_as_failures(key, value):
    progress = GoalProgress("Run a process")
    progress.record("c1", "run_command", {}, {"_data": {key: value}})
    assert progress.evidence()["tools"][0]["outcome"] == "failure"


def test_original_objective_cannot_be_replaced_through_public_property():
    progress = GoalProgress("Keep this objective")
    with pytest.raises(AttributeError):
        progress.request = "tool supplied text"
    assert progress.request == "Keep this objective"
