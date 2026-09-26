"""Tests for orchestrator-side remote-compute wiring (orchestrator/orchestrator.py): the
job poll loop's failure survival and cancel propagation, the remote_op_decision
route, boot's flag-gated poller launch, and the marker-gated trace tee.
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
from tests.helpers.remote_plane_runtime import make_remote_plane_source

_REAL_EXISTS = os.path.exists
_MARKER = "/app/.frame_trace"


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: None)


def _poll_loop(fake_self):
    return types.MethodType(Orchestrator._remote_job_poll_loop, fake_self)()


async def test_poll_loop_runs_passes_and_survives_a_failing_one(monkeypatch):
    calls: list = []
    parked = asyncio.Event()

    async def _poll_once(orch):
        calls.append(orch)
        if len(calls) == 1:
            raise RuntimeError("transport blip")
        parked.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(rj, "poll_once", _poll_once)
    monkeypatch.setattr(oo, "REMOTE_CLUSTER_POLL_INTERVAL_SECONDS", 0.0)

    fake = SimpleNamespace()
    task = asyncio.create_task(_poll_loop(fake))
    await asyncio.wait_for(parked.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

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
    assert task.cancelled()


class _WS:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, data):
        self.sent.append(data)


def _ui_host(monkeypatch, ws, user_id="u-1"):
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


class _Stop(Exception):
    pass


async def _drive_start(monkeypatch, *, remote_compute: bool):
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

    def _discard_background(coroutine, *, name):
        del name
        coroutine.close()

    async def _recover_publications():
        return SimpleNamespace(degraded_publication_ids=())

    plane_source = make_remote_plane_source(
        SimpleNamespace(machines={}, credentials={}, jobs={})
    )
    fake = SimpleNamespace(
        runtime_composition=SimpleNamespace(
            start=lambda: None,
            plane=SimpleNamespace(
                runtime=plane_source.plane_runtime,
                repositories=plane_source.plane_repositories,
            ),
        ),
        plane_repository_source=plane_source,
        user_agent_registry=plane_source,
        generated_agent_publication_service=SimpleNamespace(
            recover_once=_recover_publications,
            start=lambda: None,
        ),
        explicit_notes=SimpleNamespace(expiry_loop=_noop_loop),
        _track_startup_background_task=_discard_background,
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
            await types.MethodType(Orchestrator._run_started_server, fake)()
    finally:
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
    assert seeded[0][1] == tuple(
        agent_id for agent_id in oo.FIRST_PARTY_PUBLIC_AGENT_IDS
        if agent_id != "computer-use-1"
    )
    task = fake._remote_job_poll_task
    assert task is not None and task.get_name() == "remote-cluster-job-poller"


def test_shutdown_cancels_the_poller():
    import ast
    import inspect
    import textwrap

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(Orchestrator._close_started_services_once))
    )
    shutdown = ast.unparse(tree)
    assert "'_remote_job_poll_task'" in shutdown
    assert "task.cancel()" in shutdown
    assert "await asyncio.gather(task, return_exceptions=True)" in shutdown
    assert "setattr(self, attribute, None)" in shutdown


async def test_flag_off_boot_creates_no_poller_and_drops_remote_compute_from_the_seed(monkeypatch):
    fake, seeded = await _drive_start(monkeypatch, remote_compute=False)

    assert len(seeded) == 1
    assert "remote-compute-1" not in seeded[0][1]
    assert seeded[0][1] == tuple(
        agent_id
        for agent_id in oo.FIRST_PARTY_PUBLIC_AGENT_IDS
        if agent_id not in ("remote-compute-1", "computer-use-1")
    )
    assert fake._remote_job_poll_task is None


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
