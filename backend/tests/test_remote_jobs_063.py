"""Feature 063 US4 — remote job tracking: state classifier, card renderer, the
read-only SSH probe, and the poller state machine. Hermetic (scripted transport +
monkeypatched helpers); no DB / SSH / network / event-loop DB calls.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import remote_jobs as rj
from orchestrator.remote_transport import MachineTarget, RemoteResult, Verdict, set_transport


def _target(pinned: bool = True):
    return MachineTarget(machine_id="m1", label="dgx", address="10.0.0.5", port=22,
                         username="me", cred_type="password", secret="x",
                         host_key_fingerprint="SHA256:pinned" if pinned else None)


class _Scripted:
    """Transport double whose response depends on argv[0] (squeue/sacct/tail/…)."""

    def __init__(self, table):
        self.table = table  # argv0 -> (stdout, exit) | Verdict
        self.calls = []

    def run(self, target, argv, *, timeout, retryable=False):
        self.calls.append(list(argv))
        r = self.table.get(argv[0])
        if isinstance(r, Verdict):
            return RemoteResult(verdict=r, machine=target.label)
        stdout, exit_status = r if r is not None else ("", 0)
        return RemoteResult(verdict=Verdict.OK, machine=target.label,
                            stdout=stdout, exit_status=exit_status)

    def put_file(self, target, data, remote_path, *, timeout):
        self.calls.append(["put_file", remote_path])
        self.last_put = (remote_path, data)
        return RemoteResult(verdict=Verdict.OK, machine=target.label,
                            data={"path": remote_path, "bytes": len(data)})

    def stat(self, target, remote_path, *, timeout):
        return RemoteResult(verdict=Verdict.OK, machine=target.label, data={"exists": False})

    def probe(self, target, *, timeout):
        return RemoteResult(verdict=Verdict.OK, machine=target.label, data={"authenticated": True})


_SQUEUE_RUN = '{"jobs":[{"job_id":5,"job_state":"RUNNING","name":"t","partition":"gpu","node_count":1,"state_reason":"None"}]}'
_SQUEUE_FAILED = '{"jobs":[{"job_id":5,"job_state":"FAILED","name":"t","partition":"gpu","node_count":1,"state_reason":"None"}]}'
_SQUEUE_EMPTY = '{"jobs":[]}'
_SACCT_DONE = ('{"jobs":[{"job_id":5,"name":"t","state":{"current":["COMPLETED"]},'
               '"time":{"elapsed":60},"exit_code":{"return_code":{"number":0}},"partition":"gpu"}]}')


@pytest.fixture(autouse=True)
def _reset_transport():
    yield
    set_transport(None)


def _orch():
    return SimpleNamespace(history=SimpleNamespace(db=object()), credential_manager=object())


# ── state classifier ────────────────────────────────────────────────────────

def test_is_terminal_state():
    assert rj.is_terminal_state("COMPLETED")
    assert rj.is_terminal_state("CANCELLED by 1234")
    assert rj.is_terminal_state("COMPLETED+")
    assert rj.is_terminal_state("timeout")
    assert not rj.is_terminal_state("RUNNING")
    assert not rj.is_terminal_state("PENDING")
    assert not rj.is_terminal_state("")


# ── card renderer ────────────────────────────────────────────────────────────

def test_render_stamps_explicit_component_id():
    d = rj.render_job_card(job_id="5", machine_label="dgx", state="RUNNING",
                           terminal=False, component_id="au_rjob_5")
    assert d["type"] == "card" and d["id"] == "au_rjob_5"


def test_render_completed_includes_output_and_success():
    d = rj.render_job_card(job_id="5", machine_label="dgx", state="COMPLETED",
                           exit_code="0:0", terminal=True, output_tail="GPU 0: A100",
                           component_id="au_rjob_5")
    flat = str(d)
    assert "GPU 0: A100" in flat and "completed" in flat.lower()


def test_render_clips_huge_output():
    d = rj.render_job_card(job_id="5", machine_label="dgx", state="COMPLETED",
                           terminal=True, output_tail="x" * 50000, component_id="au_rjob_5")
    assert "truncated" in str(d)


# ── read-only probe ──────────────────────────────────────────────────────────

def test_probe_running(monkeypatch):
    monkeypatch.setattr("orchestrator.remote_machines.build_target", lambda *a, **k: _target())
    set_transport(_Scripted({"squeue": (_SQUEUE_RUN, 0)}))
    out = rj._probe_state(_orch(), {"owner_user_id": "u", "machine_id": "m1",
                                    "scheduler_job_id": "5", "state": "submitted"})
    assert out["state"] == "RUNNING" and out["terminal"] is False


def test_probe_squeue_terminal_state_closes_and_reads_output(monkeypatch):
    # Regression: a terminal state seen via squeue must close the row (terminal=True)
    # and read the output — not be treated as "still running".
    monkeypatch.setattr("orchestrator.remote_machines.build_target", lambda *a, **k: _target())
    set_transport(_Scripted({"squeue": (_SQUEUE_FAILED, 0), "tail": ("No devices were found", 0)}))
    out = rj._probe_state(_orch(), {"owner_user_id": "u", "machine_id": "m1",
                                    "scheduler_job_id": "5", "state": "running",
                                    "output_path": "/o"})
    assert out["terminal"] is True and out["state"] == "FAILED"
    assert out["output_tail"] == "No devices were found"


def test_probe_completed_reads_output(monkeypatch):
    monkeypatch.setattr("orchestrator.remote_machines.build_target", lambda *a, **k: _target())
    set_transport(_Scripted({"squeue": (_SQUEUE_EMPTY, 0), "sacct": (_SACCT_DONE, 0),
                             "tail": ("job output here", 0)}))
    out = rj._probe_state(_orch(), {"owner_user_id": "u", "machine_id": "m1",
                                    "scheduler_job_id": "5", "state": "running",
                                    "output_path": "/home/me/.astral_jobs/x.out"})
    assert out["terminal"] is True and out["state"] == "COMPLETED"
    assert out["output_tail"] == "job output here"


def test_probe_gone_from_both_when_was_running_is_completed(monkeypatch):
    monkeypatch.setattr("orchestrator.remote_machines.build_target", lambda *a, **k: _target())
    set_transport(_Scripted({"squeue": (_SQUEUE_EMPTY, 0), "sacct": (_SQUEUE_EMPTY, 0)}))
    out = rj._probe_state(_orch(), {"owner_user_id": "u", "machine_id": "m1",
                                    "scheduler_job_id": "5", "state": "running"})
    assert out["terminal"] is True and out["state"] == "COMPLETED"


def test_probe_skips_unpinned_host_key(monkeypatch):
    monkeypatch.setattr("orchestrator.remote_machines.build_target",
                        lambda *a, **k: _target(pinned=False))
    set_transport(_Scripted({"squeue": (_SQUEUE_RUN, 0)}))
    out = rj._probe_state(_orch(), {"owner_user_id": "u", "machine_id": "m1",
                                    "scheduler_job_id": "5", "state": "submitted"})
    assert out == {"skip": True}  # never TOFU unattended


def test_probe_machine_gone_is_orphan(monkeypatch):
    from orchestrator.remote_machines import MachineNotFound

    def _raise(*a, **k):
        raise MachineNotFound("gone")
    monkeypatch.setattr("orchestrator.remote_machines.build_target", _raise)
    out = rj._probe_state(_orch(), {"owner_user_id": "u", "machine_id": "m1",
                                    "scheduler_job_id": "5", "state": "running"})
    assert out == {"orphan": True}


def test_probe_transport_error_is_transient(monkeypatch):
    monkeypatch.setattr("orchestrator.remote_machines.build_target", lambda *a, **k: _target())
    set_transport(_Scripted({"squeue": Verdict.UNREACHABLE}))
    out = rj._probe_state(_orch(), {"owner_user_id": "u", "machine_id": "m1",
                                    "scheduler_job_id": "5", "state": "running"})
    assert out.get("transient") is True


# ── poller state machine (_poll_one) ─────────────────────────────────────────

@pytest.fixture
def spy(monkeypatch):
    rec = {"apply": [], "orphan": 0, "notified_mark": 0, "touch": [], "push": [], "notify": []}
    monkeypatch.setattr(rj, "_apply", lambda db, tid, **kw: rec["apply"].append(kw))
    monkeypatch.setattr(rj, "_orphan", lambda db, tid: rec.__setitem__("orphan", rec["orphan"] + 1))
    monkeypatch.setattr(rj, "_mark_notified", lambda db, tid: rec.__setitem__("notified_mark", rec["notified_mark"] + 1))
    monkeypatch.setattr(rj, "_touch_fail", lambda db, tid, fc: rec["touch"].append(fc))

    async def _push(orch, row, **kw):
        rec["push"].append(kw)

    async def _notify(orch, row, state, exit_code):
        rec["notify"].append((state, exit_code))
    monkeypatch.setattr(rj, "_push_component", _push)
    monkeypatch.setattr(rj, "_notify_finish", _notify)
    return rec


def _row(**over):
    r = {"tracked_job_id": "t1", "scheduler_job_id": "5", "state": "submitted",
         "terminal": False, "owner_user_id": "u", "machine_id": "m1", "chat_id": "c1",
         "component_id": "au_rjob_5", "notify_on_finish": True, "notified": False,
         "fail_count": 0, "output_path": "/o"}
    r.update(over)
    return r


async def test_poll_one_running_pushes_no_notify(spy, monkeypatch):
    monkeypatch.setattr(rj, "_probe_state", lambda o, r: {"state": "RUNNING", "terminal": False, "exit_code": None})
    await rj._poll_one(_orch(), _row())
    assert spy["apply"] and spy["apply"][0]["state"] == "RUNNING"
    assert spy["push"] and not spy["notify"]


async def test_poll_one_completed_pushes_and_notifies(spy, monkeypatch):
    monkeypatch.setattr(rj, "_probe_state",
                        lambda o, r: {"state": "COMPLETED", "terminal": True,
                                      "exit_code": "0:0", "output_tail": "out"})
    await rj._poll_one(_orch(), _row(state="running"))
    assert spy["apply"][0]["terminal"] is True
    assert spy["push"] and spy["notify"] == [("COMPLETED", "0:0")]
    assert spy["notified_mark"] == 1


async def test_poll_one_no_change_no_push(spy, monkeypatch):
    monkeypatch.setattr(rj, "_probe_state", lambda o, r: {"state": "RUNNING", "terminal": False, "exit_code": None})
    await rj._poll_one(_orch(), _row(state="running"))  # already running
    assert spy["apply"] and not spy["push"]  # DB refreshed, but no UI churn


async def test_poll_one_orphan(spy, monkeypatch):
    monkeypatch.setattr(rj, "_probe_state", lambda o, r: {"orphan": True})
    await rj._poll_one(_orch(), _row(state="running"))
    assert spy["orphan"] == 1 and spy["push"]


async def test_poll_one_transient_increments(spy, monkeypatch):
    monkeypatch.setattr(rj, "_probe_state", lambda o, r: {"transient": True})
    await rj._poll_one(_orch(), _row(fail_count=0))
    assert spy["touch"] == [1] and spy["orphan"] == 0


async def test_poll_one_transient_ceiling_orphans(spy, monkeypatch):
    monkeypatch.setattr(rj, "_probe_state", lambda o, r: {"transient": True})
    await rj._poll_one(_orch(), _row(fail_count=rj._FAIL_CEILING - 1))
    assert spy["orphan"] == 1


async def test_poll_one_skip_unpinned_is_noop(spy, monkeypatch):
    monkeypatch.setattr(rj, "_probe_state", lambda o, r: {"skip": True})
    await rj._poll_one(_orch(), _row())
    assert not spy["apply"] and spy["orphan"] == 0 and not spy["push"]


async def test_poll_one_completed_no_notify_when_opted_out(spy, monkeypatch):
    monkeypatch.setattr(rj, "_probe_state",
                        lambda o, r: {"state": "COMPLETED", "terminal": True, "exit_code": "0"})
    await rj._poll_one(_orch(), _row(state="running", notify_on_finish=False))
    assert spy["push"] and not spy["notify"]
