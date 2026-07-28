"""T031 (US2) — read-verb shape tests for remote-compute's observe library.

list_queue / job_status / job_history parse CANNED, realistic Slurm ``--json``
payloads (squeue --me --json / sacct --json, Slurm 23.02 envelope with dict-typed
numbers and list-typed states) into the typed fields the verbs emit; the delimited
``-o`` fallback for a pre---json scheduler maps onto the SAME typed shape.
host_facts / list_directory / list_processes return typed, bounded,
control-char-sanitised fields with truncation visibly marked (SC-002/SC-008).

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


class SequencedTransport(FakeTransport):
    """FakeTransport whose run() outcomes advance per call: (stdout, exit[, stderr])."""

    def __init__(self, outcomes, **kw):
        super().__init__(**kw)
        self._outcomes = list(outcomes)

    def run(self, target, argv, *, timeout, retryable=False):
        if self._outcomes:
            nxt = self._outcomes.pop(0)
            self.command_stdout, self.command_exit = nxt[0], nxt[1]
            self.command_stderr = nxt[2] if len(nxt) > 2 else ""
        return super().run(target, argv, timeout=timeout, retryable=retryable)


def _seq(outcomes):
    t = SequencedTransport(outcomes)
    set_transport(t)
    return t


def _argvs(t):
    return [c["argv"] for c in t.calls if c["op"] == "run"]


def _verdict(res):
    return (res.get("_data") or {}).get("verdict")


def _rows(res):
    """The first Table's rows inside the rendered component tree."""
    def walk(node):
        if isinstance(node, dict):
            if "rows" in node:
                return node["rows"]
            for v in node.values():
                r = walk(v)
                if r is not None:
                    return r
        if isinstance(node, list):
            for v in node:
                r = walk(v)
                if r is not None:
                    return r
        return None
    rows = walk(res.get("_ui_components"))
    assert rows is not None, "no Table rendered"
    return rows


# ── canned Slurm --json payloads (23.02 envelope) ─────────────────────────────

_META = {"plugin": {"type": "openapi/v0.0.39", "name": "Slurm OpenAPI v0.0.39"},
         "Slurm": {"version": {"major": 23, "micro": 8, "minor": 2}, "release": "23.02.8"}}

SQUEUE_JSON = json.dumps({
    "meta": _META,
    "jobs": [
        {"account": "lab", "job_id": 4242, "name": "train-llm", "user_name": "me",
         "job_state": ["RUNNING"], "partition": "gpu",
         "node_count": {"set": True, "infinite": False, "number": 2},
         "cpus": {"set": True, "infinite": False, "number": 16},
         "state_reason": "None", "standard_output": "/home/me/slurm-4242.out",
         "submit_time": {"set": True, "infinite": False, "number": 1721000000}},
        {"account": "lab", "job_id": 4243, "name": "preprocess", "user_name": "me",
         "job_state": ["PENDING"], "partition": "cpu",
         "node_count": {"set": True, "infinite": False, "number": 1},
         "state_reason": "Priority"},
    ],
    "warnings": [], "errors": [],
})

SACCT_JSON = json.dumps({
    "meta": _META,
    "jobs": [
        {"job_id": 4100, "name": "finetune", "partition": "gpu",
         "state": {"current": ["COMPLETED"], "reason": "None"},
         "exit_code": {"status": ["SUCCESS"],
                       "return_code": {"set": True, "infinite": False, "number": 0}},
         "time": {"elapsed": 3600, "start": 1720000000, "end": 1720003600}},
        {"job_id": 4101, "name": "bad-run", "partition": "cpu",
         "state": {"current": ["FAILED"]},
         "exit_code": {"status": ["FAILURE"],
                       "return_code": {"set": True, "infinite": False, "number": 1}},
         "time": {"elapsed": 42, "start": 1720000100, "end": 1720000142}},
    ],
})


# ── list_queue: canned --json → typed fields ──────────────────────────────────

def test_list_queue_parses_canned_squeue_json():
    t = _fake(command_stdout=SQUEUE_JSON, command_exit=0)
    res = obs.list_queue(user_id=USER, machine_id="dgx")
    assert res["_data"]["jobs"] == 2
    rows = _rows(res)
    # dict-typed node_count and list-typed job_state land as plain typed cells
    assert rows[0] == ["4242", "train-llm", "RUNNING", "gpu", "2", "None"]
    assert rows[1][2] == "PENDING" and rows[1][4] == "1" and rows[1][5] == "Priority"
    assert _argvs(t) == [["squeue", "--me", "--json"]]


def test_list_queue_bounds_and_sanitises_job_name():
    doc = {"jobs": [{"job_id": 1, "name": "N" * 80 + "\x1b[31mX\x07", "job_state": ["RUNNING"],
                     "partition": "gpu", "node_count": 1, "state_reason": ""}]}
    _fake(command_stdout=json.dumps(doc), command_exit=0)
    res = obs.list_queue(user_id=USER, machine_id="dgx")
    cell = _rows(res)[0][1]
    assert cell.endswith("…") and len(cell) == 41  # 40-char bound, truncation marked
    assert "\x1b" not in cell and "\x07" not in cell


def test_list_queue_bounds_rows_and_marks_truncation():
    doc = {"jobs": [{"job_id": i, "name": f"j{i}", "job_state": ["PENDING"], "partition": "cpu",
                     "node_count": 1, "state_reason": ""} for i in range(201)]}
    _fake(command_stdout=json.dumps(doc), command_exit=0)
    res = obs.list_queue(user_id=USER, machine_id="dgx")
    assert res["_data"]["jobs"] == 201
    assert len(_rows(res)) == 200
    assert "Showing 200 of 201" in str(res["_ui_components"])


# ── list_queue / job_status: delimited -o fallback, same typed shape ──────────

def test_list_queue_delimited_fallback_maps_to_same_shape():
    t = _seq([("", 1, "squeue: unrecognized option '--json'"),
              ("4242|gpu|RUNNING|10:03|1:59:57|2|16|32G|gpu:2|None\n"
               "4243|cpu|PENDING|0:00|4:00:00|1|4|8G|N/A|(Priority)\n", 0)])
    res = obs.list_queue(user_id=USER, machine_id="dgx")
    assert _argvs(t) == [["squeue", "--me", "--json"],
                         ["squeue", "--me", "--noheader", "-o", obs._SQUEUE_FALLBACK_FMT]]
    assert res["_data"]["jobs"] == 2
    rows = _rows(res)
    # same 6-column typed shape; the pinned format has no name column → ""
    assert rows[0] == ["4242", "", "RUNNING", "gpu", "2", "None"]
    assert rows[1][2] == "PENDING" and rows[1][5] == "(Priority)"


def test_job_status_live_leg_uses_fallback_on_old_scheduler():
    t = _seq([("", 1, "squeue: unrecognized option '--json'"),
              ("77|gpu|RUNNING|1:00|9:00|1|4|16G|N/A|None\n", 0)])
    res = obs.job_status(user_id=USER, machine_id="dgx", job_id="77")
    assert res["_data"] == {"job_id": "77", "state": "RUNNING"}
    assert _argvs(t)[1] == ["squeue", "--job", "77", "--noheader", "-o", obs._SQUEUE_FALLBACK_FMT]


def test_list_queue_fallback_garbage_is_partial_not_silent():
    _seq([("Usage: squeue [OPTIONS]", 1), ("Usage: squeue [OPTIONS]", 1)])
    res = obs.list_queue(user_id=USER, machine_id="dgx")
    assert _verdict(res) == Verdict.PARTIAL.value
    assert "parse" in res["_data"]["next_action"]


def test_list_queue_empty_fallback_is_an_empty_queue():
    _seq([("", 1), ("", 0)])
    res = obs.list_queue(user_id=USER, machine_id="dgx")
    assert res["_data"] == {"jobs": 0}


# ── job_status: accounting leg parses canned sacct --json ─────────────────────

def test_job_status_finished_job_from_canned_sacct_json():
    t = _seq([('{"jobs": []}', 0), (SACCT_JSON, 0)])
    res = obs.job_status(user_id=USER, machine_id="dgx", job_id="4100")
    assert res["_data"] == {"job_id": "4100", "state": "COMPLETED"}
    rows = _rows(res)
    assert ["Elapsed", "3600"] in rows and ["Exit code", "0"] in rows
    assert _argvs(t) == [["squeue", "--job", "4100", "--json"],
                         ["sacct", "-j", "4100", "--json", "-X"]]


# ── job_history: canned sacct --json → typed fields ───────────────────────────

def test_job_history_parses_canned_sacct_json():
    t = _fake(command_stdout=SACCT_JSON, command_exit=0)
    res = obs.job_history(user_id=USER, machine_id="dgx", days=7)
    assert res["_data"] == {"jobs": 2, "days": 7}
    rows = _rows(res)
    assert rows[0] == ["4100", "finetune", "COMPLETED", "3600", "0", "gpu"]
    assert rows[1][2] == "FAILED" and rows[1][4] == "1"
    argv = _argvs(t)[0]
    assert argv[0] == "sacct" and "--json" in argv and "now-7days" in argv


# ── host_facts: typed, bounded, sanitised (SC-002/SC-008) ─────────────────────

_PROC = ("0.52 0.48 0.45 2/1234 99999\n"          # /proc/loadavg
         "273900.42 1090000.00\n"                  # /proc/uptime
         "MemTotal:       131072000 kB\n"
         "MemFree:         4194304 kB\n"
         "MemAvailable:   65536000 kB\n")

# df -B1 --output=target,size,used,avail: header + rows + malformed lines the
# parser must skip (short row, non-numeric fields, control chars in a mount name).
_DF = ("Mounted on            1B-blocks         Used        Avail\n"
       "/                  105089261568  57544186368  42171953152\n"
       "/scratch          1099511627776 549755813888 549755813888\n"
       "/mnt/e\x1bvil\x07         1000         500          500\n"
       "garbage\n"
       "/net abc def ghi\n")


def _all_tables(res):
    """Every Table's rows in render order (host_facts now emits several tables)."""
    tables = []

    def walk(node):
        if isinstance(node, dict):
            if "rows" in node:
                tables.append(node["rows"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(res.get("_ui_components"))
    return tables


def test_host_facts_returns_typed_fields():
    t = _seq([(_PROC, 0), ("64\n", 0), (_DF, 0)])
    res = obs.host_facts(user_id=USER, machine_id="dgx")
    d = res["_data"]
    assert d["load"] == "0.52 0.48 0.45"
    assert d["uptime"] == "3d 4h 5m"
    assert d["mem_total"] == "125.0 GiB" and d["mem_avail"] == "62.5 GiB"
    assert d["cpus"] == "64"
    assert _argvs(t) == [["cat", "/proc/loadavg", "/proc/uptime", "/proc/meminfo"], ["nproc"],
                         ["df", "-B1", "--output=target,size,used,avail"]]


def test_host_facts_sanitises_and_bounds_cpu_count():
    _seq([(_PROC, 0), ("6\x074" + "9" * 40 + "\n", 0), (_DF, 0)])
    res = obs.host_facts(user_id=USER, machine_id="dgx")
    cpus = res["_data"]["cpus"]
    assert "\x07" not in cpus
    assert cpus.endswith("…") and len(cpus) == 17  # 16-char field bound, marked


def test_host_facts_disk_facts_typed_and_malformed_lines_skipped():
    _seq([(_PROC, 0), ("64\n", 0), (_DF, 0)])
    res = obs.host_facts(user_id=USER, machine_id="dgx")
    d = res["_data"]
    # header + the two malformed lines are dropped, never guessed at
    assert d["disks_total"] == 3 and d["disks_truncated"] is False
    assert d["disks"][0] == {"mount": "/", "size_bytes": 105089261568,
                             "used_bytes": 57544186368, "avail_bytes": 42171953152,
                             "use_pct": 54.8}
    assert d["disks"][1]["mount"] == "/scratch" and d["disks"][1]["use_pct"] == 50.0
    # mount names are untrusted remote strings → sanitised (SC-002)
    assert d["disks"][2]["mount"] == "/mnt/evil"
    assert "omitted" not in d
    disk_rows = _all_tables(res)[1]
    assert disk_rows[0] == ["/", "97.9 GiB", "53.6 GiB", "39.3 GiB", "55%"]
    # non-cluster role → no sinfo leg, no gpus key
    assert "gpus" not in d


def test_host_facts_disk_rows_bounded_and_marked():
    listing = "Mounted on 1B-blocks Used Avail\n" + "".join(
        f"/mnt/vol{i} 1000 500 500\n" for i in range(60))
    _seq([(_PROC, 0), ("64\n", 0), (listing, 0)])
    res = obs.host_facts(user_id=USER, machine_id="dgx")
    d = res["_data"]
    assert d["disks_shown"] == 50 and d["disks_total"] == 60 and d["disks_truncated"] is True
    assert len(_all_tables(res)[1]) == 50
    assert "Showing 50 of 60 mounts" in str(res["_ui_components"])


def test_host_facts_df_failure_is_partial_with_noted_omission():
    _seq([(_PROC, 0), ("64\n", 0),
          ("df: unrecognized option '--output'", 1)])
    res = obs.host_facts(user_id=USER, machine_id="dgx")
    d = res["_data"]
    # working facts still stand; the gap is named, not silent (FR-034)
    assert d["load"] == "0.52 0.48 0.45" and d["cpus"] == "64"
    assert "disks" not in d
    assert d["omitted"] == ["disk usage"]
    assert "Unavailable right now: disk usage" in str(res["_ui_components"])


def _cluster_role(monkeypatch):
    monkeypatch.setattr("orchestrator.remote_machines.resolve_machine",
                        lambda db, uid, ref: {"machine_id": "m1", "label": "dgx",
                                              "role": "cluster"})


def test_host_facts_cluster_role_parses_sinfo_gres(monkeypatch):
    _cluster_role(monkeypatch)
    sinfo = ("gpu*|gpu:a100:4(S:0-1)\n"          # typed + socket-affinity suffix
             "batch|gpu:8,shard:a100:16\n"       # untyped count + non-gpu gres
             "cpu|(null)\n"                       # no GPUs on this partition
             "broken line without delimiter\n")
    t = _seq([(_PROC, 0), ("64\n", 0), (_DF, 0), (sinfo, 0)])
    res = obs.host_facts(user_id=USER, machine_id="dgx")
    assert res["_data"]["gpus"] == [
        {"node_or_partition": "gpu", "gpu_type": "a100", "count": 4},
        {"node_or_partition": "batch", "gpu_type": "", "count": 8},
    ]
    # the GPU leg is sinfo GRES — NEVER nvidia-smi on the queried host (contract)
    assert _argvs(t)[3] == ["sinfo", "--noheader", "-o", "%P|%G"]
    assert not any("nvidia-smi" in a for argv in _argvs(t) for a in argv)
    gpu_rows = _all_tables(res)[2]
    assert gpu_rows == [["gpu", "a100", "4"], ["batch", "gpu", "8"]]


def test_host_facts_cluster_all_null_gres_is_real_zero_not_omission(monkeypatch):
    _cluster_role(monkeypatch)
    _seq([(_PROC, 0), ("64\n", 0), (_DF, 0), ("cpu*|(null)\n", 0)])
    res = obs.host_facts(user_id=USER, machine_id="dgx")
    d = res["_data"]
    assert d["gpus"] == []           # a known GPU-less cluster, not a failure
    assert "omitted" not in d


def test_host_facts_sinfo_failure_notes_gpu_omission(monkeypatch):
    _cluster_role(monkeypatch)
    _seq([(_PROC, 0), ("64\n", 0), (_DF, 0),
          ("sinfo: command not found", 127)])
    res = obs.host_facts(user_id=USER, machine_id="dgx")
    d = res["_data"]
    assert "gpus" not in d
    assert d["disks_total"] == 3     # disk facts unaffected
    assert d["omitted"] == ["GPU inventory"]
    assert "Unavailable right now: GPU inventory" in str(res["_ui_components"])


def test_host_facts_plain_role_never_runs_sinfo():
    t = _seq([(_PROC, 0), ("64\n", 0), (_DF, 0)])
    obs.host_facts(user_id=USER, machine_id="dgx")
    assert not any(argv[0] == "sinfo" for argv in _argvs(t))


# ── list_directory: typed entries, sanitised names, bounded rows ──────────────

def test_list_directory_sanitises_control_and_escape_bytes():
    _fake(command_stdout="f\t512\t1700000000.0\tinno\x1bcent\x07.txt\n"
                         "d\t4096\t1700000001.0\tsub\x00dir\n", command_exit=0)
    res = obs.list_directory(user_id=USER, machine_id="dgx", path="/scratch")
    rows = _rows(res)
    assert rows[0] == ["file", "innocent.txt", "512.0 B"]
    assert rows[1][0] == "dir" and rows[1][1] == "subdir"


def test_list_directory_bounds_rows_and_marks_truncation():
    listing = "".join(f"f\t1\t1700000000.0\tf{i}.txt\n" for i in range(201))
    _fake(command_stdout=listing, command_exit=0)
    res = obs.list_directory(user_id=USER, machine_id="dgx", path="/scratch")
    assert res["_data"] == {"path": "/scratch", "shown": 200, "total": 201, "truncated": True}
    assert len(_rows(res)) == 200
    assert "Showing 200 of 201" in str(res["_ui_components"])


# ── list_processes: typed rows, sanitised comm, bounded ───────────────────────

def test_list_processes_sanitises_other_users_comm():
    # ps comm values are genuinely untrusted (other users' processes — R12)
    _fake(command_stdout="1234 mallory 1.5 0.3 20480 bad\x1b]0;pwn\x07cmd\n", command_exit=0)
    res = obs.list_processes(user_id=USER, machine_id="dgx", own_only=False)
    comm = _rows(res)[0][5]
    assert "\x1b" not in comm and "\x07" not in comm
    assert _rows(res)[0][4] == "20.0 MiB"  # rss kB → typed bytes, formatted


def test_list_processes_bounds_rows_and_marks_truncation():
    _fake(command_stdout="".join(f"{i} me 0.0 0.0 1024 sleep\n" for i in range(201)),
          command_exit=0)
    res = obs.list_processes(user_id=USER, machine_id="dgx")
    assert res["_data"] == {"processes": 201, "own_only": True}
    assert len(_rows(res)) == 200
    assert "Showing 200 of 201" in str(res["_ui_components"])
