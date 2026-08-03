"""Feature 063 — the orchestrator-side wiring for remote compute (T-coverage).

The 063 changes inside ``orchestrator.py`` are seams rather than logic: the
always-on job poller loop, its boot launch + shutdown cancel, the
``remote_op_decision`` ui_event route, and the ``FF_REMOTE_COMPUTE``-conditional
safe-seed filter. Each is driven here against doubles — no DB, no SSH, no
sockets, no live boot:

- ``_remote_job_poll_loop`` — a failing pass never kills the loop, and a cancel
  propagates (the poller must not swallow ``CancelledError`` at shutdown);
- ``handle_ui_message`` — ``remote_op_decision`` reaches
  ``remote_confirmation.handle_decision`` with the SERVER-derived user id;
- ``start()`` (driven with a fake self up to a sentinel, so no server is bound)
  — the poller task exists only when the flag is on, and the safe-seed set drops
  ``remote-compute-1`` when it is off (flag-off boot is pre-063-identical);
- ``_trace_frame`` — the marker-file-gated outbound frame tracer, including its
  fail-open behavior (a broken trace must never break a send).
"""
from __future__ import annotations

import asyncio
import json
import os
import types
from types import SimpleNamespace

import pytest

from orchestrator import orchestrator as oo
from orchestrator import remote_confirmation as rc
from orchestrator import remote_jobs as rj
from orchestrator.orchestrator import Orchestrator

_REAL_EXISTS = os.path.exists
_MARKER = "/app/.frame_trace"


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: None)


# ── _remote_job_poll_loop ────────────────────────────────────────────────────

def _poll_loop(fake_self):
    """The REAL loop bound onto a bare double (it only ever touches poll_once)."""
    return types.MethodType(Orchestrator._remote_job_poll_loop, fake_self)()


async def test_poll_loop_runs_passes_and_survives_a_failing_one(monkeypatch):
    calls: list = []
    parked = asyncio.Event()

    async def _poll_once(orch):
        calls.append(orch)
        if len(calls) == 1:
            raise RuntimeError("transport blip")
        parked.set()
        await asyncio.sleep(3600)  # park so the cancel below is deterministic

    monkeypatch.setattr(rj, "poll_once", _poll_once)
    monkeypatch.setattr(oo, "REMOTE_CLUSTER_POLL_INTERVAL_SECONDS", 0.0)

    fake = SimpleNamespace()
    task = asyncio.create_task(_poll_loop(fake))
    await asyncio.wait_for(parked.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The first pass raised; the loop slept and ran a SECOND pass anyway.
    assert len(calls) == 2
    assert calls[0] is fake and calls[1] is fake


async def test_poll_loop_cancel_propagates(monkeypatch):
    entered = asyncio.Event()

    async def _poll_once(orch):
        entered.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(rj, "poll_once", _poll_once)
    monkeypatch.setattr(oo, "REMOTE_CLUSTER_POLL_INTERVAL_SECONDS", 0.0)

    task = asyncio.create_task(_poll_loop(SimpleNamespace()))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    # Re-raised, not swallowed by the "never die on a bad pass" handler —
    # otherwise shutdown would hang on a poller that refuses to stop.
    assert task.cancelled()


# ── remote_op_decision ui_event route ────────────────────────────────────────

class _WS:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, data):
        self.sent.append(data)


def _ui_host(monkeypatch, ws, user_id="u-1"):
    """A double carrying only what handle_ui_message touches before the
    action dispatch: the parsed-frame seam, the session map, the id lookup."""
    async def _record_ws_action(**kwargs):
        return None

    import audit.hooks
    monkeypatch.setattr(audit.hooks, "record_ws_action", _record_ws_action)

    fake = SimpleNamespace(
        ui_sessions={ws: {"sub": user_id, "preferred_username": "tester"}},
        _parsed_ui_frame=Orchestrator._parsed_ui_frame,
        _get_user_id=lambda _ws: user_id,
    )
    fake.handle_ui_message = types.MethodType(Orchestrator.handle_ui_message, fake)
    return fake


async def test_remote_op_decision_routes_to_the_confirmation_handler(monkeypatch):
    seen: list = []

    async def _handle_decision(orch, websocket, user_id, payload):
        seen.append((orch, websocket, user_id, payload))

    monkeypatch.setattr(rc, "handle_decision", _handle_decision)
    ws = _WS()
    fake = _ui_host(monkeypatch, ws)

    await fake.handle_ui_message(ws, json.dumps({
        "type": "ui_event",
        "action": "remote_op_decision",
        "payload": {"proposal_id": "p-1", "decision": "approve"},
    }))

    assert len(seen) == 1
    orch, sock, user_id, payload = seen[0]
    assert orch is fake and sock is ws
    # The acting principal comes from the SERVER's session map, never the frame.
    assert user_id == "u-1"
    assert payload == {"proposal_id": "p-1", "decision": "approve"}


async def test_remote_op_decision_tolerates_an_empty_payload(monkeypatch):
    seen: list = []

    async def _handle_decision(orch, websocket, user_id, payload):
        seen.append(payload)

    monkeypatch.setattr(rc, "handle_decision", _handle_decision)
    ws = _WS()
    fake = _ui_host(monkeypatch, ws)

    await fake.handle_ui_message(ws, json.dumps({
        "type": "ui_event", "action": "remote_op_decision", "payload": {}}))
    assert seen == [{}]


async def test_other_actions_do_not_reach_the_confirmation_handler(monkeypatch):
    called: list = []

    async def _handle_decision(*a, **k):
        called.append(a)

    monkeypatch.setattr(rc, "handle_decision", _handle_decision)
    ws = _WS()
    fake = _ui_host(monkeypatch, ws)

    await fake.handle_ui_message(ws, json.dumps({
        "type": "ui_event", "action": "schedule_decision", "payload": {}}))
    assert called == []


# ── boot wiring: safe-seed filter + poller launch ────────────────────────────

class _Stop(Exception):
    """Sentinel that aborts start() right after the 063 wiring runs."""


class _SeedDB:
    _FIRST_PARTY_PUBLIC_AGENT_IDS = (
        "general-1", "weather-1", "remote-compute-1", "summarizer-1")


async def _drive_start(monkeypatch, *, remote_compute: bool):
    """Run the real ``Orchestrator.start`` on a double until the sentinel.

    Everything before the 063 wiring is stubbed (no posture assert, no DB seed,
    no in-process fleet), and ``_start_phi_warm`` — the first statement after the
    poller launch — raises, so no FastAPI app is built and no port is bound."""
    seeded: list = []

    async def _seed_safe(db, ids):
        seeded.append((db, tuple(ids)))

    async def _noop_loop():
        await asyncio.sleep(3600)

    async def _revoke_once():
        return 0

    from orchestrator import agent_trust, session_store, web_auth
    from shared.feature_flags import flags

    monkeypatch.setattr(session_store, "assert_production_posture", lambda: None)
    monkeypatch.setattr(agent_trust, "seed_safe", _seed_safe)
    monkeypatch.setattr(web_auth, "process_revocation_queue_once", _revoke_once)
    enabled = {"safe_agents": True, "inprocess_agents": False,
               "remote_compute": remote_compute}
    monkeypatch.setattr(flags, "is_enabled", lambda name: enabled.get(name, False))

    def _boom():
        raise _Stop()

    fake = SimpleNamespace(
        history=SimpleNamespace(db=_SeedDB()),
        _jwks_warm_loop=_noop_loop,
        _personal_agent_watchdog_task=None,
        _personal_agent_watchdog_loop=_noop_loop,
        _remote_job_poll_task=None,
        _remote_job_poll_loop=_noop_loop,
        _start_phi_warm=_boom,
    )

    before = asyncio.all_tasks()
    try:
        with pytest.raises(_Stop):
            await types.MethodType(Orchestrator.start, fake)()
    finally:
        # The boot fires long-lived background tasks; none of them ever ran (no
        # await point between their creation and the sentinel), so cancelling
        # here leaves the loop clean.
        stragglers = [t for t in asyncio.all_tasks()
                      if t not in before and t is not asyncio.current_task()]
        for t in stragglers:
            t.cancel()
        await asyncio.gather(*stragglers, return_exceptions=True)
    return fake, seeded


async def test_boot_launches_the_poller_and_seeds_remote_compute_when_enabled(monkeypatch):
    fake, seeded = await _drive_start(monkeypatch, remote_compute=True)

    assert len(seeded) == 1
    assert "remote-compute-1" in seeded[0][1]
    task = fake._remote_job_poll_task
    assert task is not None and task.get_name() == "remote-cluster-job-poller"


def test_shutdown_cancels_the_poller():
    """The serve/shutdown block only runs after uvicorn binds a port, so its
    cancel contract is pinned structurally — the repo's convention for boot
    wiring that cannot be driven hermetically (test_t013_production_wiring_060,
    test_remote_jobs_063)."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(Orchestrator.start)))
    shutdown = "\n".join(
        ast.unparse(stmt)
        for node in ast.walk(tree) if isinstance(node, ast.Try) and node.finalbody
        for stmt in node.finalbody)
    assert "poller = getattr(self, '_remote_job_poll_task', None)" in shutdown
    assert "poller.cancel()" in shutdown
    assert "self._remote_job_poll_task = None" in shutdown


async def test_flag_off_boot_creates_no_poller_and_drops_remote_compute_from_the_seed(monkeypatch):
    fake, seeded = await _drive_start(monkeypatch, remote_compute=False)

    # Byte-identical to the pre-063 fleet: no agent_trust row for the agent...
    assert len(seeded) == 1
    assert "remote-compute-1" not in seeded[0][1]
    assert seeded[0][1] == ("general-1", "weather-1", "summarizer-1")
    # ...and no background task at all.
    assert fake._remote_job_poll_task is None


# ── _trace_frame (marker-gated diagnostic tee) ───────────────────────────────

class _CapturedFile:
    def __init__(self, sink):
        self.sink = sink

    def write(self, chunk):
        self.sink.append(chunk)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_open(sink, *, fail: bool = False):
    def _open(path, mode="r", **kw):
        if fail:
            raise OSError("read-only filesystem")
        sink.append(("open", path, mode))
        return _CapturedFile(sink)
    return _open


def _set_marker(monkeypatch, present: bool):
    def _exists(path, _real=_REAL_EXISTS):
        return present if path == _MARKER else _real(path)
    monkeypatch.setattr(os.path, "exists", _exists)


def test_trace_frame_is_off_without_the_marker(monkeypatch):
    _set_marker(monkeypatch, False)
    sink: list = []
    monkeypatch.setattr(oo, "open", _fake_open(sink), raising=False)

    Orchestrator._trace_frame(None, _WS(), json.dumps({"type": "ui_render"}), ok=True)
    assert sink == []


def test_trace_frame_appends_a_typed_record_when_armed(monkeypatch):
    _set_marker(monkeypatch, True)
    sink: list = []
    monkeypatch.setattr(oo, "open", _fake_open(sink), raising=False)

    ws = _WS()
    Orchestrator._trace_frame(None, ws, json.dumps({"type": "ui_upsert"}), ok=True)

    assert sink[0][0] == "open" and sink[0][1] == "/app/frame_trace.jsonl"
    rec = json.loads(sink[1])
    assert rec["type"] == "ui_upsert"
    assert rec["sock"] == "_WS" and rec["sock_id"] == id(ws)
    assert rec["ok"] is True and rec["error"] == ""


def test_trace_frame_records_an_unparsable_frame_as_unknown_type(monkeypatch):
    _set_marker(monkeypatch, True)
    sink: list = []
    monkeypatch.setattr(oo, "open", _fake_open(sink), raising=False)

    Orchestrator._trace_frame(None, _WS(), "not json at all", ok=False, error="boom")
    rec = json.loads(sink[1])
    assert rec["type"] == "?"
    assert rec["ok"] is False
    assert rec["error"] == "redacted_send_failure"


def test_trace_frame_is_fail_open(monkeypatch):
    _set_marker(monkeypatch, True)
    monkeypatch.setattr(oo, "open", _fake_open([], fail=True), raising=False)

    # A broken tee must never surface to the caller — _safe_send calls this on
    # its success path, so a raise here would break every outbound frame.
    Orchestrator._trace_frame(None, _WS(), json.dumps({"type": "x"}), ok=True)


async def test_safe_send_tees_both_outcomes():
    traced: list = []

    class _Dead:
        async def send(self, data):
            raise ConnectionResetError("closed")

    fake = SimpleNamespace(
        _scope_conversation_transient=lambda ws, data: data,
        _trace_frame=lambda ws, data, *, ok, error="": traced.append((ok, error)),
    )
    send = types.MethodType(Orchestrator._safe_send, fake)

    assert await send(_WS(), '{"type":"ok"}') is True
    assert await send(_Dead(), '{"type":"no"}') is False
    assert [ok for ok, _ in traced] == [True, False]
    assert "ConnectionResetError" in traced[1][1]
