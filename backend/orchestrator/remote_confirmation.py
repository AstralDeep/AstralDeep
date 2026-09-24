"""Durable single-use approval cards for destructive remote-compute and computer-use
operations. The dispatch gate calls evaluate(); approvals re-enter it via
handle_decision(), stored through astralplane's remote_proposals.
"""

from __future__ import annotations

import asyncio
import contextvars
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

PROPOSAL_TTL_S = 900

MUTATING_AGENT_ID = "remote-compute-1"

DESTRUCTIVE_CLASSIFICATION: Dict[str, Any] = {
    "make_directory": "never",
    "upload_file": "if_exists",
    "remove_path": "always",
    "cancel_job": "always",
    "signal_process": "always",
    "control_service": {"by_action": ["stop", "disable", "restart"]},
    "manage_package": {"by_action": ["remove"]},
    "submit_job": "never",
    "run_job": "never",
}

_MARKER = "_remote_op_proposal_id"

_PENDING_CARDS: Dict[tuple, tuple] = {}


_RETRY_GRACE: Dict[Tuple[Any, ...], Tuple[str, float]] = {}


def _forget_pending(proposal_id: str) -> None:
    for key, (pid, _exp) in list(_PENDING_CARDS.items()):
        if pid == proposal_id:
            _PENDING_CARDS.pop(key, None)


class AgentConfirmationPolicy:
    def __init__(self, *, agent_id: str, classification: Dict[str, Any], machine_key: str,
                 gate_unclassified_unattended: bool, unattended_allowed: frozenset,
                 card_title: str, card_caption: str, summary, machine_label,
                 is_destructive=None, refusal_text: Optional[str] = None,
                 auto_continue: bool = False, dedupe_pending: bool = False,
                 machine_id=None, card_as_result: bool = False, on_approved=None,
                 retry_grace_s: float = 0.0):
        self.agent_id = agent_id
        self.classification = classification
        self.machine_key = machine_key
        self.gate_unclassified_unattended = gate_unclassified_unattended
        self.unattended_allowed = unattended_allowed
        self.card_title = card_title
        self.card_caption = card_caption
        self.summary = summary
        self.machine_label = machine_label
        self.is_destructive = is_destructive
        self.refusal_text = refusal_text or "confirmation_required: approve the operation to proceed."
        self.auto_continue = auto_continue
        self.dedupe_pending = dedupe_pending
        self.machine_id = machine_id
        self.card_as_result = card_as_result
        self.on_approved = on_approved
        self.retry_grace_s = float(retry_grace_s or 0.0)


def _computer_use_label(orch, user_id: str, ref) -> str:
    try:
        registry = getattr(orch, "computer_hosts", None)
        if registry is not None:
            return registry.resolve(user_id, ref).name
    except Exception:  # noqa: BLE001
        pass
    return "your computer"


def _computer_use_machine_id(orch, user_id: str, args: Dict[str, Any]) -> str:
    try:
        registry = getattr(orch, "computer_hosts", None)
        if registry is not None:
            return registry.resolve(user_id, args.get("computer")).host_id
    except Exception:  # noqa: BLE001
        pass
    return str(args.get("computer") or "unresolved")


def _computer_use_on_approved(orch, row) -> None:
    from orchestrator import computer_use_policy
    sessions = getattr(orch, "computer_sessions", None)
    if sessions is None:
        return
    for session in sessions.live_for_owner(row.owner_id):
        if not row.machine_id or session.host_id == row.machine_id:
            session.grant_terminal(computer_use_policy.TERMINAL_GRANT_S)


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
            card_as_result=True,
            on_approved=_computer_use_on_approved,
            retry_grace_s=computer_use_policy.APPROVAL_RETRY_GRACE_S,
        ),
    }


GATED_AGENT_IDS = frozenset({MUTATING_AGENT_ID, "computer-use-1"})


def policy_for(agent_id: Optional[str]) -> Optional[AgentConfirmationPolicy]:
    if not agent_id:
        return None
    return _policies().get(agent_id)


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
    try:
        from audit.recorder import get_recorder
        rec = get_recorder()
        if rec is None:
            return
        rec.record_blocking(_audit_event(user_id, action_type, description, **kw))
    except Exception:  # noqa: BLE001
        logger.debug("remote_op audit failed (%s)", action_type, exc_info=True)


async def _audit_async(user_id: Optional[str], action_type: str, description: str, **kw) -> None:
    try:
        from audit.recorder import get_recorder
        rec = get_recorder()
        if rec is None:
            return
        await rec.record(_audit_event(user_id, action_type, description, **kw))
    except Exception:  # noqa: BLE001
        logger.debug("remote_op audit failed (%s)", action_type, exc_info=True)


def _canonical_args(args: Dict[str, Any]) -> str:
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


def classification_for(tool_name: str, agent_id: Optional[str] = None) -> Any:
    policy = policy_for(agent_id) if agent_id else None
    table = policy.classification if policy is not None else DESTRUCTIVE_CLASSIFICATION
    return table.get(tool_name)


def is_destructive_unattended(tool_name: str, args: Dict[str, Any],
                              agent_id: Optional[str] = None) -> bool:
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
                return True
            return bool((res.data or {}).get("exists"))
        except Exception:  # noqa: BLE001
            return True
    return True


def _no_live_human(orch, websocket) -> bool:
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


def _create_proposal(orch, user_id: str, chat_id: str | None, agent_id: str,
                     tool_name: str, args: dict[str, Any], *, on_created=None) -> tuple[str, dict[str, Any]]:
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
        # Must run in this same transaction — no orphaned confirmation
        if on_created is not None:
            on_created(transaction, proposal_id)
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
    ], id=card_component_id(proposal_id) if policy.card_as_result else None).to_dict()
    return proposal_id, card


def card_component_id(proposal_id: str) -> str:
    return f"au_approval_{proposal_id}"


async def _replace_card(orch, row, title: str, body: str, variant: str = "default") -> None:
    if not row.conversation_id:
        return
    try:
        from astralprims import Card, Text
        policy = policy_for(row.agent_id)
        if policy is None or not policy.card_as_result:
            return
        comp = Card(title=title, content=[Text(content=body, variant="body")],
                    id=card_component_id(str(row.proposal_id))).to_dict()
        cid = card_component_id(str(row.proposal_id))

        async def _mutation():
            return await orch.workspace.aupsert(row.conversation_id, row.owner_id, [comp],
                                                force_component_id=cid)
        ops = await orch.run_detached_conversation_mutation(
            chat_id=row.conversation_id, user_id=row.owner_id, mutation=_mutation)
        if ops:
            await orch.send_ui_upsert(None, row.conversation_id, row.owner_id, ops)
    except Exception:  # noqa: BLE001
        logger.debug("remote_op card replacement failed", exc_info=True)


def evaluate(orch, websocket, agent_id: Optional[str], tool_name: str,
             args: Dict[str, Any], chat_id: Optional[str], user_id: Optional[str]):
    from astralprims import Alert
    policy = policy_for(agent_id)
    if policy is None:
        return None
    machine_ref = args.get(policy.machine_key)
    classification = classification_for(tool_name, agent_id)
    if classification is None and not policy.gate_unclassified_unattended:
        return None

    # Must run before the destructiveness and marker checks below
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
        return None

    from persistent_agents.dispatch_context import current_dispatch
    if current_dispatch() is not None and classification == "if_exists":
        destructive = True
    elif policy.is_destructive is not None:
        destructive = bool(policy.is_destructive(tool_name, args))
    else:
        destructive = _is_destructive(orch, user_id, tool_name, args, classification)
    if not destructive:
        return None

    marker = args.get(_MARKER)
    if marker:
        ok = _consume_if_valid(orch, str(marker), user_id, tool_name, args)
        args.pop(_MARKER, None)
        if ok:
            _audit_sync(user_id, "remote_op.consumed",
                        f"approved & consumed {tool_name}", proposal_id=str(marker),
                        machine_id=machine_ref, verb=tool_name, chat_id=chat_id)
            return None
        _audit_sync(user_id, "remote_op.approval_invalid",
                    f"invalid approval for {tool_name}", proposal_id=str(marker),
                    machine_id=machine_ref, verb=tool_name, outcome="failure", chat_id=chat_id)
        return ("This approval is no longer valid (already used, expired, or the "
                "arguments changed). Re-request the operation.",
                [Alert(message="Approval no longer valid — re-request the operation.",
                       variant="error").to_dict()])

    pending_key = (user_id, agent_id, tool_name, _fingerprint(args))
    if policy.retry_grace_s > 0:
        grace = _RETRY_GRACE.pop(pending_key, None)
        if grace is not None and grace[1] > time.time():
            _audit_sync(user_id, "remote_op.retry_under_grace",
                        f"retry of approved {tool_name} admitted", proposal_id=grace[0],
                        machine_id=machine_ref, verb=tool_name, chat_id=chat_id)
            return None
    if policy.dedupe_pending:
        pending = _PENDING_CARDS.get(pending_key)
        if pending is not None and pending[1] > time.time():
            return (policy.refusal_text, [Alert(message="Still waiting for your approval of the "
                                                "action above.", variant="warning").to_dict()])
    pid, card = _create_proposal(orch, user_id, chat_id, agent_id, tool_name, args)
    if policy.dedupe_pending:
        _PENDING_CARDS[pending_key] = (pid, time.time() + PROPOSAL_TTL_S)
    return (policy.refusal_text, [card])


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
        logger.warning("remote_op_decision refused (not owner/found): id=%s actor=%s", proposal_id, user_id)
        await _audit_async(user_id, "remote_op.decision_refused",
                           "decision refused (not owner or not found)",
                           proposal_id=proposal_id, outcome="failure")
        await _say("That confirmation is not available.")
        return
    from persistent_agents.approvals import handle_linked_remote_decision
    if await handle_linked_remote_decision(orch, websocket, user_id, row, decision):
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
        await _replace_card(orch, row, "Declined", f"{row.summary} — not done.")
        return

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
    await _replace_card(orch, row, "Approved ✓", f"{row.summary} — running now.")
    _pol = policy_for(row.agent_id)
    if _pol is not None and _pol.on_approved is not None:
        try:
            _pol.on_approved(orch, row)
        except Exception:  # noqa: BLE001
            logger.debug("remote_op on_approved hook failed", exc_info=True)

    # Goes back through the gate to re-validate — not a direct call
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
    policy = policy_for(row.agent_id)
    not_attempted = _not_attempted_code(result)
    if policy is not None and policy.retry_grace_s > 0 and not_attempted:
        _RETRY_GRACE[(user_id, row.agent_id, row.tool_name, _fingerprint(dict(row.arguments)))] = (
            str(proposal_id), time.time() + policy.retry_grace_s)
        logger.info("remote_op approved but not attempted (%s): retry grace %.0fs id=%s",
                    not_attempted, policy.retry_grace_s, proposal_id)
    if policy is not None and policy.auto_continue and row.conversation_id and websocket is not None:
        outcome = _continuation_text(row, result, retry_grace_s=policy.retry_grace_s if not_attempted else 0)
        try:
            # Needs a detached context: the click's fence goes stale mid-task
            asyncio.create_task(
                orch._serialized_chat(websocket, outcome, row.conversation_id,
                                      "✓ Approved — continuing", user_id=user_id),
                context=_detached_turn_context(orch),
                name=f"remote-op-continue-{row.proposal_id}")
        except Exception:  # noqa: BLE001
            logger.debug("remote_op auto-continue failed", exc_info=True)


def _detached_turn_context(orch) -> contextvars.Context:
    from orchestrator.detached_context import detached_context
    return detached_context(orch)


def _result_data(result) -> Tuple[Any, Any]:
    data: Any = None
    error: Any = None
    if result is not None:
        raw = getattr(result, "result", None)
        error = getattr(result, "error", None)
        data = raw.get("_data") if isinstance(raw, dict) and "_data" in raw else raw
    return data, error


def _not_attempted_code(result) -> Optional[str]:
    data, error = _result_data(result)
    code = data.get("code") if isinstance(data, dict) else None
    if code is None and isinstance(error, dict):
        message = str(error.get("message") or "")
        code = "paused" if message.startswith("paused") else None
    return "paused" if code == "paused" else None


def _continuation_text(row, result, retry_grace_s: float = 0) -> str:
    data, error = _result_data(result)
    try:
        rendered = json.dumps(data, default=str)[:4000] if data is not None else "(no data)"
    except Exception:  # noqa: BLE001
        rendered = str(data)[:4000]
    if error:
        rendered = f"ERROR: {str((error or {}).get('message') if isinstance(error, dict) else error)[:1000]}"
    if retry_grace_s > 0:
        return (f"[The user tapped Approve.] `{row.tool_name}` was approved but could NOT run yet: "
                f"the computer is paused because someone is using it. Result: {rendered}\n\n"
                f"Call wait (seconds=10), then resume_session; once it is active, call `{row.tool_name}` "
                f"again with EXACTLY the same arguments {json.dumps(dict(row.arguments), default=str)[:1500]} — "
                f"the approval is kept for {int(retry_grace_s)} seconds, so that one retry needs no new "
                "confirmation. Then continue the task and report the outcome in plain language.")
    return (f"[The user tapped Approve.] `{row.tool_name}` has now been carried out on the computer "
            f"(do not call it again for this step). Result: {rendered}\n\n"
            "Continue the task from here — take any further actions on the computer that are "
            "needed, then report the outcome to the user in plain language.")
