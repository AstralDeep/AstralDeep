"""Checks remote-compute classification, refusal auditing and ordinary tool dispatch. Attended and unattended requests exercise the gate against an isolated transport."""

from __future__ import annotations


import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import remote_confirmation as rc
from orchestrator.chain_authority import MACHINE_TURN_CLASSES
from orchestrator.remote_transport import FakeTransport, MachineTarget, Verdict, set_transport
from tests.helpers.remote_plane_runtime import make_remote_confirmation_plane_source

USER = "user-1"
class _FakeDB:
    def __init__(self):
        self.rows: dict = {}


class _Rec:
    def __init__(self):
        self.calls = []

    async def __call__(self, *a, **k):
        self.calls.append((a, k))


class _WS:
    def __init__(self, **attrs):
        for k, v in attrs.items():
            setattr(self, k, v)


def _owner_socket(orch):
    socket = _WS()
    orch.ui_sessions[socket] = {"sub": USER}
    return socket


def _orch(db):
    source = make_remote_confirmation_plane_source(db)
    return SimpleNamespace(
        plane_repository_source=source,
        runtime_composition=SimpleNamespace(
            plane=SimpleNamespace(
                runtime=source.plane_runtime,
                repositories=source.plane_repositories,
            )
        ),
        credential_manager=object(),
        ui_sessions={},
        send_ui_render=_Rec(),
        execute_single_tool=_Rec(),
    )


@pytest.fixture(autouse=True)
def _reset_transport():
    yield
    set_transport(None)


def test_classification_for_known_and_unknown():
    assert rc.classification_for("remove_path") == "always"
    assert rc.classification_for("not_a_verb") is None


def test_is_destructive_never_always_by_action():
    o = _orch(_FakeDB())
    assert rc._is_destructive(o, USER, "make_directory", {}, "never") is False
    assert rc._is_destructive(o, USER, "remove_path", {}, "always") is True
    by = {"by_action": ["stop", "disable", "restart"]}
    assert rc._is_destructive(o, USER, "control_service", {"action": "restart"}, by) is True
    assert rc._is_destructive(o, USER, "control_service", {"action": "start"}, by) is False


def test_is_destructive_if_exists_uses_a_readonly_stat(monkeypatch):
    tgt = MachineTarget(machine_id="m1", label="dgx", address="10.0.0.5", port=22,
                        username="me", cred_type="password", secret="x")
    monkeypatch.setattr("orchestrator.remote_machines.build_target", lambda *a, **k: tgt)
    o = _orch(_FakeDB())
    set_transport(FakeTransport(files={"/there.txt": b"x"}))
    assert rc._is_destructive(o, USER, "upload_file", {"machine_id": "m1", "remote_path": "/there.txt"}, "if_exists") is True
    assert rc._is_destructive(o, USER, "upload_file", {"machine_id": "m1", "remote_path": "/absent.txt"}, "if_exists") is False


def test_is_destructive_if_exists_fails_closed_when_stat_errors(monkeypatch):
    tgt = MachineTarget(machine_id="m1", label="dgx", address="10.0.0.5", port=22,
                        username="me", cred_type="password", secret="x")
    monkeypatch.setattr("orchestrator.remote_machines.build_target", lambda *a, **k: tgt)
    o = _orch(_FakeDB())
    set_transport(FakeTransport(force_verdict=Verdict.UNREACHABLE))
    assert rc._is_destructive(o, USER, "upload_file", {"machine_id": "m1", "remote_path": "/x"}, "if_exists") is True


def test_evaluate_read_verb_on_merged_agent_never_gates():
    o = _orch(_FakeDB())
    for verb in ("list_queue", "job_status", "host_facts", "list_processes"):
        assert rc.evaluate(o, _owner_socket(o), "remote-compute-1", verb,
                           {"machine_id": "m"}, "chat", USER) is None


def test_evaluate_non_destructive_verb_proceeds():
    o = _orch(_FakeDB())
    assert rc.evaluate(o, _owner_socket(o), "remote-compute-1", "make_directory",
                       {"machine_id": "m", "path": "/tmp/x"}, "chat", USER) is None


def test_evaluate_other_agent_is_ignored():
    o = _orch(_FakeDB())
    assert rc.evaluate(o, object(), "general-1", "remove_path",
                       {"machine_id": "m", "path": "/x"}, "chat", USER) is None


def test_evaluate_unattended_refuses_and_creates_no_proposal():
    db = _FakeDB()
    o = _orch(db)
    out = rc.evaluate(o, None, "remote-compute-1", "remove_path",
                      {"machine_id": "m", "path": "/data"}, "chat", USER)
    assert out is not None
    msg, _ = out
    assert "unattended_refused" in msg
    assert db.rows == {}


class _FakeRecorder:
    def __init__(self):
        self.events = []

    def record_blocking(self, ev):
        self.events.append(ev)
        return ev

    async def record(self, ev):
        self.events.append(ev)
        return ev


@pytest.fixture
def audit_rec(monkeypatch):
    rec = _FakeRecorder()
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: rec)
    return rec


def _types(rec):
    return [e.action_type for e in rec.events]


def test_audit_unattended_refusal_is_recorded_as_failure(audit_rec):
    rc.evaluate(_orch(_FakeDB()), None, "remote-compute-1", "remove_path",
                {"machine_id": "m", "path": "/data"}, "chat", USER)
    assert _types(audit_rec) == ["remote_op.refused_unattended"]
    assert audit_rec.events[0].outcome == "failure"


def test_audit_unattended_refusal_recorded_for_non_destructive_verb(audit_rec):
    rc.evaluate(_orch(_FakeDB()), None, "remote-compute-1", "submit_job",
                {"machine_id": "m", "script": "echo hi"}, "chat", USER)
    assert _types(audit_rec) == ["remote_op.refused_unattended"]
    assert audit_rec.events[0].outcome == "failure"
    assert audit_rec.events[0].inputs_meta.get("verb") == "submit_job"


_TGT = MachineTarget(machine_id="m1", label="dgx", address="10.0.0.5", port=22,
                     username="me", cred_type="password", secret="x")


def _fake(**kw):
    t = FakeTransport(**kw)
    set_transport(t)
    return t


def _destructive_ops(t):
    return [c for c in t.calls if c["op"] in ("run", "put_file")]


def _tc(name, args):
    return SimpleNamespace(id=f"tc-{name}", function=SimpleNamespace(
        name=name, arguments=json.dumps(args)))


_FIRST_CALL_CASES = {
    "remove_path": {"machine_id": "m1", "path": "/data", "recursive": True},
    "cancel_job": {"machine_id": "m1", "job_id": "7"},
    "signal_process": {"machine_id": "m1", "pid": 4242, "signal": "TERM"},
    "control_service": {"machine_id": "m1", "service_name": "nginx", "action": "stop"},
    "manage_package": {"machine_id": "m1", "package_name": "vim", "action": "remove"},
    "upload_file": {"machine_id": "m1", "remote_path": "/exists.txt"},
}

_NON_DESTRUCTIVE_CALL_CASES = {
    "make_directory": {"machine_id": "m1", "path": "/tmp/new"},
    "submit_job": {"machine_id": "m1", "script": "echo hi"},
    "run_job": {"machine_id": "m1", "script": "nvidia-smi"},
    "upload_file": {"machine_id": "m1", "remote_path": "/absent.txt"},
    "control_service": {"machine_id": "m1", "service_name": "nginx", "action": "start"},
    "manage_package": {"machine_id": "m1", "package_name": "vim", "action": "install"},
}

_MUTATING_SWEEP = list(_FIRST_CALL_CASES.items()) + list(_NON_DESTRUCTIVE_CALL_CASES.items())


def test_first_call_matrix_covers_every_gated_verb():
    gated = {v for v, c in rc.DESTRUCTIVE_CLASSIFICATION.items() if c != "never"}
    assert set(_FIRST_CALL_CASES) == gated


def test_mutating_sweep_covers_every_registry_verb():
    assert {v for v, _ in _MUTATING_SWEEP} == set(rc.DESTRUCTIVE_CLASSIFICATION)


@pytest.mark.parametrize("verb", sorted(_FIRST_CALL_CASES))
def test_each_destructive_verb_requires_consent_without_storing_a_proposal(verb, monkeypatch):
    monkeypatch.setattr("orchestrator.remote_machines.build_target", lambda *a, **k: _TGT)
    t = _fake(files={"/exists.txt": b"x"})
    db = _FakeDB()
    o = _orch(db)
    out = rc.evaluate(o, _owner_socket(o), "remote-compute-1", verb,
                      dict(_FIRST_CALL_CASES[verb]), "chat", USER)
    assert out is not None and "attended authorization" in out[0]
    assert db.rows == {}
    assert all(component["type"] == "alert" for component in out[1])
    assert _destructive_ops(t) == []
    assert o.execute_single_tool.calls == []


@pytest.fixture
def real_orch(monkeypatch):
    from orchestrator.orchestrator import Orchestrator

    proposal_storage = _FakeDB()
    source = make_remote_confirmation_plane_source(proposal_storage)
    o = Orchestrator.__new__(Orchestrator)
    o.proposal_storage = proposal_storage
    o.plane_repository_source = source
    o.runtime_composition = SimpleNamespace(
        plane=SimpleNamespace(
            runtime=source.plane_runtime,
            repositories=source.plane_repositories,
        )
    )
    o.security_flags = {}
    o.ui_sessions = {}
    o.agent_cards = {}
    o.send_ui_render = AsyncMock()
    o.tool_permissions = MagicMock()
    o.tool_permissions.is_tool_allowed.return_value = True
    from agents.remote_compute.mcp_tools import TOOL_REGISTRY
    o.tool_permissions.get_tool_scope_map.return_value = {
        name: entry["scope"] for name, entry in TOOL_REGISTRY.items()
    }
    o._record_hop_audit = AsyncMock()
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: None)
    return o


async def test_each_destructive_verb_in_turn_still_gates(real_orch):
    t = _fake()
    results = []
    for tool, args in (("remove_path", {"machine_id": "m", "path": "/data"}),
                       ("cancel_job", {"machine_id": "m", "job_id": "7"})):
        results.append(await real_orch.execute_single_tool(
            _owner_socket(real_orch), _tc(tool, args), {tool: "remote-compute-1"}, "chat-1", user_id=USER))
    assert len(results) == 2
    for r in results:
        assert r.error and "attended authorization" in r.error["message"]
        assert r.ui_components is None
    rendered = [call.args[1] for call in real_orch.send_ui_render.await_args_list]
    assert all(component["type"] == "alert" for group in rendered for component in group)
    assert real_orch.proposal_storage.rows == {}
    assert _destructive_ops(t) == []


async def test_chained_hop_reaching_destructive_verb_still_gates(real_orch):
    t = _fake()
    parent = {"sub": USER, "scopes": ["tools:write"], "depth": 0,
              "act": {"sub": "agent:summarizer-1"}}
    resp = await real_orch.execute_single_tool(
        _owner_socket(real_orch), _tc("remove_path", {"machine_id": "m", "path": "/data"}),
        {"remove_path": "remote-compute-1"}, "chat-1", user_id=USER,
        parent_token=parent, initiating_agent_id="summarizer-1")
    assert resp.error and "attended authorization" in resp.error["message"]
    real_orch._record_hop_audit.assert_awaited_once()
    kw = real_orch._record_hop_audit.await_args.kwargs
    assert kw["operation"] == "mint" and kw["outcome"] == "failure"
    assert real_orch.proposal_storage.rows == {}
    assert _destructive_ops(t) == []


@pytest.mark.parametrize("mclass", MACHINE_TURN_CLASSES)
def test_machine_turn_every_mutating_verb_refused_before_any_transport_contact(mclass, monkeypatch):
    monkeypatch.setattr("orchestrator.remote_machines.build_target", lambda *a, **k: _TGT)
    t = _fake(files={"/exists.txt": b"x"})
    db = _FakeDB()
    o = _orch(db)
    ws = _WS(machine_claims={"machine_class": mclass})
    for verb, args in _MUTATING_SWEEP:
        out = rc.evaluate(o, ws, "remote-compute-1", verb, dict(args), "chat", USER)
        assert out is not None and "unattended_refused" in out[0], verb
    assert db.rows == {}
    assert t.calls == []
    assert o.execute_single_tool.calls == []


def test_machine_turn_upload_refused_before_the_if_exists_probe(monkeypatch):
    monkeypatch.setattr("orchestrator.remote_machines.build_target", lambda *a, **k: _TGT)
    t = _fake(files={"/exists.txt": b"x"})
    db = _FakeDB()
    o = _orch(db)
    ws = _WS(machine_claims={"machine_class": MACHINE_TURN_CLASSES[0]})
    for path in ("/exists.txt", "/absent.txt"):
        out = rc.evaluate(o, ws, "remote-compute-1", "upload_file",
                          {"machine_id": "m1", "remote_path": path}, "chat", USER)
        assert out is not None and "unattended_refused" in out[0]
    assert db.rows == {}
    assert t.calls == []
    assert t.files["/exists.txt"] == b"x"


def test_attended_non_destructive_mutating_verbs_still_proceed(monkeypatch):
    monkeypatch.setattr("orchestrator.remote_machines.build_target", lambda *a, **k: _TGT)
    t = _fake()
    db = _FakeDB()
    o = _orch(db)
    for verb, args in _NON_DESTRUCTIVE_CALL_CASES.items():
        assert rc.evaluate(o, _owner_socket(o), "remote-compute-1", verb,
                           dict(args), "chat", USER) is None, verb
    assert db.rows == {}
    assert [c["op"] for c in t.calls] == ["stat"]
    assert _destructive_ops(t) == []


def test_machine_turn_read_verbs_still_pass_this_gate():
    t = _fake()
    db = _FakeDB()
    o = _orch(db)
    ws = _WS(machine_claims={"machine_class": MACHINE_TURN_CLASSES[0]})
    for verb in ("list_queue", "job_status", "host_facts", "list_processes"):
        assert rc.evaluate(o, ws, "remote-compute-1", verb,
                           {"machine_id": "m"}, "chat", USER) is None
    assert db.rows == {} and t.calls == []


@pytest.mark.parametrize("bound_authority", [False, True])
async def test_machine_turn_refused_even_with_full_scope_grant(real_orch, bound_authority):
    t = _fake()
    ws = _WS()
    real_orch.ui_sessions[ws] = {"machine_class": "scheduled_job"}
    real_orch.tool_permissions.get_tool_scope.return_value = "tools:write"
    if bound_authority:
        real_orch.ui_sessions[ws].update(
            sub=USER, _machine_authority_scopes=("tools:write",),
            _machine_authority_agent="remote-compute-1")
    resp = await real_orch.execute_single_tool(
        ws, _tc("remove_path", {"machine_id": "m", "path": "/data"}),
        {"remove_path": "remote-compute-1"}, "chat-1", user_id=USER)
    if bound_authority:
        assert resp.error and "unattended_refused" in resp.error["message"]
        real_orch.tool_permissions.is_tool_allowed.assert_called_once()
    else:
        assert resp.error == {
            "message": "Tool 'remove_path' is outside this unattended task's approved "
                       "permissions. Approve a new schedule to change its authority.",
            "retryable": False,
        }
        real_orch.tool_permissions.is_tool_allowed.assert_not_called()
    assert real_orch.proposal_storage.rows == {}
    assert t.calls == []


@pytest.mark.parametrize("bound_authority", [False, True])
async def test_machine_turn_submit_refused_even_with_full_scope_grant(real_orch, bound_authority):
    t = _fake()
    ws = _WS()
    real_orch.ui_sessions[ws] = {"machine_class": "scheduled_job"}
    real_orch.tool_permissions.get_tool_scope.return_value = "tools:write"
    if bound_authority:
        real_orch.ui_sessions[ws].update(
            sub=USER, _machine_authority_scopes=("tools:write",),
            _machine_authority_agent="remote-compute-1")
    resp = await real_orch.execute_single_tool(
        ws, _tc("submit_job", {"machine_id": "m", "script": "echo hi"}),
        {"submit_job": "remote-compute-1"}, "chat-1", user_id=USER)
    if bound_authority:
        assert resp.error and "unattended_refused" in resp.error["message"]
        real_orch.tool_permissions.is_tool_allowed.assert_called_once()
    else:
        assert resp.error == {
            "message": "Tool 'submit_job' is outside this unattended task's approved "
                       "permissions. Approve a new schedule to change its authority.",
            "retryable": False,
        }
        real_orch.tool_permissions.is_tool_allowed.assert_not_called()
    assert real_orch.proposal_storage.rows == {}
    assert t.calls == []


class _BoomRecorder:
    def record_blocking(self, ev):
        raise RuntimeError("audit sink down")

    async def record(self, ev):
        raise RuntimeError("audit sink down")


class TestDestructiveClassificationEdges:
    def test_if_exists_fails_closed_when_the_machine_cannot_be_resolved(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("credential undecryptable")
        monkeypatch.setattr("orchestrator.remote_machines.build_target", _boom)
        set_transport(FakeTransport())
        assert rc._is_destructive(_orch(_FakeDB()), USER, "upload_file",
                                  {"machine_id": "m1", "remote_path": "/x"}, "if_exists") is True

    def test_unknown_classification_fails_closed(self):
        assert rc._is_destructive(_orch(_FakeDB()), USER, "weird_verb", {}, "sometimes") is True


@pytest.mark.parametrize("state", ["pending", "approved", "consumed"])
async def test_historical_proposal_does_not_authorize_or_replay_an_effect(real_orch, state):
    row = {"owner_user_id": USER, "verb": "cancel_job", "status": state}
    real_orch.proposal_storage.rows["previous"] = dict(row)
    real_orch._execute_with_retry_audited = AsyncMock()
    result = await real_orch.execute_single_tool(
        _WS(), _tc("cancel_job", {"machine_id": "machine", "job_id": "7", "_remote_op_proposal_id": "previous"}),
        {"cancel_job": "remote-compute-1"}, "chat-1", user_id=USER,
    )
    assert result.error and result.error["retryable"] is False
    assert real_orch.proposal_storage.rows == {"previous": row}
    real_orch._execute_with_retry_audited.assert_not_awaited()


def test_unattended_refusal_survives_audit_sink_failure(monkeypatch):
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: _BoomRecorder())
    result = rc.evaluate(_orch(_FakeDB()), None, "remote-compute-1", "remove_path",
                         {"machine_id": "machine", "path": "/data"}, "chat", USER)
    assert result and "unattended_refused" in result[0]


@pytest.mark.parametrize("registration", [None, {"sub": "foreign"}, {"sub": USER, "machine_class": "scheduler"},
                                            {"sub": USER, "_invocation_channel": "a2a"}])
def test_unregistered_foreign_and_machine_sockets_cannot_satisfy_attendance(registration):
    orch = _orch(_FakeDB())
    socket = _WS()
    if registration is not None:
        orch.ui_sessions[socket] = registration
    result = rc.evaluate(orch, socket, "remote-compute-1", "submit_job",
                         {"machine_id": "machine", "script": "echo test"}, "chat", USER)
    assert result[0].startswith("unattended_refused")
    assert orch.execute_single_tool.calls == []


def test_only_a_registered_owner_socket_reaches_the_attended_refusal(audit_rec):
    orch = _orch(_FakeDB())
    result = rc.evaluate(orch, _owner_socket(orch), "remote-compute-1", "cancel_job",
                         {"machine_id": "machine", "job_id": "7"}, "chat", USER)
    assert result[1][0]["type"] == "alert"
    assert _types(audit_rec) == ["remote_op.refused"]
    event, = audit_rec.events
    assert event.outcome == "failure" and event.inputs_meta["verb"] == "cancel_job"
