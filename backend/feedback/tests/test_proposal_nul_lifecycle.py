"""Verifies NUL-safe proposal generation and review through real Plane persistence. Raw artifact hashes still fence stale review, and failed acceptance never claims an applied proposal."""

import difflib
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from audit.recorder import Recorder
from audit.repository import AuditRepository
from feedback import proposals
from tests.helpers.plane_template import plane_template as plane_template  # noqa: F401
from tests.test_proposal_lifecycle_acceptance import proposal_repo as proposal_repo  # noqa: F401


@pytest.fixture
async def lifecycle(proposal_repo, monkeypatch, tmp_path):
    repo, artifact = proposal_repo
    monkeypatch.setenv('AUDIT_HMAC_SECRET', 'proposal-nul-lifecycle-synthetic-audit-key')
    monkeypatch.setenv('AUDIT_HMAC_KEY_ID', 'proposal_nul')
    runtime = repo._proposals.plane_runtime
    audit = AuditRepository(plane_runtime=runtime, plane_repositories=runtime.repositories)
    queue = tmp_path / 'audit-retry.jsonl'
    recorder = Recorder(audit, retry_queue=queue)
    monkeypatch.setattr(proposals, 'get_recorder', lambda: recorder)
    try:
        yield SimpleNamespace(repo=repo, artifact=artifact, audit=audit)
    finally:
        await recorder.close()
        assert not queue.exists()


async def _generate(lifecycle, monkeypatch, original, proposed, *, refined=False):
    artifact = lifecycle.artifact
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(original.encode('utf-8'))
    draft = 'Unrefined draft.\n' if refined else proposed
    monkeypatch.setattr(proposals, '_proposed_content', lambda **kwargs: draft)
    refine = AsyncMock(return_value=proposed) if refined else None
    result = await proposals.generate_for_underperforming(lifecycle.repo, refine_with_llm=refine)
    assert artifact.read_bytes() == original.encode('utf-8')
    assert len(result) == 1
    stored = lifecycle.repo.get_proposal(result[0].id)
    assert stored.status == 'pending'
    assert stored.artifact_sha_at_gen == hashlib.sha256(original.encode('utf-8')).hexdigest()
    assert isinstance(stored.diff_payload, str) and '\x00' not in stored.diff_payload
    if refine is not None:
        refine.assert_awaited_once()
    return stored


async def _apply(lifecycle, proposal, *, edited_diff=None):
    return await proposals.apply_accepted(lifecycle.repo, proposal.id,
        reviewer_user_id='synthetic-reviewer', auth_principal='synthetic-reviewer',
        edited_diff=edited_diff)


@pytest.mark.asyncio
@pytest.mark.parametrize('original,proposed,refined', [
    ('alpha\nold\x00line\nomega\n', 'alpha\nreplacement\nomega\n', False),
    ('alpha\nold line\nomega\n', 'alpha\nnew\x00line\nomega\n', False),
    ('alpha\nold line\nomega\n', 'alpha\nnew\x00line\nomega\n', True),
    ('alpha\nold\x00line\nomega\n', 'alpha\nnew\x00line\nomega\n', True),
    ('Plain old.\n', 'Plain revised.\n', False),
    ('Original café 👩‍⚕️ e\u0301.\n', 'Revised café 👩‍⚕️ e\u0301.\n', True),
])
async def test_generated_proposal_applies_without_nul_and_preserves_raw_source_hash(
    lifecycle, monkeypatch, original, proposed, refined,
):
    stored = await _generate(lifecycle, monkeypatch, original, proposed, refined=refined)
    applied = await _apply(lifecycle, stored)
    assert applied.status == lifecycle.repo.get_proposal(stored.id).status == 'applied'
    assert applied.reviewer_user_id == 'synthetic-reviewer'
    assert lifecycle.artifact.read_text() == proposed.replace('\x00', '\ufffd')
    events, _ = lifecycle.audit.list_for_user('synthetic-reviewer')
    assert {event.action_type for event in events} == {'proposal.accept', 'proposal.applied'}
    assert len(events) == 2
    assert lifecycle.audit.verify_chain('synthetic-reviewer') is None
    assert lifecycle.audit.verify_chain('system') is None


@pytest.mark.asyncio
async def test_edited_diff_normalizes_nul_and_applies_to_unchanged_raw_artifact(lifecycle, monkeypatch):
    original = 'alpha\nold\x00line\nomega\n'
    stored = await _generate(lifecycle, monkeypatch, original, 'Generated proposal.\n')
    edited = 'alpha\nreviewed\x00line\nomega\n'
    diff = ''.join(difflib.unified_diff(original.splitlines(keepends=True),
        edited.splitlines(keepends=True), fromfile='a/synthetic.md', tofile='b/synthetic.md'))
    applied = await _apply(lifecycle, stored, edited_diff=diff)
    assert applied.status == 'applied'
    assert lifecycle.artifact.read_text() == edited.replace('\x00', '\ufffd')
    assert lifecycle.repo.get_proposal(stored.id).diff_payload == stored.diff_payload


@pytest.mark.asyncio
async def test_normalized_equal_concurrent_bytes_still_trigger_raw_hash_stale_fence(lifecycle, monkeypatch):
    original = 'alpha\nold\x00line\nomega\n'
    stored = await _generate(lifecycle, monkeypatch, original, 'Revised.\n')
    concurrent = original.replace('\x00', '\ufffd').encode()
    lifecycle.artifact.write_bytes(concurrent)
    with pytest.raises(proposals.StaleProposalError):
        await _apply(lifecycle, stored)
    assert lifecycle.artifact.read_bytes() == concurrent
    assert lifecycle.repo.get_proposal(stored.id).status == 'pending'
    assert lifecycle.audit.list_for_user('synthetic-reviewer') == ([], None)


@pytest.mark.asyncio
async def test_refinement_failure_keeps_nul_safe_draft_applyable(lifecycle, monkeypatch):
    original, draft = 'Original.\n', 'Draft\x00guidance.\n'
    lifecycle.artifact.parent.mkdir(parents=True, exist_ok=True)
    lifecycle.artifact.write_text(original)
    monkeypatch.setattr(proposals, '_proposed_content', lambda **kwargs: draft)
    refine = AsyncMock(side_effect=RuntimeError('synthetic refinement failure'))
    generated = await proposals.generate_for_underperforming(lifecycle.repo, refine_with_llm=refine)
    assert len(generated) == 1 and lifecycle.artifact.read_text() == original
    applied = await _apply(lifecycle, generated[0])
    assert applied.status == 'applied'
    assert lifecycle.artifact.read_text() == draft.replace('\x00', '\ufffd')


@pytest.mark.asyncio
async def test_bad_edited_context_does_not_accept_or_write(lifecycle, monkeypatch):
    original = 'Original\x00guidance.\n'
    stored = await _generate(lifecycle, monkeypatch, original, 'Revised.\n')
    bad = proposals._make_unified_diff('Unrelated source.\n', 'Forged replacement.\n', 'synthetic.md')
    with pytest.raises(ValueError, match='diff context mismatch'):
        await _apply(lifecycle, stored, edited_diff=bad)
    assert lifecycle.artifact.read_text() == original
    assert lifecycle.repo.get_proposal(stored.id).status == 'pending'
    assert lifecycle.audit.list_for_user('synthetic-reviewer') == ([], None)


@pytest.mark.asyncio
async def test_failed_accept_transition_does_not_write(lifecycle, monkeypatch):
    original = 'Original\x00guidance.\n'
    stored = await _generate(lifecycle, monkeypatch, original, 'Revised.\n')
    monkeypatch.setattr(lifecycle.repo, 'transition_proposal', lambda *args, **kwargs: None)
    with pytest.raises(FileNotFoundError):
        await _apply(lifecycle, stored)
    assert lifecycle.artifact.read_text() == original
    assert lifecycle.repo.get_proposal(stored.id).status == 'pending'
    assert lifecycle.audit.list_for_user('synthetic-reviewer') == ([], None)


@pytest.mark.asyncio
async def test_failed_atomic_replace_retains_accepted_state_without_applied_success(lifecycle, monkeypatch):
    original = 'Original\x00guidance.\n'
    stored = await _generate(lifecycle, monkeypatch, original, 'Revised.\n')
    def refuse_replace(*args):
        raise OSError('synthetic atomic replace failure')
    monkeypatch.setattr(proposals.os, 'replace', refuse_replace)
    with pytest.raises(OSError, match='synthetic atomic replace failure'):
        await _apply(lifecycle, stored)
    assert lifecycle.artifact.read_text() == original
    assert lifecycle.repo.get_proposal(stored.id).status == 'accepted'
    assert list(lifecycle.artifact.parent.glob('.kup-*')) == []
    events, _ = lifecycle.audit.list_for_user('synthetic-reviewer')
    assert [event.action_type for event in events] == ['proposal.accept']
    assert lifecycle.audit.verify_chain('synthetic-reviewer') is None


@pytest.mark.asyncio
async def test_generation_insert_failure_does_not_change_source_or_publish_a_proposal(lifecycle, monkeypatch):
    original = 'Original\x00guidance.\n'
    lifecycle.artifact.parent.mkdir(parents=True, exist_ok=True)
    lifecycle.artifact.write_text(original)
    def refuse_insert(**kwargs):
        raise RuntimeError('synthetic proposal persistence failure')
    monkeypatch.setattr(lifecycle.repo, 'insert_proposal', refuse_insert)
    assert await proposals.generate_for_underperforming(lifecycle.repo) == []
    assert lifecycle.artifact.read_text() == original
    assert lifecycle.repo.pending_count() == 0
    assert lifecycle.audit.list_for_user('system') == ([], None)


def test_diff_return_contract_and_normalized_equal_inputs():
    assert proposals._make_unified_diff('same\x00line\n', 'same\ufffdline\n', 'synthetic.md') == ''
    diff = proposals._make_unified_diff('old\x00line\n', 'new\x00line\n', 'synthetic.md')
    assert isinstance(diff, str) and '\x00' not in diff
    assert proposals._apply_unified_diff('old\x00line\n', diff) == 'new\ufffdline\n'
