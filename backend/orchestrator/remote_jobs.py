"""Durable tracking for submitted remote Slurm jobs: a background poller (started in
Orchestrator.start) reads job status read-only over SSH via remote_transport.py and
refreshes the job's canvas card in place through the detached-mutation boundary.
Guarded work_resume.py can reconcile one paused target's terminal tracked-job wait.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from astralplane.repositories import RepositoryConflictError, RepositoryError
from astralplane.repositories.assignments import digest as _outcome_digest
from astralplane.repositories.tracked_jobs import TrackedJobRecord
from orchestrator.work_subscriptions import event_waiters
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    plane_source_from_orchestrator,
    repository_from,
)

logger = logging.getLogger("RemoteJobs")

WAKE_EVENT_PREFIX = "remote-job:"
_WAKE_RETRIES = 2

TERMINAL_STATES = {
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL",
    "BOOT_FAIL", "DEADLINE", "PREEMPTED", "REVOKED", "SPECIAL_EXIT",
}
_FAIL_CEILING = 8
_OUTPUT_CLIP = 16000
_TAIL_LINES = 200


def _base_state(state: Optional[str]) -> str:
    if not state:
        return ""
    return str(state).split()[0].split("+")[0].upper()


def is_terminal_state(state: Optional[str]) -> bool:
    return _base_state(state) in TERMINAL_STATES


def _tracked_context(db) -> PlaneRepositoryContext:
    repository, runtime = repository_from(
        "tracked_jobs",
        plane_runtime=None,
        repositories=None,
        legacy_database=db,
    )
    return PlaneRepositoryContext(
        repository=repository,
        plane_runtime=runtime,
        legacy_database=db,
    )


def _record_to_dict(record: TrackedJobRecord) -> Dict[str, Any]:
    return {
        "tracked_job_id": record.tracked_job_id,
        "owner_user_id": record.owner_id,
        "machine_id": record.machine_id,
        "chat_id": record.conversation_id,
        "scheduler_job_id": record.scheduler_job_id,
        "submit_marker": record.submit_marker,
        "output_path": record.output_path,
        "component_id": record.component_id,
        "job_name": record.job_name,
        "state": record.state,
        "exit_code": record.exit_code,
        "terminal": record.terminal,
        "notify_on_finish": record.notify_on_finish,
        "notified": record.notified,
        "fail_count": record.fail_count,
        "created_at": record.created_at,
        "last_polled_at": record.last_polled_at,
        "finished_at": record.finished_at,
    }

def create_tracked_job(db, *, owner_user_id: str, machine_id: str, chat_id: Optional[str],
                       scheduler_job_id: str, submit_marker: Optional[str],
                       output_path: Optional[str], component_id: Optional[str],
                       job_name: Optional[str], notify_on_finish: bool) -> str:
    tracked_job_id = uuid.uuid4().hex
    context = _tracked_context(db)
    record = context.call(
        context.repository.create,
        record=TrackedJobRecord(
            tracked_job_id=tracked_job_id,
            owner_id=owner_user_id,
            machine_id=machine_id,
            scheduler_job_id=scheduler_job_id,
            conversation_id=chat_id,
            submit_marker=submit_marker,
            output_path=output_path,
            component_id=component_id,
            job_name=job_name or "",
            state="submitted",
            exit_code=None,
            terminal=False,
            notify_on_finish=bool(notify_on_finish),
            notified=False,
            fail_count=0,
            created_at=int(time.time()),
            last_polled_at=None,
            finished_at=None,
        ),
    )
    return record.tracked_job_id


def get_by_job(db, owner_user_id: str, scheduler_job_id: str) -> Optional[Dict[str, Any]]:
    context = _tracked_context(db)
    record = context.call(
        context.repository.get_by_scheduler_job,
        owner_id=owner_user_id,
        scheduler_job_id=str(scheduler_job_id),
    )
    return None if record is None else _record_to_dict(record)


def list_open(db, limit: int = 200) -> List[Dict[str, Any]]:
    context = _tracked_context(db)
    records = context.call(
        context.repository.list_open_for_administration,
        limit=int(limit),
    )
    return [_record_to_dict(record) for record in records]


def _apply(db, row: Dict[str, Any], *, state: str, exit_code: Optional[str],
           terminal: bool, fail_count: int) -> None:
    context = _tracked_context(db)
    context.call(
        context.repository.apply_poll,
        owner_id=row["owner_user_id"],
        tracked_job_id=row["tracked_job_id"],
        expected_fail_count=int(row.get("fail_count") or 0),
        expected_last_polled_at=row.get("last_polled_at"),
        state=state,
        exit_code=exit_code,
        terminal=bool(terminal),
        fail_count=int(fail_count),
        polled_at=int(time.time()),
    )


def _mark_notified(db, row: Dict[str, Any]) -> None:
    context = _tracked_context(db)
    context.call(
        context.repository.mark_notified,
        owner_id=row["owner_user_id"],
        tracked_job_id=row["tracked_job_id"],
    )


def _job_outcome(state: str, exit_code: Optional[str], *, confirmed: bool = True) -> str:
    base = _base_state(state)
    if base == "ORPHANED":
        return "orphaned"
    if base in {"CANCELLED", "REVOKED"}:
        return "cancelled"
    if base == "COMPLETED" and str(exit_code or "0").split(":")[0] in ("", "0"):
        return "completed" if confirmed else "unconfirmed"
    return "failed"


def _try_wake(assignments, tx, *, owner_id: str, assignment_id: str, event_id: str,
              event_key: str, revision: int, event_digest: str,
              woken: List[str], skipped: List[Dict[str, str]]) -> None:
    attempts_left = _WAKE_RETRIES + 1
    while attempts_left:
        attempts_left -= 1
        try:
            with tx.savepoint("rj_wake_" + assignment_id.replace("-", "")):
                current = assignments.get_operation(tx, owner_id=owner_id,
                                                    assignment_id=assignment_id)
                if current is None:
                    return
                record = current.assignment
                wait = (record.operation or {}).get("control", {}).get("wait") or {}
                observed = wait.get("source_revision", 0)
                if revision < observed:
                    raise RepositoryConflictError("assignment_event_revision_conflict",
                                                  code="assignment_event_revision_conflict")
                wake_revision = max(revision, observed + 1) if wait else revision
                result = assignments.accept_wake(
                    tx, owner_id=owner_id, assignment_id=assignment_id,
                    expected_state_version=record.state_version,
                    expected_instruction_revision=record.instruction_revision,
                    expected_control_epoch=record.control_epoch,
                    event_id=event_id, event_key=event_key,
                    source_revision=wake_revision, event_digest=event_digest)
                if result.applied:
                    woken.append(assignment_id)
                return
        except RepositoryConflictError as exc:
            skipped.append({"assignment_id": assignment_id, "reason": exc.code})
            return
        except RepositoryError:
            if not attempts_left:
                raise


def _wake_subscribers(assignments, tx, *, owner_id: str, event_key: str, event_id: str,
                      revision: int, event_digest: str) -> tuple[List[str], List[Dict[str, str]]]:
    woken: List[str] = []
    skipped: List[Dict[str, str]] = []
    for read in event_waiters(assignments, tx, owner_id=owner_id, event_keys={event_key}):
        _try_wake(assignments, tx, owner_id=owner_id,
                  assignment_id=read.assignment.assignment_id, event_id=event_id,
                  event_key=event_key, revision=revision, event_digest=event_digest,
                  woken=woken, skipped=skipped)
    return woken, skipped


def _audit_completion_wakes(runtime, tx, *, owner_id, tracked_id, revision, outcome, woken):
    from datetime import UTC, datetime

    from audit.repository import AuditRepository
    from audit.schemas import AuditEventCreate, AuditEventDTO

    audit = AuditRepository(plane_runtime=runtime)
    for target_id in woken:
        now = datetime.now(UTC)
        result = audit.insert_in_transaction(AuditEventCreate(
            actor_user_id=owner_id, auth_principal=owner_id, event_class="settings",
            action_type="remote_job.completion_wake", description="Remote job completion wake",
            correlation_id=target_id, outcome="success",
            outputs_meta={"source_tracked_job_id": tracked_id, "target_operation_id": target_id,
                          "source_revision": revision, "outcome": outcome},
            started_at=now, completed_at=now), transaction=tx, plane_runtime=runtime)
        if type(result) is not AuditEventDTO:
            raise RepositoryError("remote job wake audit unavailable")


def reconcile_completion_wait(assignments, tx, repository, record):
    from persistent_agents.models import AssignmentError

    context = _tracked_context(plane_source_from_orchestrator(assignments.orch))
    if (context.plane_runtime is not assignments.store.plane_runtime
            or repository is not context.plane_runtime.repositories.assignments
            or context.repository is not context.plane_runtime.repositories.tracked_jobs):
        raise AssignmentError("work_control_unavailable", 503)
    key = record.operation["control"]["wait"]["event_key"]
    tracked_id = key[len(WAKE_EVENT_PREFIX):]
    job = context.repository.get(tx, owner_id=record.owner_id, tracked_job_id=tracked_id, lock=True)
    if job is None:
        raise AssignmentError("work_completion_source_missing", 409)
    if (type(job) is not TrackedJobRecord or job.owner_id != record.owner_id
            or job.tracked_job_id != tracked_id):
        raise AssignmentError("work_control_unavailable", 503)
    if not job.terminal:
        return False
    if (not is_terminal_state(job.state) and _base_state(job.state) != "ORPHANED"
            or type(job.finished_at) is not int):
        raise AssignmentError("work_job_completion_unconfirmed", 409)
    outcome = _job_outcome(job.state, job.exit_code)
    revision = job.finished_at
    woken, skipped = [], []
    _try_wake(repository, tx, owner_id=record.owner_id, assignment_id=record.assignment_id,
              event_key=key, event_id="rj-wake:" + tracked_id, revision=revision,
              event_digest=_outcome_digest({"event": "remote_job_terminal",
                  "tracked_job_id": tracked_id, "scheduler_job_id": job.scheduler_job_id,
                  "machine_id": job.machine_id, "state": job.state, "exit_code": job.exit_code,
                  "outcome": outcome, "revision": revision}), woken=woken, skipped=skipped)
    if woken != [record.assignment_id]:
        raise AssignmentError("work_control_unavailable", 503)
    _audit_completion_wakes(context.plane_runtime, tx, owner_id=record.owner_id,
                            tracked_id=tracked_id, revision=revision, outcome=outcome, woken=woken)
    return True


def _apply_and_wake(db, row: Dict[str, Any], *, state: str, exit_code: Optional[str],
                    outcome_name: str) -> TrackedJobRecord:
    if outcome_name not in {"completed", "failed", "cancelled", "orphaned"}:
        raise RepositoryConflictError("remote_job_completion_unconfirmed",
                                      code="remote_job_completion_unconfirmed")
    context = _tracked_context(db)
    assignments = context.plane_runtime.repositories.assignments
    tracked_id = row["tracked_job_id"]
    with context.plane_runtime.transaction() as tx:
        record = context.repository.apply_poll(
            tx,
            owner_id=row["owner_user_id"],
            tracked_job_id=tracked_id,
            expected_fail_count=int(row.get("fail_count") or 0),
            expected_last_polled_at=row.get("last_polled_at"),
            state=state,
            exit_code=exit_code,
            terminal=True,
            fail_count=0,
            polled_at=int(time.time()),
        )
        revision = record.finished_at
        woken, skipped = _wake_subscribers(
            assignments, tx, owner_id=row["owner_user_id"],
            event_key=WAKE_EVENT_PREFIX + tracked_id,
            event_id="rj-wake:" + tracked_id, revision=revision,
            event_digest=_outcome_digest({
                "event": "remote_job_terminal",
                "tracked_job_id": tracked_id,
                "scheduler_job_id": row.get("scheduler_job_id"),
                "machine_id": row.get("machine_id"),
                "state": state,
                "exit_code": exit_code,
                "outcome": outcome_name,
                "revision": revision,
            }))
        if woken:
            _audit_completion_wakes(context.plane_runtime, tx,
                                    owner_id=row["owner_user_id"], tracked_id=tracked_id,
                                    revision=revision, outcome=outcome_name, woken=woken)
    if woken or skipped:
        logger.info("remote-job wake job=%s outcome=%s woken=%d skipped=%s",
                    tracked_id, outcome_name, len(woken), skipped)
    return record


def _orphan(db, row: Dict[str, Any]) -> None:
    # atomic apply+wake: a crash rolls both back and the next cycle re-probes and retries the delivery
    _apply_and_wake(db, row, state="orphaned", exit_code=row.get("exit_code"),
                    outcome_name="orphaned")


def _touch_fail(db, row: Dict[str, Any], fail_count: int) -> None:
    _apply(
        db,
        row,
        state=row.get("state") or "submitted",
        exit_code=row.get("exit_code"),
        terminal=False,
        fail_count=int(fail_count),
    )


def render_job_card(*, job_id: str, machine_label: str, state: str,
                    exit_code: Optional[str] = None, terminal: bool = False,
                    output_tail: Optional[str] = None, component_id: str,
                    job_name: Optional[str] = None) -> Dict[str, Any]:
    from astralprims import Alert, Card, CodeBlock, Table, Text
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
        content.append(Text(content="Output", variant="caption"))
        content.append(CodeBlock(code=_clip_output(output_tail), language=""))
    card = Card(title=f"Remote job {job_id} — {label}", content=content)
    card.id = component_id
    return card.to_dict()


def _clip_output(text: str) -> str:
    import re
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\x1b]", "", str(text or ""))
    if len(cleaned) > _OUTPUT_CLIP:
        cleaned = "…(truncated)…\n" + cleaned[-_OUTPUT_CLIP:]
    return cleaned


def _probe_state(orch, row: Dict[str, Any]) -> Dict[str, Any]:
    from agents.remote_observe.mcp_tools import _parse_sacct_json, _parse_squeue_json
    from orchestrator import remote_machines
    from orchestrator.credential_manager import CredentialNotConfigured, CredentialUndecryptable
    from orchestrator.remote_machines import MachineNotFound
    from orchestrator.remote_transport import get_transport

    try:
        target = remote_machines.build_target(
            plane_source_from_orchestrator(orch),
            orch.credential_manager,
            row["owner_user_id"],
            row["machine_id"],
        )
    except (MachineNotFound, CredentialNotConfigured, CredentialUndecryptable):
        return {"orphan": True}
    except Exception:  # noqa: BLE001
        return {"transient": True}
    # Poller must never trust-on-first-use a host key here
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
        st = jobs[0]["state"] or "RUNNING"
        term = is_terminal_state(st)
        out = {"state": st, "terminal": term, "exit_code": None, "machine": label,
               "confirmed": True}
        if term and row.get("output_path"):
            out["output_tail"] = _tail(tx, target, row["output_path"])
        return out
    hist = tx.run(target, ["sacct", "-j", jid, "--json", "-X"], timeout=30.0, retryable=True)
    if not hist.ok:
        return {"transient": True, "machine": label}
    acct = _parse_sacct_json(hist.stdout)
    if acct:
        st = acct[0]["state"] or "COMPLETED"
        term = is_terminal_state(st)
        out = {"state": st, "terminal": term, "exit_code": acct[0].get("exit_code") or "",
               "machine": label, "confirmed": True}
        if term and row.get("output_path"):
            out["output_tail"] = _tail(tx, target, row["output_path"])
        return out
    if _base_state(row.get("state")) in ("RUNNING", "COMPLETING", "PENDING", "CONFIGURING"):
        return {"transient": True, "machine": label}
    return {"state": row.get("state") or "submitted", "terminal": False,
            "exit_code": None, "machine": label}


def _tail(tx, target, path: str) -> Optional[str]:
    try:
        r = tx.run(target, ["tail", "-n", str(_TAIL_LINES), path], timeout=20.0, retryable=True)
        return r.stdout if r.ok else None
    except Exception:  # noqa: BLE001
        return None


async def poll_once(orch) -> None:
    import asyncio
    source = plane_source_from_orchestrator(orch)
    rows = await asyncio.to_thread(list_open, source)
    for row in rows:
        try:
            await _poll_one(orch, row)
        except Exception:  # noqa: BLE001
            logger.warning("tracked-job poll failed for %s", row.get("tracked_job_id"),
                           exc_info=True)


async def _poll_one(orch, row: Dict[str, Any]) -> None:
    import asyncio
    source = plane_source_from_orchestrator(orch)
    outcome = await asyncio.to_thread(_probe_state, orch, row)
    if outcome.get("terminal") and outcome.get("confirmed") is False:
        outcome = {"transient": True}

    if outcome.get("skip"):
        return
    if outcome.get("orphan"):
        await asyncio.to_thread(_orphan, source, row)
        await _push_component(orch, row, state="orphaned", exit_code=None, terminal=True)
        return
    if outcome.get("transient"):
        fails = int(row.get("fail_count") or 0) + 1
        if fails >= _FAIL_CEILING:
            await asyncio.to_thread(_orphan, source, row)
            await _push_component(orch, row, state="orphaned", exit_code=None, terminal=True)
        else:
            await asyncio.to_thread(_touch_fail, source, row, fails)
        return

    new_state = outcome["state"]
    terminal = bool(outcome.get("terminal"))
    exit_code = outcome.get("exit_code")
    changed = (_base_state(new_state) != _base_state(row.get("state"))) or terminal != bool(row.get("terminal"))
    if terminal:
        await asyncio.to_thread(
            _apply_and_wake, source, row, state=new_state, exit_code=exit_code,
            outcome_name=_job_outcome(new_state, exit_code,
                                      confirmed=bool(outcome.get("confirmed", True))))
    else:
        await asyncio.to_thread(_apply, source, row, state=new_state, exit_code=exit_code,
                                terminal=False, fail_count=0)
    if changed:
        await _push_component(orch, row, state=new_state, exit_code=exit_code,
                              terminal=terminal, output_tail=outcome.get("output_tail"))
    if terminal and row.get("notify_on_finish") and not row.get("notified"):
        await _notify_finish(orch, row, new_state, exit_code)
        await asyncio.to_thread(_mark_notified, source, row)


async def _push_component(orch, row: Dict[str, Any], *, state: str, exit_code: Optional[str],
                          terminal: bool, output_tail: Optional[str] = None) -> None:
    import asyncio
    chat_id = row.get("chat_id")
    component_id = row.get("component_id")
    user_id = row.get("owner_user_id")
    if not (chat_id and component_id and user_id):
        return
    label = row.get("machine_id") or "?"
    try:
        from orchestrator import remote_machines
        m = await asyncio.to_thread(
            remote_machines.get_machine,
            plane_source_from_orchestrator(orch),
            user_id,
            row["machine_id"],
        )
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
    except Exception:  # noqa: BLE001
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
