"""Durable async tracking for submitted remote Slurm jobs (feature 063 US4).

A submitted cluster job gets a durable ``tracked_job`` row (owner + cluster job id),
so it outlives the browser tab, the process, and a restart, and is truthfully
reportable from any device (FR-042/FR-046). A single always-on background poller
(launched in ``Orchestrator.start`` when FF_REMOTE_COMPUTE is on) polls each open
job's status over SSH — **read-only by construction**: it only ever runs
``squeue``/``sacct``/``tail``, never a mutating verb (FR-044). On a state change it
refreshes the job's canvas component IN PLACE (via the detached-mutation publication
boundary so revisioned chats stay consistent) and, on a terminal state with
``notify_on_finish``, notifies the owner's signed-in clients (FR-045).

This module holds: the tracked_job repository (sync DB helpers), the shared
``render_job_card`` used by both the submit verb and the poller, the pure state
classifier, and ``poll_once(orch)`` — the one poll pass the loop calls.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger("RemoteJobs")

# Slurm states that mean the job is finished (base token, before any suffix like
# "CANCELLED by 1234" or "COMPLETED+"). Anything else (RUNNING/PENDING/…) is live.
TERMINAL_STATES = {
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL",
    "BOOT_FAIL", "DEADLINE", "PREEMPTED", "REVOKED", "SPECIAL_EXIT",
}
_FAIL_CEILING = 8          # consecutive transport failures before a job is orphaned
_OUTPUT_CLIP = 16000       # max chars of job output shown in a component
_TAIL_LINES = 200


def _base_state(state: Optional[str]) -> str:
    if not state:
        return ""
    return str(state).split()[0].split("+")[0].upper()


def is_terminal_state(state: Optional[str]) -> bool:
    return _base_state(state) in TERMINAL_STATES


# ── repository (sync; callers wrap in asyncio.to_thread off the event loop) ─────

def create_tracked_job(db, *, owner_user_id: str, machine_id: str, chat_id: Optional[str],
                       scheduler_job_id: str, submit_marker: Optional[str],
                       output_path: Optional[str], component_id: Optional[str],
                       job_name: Optional[str], notify_on_finish: bool) -> str:
    tracked_job_id = uuid.uuid4().hex
    db.execute(
        """INSERT INTO tracked_job
           (tracked_job_id, owner_user_id, machine_id, chat_id, scheduler_job_id,
            submit_marker, output_path, component_id, job_name, state,
            notify_on_finish, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitted', ?, ?)""",
        (tracked_job_id, owner_user_id, machine_id, chat_id, scheduler_job_id,
         submit_marker, output_path, component_id, job_name or "",
         bool(notify_on_finish), int(time.time())))
    return tracked_job_id


def get_by_job(db, owner_user_id: str, scheduler_job_id: str) -> Optional[Dict[str, Any]]:
    row = db.fetch_one(
        "SELECT * FROM tracked_job WHERE owner_user_id = ? AND scheduler_job_id = ?",
        (owner_user_id, str(scheduler_job_id)))
    return dict(row) if row else None


def list_open(db, limit: int = 200) -> List[Dict[str, Any]]:
    rows = db.fetch_all(
        "SELECT * FROM tracked_job WHERE terminal = FALSE ORDER BY created_at ASC LIMIT ?",
        (int(limit),)) or []
    return [dict(r) for r in rows]


def _apply(db, tracked_job_id: str, *, state: str, exit_code: Optional[str],
           terminal: bool, fail_count: int, notified: Optional[bool] = None) -> None:
    now = int(time.time())
    finished = now if terminal else None
    if notified is None:
        db.execute(
            "UPDATE tracked_job SET state=?, exit_code=?, terminal=?, fail_count=?, "
            "last_polled_at=?, finished_at=COALESCE(finished_at, ?) WHERE tracked_job_id=?",
            (state, exit_code, bool(terminal), int(fail_count), now,
             finished, tracked_job_id))
    else:
        db.execute(
            "UPDATE tracked_job SET state=?, exit_code=?, terminal=?, fail_count=?, "
            "last_polled_at=?, finished_at=COALESCE(finished_at, ?), notified=? "
            "WHERE tracked_job_id=?",
            (state, exit_code, bool(terminal), int(fail_count), now, finished,
             bool(notified), tracked_job_id))


def _mark_notified(db, tracked_job_id: str) -> None:
    db.execute("UPDATE tracked_job SET notified=TRUE WHERE tracked_job_id=?", (tracked_job_id,))


def _orphan(db, tracked_job_id: str) -> None:
    db.execute(
        "UPDATE tracked_job SET state='orphaned', terminal=TRUE, "
        "finished_at=COALESCE(finished_at, ?) WHERE tracked_job_id=?",
        (int(time.time()), tracked_job_id))


def _touch_fail(db, tracked_job_id: str, fail_count: int) -> None:
    db.execute("UPDATE tracked_job SET fail_count=?, last_polled_at=? WHERE tracked_job_id=?",
               (int(fail_count), int(time.time()), tracked_job_id))


# ── shared component renderer (submit verb + poller build the SAME card) ────────

def render_job_card(*, job_id: str, machine_label: str, state: str,
                    exit_code: Optional[str] = None, terminal: bool = False,
                    output_tail: Optional[str] = None, component_id: str,
                    job_name: Optional[str] = None) -> Dict[str, Any]:
    """Build the tracked-job canvas Card as a serialized dict, stamped with an
    explicit ``au_``-prefixed id so the workspace assigns EXACTLY component_id and
    the poller can update the same component in place."""
    from astralprims import Alert, Card, Table, Text
    st = state or "submitted"
    base = _base_state(st)
    label = machine_label or "?"
    if not terminal:
        alert = Alert(message=f"Job {job_id} on {label} — {st}…", variant="info")
    elif base == "COMPLETED" and str(exit_code or "0").split(":")[0] in ("", "0"):
        alert = Alert(message=f"Job {job_id} completed on {label}.", variant="success")
    elif base == "ORPHANED":
        alert = Alert(message=f"Job {job_id}: tracking stopped (machine or credential gone).",
                      variant="warning")
    else:
        detail = f" (exit {exit_code})" if exit_code else ""
        alert = Alert(message=f"Job {job_id} ended on {label}: {st}{detail}.",
                      variant="error" if base != "CANCELLED" else "warning")
    rows = [["Job id", str(job_id)], ["Machine", label], ["State", st]]
    if job_name:
        rows.insert(1, ["Name", str(job_name)])
    if exit_code:
        rows.append(["Exit code", str(exit_code)])
    content: List[Any] = [alert, Table(headers=["Field", "Value"], rows=rows)]
    if output_tail:
        clipped = _clip_output(output_tail)
        content.append(Text(content="**Output**", variant="caption"))
        content.append(Text(content="```\n" + clipped + "\n```", variant="body"))
    card = Card(title=f"Remote job {job_id} — {label}", content=content)
    card.id = component_id  # explicit id → resolve_identity uses it verbatim
    return card.to_dict()


def _clip_output(text: str) -> str:
    import re
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\x1b]", "", str(text or ""))
    if len(cleaned) > _OUTPUT_CLIP:
        cleaned = "…(truncated)…\n" + cleaned[-_OUTPUT_CLIP:]
    return cleaned


# ── the poll pass ───────────────────────────────────────────────────────────────

def _probe_state(orch, row: Dict[str, Any]) -> Dict[str, Any]:
    """Blocking (run in a thread): resolve the machine + read the job's current
    Slurm state read-only. Returns a small outcome dict — never mutates anything."""
    from agents.remote_observe.mcp_tools import _parse_sacct_json, _parse_squeue_json
    from orchestrator import remote_machines
    from orchestrator.credential_manager import CredentialNotConfigured, CredentialUndecryptable
    from orchestrator.remote_machines import MachineNotFound
    from orchestrator.remote_transport import get_transport

    try:
        target = remote_machines.build_target(
            orch.history.db, orch.credential_manager, row["owner_user_id"], row["machine_id"])
    except (MachineNotFound, CredentialNotConfigured, CredentialUndecryptable):
        return {"orphan": True}
    except Exception:  # noqa: BLE001 — treat an unexpected resolve error as transient
        return {"transient": True}
    # Host-key TOFU guard (FR-020): never let the UNATTENDED poller trust-on-first-use.
    # Only poll a machine whose identity was pinned by an interactive probe.
    if not getattr(target, "host_key_fingerprint", None):
        return {"skip": True}

    tx = get_transport()
    jid = str(row["scheduler_job_id"])
    label = target.label
    live = tx.run(target, ["squeue", "--job", jid, "--json"], timeout=30.0, retryable=True)
    if not live.ok:
        return {"transient": True, "machine": label}
    jobs = _parse_squeue_json(live.stdout)
    if jobs:
        # A job usually shows a live state here (RUNNING/PENDING/…), but some Slurm
        # configs still list a just-finished job in a TERMINAL state — classify by
        # the state itself, never assume "still running" (else the row never closes).
        st = jobs[0]["state"] or "RUNNING"
        term = is_terminal_state(st)
        out = {"state": st, "terminal": term, "exit_code": None, "machine": label}
        if term and row.get("output_path"):
            out["output_tail"] = _tail(tx, target, row["output_path"])
        return out
    # Not in the live queue → the accounting DB has the terminal record.
    hist = tx.run(target, ["sacct", "-j", jid, "--json", "-X"], timeout=30.0, retryable=True)
    if not hist.ok:
        return {"transient": True, "machine": label}
    acct = _parse_sacct_json(hist.stdout)
    if acct:
        st = acct[0]["state"] or "COMPLETED"
        term = is_terminal_state(st)
        out = {"state": st, "terminal": term, "exit_code": acct[0].get("exit_code") or "",
               "machine": label}
        if term and row.get("output_path"):
            out["output_tail"] = _tail(tx, target, row["output_path"])
        return out
    # Neither squeue nor sacct knows it. If it was already active, it finished and
    # its accounting was purged/lagging → resolve terminal (best-effort, FR-043).
    if _base_state(row.get("state")) in ("RUNNING", "COMPLETING", "PENDING", "CONFIGURING"):
        out = {"state": "COMPLETED", "terminal": True, "exit_code": "", "machine": label}
        if row.get("output_path"):
            out["output_tail"] = _tail(tx, target, row["output_path"])
        return out
    return {"state": row.get("state") or "submitted", "terminal": False,
            "exit_code": None, "machine": label}


def _tail(tx, target, path: str) -> Optional[str]:
    try:
        r = tx.run(target, ["tail", "-n", str(_TAIL_LINES), path], timeout=20.0, retryable=True)
        return r.stdout if r.ok else None
    except Exception:  # noqa: BLE001
        return None


async def poll_once(orch) -> None:
    """One poll pass over every open tracked job. Best-effort per job — one bad
    job never blocks the others, and a transport blip only degrades that job."""
    import asyncio
    db = orch.history.db
    rows = await asyncio.to_thread(list_open, db)
    for row in rows:
        try:
            await _poll_one(orch, row)
        except Exception:  # noqa: BLE001
            logger.warning("tracked-job poll failed for %s", row.get("tracked_job_id"),
                           exc_info=True)


async def _poll_one(orch, row: Dict[str, Any]) -> None:
    import asyncio
    db = orch.history.db
    tid = row["tracked_job_id"]
    outcome = await asyncio.to_thread(_probe_state, orch, row)

    if outcome.get("skip"):
        return  # unpinned host key — do not TOFU unattended
    if outcome.get("orphan"):
        await asyncio.to_thread(_orphan, db, tid)
        await _push_component(orch, row, state="orphaned", exit_code=None, terminal=True)
        return
    if outcome.get("transient"):
        fails = int(row.get("fail_count") or 0) + 1
        if fails >= _FAIL_CEILING:
            await asyncio.to_thread(_orphan, db, tid)
            await _push_component(orch, row, state="orphaned", exit_code=None, terminal=True)
        else:
            await asyncio.to_thread(_touch_fail, db, tid, fails)
        return

    new_state = outcome["state"]
    terminal = bool(outcome.get("terminal"))
    exit_code = outcome.get("exit_code")
    changed = (_base_state(new_state) != _base_state(row.get("state"))) or terminal != bool(row.get("terminal"))
    await asyncio.to_thread(_apply, db, tid, state=new_state, exit_code=exit_code,
                            terminal=terminal, fail_count=0)
    if changed:
        await _push_component(orch, row, state=new_state, exit_code=exit_code,
                              terminal=terminal, output_tail=outcome.get("output_tail"))
    if terminal and row.get("notify_on_finish") and not row.get("notified"):
        await _notify_finish(orch, row, new_state, exit_code)
        await asyncio.to_thread(_mark_notified, db, tid)


async def _push_component(orch, row: Dict[str, Any], *, state: str, exit_code: Optional[str],
                          terminal: bool, output_tail: Optional[str] = None) -> None:
    """Refresh the job's canvas component in place (durable + live), tolerant of a
    missing chat/component or a mutation-boundary error."""
    import asyncio
    chat_id = row.get("chat_id")
    component_id = row.get("component_id")
    user_id = row.get("owner_user_id")
    if not (chat_id and component_id and user_id):
        return
    label = row.get("machine_id") or "?"  # prefer the friendly label if resolvable
    try:
        from orchestrator import remote_machines
        m = await asyncio.to_thread(remote_machines.get_machine, orch.history.db,
                                    user_id, row["machine_id"])
        if m and m.get("label"):
            label = m["label"]
    except Exception:  # noqa: BLE001
        pass
    comp = render_job_card(
        job_id=str(row["scheduler_job_id"]), machine_label=label, state=state,
        exit_code=exit_code, terminal=terminal, output_tail=output_tail,
        component_id=component_id, job_name=row.get("job_name") or None)

    async def _mutation():
        return await orch.workspace.aupsert(chat_id, user_id, [comp],
                                            force_component_id=component_id)
    try:
        ops = await orch.run_detached_conversation_mutation(
            chat_id=chat_id, user_id=user_id, mutation=_mutation)
        if ops:
            await orch.send_ui_upsert(None, chat_id, user_id, ops)
    except Exception:  # noqa: BLE001 — UI refresh is best-effort; DB state is authoritative
        logger.debug("tracked-job component refresh failed for %s", component_id, exc_info=True)


async def _notify_finish(orch, row: Dict[str, Any], state: str, exit_code: Optional[str]) -> None:
    jid = str(row["scheduler_job_id"])
    base = _base_state(state)
    ok = base == "COMPLETED" and str(exit_code or "0").split(":")[0] in ("", "0")
    body = (f"Remote job {jid} finished: {state}"
            + (f" (exit {exit_code})" if exit_code else "") + ".")
    try:
        await orch.notify_user(row["owner_user_id"], {
            "type": "notification",
            "level": "info" if ok else "warning",
            "source": "remote_job",
            "job_id": jid,
            "chat_id": row.get("chat_id"),
            "title": "Remote job finished" if ok else "Remote job ended",
            "body": body,
        })
    except Exception:  # noqa: BLE001
        logger.debug("tracked-job finish notification failed for %s", jid, exc_info=True)
