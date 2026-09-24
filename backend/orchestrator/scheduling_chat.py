"""Exposes recurring-job scheduling as a chat meta-tool: the LLM proposes via
schedule_recurring_task, validation reuses scheduler/governance.py and cron.py, and
nothing is created until the user's explicit consent card is approved via
handle_decision.
"""

import asyncio
import logging
import math
import time
import uuid
from typing import Any, Dict, List, Optional

from astralprims import Alert, Button, Card, Text
from orchestrator.plane_repository_context import plane_source_from_orchestrator
from shared.feature_flags import flags

logger = logging.getLogger("Orchestrator.SchedulingChat")

META_AGENT_ID = "__scheduler__"

PROPOSAL_TTL_S = 900

_VALID_KINDS = ("one_shot", "interval", "cron")

SYSTEM_PROMPT_ADDENDUM = """
RECURRING / SCHEDULED WORK (schedule_recurring_task):
- This system DOES support scheduled and recurring background jobs. NEVER tell the user you
  cannot schedule recurring tasks.
- When the user asks for work on a schedule ("every Monday...", "daily digest", "remind me in
  2 hours", "compile X weekly"), call `schedule_recurring_task`. The user confirms via a consent
  card before anything is created — propose, don't ask permission in prose first.
- schedule_kind/schedule_expr: "interval" with "<N><unit>" (s/m/h/d, e.g. "1d", "12h");
  "cron" with a 5-field cron expression (e.g. "0 9 * * 1" = Mondays 09:00); "one_shot" with an
  ISO-8601 datetime.
- Put WHAT the job should do each run in `instruction`, phrased as a standalone request.
"""


def meta_tool_definitions() -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "schedule_recurring_task",
                "description": (
                    "Propose a scheduled (recurring or one-shot) background job. The user "
                    "sees a consent card with the cadence and instruction and must approve "
                    "before the job is created. Use for any 'every day/week/Monday...', "
                    "'remind me', or 'compile X on a schedule' request."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Short human name for the job"},
                        "instruction": {"type": "string", "description": "What to do on each run, phrased as a standalone request"},
                        "schedule_kind": {"type": "string", "enum": list(_VALID_KINDS)},
                        "schedule_expr": {"type": "string", "description": "interval: '<N><unit>' (s/m/h/d); cron: 5-field expression; one_shot: ISO-8601 datetime"},
                        "timezone": {"type": "string", "description": "IANA timezone, default UTC"},
                        "agent_id": {"type": "string", "description": "Optional agent whose tools the job may use (must already be enabled for the user)"},
                    },
                    "required": ["name", "instruction", "schedule_kind", "schedule_expr"],
                },
            },
        },
    ]


def should_inject(draft_agent_id: Optional[str]) -> bool:
    return flags.is_enabled("scheduling_chat") and not draft_agent_id


async def _audit(user_id: str, action_type: str, description: str,
                 correlation_id: str, outcome: str = "success",
                 chat_id: Optional[str] = None,
                 inputs_meta: Optional[Dict] = None) -> None:
    try:
        from datetime import datetime, timezone

        from audit.recorder import get_recorder
        from audit.schemas import AuditEventCreate
        rec = get_recorder()
        if rec is None:
            return
        await rec.record(AuditEventCreate(
            actor_user_id=user_id or "unknown",
            auth_principal=user_id or "unknown",
            event_class="schedule",
            action_type=action_type,
            description=description[:1024],
            conversation_id=chat_id,
            correlation_id=correlation_id,
            outcome=outcome,
            inputs_meta=inputs_meta or {},
            started_at=datetime.now(timezone.utc),
        ))
    except Exception:
        logger.debug("scheduling_chat: audit record failed (%s)", action_type, exc_info=True)


def _proposals(orch) -> Dict[str, Dict[str, Any]]:
    if not hasattr(orch, "_schedule_proposals"):
        orch._schedule_proposals = {}
    return orch._schedule_proposals


def _scheduler_store(orch):
    from scheduler.store import ScheduledJobStore

    injected = getattr(orch, "scheduled_job_store", None)
    if injected is not None:
        return injected
    source = plane_source_from_orchestrator(orch)
    return ScheduledJobStore(
        coordinator=getattr(orch, "work_admission", None),
        plane_runtime=source.plane_runtime,
        plane_repositories=source.plane_repositories,
    )


def human_cadence(schedule_kind: str, schedule_expr: str, tz: str) -> str:
    if schedule_kind == "interval":
        return f"every {schedule_expr} ({tz})"
    if schedule_kind == "cron":
        return f"on the cron schedule `{schedule_expr}` ({tz})"
    return f"once, at {schedule_expr} ({tz})"


def _validate_proposal(orch, user_id: str, args: Dict[str, Any]):
    from agentic_settings import (SCHEDULE_MAX_ACTIVE_JOBS_PER_USER,
                                  SCHEDULE_MIN_INTERVAL_SECONDS)
    from scheduler.cron import ScheduleError, compute_next_run_ms
    from scheduler.governance import GovernanceError, validate_new_job

    name = str(args.get("name") or "").strip()[:120]
    instruction = str(args.get("instruction") or "").strip()[:2000]
    schedule_kind = str(args.get("schedule_kind") or "").strip()
    schedule_expr = str(args.get("schedule_expr") or "").strip()[:100]
    tz = str(args.get("timezone") or "UTC").strip()[:64]
    agent_id = (str(args.get("agent_id")).strip() or None) if args.get("agent_id") else None

    if not name or not instruction:
        raise ValueError("A job needs both a name and an instruction.")
    if schedule_kind not in _VALID_KINDS:
        raise ValueError(f"schedule_kind must be one of {_VALID_KINDS}.")
    if not schedule_expr:
        raise ValueError("schedule_expr is required.")
    if agent_id:
        if agent_id not in getattr(orch, "agent_cards", {}) or orch._is_draft_agent(agent_id):
            raise ValueError(f"Unknown agent '{agent_id}' — omit agent_id or use a live agent.")

    store = _scheduler_store(orch)
    try:
        validate_new_job(
            active_job_count=store.count_active(user_id),
            max_active=SCHEDULE_MAX_ACTIVE_JOBS_PER_USER,
            schedule_kind=schedule_kind,
            schedule_expr=schedule_expr,
            min_interval_seconds=SCHEDULE_MIN_INTERVAL_SECONDS,
        )
        next_run = compute_next_run_ms(schedule_kind, schedule_expr, tz,
                                       int(time.time() * 1000))
    except (GovernanceError, ScheduleError) as exc:
        raise ValueError(str(exc)) from exc

    cleaned = {"name": name, "instruction": instruction,
               "schedule_kind": schedule_kind, "schedule_expr": schedule_expr,
               "timezone": tz, "agent_id": agent_id}
    return cleaned, next_run


async def handle_meta_tool(orch, tool_name: str, args: Dict[str, Any], *,
                           user_id: str, chat_id: Optional[str], websocket):
    from shared.protocol import MCPResponse

    if tool_name != "schedule_recurring_task":
        return MCPResponse(error={"message": f"Unknown scheduling tool '{tool_name}'",
                                  "retryable": False})
    try:
        cleaned, next_run = await asyncio.to_thread(
            _validate_proposal, orch, user_id, args or {})
    except ValueError as exc:
        return MCPResponse(error={"message": str(exc), "retryable": False})

    proposal_id = uuid.uuid4().hex
    _proposals(orch)[proposal_id] = {
        "user_id": user_id, "chat_id": chat_id, "args": cleaned,
        "created_at": time.time(),
    }
    await _audit(user_id, "schedule.proposed",
                 f"Chat proposed scheduled job '{cleaned['name']}'",
                 correlation_id=proposal_id, chat_id=chat_id,
                 inputs_meta={"kind": cleaned["schedule_kind"],
                              "expr": cleaned["schedule_expr"]})
    logger.info("schedule proposal %s user=%s name=%r kind=%s expr=%s",
                proposal_id, user_id, cleaned["name"], cleaned["schedule_kind"],
                cleaned["schedule_expr"])

    agent_line = (f"Uses agent: {cleaned['agent_id']} (with only the permissions "
                  "you currently grant it)." if cleaned["agent_id"]
                  else "Runs as a normal assistant turn that may use whichever of "
                       "your enabled agents' tools the request needs.")
    card_content = [
        Text(content=cleaned["instruction"]),
        Text(content=(f"Runs {human_cadence(cleaned['schedule_kind'], cleaned['schedule_expr'], cleaned['timezone'])}. "
                      f"{agent_line} Results are delivered in-app to this chat. "
                      "Nothing is scheduled until you approve."),
             variant="caption"),
    ]
    granting = await _consented_scopes_for(orch, user_id, cleaned["agent_id"] or None)
    subject = (f"**{cleaned['agent_id']}**" if cleaned["agent_id"]
               else "this job (any of your enabled agents' tools)")
    card_content.append(Text(
        content=("**Approving grants durable consent.** To run on your behalf "
                 "while you are signed out, this job stores a revocable "
                 "authorization for "
                 f"{subject} limited to: "
                 f"**{', '.join(granting) if granting else 'no scopes yet'}**. "
                 "Each run acts under fresh authority narrowed to those scopes "
                 "AND whatever you allow at that moment — never more. It expires "
                 "within 365 days, and you can revoke it any time in Settings → "
                 "Personalization → Schedule; signing out everywhere revokes it too."),
        variant="markdown"))
    card_content.extend([
        Button(label="Approve & create schedule", action="schedule_decision",
               payload={"proposal_id": proposal_id, "decision": "approve"}),
        Button(label="Cancel", action="schedule_decision", variant="secondary",
               payload={"proposal_id": proposal_id, "decision": "discard"}),
    ])
    card = Card(title=f"⏰ Schedule proposal: {cleaned['name']}",
                content=card_content).to_dict()
    return MCPResponse(
        result={"status": "proposed", "proposal_id": proposal_id,
                "message": "Consent card shown — the user must approve before the job exists."},
        ui_components=[card],
    )


async def _consented_scopes_for(orch, user_id: str,
                                agent_id: Optional[str]) -> List[str]:
    if agent_id:
        names = await asyncio.to_thread(
            orch.tool_permissions.get_enabled_scope_names, user_id, agent_id)
        return list(names or [])
    from orchestrator.tool_visibility import enabled_scope_union
    from scheduler.runner import _UNREVIEWED_MUTATING_SCOPES
    try:
        names = await asyncio.to_thread(enabled_scope_union, orch, user_id)
    except Exception as exc:
        logger.warning("consented scope union failed user=%s: %s", user_id, exc)
        return []
    return [n for n in (names or []) if n not in _UNREVIEWED_MUTATING_SCOPES]


async def _capture_consent(orch, user_id: str, agent_id: Optional[str],
                          consented: List[str], *, selected_session=None):
    try:
        from orchestrator.session_consent import ConsentSession
        if not isinstance(selected_session, ConsentSession):
            logger.info("consent_capture: no selected live session for user=%s "
                        "— job created without unattended authority", user_id)
            return None
        selected_session.reference(user_id)
        grants = orch.offline_grants
        return await asyncio.to_thread(
            grants.prepare_capture, user_id, selected_session, agent_id)
    except Exception:
        logger.warning("consent preparation unavailable user=%s agent=%s", user_id, agent_id)
        return None


async def handle_decision(orch, websocket, user_id: str, payload: Dict[str, Any]) -> None:
    proposal_id = str(payload.get("proposal_id") or "")
    decision = str(payload.get("decision") or "")
    prop = _proposals(orch).get(proposal_id)
    registration = getattr(orch, "ui_sessions", {}).get(websocket)

    def current_registration():
        if not (isinstance(registration, dict) and registration.get("sub") == user_id
                and getattr(orch, "ui_sessions", {}).get(websocket) is registration):
            return False
        expiry = registration.get("exp")
        return type(expiry) in (int, float) and math.isfinite(expiry) and expiry > time.time()

    async def _say(message: str, variant: str = "info"):
        if not current_registration():
            return
        await orch.send_ui_render(websocket, [Alert(message=message, variant=variant).to_dict()],
                                  target="chat")

    if not prop or prop["user_id"] != user_id:
        await _say("This schedule proposal is no longer available — ask again to recreate it.",
                   "warning")
        return
    if time.time() - prop["created_at"] > PROPOSAL_TTL_S:
        _proposals(orch).pop(proposal_id, None)
        await _say("This schedule proposal expired — ask again to recreate it.", "warning")
        return

    if decision != "approve":
        _proposals(orch).pop(proposal_id, None)
        await _audit(user_id, "schedule.discarded",
                     f"User declined scheduled job '{prop['args']['name']}'",
                     correlation_id=proposal_id, chat_id=prop.get("chat_id"))
        await _say("Cancelled — nothing was scheduled.")
        return

    args = prop["args"]
    from orchestrator.session_consent import select_consent_session
    selected = await select_consent_session(
        websocket, principal=registration, store=getattr(orch, "web_sessions", None))
    if not current_registration():
        return
    try:
        cleaned, next_run = await asyncio.to_thread(_validate_proposal, orch, user_id, args)
    except ValueError as exc:
        _proposals(orch).pop(proposal_id, None)
        await _say(f"That schedule can no longer be created: {exc}", "warning")
        return

    consented: List[str] = await _consented_scopes_for(
        orch, user_id, cleaned["agent_id"] or None)

    if not current_registration():
        return
    prepared = await _capture_consent(
        orch, user_id, cleaned["agent_id"] or None, consented,
        selected_session=selected)
    if not current_registration():
        return

    store = _scheduler_store(orch)
    consent_kwargs = {"consent_current": current_registration}
    if prepared is not None:
        consent_kwargs.update(prepared_consent=prepared, offline_grants=orch.offline_grants)
    from orchestrator.offline_grant import OfflineGrantError
    from scheduler.store import ScheduleActionError
    try:
        job = await asyncio.to_thread(
            store.create_job,
            user_id, name=cleaned["name"], instruction=cleaned["instruction"],
            schedule_kind=cleaned["schedule_kind"], schedule_expr=cleaned["schedule_expr"],
            timezone=cleaned["timezone"], consented_scopes=consented,
            agent_id=cleaned["agent_id"], target_chat_id=prop.get("chat_id"),
            next_run_at=next_run, offline_grant_id=None, **consent_kwargs)
    except OfflineGrantError:
        await _say("Your sign-in changed or consent expired. Approve this schedule again.", "warning")
        return
    except ScheduleActionError as exc:
        message = ("Your sign-in changed or consent expired. Approve this schedule again."
                   if exc.code == "schedule_consent_unavailable" else
                   "Couldn’t confirm the schedule was saved. Check your schedules before trying again.")
        await _say(message, "warning")
        return
    grant_id = job.get("offline_grant_id")
    _proposals(orch).pop(proposal_id, None)
    if grant_id:
        agent_id = cleaned["agent_id"] or None
        await _audit(user_id, "schedule.consent_captured",
                     (f"Captured durable offline consent for agent '{agent_id}'"
                      if agent_id else "Captured durable offline consent for an agent-less job"),
                     correlation_id=grant_id,
                     inputs_meta={"agent_id": agent_id, "consented_scopes": consented,
                                  "grant_id": grant_id, "durable_days": 365})
    await _audit(user_id, "schedule.create",
                 f"Created scheduled job '{cleaned['name']}' from chat consent",
                 correlation_id=proposal_id, chat_id=prop.get("chat_id"),
                 inputs_meta={"job_id": job["id"], "kind": cleaned["schedule_kind"],
                              "consented_scopes": consented,
                              "durable_consent": bool(grant_id)})
    logger.info("schedule created from chat: job=%s user=%s name=%r consent=%s",
                job["id"], user_id, cleaned["name"], bool(grant_id))

    if grant_id:
        offline_hint = (" It can run while you are signed out; revoke that access "
                        "any time in Settings → Personalization → Schedule (signing "
                        "out everywhere also revokes it).")
    else:
        offline_hint = (" It cannot run while you are signed out yet — grant offline "
                        "access in Settings → Personalization → Schedule.")
    if not current_registration():
        return
    await orch.send_ui_render(websocket, [
        Alert(message=(f"Scheduled '{cleaned['name']}' — runs "
                       f"{human_cadence(cleaned['schedule_kind'], cleaned['schedule_expr'], cleaned['timezone'])}."
                       + offline_hint),
              variant="success").to_dict(),
        Button(label="Manage schedules", action="chrome_open",
               payload={"surface": "personalization"}, variant="secondary").to_dict(),
    ], target="chat")
    await asyncio.sleep(0)
