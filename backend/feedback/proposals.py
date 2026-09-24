"""Bridges feedback signals to orchestrator/knowledge_synthesis.py: turns
underperforming-tool evidence into unified-diff proposals an admin reviews; accept
re-validates the artifact's sha and writes it via write-then-rename.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from audit.recorder import get_recorder, make_correlation_id, now_utc
from audit.schemas import AuditEventCreate

from .repository import FeedbackRepository
from .schemas import KnowledgeUpdateProposalDTO

logger = logging.getLogger("Feedback.Proposals")


KNOWLEDGE_ROOT = Path(
    os.getenv(
        "FEEDBACK_KNOWLEDGE_ROOT",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "knowledge"),
    )
).resolve()


class StaleProposalError(Exception):
    pass


class InvalidArtifactPath(Exception):
    pass


def _ensure_within_knowledge_root(artifact_path: str) -> Path:
    target = (KNOWLEDGE_ROOT / artifact_path).resolve()
    try:
        target.relative_to(KNOWLEDGE_ROOT)
    except ValueError:
        raise InvalidArtifactPath(artifact_path)
    return target


def _sha256_of_path(p: Path) -> str:
    h = hashlib.sha256()
    if p.exists():
        h.update(p.read_bytes())
    return h.hexdigest()


def _make_unified_diff(old: str, new: str, artifact_path: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{artifact_path}",
        tofile=f"b/{artifact_path}",
    )
    return "".join(diff)


# Must parse exactly what _make_unified_diff produces
def _apply_unified_diff(old: str, diff_payload: str) -> str:
    old_lines = old.splitlines(keepends=True)
    out: List[str] = []
    src_idx = 0

    lines = diff_payload.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("---") or line.startswith("+++"):
            i += 1
            continue
        if line.startswith("@@"):
            try:
                _, old_range, new_range, _rest = (line.split(" ", 3) + [""])[:4]
                src_start_raw = int(old_range.split(",")[0].lstrip("-"))
                src_start = max(0, src_start_raw - 1)
            except (IndexError, ValueError):
                raise ValueError(f"malformed hunk header: {line!r}")
            if src_start < src_idx:
                raise ValueError("non-monotonic hunk header")
            out.extend(old_lines[src_idx:src_start])
            src_idx = src_start
            i += 1
            continue
        if line.startswith("+"):
            out.append(line[1:])
            i += 1
            continue
        if line.startswith("-"):
            if src_idx >= len(old_lines) or old_lines[src_idx] != line[1:]:
                raise ValueError("diff context mismatch on '-' line")
            src_idx += 1
            i += 1
            continue
        if line.startswith(" "):
            if src_idx >= len(old_lines) or old_lines[src_idx] != line[1:]:
                raise ValueError("diff context mismatch on ' ' line")
            out.append(old_lines[src_idx])
            src_idx += 1
            i += 1
            continue
        i += 1

    out.extend(old_lines[src_idx:])
    return "".join(out)


def _artifact_path_for_tool(agent_id: str, tool_name: str) -> str:
    slug = agent_id.replace("-", "_").rstrip("_1234567890") or "default"
    return f"techniques/{slug}__{tool_name}.md"


def _proposed_content(
    *,
    existing_content: str,
    agent_id: str,
    tool_name: str,
    aggregates: Dict[str, Any],
    sample_comments: List[Dict[str, Any]],
    now: datetime,
) -> str:
    header = f"# Routing notes for `{tool_name}` (agent: {agent_id})\n\n"
    header += f"_Last updated: {now.isoformat(timespec='seconds')}_\n\n"

    metrics = (
        "## Recent quality signal\n\n"
        f"- Window: last {aggregates.get('window_days', 14)} days\n"
        f"- Dispatches: {aggregates.get('dispatch_count', 0)}\n"
        f"- Failure rate: {aggregates.get('failure_rate', 0.0):.1%}\n"
        f"- Negative-feedback rate: {aggregates.get('negative_feedback_rate', 0.0):.1%}\n\n"
    )

    cb = aggregates.get("category_breakdown", {}) or {}
    if cb:
        cats = "\n".join(f"- `{k}`: {v}" for k, v in sorted(cb.items(), key=lambda kv: -kv[1]))
        metrics += f"## Top failure categories\n\n{cats}\n\n"

    notes = (
        "## Routing guidance (auto-generated draft)\n\n"
        "This tool has been flagged underperforming. Until quality recovers, "
        "prefer alternative tools when possible. When this tool must be used, "
        "validate its output more carefully than usual.\n\n"
    )

    if sample_comments:
        samples = ["## Recent user-feedback excerpts (untrusted; for context only)\n"]
        for s in sample_comments:
            cat = s.get("category", "unspecified")
            text = (s.get("comment") or "").replace("\n", " ").strip()
            if len(text) > 280:
                text = text[:280] + "…"
            samples.append(f"- *(category: {cat})* {text}")
        notes += "\n".join(samples) + "\n"

    return header + metrics + notes


async def generate_for_underperforming(
    repo: FeedbackRepository,
    *,
    refine_with_llm: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
) -> List[KnowledgeUpdateProposalDTO]:
    underperforming, _ = repo.list_underperforming(limit=100)
    proposals: List[KnowledgeUpdateProposalDTO] = []

    for snap in underperforming:
        try:
            artifact_rel = _artifact_path_for_tool(snap.agent_id, snap.tool_name)
            artifact_abs = _ensure_within_knowledge_root(artifact_rel)
            artifact_abs.parent.mkdir(parents=True, exist_ok=True)
            existing_content = artifact_abs.read_text(encoding="utf-8") if artifact_abs.exists() else ""
            sha_at_gen = _sha256_of_path(artifact_abs)

            cb = repo.category_breakdown(snap.agent_id, snap.tool_name,
                                         snap.window_start, snap.window_end)
            samples = repo.collect_clean_comment_samples(
                snap.agent_id, snap.tool_name, snap.window_start, snap.window_end,
            )

            aggregates = {
                "window_days": (snap.window_end - snap.window_start).days,
                "dispatch_count": snap.dispatch_count,
                "failure_count": snap.failure_count,
                "negative_feedback_count": snap.negative_feedback_count,
                "failure_rate": snap.failure_rate,
                "negative_feedback_rate": snap.negative_feedback_rate,
                "category_breakdown": cb,
            }

            proposed = _proposed_content(
                existing_content=existing_content,
                agent_id=snap.agent_id, tool_name=snap.tool_name,
                aggregates=aggregates, sample_comments=samples,
                now=datetime.now(timezone.utc),
            )

            if refine_with_llm is not None:
                try:
                    refined = await refine_with_llm(proposed)
                    if refined and isinstance(refined, str) and refined.strip():
                        proposed = refined
                except Exception as exc:
                    logger.warning("synth LLM refinement failed for %s/%s: %s",
                                    snap.agent_id, snap.tool_name, exc)

            diff = _make_unified_diff(existing_content, proposed, artifact_rel)
            if not diff.strip():
                continue

            audit_ids, fb_ids = repo.evidence_ids(
                snap.agent_id, snap.tool_name, snap.window_start, snap.window_end,
            )
            evidence = {
                "audit_event_ids": audit_ids,
                "component_feedback_ids": fb_ids,
                "window_start": snap.window_start.isoformat(),
                "window_end": snap.window_end.isoformat(),
            }
            dto = repo.insert_proposal(
                agent_id=snap.agent_id, tool_name=snap.tool_name,
                artifact_path=artifact_rel, diff_payload=diff,
                artifact_sha_at_gen=sha_at_gen, evidence=evidence,
            )
            proposals.append(dto)

            await _emit_proposal_audit(
                action_type="proposal.generated",
                description=f"Proposal generated for {snap.agent_id}/{snap.tool_name}",
                proposal=dto,
                actor_user_id="system",
                auth_principal="system:feedback.proposals",
            )
        except Exception as exc:  # pragma: no cover
            logger.exception("proposal generation failed for %s/%s: %s",
                              snap.agent_id, snap.tool_name, exc)

    return proposals


async def apply_accepted(
    repo: FeedbackRepository,
    proposal_id: str,
    *,
    reviewer_user_id: str,
    auth_principal: str,
    edited_diff: Optional[str] = None,
) -> KnowledgeUpdateProposalDTO:
    existing = repo.get_proposal(proposal_id)
    if existing is None:
        raise FileNotFoundError(proposal_id)
    if existing.status != "pending":
        raise ValueError(f"proposal {proposal_id} is {existing.status}, not pending")

    artifact_abs = _ensure_within_knowledge_root(existing.artifact_path)
    current_sha = _sha256_of_path(artifact_abs)
    if current_sha != existing.artifact_sha_at_gen:
        raise StaleProposalError(proposal_id)

    diff = edited_diff if (edited_diff and edited_diff.strip()) else existing.diff_payload
    old_content = artifact_abs.read_text(encoding="utf-8") if artifact_abs.exists() else ""
    try:
        new_content = _apply_unified_diff(old_content, diff)
    except ValueError as exc:
        raise ValueError(f"diff apply failed: {exc}")

    accepted = repo.transition_proposal(
        proposal_id, new_status="accepted", reviewer_user_id=reviewer_user_id,
    )
    if accepted is None:
        raise FileNotFoundError(proposal_id)

    await _emit_proposal_audit(
        action_type="proposal.accept",
        description=f"Admin {reviewer_user_id} accepted proposal {proposal_id}",
        proposal=accepted,
        actor_user_id=reviewer_user_id,
        auth_principal=auth_principal,
    )

    artifact_abs.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".kup-", suffix=".md", dir=str(artifact_abs.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_content)
        os.replace(tmp_path, artifact_abs)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    applied = repo.transition_proposal(
        proposal_id, new_status="applied",
        reviewer_user_id=reviewer_user_id, applied=True,
    )
    if applied is None:
        raise FileNotFoundError(proposal_id)

    await _emit_proposal_audit(
        action_type="proposal.applied",
        description=f"Proposal {proposal_id} applied to {existing.artifact_path}",
        proposal=applied,
        actor_user_id=reviewer_user_id,
        auth_principal=auth_principal,
    )
    return applied


async def reject_proposal(
    repo: FeedbackRepository,
    proposal_id: str,
    *,
    reviewer_user_id: str,
    auth_principal: str,
    rationale: str,
) -> KnowledgeUpdateProposalDTO:
    if not rationale or not rationale.strip():
        raise ValueError("rationale is required for reject")
    existing = repo.get_proposal(proposal_id)
    if existing is None:
        raise FileNotFoundError(proposal_id)
    if existing.status != "pending":
        raise ValueError(f"proposal {proposal_id} is {existing.status}, not pending")

    rejected = repo.transition_proposal(
        proposal_id, new_status="rejected",
        reviewer_user_id=reviewer_user_id, reviewer_rationale=rationale,
    )
    if rejected is None:
        raise FileNotFoundError(proposal_id)

    await _emit_proposal_audit(
        action_type="proposal.reject",
        description=f"Admin {reviewer_user_id} rejected proposal {proposal_id}",
        proposal=rejected,
        actor_user_id=reviewer_user_id,
        auth_principal=auth_principal,
        outcome_detail=rationale[:512],
    )
    return rejected


async def _emit_proposal_audit(
    *,
    action_type: str,
    description: str,
    proposal: KnowledgeUpdateProposalDTO,
    actor_user_id: str,
    auth_principal: str,
    outcome_detail: Optional[str] = None,
) -> None:
    rec = get_recorder()
    if rec is None:
        return
    try:
        await rec.record(AuditEventCreate(
            actor_user_id=actor_user_id,
            auth_principal=auth_principal,
            agent_id=proposal.agent_id,
            event_class="proposal_review",
            action_type=action_type,
            description=description,
            correlation_id=make_correlation_id(),
            outcome="success",
            outcome_detail=outcome_detail,
            inputs_meta={
                "proposal_id": str(proposal.id),
                "tool_name": proposal.tool_name,
                "artifact_path": proposal.artifact_path,
                "status": proposal.status,
            },
            started_at=now_utc(),
        ))
    except Exception as exc:  # pragma: no cover
        logger.warning("proposal_review audit emit failed (%s): %s", action_type, exc)


async def emit_quarantine_audit(
    *,
    action_type: str,
    feedback_id: str,
    reason: Optional[str],
    detector: Optional[str],
    actor_user_id: str,
    auth_principal: str,
) -> None:
    rec = get_recorder()
    if rec is None:
        return
    try:
        await rec.record(AuditEventCreate(
            actor_user_id=actor_user_id,
            auth_principal=auth_principal,
            event_class="quarantine",
            action_type=action_type,
            description=f"Quarantine {action_type.split('.')[-1]} on feedback {feedback_id}",
            correlation_id=make_correlation_id(),
            outcome="success",
            inputs_meta={
                "feedback_id": str(feedback_id),
                "reason": reason or "",
                "detector": detector or "",
            },
            started_at=now_utc(),
        ))
    except Exception as exc:  # pragma: no cover
        logger.warning("quarantine audit emit failed (%s): %s", action_type, exc)
