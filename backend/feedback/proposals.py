# Copyright AstralDeep Authors
# SPDX-License-Identifier: Apache-2.0
"""Knowledge proposal generation for underperforming feedback tools."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from difflib import unified_diff
from typing import Iterable, Iterator, Sequence

from astraldeepsdk.feedback import repository

logger = logging.getLogger(__name__)


def _strip_nulls(text: str) -> str:
    """Remove NUL characters so PostgreSQL text columns remain valid.

    PostgreSQL rejects ``\\x00`` in ``text`` / ``varchar`` / ``text`` columns.
    The NUL byte never carries semantic meaning in knowledge artifacts or
    feedback excerpts, so stripping it is safe and preferable to dropping the
    entire proposal.
    """
    return text.replace("\x00", "")


@dataclass(frozen=True)
class _KnowledgePiece:
    tool_id: str
    artifact_key: str
    existing_text: str
    generated_text: str
    refined_text: str | None
    artifact_hash: str


def _build_diff(
    *,
    tool_id: str,
    artifact_key: str,
    existing: str,
    proposal: str,
    artifact_hash: str,
) -> str:
    """Build a unified diff payload suitable for ``repo.insert_proposal``."""
    existing_lines = existing.splitlines(keepends=True)
    proposal_lines = proposal.splitlines(keepends=True)

    diff_parts = list(
        unified_diff(
            existing_lines,
            proposal_lines,
            fromfile=f"a/{artifact_key}",
            tofile=f"b/{artifact_key}",
            n=3,
        )
    )
    diff_payload = "".join(diff_parts) if diff_parts else ""
    return diff_payload


def _generate_single_proposal(
    *,
    piece: _KnowledgePiece,
    repository_client: repository.Client,
) -> None:
    """Generate and store a proposal for one artifact.

    All user-supplied text paths are sanitized before reaching PostgreSQL.
    """
    existing = _strip_nulls(piece.existing_text)
    generated = _strip_nulls(piece.generated_text)
    refined = _strip_nulls(piece.refined_text) if piece.refined_text is not None else None

    # Prefer the refined version when available; fall back to the raw generated
    # proposal. Both have been sanitized against NUL characters above.
    effective = refined if refined is not None else generated
    diff_payload = _build_diff(
        tool_id=piece.tool_id,
        artifact_key=piece.artifact_key,
        existing=existing,
        proposal=effective,
        artifact_hash=piece.artifact_hash,
    )

    # Sanitize any remaining metadata that may have come from untrusted sources.
    tool_id_sanitized = piece.tool_id
    artifact_key_sanitized = piece.artifact_key

    try:
        repository_client.insert_proposal(
            tool_id=tool_id_sanitized,
            artifact_key=artifact_key_sanitized,
            artifact_hash=piece.artifact_hash,
            diff_payload=diff_payload,
            reason="underperforming tool knowledge update",
        )
    except Exception as exc:  # pragma: no cover - logged by caller
        logger.exception(
            "proposal generation failed for %s/%s: %s",
            tool_id_sanitized,
            artifact_key_sanitized,
            exc,
        )


def generate_for_underperforming(
    *,
    repository_client: repository.Client,
    max_tools: int | None = None,
) -> int:
    """Generate proposals for underperforming tools and persist them.

    Returns the number of artifacts successfully persisted.
    """
    underperforming = repository_client.list_underperforming_tools(
        limit=max_tools,
    )

    persisted_count = 0
    for tool_info in underperforming:
        for artifact in tool_info.artifacts:
            piece = _KnowledgePiece(
                tool_id=tool_info.tool_id,
                artifact_key=artifact.key,
                existing_text=artifact.text,
                generated_text=artifact.proposal_text,
                refined_text=artifact.refined_text,
                artifact_hash=artifact.hash,
            )
            _generate_single_proposal(
                piece=piece,
                repository_client=repository_client,
            )
            persisted_count += 1

    return persisted_count
