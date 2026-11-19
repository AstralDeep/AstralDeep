"""Pure, DB-free evolutionary archive of past generated-agent drafts that conditions
codegen prompts on the most relevant past successes.
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger("Orchestrator.DraftArchive")

__all__ = [
    "ArchivedDraft",
    "archive_enabled",
    "top_exemplars",
    "condition_prompt",
    "get_archive",
    "record_archived_draft",
    "reset_archive",
    "exemplar_prompt_for",
    "draft_fingerprint",
    "ARCHIVE_MAX",
]

ARCHIVE_MAX = int(os.getenv("DRAFT_ARCHIVE_MAX", "200"))


def archive_enabled() -> bool:
    return os.getenv("FF_DRAFT_ARCHIVE", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


@dataclass(frozen=True)
class ArchivedDraft:
    fingerprint: str
    code: str
    score: float
    created_at: int = 0
    owner_user_id: str = ""
    draft_uuid: str = ""
    source_state_revision: int = 0
    idempotency_key: str = ""


def _tokens(fingerprint: str) -> set[str]:
    return {t for t in re.split(r"[^0-9a-zA-Z]+", (fingerprint or "").lower()) if t}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _clamp01(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def top_exemplars(
    archive: List[ArchivedDraft],
    fingerprint: str,
    *,
    k: int = 3,
) -> List[ArchivedDraft]:
    if k <= 0:
        return []

    target = _tokens(fingerprint)
    candidates = [d for d in archive if d.score > 0.0]

    ranked = sorted(
        candidates,
        key=lambda d: (_jaccard(target, _tokens(d.fingerprint)), d.score),
        reverse=True,
    )
    return ranked[:k]


_EXEMPLAR_HEADER = "## Exemplars from past successful agents"


def condition_prompt(
    base_prompt: str,
    exemplars: List[ArchivedDraft],
    *,
    max_chars: int = 4000,
) -> str:
    if not exemplars or max_chars <= 0:
        return base_prompt

    parts: List[str] = ["\n\n" + _EXEMPLAR_HEADER + "\n"]
    used = len(parts[0])

    appended_any = False
    for idx, ex in enumerate(exemplars, start=1):
        prefix = f"\n### Exemplar {idx} (score {ex.score:.2f})\n```python\n"
        suffix = "\n```\n"
        scaffold = len(prefix) + len(suffix)

        remaining = max_chars - used - scaffold
        if remaining <= 0:
            break

        body = ex.code or ""
        if len(body) > remaining:
            body = body[:remaining]

        block = prefix + body + suffix
        parts.append(block)
        used += len(block)
        appended_any = True

    if not appended_any:
        return base_prompt

    return base_prompt + "".join(parts)


_ARCHIVE: List[ArchivedDraft] = []
_ARCHIVE_LOCK = threading.Lock()


def draft_fingerprint(draft: dict) -> str:
    try:
        gap = (draft.get("gap_fingerprint") or "").strip()
        name = (draft.get("agent_name") or "").strip()
        slug = (draft.get("agent_slug") or "").strip()
        desc = (draft.get("description") or "").strip()
        basis = " ".join(t for t in (gap, name, slug, desc) if t)
        return basis or (gap or name or slug)
    except Exception:  # pragma: no cover
        return ""


def get_archive(owner_user_id: str) -> List[ArchivedDraft]:
    if not isinstance(owner_user_id, str) or not owner_user_id.strip():
        raise ValueError("owner_user_id is required")
    with _ARCHIVE_LOCK:
        return [
            record
            for record in _ARCHIVE
            if record.owner_user_id == owner_user_id
        ]


def reset_archive() -> None:
    with _ARCHIVE_LOCK:
        _ARCHIVE.clear()


def record_archived_draft(
    fingerprint: str,
    code: str,
    score: float,
    *,
    owner_user_id: str,
    draft_uuid: str,
    source_state_revision: int,
    idempotency_key: str | None = None,
    created_at: Optional[int] = None,
) -> Optional[ArchivedDraft]:
    if not archive_enabled():
        return None
    try:
        if not (code or "").strip() or score <= 0.0:
            return None
        if not isinstance(owner_user_id, str) or not owner_user_id.strip():
            return None
        parsed_draft_uuid = uuid.UUID(str(draft_uuid))
        if parsed_draft_uuid.version != 4:
            return None
        if (
            type(source_state_revision) is not int
            or source_state_revision < 0
        ):
            return None
        stable_key = idempotency_key or (
            f"draft-archive:{parsed_draft_uuid}:{source_state_revision}"
        )
        if (
            not isinstance(stable_key, str)
            or not stable_key.strip()
            or len(stable_key) > 256
        ):
            return None
        rec = ArchivedDraft(
            fingerprint=fingerprint or "",
            code=code,
            score=_clamp01(score),
            created_at=int(created_at if created_at is not None else time.time()),
            owner_user_id=owner_user_id,
            draft_uuid=str(parsed_draft_uuid),
            source_state_revision=source_state_revision,
            idempotency_key=stable_key,
        )
        with _ARCHIVE_LOCK:
            existing = next(
                (
                    record
                    for record in _ARCHIVE
                    if record.owner_user_id == owner_user_id
                    and record.idempotency_key == stable_key
                ),
                None,
            )
            if existing is not None:
                if (
                    existing.draft_uuid == rec.draft_uuid
                    and existing.source_state_revision
                    == rec.source_state_revision
                    and existing.fingerprint == rec.fingerprint
                    and existing.code == rec.code
                    and existing.score == rec.score
                ):
                    return existing
                logger.warning(
                    "draft-archive: refused conflicting idempotency replay"
                )
                return None
            _ARCHIVE.append(rec)
            if len(_ARCHIVE) > ARCHIVE_MAX:
                del _ARCHIVE[: len(_ARCHIVE) - ARCHIVE_MAX]
        logger.info(
            "draft-archive: recorded owner-scoped exemplar "
            "(score=%.2f, size=%d)",
            rec.score,
            len(_ARCHIVE),
        )
        return rec
    except Exception:  # pragma: no cover
        logger.debug("draft-archive: record failed", exc_info=True)
        return None


def exemplar_prompt_for(
    base_prompt: str,
    fingerprint: str,
    *,
    owner_user_id: str,
    k: int = 3,
    max_chars: int = 4000,
) -> str:
    if not archive_enabled():
        return base_prompt
    try:
        exemplars = top_exemplars(
            get_archive(owner_user_id), fingerprint, k=k
        )
        return condition_prompt(base_prompt, exemplars, max_chars=max_chars)
    except Exception:  # pragma: no cover
        logger.debug("draft-archive: exemplar conditioning failed", exc_info=True)
        return base_prompt
