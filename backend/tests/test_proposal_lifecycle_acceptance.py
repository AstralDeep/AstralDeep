"""Qualifies knowledge proposals through the real feedback repository and isolated Plane PostgreSQL.
Generation must preserve original artifact bytes and any emitted proposal must apply to that source.
"""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from feedback import proposals
from feedback.repository import FeedbackRepository
from feedback.schemas import ToolQualitySignalDTO
from tests.helpers.plane_template import engine_clone


@pytest.fixture
def proposal_repo(plane_template, tmp_path, monkeypatch):
    with engine_clone(plane_template) as (runtime, _database):
        assert runtime.health().ready
        repo = FeedbackRepository(
            None, plane_runtime=runtime, plane_repositories=runtime.repositories
        )
        repo.insert_quality_signal(
            ToolQualitySignalDTO(
                id=str(uuid4()),
                agent_id="ci-lifecycle-1",
                tool_name="inspect",
                window_start=datetime(2026, 1, 1, tzinfo=UTC),
                window_end=datetime(2026, 1, 8, tzinfo=UTC),
                dispatch_count=10,
                failure_count=4,
                negative_feedback_count=2,
                failure_rate=0.4,
                negative_feedback_rate=0.2,
                status="underperforming",
                computed_at=datetime(2026, 1, 8, tzinfo=UTC),
            )
        )
        monkeypatch.setattr(proposals, "KNOWLEDGE_ROOT", tmp_path)
        yield repo, tmp_path / "techniques/ci_lifecycle__inspect.md"


async def generate(repo, artifact, original):
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(original)
    expected = (
        original.replace(b"\x00", b"[NUL]")
        + "Reviewed guidance: café 👩‍⚕️ e\u0301.\n".encode()
    )

    async def refine(_draft):
        return expected.decode("utf-8")

    generated = await proposals.generate_for_underperforming(
        repo, refine_with_llm=refine
    )
    assert artifact.read_bytes() == original, (
        "generation changed original artifact bytes"
    )
    assert (
        hashlib.sha256(artifact.read_bytes()).hexdigest()
        == hashlib.sha256(original).hexdigest()
    )
    return generated, expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original",
    [b"Existing routing guidance.\n", "Original: café 👩‍⚕️ e\u0301.\n".encode()],
)
async def test_generated_proposal_applies_to_original_artifact(proposal_repo, original):
    repo, artifact = proposal_repo
    generated, expected = await generate(repo, artifact, original)
    assert len(generated) == 1, "ordinary artifact must produce one actionable proposal"
    dto = repo.get_proposal(generated[0].id)
    assert dto.status == "pending"
    assert dto.artifact_sha_at_gen == hashlib.sha256(original).hexdigest()
    applied = await proposals.apply_accepted(
        repo,
        dto.id,
        reviewer_user_id="ci-reviewer",
        auth_principal="ci-reviewer",
    )
    assert applied.status == "applied" and applied.reviewer_user_id == "ci-reviewer"
    assert repo.get_proposal(dto.id).status == "applied"
    assert artifact.read_bytes() == expected
    assert (
        hashlib.sha256(artifact.read_bytes()).hexdigest()
        == hashlib.sha256(expected).hexdigest()
    )


@pytest.mark.asyncio
async def test_nul_proposal_is_rejected_or_applies_to_original_bytes(proposal_repo):
    repo, artifact = proposal_repo
    original = b"Original \x00 boundary.\n" + "Unicode: 👩‍⚕️ e\u0301.\n".encode()
    generated, expected = await generate(repo, artifact, original)
    if not generated:
        assert repo.pending_count() == 0
        assert artifact.read_bytes() == original
        return
    assert len(generated) == 1
    dto = repo.get_proposal(generated[0].id)
    assert dto.artifact_sha_at_gen == hashlib.sha256(original).hexdigest()
    error = None
    try:
        applied = await proposals.apply_accepted(
            repo,
            dto.id,
            reviewer_user_id="ci-reviewer",
            auth_principal="ci-reviewer",
        )
    except Exception as caught:
        error = caught
    assert error is None, "emitted proposal did not apply to original unchanged bytes"
    assert applied.status == "applied" and artifact.read_bytes() == expected


@pytest.mark.asyncio
async def test_proposal_rejects_artifact_tampering_before_review(proposal_repo):
    repo, artifact = proposal_repo
    generated, _expected = await generate(
        repo, artifact, b"Original routing guidance.\n"
    )
    assert len(generated) == 1
    concurrent = b"Concurrent owner edit.\n"
    artifact.write_bytes(concurrent)
    error = None
    try:
        await proposals.apply_accepted(
            repo,
            generated[0].id,
            reviewer_user_id="ci-reviewer",
            auth_principal="ci-reviewer",
        )
    except Exception as caught:
        error = caught
    assert isinstance(error, proposals.StaleProposalError), (
        "artifact digest fence was bypassed"
    )
    assert artifact.read_bytes() == concurrent
    assert repo.get_proposal(generated[0].id).status == "pending"


@pytest.mark.asyncio
async def test_rejected_proposal_cannot_write_artifact(proposal_repo):
    repo, artifact = proposal_repo
    original = b"Existing routing guidance.\n"
    generated, _expected = await generate(repo, artifact, original)
    assert len(generated) == 1
    dto = await proposals.reject_proposal(
        repo,
        generated[0].id,
        reviewer_user_id="ci-reviewer",
        auth_principal="ci-reviewer",
        rationale="Synthetic review denial.",
    )
    assert dto.status == "rejected"
    with pytest.raises(ValueError, match="not pending"):
        await proposals.apply_accepted(
            repo,
            dto.id,
            reviewer_user_id="ci-reviewer",
            auth_principal="ci-reviewer",
        )
    assert (
        artifact.read_bytes() == original
        and repo.get_proposal(dto.id).status == "rejected"
    )
