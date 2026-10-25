"""Verifies diagnostic registration readiness shares answer-stream bounds and deadlines.
Bootstrap and duplicate acknowledgements never provide answers or submit extra turns.
"""

import json
import threading

import pytest

from clinical_diagnostic.budget import BudgetTracker, ExecutionLimits
from clinical_diagnostic.dispatcher import DispatcherDenied, DispatcherError, DispatcherProducer, DispatcherTimeout
from clinical_diagnostic.grading import MarkerGrader
from clinical_diagnostic.runner import _execute_jobs
from clinical_diagnostic.tests.test_dispatcher import (
    FakeTransport, case, committed_answer, completed_turn, dispatcher,
)
from clinical_diagnostic.tests.test_final_boundaries import Clock


class RegistrationTransport(FakeTransport):
    def recv(self, timeout):
        if self.frames and callable(self.frames[0]):
            frame = self.frames.pop(0)
            return json.dumps(frame(self.sent[1]))
        return super().recv(timeout)


def adapter_for(frames, **kwargs):
    transport = RegistrationTransport(frames)
    adapter, _ = dispatcher([], transport_factory=lambda: transport, **kwargs)
    return adapter, transport


def test_bootstrap_and_duplicate_readiness_submit_exactly_one_turn():
    adapter, transport = adapter_for([
        {"type": "registered"}, {"type": "ui_render", "components": [
            {"type": "text", "content": "Bootstrap text must not be graded."}]},
        {"type": "rote_config"}, {"type": "rote_config"},
        committed_answer(), completed_turn(),
    ])
    receive = transport.recv
    observed = []

    def bounded_receive(timeout):
        observed.append(len(transport.sent))
        return receive(timeout)

    transport.recv = bounded_receive
    answer = adapter.answer(case())
    assert observed == [1, 1, 1, 2, 2, 2]
    assert [frame["type"] for frame in transport.sent] == ["register_ui", "ui_event"]
    assert transport.sent[0]["connection_generation"] == transport.sent[1]["connection_generation"]
    assert answer.text == "Final answer."
    assert answer.frame_types == ("registered", "ui_render", "rote_config", "rote_config",
                                  "conversation_snapshot", "operation_status")
    assert transport.closed


@pytest.mark.parametrize("frame,error,detail", [
    ({"type": "auth_required"}, DispatcherDenied, "denied"),
    ({"type": "auth_error"}, DispatcherDenied, "denied"),
    (TimeoutError(), DispatcherTimeout, "timed out"),
    (ConnectionError(), DispatcherError, "connection failed"),
    ("{", DispatcherError, "invalid JSON"),
    (b"\xff", DispatcherError, "UTF-8"),
    ([], DispatcherError, "not an object"),
    ("x" * 1025, DispatcherError, "size bound"),
    ({"type": "ui_render", "value": [[[[[0]]]]]}, DispatcherError, "nesting"),
])
def test_failed_registration_never_submits_chat(monkeypatch, frame, error, detail):
    if detail == "nesting":
        monkeypatch.setattr("clinical_diagnostic.dispatcher.MAX_JSON_DEPTH", 3)
    adapter, transport = adapter_for([frame], max_message_bytes=1024)
    with pytest.raises(error, match=detail):
        adapter.answer(case())
    assert [frame["type"] for frame in transport.sent] == ["register_ui"]
    assert transport.closed


@pytest.mark.parametrize("frames,limit", [
    ([{"type": "rote_config"}], 1),
    ([{"type": "registered"}, {"type": "rote_config"}], 2),
    ([{"type": "registered"}, {"type": "registered"}], 2),
])
def test_bootstrap_consumes_the_existing_frame_allowance_without_submission(frames, limit):
    adapter, transport = adapter_for(frames, max_frames=limit)
    with pytest.raises(DispatcherError):
        adapter.answer(case())
    assert [frame["type"] for frame in transport.sent] == ["register_ui"]
    assert transport.closed


def test_bootstrap_and_answer_share_the_same_frame_allowance():
    adapter, transport = adapter_for([
        {"type": "registered"}, {"type": "rote_config"}, committed_answer(), completed_turn(),
    ], max_frames=3)
    with pytest.raises(DispatcherError, match="terminal"):
        adapter.answer(case())
    assert len(transport.sent) == 2 and len(transport.frames) == 1 and transport.closed


def test_bootstrap_and_answer_share_the_same_aggregate_byte_allowance(monkeypatch):
    monkeypatch.setattr("clinical_diagnostic.dispatcher.MAX_RESPONSE_BYTES", 1200)
    adapter, transport = adapter_for([
        {"type": "registered", "text": "x" * 700}, {"type": "rote_config"},
        committed_answer(), completed_turn(),
    ])
    with pytest.raises(DispatcherError, match="response exceeds"):
        adapter.answer(case())
    assert len(transport.sent) == 2 and transport.closed


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("acknowledged", [False, True])
def test_shared_control_checked_during_bootstrap_and_after_readiness(cancel, acknowledged):
    clock, event = Clock(), threading.Event()
    adapter, transport = adapter_for([{"type": "rote_config" if acknowledged else "registered"}])
    receive = transport.recv

    def exhausted(timeout):
        frame = receive(timeout)
        if cancel:
            event.set()
        else:
            clock.now = 2
        return frame

    transport.recv = exhausted
    budget = BudgetTracker(ExecutionLimits(max_elapsed_seconds=1), clock=clock, cancel_event=event)
    result = _execute_jobs([(DispatcherProducer(adapter), case())], MarkerGrader(), budget, 0)[0]
    assert result.failure.code.value == ("cancelled" if cancel else "budget_exhausted")
    assert [frame["type"] for frame in transport.sent] == ["register_ui"]
    assert transport.closed


@pytest.mark.parametrize("frame,expected", [
    ({"type": "auth_required"}, "dispatcher_denied"),
    (TimeoutError(), "timeout"), ("{", "protocol_error"),
])
def test_registration_failures_finalize_non_success_reports(tmp_path, monkeypatch, frame, expected):
    from clinical_diagnostic.tests.test_final_boundaries import public_run

    adapter, transport = adapter_for([frame])
    outcome = public_run(tmp_path, monkeypatch, DispatcherProducer(adapter))
    assert outcome.exit_code == 3
    result = outcome.record.results[0]
    assert result.status == "infrastructure_failure" and result.score is None
    assert result.failure.code.value == expected
    assert json.loads(open(outcome.record_path).read())["results"][0]["score"] is None
    assert "Infrastructure" in open(outcome.report_path).read()
    assert [frame["type"] for frame in transport.sent] == ["register_ui"]
    assert transport.closed


def test_bootstrap_json_nodes_are_bounded_before_chat(monkeypatch):
    monkeypatch.setattr("clinical_diagnostic.dispatcher.MAX_JSON_NODES", 4)
    adapter, transport = adapter_for([{"type": "registered", "nodes": [0, 0, 0, 0]}])
    with pytest.raises(DispatcherError, match="node bound"):
        adapter.answer(case())
    assert len(transport.sent) == 1 and transport.closed


def test_acknowledgement_with_no_byte_allowance_does_not_submit(monkeypatch):
    acknowledgement = {"type": "rote_config"}
    monkeypatch.setattr("clinical_diagnostic.dispatcher.MAX_RESPONSE_BYTES", len(json.dumps(acknowledgement)))
    adapter, transport = adapter_for([acknowledgement])
    with pytest.raises(DispatcherError, match="registration exhausted"):
        adapter.answer(case())
    assert len(transport.sent) == 1 and transport.closed
