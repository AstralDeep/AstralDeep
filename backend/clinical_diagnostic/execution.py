"""Supervises concrete synchronous diagnostic I/O in an owned process with bounded
deadline/cancellation cleanup; only bounded JSON results cross back to the runner.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import multiprocessing

from clinical_diagnostic.budget import DeadlineControl, use_control

MAX_RESULT_BYTES = 2 * 1024 * 1024
POLL_SECONDS = 0.05
CLEANUP_SECONDS = 0.5


def execute_inline(kind, payload):
    if kind == "completion":
        from clinical_diagnostic.provider import CompletionConfig, OpenAICompletionProvider
        return OpenAICompletionProvider(CompletionConfig(**payload["config"]))._complete(payload["messages"])
    if kind == "dispatcher":
        from clinical_diagnostic.dispatcher import WebSocketDispatcher
        return WebSocketDispatcher(**payload["config"])._answer(payload["case"])
    raise ValueError("unknown concrete diagnostic operation")


def _worker(sender, kind, payload, deadline, cancel_event):
    try:
        with use_control(DeadlineControl(deadline, cancel_event)):
            value = execute_inline(kind, payload)
        message = {"result": asdict(value)}
    except Exception as exc:
        code = getattr(exc, "code", None)
        message = {"error": {"code": getattr(code, "value", "internal_error"),
                             "detail": getattr(exc, "detail", type(exc).__name__),
                             "retryable": getattr(exc, "retryable", False),
                             "completed_cost": getattr(exc, "completed_cost", None)}}
    try:
        encoded = json.dumps(message, allow_nan=False).encode()
        if len(encoded) > MAX_RESULT_BYTES:
            encoded = b'{"error":{"code":"protocol_error","detail":"worker result exceeds bound"}}'
        sender.send_bytes(encoded)
    finally:
        sender.close()


def supervise(kind, payload, control, *, process_context=None):
    context = process_context or multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    shared_cancel = control.cancel_event
    if type(shared_cancel).__module__ == "threading":
        receiver.close()
        sender.close()
        raise ValueError("concrete I/O requires a process-shared cancellation_event")
    process = context.Process(target=_worker,
                              args=(sender, kind, payload, control.deadline, shared_cancel))
    started = False
    try:
        control.remaining_seconds()
        process.start()
        started = True
        sender.close()
        while True:
            remaining = control.remaining_seconds()
            if receiver.poll(min(POLL_SECONDS, remaining)):
                raw = receiver.recv_bytes(MAX_RESULT_BYTES)
                message = json.loads(raw)
                break
            if not process.is_alive():
                raise RuntimeError("diagnostic worker exited without a result")
    finally:
        try:
            if started:
                if process.is_alive():
                    process.terminate()
                process.join(CLEANUP_SECONDS)
                if process.is_alive():
                    process.kill()
                    process.join(CLEANUP_SECONDS)
                if process.is_alive():
                    raise RuntimeError("diagnostic worker cleanup failed")
            process.close()
        finally:
            sender.close()
            receiver.close()
    if "error" in message:
        from clinical_diagnostic.models import FailureCode
        from clinical_diagnostic.provider import ProducerError
        error = message["error"]
        raise ProducerError(error["detail"], code=FailureCode(error["code"]),
                            retryable=error.get("retryable", False),
                            completed_cost=error.get("completed_cost"))
    if kind == "completion":
        from clinical_diagnostic.provider import ProviderReply
        return ProviderReply(**message["result"])
    from clinical_diagnostic.dispatcher import DispatchedAnswer
    return DispatchedAnswer(**message["result"])
