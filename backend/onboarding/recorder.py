"""Wraps audit/recorder.py to emit onboarding's five audit event classes (started,
completed, skipped, replayed, step_edited) without callers building payloads inline.
Used by onboarding/api.py and the admin_tools/tour surfaces.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from audit.recorder import get_recorder, make_correlation_id, now_utc
from audit.schemas import AuditEventCreate

logger = logging.getLogger("Onboarding.Recorder")


async def record_onboarding_started(
    *,
    actor_user_id: str,
    auth_principal: str,
    step_slug: Optional[str],
) -> None:
    rec = get_recorder()
    if rec is None:
        logger.debug("audit recorder not wired; dropping onboarding_started")
        return
    started = now_utc()
    await rec.record(
        AuditEventCreate(
            actor_user_id=actor_user_id,
            auth_principal=auth_principal,
            event_class="onboarding_started",
            action_type="onboarding.start",
            description="User started the getting-started tutorial.",
            correlation_id=make_correlation_id(),
            outcome="success",
            inputs_meta={"step_slug": step_slug or ""},
            started_at=started,
            completed_at=started,
        )
    )


async def record_onboarding_completed(
    *,
    actor_user_id: str,
    auth_principal: str,
    last_step_slug: Optional[str],
) -> None:
    rec = get_recorder()
    if rec is None:
        return
    started = now_utc()
    await rec.record(
        AuditEventCreate(
            actor_user_id=actor_user_id,
            auth_principal=auth_principal,
            event_class="onboarding_completed",
            action_type="onboarding.complete",
            description="User completed the getting-started tutorial.",
            correlation_id=make_correlation_id(),
            outcome="success",
            inputs_meta={"last_step_slug": last_step_slug or ""},
            started_at=started,
            completed_at=started,
        )
    )


async def record_onboarding_skipped(
    *,
    actor_user_id: str,
    auth_principal: str,
    last_step_slug: Optional[str],
) -> None:
    rec = get_recorder()
    if rec is None:
        return
    started = now_utc()
    await rec.record(
        AuditEventCreate(
            actor_user_id=actor_user_id,
            auth_principal=auth_principal,
            event_class="onboarding_skipped",
            action_type="onboarding.skip",
            description="User skipped the getting-started tutorial.",
            correlation_id=make_correlation_id(),
            outcome="success",
            inputs_meta={"last_step_slug": last_step_slug or ""},
            started_at=started,
            completed_at=started,
        )
    )


async def record_onboarding_replayed(
    *,
    actor_user_id: str,
    auth_principal: str,
    prior_status: str,
) -> None:
    rec = get_recorder()
    if rec is None:
        return
    started = now_utc()
    await rec.record(
        AuditEventCreate(
            actor_user_id=actor_user_id,
            auth_principal=auth_principal,
            event_class="onboarding_replayed",
            action_type="onboarding.replay",
            description="User replayed the getting-started tutorial.",
            correlation_id=make_correlation_id(),
            outcome="success",
            inputs_meta={"prior_status": prior_status},
            started_at=started,
            completed_at=started,
        )
    )


async def record_tutorial_step_edited(
    *,
    actor_user_id: str,
    auth_principal: str,
    step_id: int,
    step_slug: str,
    change_kind: str,
    changed_fields: List[str],
) -> None:
    rec = get_recorder()
    if rec is None:
        return
    started = now_utc()
    await rec.record(
        AuditEventCreate(
            actor_user_id=actor_user_id,
            auth_principal=auth_principal,
            event_class="tutorial_step_edited",
            action_type=f"tutorial_step.{change_kind}",
            description=f"Admin {change_kind}d tutorial step {step_slug!r} (id={step_id}).",
            correlation_id=make_correlation_id(),
            outcome="success",
            inputs_meta={
                "step_id": step_id,
                "step_slug": step_slug,
                "change_kind": change_kind,
                "changed_fields": ",".join(changed_fields) if changed_fields else "",
            },
            started_at=started,
            completed_at=started,
        )
    )
