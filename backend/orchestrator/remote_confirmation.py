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

import asyncio
import hashlib
import json
import logging
import time
import uuid
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple

from astralplane.repositories.remote_proposals import RemoteOperationProposalRecord
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    plane_source_from_orchestrator,
    repository_from,
)

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

#: (owner, agent, tool, args_fingerprint) → (proposal_id, expires_at) for cards
#: still awaiting an answer; a repeat reach re-uses the card. Cleared on any
#: decision. In-process only — a restart simply allows a fresh card.
_PENDING_CARDS: Dict[tuple, tuple] = {}


def _forget_pending(proposal_id: str) -> None:
    for key, (pid, _exp) in list(_PENDING_CARDS.items()):
        if pid == proposal_id:
            _PENDING_CARDS.pop(key, None)


# ── per-agent policy (feature 076 generalization) ─────────────────────────────
#
# The mechanism (durable single-use proposal → approval card → re-entry through
# the full gate stack) is shared by every machine-control agent; only the verb
# classification, the unattended rule, the card copy and the machine label
# differ. Feature 063's behaviour is byte-identical under its own policy entry.

class AgentConfirmationPolicy:
    """What the shared gate needs to know about one machine-control agent."""

    def __init__(self, *, agent_id: str, classification: Dict[str, Any], machine_key: str,
                 gate_unclassified_unattended: bool, unattended_allowed: frozenset,
                 card_title: str, card_caption: str, summary, machine_label,
                 is_destructive=None, refusal_text: Optional[str] = None,
                 auto_continue: bool = False, dedupe_pending: bool = False,
                 machine_id=None):
        self.agent_id = agent_id
        self.classification = classification
        self.machine_key = machine_key
        # False (063): read verbs — classification None — pass straight through,
        # even unattended (status polls). True (076): an unattended turn may run
        # ONLY ``unattended_allowed``; everything else is refused before any
        # frame reaches the machine.
        self.gate_unclassified_unattended = gate_unclassified_unattended
        self.unattended_allowed = unattended_allowed
        self.card_title = card_title      # may contain {host}
        self.card_caption = card_caption
        self.summary = summary            # (orch, user_id, tool_name, args) -> str
        self.machine_label = machine_label  # (orch, user_id, machine_ref) -> str
        # Optional (tool_name, args) -> bool predicate for classifications the
        # 063 vocabulary cannot express (076's shell-app rule); None ⇒ the
        # shared ``_is_destructive`` vocabulary decides.
        self.is_destructive = is_destructive
        # Model-facing text of the refusal that accompanies the card. 076 tells
        # the model in so many words to END ITS TURN (live finding: without it
        # the model re-requested approval and burned the whole turn budget).
        self.refusal_text = refusal_text or "confirmation_required: approve the operation to proceed."
        # 076: after the owner approves and the verb ran, replay a continuation
        # turn so the model finishes the task with the result in hand.
        self.auto_continue = auto_continue
        # 076: a repeat reach of an operation whose card is still pending gets
        # the same answer without a second card. 063 keeps one card per reach
        # (its tests pin that), so this is opt-in per policy.
        self.dedupe_pending = dedupe_pending
        # Optional (orch, user_id, args) -> str resolver for the machine id
        # stored on the proposal row (the store requires a non-empty id). 076
        # verbs may omit ``computer`` when one host is online, so the id is
        # resolved from the registry rather than read off the arguments.
        self.machine_id = machine_id


def _computer_use_label(orch, user_id: str, ref) -> str:
    try:
        registry = getattr(orch, "computer_hosts", None)
        if registry is not None:
            return registry.resolve(user_id, ref).name
    except Exception:  # noqa: BLE001 — a label only
        pass
    return "your computer"


def _computer_use_machine_id(orch, user_id: str, args: Dict[str, Any]) -> str:
    try:
        registry = getattr(orch, "computer_hosts", None)
        if registry is not None:
            return registry.resolve(user_id, args.get("computer")).host_id
    except Exception:  # noqa: BLE001 — an unresolvable host still gets a card
        pass
    return str(args.get("computer") or "unresolved")


def _computer_use_summary(orch, user_id: str, tool_name: str, args: Dict[str, Any]) -> str:
    from orchestrator import computer_use_policy
    return computer_use_policy.summary_for(
        tool_name, args, _computer_use_label(orch, user_id, args.get("computer")))


def _policies() -> Dict[str, AgentConfirmationPolicy]:
    from orchestrator import computer_use_policy
    return {
        MUTATING_AGENT_ID: AgentConfirmationPolicy(
            agent_id=MUTATING_AGENT_ID,
            classification=DESTRUCTIVE_CLASSIFICATION,
            machine_key="machine_id",
            gate_unclassified_unattended=False,
            unattended_allowed=frozenset(),
            card_title="Confirm a destructive operation",
            card_caption=("This changes the remote machine and cannot be undone by me. "
                          "Approve to run it exactly as shown, or decline."),
            summary=_summary,
            machine_label=_machine_label,
        ),
        computer_use_policy.AGENT_ID: AgentConfirmationPolicy(
            agent_id=computer_use_policy.AGENT_ID,
            classification=computer_use_policy.DESTRUCTIVE_CLASSIFICATION,
            machine_key="computer",
            gate_unclassified_unattended=True,
            unattended_allowed=computer_use_policy.UNATTENDED_ALLOWED,
            card_title=computer_use_policy.CARD_TITLE,
            card_caption=computer_use_policy.CARD_CAPTION,
            summary=_computer_use_summary,
            machine_label=_computer_use_label,
            is_destructive=computer_use_policy.is_destructive,
            refusal_text=computer_use_policy.REFUSAL_TEXT,
            auto_continue=True,
            dedupe_pending=True,
            machine_id=_computer_use_machine_id,
        ),
    }


#: Agents whose tool calls the dispatch gate routes through this module.
GATED_AGENT_IDS = frozenset({MUTATING_AGENT_ID, "computer-use-1"})


def policy_for(agent_id: Optional[str]) -> Optional[AgentConfirmationPolicy]:
    if not agent_id:
        return None
    return _policies().get(agent_id)


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
        row = remote_machines.get_machine(
            plane_source_from_orchestrator(orch),
            user_id,
            machine_id,
        )
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

def classification_for(tool_name: str, agent_id: Optional[str] = None) -> Any:
    policy = policy_for(agent_id) if agent_id else None
    table = policy.classification if policy is not None else DESTRUCTIVE_CLASSIFICATION
    return table.get(tool_name)


def is_destructive_unattended(tool_name: str, args: Dict[str, Any],
                              agent_id: Optional[str] = None) -> bool:
    """Conservatively classify an unattended/MCP call without remote I/O.

    ``if_exists`` cannot be proven safe without contacting the remote machine,
    so the unattended boundary refuses it. Conditional action classifiers can
    be decided entirely from the submitted arguments. For an agent whose policy
    gates unclassified verbs unattended (076), everything outside its
    ``unattended_allowed`` set counts as refused here too.
    """

    policy = policy_for(agent_id) if agent_id else None
    if policy is not None and policy.gate_unclassified_unattended \
            and tool_name not in policy.unattended_allowed:
        return True
    classification = classification_for(tool_name, agent_id)
    if classification in (None, "never"):
        return False
    if classification == "always" or classification == "if_exists":
        return True
    if isinstance(classification, dict) and "by_action" in classification:
        return args.get("action") in set(classification["by_action"])
    if policy is not None and policy.is_destructive is not None:
        return bool(policy.is_destructive(tool_name, args))
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
                plane_source_from_orchestrator(orch),
                orch.credential_manager,
                user_id,
                args.get("machine_id"),
            )
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

def _proposal_context(orch) -> PlaneRepositoryContext:
    plane = orch.runtime_composition.plane
    repository, runtime = repository_from(
        "remote_operation_proposals",
        plane_runtime=plane.runtime,
        repositories=plane.repositories,
        legacy_database=None,
    )
    return PlaneRepositoryContext(repository=repository, plane_runtime=runtime)


def _consume_if_valid(
    orch,
    proposal_id: str,
    user_id: str,
    tool_name: str,
    args: Dict[str, Any],
) -> bool:
    """Atomically consume an APPROVED, matching, unexpired proposal. Single-use: the
    guarded ``UPDATE ... WHERE status='approved' RETURNING`` yields the row to exactly
    one caller (FR-031)."""
    context = _proposal_context(orch)
    with context.transaction() as transaction:
        consumed = context.repository.consume_if_valid(
            transaction,
            owner_id=user_id,
            proposal_id=proposal_id,
            expected_tool_name=tool_name,
            expected_args_fingerprint=_fingerprint(args),
            consumed_at=int(time.time()),
        )
    return consumed is not None


def _create_proposal(orch, user_id: str, chat_id: Optional[str], agent_id: str,
                     tool_name: str, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    from astralprims import Button, Card, Text
    policy = policy_for(agent_id) or policy_for(MUTATING_AGENT_ID)
    proposal_id = uuid.uuid4().hex
    now = int(time.time())
    summary = policy.summary(orch, user_id, tool_name, args)
    machine_ref = args.get(policy.machine_key)
    if policy.machine_id is not None:
        machine_ref = policy.machine_id(orch, user_id, args)
    context = _proposal_context(orch)
    with context.transaction() as transaction:
        context.repository.create(
            transaction,
            RemoteOperationProposalRecord(
                proposal_id=proposal_id,
                owner_id=user_id,
                conversation_id=chat_id,
                machine_id=str(machine_ref or ""),
                agent_id=agent_id,
                tool_name=tool_name,
                args_fingerprint=_fingerprint(args),
                arguments={
                    key: value
                    for key, value in args.items()
                    if not str(key).startswith("_")
                },
                summary=summary,
                status="pending",
                created_at=now,
                expires_at=now + PROPOSAL_TTL_S,
            ),
        )
    logger.info("remote_op proposal created: %s verb=%s owner=%s", proposal_id, tool_name, user_id)
    _audit_sync(user_id, "remote_op.proposed", f"proposed {tool_name}: {summary}",
                proposal_id=proposal_id, machine_id=machine_ref, verb=tool_name,
                outcome="in_progress", chat_id=chat_id)
    title = policy.card_title
    if "{host}" in title:
        title = title.replace("{host}", policy.machine_label(orch, user_id, machine_ref))
    card = Card(title=title, content=[
        Text(content=summary, variant="body"),
        Text(content=policy.card_caption, variant="caption"),
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
    policy = policy_for(agent_id)
    if policy is None:
        return None
    machine_ref = args.get(policy.machine_key)
    classification = classification_for(tool_name, agent_id)
    if classification is None and not policy.gate_unclassified_unattended:
        return None  # read verb — permitted unattended (FR-044's status-poll allowance)

    # FR-033: EVERY mutating verb needs a live human — checked BEFORE destructiveness
    # (so before any transport contact, including the if_exists stat), before any
    # proposal row, and before marker consumption (an approval can never be spent by
    # a machine turn). Applies regardless of granted scope.
    if _no_live_human(orch, websocket) and tool_name not in policy.unattended_allowed:
        logger.info("remote_op refused (unattended): verb=%s owner=%s", tool_name, user_id)
        _audit_sync(user_id, "remote_op.refused_unattended",
                    f"refused unattended {tool_name}", machine_id=machine_ref,
                    verb=tool_name, outcome="failure", chat_id=chat_id)
        return ("unattended_refused: remote-control operations need a live person; "
                "re-issue it interactively.",
                [Alert(message="Remote-control operations can't run unattended — "
                               "re-issue this interactively.", variant="error").to_dict()])

    if classification is None:
        return None  # 076 observe/input verb on an attended turn — session-gated by the agent

    if policy.is_destructive is not None:
        destructive = bool(policy.is_destructive(tool_name, args))
    else:
        destructive = _is_destructive(orch, user_id, tool_name, args, classification)
    if not destructive:
        return None  # non-destructive mutating verb — the explicit grant already gated it

    marker = args.get(_MARKER)
    if marker:
        ok = _consume_if_valid(orch, str(marker), user_id, tool_name, args)
        args.pop(_MARKER, None)  # never hand the marker to the agent
        if ok:
            _audit_sync(user_id, "remote_op.consumed",
                        f"approved & consumed {tool_name}", proposal_id=str(marker),
                        machine_id=machine_ref, verb=tool_name, chat_id=chat_id)
            return None  # approved, fresh, matching, single-use consumed -> proceed
        _audit_sync(user_id, "remote_op.approval_invalid",
                    f"invalid approval for {tool_name}", proposal_id=str(marker),
                    machine_id=machine_ref, verb=tool_name, outcome="failure", chat_id=chat_id)
        return ("This approval is no longer valid (already used, expired, or the "
                "arguments changed). Re-request the operation.",
                [Alert(message="Approval no longer valid — re-request the operation.",
                       variant="error").to_dict()])

    # First reach of a destructive verb on an attended turn: refuse with a proposal.
    # A repeat reach of the SAME operation while its card is still pending gets the
    # same answer without a second card (076 live finding: a model that does not
    # stop can otherwise paper the chat with identical cards).
    pending_key = (user_id, agent_id, tool_name, _fingerprint(args))
    if policy.dedupe_pending:
        pending = _PENDING_CARDS.get(pending_key)
        if pending is not None and pending[1] > time.time():
            return (policy.refusal_text, [Alert(message="Still waiting for your approval of the "
                                                "action above.", variant="warning").to_dict()])
    pid, card = _create_proposal(orch, user_id, chat_id, agent_id, tool_name, args)
    if policy.dedupe_pending:
        _PENDING_CARDS[pending_key] = (pid, time.time() + PROPOSAL_TTL_S)
    return (policy.refusal_text, [card])


# ── the remote_op_decision ui_event handler (async) ────────────────────────────

async def handle_decision(orch, websocket, user_id: str, payload: Dict[str, Any]) -> None:
    from astralprims import Alert
    proposal_id = (payload or {}).get("proposal_id")
    decision = (payload or {}).get("decision")

    async def _say(message: str, variant: str = "warning") -> None:
        await orch.send_ui_render(websocket, [Alert(message=message, variant=variant).to_dict()], target="chat")

    context = _proposal_context(orch)
    row = None
    if proposal_id:
        row = await asyncio.to_thread(
            context.call,
            context.repository.get,
            owner_id=user_id,
            proposal_id=str(proposal_id),
        )
    if row is None:
        # Not found, or belongs to a different user (US3-4) — refuse + audit.
        logger.warning("remote_op_decision refused (not owner/found): id=%s actor=%s", proposal_id, user_id)
        await _audit_async(user_id, "remote_op.decision_refused",
                           "decision refused (not owner or not found)",
                           proposal_id=proposal_id, outcome="failure")
        await _say("That confirmation is not available.")
        return
    now = int(time.time())
    _mid, _verb, _cid = row.machine_id, row.tool_name, row.conversation_id
    _forget_pending(str(proposal_id))
    if row.status != "pending":
        await _say("This request was already handled.")
        return
    if now > row.expires_at:
        def _expire():
            with context.transaction() as transaction:
                return context.repository.expire_if_pending(
                    transaction,
                    owner_id=user_id,
                    proposal_id=str(proposal_id),
                    observed_at=now,
                )

        expired = await asyncio.to_thread(_expire)
        if expired is None:
            await _say("This request was already handled.")
            return
        await _audit_async(user_id, "remote_op.expired", f"approval expired for {_verb}",
                           proposal_id=proposal_id, machine_id=_mid, verb=_verb,
                           outcome="failure", chat_id=_cid)
        await _say("This request expired — please re-request the operation.")
        return
    if decision != "approve":
        def _decline():
            with context.transaction() as transaction:
                return context.repository.decide_if_pending(
                    transaction,
                    owner_id=user_id,
                    proposal_id=str(proposal_id),
                    decision="declined",
                    decided_at=now,
                )

        declined = await asyncio.to_thread(_decline)
        if declined is None:
            await _say("This request was already handled.")
            return
        await _audit_async(user_id, "remote_op.declined", f"declined {_verb}",
                           proposal_id=proposal_id, machine_id=_mid, verb=_verb,
                           outcome="failure", chat_id=_cid)
        await _say("Declined — nothing was changed.", variant="info")
        return

    # Approve atomically (single-use guard against a double-click / concurrent tab).
    def _approve():
        with context.transaction() as transaction:
            return context.repository.decide_if_pending(
                transaction,
                owner_id=user_id,
                proposal_id=str(proposal_id),
                decision="approved",
                decided_at=now,
            )

    approved = await asyncio.to_thread(_approve)
    if approved is None:
        await _say("This request was already handled.")
        return
    await _audit_async(user_id, "remote_op.approved", f"approved {_verb}",
                       proposal_id=proposal_id, machine_id=_mid, verb=_verb, chat_id=_cid)

    # Re-enter the tool with the STORED args + the consume marker. The gate re-checks
    # the full stack, matches the approved proposal by (owner, verb, args), consumes it
    # single-use, strips the marker, and dispatches — FR-033's not-a-direct-dispatch.
    stored_args = dict(row.arguments)
    stored_args[_MARKER] = proposal_id
    tc = SimpleNamespace(id="remote-op", function=SimpleNamespace(
        name=row.tool_name, arguments=json.dumps(stored_args)))
    logger.info("remote_op approved -> re-dispatch: id=%s verb=%s", proposal_id, row.tool_name)
    result = await orch.execute_single_tool(
        websocket,
        tc,
        {row.tool_name: row.agent_id},
        row.conversation_id,
        user_id=user_id,
    )
    # 076: the approved verb ran, but the model's turn ended when it asked. Hand
    # the outcome back as a continuation turn on the SAME chat so the task
    # finishes without the user having to say "go on". A visible one-liner
    # stands in for the machine-authored text in the transcript.
    policy = policy_for(row.agent_id)
    if policy is not None and policy.auto_continue and row.conversation_id and websocket is not None:
        outcome = _continuation_text(row, result)
        try:
            asyncio.create_task(orch.handle_chat_message(
                websocket, outcome, row.conversation_id,
                display_message="✓ Approved — continuing", user_id=user_id))
        except Exception:  # noqa: BLE001 — the approved verb already ran; continuation is best-effort
            logger.debug("remote_op auto-continue failed", exc_info=True)


def _continuation_text(row, result) -> str:
    """The machine-authored user turn that resumes the task after an approval."""
    data: Any = None
    error: Any = None
    if result is not None:
        raw = getattr(result, "result", None)
        error = getattr(result, "error", None)
        data = raw.get("_data") if isinstance(raw, dict) and "_data" in raw else raw
    try:
        rendered = json.dumps(data, default=str)[:4000] if data is not None else "(no data)"
    except Exception:  # noqa: BLE001
        rendered = str(data)[:4000]
    if error:
        rendered = f"ERROR: {str((error or {}).get('message') if isinstance(error, dict) else error)[:1000]}"
    return (f"[The user tapped Approve.] `{row.tool_name}` has now been carried out on the computer "
            f"(do not call it again for this step). Result: {rendered}\n\n"
            "Continue the task from here — take any further actions on the computer that are "
            "needed, then report the outcome to the user in plain language.")
