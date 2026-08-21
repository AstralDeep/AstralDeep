"""High-level submit / retract / amend orchestration for component feedback.

Wraps :mod:`backend.feedback.repository` with the inline safety screen
(:mod:`backend.feedback.safety`), the dedup window (FR-009a), the 24-hour
edit window (FR-028 / FR-029), and audit-log emission (FR-008 / FR-030).

Public entrypoints:

* :meth:`Recorder.submit` — inline safety screen + dedup-window-aware
  insert/update + audit emit.
* :meth:`Recorder.retract` — 24 h gate + lifecycle update + audit emit.
* :meth:`Recorder.amend` — 24 h gate + supersede + new active row + audit emit.

Cross-user attempts return ``None`` indistinguishably from "not found";
the API layer converts that into a 404.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from audit.recorder import get_recorder, make_correlation_id, now_utc
from audit.schemas import AuditEventCreate

from .repository import FeedbackRepository
from .safety import classify
from .schemas import (
    DEFAULT_DEDUP_WINDOW_SECONDS,
    DEFAULT_EDIT_WINDOW_SECONDS,
    ComponentFeedbackDTO,
)

logger = logging.getLogger("Feedback.Recorder")


class EditWindowExpired(Exception):
    """Raised when retract / amend is attempted after the 24 h window."""


class FeedbackNotFound(Exception):
    """Raised when a feedback id does not exist OR belongs to another user.

    The two cases are deliberately indistinguishable from outside (FR-009).
    """


@dataclass
class SubmitResult:
    feedback: ComponentFeedbackDTO
    status: str          # "recorded" | "quarantined"
    deduped: bool        # True when this submission collapsed into an in-window prior


class Recorder:
    """Public façade used by the REST API and WS handlers."""

    def __init__(
        self,
        repo: FeedbackRepository,
        *,
        dedup_window_seconds: int = DEFAULT_DEDUP_WINDOW_SECONDS,
        edit_window_seconds: int = DEFAULT_EDIT_WINDOW_SECONDS,
    ):
        self._repo = repo
        self._dedup_window = dedup_window_seconds
        self._edit_window = edit_window_seconds

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    async def submit(
        self,
        actor_user_id: str,
        auth_principal: str,
        *,
        conversation_id: Optional[str],
        correlation_id: Optional[str],
        source_agent: Optional[str],
        source_tool: Optional[str],
        component_id: Optional[str],
        sentiment: str,
        category: str,
        comment: Optional[str],
    ) -> SubmitResult:
        """Submit feedback, applying dedup-window collapse and inline safety screen.

        Returns the resulting :class:`SubmitResult`. Always succeeds for
        valid input (the safety screen quarantines, never rejects).
        """
        safety_status, safety_reason = classify(comment)

        # Dedup check — same user, same dispatch, same component, within window.
        existing = self._repo.find_in_dedup_window(
            actor_user_id, correlation_id, component_id,
            window_seconds=self._dedup_window,
        )
        if existing is not None:
            updated = self._repo.update_in_window(
                actor_user_id, existing.id,
                sentiment=sentiment, category=category,
                comment_raw=comment, comment_safety=safety_status,
                comment_safety_reason=safety_reason,
            )
            if updated is None:
                # Lost a race; fall through to insert path
                pass
            else:
                # In-window update — no audit row written (FR-009a).
                # If safety transitioned, refresh quarantine_entry.
                if safety_status == "quarantined":
                    self._repo.upsert_quarantine(
                        updated.id,
                        owner_user_id=actor_user_id,
                        reason=safety_reason or "inline",
                        detector="inline",
                    )
                return SubmitResult(
                    feedback=updated,
                    status="quarantined" if safety_status == "quarantined" else "recorded",
                    deduped=True,
                )

        # Outside dedup window → new active row, supersede prior active row
        # for same target if any.
        prior = self._repo.find_in_dedup_window(
            actor_user_id, correlation_id, component_id,
            window_seconds=self._edit_window,  # any prior active counts here
        )
        # Note: we only supersede if the prior is active (which find_in_dedup_window already
        # filters for) — but we want a longer search horizon than the dedup window.
        # The simpler approach: just look up most-recent active for this target.
        if prior is None:
            prior = self._lookup_prior_active(actor_user_id, correlation_id, component_id)

        new_row = self._repo.insert(
            actor_user_id,
            conversation_id=conversation_id,
            correlation_id=correlation_id,
            source_agent=source_agent,
            source_tool=source_tool,
            component_id=component_id,
            sentiment=sentiment,
            category=category,
            comment_raw=comment,
            comment_safety=safety_status,
            comment_safety_reason=safety_reason,
            supersedes_id=prior.id if prior else None,
        )

        if safety_status == "quarantined":
            self._repo.upsert_quarantine(
                new_row.id,
                owner_user_id=actor_user_id,
                reason=safety_reason or "inline",
                detector="inline",
            )

        # Emit audit row.
        await self._emit_audit(
            actor_user_id=actor_user_id,
            auth_principal=auth_principal,
            event_class="component_feedback",
            action_type="feedback.submit",
            description=f"User submitted feedback ({sentiment}/{category}) for {source_tool or 'static'}",
            agent_id=source_agent,
            conversation_id=conversation_id,
            correlation_id=correlation_id,
            outcome="success" if safety_status == "clean" else "interrupted",
            outcome_detail=None if safety_status == "clean" else f"quarantined:{safety_reason}",
            inputs_meta={
                "feedback_id": str(new_row.id),
                "sentiment": sentiment,
                "category": category,
                "has_comment": comment is not None and comment != "",
                "comment_safety": safety_status,
                "source_tool": source_tool,
            },
        )

        return SubmitResult(
            feedback=new_row,
            status="quarantined" if safety_status == "quarantined" else "recorded",
            deduped=False,
        )

    def _lookup_prior_active(
        self, actor_user_id: str, correlation_id: Optional[str], component_id: Optional[str]
    ) -> Optional[ComponentFeedbackDTO]:
        # Reuse find_in_dedup_window with a very long horizon — within the
        # 24h edit window everything that's still active is supersedable.
        return self._repo.find_in_dedup_window(
            actor_user_id, correlation_id, component_id,
            window_seconds=self._edit_window,
        )

    # ------------------------------------------------------------------
    # Retract
    # ------------------------------------------------------------------

    async def retract(
        self, actor_user_id: str, auth_principal: str, feedback_id: str,
    ) -> ComponentFeedbackDTO:
        existing = self._repo.get_for_user(actor_user_id, feedback_id)
        if existing is None:
            raise FeedbackNotFound(feedback_id)
        self._guard_edit_window(existing)
        if existing.lifecycle != "active":
            # Already retracted or superseded — treat as a no-op for the client
            # (no audit emission; lifecycle stays as-is).
            return existing

        updated = self._repo.retract(actor_user_id, feedback_id)
        if updated is None:
            raise FeedbackNotFound(feedback_id)

        await self._emit_audit(
            actor_user_id=actor_user_id,
            auth_principal=auth_principal,
            event_class="component_feedback",
            action_type="feedback.retract",
            description=f"User retracted feedback {feedback_id}",
            agent_id=existing.source_agent,
            conversation_id=existing.conversation_id,
            correlation_id=existing.correlation_id,
            outcome="success",
            inputs_meta={"feedback_id": feedback_id},
        )
        return updated

    # ------------------------------------------------------------------
    # Amend
    # ------------------------------------------------------------------

    async def amend(
        self,
        actor_user_id: str,
        auth_principal: str,
        feedback_id: str,
        *,
        sentiment: Optional[str],
        category: Optional[str],
        comment: Optional[str],
        comment_explicit: bool,
    ) -> ComponentFeedbackDTO:
        """Amend the user's own feedback by superseding the prior row.

        ``comment_explicit`` distinguishes ``comment=None`` (clear comment)
        from "comment field omitted from request" (inherit from prior).
        """
        existing = self._repo.get_for_user(actor_user_id, feedback_id)
        if existing is None:
            raise FeedbackNotFound(feedback_id)
        self._guard_edit_window(existing)
        if existing.lifecycle != "active":
            raise FeedbackNotFound(feedback_id)

        new_sentiment = sentiment if sentiment is not None else existing.sentiment
        new_category = category if category is not None else existing.category
        new_comment = comment if comment_explicit else existing.comment_raw

        safety_status, safety_reason = classify(new_comment)

        new_row = self._repo.insert(
            actor_user_id,
            conversation_id=existing.conversation_id,
            correlation_id=existing.correlation_id,
            source_agent=existing.source_agent,
            source_tool=existing.source_tool,
            component_id=existing.component_id,
            sentiment=new_sentiment,
            category=new_category,
            comment_raw=new_comment,
            comment_safety=safety_status,
            comment_safety_reason=safety_reason,
            supersedes_id=existing.id,
        )

        if safety_status == "quarantined":
            self._repo.upsert_quarantine(
                new_row.id,
                owner_user_id=actor_user_id,
                reason=safety_reason or "inline",
                detector="inline",
            )

        await self._emit_audit(
            actor_user_id=actor_user_id,
            auth_principal=auth_principal,
            event_class="component_feedback",
            action_type="feedback.amend",
            description=f"User amended feedback (now {new_row.id}; was {existing.id})",
            agent_id=existing.source_agent,
            conversation_id=existing.conversation_id,
            correlation_id=existing.correlation_id,
            outcome="success",
            inputs_meta={
                "prior_feedback_id": str(existing.id),
                "feedback_id": str(new_row.id),
                "sentiment": new_sentiment,
                "category": new_category,
                "comment_safety": safety_status,
            },
        )
        return new_row

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _guard_edit_window(self, existing: ComponentFeedbackDTO) -> None:
        delta = (datetime.now(timezone.utc) - existing.created_at).total_seconds()
        if delta > self._edit_window:
            raise EditWindowExpired(str(existing.id))

    async def _emit_audit(
        self,
        *,
        actor_user_id: str,
        auth_principal: str,
        event_class: str,
        action_type: str,
        description: str,
        agent_id: Optional[str],
        conversation_id: Optional[str],
        correlation_id: Optional[str],
        outcome: str,
        outcome_detail: Optional[str] = None,
        inputs_meta: Optional[dict] = None,
    ) -> None:
        rec = get_recorder()
        if rec is None:
            return
        # The audit log's correlation_id column is UUID-typed. The
        # frontend-supplied value (when present) is the audit UUID of the
        # originating dispatch, which IS a UUID. But for non-tool-dispatch
        # components — and for any defensive case where the value isn't a
        # well-formed UUID — synthesize a fresh audit UUID and stash the
        # caller-supplied value in inputs_meta so we still have it.
        meta = dict(inputs_meta or {})
        audit_corr_id = correlation_id
        if audit_corr_id:
            try:
                # Validate; UUID() raises ValueError on bad input.
                from uuid import UUID as _UUID
                _UUID(audit_corr_id)
            except (TypeError, ValueError):
                meta.setdefault("source_correlation_id", audit_corr_id)
                audit_corr_id = None
        if not audit_corr_id:
            audit_corr_id = make_correlation_id()
        try:
            await rec.record(AuditEventCreate(
                actor_user_id=actor_user_id,
                auth_principal=auth_principal,
                agent_id=agent_id,
                event_class=event_class,
                action_type=action_type,
                description=description,
                conversation_id=conversation_id,
                correlation_id=audit_corr_id,
                outcome=outcome,
                outcome_detail=outcome_detail,
                inputs_meta=meta,
                started_at=now_utc(),
            ))
        except Exception as exc:  # pragma: no cover — never block on audit
            logger.warning("feedback audit emit failed (%s/%s): %s",
                            event_class, action_type, exc)
