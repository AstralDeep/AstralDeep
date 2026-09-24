"""High-level submit/retract/amend orchestration wrapping feedback/repository.py with
the inline safety screen (feedback/safety.py), dedup/edit-window enforcement, and
audit emission. Cross-user access reads as not-found.
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
    pass


class FeedbackNotFound(Exception):
    pass


@dataclass
class SubmitResult:
    feedback: ComponentFeedbackDTO
    status: str
    deduped: bool


class Recorder:
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
        safety_status, safety_reason = classify(comment)

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
                pass
            else:
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

        prior = self._repo.find_in_dedup_window(
            actor_user_id, correlation_id, component_id,
            window_seconds=self._edit_window,
        )
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
        return self._repo.find_in_dedup_window(
            actor_user_id, correlation_id, component_id,
            window_seconds=self._edit_window,
        )

    async def retract(
        self, actor_user_id: str, auth_principal: str, feedback_id: str,
    ) -> ComponentFeedbackDTO:
        existing = self._repo.get_for_user(actor_user_id, feedback_id)
        if existing is None:
            raise FeedbackNotFound(feedback_id)
        self._guard_edit_window(existing)
        if existing.lifecycle != "active":
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
        meta = dict(inputs_meta or {})
        audit_corr_id = correlation_id
        if audit_corr_id:
            try:
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
        except Exception as exc:  # pragma: no cover
            logger.warning("feedback audit emit failed (%s/%s): %s",
                            event_class, action_type, exc)
