"""Durable destructive-operation confirmation for the remote-compute agents (US3).

The net-new mechanism (spec contracts/confirmation.md): a durable, single-use,
expiring, user-bound, argument-bound approval for destructive remote operations,
enforced at the shared dispatch gate (``_run_gate_stack``) so a differently-named
verb, a parallel batch, or a chained hop cannot bypass it, and refused outright on
any turn with no live human principal (FR-033).

Two entry points:
- ``evaluate(...)`` — called SYNCHRONOUSLY from the gate (the orchestrator wraps it
  in ``asyncio.to_thread`` because it touches the DB / SFTP). Returns ``None`` to let
  the call proceed, or ``(message, [component_dicts])`` to refuse (the orchestrator
  wraps that in a ``GateRefusal``).
- ``handle_decision(...)`` — the ``remote_op_decision`` ui_event handler (async):
  validates ownership/TTL/single-use and, on approval, re-enters the tool with the
  STORED arguments so the operation faces the full gate stack once more.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("RemoteConfirmation")

PROPOSAL_TTL_S = 900  # 15 minutes; absolute server time, clock-skew-safe (FR-031)

MUTATING_AGENT_ID = "remote-compute-1"

#: SINGLE SOURCE OF TRUTH for destructive classification (the gate enforces this;
#: the agent's registry imports it so verb + classification cannot drift — FR-028).
#: Values: "never" | "always" | "if_exists" | {"by_action": [<action>, ...]}.
DESTRUCTIVE_CLASSIFICATION: Dict[str, Any] = {
    "make_directory": "never",
    "upload_file": "if_exists",
    "remove_path": "always",
    "cancel_job": "always",
    "signal_process": "always",
    "control_service": {"by_action": ["stop", "disable", "restart"]},
    "manage_package": {"by_action": ["remove"]},
    "submit_job": "never",
    "run_job": "never",  # US4: submitting a job creates new work (not destructive)
}

_MARKER = "_remote_op_proposal_id"


# ── hash-chained audit (FR-047/FR-048) ─────────────────────────────────────────
#
# Every proposal transition (proposed / approved / declined / expired / consumed)
# and every refusal (unattended, non-owner, invalid approval) is appended to the
# hash-chained audit under ``agent_lifecycle``, correlated by ``proposal_id`` and
# naming the acting user + machine + verb — enough to reconstruct after the fact
# what was done to which machine, by whom, under which approval. NO secrets and NO
# argument values are recorded (FR-049) — only the machine id, the verb, and the
# args *fingerprint* live in the row (via the proposal); ``inputs_meta`` carries a
# hash-safe subset. Best-effort: an audit failure never blocks or breaks the gate.

def _audit_event(user_id: Optional[str], action_type: str, description: str, *,
                 proposal_id: Optional[str] = None, machine_id: Optional[str] = None,
                 verb: Optional[str] = None, outcome: str = "success",
                 chat_id: Optional[str] = None):
    from datetime import datetime, timezone

    from audit.schemas import AuditEventCreate
    meta: Dict[str, Any] = {}
    if machine_id:
        meta["machine_id"] = str(machine_id)
    if verb:
        meta["verb"] = str(verb)
    if proposal_id:
        meta["proposal_id"] = str(proposal_id)
    return AuditEventCreate(
        actor_user_id=user_id or "unknown",
        auth_principal=user_id or "unknown",
        event_class="agent_lifecycle",
        action_type=action_type,
        description=description[:1024],
        conversation_id=chat_id,
        correlation_id=proposal_id or uuid.uuid4().hex,
        outcome=outcome,
        inputs_meta=meta,
        started_at=datetime.now(timezone.utc),
    )


def _audit_sync(user_id: Optional[str], action_type: str, description: str, **kw) -> None:
    """Record a proposal-lifecycle event from the SYNC gate (runs in a worker
    thread via asyncio.to_thread, so a blocking insert is safe)."""
    try:
        from audit.recorder import get_recorder
        rec = get_recorder()
        if rec is None:
            return
        rec.record_blocking(_audit_event(user_id, action_type, description, **kw))
    except Exception:  # noqa: BLE001 — audit is best-effort, never fatal
        logger.debug("remote_op audit failed (%s)", action_type, exc_info=True)


async def _audit_async(user_id: Optional[str], action_type: str, description: str, **kw) -> None:
    """Record a proposal-lifecycle event from the async decision handler."""
    try:
        from audit.recorder import get_recorder
        rec = get_recorder()
        if rec is None:
            return
        await rec.record(_audit_event(user_id, action_type, description, **kw))
    except Exception:  # noqa: BLE001
        logger.debug("remote_op audit failed (%s)", action_type, exc_info=True)


# ── fingerprint / summary ─────────────────────────────────────────────────────

def _canonical_args(args: Dict[str, Any]) -> str:
    """Canonical JSON of the model-supplied args (excluding injected ``_`` keys)."""
    clean = {k: v for k, v in args.items() if not str(k).startswith("_")}
    return json.dumps(clean, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(args: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_args(args).encode("utf-8")).hexdigest()


def _machine_label(orch, user_id: str, machine_id: Optional[str]) -> str:
    if not machine_id:
        return "?"
    try:
        from orchestrator import remote_machines
        row = remote_machines.get_machine(orch.history.db, user_id, machine_id)
        if row:
            return str(row.get("label") or machine_id)
    except Exception:  # noqa: BLE001
        pass
    return str(machine_id)


def _summary(orch, user_id: str, tool_name: str, args: Dict[str, Any]) -> str:
    m = _machine_label(orch, user_id, args.get("machine_id"))
    if tool_name == "remove_path":
        return f"Delete {args.get('path')} on {m}" + (" (recursive)" if args.get("recursive") else "")
    if tool_name == "cancel_job":
        return f"Cancel job {args.get('job_id')} on {m}"
    if tool_name == "signal_process":
        return f"Send {args.get('signal')} to process {args.get('pid')} on {m}"
    if tool_name == "control_service":
        return f"{str(args.get('action') or '').capitalize()} service {args.get('service_name')} on {m}"
    if tool_name == "manage_package":
        return f"Remove package {args.get('package_name')} on {m}"
    if tool_name == "upload_file":
        return f"Overwrite {args.get('remote_path')} on {m}"
    return f"{tool_name} on {m}"


# ── classification ────────────────────────────────────────────────────────────

def classification_for(tool_name: str) -> Any:
    return DESTRUCTIVE_CLASSIFICATION.get(tool_name)


def is_destructive_unattended(tool_name: str, args: Dict[str, Any]) -> bool:
    """Conservatively classify an unattended/MCP call without remote I/O.

    ``if_exists`` cannot be proven safe without contacting the remote machine,
    so the unattended boundary refuses it. Conditional action classifiers can
    be decided entirely from the submitted arguments.
    """

    classification = classification_for(tool_name)
    if classification in (None, "never"):
        return False
    if classification == "always" or classification == "if_exists":
        return True
    if isinstance(classification, dict) and "by_action" in classification:
        return args.get("action") in set(classification["by_action"])
    return True


def _is_destructive(orch, user_id: str, tool_name: str, args: Dict[str, Any], classification: Any) -> bool:
    if classification == "never":
        return False
    if classification == "always":
        return True
    if isinstance(classification, dict) and "by_action" in classification:
        return args.get("action") in set(classification["by_action"])
    if classification == "if_exists":
        # A read-only stat decides: overwriting existing content is destructive.
        from orchestrator import remote_machines
        from orchestrator.remote_transport import get_transport
        try:
            target = remote_machines.build_target(
                orch.history.db, orch.credential_manager, user_id, args.get("machine_id"))
            res = get_transport().stat(target, str(args.get("remote_path") or ""), timeout=15.0)
            if not res.ok:
                return True  # cannot tell -> treat as destructive (fail-closed)
            return bool((res.data or {}).get("exists"))
        except Exception:  # noqa: BLE001
            return True
    return True  # unknown classification -> fail-closed


def _no_live_human(orch, websocket) -> bool:
    """Every mutating remote-control verb needs a live human on an interactive
    channel (FR-033) — destructive ones additionally to show a proposal + collect
    an approval. A machine turn, a background/async VirtualWebSocket, or a None
    socket has no such human — refuse there."""
    if websocket is None:
        return True
    try:
        from orchestrator.async_tasks import VirtualWebSocket
        if isinstance(websocket, VirtualWebSocket):
            return True
    except Exception:  # noqa: BLE001
        pass
    claims = orch.ui_sessions.get(websocket) or getattr(websocket, "machine_claims", None)
    if isinstance(claims, dict) and claims.get("machine_class"):
        return True
    return False


# ── proposal store ────────────────────────────────────────────────────────────

def _consume_if_valid(db, proposal_id: str, user_id: str, tool_name: str, args: Dict[str, Any]) -> bool:
    """Atomically consume an APPROVED, matching, unexpired proposal. Single-use: the
    guarded ``UPDATE ... WHERE status='approved' RETURNING`` yields the row to exactly
    one caller (FR-031)."""
    row = db.fetch_one(
        "SELECT owner_user_id, verb, status, expires_at, args_fingerprint "
        "FROM remote_operation_proposal WHERE proposal_id = ?", (proposal_id,))
    if row is None:
        return False
    if row["owner_user_id"] != user_id or row["verb"] != tool_name or row["status"] != "approved":
        return False
    if int(time.time()) > int(row["expires_at"]):
        return False
    if row["args_fingerprint"] != _fingerprint(args):
        return False  # arguments must match exactly the ones approved
    consumed = db.fetch_one(
        "UPDATE remote_operation_proposal SET status='consumed', consumed_at=? "
        "WHERE proposal_id=? AND status='approved' RETURNING proposal_id",
        (int(time.time()), proposal_id))
    return consumed is not None


def _create_proposal(orch, user_id: str, chat_id: Optional[str], agent_id: str,
                     tool_name: str, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    from astralprims import Button, Card, Text
    db = orch.history.db
    proposal_id = uuid.uuid4().hex
    now = int(time.time())
    summary = _summary(orch, user_id, tool_name, args)
    db.execute(
        """INSERT INTO remote_operation_proposal
           (proposal_id, owner_user_id, chat_id, machine_id, agent_id, verb, args_json,
            args_fingerprint, summary, status, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (proposal_id, user_id, chat_id, args.get("machine_id"), agent_id, tool_name,
         _canonical_args(args), _fingerprint(args), summary, now, now + PROPOSAL_TTL_S))
    logger.info("remote_op proposal created: %s verb=%s owner=%s", proposal_id, tool_name, user_id)
    _audit_sync(user_id, "remote_op.proposed", f"proposed {tool_name}: {summary}",
                proposal_id=proposal_id, machine_id=args.get("machine_id"), verb=tool_name,
                outcome="in_progress", chat_id=chat_id)
    card = Card(title="Confirm a destructive operation", content=[
        Text(content=summary, variant="body"),
        Text(content="This changes the remote machine and cannot be undone by me. "
                     "Approve to run it exactly as shown, or decline.", variant="caption"),
        Button(label="Approve", action="remote_op_decision",
               payload={"proposal_id": proposal_id, "decision": "approve"}),
        Button(label="Decline", action="remote_op_decision", variant="secondary",
               payload={"proposal_id": proposal_id, "decision": "decline"}),
    ]).to_dict()
    return proposal_id, card


# ── the gate hook (called from _run_gate_stack via asyncio.to_thread) ──────────

def evaluate(orch, websocket, agent_id: Optional[str], tool_name: str,
             args: Dict[str, Any], chat_id: Optional[str], user_id: Optional[str]):
    """Return None to PROCEED, or (message, [component_dicts]) to REFUSE.

    Mutates ``args`` to strip a consumed proposal marker so it never reaches the agent.
    """
    from astralprims import Alert
    if agent_id != MUTATING_AGENT_ID:
        return None
    classification = classification_for(tool_name)
    if classification is None:
        return None  # read verb — permitted unattended (FR-044's status-poll allowance)

    # FR-033: EVERY mutating verb needs a live human — checked BEFORE destructiveness
    # (so before any transport contact, including the if_exists stat), before any
    # proposal row, and before marker consumption (an approval can never be spent by
    # a machine turn). Applies regardless of granted scope.
    if _no_live_human(orch, websocket):
        logger.info("remote_op refused (unattended): verb=%s owner=%s", tool_name, user_id)
        _audit_sync(user_id, "remote_op.refused_unattended",
                    f"refused unattended {tool_name}", machine_id=args.get("machine_id"),
                    verb=tool_name, outcome="failure", chat_id=chat_id)
        return ("unattended_refused: remote-control operations need a live person; "
                "re-issue it interactively.",
                [Alert(message="Remote-control operations can't run unattended — "
                               "re-issue this interactively.", variant="error").to_dict()])

    if not _is_destructive(orch, user_id, tool_name, args, classification):
        return None  # non-destructive mutating verb — the explicit grant already gated it

    marker = args.get(_MARKER)
    if marker:
        ok = _consume_if_valid(orch.history.db, str(marker), user_id, tool_name, args)
        args.pop(_MARKER, None)  # never hand the marker to the agent
        if ok:
            _audit_sync(user_id, "remote_op.consumed",
                        f"approved & consumed {tool_name}", proposal_id=str(marker),
                        machine_id=args.get("machine_id"), verb=tool_name, chat_id=chat_id)
            return None  # approved, fresh, matching, single-use consumed -> proceed
        _audit_sync(user_id, "remote_op.approval_invalid",
                    f"invalid approval for {tool_name}", proposal_id=str(marker),
                    machine_id=args.get("machine_id"), verb=tool_name, outcome="failure", chat_id=chat_id)
        return ("This approval is no longer valid (already used, expired, or the "
                "arguments changed). Re-request the operation.",
                [Alert(message="Approval no longer valid — re-request the operation.",
                       variant="error").to_dict()])

    # First reach of a destructive verb on an attended turn: refuse with a proposal.
    _pid, card = _create_proposal(orch, user_id, chat_id, agent_id, tool_name, args)
    return ("confirmation_required: approve the operation to proceed.", [card])


# ── the remote_op_decision ui_event handler (async) ────────────────────────────

async def handle_decision(orch, websocket, user_id: str, payload: Dict[str, Any]) -> None:
    from astralprims import Alert
    db = orch.history.db
    proposal_id = (payload or {}).get("proposal_id")
    decision = (payload or {}).get("decision")

    async def _say(message: str, variant: str = "warning") -> None:
        await orch.send_ui_render(websocket, [Alert(message=message, variant=variant).to_dict()], target="chat")

    row = None
    if proposal_id:
        row = await db.afetch_one(
            "SELECT * FROM remote_operation_proposal WHERE proposal_id = ?", (proposal_id,))
    if row is None or row["owner_user_id"] != user_id:
        # Not found, or belongs to a different user (US3-4) — refuse + audit.
        logger.warning("remote_op_decision refused (not owner/found): id=%s actor=%s", proposal_id, user_id)
        await _audit_async(user_id, "remote_op.decision_refused",
                           "decision refused (not owner or not found)",
                           proposal_id=proposal_id, outcome="failure")
        await _say("That confirmation is not available.")
        return
    now = int(time.time())
    _mid, _verb, _cid = row["machine_id"], row["verb"], row["chat_id"]
    if row["status"] != "pending":
        await _say("This request was already handled.")
        return
    if now > int(row["expires_at"]):
        await db.aexecute("UPDATE remote_operation_proposal SET status='expired' "
                          "WHERE proposal_id=? AND status='pending'", (proposal_id,))
        await _audit_async(user_id, "remote_op.expired", f"approval expired for {_verb}",
                           proposal_id=proposal_id, machine_id=_mid, verb=_verb,
                           outcome="failure", chat_id=_cid)
        await _say("This request expired — please re-request the operation.")
        return
    if decision != "approve":
        await db.aexecute("UPDATE remote_operation_proposal SET status='declined', decided_at=? "
                          "WHERE proposal_id=? AND status='pending'", (now, proposal_id))
        await _audit_async(user_id, "remote_op.declined", f"declined {_verb}",
                           proposal_id=proposal_id, machine_id=_mid, verb=_verb,
                           outcome="failure", chat_id=_cid)
        await _say("Declined — nothing was changed.", variant="info")
        return

    # Approve atomically (single-use guard against a double-click / concurrent tab).
    approved = await db.afetch_one(
        "UPDATE remote_operation_proposal SET status='approved', decided_at=? "
        "WHERE proposal_id=? AND status='pending' RETURNING proposal_id", (now, proposal_id))
    if approved is None:
        await _say("This request was already handled.")
        return
    await _audit_async(user_id, "remote_op.approved", f"approved {_verb}",
                       proposal_id=proposal_id, machine_id=_mid, verb=_verb, chat_id=_cid)

    # Re-enter the tool with the STORED args + the consume marker. The gate re-checks
    # the full stack, matches the approved proposal by (owner, verb, args), consumes it
    # single-use, strips the marker, and dispatches — FR-033's not-a-direct-dispatch.
    stored_args = json.loads(row["args_json"])
    stored_args[_MARKER] = proposal_id
    tc = SimpleNamespace(id="remote-op", function=SimpleNamespace(
        name=row["verb"], arguments=json.dumps(stored_args)))
    logger.info("remote_op approved -> re-dispatch: id=%s verb=%s", proposal_id, row["verb"])
    await orch.execute_single_tool(
        websocket, tc, {row["verb"]: row["agent_id"]}, row["chat_id"], user_id=user_id)
