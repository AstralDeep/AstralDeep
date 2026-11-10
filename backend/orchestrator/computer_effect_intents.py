"""Durable outcome bookkeeping for Mac computer-use host effects: one intent row per effect
request is committed through the application AstralPlane chat-step store before the host frame
is sent and the outcome commits when the host answers, while reconciliation turns intents left
open by a lost answer outcome-unknown until the owner acknowledges or discards them on the
computer; only digests, redacted bounded summaries, and identifiers are persisted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Dict, Mapping, Optional, Tuple

from shared.phi_redactor import redact

logger = logging.getLogger("ComputerEffectIntents")

INTENT_KIND = "computer_effect_intent"
APPROVAL_KIND = "computer_approval_open"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_ERRORED = "errored"
STATUS_CANCELLED = "cancelled"
STATUS_INTERRUPTED = "interrupted"

RESUME_MARKER = "[Recovered]"
SUMMARY_CHARS = 300
MAX_LIVE_INTENTS = 512
MAX_OPEN_INTENTS_PER_CHAT = 64
RECONCILE_CHAT_LIMIT = 50
RECONCILE_STEP_LIMIT = 1000
RESUME_MAX_ACTIONS = 12

AUDIT_INTENT = "effect.intent"
AUDIT_COMMITTED = "effect.committed"
AUDIT_UNKNOWN = "effect.unknown"
AUDIT_ACKNOWLEDGED = "effect.acknowledged"


class EffectIntentError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class EffectIntentState(StrEnum):
    PENDING = "pending"
    COMMITTED = "committed"
    UNKNOWN = "unknown"
    ACKNOWLEDGED = "acknowledged"


@dataclass(frozen=True)
class EffectIntent:
    operation_id: str
    owner_id: str
    chat_id: str
    session_id: str
    host_id: str
    host_incarnation: str
    verb: str
    argument_digest: str
    summary: str
    state: EffectIntentState
    decision: Optional[str] = None
    created_at_ms: int = 0


def argument_digest(verb: str, args: Any) -> str:
    payload = json.dumps(
        {"verb": verb, "args": _public_args(args)},
        sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _public_args(args: Any) -> Any:
    if not isinstance(args, Mapping):
        return None
    return {str(key): args[key] for key in sorted(args, key=str) if not str(key).startswith("_")}


def _clip(value: Any, limit: int = 120) -> str:
    return " ".join(str(value or "").split())[:limit]


def _row_payload(intent: EffectIntent) -> str:
    return json.dumps({
        "operation_id": intent.operation_id,
        "verb": intent.verb,
        "argument_digest": intent.argument_digest,
        "host_id": intent.host_id,
        "host_incarnation": intent.host_incarnation,
        "session_id": intent.session_id,
        "summary": intent.summary,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _intent_from_row(owner_id: str, chat_id: str, row: Any) -> Optional[EffectIntent]:
    try:
        record = json.loads(row.args_truncated or "")
    except ValueError:
        return None
    if not isinstance(record, Mapping):
        return None
    verb = record.get("verb")
    digest = record.get("argument_digest")
    if not isinstance(verb, str) or not isinstance(digest, str):
        return None
    return EffectIntent(
        operation_id=str(row.step_id),
        owner_id=owner_id,
        chat_id=chat_id,
        session_id=str(record.get("session_id") or ""),
        host_id=str(record.get("host_id") or ""),
        host_incarnation=str(record.get("host_incarnation") or ""),
        verb=verb,
        argument_digest=digest,
        summary=str(record.get("summary") or ""),
        state=EffectIntentState.UNKNOWN,
    )


def resume_text(host_name: str, intents: Tuple[EffectIntent, ...], lost_approvals: Tuple[str, ...],
                decision: str) -> str:
    counted: Dict[str, int] = {}
    for intent in intents:
        counted[intent.verb] = counted.get(intent.verb, 0) + 1
    named = ", ".join(f"{verb} ×{count}" if count > 1 else verb
                      for verb, count in list(counted.items())[:RESUME_MAX_ACTIONS])
    if len(counted) > RESUME_MAX_ACTIONS:
        named += f" and {len(counted) - RESUME_MAX_ACTIONS} more action kinds"
    verdict = ("The owner acknowledged the uncertainty on the computer; treat the outcomes as unknown "
               "and verify current state before repeating any of them."
               if decision == "acknowledge" else
               "The owner discarded them as not having happened; do not repeat them on that basis alone.")
    lines = [
        f"{RESUME_MARKER} The connection to {host_name} was interrupted while these actions were in "
        f"flight: {named}. They were not re-run and their outcome is unknown. {verdict}",
    ]
    if lost_approvals:
        lines.append(f"{RESUME_MARKER} An approval card for {', '.join(lost_approvals[:RESUME_MAX_ACTIONS])} "
                     "was lost in the same interruption; it approved nothing and nothing ran from it.")
    return "\n".join(lines)


class EffectIntentLedger:
    def __init__(self, orch: Any, *, context: Any = None, conversations: Any = None):
        self._orch = orch
        self._injected = context
        self._conversations = conversations
        self._context: Any = None
        self._conversation_context: Any = None
        self._live: Dict[str, EffectIntent] = {}
        self._unknown: Dict[Tuple[str, str], Dict[str, EffectIntent]] = {}
        self._reconciled: set = set()
        self._resume_gates: Dict[Tuple[str, str], str] = {}
        self._approval_markers: Dict[str, Tuple[str, str]] = {}
        self._lost_approvals: Dict[Tuple[str, str], Dict[str, None]] = {}

    def _repository(self) -> Any:
        if self._injected is not None:
            return self._injected
        if self._context is None:
            from orchestrator.plane_repository_context import (
                PlaneRepositoryContext,
                plane_source_from_orchestrator,
                repository_from,
            )
            source = plane_source_from_orchestrator(self._orch)
            repository, runtime = repository_from(
                "chat_steps", plane_runtime=source.plane_runtime, repositories=source.plane_repositories)
            self._context = PlaneRepositoryContext(repository=repository, plane_runtime=runtime)
        return self._context

    def _conversation_repository(self) -> Any:
        if self._conversations is not None:
            return self._conversations
        if self._conversation_context is None:
            from orchestrator.plane_repository_context import (
                PlaneRepositoryContext,
                plane_source_from_orchestrator,
                repository_from,
            )
            source = plane_source_from_orchestrator(self._orch)
            repository, runtime = repository_from(
                "history", plane_runtime=source.plane_runtime, repositories=source.plane_repositories)
            self._conversation_context = PlaneRepositoryContext(
                repository=repository.conversations, plane_runtime=runtime)
        return self._conversation_context

    def live_count(self) -> int:
        return len(self._live)

    def unknown_for(self, owner_id: str, chat_id: str) -> Tuple[EffectIntent, ...]:
        return tuple(self._unknown.get((owner_id, chat_id or ""), {}).values())

    def lost_approvals(self, owner_id: str, chat_id: str) -> Tuple[str, ...]:
        return tuple(self._lost_approvals.get((owner_id, chat_id or ""), {}))

    async def begin_request(self, *, owner_id: str, host: Any, session_id: str, chat_id: Optional[str],
                            verb: str, args: Any, operation_id: str) -> EffectIntent:
        if not chat_id:
            raise EffectIntentError(
                "effect_intent_failed", "the action has no chat to record its intent in; start the session from a chat")
        if len(self._live) >= MAX_LIVE_INTENTS:
            raise EffectIntentError(
                "effect_intent_failed", "too many actions are awaiting outcomes; resolve them before starting more")
        bucket = self._unknown.get((owner_id, chat_id))
        if bucket:
            raise EffectIntentError(
                "chat_blocked_for_acknowledgment",
                f"{len(bucket)} earlier action(s) in this chat have unknown outcomes; the owner must resolve the "
                "outcome-unknown card on the computer before this chat runs more effects")
        open_count = sum(1 for item in self._live.values()
                         if item.owner_id == owner_id and item.chat_id == chat_id)
        if open_count >= MAX_OPEN_INTENTS_PER_CHAT:
            raise EffectIntentError(
                "effect_intent_failed", "this chat has too many actions awaiting outcomes; resolve them first")
        summary, _truncated = redact(
            json.dumps(_public_args(args), default=str, ensure_ascii=False) if isinstance(args, Mapping) else "",
            max_length=SUMMARY_CHARS)
        intent = EffectIntent(
            operation_id=operation_id,
            owner_id=owner_id,
            chat_id=chat_id,
            session_id=session_id,
            host_id=str(host.host_id),
            host_incarnation=str(getattr(host, "descriptor", {}).get("incarnation_id") or ""),
            verb=verb,
            argument_digest=argument_digest(verb, args),
            summary=_clip(summary),
            state=EffectIntentState.PENDING,
            created_at_ms=int(time.time() * 1000),
        )
        self._live[operation_id] = intent
        try:
            await self._write_intent_row(intent)
        except Exception as exc:  # noqa: BLE001
            self._live.pop(operation_id, None)
            logger.warning("effect intent write failed for %s (%s)", verb, exc)
            raise EffectIntentError(
                "effect_intent_failed",
                "the intent for this action could not be recorded, so the action was not sent") from exc
        await self._audit(intent, AUDIT_INTENT, f"effect intent committed before the {verb} request",
                          outcome="in_progress")
        return intent

    async def commit_outcome(self, operation_id: str, *, ok: bool, error_code: str = "") -> bool:
        intent = self._live.pop(operation_id, None)
        if intent is None or intent.state is not EffectIntentState.PENDING:
            return False
        status = STATUS_COMPLETED if ok else STATUS_ERRORED
        summary = "the host reported success" if ok else f"the host refused or failed: {_clip(error_code)}"
        if not await self._finish_row(intent, status=status, summary=summary):
            logger.warning("effect outcome commit lost for %s; reconciliation will report it unknown",
                           operation_id)
            await self._mark_unknown(intent, "its outcome row could not be closed")
            return False
        await self._audit(intent, AUDIT_COMMITTED,
                          f"effect outcome committed ({'success' if ok else 'error'})",
                          outcome="success" if ok else "failure")
        return True

    async def outcome_lost(self, intent: EffectIntent, *, reason: str) -> None:
        self._live.pop(intent.operation_id, None)
        await self._mark_unknown(intent, reason)

    async def reconcile_chat(self, owner_id: str, chat_id: str) -> Tuple[EffectIntent, ...]:
        if chat_id is None or (owner_id, chat_id) in self._reconciled:
            return ()
        self._reconciled.add((owner_id, chat_id))
        context = self._repository()
        try:
            rows = await asyncio.to_thread(
                context.call,
                context.repository.list_steps,
                owner_id=owner_id,
                conversation_id=chat_id,
                limit=RECONCILE_STEP_LIMIT,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("effect reconciliation could not read chat steps: %s", exc)
            self._reconciled.discard((owner_id, chat_id))
            return ()
        recovered: list = []
        for row in rows:
            if row.status.value != STATUS_IN_PROGRESS:
                continue
            if row.kind == INTENT_KIND:
                if row.step_id in self._live:
                    continue
                intent = _intent_from_row(owner_id, chat_id, row)
                if intent is not None:
                    recovered.append(await self._mark_unknown(intent, "recovered after an interruption"))
            elif row.kind == APPROVAL_KIND and row.step_id not in self._approval_markers:
                self._lost_marker(owner_id, chat_id, row)
        return tuple(recovered)

    def _lost_marker(self, owner_id: str, chat_id: str, row: Any) -> None:
        try:
            record = json.loads(row.args_truncated or "")
        except ValueError:
            record = {}
        tool = record.get("tool_name") if isinstance(record, Mapping) else None
        tool = str(tool or row.name or "an action")
        self._lost_approvals.setdefault((owner_id, chat_id), {})[tool] = None

    async def reconcile_owner(self, owner_id: str) -> Tuple[EffectIntent, ...]:
        try:
            chats = await asyncio.to_thread(
                self._conversation_repository().call,
                self._conversation_repository().repository.list_recent,
                owner_id=owner_id,
                limit=RECONCILE_CHAT_LIMIT,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("effect reconciliation could not list chats for %s: %s", owner_id[:8], exc)
            return ()
        recovered: list = []
        for record in chats:
            recovered.extend(await self.reconcile_chat(owner_id, record.conversation_id))
        return tuple(recovered)

    async def resolve_owner_unknowns(self, owner_id: str, host_id: str, *, decision: str
                                     ) -> Dict[str, Tuple[EffectIntent, ...]]:
        resolved: Dict[str, list] = {}
        for (owner, chat_id), bucket in list(self._unknown.items()):
            if owner != owner_id:
                continue
            for operation_id, intent in list(bucket.items()):
                if intent.host_id != host_id:
                    continue
                status = STATUS_INTERRUPTED if decision == "acknowledge" else STATUS_CANCELLED
                if not await self._finish_row(
                        intent, status=status,
                        summary=f"outcome unknown; {decision} by the owner on the computer"):
                    continue
                bucket.pop(operation_id, None)
                marked = replace(intent, state=EffectIntentState.ACKNOWLEDGED, decision=decision)
                resolved.setdefault(chat_id, []).append(marked)
                await self._audit(marked, AUDIT_ACKNOWLEDGED,
                                  f"outcome-unknown effect acknowledged ({decision})", outcome="success")
        return {chat: tuple(found) for chat, found in resolved.items()}

    def grant_resume_input(self, owner_id: str, chat_id: str, incarnation_id: str) -> bool:
        key = (owner_id, chat_id or "")
        if not chat_id or self._resume_gates.get(key) == incarnation_id:
            return False
        self._resume_gates[key] = incarnation_id
        return True

    def approval_opened(self, *, proposal_id: str, owner_id: str, chat_id: Optional[str],
                        tool_name: str, summary: str) -> None:
        if not chat_id:
            return
        key = f"apr_{_clip(proposal_id, 120)}"
        try:
            payload, _truncated = redact(summary, max_length=SUMMARY_CHARS)
            self._approval_markers[key] = (owner_id, chat_id)
            context = self._repository()
            context.call(
                context.repository.create_step,
                step_id=key,
                owner_id=owner_id,
                conversation_id=chat_id,
                turn_message_id=None,
                kind=APPROVAL_KIND,
                name=tool_name,
                args_truncated=json.dumps(
                    {"tool_name": tool_name, "proposal_id": str(proposal_id), "summary": payload},
                    sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                args_was_truncated=False,
                started_at=int(time.time() * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            self._approval_markers.pop(key, None)
            logger.debug("approval marker write failed (non-fatal): %s", exc)

    def approval_resolved(self, proposal_id: str, *, note: str = "") -> None:
        key = f"apr_{_clip(proposal_id, 120)}"
        entry = self._approval_markers.pop(key, None)
        if entry is None:
            return
        owner_id = entry[0]
        try:
            context = self._repository()
            context.call(
                context.repository.finish_step,
                owner_id=owner_id,
                step_id=key,
                expected_status=STATUS_IN_PROGRESS,
                status=STATUS_COMPLETED,
                ended_at=int(time.time() * 1000),
                result_summary=_clip(note or "the approval card received a decision", 200),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("approval marker close failed (non-fatal): %s", exc)

    async def _write_intent_row(self, intent: EffectIntent) -> None:
        context = self._repository()
        await asyncio.to_thread(
            context.call,
            context.repository.create_step,
            step_id=intent.operation_id,
            owner_id=intent.owner_id,
            conversation_id=intent.chat_id,
            turn_message_id=None,
            kind=INTENT_KIND,
            name=intent.verb,
            args_truncated=_row_payload(intent),
            args_was_truncated=False,
            started_at=intent.created_at_ms,
        )

    async def _finish_row(self, intent: EffectIntent, *, status: str, summary: str) -> bool:
        try:
            context = self._repository()
            await asyncio.to_thread(
                context.call,
                context.repository.finish_step,
                owner_id=intent.owner_id,
                step_id=intent.operation_id,
                expected_status=STATUS_IN_PROGRESS,
                status=status,
                ended_at=int(time.time() * 1000),
                result_summary=_clip(summary, 200),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("effect row finish failed for %s (%s)", intent.operation_id, exc)
            return False
        return True

    async def _mark_unknown(self, intent: EffectIntent, reason: str) -> EffectIntent:
        marked = replace(intent, state=EffectIntentState.UNKNOWN)
        bucket = self._unknown.setdefault((marked.owner_id, marked.chat_id), {})
        if marked.operation_id not in bucket:
            bucket[marked.operation_id] = marked
            await self._audit(marked, AUDIT_UNKNOWN, f"effect outcome unknown ({_clip(reason, 200)})",
                              outcome="interrupted")
        return marked

    async def _audit(self, intent: EffectIntent, action_type: str, description: str, *,
                     outcome: str) -> None:
        try:
            from datetime import datetime, timezone

            from audit.recorder import get_recorder
            from audit.schemas import AuditEventCreate
            recorder = getattr(self._orch, "audit_recorder", None) or get_recorder()
            if recorder is None:
                return
            await recorder.record(AuditEventCreate(
                actor_user_id=intent.owner_id,
                auth_principal=intent.owner_id,
                event_class="agent_lifecycle",
                action_type=action_type,
                description=_clip(description, 400),
                conversation_id=intent.chat_id,
                correlation_id=intent.operation_id,
                outcome=outcome,
                inputs_meta={
                    "operation_id": intent.operation_id,
                    "host_id": intent.host_id,
                    "host_incarnation": intent.host_incarnation,
                    "verb": intent.verb,
                    "argument_digest": intent.argument_digest,
                    "state": intent.state.value,
                    **({"decision": intent.decision} if intent.decision else {}),
                },
                started_at=datetime.now(timezone.utc),
            ))
        except Exception:  # noqa: BLE001
            logger.debug("effect intent audit failed", exc_info=True)
