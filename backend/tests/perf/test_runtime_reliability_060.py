"""Reliability tests for Orchestrator.handle_ui_connection and
handle_ui_connection_fastapi: registration backpressure, drain-on-disconnect
ownership, the reader/mutation barrier, and runtime_registry coherence under load.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import types
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.fspath(BACKEND_ROOT))

pytestmark = pytest.mark.perf

FRAME_COUNT = 1_000
INTERACTIVE_ACTIVE_LIMIT = 20
INTERACTIVE_QUEUE_LIMIT = 100
SHORT_DEADLINE_SECONDS = 0.01
_DISCONNECT = object()


class _FakeSocket:
    def __init__(self, disconnect_error: type[BaseException]) -> None:
        self._disconnect_error = disconnect_error
        self._incoming: asyncio.Queue[object] = asyncio.Queue()
        self.accepted = asyncio.Event()
        self.receiving = asyncio.Event()
        self.server_closed = asyncio.Event()
        self.client_disconnected = asyncio.Event()
        self.sent: list[str] = []
        self.close_code: int | None = None
        self.close_reason: str | None = None

    def feed(self, frame: str) -> None:
        self._incoming.put_nowait(frame)

    def disconnect(self) -> None:
        if not self.client_disconnected.is_set():
            self.client_disconnected.set()
            self._incoming.put_nowait(_DISCONNECT)

    async def accept(self) -> None:
        self.accepted.set()

    async def receive_text(self) -> str:
        self.receiving.set()
        item = await self._incoming.get()
        if item is _DISCONNECT:
            raise self._disconnect_error()
        assert isinstance(item, str)
        return item

    def __aiter__(self) -> _FakeSocket:
        self.receiving.set()
        return self

    async def __anext__(self) -> str:
        item = await self._incoming.get()
        if item is _DISCONNECT:
            raise StopAsyncIteration
        assert isinstance(item, str)
        return item

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        self.close_reason = reason
        self.server_closed.set()
        self.disconnect()

    def payloads(self) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for raw in self.sent:
            try:
                value = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                payloads.append(value)
        return payloads


class _CloseFailureSocket(_FakeSocket):
    async def close(self, code: int = 1000, reason: str = "") -> None:
        raise RuntimeError(f"close failed ({code}, {reason})")


class _Hooks:
    async def emit(self, _context: object) -> None:
        return None


class _Rote:
    def cleanup(self, _websocket: object) -> None:
        return None


class _MessageProbe:
    def __init__(self) -> None:
        self.orchestrator: Any | None = None
        self.registrations = 0
        self.registered = asyncio.Event()
        self.starts: list[str] = []
        self.controls: list[str] = []
        self.terminals: Counter[str] = Counter()
        self.cancellations: Counter[str] = Counter()
        self.active = 0
        self.max_active = 0
        self._started: dict[str, asyncio.Event] = {}
        self._finished: dict[str, asyncio.Event] = {}
        self._release: dict[str, asyncio.Event] = {}
        self._release_everything = False
        self.background_started = asyncio.Event()
        self.background_release = asyncio.Event()
        self.background_task: asyncio.Task[None] | None = None

    def started(self, probe_id: str) -> asyncio.Event:
        return self._started.setdefault(probe_id, asyncio.Event())

    def finished(self, probe_id: str) -> asyncio.Event:
        return self._finished.setdefault(probe_id, asyncio.Event())

    def release(self, probe_id: str) -> None:
        self._release.setdefault(probe_id, asyncio.Event()).set()

    def release_all(self) -> None:
        self._release_everything = True
        for event in self._release.values():
            event.set()

    async def _user_owned_background(self) -> None:
        self.background_started.set()
        await self.background_release.wait()

    async def __call__(self, websocket: object, raw: str) -> None:
        assert self.orchestrator is not None
        frame = json.loads(raw)
        if frame.get("type") == "register_ui":
            self.registrations += 1
            self.orchestrator.ui_sessions[websocket] = {
                "sub": "runtime-reliability-060"
            }
            event = self.orchestrator._registered_events.get(id(websocket))
            if event is not None:
                event.set()
            self.registered.set()
            return

        action = str(frame.get("action") or frame.get("type") or "unknown")
        if action in {"cancel_task", "close"}:
            self.controls.append(action)
            return

        payload = frame.get("payload") or {}
        probe_id = str(payload.get("probe_id", action))
        self.starts.append(probe_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started(probe_id).set()
        try:
            if payload.get("spawn_user_background"):
                self.background_task = asyncio.create_task(
                    self._user_owned_background(),
                    name="t021-user-owned-background",
                )
            if payload.get("block") and not self._release_everything:
                try:
                    await self._release.setdefault(
                        probe_id, asyncio.Event()
                    ).wait()
                except asyncio.CancelledError:
                    self.cancellations[probe_id] += 1
                    if payload.get("stubborn") and self.cancellations[probe_id] == 1:
                        await self._release.setdefault(
                            probe_id, asyncio.Event()
                        ).wait()
                    raise
        finally:
            self.active -= 1
            self.terminals[probe_id] += 1
            self.finished(probe_id).set()


@pytest.fixture
def runtime_module(monkeypatch: pytest.MonkeyPatch):
    import audit.hooks
    import orchestrator.orchestrator as runtime

    async def _no_audit(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(audit.hooks, "record_auth_event", _no_audit)
    monkeypatch.setattr(runtime.flags, "is_enabled", lambda _name: False)
    return runtime


@pytest.fixture(params=("legacy", "fastapi"))
def entrypoint(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _coordinator() -> object:
    from orchestrator.work_admission import (
        AdmissionClass,
        AdmissionClassConfig,
        InMemoryWorkAdmissionRepository,
        WorkAdmissionCoordinator,
    )

    current = datetime(2026, 7, 15, tzinfo=UTC)
    return WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.INTERACTIVE,
                parent_class_name=None,
                active_limit=INTERACTIVE_ACTIVE_LIMIT,
                queue_limit=INTERACTIVE_QUEUE_LIMIT,
                max_wait_ms=5_000,
                config_revision="test-t021",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=lambda: current,
        operation_retention=timedelta(hours=24),
    )


def _orchestrator(runtime: Any, probe: _MessageProbe) -> Any:
    async def _teardown(_websocket: object) -> None:
        return None

    async def _handle(_self: object, websocket: object, raw: str) -> None:
        await probe(websocket, raw)

    orch = runtime.Orchestrator.__new__(runtime.Orchestrator)
    orch.ui_clients = []
    orch.ui_sessions = {}
    orch._registered_events = {}
    orch._connection_contexts = {}
    orch._ws_active_chat = {}
    orch._ws_timeline_mode = {}
    orch._ws_welcome = {}
    orch._chat_locks = {}
    orch._chat_recorders = {}
    orch._agent_host_sockets = {}
    orch._ws_llm_gated = {}
    orch.stream_manager = None
    orch.hooks = _Hooks()
    orch.rote = _Rote()
    orch._cleanup_streams = lambda _websocket: None
    orch._teardown_owner_tunnels = _teardown
    coordinator = _coordinator()
    orch.work_admission = coordinator
    orch.operation_coordinator = coordinator
    orch._work_admission = coordinator
    orch._safe_send = types.MethodType(runtime.Orchestrator._safe_send, orch)
    orch.handle_ui_message = types.MethodType(_handle, orch)
    probe.orchestrator = orch
    return orch


class _RenewalProbe:
    def __init__(self, delegate: object) -> None:
        self.delegate = delegate
        self.renewed = threading.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def renew_execution_lease(self, fence: object) -> object:
        result = self.delegate.renew_execution_lease(fence)
        self.renewed.set()
        return result


class _LeaseFailureProbe:
    def __init__(self, delegate: object, failure: BaseException) -> None:
        self.delegate = delegate
        self.failure = failure
        self.attempted = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def renew_execution_lease(self, _fence: object) -> object:
        self.attempted.set()
        raise self.failure


class _SubmitFailureProbe:
    def __init__(self, delegate: object) -> None:
        self.delegate = delegate
        self.failed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def submit(self, request: object) -> object:
        if not self.failed:
            self.failed = True
            raise RuntimeError("submit probe failure")
        return self.delegate.submit(request)


class _QueryFailureProbe:
    def __init__(self, delegate: object) -> None:
        self.delegate = delegate
        self.failed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def query_operation(self, **kwargs: object) -> object:
        if not self.failed:
            self.failed = True
            raise RuntimeError("projection probe failure")
        return self.delegate.query_operation(**kwargs)


def _socket(runtime: Any) -> _FakeSocket:
    return _FakeSocket(runtime.WebSocketDisconnect)


async def _turns(count: int = 20) -> None:
    for _ in range(count):
        await asyncio.sleep(0)


async def _wait(event: asyncio.Event, label: str, timeout: float = 1.0) -> None:
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except TimeoutError:
        pytest.fail(f"timed out waiting for {label}")


async def _start(
    runtime: Any,
    orch: Any,
    websocket: _FakeSocket,
    selected_entrypoint: str,
) -> asyncio.Task[None]:
    if selected_entrypoint == "fastapi":
        coroutine = runtime.Orchestrator.handle_ui_connection_fastapi(
            orch, websocket
        )
        ready = websocket.accepted
    else:
        coroutine = runtime.Orchestrator.handle_ui_connection(orch, websocket)
        ready = websocket.receiving
    task = asyncio.create_task(coroutine, name=f"t021-{selected_entrypoint}-serve")
    await _wait(ready, f"{selected_entrypoint} socket acceptance")
    return task


async def _cleanup(
    orch: Any,
    websocket: _FakeSocket,
    serve_task: asyncio.Task[None],
    probe: _MessageProbe,
    baseline_tasks: set[asyncio.Task[Any]],
) -> None:
    probe.release_all()
    probe.background_release.set()
    for event in list(getattr(orch, "_registered_events", {}).values()):
        event.set()
    websocket.disconnect()
    await _turns(20)
    if not serve_task.done():
        serve_task.cancel()
    await asyncio.gather(serve_task, return_exceptions=True)
    if probe.background_task is not None:
        await asyncio.gather(probe.background_task, return_exceptions=True)
    current = asyncio.current_task()
    leaked = [
        task
        for task in asyncio.all_tasks() - baseline_tasks
        if task is not current and not task.done()
    ]
    for task in leaked:
        task.cancel()
    if leaked:
        await asyncio.gather(*leaked, return_exceptions=True)


def _set_short_policy(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime,
        "REGISTRATION_TIMEOUT_SECONDS",
        SHORT_DEADLINE_SECONDS,
        raising=False,
    )
    monkeypatch.setattr(
        runtime,
        "CONNECTION_DRAIN_TIMEOUT_SECONDS",
        SHORT_DEADLINE_SECONDS,
        raising=False,
    )
    monkeypatch.setattr(
        runtime, "REGISTRATION_QUEUE_LIMIT", 16, raising=False
    )


def _diagnostics(orch: Any) -> Mapping[str, int]:
    diagnostics = getattr(orch, "connection_diagnostics", None)
    assert callable(diagnostics), (
        "T025 must expose non-sensitive aggregate connection_diagnostics() gauges"
    )
    result = diagnostics()
    assert isinstance(result, Mapping)
    required = {
        "active_connections",
        "tracked_tasks",
        "registration_waiters",
        "preregistration_queued",
    }
    assert required <= set(result), f"missing connection diagnostics: {required - set(result)}"
    assert all(
        isinstance(result[key], int) and not isinstance(result[key], bool)
        and result[key] >= 0
        for key in required
    )
    return result


def _assert_drained(orch: Any) -> None:
    diagnostics = _diagnostics(orch)
    assert diagnostics["active_connections"] == 0
    assert diagnostics["tracked_tasks"] == 0
    assert diagnostics["registration_waiters"] == 0
    assert diagnostics["preregistration_queued"] == 0


def _register_frame(connection_generation: uuid.UUID) -> str:
    return json.dumps(
        {
            "type": "register_ui",
            "capabilities": [],
            "connection_generation": str(connection_generation),
        },
        separators=(",", ":"),
    )


def _event_frame(
    probe_id: str,
    *,
    action: str = "get_history",
    submission_id: uuid.UUID | None = None,
    request_generation: uuid.UUID | None = None,
    block: bool = False,
    stubborn: bool = False,
    spawn_user_background: bool = False,
    text: str | None = None,
    connection_generation: uuid.UUID | None = None,
) -> str:
    submission = str(submission_id or uuid.uuid4())
    request = str(request_generation or uuid.uuid4())
    payload: dict[str, Any] = {
        "probe_id": probe_id,
        "submission_id": submission,
        "request_generation": request,
        "surface": "runtime_probe",
        "block": block,
        "stubborn": stubborn,
        "spawn_user_background": spawn_user_background,
    }
    frame: dict[str, Any] = {
        "type": "ui_event",
        "action": action,
        "submission_id": submission,
        "request_generation": request,
        "payload": payload,
    }
    if connection_generation is not None:
        connection = str(connection_generation)
        frame["connection_generation"] = connection
        payload["connection_generation"] = connection
    if text is not None:
        payload["text"] = text
    return json.dumps(frame, separators=(",", ":"))


def _control_frame(action: str) -> str:
    if action == "close":
        return json.dumps({"type": "close", "code": 1000})
    return json.dumps(
        {
            "type": "ui_event",
            "action": action,
            "payload": {"task_id": str(uuid.uuid4())},
        }
    )


def _codes(payloads: list[dict[str, Any]]) -> list[str]:
    codes: list[str] = []
    for payload in payloads:
        code = payload.get("code")
        if isinstance(code, str):
            codes.append(code)
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            codes.append(error["code"])
    return codes


def _operation_accounting(
    payloads: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    accepted = [
        item
        for item in payloads
        if item.get("type") == "operation_status"
        and item.get("state") == "accepted"
        and item.get("terminal") is False
    ]
    refused = [item for item in payloads if item.get("accepted") is False]
    terminal = [
        item
        for item in payloads
        if item.get("type") == "operation_status" and item.get("terminal") is True
    ]
    return accepted, refused, terminal


def test_connection_policy_defaults_match_spec(runtime_module: Any) -> None:
    assert runtime_module.REGISTRATION_TIMEOUT_SECONDS == 5.0
    assert runtime_module.CONNECTION_DRAIN_TIMEOUT_SECONDS == 5.0
    assert runtime_module.REGISTRATION_QUEUE_LIMIT == 16


def test_connection_frame_helpers_reject_malformed_identity_and_stay_safe(
    runtime_module: Any,
) -> None:
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    orch._connection_contexts = None
    websocket = _socket(runtime_module)
    context = orch._new_connection_context(websocket)

    assert orch._parsed_ui_frame("not-json") is None
    assert orch._parsed_ui_frame("[]") is None
    assert orch._ui_control_kind(None) is None
    assert orch._ui_control_kind({"type": 7}) is None
    assert orch._ui_control_kind({"type": "cancel"}) == "cancel_task"
    assert orch._optional_uuid("not-a-uuid") is None
    assert context is orch._connection_contexts[id(websocket)]

    invalid_submission = {
        "type": "ui_event",
        "action": "get_history",
        "payload": {"submission_id": "invalid"},
    }
    invalid_request = {
        "type": "ui_event",
        "action": "get_history",
        "payload": {
            "submission_id": str(uuid.uuid4()),
            "request_generation": "invalid",
        },
    }
    assert orch._connection_frame(
        context, json.dumps(invalid_submission), invalid_submission
    ) is None
    assert orch._connection_frame(
        context, json.dumps(invalid_request), invalid_request
    ) is None

    submission_id = uuid.uuid4()
    request_generation = uuid.uuid4()
    non_serializable = {
        "type": "ui_event",
        "action": 17,
        "payload": {
            "submission_id": str(submission_id),
            "request_generation": str(request_generation),
            "surface": 9,
        },
        "direct_helper_only": {"not", "json"},
    }
    assert orch._connection_frame(
        context, "fallback", non_serializable
    ) is None
    future_non_ui_frame = {
        "type": "future_frame",
        "action": 17,
        "payload": {
            "submission_id": str(submission_id),
            "request_generation": str(request_generation),
        },
        "direct_helper_only": {"not", "json"},
    }
    frame = orch._connection_frame(context, "fallback", future_non_ui_frame)
    assert frame is not None
    assert frame.action == "connection_frame"
    assert frame.surface is None
    assert frame.normalized_digest
    assert orch._rfc3339(datetime(2026, 7, 15)).endswith("Z")
    assert orch._public_terminal_code("stale_generation") == "stale_generation"
    assert orch._public_terminal_code("unknown_internal") == "operation_failed"


async def test_connection_compatibility_helpers_are_bounded_and_off_loop(
    runtime_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    loop_thread = threading.get_ident()
    orch.work_admission = types.SimpleNamespace(_repository=object())

    observed_thread = await orch._call_work_admission(threading.get_ident)
    assert observed_thread != loop_thread

    close_failure = _CloseFailureSocket(runtime_module.WebSocketDisconnect)
    await orch._close_ui_socket(
        close_failure,
        code=1008,
        reason="expected-test-failure",
    )

    websocket = _socket(runtime_module)
    event = asyncio.Event()
    orch._registered_events[id(websocket)] = event
    handled = asyncio.Event()

    async def _handle(
        _self: object, _websocket: object, _raw: str
    ) -> None:
        handled.set()

    orch.handle_ui_message = types.MethodType(_handle, orch)
    pending = asyncio.create_task(
        orch._safe_handle_ui_message(websocket, _event_frame("compat"))
    )
    await _turns()
    assert not handled.is_set()
    event.set()
    await pending
    assert handled.is_set()

    missing_event_socket = _socket(runtime_module)
    missing_event_context = orch._new_connection_context(
        missing_event_socket
    )
    orch._registered_events.pop(id(missing_event_socket), None)

    async def _register_without_prebuilt_event(
        _self: object, ws: object, _raw: str
    ) -> None:
        orch.ui_sessions[ws] = {"sub": "missing-event"}
        orch._registered_events[id(ws)].set()

    orch.handle_ui_message = types.MethodType(
        _register_without_prebuilt_event, orch
    )
    assert await orch._route_ui_frame(
        missing_event_context,
        _register_frame(uuid.uuid4()),
    )
    assert missing_event_context.registered
    await orch._drain_connection_context(missing_event_context)

    async def _fail(
        _self: object, _websocket: object, _raw: str
    ) -> None:
        raise RuntimeError("compatibility wrapper failure")

    orch.handle_ui_message = types.MethodType(_fail, orch)
    await orch._safe_handle_ui_message(
        websocket, _register_frame(uuid.uuid4())
    )

    context = runtime_module.ConnectionContext(
        websocket=websocket,
        connection_scope_id=uuid.uuid4(),
        registration_deadline=0.0,
        closing=True,
    )
    await orch._drain_connection_context(context)

    monkeypatch.setattr(
        runtime_module,
        "_CONNECTION_CLAIM_POLL_SECONDS",
        0.001,
    )
    waiting_context = runtime_module.ConnectionContext(
        websocket=websocket,
        connection_scope_id=uuid.uuid4(),
        registration_deadline=0.0,
    )
    revision = orch._interactive_capacity_revision
    assert (
        await orch._wait_for_interactive_capacity(
            waiting_context,
            revision,
        )
        == revision
    )


async def test_registration_type_is_structural_not_substring(
    runtime_module: Any,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_short_policy(runtime_module, monkeypatch)
    baseline = set(asyncio.all_tasks())
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    websocket = _socket(runtime_module)
    serve = await _start(runtime_module, orch, websocket, entrypoint)
    disguised = _event_frame(
        "disguised",
        action="chat_message",
        text="register_ui",
    )
    try:
        assert '"register_ui"' in disguised
        websocket.feed(disguised)
        await _turns()
        assert probe.starts == [], "payload text must not bypass registration"

        websocket.feed(_register_frame(uuid.uuid4()))
        await _wait(probe.registered, "structurally valid registration")
        await _wait(probe.finished("disguised"), "queued preregistration frame")
        assert probe.registrations == 1
        assert probe.starts == ["disguised"]

        websocket.feed(_register_frame(uuid.uuid4()))
        for _ in range(50):
            if probe.registrations == 2:
                break
            await asyncio.sleep(0)
        assert probe.registrations == 2
    finally:
        await _cleanup(orch, websocket, serve, probe, baseline)


async def test_malformed_frames_wait_while_transport_controls_bypass_registration(
    runtime_module: Any,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_short_policy(runtime_module, monkeypatch)
    baseline = set(asyncio.all_tasks())
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    websocket = _socket(runtime_module)
    serve = await _start(runtime_module, orch, websocket, entrypoint)
    malformed_type = json.dumps(
        {"type": 7, "payload": {"probe_id": "malformed-type"}}
    )
    try:
        websocket.feed("not-json")
        websocket.feed(malformed_type)
        websocket.feed(json.dumps({"type": "ping"}))
        websocket.feed(json.dumps({"type": "pong"}))
        websocket.feed(json.dumps({"type": "cancel_task"}))
        await _turns()

        assert probe.starts == []
        assert probe.controls == ["cancel_task"]
        assert any(
            payload.get("type") == "pong" for payload in websocket.payloads()
        )

        websocket.feed(_register_frame(uuid.uuid4()))
        await _wait(probe.registered, "registration")
        await _wait(probe.finished("malformed-type"), "malformed type frame")
        for _ in range(100):
            accepted, _refused, terminal = _operation_accounting(
                websocket.payloads()
            )
            if len(terminal) == len(accepted) == 2:
                break
            await asyncio.sleep(0)

        assert probe.starts == ["malformed-type"]
        assert len(accepted) == 2
        assert sorted(item["state"] for item in terminal) == [
            "completed",
            "failed",
        ]
    finally:
        await _cleanup(orch, websocket, serve, probe, baseline)


async def test_preregistration_flood_is_bounded_and_closes_explicitly(
    runtime_module: Any,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_short_policy(runtime_module, monkeypatch)
    baseline = set(asyncio.all_tasks())
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    websocket = _socket(runtime_module)
    serve = await _start(runtime_module, orch, websocket, entrypoint)
    submission_ids = [uuid.uuid4() for _ in range(17)]
    try:
        for index, submission_id in enumerate(submission_ids):
            websocket.feed(
                _event_frame(
                    f"preregister-{index}",
                    submission_id=submission_id,
                )
            )
        await _wait(websocket.server_closed, "registration queue overflow close")
        await asyncio.gather(serve)

        assert probe.starts == []
        refusals = [
            payload
            for payload in websocket.payloads()
            if payload.get("code") == "registration_queue_full"
        ]
        assert [payload["submission_id"] for payload in refusals] == [
            str(submission_id) for submission_id in submission_ids
        ]
        assert all(
            set(payload)
            == {
                "type",
                "submission_id",
                "accepted",
                "code",
                "message",
                "retryable",
                "retry_after_ms",
            }
            and payload["accepted"] is False
            for payload in refusals
        )
        _assert_drained(orch)
    finally:
        await _cleanup(orch, websocket, serve, probe, baseline)


async def test_registration_timeout_terminalizes_every_queued_frame(
    runtime_module: Any,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_short_policy(runtime_module, monkeypatch)
    baseline = set(asyncio.all_tasks())
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    websocket = _socket(runtime_module)
    serve = await _start(runtime_module, orch, websocket, entrypoint)
    queued = 3
    submission_ids = [uuid.uuid4() for _ in range(queued)]
    try:
        for index, submission_id in enumerate(submission_ids):
            websocket.feed(
                _event_frame(
                    f"timeout-{index}",
                    submission_id=submission_id,
                )
            )
        await _wait(websocket.server_closed, "registration deadline close")
        await asyncio.gather(serve)

        assert probe.starts == []
        refusals = [
            payload
            for payload in websocket.payloads()
            if payload.get("code") == "registration_timeout"
        ]
        assert [payload["submission_id"] for payload in refusals] == [
            str(submission_id) for submission_id in submission_ids
        ]
        assert all(payload["accepted"] is False for payload in refusals)
        _assert_drained(orch)
    finally:
        await _cleanup(orch, websocket, serve, probe, baseline)


@pytest.mark.parametrize("failure_mode", ("handler_timeout", "handler_error"))
async def test_registration_handler_must_finish_before_the_absolute_deadline(
    runtime_module: Any,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    _set_short_policy(runtime_module, monkeypatch)
    baseline = set(asyncio.all_tasks())
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    websocket = _socket(runtime_module)
    blocked = asyncio.Event()

    async def _registration_failure(
        _self: object, _websocket: object, _raw: str
    ) -> None:
        if failure_mode == "handler_error":
            raise RuntimeError("registration probe failure")
        await blocked.wait()

    orch.handle_ui_message = types.MethodType(_registration_failure, orch)
    serve = await _start(runtime_module, orch, websocket, entrypoint)
    try:
        websocket.feed(_register_frame(uuid.uuid4()))
        await _wait(websocket.server_closed, "registration handler deadline")
        await asyncio.gather(serve)

        assert "registration_timeout" in _codes(websocket.payloads()) or (
            websocket.close_reason == "registration_timeout"
        )
        _assert_drained(orch)
    finally:
        blocked.set()
        await _cleanup(orch, websocket, serve, probe, baseline)


async def test_mutations_are_fifo_while_cancel_control_bypasses_lane(
    runtime_module: Any,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_short_policy(runtime_module, monkeypatch)
    baseline = set(asyncio.all_tasks())
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    websocket = _socket(runtime_module)
    serve = await _start(runtime_module, orch, websocket, entrypoint)
    try:
        websocket.feed(_register_frame(uuid.uuid4()))
        await _wait(probe.registered, "registration")
        for index in range(3):
            websocket.feed(
                _event_frame(
                    f"mutation-{index}", action="chat_message", block=True
                )
            )

        await _wait(probe.started("mutation-0"), "first mutation")
        await _turns()
        assert probe.starts == ["mutation-0"]

        websocket.feed(_control_frame("cancel_task"))
        for _ in range(50):
            if probe.controls:
                break
            await asyncio.sleep(0)
        assert probe.controls == ["cancel_task"]
        assert not probe.finished("mutation-0").is_set()

        probe.release("mutation-0")
        await _wait(probe.started("mutation-1"), "second mutation")
        assert probe.starts == ["mutation-0", "mutation-1"]
        probe.release("mutation-1")
        await _wait(probe.started("mutation-2"), "third mutation")
        assert probe.starts == ["mutation-0", "mutation-1", "mutation-2"]
        probe.release("mutation-2")
        await _wait(probe.finished("mutation-2"), "third mutation completion")
        assert probe.terminals == Counter(
            {"mutation-0": 1, "mutation-1": 1, "mutation-2": 1}
        )
    finally:
        await _cleanup(orch, websocket, serve, probe, baseline)


async def test_reads_and_mutations_never_overlap_live_connection_state(
    runtime_module: Any,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_short_policy(runtime_module, monkeypatch)
    baseline = set(asyncio.all_tasks())
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    websocket = _socket(runtime_module)
    serve = await _start(runtime_module, orch, websocket, entrypoint)
    try:
        websocket.feed(
            _register_frame(uuid.uuid4())
        )
        await _wait(probe.registered, "registration")

        websocket.feed(
            _event_frame("read-before", action="get_history", block=True)
        )
        await _wait(probe.started("read-before"), "leading read")
        websocket.feed(
            _event_frame("mutation-middle", action="chat_message", block=True)
        )
        await _turns()
        assert not probe.started("mutation-middle").is_set(), (
            "a mutation must wait for readers admitted before it"
        )

        probe.release("read-before")
        await _wait(probe.started("mutation-middle"), "middle mutation")
        websocket.feed(
            _event_frame("read-after", action="get_history", block=True)
        )
        await _turns()
        assert not probe.started("read-after").is_set(), (
            "a read must wait for the mutation admitted before it"
        )

        probe.release("mutation-middle")
        await _wait(probe.started("read-after"), "trailing read")
        probe.release("read-after")
        await _wait(probe.finished("read-after"), "trailing read completion")
        assert probe.starts == [
            "read-before",
            "mutation-middle",
            "read-after",
        ]
    finally:
        await _cleanup(orch, websocket, serve, probe, baseline)


async def test_close_control_bypasses_a_blocked_mutation_and_drains(
    runtime_module: Any,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_short_policy(runtime_module, monkeypatch)
    baseline = set(asyncio.all_tasks())
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    websocket = _socket(runtime_module)
    serve = await _start(runtime_module, orch, websocket, entrypoint)
    try:
        websocket.feed(_register_frame(uuid.uuid4()))
        await _wait(probe.registered, "registration")
        websocket.feed(
            _event_frame("blocked-before-close", action="chat_message", block=True)
        )
        await _wait(probe.started("blocked-before-close"), "blocked mutation")
        websocket.feed(_control_frame("close"))
        await _wait(websocket.server_closed, "structural close control")
        await asyncio.gather(serve)

        assert probe.terminals["blocked-before-close"] == 1
        _assert_drained(orch)
    finally:
        await _cleanup(orch, websocket, serve, probe, baseline)


async def test_exact_duplicate_retry_executes_and_terminalizes_once(
    runtime_module: Any,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_short_policy(runtime_module, monkeypatch)
    baseline = set(asyncio.all_tasks())
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    websocket = _socket(runtime_module)
    serve = await _start(runtime_module, orch, websocket, entrypoint)
    submission_id = uuid.uuid4()
    request_generation = uuid.uuid4()
    duplicate = _event_frame(
        "duplicate",
        action="get_history",
        submission_id=submission_id,
        request_generation=request_generation,
    )
    try:
        websocket.feed(_register_frame(uuid.uuid4()))
        await _wait(probe.registered, "registration")
        websocket.feed(duplicate)
        await _wait(probe.finished("duplicate"), "original terminal")
        websocket.feed(duplicate)
        await _turns(50)

        accepted, refused, terminal = _operation_accounting(websocket.payloads())
        accepted_ids = {item.get("operation_id") for item in accepted}
        terminal_ids = [item.get("operation_id") for item in terminal]
        assert refused == []
        assert len(accepted_ids) == 1
        assert terminal_ids == list(accepted_ids)
        assert probe.starts == ["duplicate"]
        assert probe.terminals == Counter({"duplicate": 1})
    finally:
        await _cleanup(orch, websocket, serve, probe, baseline)


async def test_real_identity_bearing_generic_action_terminalizes_completed(
    runtime_module: Any,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import audit.hooks
    import orchestrator.chrome_events as chrome_events

    _set_short_policy(runtime_module, monkeypatch)
    baseline = set(asyncio.all_tasks())
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    websocket = _socket(runtime_module)
    connection_generation = uuid.uuid4()
    handled = asyncio.Event()

    async def no_audit(**_kwargs: object) -> None:
        return None

    async def handle_generic(
        _orchestrator: object,
        target: object,
        action: str,
        payload: dict[str, Any],
        user_id: str,
    ) -> bool:
        assert target is websocket
        assert action == "future_generic_action"
        assert payload["connection_generation"] == str(connection_generation)
        assert user_id == "runtime-reliability-060"
        handled.set()
        return True

    monkeypatch.setattr(audit.hooks, "record_ws_action", no_audit)
    monkeypatch.setattr(chrome_events, "handle_chrome_event", handle_generic)
    serve = await _start(runtime_module, orch, websocket, entrypoint)
    try:
        websocket.feed(_register_frame(connection_generation))
        await _wait(probe.registered, "registration")
        orch.handle_ui_message = types.MethodType(
            runtime_module.Orchestrator.handle_ui_message, orch
        )
        websocket.feed(
            _event_frame(
                "generic-success",
                action="future_generic_action",
                connection_generation=connection_generation,
            )
        )
        await _wait(handled, "generic chrome handler")
        for _ in range(100):
            accepted, refused, terminal = _operation_accounting(
                websocket.payloads()
            )
            if terminal:
                break
            await asyncio.sleep(0)

        assert refused == []
        assert len(accepted) == len(terminal) == 1
        assert terminal[0]["state"] == "completed"
        assert terminal[0]["request_generation"] == accepted[0][
            "request_generation"
        ]
    finally:
        await _cleanup(orch, websocket, serve, probe, baseline)


@pytest.mark.parametrize("failure_mode", ("unhandled", "raised"))
async def test_real_identity_bearing_generic_action_never_fabricates_completion(
    runtime_module: Any,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    import audit.hooks
    import orchestrator.chrome_events as chrome_events

    _set_short_policy(runtime_module, monkeypatch)
    baseline = set(asyncio.all_tasks())
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    websocket = _socket(runtime_module)
    connection_generation = uuid.uuid4()

    async def no_audit(**_kwargs: object) -> None:
        return None

    async def fail_generic(
        _orchestrator: object,
        _target: object,
        _action: str,
        _payload: dict[str, Any],
        _user_id: str,
    ) -> bool:
        if failure_mode == "raised":
            raise RuntimeError("generic handler probe failure")
        return False

    monkeypatch.setattr(audit.hooks, "record_ws_action", no_audit)
    monkeypatch.setattr(chrome_events, "handle_chrome_event", fail_generic)
    serve = await _start(runtime_module, orch, websocket, entrypoint)
    try:
        websocket.feed(_register_frame(connection_generation))
        await _wait(probe.registered, "registration")
        orch.handle_ui_message = types.MethodType(
            runtime_module.Orchestrator.handle_ui_message, orch
        )
        websocket.feed(
            _event_frame(
                f"generic-{failure_mode}",
                action="future_generic_action",
                connection_generation=connection_generation,
            )
        )
        for _ in range(100):
            accepted, refused, terminal = _operation_accounting(
                websocket.payloads()
            )
            if terminal:
                break
            await asyncio.sleep(0)

        assert refused == []
        assert len(accepted) == len(terminal) == 1
        assert terminal[0]["state"] == "failed"
        assert terminal[0]["error"]["code"] == "operation_failed"
        assert not any(
            frame.get("type") == "operation_status"
            and frame.get("state") == "completed"
            for frame in websocket.payloads()
        )
    finally:
        await _cleanup(orch, websocket, serve, probe, baseline)


async def test_invalid_identity_conflict_ingress_limit_and_closing_are_refused(
    runtime_module: Any,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_short_policy(runtime_module, monkeypatch)
    baseline = set(asyncio.all_tasks())
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    websocket = _socket(runtime_module)
    serve = await _start(runtime_module, orch, websocket, entrypoint)
    submission_id = uuid.uuid4()
    request_generation = uuid.uuid4()
    try:
        websocket.feed(_register_frame(uuid.uuid4()))
        await _wait(probe.registered, "registration")

        websocket.feed(
            json.dumps(
                {
                    "type": "ui_event",
                    "action": "get_history",
                    "payload": {"submission_id": "invalid"},
                }
            )
        )
        missing_identity_submission = uuid.uuid4()
        websocket.feed(
            json.dumps(
                {
                    "type": "ui_event",
                    "action": "get_history",
                    "payload": {},
                }
            )
        )
        websocket.feed(
            json.dumps(
                {
                    "type": "ui_event",
                    "action": "get_history",
                    "payload": {
                        "submission_id": str(missing_identity_submission),
                    },
                }
            )
        )
        websocket.feed(
            json.dumps(
                {
                    "type": "ui_event",
                    "action": "get_history",
                    "payload": {
                        "submission_id": str(uuid.uuid1()),
                        "request_generation": str(uuid.uuid1()),
                    },
                }
            )
        )
        websocket.feed(
            _event_frame("bad-surface").replace(
                '"surface":"runtime_probe"', '"surface":"bad-surface"'
            )
        )
        websocket.feed(
            _event_frame("bad-chat").replace(
                '"surface":"runtime_probe"',
                '"surface":"runtime_probe","chat_id":"not-a-uuid"',
            )
        )
        websocket.feed(
            json.dumps(
                {
                    "type": "ui_event",
                    "action": "get_history",
                    "payload": {
                        "submission_id": str(uuid.uuid4()),
                        "request_generation": "invalid",
                    },
                }
            )
        )
        submission_conflict = json.loads(_event_frame("submission-conflict"))
        submission_conflict["submission_id"] = str(uuid.uuid4())
        websocket.feed(json.dumps(submission_conflict))
        request_conflict = json.loads(_event_frame("request-conflict"))
        request_conflict["request_generation"] = str(uuid.uuid4())
        websocket.feed(json.dumps(request_conflict))

        uppercase_identity = json.loads(_event_frame("uppercase-identity"))
        uppercase_submission = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
        uppercase_request = "BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB"
        uppercase_identity["submission_id"] = uppercase_submission
        uppercase_identity["request_generation"] = uppercase_request
        uppercase_identity["payload"]["submission_id"] = uppercase_submission
        uppercase_identity["payload"]["request_generation"] = uppercase_request
        websocket.feed(json.dumps(uppercase_identity))

        braced_identity = json.loads(_event_frame("braced-identity"))
        braced_submission = "{aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa}"
        braced_request = "{bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb}"
        braced_identity["submission_id"] = braced_submission
        braced_identity["request_generation"] = braced_request
        braced_identity["payload"]["submission_id"] = braced_submission
        braced_identity["payload"]["request_generation"] = braced_request
        websocket.feed(json.dumps(braced_identity))

        uppercase_chat = json.loads(_event_frame("uppercase-chat"))
        uppercase_chat_id = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
        uppercase_chat["session_id"] = uppercase_chat_id
        uppercase_chat["payload"]["chat_id"] = uppercase_chat_id
        websocket.feed(json.dumps(uppercase_chat))

        braced_chat = json.loads(_event_frame("braced-chat"))
        braced_chat_id = "{aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa}"
        braced_chat["session_id"] = braced_chat_id
        braced_chat["payload"]["chat_id"] = braced_chat_id
        websocket.feed(json.dumps(braced_chat))

        first = _event_frame(
            "conflict-original",
            submission_id=submission_id,
            request_generation=request_generation,
        )
        second = _event_frame(
            "conflict-changed",
            submission_id=submission_id,
            request_generation=request_generation,
        )
        websocket.feed(first)
        await _wait(probe.finished("conflict-original"), "original submission")
        websocket.feed(second)

        monkeypatch.setattr(
            runtime_module, "CONNECTION_INGRESS_LIMIT", 0
        )
        websocket.feed(_event_frame("ingress-full"))
        await _turns(50)

        context = orch._connection_contexts[id(websocket)]
        context.closing = True
        closing_frame = _event_frame("closing")
        await orch._enqueue_connection_frame(
            context,
            closing_frame,
            json.loads(closing_frame),
        )
        context.closing = False

        codes = _codes(websocket.payloads())
        assert codes.count("invalid_input") == 13
        assert codes.count("idempotency_conflict") == 1
        assert codes.count("capacity_exceeded") == 1
        assert codes.count("connection_closing") == 1
        assert "conflict-changed" not in probe.starts
        assert "ingress-full" not in probe.starts
        assert "bad-surface" not in probe.starts
        assert "bad-chat" not in probe.starts
        assert "submission-conflict" not in probe.starts
        assert "request-conflict" not in probe.starts
        assert "uppercase-identity" not in probe.starts
        assert "braced-identity" not in probe.starts
        assert "uppercase-chat" not in probe.starts
        assert "braced-chat" not in probe.starts
        assert len(
            [
                payload
                for payload in websocket.payloads()
                if payload.get("type") == "operation_status"
                and payload.get("state") == "accepted"
            ]
        ) == 1
        invalid_refusals = [
            payload
            for payload in websocket.payloads()
            if payload.get("code") == "invalid_input"
        ]
        assert any(
            payload.get("submission_id") == str(missing_identity_submission)
            for payload in invalid_refusals
        )
        admission_refusals = [
            payload
            for payload in websocket.payloads()
            if payload.get("accepted") is False
        ]
        assert admission_refusals
        assert all(
            isinstance(payload.get("submission_id"), str)
            and str(uuid.UUID(payload["submission_id"]))
            == payload["submission_id"]
            and uuid.UUID(payload["submission_id"]).version == 4
            for payload in admission_refusals
        )
        assert not any(
            payload.get("accepted") is False
            and payload.get("submission_id") is None
            for payload in websocket.payloads()
        )
    finally:
        await _cleanup(orch, websocket, serve, probe, baseline)


async def test_admission_failure_is_retryable_without_losing_submission(
    runtime_module: Any,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_short_policy(runtime_module, monkeypatch)
    baseline = set(asyncio.all_tasks())
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    submit_failure = _SubmitFailureProbe(orch.work_admission)
    orch.work_admission = submit_failure
    orch.operation_coordinator = submit_failure
    orch._work_admission = submit_failure
    websocket = _socket(runtime_module)
    submission_id = uuid.uuid4()
    request_generation = uuid.uuid4()
    frame = _event_frame(
        "submit-retry",
        submission_id=submission_id,
        request_generation=request_generation,
    )
    serve = await _start(runtime_module, orch, websocket, entrypoint)
    try:
        websocket.feed(_register_frame(uuid.uuid4()))
        await _wait(probe.registered, "registration")
        websocket.feed(frame)
        for _ in range(100):
            if "operation_failed" in _codes(websocket.payloads()):
                break
            await asyncio.sleep(0)
        assert "operation_failed" in _codes(websocket.payloads())

        websocket.feed(frame)
        await _wait(probe.finished("submit-retry"), "retried submission")
        assert probe.starts == ["submit-retry"]
    finally:
        await _cleanup(orch, websocket, serve, probe, baseline)


async def test_accepted_work_survives_non_authoritative_projection_failure(
    runtime_module: Any,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_short_policy(runtime_module, monkeypatch)
    baseline = set(asyncio.all_tasks())
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    query_failure = _QueryFailureProbe(orch.work_admission)
    orch.work_admission = query_failure
    orch.operation_coordinator = query_failure
    orch._work_admission = query_failure
    websocket = _socket(runtime_module)
    serve = await _start(runtime_module, orch, websocket, entrypoint)
    try:
        websocket.feed(_register_frame(uuid.uuid4()))
        await _wait(probe.registered, "registration")
        websocket.feed(_event_frame("projection-failure"))
        await _wait(probe.finished("projection-failure"), "accepted work")
        for _ in range(100):
            accepted, refused, terminal = _operation_accounting(
                websocket.payloads()
            )
            if terminal:
                break
            await asyncio.sleep(0)

        assert query_failure.failed
        assert len(accepted) == len(terminal) == 1
        assert refused == []
        assert terminal[0]["state"] == "completed"
    finally:
        await _cleanup(orch, websocket, serve, probe, baseline)


async def test_interactive_frame_reuses_connection_owner_and_renews_lease(
    runtime_module: Any,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_short_policy(runtime_module, monkeypatch)
    monkeypatch.setattr(
        runtime_module,
        "CONNECTION_LEASE_RENEW_SECONDS",
        0.001,
        raising=False,
    )
    baseline = set(asyncio.all_tasks())
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    renewal = _RenewalProbe(orch.work_admission)
    orch.work_admission = renewal
    orch.operation_coordinator = renewal
    orch._work_admission = renewal
    websocket = _socket(runtime_module)
    captured: list[dict[str, Any] | None] = []
    chat_started = asyncio.Event()
    chat_release = asyncio.Event()

    async def _chat(
        _self: object,
        _websocket: object,
        _message: str,
        _chat_id: str,
        _display_message: str | None = None,
        **kwargs: object,
    ) -> None:
        authority = kwargs.get("operation_context")
        captured.append(authority if isinstance(authority, dict) else None)
        chat_started.set()
        await chat_release.wait()

    async def _handle(_self: object, ws: object, raw: str) -> None:
        frame = json.loads(raw)
        if frame.get("type") == "register_ui":
            await probe(ws, raw)
            return
        await runtime_module.Orchestrator._serialized_chat(
            orch,
            ws,
            "lease probe",
            "lease-chat",
            None,
        )

    orch.handle_chat_message = types.MethodType(_chat, orch)
    orch.handle_ui_message = types.MethodType(_handle, orch)
    serve = await _start(runtime_module, orch, websocket, entrypoint)
    try:
        websocket.feed(_register_frame(uuid.uuid4()))
        await _wait(probe.registered, "registration")
        websocket.feed(_event_frame("lease-owner", action="chat_message"))
        await _wait(chat_started, "connection-owned chat")
        renewed = await asyncio.wait_for(
            asyncio.to_thread(renewal.renewed.wait, 0.5),
            timeout=1.0,
        )

        assert renewed
        assert len(captured) == 1
        authority = captured[0]
        assert authority is not None
        operation = authority["operation"]
        owner = authority["owner"]
        fence = authority["execution_fence"]
        context = orch._connection_contexts[id(websocket)]
        assert operation.operation_id == fence.operation_id
        assert owner.connection_scope_id == context.connection_scope_id
        assert owner.owner_user_id is None

        chat_release.set()
        for _ in range(100):
            _accepted, _refused, terminal = _operation_accounting(
                websocket.payloads()
            )
            if terminal:
                break
            await asyncio.sleep(0)
        assert len(terminal) == 1
        assert terminal[0]["state"] == "completed"
    finally:
        chat_release.set()
        await _cleanup(orch, websocket, serve, probe, baseline)


@pytest.mark.parametrize("failure_kind", ("stale", "error"))
async def test_lease_loss_cancels_worker_and_terminalizes_retryable(
    runtime_module: Any,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    _set_short_policy(runtime_module, monkeypatch)
    monkeypatch.setattr(
        runtime_module,
        "CONNECTION_LEASE_RENEW_SECONDS",
        0.001,
    )
    baseline = set(asyncio.all_tasks())
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    failure: BaseException
    if failure_kind == "stale":
        failure = runtime_module.StaleExecutionFenceError("stale probe")
    else:
        failure = RuntimeError("renewal probe failure")
    lease_failure = _LeaseFailureProbe(orch.work_admission, failure)
    orch.work_admission = lease_failure
    orch.operation_coordinator = lease_failure
    orch._work_admission = lease_failure
    websocket = _socket(runtime_module)
    chat_started = asyncio.Event()
    chat_release = asyncio.Event()

    async def _chat(
        _self: object,
        _websocket: object,
        _message: str,
        _chat_id: str,
        _display_message: str | None = None,
        **_kwargs: object,
    ) -> None:
        chat_started.set()
        await chat_release.wait()

    async def _handle(_self: object, ws: object, raw: str) -> None:
        frame = json.loads(raw)
        if frame.get("type") == "register_ui":
            await probe(ws, raw)
            return
        await runtime_module.Orchestrator._serialized_chat(
            orch, ws, "lease loss", "lease-loss-chat", None
        )

    orch.handle_chat_message = types.MethodType(_chat, orch)
    orch.handle_ui_message = types.MethodType(_handle, orch)
    serve = await _start(runtime_module, orch, websocket, entrypoint)
    try:
        websocket.feed(_register_frame(uuid.uuid4()))
        await _wait(probe.registered, "registration")
        websocket.feed(_event_frame("lease-loss", action="chat_message"))
        await _wait(chat_started, "lease-loss worker")
        await _wait(lease_failure.attempted, "lease renewal failure")
        for _ in range(100):
            _accepted, _refused, terminal = _operation_accounting(
                websocket.payloads()
            )
            if terminal:
                break
            await asyncio.sleep(0)

        assert len(terminal) == 1
        assert terminal[0]["state"] == "retryable"
        assert terminal[0]["error"]["code"] == "stale_generation"
    finally:
        chat_release.set()
        await _cleanup(orch, websocket, serve, probe, baseline)


async def test_thousand_read_only_frames_are_bounded_and_fully_accounted(
    runtime_module: Any,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_short_policy(runtime_module, monkeypatch)
    baseline = set(asyncio.all_tasks())
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    websocket = _socket(runtime_module)
    serve = await _start(runtime_module, orch, websocket, entrypoint)
    try:
        websocket.feed(_register_frame(uuid.uuid4()))
        await _wait(probe.registered, "registration")
        for index in range(FRAME_COUNT):
            websocket.feed(
                _event_frame(f"read-{index}", action="get_history", block=True)
            )
        await _turns(100)

        live = _diagnostics(orch)
        assert live["active_connections"] == 1
        assert probe.max_active <= INTERACTIVE_ACTIVE_LIMIT
        assert live["tracked_tasks"] <= (
            INTERACTIVE_ACTIVE_LIMIT + INTERACTIVE_QUEUE_LIMIT
        )

        probe.release_all()
        for _ in range(500):
            accepted, refused, terminal = _operation_accounting(
                websocket.payloads()
            )
            if len(accepted) + len(refused) == FRAME_COUNT and len(terminal) == len(
                accepted
            ):
                break
            await asyncio.sleep(0)

        accepted, refused, terminal = _operation_accounting(websocket.payloads())
        assert len(accepted) + len(refused) == FRAME_COUNT, "no frame may disappear"
        assert len(accepted) == (
            INTERACTIVE_ACTIVE_LIMIT + INTERACTIVE_QUEUE_LIMIT
        )
        assert len(refused) == FRAME_COUNT - (
            INTERACTIVE_ACTIVE_LIMIT + INTERACTIVE_QUEUE_LIMIT
        )
        assert probe.max_active == INTERACTIVE_ACTIVE_LIMIT
        assert all(item.get("code") == "capacity_exceeded" for item in refused)
        accepted_ids = [item.get("operation_id") for item in accepted]
        terminal_ids = [item.get("operation_id") for item in terminal]
        assert len(set(accepted_ids)) == len(accepted_ids)
        assert Counter(terminal_ids) == Counter({item: 1 for item in accepted_ids})
        assert len(probe.starts) == len(accepted)
        assert all(count == 1 for count in probe.terminals.values())

        websocket.disconnect()
        await asyncio.wait_for(asyncio.shield(serve), timeout=1.0)
        _assert_drained(orch)
    finally:
        await _cleanup(orch, websocket, serve, probe, baseline)


async def test_disconnect_drains_connection_work_but_not_user_background(
    runtime_module: Any,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_short_policy(runtime_module, monkeypatch)
    baseline = set(asyncio.all_tasks())
    probe = _MessageProbe()
    orch = _orchestrator(runtime_module, probe)
    websocket = _socket(runtime_module)
    serve = await _start(runtime_module, orch, websocket, entrypoint)
    try:
        websocket.feed(_register_frame(uuid.uuid4()))
        await _wait(probe.registered, "registration")
        websocket.feed(
            _event_frame(
                "launch-user-background",
                action="chat_message",
                spawn_user_background=True,
            )
        )
        await _wait(probe.background_started, "user-owned background work")
        websocket.feed(
            _event_frame(
                "connection-owned",
                action="get_history",
                block=True,
                stubborn=True,
            )
        )
        await _wait(probe.started("connection-owned"), "connection-owned work")

        websocket.disconnect()
        await asyncio.wait_for(asyncio.shield(serve), timeout=1.0)

        assert probe.cancellations["connection-owned"] >= 1
        assert probe.terminals["connection-owned"] == 1
        assert probe.background_task is not None
        assert not probe.background_task.done(), (
            "disconnect must not cancel user-owned background work"
        )
        _assert_drained(orch)
    finally:
        await _cleanup(orch, websocket, serve, probe, baseline)


def test_ten_thousand_runtime_registry_interleavings_are_coherent() -> None:
    from orchestrator.runtime_registry import (
        RegistryKind,
        RuntimeRegistry,
        RuntimeRegistryRecord,
    )

    registry = RuntimeRegistry()
    kinds = (
        RegistryKind.RUNTIME,
        RegistryKind.HOST_SESSION,
        RegistryKind.LIFECYCLE,
        RegistryKind.CARD,
    )
    writer_count = len(kinds)
    reader_count = 2
    publications_per_writer = 2_500
    start = threading.Barrier(writer_count + reader_count)
    finished = threading.Event()
    finish_lock = threading.Lock()
    writers_remaining = writer_count

    def writer(writer_number: int) -> None:
        nonlocal writers_remaining
        kind = kinds[writer_number]
        identity = f"registry-profile-{writer_number}"
        start.wait(timeout=10)
        try:
            for revision in range(publications_per_writer):
                registry.register(
                    RuntimeRegistryRecord(
                        kind=kind,
                        identity=identity,
                        state_revision=revision,
                        value={
                            "writer": writer_number,
                            "revision": revision,
                        },
                    ),
                    expected_state_revision=(
                        None if revision == 0 else revision - 1
                    ),
                )
        finally:
            with finish_lock:
                writers_remaining -= 1
                if writers_remaining == 0:
                    finished.set()

    def reader() -> int:
        start.wait(timeout=10)
        prior_version = 0
        observations = 0
        while not finished.is_set() or observations < 256:
            snapshot = registry.snapshot()
            assert snapshot.registry_version >= prior_version
            prior_version = snapshot.registry_version
            partitions = (
                (RegistryKind.RUNTIME, snapshot.runtimes),
                (RegistryKind.HOST_SESSION, snapshot.host_sessions),
                (RegistryKind.LIFECYCLE, snapshot.lifecycles),
                (RegistryKind.CARD, snapshot.cards),
            )
            for kind, records in partitions:
                identities = tuple(record.identity for record in records)
                assert identities == tuple(sorted(identities))
                assert len(identities) == len(set(identities))
                for record in records:
                    assert record.kind is kind
                    assert record.value["revision"] == record.state_revision
            observations += 1
            if observations % 64 == 0:
                time.sleep(0)
        return observations

    started = time.perf_counter()
    with ThreadPoolExecutor(
        max_workers=writer_count + reader_count,
        thread_name_prefix="registry-profile",
    ) as executor:
        writers = [executor.submit(writer, index) for index in range(writer_count)]
        readers = [executor.submit(reader) for _ in range(reader_count)]
        for future in writers:
            future.result(timeout=30)
        observations = [future.result(timeout=30) for future in readers]
    duration_seconds = time.perf_counter() - started

    final = registry.snapshot()
    expected_publications = writer_count * publications_per_writer
    assert expected_publications == 10_000
    assert final.registry_version == expected_publications
    for kind in kinds:
        records = final.records(kind)
        assert len(records) == 1
        assert records[0].state_revision == publications_per_writer - 1
        assert records[0].value["revision"] == publications_per_writer - 1
    assert all(count >= 256 for count in observations)
    print(
        "US6 registry profile: "
        f"publications={expected_publications} readers={reader_count} "
        f"observations={sum(observations)} "
        f"final_registry_version={final.registry_version} "
        f"duration_seconds={duration_seconds:.3f}"
    )


async def test_release_load_maintenance_and_process_work_preserves_latency() -> None:
    from orchestrator.bounded_work import BoundedWorkExecutor
    from shared.process_supervision import (
        ProcessOwner,
        ProcessSupervisor,
        TerminationReason,
    )

    executor = BoundedWorkExecutor(
        name="maintenance_profile",
        max_workers=2,
        queue_limit=0,
    )
    maintenance_release = threading.Event()
    maintenance_started = (threading.Event(), threading.Event())
    supervisor = ProcessSupervisor()
    process_count = 8
    children = []
    maintenance_tasks: list[asyncio.Task[None]] = []

    def blocking_maintenance(index: int) -> None:
        maintenance_started[index].set()
        if not maintenance_release.wait(timeout=10):
            raise TimeoutError("maintenance release profile timed out")

    try:
        for index in range(process_count):
            children.append(
                supervisor.spawn(
                    process_id=uuid.uuid4(),
                    owner=ProcessOwner(
                        owner_kind="release_load_profile",
                        owner_id=f"child-{index}",
                    ),
                    argv=(
                        sys.executable,
                        "-u",
                        "-c",
                        "import time; time.sleep(30)",
                    ),
                )
            )
        maintenance_tasks = [
            asyncio.create_task(executor.run(blocking_maintenance, index))
            for index in range(len(maintenance_started))
        ]
        for _ in range(1_000):
            if all(event.is_set() for event in maintenance_started):
                break
            await asyncio.sleep(0)
        assert all(event.is_set() for event in maintenance_started)
        assert executor.in_flight == 2
        assert all(child.poll() is None for child in children)

        async def acknowledge() -> float:
            began = time.perf_counter()
            await asyncio.sleep(0)
            return time.perf_counter() - began

        latencies = await asyncio.gather(
            *(acknowledge() for _ in range(100))
        )
        ordered = sorted(latencies)
        p95_seconds = ordered[94]
        maximum_seconds = ordered[-1]
        within_two_seconds = sum(value <= 2.0 for value in latencies)
        print(
            "US6 release-load latency profile: "
            f"acknowledgements={len(latencies)} "
            f"within_2s={within_two_seconds} "
            f"p95_ms={p95_seconds * 1_000:.3f} "
            f"max_ms={maximum_seconds * 1_000:.3f} "
            f"maintenance_workers={len(maintenance_started)} "
            f"supervised_processes={process_count}"
        )
        assert within_two_seconds >= 95
        assert p95_seconds <= 2.0
        assert maximum_seconds <= 5.0
    finally:
        maintenance_release.set()
        if maintenance_tasks:
            await asyncio.gather(*maintenance_tasks, return_exceptions=True)
        snapshots = supervisor.terminate_all(reason=TerminationReason.QUIT)
        assert len(snapshots) == len(children)
        assert all(snapshot.process_tree_terminated for snapshot in snapshots)
        assert all(snapshot.readers_joined for snapshot in snapshots)
        assert all(snapshot.pipes_closed for snapshot in snapshots)
