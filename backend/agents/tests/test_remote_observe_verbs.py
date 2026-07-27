"""Behaviour tests for the 4 read verbs added in feature 063 (remote-observe-1):
job_status, job_history, list_directory, list_processes — plus their pure parsers.

Transport seam + monkeypatched machine resolution; no DB / SSH / network.
"""
from __future__ import annotations

import json

import pytest

from agents.remote_observe import mcp_tools as obs
from orchestrator.remote_transport import FakeTransport, MachineTarget, Verdict, set_transport

USER = "user-1"


def _target():
    return MachineTarget(machine_id="m1", label="dgx", address="10.0.0.5", port=22,
                         username="me", cred_type="password", secret="x")


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    obs.register_deps(object(), object())
    monkeypatch.setattr("orchestrator.remote_machines.resolve_machine",
                        lambda db, uid, ref: {"machine_id": "m1", "label": "dgx"})
    monkeypatch.setattr("orchestrator.remote_machines.build_target",
                        lambda db, cm, uid, mid: _target())
    yield
    set_transport(None)


def _fake(**kw):
    t = FakeTransport(**kw)
    set_transport(t)
    return t


def _argvs(t):
    return [c["argv"] for c in t.calls if c["op"] == "run"]


def _verdict(res):
    return (res.get("_data") or {}).get("verdict")


# ── pure parsers ─────────────────────────────────────────────────────────────

def test_parse_sacct_json_extracts_typed_fields():
    doc = {"jobs": [{"job_id": 50, "name": "train", "state": {"current": ["COMPLETED"]},
                     "time": {"elapsed": 120}, "exit_code": {"return_code": {"number": 0}},
                     "partition": "gpu"}]}
    (j,) = obs._parse_sacct_json(json.dumps(doc))
    assert j["id"] == "50" and j["state"] == "COMPLETED" and j["exit_code"] == "0"


def test_parse_sacct_json_bad_input_is_none():
    assert obs._parse_sacct_json("not json") is None


def test_parse_find_rows():
    text = "d\t4096\t1700000000.0\tsubdir\nf\t512\t1700000001.5\tfile.txt\n"
    entries = obs._parse_find(text)
    assert entries[0] == {"type": "dir", "size_bytes": 4096, "mtime": "1700000000", "name": "subdir"}
    assert entries[1]["type"] == "file" and entries[1]["name"] == "file.txt"


def test_parse_ps_rows_convert_rss_to_bytes():
    procs = obs._parse_ps("1234 me 1.5 0.3 20480 python\n5678 me 0.0 0.1 1024 bash\n")
    assert procs[0]["pid"] == "1234" and procs[0]["rss_bytes"] == 20480 * 1024
    assert procs[1]["comm"] == "bash"


# ── job_status ────────────────────────────────────────────────────────────────

def test_job_status_live_queue_hit():
    doc = {"jobs": [{"job_id": 123, "name": "train", "job_state": "RUNNING",
                     "partition": "gpu", "node_count": 2, "state_reason": "None"}]}
    t = _fake(command_stdout=json.dumps(doc), command_exit=0)
    res = obs.job_status(user_id=USER, machine_id="dgx", job_id="123")
    assert res["_data"]["state"] == "RUNNING"
    assert _argvs(t)[0] == ["squeue", "--job", "123", "--json"]


def test_job_status_falls_back_to_accounting_then_not_found():
    t = _fake(command_stdout='{"jobs":[]}', command_exit=0)
    res = obs.job_status(user_id=USER, machine_id="dgx", job_id="123")
    assert _verdict(res) == Verdict.NOT_FOUND.value
    # both the live queue AND the accounting DB were consulted
    assert _argvs(t) == [["squeue", "--job", "123", "--json"], ["sacct", "-j", "123", "--json", "-X"]]


def test_job_status_rejects_non_numeric():
    t = _fake(command_exit=0)
    res = obs.job_status(user_id=USER, machine_id="dgx", job_id="x")
    assert _verdict(res) == Verdict.INVALID_ARGUMENT.value and _argvs(t) == []


# ── job_history ───────────────────────────────────────────────────────────────

def test_job_history_clamps_days_and_queries_sacct():
    doc = {"jobs": [{"job_id": 7, "name": "j", "state": {"current": ["FAILED"]},
                     "time": {"elapsed": 5}, "exit_code": {"return_code": {"number": 1}}}]}
    t = _fake(command_stdout=json.dumps(doc), command_exit=0)
    res = obs.job_history(user_id=USER, machine_id="dgx", days=999)
    assert res["_data"]["days"] == 30  # clamped to the max
    argv = _argvs(t)[0]
    assert argv[0] == "sacct" and "now-30days" in argv


# ── list_directory ──────────────────────────────────────────────────────────

def test_list_directory_lists_entries():
    t = _fake(command_stdout="d\t4096\t1700000000\tsub\nf\t10\t1700000001\ta.txt\n", command_exit=0)
    res = obs.list_directory(user_id=USER, machine_id="dgx", path="/home/me")
    assert res["_data"]["total"] == 2
    argv = _argvs(t)[0]
    assert argv[0:2] == ["find", "/home/me"] and "-maxdepth" in argv


def test_list_directory_rejects_relative_path():
    t = _fake(command_exit=0)
    res = obs.list_directory(user_id=USER, machine_id="dgx", path="relative")
    assert _verdict(res) == Verdict.INVALID_ARGUMENT.value and _argvs(t) == []


# ── list_processes ────────────────────────────────────────────────────────────

def test_list_processes_own_only_uses_username():
    t = _fake(command_stdout="1234 me 1.0 0.5 2048 python\n", command_exit=0)
    res = obs.list_processes(user_id=USER, machine_id="dgx", own_only=True)
    assert res["_data"]["processes"] == 1
    argv = _argvs(t)[0]
    assert argv[0:3] == ["ps", "-u", "me"]  # target.username


def test_list_processes_all_uses_eo():
    t = _fake(command_stdout="1 root 0.0 0.0 100 init\n", command_exit=0)
    obs.list_processes(user_id=USER, machine_id="dgx", own_only=False)
    assert _argvs(t)[0][0:2] == ["ps", "-eo"]


# ── read_job_output (US4 — bounded tail of a tracked job's output) ────────────

def test_read_job_output_from_tracked_job(monkeypatch):
    from orchestrator import remote_jobs
    monkeypatch.setattr(remote_jobs, "get_by_job",
                        lambda db, uid, jid: {"output_path": "/home/me/.astral_jobs/x.out"})
    t = _fake(command_stdout="GPU 0: NVIDIA A100\n", command_exit=0)
    res = obs.read_job_output(user_id=USER, machine_id="dgx", job_id="5")
    assert "A100" in str(res["_ui_components"][0])
    argv = _argvs(t)[0]
    assert argv[0] == "tail" and argv[-1] == "/home/me/.astral_jobs/x.out"


def test_read_job_output_unknown_job_is_not_found(monkeypatch):
    from orchestrator import remote_jobs
    monkeypatch.setattr(remote_jobs, "get_by_job", lambda db, uid, jid: None)
    t = _fake(command_exit=0)
    res = obs.read_job_output(user_id=USER, machine_id="dgx", job_id="5")
    assert _verdict(res) == Verdict.NOT_FOUND.value and _argvs(t) == []


def test_read_job_output_explicit_path(monkeypatch):
    from orchestrator import remote_jobs
    monkeypatch.setattr(remote_jobs, "get_by_job", lambda db, uid, jid: None)
    t = _fake(command_stdout="line1\nline2\n", command_exit=0)
    res = obs.read_job_output(user_id=USER, machine_id="dgx", output_path="/abs/out.log")
    assert _argvs(t)[0][-1] == "/abs/out.log" and "line1" in str(res["_ui_components"][0])
