"""Behaviour tests for the 4 read verbs added in feature 063 (remote-observe-1):
job_status, job_history, list_directory, list_processes — plus their pure parsers.

Transport seam + monkeypatched machine resolution; no DB / SSH / network.
"""
from __future__ import annotations

import json

import pytest

from agents.remote_observe import mcp_tools as obs
from orchestrator.remote_transport import (FakeTransport, MachineTarget, RemoteResult, Verdict,
                                            set_transport)

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


def test_read_job_output_tail_reaches_the_model_tier(monkeypatch):
    # The orchestrator's two-tier rule shows the LLM ONLY `_data`
    # (`_tool_result_to_llm_content`); the CodeBlock in `_ui_components` is
    # render-only. Without the tail in `_data` the model is blind to the very
    # output it was asked to interpret — live-found 2026-07-28: it re-submitted
    # `cat` jobs for content the canvas already showed.
    from orchestrator import remote_jobs
    monkeypatch.setattr(remote_jobs, "get_by_job",
                        lambda db, uid, jid: {"output_path": "/home/me/.astral_jobs/x.out"})
    _fake(command_stdout="=== GPU Status ===\nNo devices were found\n", command_exit=0)
    res = obs.read_job_output(user_id=USER, machine_id="dgx", job_id="5")
    assert "No devices were found" in res["_data"]["tail"]


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


# ── resolution + degraded paths: every outcome maps to the vocabulary ─────────
#
# The verbs never raise at the model: an unknown machine, a missing credential,
# an undecryptable one, a dead transport or unparseable output each become a
# named verdict (FR-034 / SC-011).

class _SeqTransport(FakeTransport):
    """FakeTransport whose run() outcomes advance per call. Each outcome is either
    ``(stdout, exit)`` — a transport-level success — or a bare ``Verdict``, a
    transport-level failure (unreachable/timeout/…) for the legs that must degrade."""

    def __init__(self, outcomes, **kw):
        super().__init__(**kw)
        self._outcomes = list(outcomes)

    def run(self, target, argv, *, timeout, retryable=False):
        nxt = self._outcomes.pop(0) if self._outcomes else ("", 0)
        if isinstance(nxt, Verdict):
            self.calls.append({"op": "run", "argv": list(argv)})
            return RemoteResult(verdict=nxt, machine=target.label,
                                next_action="try again", retryable=retryable)
        self.command_stdout, self.command_exit = nxt[0], nxt[1]
        return super().run(target, argv, timeout=timeout, retryable=retryable)


def _seq(outcomes):
    t = _SeqTransport(outcomes)
    set_transport(t)
    return t


def _rows(res):
    """The first Table's rows inside the rendered component tree."""
    def walk(node):
        if isinstance(node, dict):
            if "rows" in node:
                return node["rows"]
            for v in node.values():
                found = walk(v)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for v in node:
                found = walk(v)
                if found is not None:
                    return found
        return None
    rows = walk(res.get("_ui_components"))
    assert rows is not None, "no Table rendered"
    return rows


_PROC = ("0.10 0.20 0.30 1/2 3\n"
         "7200.00 1000.00\n"
         "MemTotal:       1048576 kB\n"
         "MemAvailable:    524288 kB\n")


def test_sanitize_none_is_the_empty_string():
    assert obs._sanitize(None) == ""


def test_verbs_refuse_without_a_signed_in_principal():
    # No principal → the vocabulary's no-live-human verdict, never a bare
    # permission_denied (SC-011).
    t = _fake()
    for res in (obs.list_machines(), obs.list_queue(machine_id="dgx"),
                obs.job_status(machine_id="dgx", job_id="1"),
                obs.job_history(machine_id="dgx"),
                obs.host_facts(machine_id="dgx"),
                obs.list_directory(machine_id="dgx", path="/tmp"),
                obs.list_processes(machine_id="dgx"),
                obs.read_job_output(machine_id="dgx", output_path="/a.log")):
        assert _verdict(res) == Verdict.UNATTENDED_REFUSED.value
    assert _argvs(t) == []


def test_unnamed_machine_is_an_invalid_argument():
    t = _fake()
    assert _verdict(obs.probe_machine(user_id=USER)) == Verdict.INVALID_ARGUMENT.value
    assert _argvs(t) == []


def test_machine_outside_your_inventory_is_not_found(monkeypatch):
    monkeypatch.setattr("orchestrator.remote_machines.resolve_machine",
                        lambda db, uid, ref: None)
    t = _fake()
    assert _verdict(obs.host_facts(user_id=USER, machine_id="ghost")) == Verdict.NOT_FOUND.value
    assert _argvs(t) == []


@pytest.mark.parametrize("exc,verdict", [
    ("MachineNotFound", Verdict.NOT_FOUND),
    ("CredentialNotConfigured", Verdict.CREDENTIAL_NOT_CONFIGURED),
    ("CredentialUndecryptable", Verdict.CREDENTIAL_UNDECRYPTABLE),
])
def test_credential_failures_map_to_the_vocabulary(monkeypatch, exc, verdict):
    from orchestrator.credential_manager import (CredentialNotConfigured,
                                                 CredentialUndecryptable)
    from orchestrator.remote_machines import MachineNotFound
    klass = {"MachineNotFound": MachineNotFound,
             "CredentialNotConfigured": CredentialNotConfigured,
             "CredentialUndecryptable": CredentialUndecryptable}[exc]

    def _boom(*a, **k):
        raise klass("nope")
    monkeypatch.setattr("orchestrator.remote_machines.build_target", _boom)
    t = _fake()
    res = obs.list_directory(user_id=USER, machine_id="dgx", path="/tmp")
    assert _verdict(res) == verdict.value
    assert _argvs(t) == []   # no connection is opened for an unusable credential


# ── list_machines / probe_machine ────────────────────────────────────────────

def test_list_machines_empty_inventory_says_so(monkeypatch):
    monkeypatch.setattr("orchestrator.remote_machines.list_machines", lambda db, uid: [])
    res = obs.list_machines(user_id=USER)
    assert res["_data"] == {"machines": []}
    assert "no registered machines" in str(res["_ui_components"])


def test_list_machines_renders_typed_sanitised_rows(monkeypatch):
    monkeypatch.setattr("orchestrator.remote_machines.list_machines", lambda db, uid: [
        {"machine_id": "m1", "label": "dgx\x07", "address": "10.0.0.5", "port": 22,
         "os_family": "linux", "role": "cluster", "last_verdict": None}])
    res = obs.list_machines(user_id=USER)
    assert res["_data"]["count"] == 1
    assert res["_data"]["machines"][0]["last_verdict"] is None
    assert _rows(res)[0] == ["dgx", "10.0.0.5:22", "linux", "cluster", "—"]


def test_probe_machine_reports_the_host_key_and_records_the_verdict(monkeypatch):
    recorded = []
    monkeypatch.setattr("orchestrator.remote_machines.record_probe",
                        lambda *a, **k: recorded.append(a))
    t = _fake()
    res = obs.probe_machine(user_id=USER, machine_id="dgx")
    assert res["_data"] == {"verdict": "ok", "authenticated": True}
    assert ["Host key", "SHA256:fake"] in _rows(res)
    assert recorded[0][3] == "ok"
    assert [c["op"] for c in t.calls] == ["probe"]


def test_probe_machine_unreachable_survives_a_failed_verdict_write(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr("orchestrator.remote_machines.record_probe", _boom)
    _fake(reachable=False)
    assert _verdict(obs.probe_machine(user_id=USER,
                                      machine_id="dgx")) == Verdict.UNREACHABLE.value


# ── pure parsers + formatters ────────────────────────────────────────────────

def test_parse_squeue_json_bad_input_is_none():
    assert obs._parse_squeue_json("not json") is None
    assert obs._parse_squeue_json("") is None


def test_parse_squeue_delim_skips_short_lines():
    jobs = obs._parse_squeue_delim("truncated|line\n\n"
                                   "7|gpu|RUNNING|1:00|9:00|1|4|16G|N/A|None\n")
    assert [j["id"] for j in jobs] == ["7"]


def test_fmt_uptime_renders_each_magnitude():
    assert obs._fmt_uptime(273900) == "3d 4h 5m"
    assert obs._fmt_uptime(3661) == "1h 1m"
    assert obs._fmt_uptime(90) == "1m"


def test_fmt_bytes_tops_out_at_tib():
    # The TiB guard always returns, so the loop never falls through.
    assert obs._fmt_bytes(512) == "512.0 B"
    assert obs._fmt_bytes(5 * 1024 ** 5) == "5120.0 TiB"


def test_parse_proc_skips_unparseable_uptime_and_meminfo():
    facts = obs._parse_proc("0.10 0.20 0.30 1/2 3\nnot-a-number 4\n"
                            "MemTotal:       notanumber kB\n"
                            "MemAvailable:    1024 kB\n")
    assert facts["load"] == "0.10 0.20 0.30"
    assert "uptime" not in facts and "mem_total" not in facts
    assert facts["mem_avail"] == "1.0 MiB"


def test_parse_find_skips_short_lines_and_unparseable_sizes():
    entries = obs._parse_find("f\t512\n"                       # too few columns
                              "f\tnot-a-number\t170\todd.txt\n"
                              "f\t10\t171\tgood.txt\n")
    assert [e["name"] for e in entries] == ["odd.txt", "good.txt"]
    assert entries[0]["size_bytes"] is None and entries[1]["size_bytes"] == 10


def test_parse_ps_skips_short_lines_and_unparseable_rss():
    procs = obs._parse_ps("1 me 0.0 0.0 1024\n"                # too few columns
                          "2 me 0.0 0.0 notanumber python\n"
                          "3 me 0.0 0.0 2048 bash\n")
    assert [p["pid"] for p in procs] == ["2", "3"]
    assert procs[0]["rss_bytes"] is None and procs[1]["rss_bytes"] == 2048 * 1024


# ── transport failures inside each leg degrade honestly ──────────────────────

def test_list_queue_transport_failure_is_a_verdict():
    _fake(force_verdict=Verdict.TIMEOUT)
    assert _verdict(obs.list_queue(user_id=USER, machine_id="dgx")) == Verdict.TIMEOUT.value


def test_list_queue_fallback_transport_failure_is_a_verdict():
    # --json refused (old scheduler), then the delimited retry never lands.
    t = _seq([("squeue: unrecognized option", 1), Verdict.UNREACHABLE])
    assert _verdict(obs.list_queue(user_id=USER,
                                   machine_id="dgx")) == Verdict.UNREACHABLE.value
    assert len(_argvs(t)) == 2


def test_host_facts_transport_failure_is_a_verdict():
    _fake(force_verdict=Verdict.AUTH_FAILED)
    assert _verdict(obs.host_facts(user_id=USER,
                                   machine_id="dgx")) == Verdict.AUTH_FAILED.value


def test_host_facts_unreadable_role_skips_the_cluster_gpu_leg(monkeypatch):
    # The GPU leg keys off the inventory row's declared role; if that lookup
    # fails the leg is skipped rather than guessed at (never nvidia-smi).
    calls = {"n": 0}

    def _resolve(db, uid, ref):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("db down")
        return {"machine_id": "m1", "label": "dgx", "role": "cluster"}
    monkeypatch.setattr("orchestrator.remote_machines.resolve_machine", _resolve)
    t = _seq([(_PROC, 0), ("8\n", 0), ("", 0)])
    res = obs.host_facts(user_id=USER, machine_id="dgx")
    assert not any(argv[0] == "sinfo" for argv in _argvs(t))
    assert res["_data"]["uptime"] == "2h 0m"
    assert res["_data"]["omitted"] == ["disk usage"]


# ── job_status / job_history degraded legs ───────────────────────────────────

def test_job_status_live_leg_failure_is_a_verdict():
    _fake(force_verdict=Verdict.TIMEOUT)
    assert _verdict(obs.job_status(user_id=USER, machine_id="dgx",
                                   job_id="7")) == Verdict.TIMEOUT.value


def test_job_status_accounting_leg_failure_is_a_verdict():
    t = _seq([('{"jobs": []}', 0), Verdict.TIMEOUT])
    assert _verdict(obs.job_status(user_id=USER, machine_id="dgx",
                                   job_id="7")) == Verdict.TIMEOUT.value
    assert _argvs(t)[1] == ["sacct", "-j", "7", "--json", "-X"]


def test_job_history_transport_failure_is_a_verdict():
    _fake(force_verdict=Verdict.UNREACHABLE)
    assert _verdict(obs.job_history(user_id=USER,
                                    machine_id="dgx")) == Verdict.UNREACHABLE.value


def test_job_history_unparseable_accounting_is_partial_not_silent():
    _fake(command_stdout="sacct: error: slurm_persist_conn_open failed", command_exit=0)
    res = obs.job_history(user_id=USER, machine_id="dgx")
    assert _verdict(res) == Verdict.PARTIAL.value
    assert "parse" in res["_data"]["next_action"]


def test_job_history_non_numeric_days_falls_back_to_the_default():
    t = _fake(command_stdout='{"jobs": []}', command_exit=0)
    res = obs.job_history(user_id=USER, machine_id="dgx", days="soon")
    assert res["_data"] == {"jobs": 0}
    assert "now-7days" in _argvs(t)[0]
    assert "No jobs in the last 7 day(s)." in str(res["_ui_components"])


def test_job_history_bounds_rows_and_marks_truncation():
    doc = {"jobs": [{"job_id": i, "name": f"j{i}", "state": {"current": ["COMPLETED"]},
                     "time": {"elapsed": 1}, "exit_code": {"return_code": {"number": 0}},
                     "partition": "cpu"} for i in range(201)]}
    _fake(command_stdout=json.dumps(doc), command_exit=0)
    res = obs.job_history(user_id=USER, machine_id="dgx")
    assert res["_data"]["jobs"] == 201
    assert len(_rows(res)) == 200
    assert "Showing 200 of 201" in str(res["_ui_components"])


# ── list_directory / list_processes degraded legs ────────────────────────────

def test_list_directory_transport_failure_is_a_verdict():
    _fake(force_verdict=Verdict.TIMEOUT)
    assert _verdict(obs.list_directory(user_id=USER, machine_id="dgx",
                                       path="/scratch")) == Verdict.TIMEOUT.value


def test_list_directory_empty_listing_is_reported_not_blank():
    _fake(command_stdout="", command_exit=0)
    res = obs.list_directory(user_id=USER, machine_id="dgx", path="/scratch")
    assert res["_data"] == {"entries": 0}
    assert "Empty (or not a directory)." in str(res["_ui_components"])


def test_list_processes_transport_failure_is_a_verdict():
    _fake(force_verdict=Verdict.PERMISSION_DENIED_REMOTE)
    assert _verdict(obs.list_processes(
        user_id=USER, machine_id="dgx")) == Verdict.PERMISSION_DENIED_REMOTE.value


def test_list_processes_no_matches_is_reported_not_blank():
    _fake(command_stdout="", command_exit=0)
    res = obs.list_processes(user_id=USER, machine_id="dgx")
    assert res["_data"] == {"processes": 0}
    assert "No matching processes." in str(res["_ui_components"])


# ── read_job_output degraded legs ────────────────────────────────────────────

def test_read_job_output_unreadable_tracking_row_falls_back_to_the_path(monkeypatch):
    from orchestrator import remote_jobs

    def _boom(db, uid, jid):
        raise RuntimeError("db down")
    monkeypatch.setattr(remote_jobs, "get_by_job", _boom)
    t = _fake(command_stdout="line\n", command_exit=0)
    res = obs.read_job_output(user_id=USER, machine_id="dgx", job_id="5",
                              output_path="/abs/out.log")
    assert res["_data"]["job_id"] == "5"
    assert _argvs(t)[0][-1] == "/abs/out.log"


def test_read_job_output_non_numeric_line_count_falls_back_to_the_default(monkeypatch):
    from orchestrator import remote_jobs
    monkeypatch.setattr(remote_jobs, "get_by_job", lambda db, uid, jid: None)
    t = _fake(command_stdout="line\n", command_exit=0)
    obs.read_job_output(user_id=USER, machine_id="dgx", output_path="/abs/out.log",
                        lines="lots")
    assert _argvs(t)[0] == ["tail", "-n", "200", "/abs/out.log"]


def test_read_job_output_transport_failure_is_a_verdict(monkeypatch):
    from orchestrator import remote_jobs
    monkeypatch.setattr(remote_jobs, "get_by_job", lambda db, uid, jid: None)
    _fake(force_verdict=Verdict.TIMEOUT)
    assert _verdict(obs.read_job_output(user_id=USER, machine_id="dgx",
                                        output_path="/abs/out.log")) == Verdict.TIMEOUT.value


def test_read_job_output_empty_file_says_no_output_yet(monkeypatch):
    from orchestrator import remote_jobs
    monkeypatch.setattr(remote_jobs, "get_by_job", lambda db, uid, jid: None)
    _fake(command_stdout="   \n", command_exit=0)
    res = obs.read_job_output(user_id=USER, machine_id="dgx", output_path="/abs/out.log")
    assert res["_data"] == {"job_id": "", "bytes": 0}
    assert "(no output yet)" in str(res["_ui_components"])
