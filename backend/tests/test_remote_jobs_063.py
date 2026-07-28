"""Feature 063 US4 — remote job tracking: state classifier, card renderer, the
read-only SSH probe, and the poller state machine. Hermetic (scripted transport +
monkeypatched helpers); no DB / SSH / network / event-loop DB calls.

Also closes T046/T047 (SC-009/SC-010): a submit writes a durable ``tracked_job``
row; the boot reconciliation pass (``poll_once``, launched in ``Orchestrator.start``)
resolves a job that finished while the orchestrator was down; a slow/lost sbatch
surfaces the honest non-retryable ``unconfirmed`` verdict and never creates a
duplicate tracking row; unattended cancel is refused at the confirmation gate while
the unattended poller is narrowed to read-only status verbs by construction.
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
            # Mirror the production transports (FR-036): a consequential
            # (non-retryable) timeout surfaces as the honest ``unconfirmed``.
            if r is Verdict.TIMEOUT and not retryable:
                r = Verdict.UNCONFIRMED
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


def _orch(db=None):
    return SimpleNamespace(history=SimpleNamespace(db=db if db is not None else object()),
                           credential_manager=object())


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


# ── T046/T047 fixtures: tracked_job store double + wired mutating verbs ────────

class _JobsDB:
    """tracked_job repository double implementing exactly the SQL that
    ``remote_jobs`` and ``run_job`` issue (the _FakeDB pattern of
    test_remote_confirmation_063 — anything unexpected raises)."""

    def __init__(self):
        self.rows = {}  # tracked_job_id -> row dict

    def execute(self, q, params):
        qs = " ".join(q.split())
        if qs.startswith("INSERT INTO tracked_job"):
            (tid, owner, machine, chat, jid, marker, out, comp, name, notify, created) = params
            self.rows[tid] = {
                "tracked_job_id": tid, "owner_user_id": owner, "machine_id": machine,
                "chat_id": chat, "scheduler_job_id": jid, "submit_marker": marker,
                "output_path": out, "component_id": comp, "job_name": name,
                "state": "submitted", "exit_code": None, "terminal": False,
                "fail_count": 0, "notify_on_finish": notify, "notified": False,
                "created_at": created, "last_polled_at": None, "finished_at": None,
            }
            return
        if "SET state=?, exit_code=?" in qs and "notified=?" not in qs:   # _apply
            state, exit_code, terminal, fc, now, finished, tid = params
            r = self.rows[tid]
            r.update(state=state, exit_code=exit_code, terminal=terminal,
                     fail_count=fc, last_polled_at=now)
            r["finished_at"] = r["finished_at"] or finished
            return
        if "notified=TRUE" in qs:                                          # _mark_notified
            self.rows[params[0]]["notified"] = True
            return
        if "state='orphaned'" in qs:                                       # _orphan
            r = self.rows[params[1]]
            r.update(state="orphaned", terminal=True)
            r["finished_at"] = r["finished_at"] or params[0]
            return
        if "SET fail_count=?, last_polled_at=?" in qs:                     # _touch_fail
            fc, now, tid = params
            self.rows[tid].update(fail_count=fc, last_polled_at=now)
            return
        raise AssertionError("unexpected execute: " + qs)

    def fetch_all(self, q, params):
        assert "terminal = FALSE" in q  # list_open
        return [dict(r) for r in self.rows.values() if not r["terminal"]]

    def fetch_one(self, q, params):
        assert "scheduler_job_id" in q  # get_by_job
        owner, jid = params
        for r in self.rows.values():
            if r["owner_user_id"] == owner and str(r["scheduler_job_id"]) == str(jid):
                return dict(r)
        return None


@pytest.fixture
def ctl_db(monkeypatch):
    """Wire the mutating verb library to a fake inventory + tracked_job store."""
    from agents.remote_control import mcp_tools as ctl
    db = _JobsDB()
    ctl.register_deps(db, object())
    monkeypatch.setattr("orchestrator.remote_machines.resolve_machine",
                        lambda d, uid, ref: {"machine_id": "m1", "label": "dgx"})
    monkeypatch.setattr("orchestrator.remote_machines.build_target",
                        lambda *a, **k: _target())
    return db


def _sbatch_argv(t):
    return next(a for a in t.calls if a[0] == "sbatch")


# ── T046: durable submit + boot reconciliation (SC-009) ────────────────────────

def test_run_job_writes_durable_tracked_job_row(ctl_db):
    from agents.remote_control import mcp_tools as ctl
    t = _Scripted({"pwd": ("/home/me\n", 0), "mkdir": ("", 0), "sbatch": ("4242\n", 0)})
    set_transport(t)
    res = ctl.run_job(user_id="u", session_id="c1", machine_id="dgx",
                      script="nvidia-smi", job_name="probe")
    data = res["_data"]
    assert data["job_id"] == "4242" and data["tracked"] is True
    (row,) = ctl_db.rows.values()
    assert row["owner_user_id"] == "u" and row["machine_id"] == "m1"
    assert row["chat_id"] == "c1" and row["scheduler_job_id"] == "4242"
    assert row["state"] == "submitted" and row["terminal"] is False
    assert row["component_id"] == "au_rjob_4242" and row["job_name"] == "probe"
    assert row["notify_on_finish"] is True
    # The idempotency nonce rides BOTH sbatch's --comment and the durable row, so
    # an ambiguous submit stays reconcilable cluster-side (FR-037).
    argv = _sbatch_argv(t)
    marker = next(tok for tok in argv if tok.startswith("--comment="))
    assert marker == f"--comment=astral:{row['submit_marker']}"
    assert f"--output={row['output_path']}" in argv
    assert data["output_path"] == row["output_path"]
    # Truthfully reportable by (owner, scheduler id) — the read the poller and the
    # status verbs use (FR-042).
    assert rj.get_by_job(ctl_db, "u", "4242")["tracked_job_id"] == row["tracked_job_id"]


async def test_boot_reconciliation_resolves_job_finished_during_outage(ctl_db, monkeypatch):
    # SC-009: the row was open ('running') when the orchestrator went down and the
    # job completed during the outage. The first poll_once pass — the same
    # read-only pass Orchestrator.start's poller runs — must close the row from
    # sacct (terminal + exit code + notification), never leave it "running".
    tid = rj.create_tracked_job(
        ctl_db, owner_user_id="u", machine_id="m1", chat_id="c1", scheduler_job_id="5",
        submit_marker="abc123", output_path="/o", component_id="au_rjob_5",
        job_name="t", notify_on_finish=True)
    ctl_db.rows[tid]["state"] = "running"  # progressed before the outage
    set_transport(_Scripted({"squeue": (_SQUEUE_EMPTY, 0), "sacct": (_SACCT_DONE, 0),
                             "tail": ("final output", 0)}))
    pushes, notifies = [], []

    async def _push(orch, row, **kw):
        pushes.append(kw)

    async def _notify(orch, row, state, exit_code):
        notifies.append((state, exit_code))
    monkeypatch.setattr(rj, "_push_component", _push)
    monkeypatch.setattr(rj, "_notify_finish", _notify)

    await rj.poll_once(_orch(ctl_db))
    row = ctl_db.rows[tid]
    assert row["terminal"] is True and row["state"] == "COMPLETED"
    assert row["finished_at"] and row["notified"] is True
    assert pushes and pushes[0]["terminal"] is True
    assert pushes[0]["output_tail"] == "final output"
    assert notifies and notifies[0][0] == "COMPLETED"


def test_boot_reconciliation_pass_is_wired_into_orchestrator_start():
    # The reconciliation pass must actually run at boot: start() launches the
    # poller task (flag-gated, fail-closed off) and its loop body is poll_once.
    import inspect
    from orchestrator.orchestrator import Orchestrator
    start_src = inspect.getsource(Orchestrator.start)
    assert "_remote_job_poll_loop" in start_src
    assert 'is_enabled("remote_compute")' in start_src
    assert "poll_once" in inspect.getsource(Orchestrator._remote_job_poll_loop)


# ── T047: slow/lost submit → unconfirmed, never duplicated (SC-010) ────────────

def test_transport_maps_consequential_timeout_to_unconfirmed():
    # The mechanism behind SC-010: BOTH production transports surface a timed-out
    # NON-retryable call as ``unconfirmed`` (outcome unknown — verify, don't
    # re-issue), while a retryable read keeps the plain ``timeout``.
    from orchestrator.remote_transport import FakeTransport, ParamikoTransport
    ft = FakeTransport(force_verdict=Verdict.TIMEOUT)
    assert ft.run(_target(), ["sbatch", "/s"], timeout=5, retryable=False).verdict is Verdict.UNCONFIRMED
    assert ft.run(_target(), ["squeue"], timeout=5, retryable=True).verdict is Verdict.TIMEOUT
    pt = ParamikoTransport()
    slow = pt._result_for_exception(_target(), TimeoutError("deadline"), retryable=False)
    assert slow.verdict is Verdict.UNCONFIRMED and slow.retryable is False
    assert pt._result_for_exception(_target(), TimeoutError("deadline"),
                                    retryable=True).verdict is Verdict.TIMEOUT


def test_slow_lost_submit_unconfirmed_and_never_duplicated(ctl_db):
    # SC-010: across >=20 induced slow/lost sbatch responses, every attempt reports
    # the non-retryable ``unconfirmed``; the verb never re-issues sbatch on its own
    # and never records a tracking row for an unconfirmed submit — zero rows, so a
    # duplicate tracked_job is impossible. The --comment nonce keeps even a
    # landed-but-lost job reconcilable cluster-side (FR-037).
    from agents.remote_control import mcp_tools as ctl
    for _ in range(20):
        t = _Scripted({"pwd": ("/home/me\n", 0), "mkdir": ("", 0),
                       "sbatch": Verdict.TIMEOUT})  # induced slow/lost submit
        set_transport(t)
        res = ctl.run_job(user_id="u", session_id="c1", machine_id="dgx", script="nvidia-smi")
        assert (res["_data"] or {}).get("verdict") == "unconfirmed"
        assert any(c.get("variant") == "error" for c in res["_ui_components"])
        sbatches = [a for a in t.calls if a[0] == "sbatch"]
        assert len(sbatches) == 1  # exactly one attempt — no silent internal retry
        assert any(tok.startswith("--comment=astral:") for tok in sbatches[0])
    assert ctl_db.rows == {}  # zero rows recorded → zero duplicates across all 20
    from agents.remote_control.mcp_tools import TOOL_REGISTRY
    assert TOOL_REGISTRY["run_job"]["retryable"] is False  # dispatch never re-attempts (FR-036)


# ── T047: unattended authority — cancel refused, status poll permitted ─────────

class _NoWriteDB:
    """Sentinel store: ANY access proves the unattended refusal touched the DB."""

    def execute(self, q, params):
        raise AssertionError("unattended refusal must not write: " + q)

    def fetch_one(self, q, params):
        raise AssertionError("unattended refusal must not read: " + q)


def test_unattended_cancel_refused_and_status_verbs_pass_the_gate():
    # FR-044 gate half: on a machine turn (no live human) a destructive cancel is
    # refused outright — no proposal row is persisted that could be approved
    # out-of-band later — while the read-only status verbs pass this gate.
    from orchestrator import remote_confirmation as rc

    class _MachineWS:
        machine_claims = {"machine_class": "scheduled_job"}

    orch = SimpleNamespace(history=SimpleNamespace(db=_NoWriteDB()),
                           credential_manager=object(), ui_sessions={})
    out = rc.evaluate(orch, _MachineWS(), "remote-compute-1", "cancel_job",
                      {"machine_id": "m1", "job_id": "5"}, "c1", "u")
    assert out is not None and "unattended_refused" in out[0]
    for verb in ("job_status", "list_queue"):
        assert rc.evaluate(orch, _MachineWS(), "remote-compute-1", verb,
                           {"machine_id": "m1"}, "c1", "u") is None


async def test_unattended_poller_is_read_only_and_permitted(ctl_db, monkeypatch):
    # FR-044 structural half — the poller's narrowed authority: a full unattended
    # pass over a live job and then a finished job issues ONLY status reads
    # (squeue/sacct/tail); sbatch (submit) and scancel (cancel) are unreachable
    # from this code path — while the poll itself IS permitted (the row advances
    # with no human socket anywhere).
    tid = rj.create_tracked_job(
        ctl_db, owner_user_id="u", machine_id="m1", chat_id="c1", scheduler_job_id="5",
        submit_marker="n", output_path="/o", component_id="au_rjob_5",
        job_name="t", notify_on_finish=False)

    async def _push(orch, row, **kw):
        pass
    monkeypatch.setattr(rj, "_push_component", _push)

    live = _Scripted({"squeue": (_SQUEUE_RUN, 0)})
    set_transport(live)
    await rj.poll_once(_orch(ctl_db))
    assert ctl_db.rows[tid]["state"] == "RUNNING"  # unattended status poll permitted

    done = _Scripted({"squeue": (_SQUEUE_EMPTY, 0), "sacct": (_SACCT_DONE, 0),
                      "tail": ("out", 0)})
    set_transport(done)
    await rj.poll_once(_orch(ctl_db))
    assert ctl_db.rows[tid]["terminal"] is True

    seen = {argv[0] for argv in live.calls + done.calls}
    assert seen <= {"squeue", "sacct", "tail"}
    assert not seen & {"sbatch", "scancel"}


# ── T049 parity: submit_job (EXISTING-script leg) marker + durable row ─────────

class TestSubmitJobParity:
    """T049 leftover: the EXISTING-SCRIPT submit leg (``submit_job``) carries the
    same FR-037 idempotency marker and durable ``tracked_job`` row as the inline
    ``run_job`` leg, and keeps the honest non-retryable ``unconfirmed`` posture on
    a slow/lost sbatch — zero rows, so a duplicate is impossible (SC-010)."""

    def test_submit_job_writes_durable_row_and_marker(self, ctl_db):
        from agents.remote_control import mcp_tools as ctl
        t = _Scripted({"sbatch": ("777\n", 0)})
        set_transport(t)
        res = ctl.submit_job(user_id="u", session_id="c1", machine_id="dgx",
                             script_path="/home/me/job.sbatch", job_name="probe")
        data = res["_data"]
        assert data["job_id"] == "777" and data["tracked"] is True
        assert data["script_path"] == "/home/me/job.sbatch"
        (row,) = ctl_db.rows.values()
        assert row["owner_user_id"] == "u" and row["machine_id"] == "m1"
        assert row["chat_id"] == "c1" and row["scheduler_job_id"] == "777"
        assert row["state"] == "submitted" and row["terminal"] is False
        assert row["component_id"] == "au_rjob_777" and row["job_name"] == "probe"
        assert row["notify_on_finish"] is True
        # No inline script → no controlled --output: the pre-existing script's own
        # directives decide, so the row records no output_path to tail.
        assert row["output_path"] is None
        # The idempotency nonce rides BOTH sbatch's --comment and the durable row
        # (FR-037) — identical posture to run_job.
        argv = _sbatch_argv(t)
        marker = next(tok for tok in argv if tok.startswith("--comment="))
        assert marker == f"--comment=astral:{row['submit_marker']}"
        assert argv[-1] == "/home/me/job.sbatch"
        # submit_job never stages a script: sbatch is the ONLY transport op.
        assert [a[0] for a in t.calls] == ["sbatch"]
        # The returned canvas card is stamped with the tracked component id, so
        # the poller updates the SAME component in place.
        assert res["_ui_components"][0]["id"] == "au_rjob_777"
        assert rj.get_by_job(ctl_db, "u", "777")["tracked_job_id"] == row["tracked_job_id"]

    def test_submit_job_rejected_by_sbatch_records_no_row(self, ctl_db):
        from agents.remote_control import mcp_tools as ctl
        t = _Scripted({"sbatch": ("", 1)})
        set_transport(t)
        res = ctl.submit_job(user_id="u", session_id="c1", machine_id="dgx",
                             script_path="/home/me/job.sbatch")
        assert (res["_data"] or {}).get("verdict") == "partial"
        assert ctl_db.rows == {}  # a refused submit tracks nothing

    def test_submit_job_slow_lost_unconfirmed_and_never_duplicated(self, ctl_db):
        # SC-010 sweep, mirroring run_job's: across >=20 induced slow/lost sbatch
        # responses every attempt reports the non-retryable ``unconfirmed``; the
        # verb never re-issues sbatch on its own and never records a tracking row
        # for an unconfirmed submit — zero rows, so a duplicate tracked_job is
        # impossible. The --comment nonce keeps even a landed-but-lost job
        # reconcilable cluster-side (FR-037).
        from agents.remote_control import mcp_tools as ctl
        for _ in range(20):
            t = _Scripted({"sbatch": Verdict.TIMEOUT})  # induced slow/lost submit
            set_transport(t)
            res = ctl.submit_job(user_id="u", session_id="c1", machine_id="dgx",
                                 script_path="/home/me/job.sbatch")
            assert (res["_data"] or {}).get("verdict") == "unconfirmed"
            assert any(c.get("variant") == "error" for c in res["_ui_components"])
            sbatches = [a for a in t.calls if a[0] == "sbatch"]
            assert len(sbatches) == 1  # exactly one attempt — no silent internal retry
            assert any(tok.startswith("--comment=astral:") for tok in sbatches[0])
        assert ctl_db.rows == {}  # zero rows recorded → zero duplicates across all 20
        from agents.remote_control.mcp_tools import TOOL_REGISTRY
        assert TOOL_REGISTRY["submit_job"]["retryable"] is False  # dispatch never re-attempts
