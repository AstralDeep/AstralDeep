"""Exercises owned I/O-worker result bounds, cancellation, deadline and cleanup barriers
without network calls, including one real spawned blocking worker.
"""

import json
import multiprocessing
import threading
from types import SimpleNamespace

import pytest

from clinical_diagnostic.budget import DeadlineControl, BudgetExhausted, RunCancelled, cancellation_event
from clinical_diagnostic.execution import supervise, _worker, execute_inline
from clinical_diagnostic.provider import ProducerError, ProviderReply


class Endpoint:
    def __init__(self, message=None, poll=None):
        self.message, self.on_poll, self.closed = message, poll, False

    def poll(self, timeout):
        return self.on_poll() if self.on_poll else True

    def recv_bytes(self, bound):
        return json.dumps(self.message).encode()

    def send_bytes(self, data):
        self.message = json.loads(data)

    def close(self):
        self.closed = True


class Process:
    def __init__(self, alive=True, kill=False, refuse=False, start_error=False):
        self.alive, self.need_kill, self.refuse = alive, kill, refuse
        self.start_error = start_error
        self.calls = []

    def start(self):
        self.calls.append("start")
        if self.start_error:
            raise OSError("synthetic spawn failure")

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.calls.append("terminate")
        if not self.need_kill:
            self.alive = False

    def kill(self):
        self.calls.append("kill")
        if not self.refuse:
            self.alive = False

    def join(self, timeout):
        assert 0 < timeout <= 0.5
        self.calls.append("join")

    def close(self):
        self.calls.append("close")


class Context:
    def __init__(self, message=None, poll=None, **kwargs):
        self.receiver, self.sender = Endpoint(message, poll), Endpoint()
        self.process = Process(**kwargs)

    def Pipe(self, duplex):
        assert duplex is False
        return self.receiver, self.sender

    def Process(self, **kwargs):
        return self.process


def control():
    return DeadlineControl(1, cancellation_event(), clock=lambda: 0)


def test_result_cleanup_does_not_cancel_the_shared_run():
    context = Context({"result": {"text": "fact", "model_identity": "synthetic", "cost": 0.5}})
    selected = control()
    result = supervise("completion", {}, selected, process_context=context)
    assert result == ProviderReply("fact", "synthetic", 0.5)
    assert context.process.calls == ["start", "terminate", "join", "close"]
    assert not selected.cancel_event.is_set()
    assert context.sender.closed and context.receiver.closed


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_active_stop_terminates_and_joins_before_raising(stop):
    selected, now = control(), [0]
    selected.clock = lambda: now[0]

    def poll():
        if stop == "cancel":
            selected.cancel_event.set()
        else:
            now[0] = 2
        return False

    context = Context(poll=poll, kill=True)
    with pytest.raises(RunCancelled if stop == "cancel" else BudgetExhausted):
        supervise("completion", {}, selected, process_context=context)
    assert context.process.calls == ["start", "terminate", "join", "kill", "join", "close"]
    assert not context.process.is_alive()
    assert context.receiver.closed and context.sender.closed


def test_cleanup_failure_is_conspicuous_and_closes_pipes():
    context = Context({"result": {}}, kill=True, refuse=True)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        supervise("completion", {}, control(), process_context=context)
    assert context.receiver.closed and context.sender.closed


def test_spawn_and_unreported_worker_exit_close_resources():
    for context in (Context(start_error=True), Context(alive=False, poll=lambda: False)):
        with pytest.raises((RuntimeError, OSError)):
            supervise("completion", {}, control(), process_context=context)
        assert context.receiver.closed and context.sender.closed
        assert context.process.calls[-1] == "close"


def test_thread_only_cancellation_is_refused_before_process_start():
    context = Context()
    with pytest.raises(ValueError, match="process-shared"):
        supervise("completion", {}, DeadlineControl(1, threading.Event()), process_context=context)
    assert context.process.calls == []
    assert context.receiver.closed and context.sender.closed


def test_typed_worker_error_preserves_known_charge_and_dispatcher_result():
    context = Context({"error": {"code": "invalid_grade", "detail": "invalid", "completed_cost": 0.5}})
    with pytest.raises(ProducerError) as raised:
        supervise("completion", {}, control(), process_context=context)
    assert raised.value.completed_cost == 0.5
    context = Context({"result": {"text": "fact", "chat_id": "synthetic-chat"}})
    assert supervise("dispatcher", {}, control(), process_context=context).chat_id == "synthetic-chat"


def test_worker_serializes_typed_unknown_and_oversized_results(monkeypatch):
    selected = control()
    for action, expected in [
        (lambda *_: ProviderReply("fact", "model"), "result"),
        (lambda *_: SimpleNamespace(), "error"),
    ]:
        monkeypatch.setattr("clinical_diagnostic.execution.execute_inline", action)
        sender = Endpoint()
        _worker(sender, "completion", {}, selected.deadline, selected.cancel_event)
        assert expected in sender.message and sender.closed

    def fail(*_):
        raise ProducerError("invalid", completed_cost=0.5)

    monkeypatch.setattr("clinical_diagnostic.execution.execute_inline", fail)
    sender = Endpoint()
    _worker(sender, "completion", {}, selected.deadline, selected.cancel_event)
    assert sender.message["error"]["completed_cost"] == 0.5
    monkeypatch.setattr("clinical_diagnostic.execution.MAX_RESULT_BYTES", 1)
    sender = Endpoint()
    _worker(sender, "completion", {}, selected.deadline, selected.cancel_event)
    assert sender.message["error"]["code"] == "protocol_error"


def test_inline_unknown_operation_and_dispatcher_reconstruction(monkeypatch):
    from clinical_diagnostic.dispatcher import DispatchedAnswer

    with pytest.raises(ValueError, match="unknown"):
        execute_inline("unknown", {})
    monkeypatch.setattr("clinical_diagnostic.dispatcher.WebSocketDispatcher._answer",
                        lambda self, case: DispatchedAnswer("fact", "synthetic-chat"))
    result = execute_inline("dispatcher", {"config": {"token": "synthetic", "ws_uri": "wss://backend.invalid/ws",
                           "http_uri": "https://backend.invalid"}, "case": None})
    assert result.text == "fact"


def _blocking_worker(sender, kind, payload, deadline, cancelled):
    payload["ready"].set()
    payload["block"].wait()


def test_real_spawned_blocking_io_worker_is_joined_on_cancellation():
    context = multiprocessing.get_context("spawn")
    ready, block, selected = context.Event(), context.Event(), control()
    collected = []

    class Receiver:
        def poll(self, timeout):
            assert ready.wait(5), "owned worker did not reach its blocking barrier"
            selected.cancel_event.set()
            return False

        def close(self):
            pass

    class OwnedContext:
        def Pipe(self, duplex):
            receiver, sender = context.Pipe(duplex=duplex)
            receiver.close()
            return Receiver(), sender

        def Process(self, **kwargs):
            process = context.Process(target=_blocking_worker, args=kwargs["args"])

            class OwnedProcess:
                def __getattr__(self, name):
                    return getattr(process, name)

                def close(self):
                    collected.append((process.is_alive(), process.exitcode))
                    process.close()

            return OwnedProcess()

    with pytest.raises(RunCancelled):
        supervise("completion", {"ready": ready, "block": block}, selected, process_context=OwnedContext())
    assert collected and collected[0][0] is False and collected[0][1] is not None
