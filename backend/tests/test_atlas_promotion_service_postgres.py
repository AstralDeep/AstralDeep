"""Tests for owner-approved Atlas source promotion over a real PostgreSQL-backed
AstralPlane runtime: bounded proposals, exact source/destination snapshots, append-only
reconciliation, cross-owner and stale-source denial, replay protection, and rollback.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from queue import Queue
from threading import Event
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from astralplane.repositories.guidance_models import ExplicitNoteRecord  # noqa: E402
from astralplane.repositories.preferences import MemoryRecord  # noqa: E402
from audit.repository import AuditRepository  # noqa: E402
from orchestrator.credential_manager import CredentialManager  # noqa: E402
from personalization import phi_gate  # noqa: E402
from personalization.atlas_pages import AtlasPageError  # noqa: E402
from personalization.atlas_promotions import PromotionSourceInput  # noqa: E402
from personalization.atlas_service import (  # noqa: E402
    AtlasConflict,
    AtlasNotFound,
    AtlasPageService,
    AtlasUnavailable,
    PageInput,
)
from personalization.explicit_notes import ExplicitNoteMetadata  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402

OWNER_A = "atlas-promotion-owner-a"
OWNER_B = "atlas-promotion-owner-b"
PROJECT = "atlas-promotion"


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("atlas_promotion") as runtime:
        yield runtime


@pytest.fixture(scope="module")
def audit_repo(plane_runtime):
    return AuditRepository(
        plane_runtime=plane_runtime, plane_repositories=plane_runtime.repositories
    )


@pytest.fixture(scope="module")
def credential_manager(plane_runtime, tmp_path_factory):
    return CredentialManager(
        plane_runtime=plane_runtime, data_dir=str(tmp_path_factory.mktemp("atlas-promotion-keys"))
    )


@pytest.fixture(autouse=True)
def deterministic_phi_gate(monkeypatch):
    monkeypatch.setattr(
        phi_gate,
        "_GATE",
        phi_gate.PHIGate(analyzer=SimpleNamespace(analyze=lambda **_: [])),
    )


@pytest.fixture
def service(plane_runtime, audit_repo, credential_manager):
    return AtlasPageService(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
        audit_repo=audit_repo,
        fernet=credential_manager._fernet,
    )


def _slug(prefix="promotion"):
    return f"{prefix}-{uuid4().hex[:10]}"


def _seed_note(service, owner=OWNER_A, *, value="Keep answers concise.", category="preference",
               enabled=True, expires_after=None):
    note_id = str(uuid4())
    with service.runtime.transaction() as transaction:
        prepared = service.notes.prepare_explicit_note(
            transaction, owner_id=owner, note_id=note_id, expected_revision=0
        )
        now = prepared.observed_at_ms
        metadata = ExplicitNoteMetadata(
            owner_id=owner,
            note_id=note_id,
            revision=1,
            format_version=1,
            category=category,
            enabled=enabled,
            created_at=now,
            updated_at=now,
            expires_at=None if expires_after is None else now + expires_after,
        )
        encrypted = service.note_cipher.seal(metadata, value)
        record = ExplicitNoteRecord(**asdict(encrypted.metadata), ciphertext=encrypted.ciphertext)
        return service.notes.put_explicit_note(
            transaction, preparation=prepared, record=record
        )


def _revise_note(service, owner, record, value):
    with service.runtime.transaction() as transaction:
        prepared = service.notes.prepare_explicit_note(
            transaction, owner_id=owner, note_id=record.note_id,
            expected_revision=record.revision,
        )
        now = prepared.observed_at_ms
        metadata = ExplicitNoteMetadata(
            owner_id=owner,
            note_id=record.note_id,
            revision=record.revision + 1,
            format_version=1,
            category=record.category,
            enabled=record.enabled,
            created_at=record.created_at,
            updated_at=now,
            expires_at=record.expires_at,
        )
        encrypted = service.note_cipher.seal(metadata, value)
        return service.notes.put_explicit_note(
            transaction,
            preparation=prepared,
            record=ExplicitNoteRecord(**asdict(encrypted.metadata), ciphertext=encrypted.ciphertext),
        )


def _forget_note(service, owner, record):
    with service.runtime.transaction() as transaction:
        service.notes.prepare_explicit_note_retirement(
            transaction, owner_id=owner, note_id=record.note_id,
            expected_revision=record.revision, reason="forgotten",
        )
        service.notes.forget_explicit_note(
            transaction, owner_id=owner, note_id=record.note_id,
            expected_revision=record.revision,
        )


def _seed_memory(service, owner=OWNER_A, *, value="Ship the Atlas promotion workflow.",
                 category="goal", project_id=PROJECT):
    now = int(time.time() * 1000)
    record = MemoryRecord(
        memory_id=str(uuid4()),
        owner_id=owner,
        category=category,
        value=value,
        source="explicit",
        salience=0.0,
        created_at=now,
        updated_at=now,
        superseded_by=None,
        superseded_at=None,
        keywords=None,
        signature=None,
        valid_from=None,
        valid_to=None,
        ingested_at=None,
        recall_count=0,
        last_recalled_at=None,
        project_id=project_id,
    )
    with service.runtime.transaction() as transaction:
        return service.notes.create_memory(transaction, record)


def _proposal(service, *, source, slug, owner=OWNER_A, title="Owner instructions",
              mode="auto", expected_revision=None):
    return service.propose_promotion(
        owner_id=owner,
        project_id=PROJECT,
        sources=(source,),
        slug=slug,
        title=title,
        mode=mode,
        expected_revision=expected_revision,
    )


def _approve(service, proposal, owner=OWNER_A):
    return service.approve_promotion(
        owner_id=owner,
        project_id=PROJECT,
        proposal_id=proposal.proposal_id,
        approval_token=proposal.approval_token,
        proposal_digest=proposal.proposal_digest,
    )


def test_proposal_is_read_only_and_approval_creates_provenanced_page_once(service):
    note = _seed_note(service)
    slug = _slug("promote-note")
    proposal = _proposal(
        service,
        source=PromotionSourceInput("note", note.note_id, note.revision),
        slug=slug,
    )
    assert proposal.action == "create"
    assert proposal.sources[0].source_id == note.note_id
    assert proposal.sources[0].revision == note.revision
    assert proposal.body.count("Keep answers concise.") == 1
    with pytest.raises(AtlasNotFound):
        service.get_page(owner_id=OWNER_A, project_id=PROJECT, reference=slug)

    applied = _approve(service, proposal)
    assert applied.proposal_id == proposal.proposal_id
    assert applied.page.revision == 1
    assert applied.page.owner_id == OWNER_A
    assert applied.page.project_id == PROJECT
    assert "Keep answers concise." in applied.page.body
    assert f"note:{note.note_id}@{note.revision}" in applied.page.sources
    stored = service.get_page(owner_id=OWNER_A, project_id=PROJECT, reference=slug)
    assert stored.page_id == applied.page.page_id
    assert stored.revision == 1

    with pytest.raises(AtlasConflict) as replay:
        _approve(service, proposal)
    assert replay.value.code == "promotion_already_applied"
    assert service.get_page(
        owner_id=OWNER_A, project_id=PROJECT, reference=slug
    ).revision == 1
    assert service.runtime.fetch_one(
        "SELECT count(*) AS total FROM audit_events "
        "WHERE actor_user_id = %s AND action_type = 'atlas_promotion_apply' "
        "AND event_id = %s",
        (OWNER_A, proposal.proposal_id),
    )["total"] == 1


def test_corrected_source_invalidates_the_proposal_without_mutation(service):
    note = _seed_note(service, value="Prefer short answers.")
    slug = _slug("stale-note")
    proposal = _proposal(
        service,
        source=PromotionSourceInput("note", note.note_id, note.revision),
        slug=slug,
    )
    corrected = _revise_note(service, OWNER_A, note, "Prefer detailed answers.")
    with pytest.raises(AtlasConflict) as stale:
        _approve(service, proposal)
    assert stale.value.code == "promotion_source_stale"
    with pytest.raises(AtlasNotFound):
        service.get_page(owner_id=OWNER_A, project_id=PROJECT, reference=slug)

    corrected_proposal = _proposal(
        service,
        source=PromotionSourceInput("note", corrected.note_id, corrected.revision),
        slug=slug,
    )
    applied = _approve(service, corrected_proposal)
    assert "Prefer detailed answers." in applied.page.body
    assert "Prefer short answers." not in applied.page.body


def test_deleted_disabled_and_expired_sources_are_refused(service, monkeypatch):
    deleted = _seed_note(service, value="Temporary note.")
    slug = _slug("deleted-note")
    proposal = _proposal(
        service,
        source=PromotionSourceInput("note", deleted.note_id, deleted.revision),
        slug=slug,
    )
    _forget_note(service, OWNER_A, deleted)
    with pytest.raises(AtlasNotFound) as missing:
        _approve(service, proposal)
    assert missing.value.code == "promotion_source_not_found"
    with pytest.raises(AtlasNotFound):
        service.get_page(owner_id=OWNER_A, project_id=PROJECT, reference=slug)

    disabled = _seed_note(service, value="Hidden guidance.", enabled=False)
    with pytest.raises(AtlasConflict) as refused:
        _proposal(
            service,
            source=PromotionSourceInput("note", disabled.note_id, disabled.revision),
            slug=_slug("disabled"),
        )
    assert refused.value.code == "promotion_source_disabled"

    expiring = _seed_note(service, value="Expiring guidance.", expires_after=60_000)
    from personalization import atlas_service as service_module

    monkeypatch.setattr(service_module, "_now_ms", lambda: expiring.expires_at + 1)
    with pytest.raises(AtlasConflict) as expired:
        _proposal(
            service,
            source=PromotionSourceInput("note", expiring.note_id, expiring.revision),
            slug=_slug("expired"),
        )
    assert expired.value.code == "promotion_source_expired"


def test_cross_owner_and_cross_project_sources_are_denied(service):
    foreign_note = _seed_note(service, owner=OWNER_B, value="Foreign secret.")
    with pytest.raises(AtlasNotFound):
        _proposal(
            service,
            owner=OWNER_A,
            source=PromotionSourceInput("note", foreign_note.note_id, foreign_note.revision),
            slug=_slug("foreign-note"),
        )
    own_note = _seed_note(service, value="My guidance.")
    proposal = _proposal(
        service,
        source=PromotionSourceInput("note", own_note.note_id, own_note.revision),
        slug=_slug("owner-bound"),
    )
    with pytest.raises(AtlasPageError):
        _approve(service, proposal, owner=OWNER_B)
    with pytest.raises(AtlasNotFound):
        service.get_page(owner_id=OWNER_B, project_id=PROJECT, reference=proposal.destination.slug)

    foreign_memory = _seed_memory(
        service, owner=OWNER_B, value="Foreign memory.", project_id=PROJECT
    )
    with pytest.raises(AtlasNotFound):
        _proposal(
            service,
            owner=OWNER_A,
            source=PromotionSourceInput("memory", foreign_memory.memory_id, foreign_memory.updated_at),
            slug=_slug("foreign-memory"),
        )
    scoped_memory = _seed_memory(
        service, owner=OWNER_A, value="Other project memory.", project_id="another-project"
    )
    with pytest.raises(AtlasPageError) as mismatch:
        _proposal(
            service,
            source=PromotionSourceInput("memory", scoped_memory.memory_id, scoped_memory.updated_at),
            slug=_slug("scope-mismatch"),
        )
    assert mismatch.value.code == "promotion_source_scope_mismatch"


def test_append_preserves_owner_text_and_replacement_is_not_offered(service):
    slug = _slug("owner-page")
    owner_page = service.create_page(
        owner_id=OWNER_A,
        page=PageInput(
            slug=slug,
            title="Owner instructions",
            body="Owner-authored instruction that must not be replaced.",
            sources=("https://example.invalid/owner",),
            project_id=PROJECT,
        ),
    )
    note = _seed_note(service)
    with pytest.raises(AtlasConflict) as create_conflict:
        _proposal(
            service,
            source=PromotionSourceInput("note", note.note_id, note.revision),
            slug=slug,
            mode="create",
        )
    assert create_conflict.value.code == "page_already_exists"
    with pytest.raises(AtlasPageError) as replacement:
        _proposal(
            service,
            source=PromotionSourceInput("note", note.note_id, note.revision),
            slug=slug,
            mode="replace",
        )
    assert replacement.value.code == "promotion_mode_invalid"

    proposal = _proposal(
        service,
        source=PromotionSourceInput("note", note.note_id, note.revision),
        slug=slug,
        title=None,
    )
    assert proposal.action == "append"
    assert proposal.destination.revision == owner_page.revision
    applied = _approve(service, proposal)
    assert applied.page.revision == 2
    assert applied.page.title == "Owner instructions"
    assert applied.page.body.startswith(
        "Owner-authored instruction that must not be replaced.\n\n"
    )
    assert "Keep answers concise." in applied.page.body
    assert "https://example.invalid/owner" in applied.page.sources
    assert f"note:{note.note_id}@{note.revision}" in applied.page.sources


def test_destination_change_after_proposal_blocks_approval(service):
    note = _seed_note(service)
    slug = _slug("destination-race")
    page = service.create_page(
        owner_id=OWNER_A,
        page=PageInput(
            slug=slug, title="Owner instructions", body="Original owner text.",
            project_id=PROJECT,
        ),
    )
    proposal = _proposal(
        service,
        source=PromotionSourceInput("note", note.note_id, note.revision),
        slug=slug,
        title=None,
    )
    updated = service.update_page(
        owner_id=OWNER_A,
        project_id=PROJECT,
        reference=slug,
        title=None,
        body="Owner changed this before approval.",
        sources=None,
        links=None,
        expected_revision=page.revision,
    )
    with pytest.raises(AtlasConflict) as stale:
        _approve(service, proposal)
    assert stale.value.code == "promotion_destination_stale"
    unchanged = service.get_page(owner_id=OWNER_A, project_id=PROJECT, reference=slug)
    assert unchanged.revision == updated.revision
    assert unchanged.body == "Owner changed this before approval."


def test_memory_source_is_revision_bound_and_approval_rolls_back_on_failure(
    service, audit_repo, monkeypatch
):
    memory = _seed_memory(service)
    slug = _slug("memory")
    proposal = _proposal(
        service,
        source=PromotionSourceInput("memory", memory.memory_id, memory.updated_at),
        slug=slug,
    )
    original_insert = audit_repo.insert_in_transaction
    failed = {"once": True}

    def failing_promotion_audit(event, **kwargs):
        if failed["once"] and event.action_type == "atlas_promotion_apply":
            failed["once"] = False
            raise RuntimeError("synthetic audit failure")
        return original_insert(event, **kwargs)

    monkeypatch.setattr(audit_repo, "insert_in_transaction", failing_promotion_audit)
    with pytest.raises(AtlasUnavailable) as failure:
        _approve(service, proposal)
    assert failure.value.code == "atlas_audit_unavailable"
    with pytest.raises(AtlasNotFound):
        service.get_page(owner_id=OWNER_A, project_id=PROJECT, reference=slug)
    assert service.runtime.fetch_one(
        "SELECT count(*) AS total FROM audit_events "
        "WHERE actor_user_id = %s AND action_type = 'atlas_promotion_apply' "
        "AND event_id = %s",
        (OWNER_A, proposal.proposal_id),
    )["total"] == 0

    applied = _approve(service, proposal)
    assert "Ship the Atlas promotion workflow." in applied.page.body
    assert f"memory:{memory.memory_id}@{memory.updated_at}" in applied.page.sources

    second = _seed_memory(service, value="A revised goal.")
    stale_proposal = _proposal(
        service,
        source=PromotionSourceInput("memory", second.memory_id, second.updated_at),
        slug=_slug("memory-stale"),
    )
    with service.runtime.transaction() as transaction:
        service.notes.update_memory_value(
            transaction,
            owner_id=OWNER_A,
            memory_id=second.memory_id,
            value="A corrected goal.",
            signature=None,
            updated_at=second.updated_at + 1,
            expected_updated_at=second.updated_at,
        )
    with pytest.raises(AtlasConflict) as stale:
        _approve(service, stale_proposal)
    assert stale.value.code == "promotion_source_stale"


def test_create_destination_appearing_after_proposal_conflicts(service):
    note = _seed_note(service)
    slug = _slug("create-race")
    proposal = _proposal(
        service,
        source=PromotionSourceInput("note", note.note_id, note.revision),
        slug=slug,
    )
    service.create_page(
        owner_id=OWNER_A,
        page=PageInput(
            slug=slug, title="Created elsewhere", body="Owner created this first.",
            project_id=PROJECT,
        ),
    )
    with pytest.raises(AtlasConflict) as conflict:
        _approve(service, proposal)
    assert conflict.value.code == "page_already_exists"
    assert service.get_page(
        owner_id=OWNER_A, project_id=PROJECT, reference=slug
    ).body == "Owner created this first."


def test_approval_token_digest_and_project_are_exact(service):
    note = _seed_note(service)
    proposal = _proposal(
        service,
        source=PromotionSourceInput("note", note.note_id, note.revision),
        slug=_slug("digest"),
    )
    with pytest.raises(AtlasPageError):
        service.approve_promotion(
            owner_id=OWNER_A,
            project_id=PROJECT,
            proposal_id=proposal.proposal_id,
            approval_token=proposal.approval_token,
            proposal_digest="0" * 64,
        )
    with pytest.raises(AtlasConflict) as not_approved:
        service.approve_promotion(
            owner_id=OWNER_A,
            project_id=PROJECT,
            proposal_id=proposal.proposal_id,
            approval_token=proposal.approval_token,
            approved=False,
        )
    assert not_approved.value.code == "promotion_not_approved"
    with pytest.raises(AtlasPageError):
        service.approve_promotion(
            owner_id=OWNER_A,
            project_id="another-project",
            proposal_id=proposal.proposal_id,
            approval_token=proposal.approval_token,
        )
    with pytest.raises(AtlasPageError):
        AtlasPageService(
            plane_runtime=service.runtime,
            plane_repositories=service.runtime.repositories,
            audit_repo=service.audit,
            fernet=Fernet(Fernet.generate_key()),
        ).approve_promotion(
            owner_id=OWNER_A,
            project_id=PROJECT,
            proposal_id=proposal.proposal_id,
            approval_token=proposal.approval_token,
        )


def test_multiple_sources_are_bounded_and_duplicates_are_refused(service):
    first = _seed_note(service, value="First source preference.")
    second = _seed_memory(service, value="Second source goal.", project_id=PROJECT)
    proposal = service.propose_promotion(
        owner_id=OWNER_A,
        project_id=PROJECT,
        sources=(
            PromotionSourceInput("note", first.note_id, first.revision),
            {"kind": "memory", "id": second.memory_id, "revision": second.updated_at},
        ),
        slug=_slug("multi-source"),
    )
    assert proposal.action == "create"
    assert len(proposal.sources) == 2
    assert "First source preference." in proposal.body
    assert "Second source goal." in proposal.body
    applied = _approve(service, proposal)
    assert len(applied.sources) == 2
    assert "First source preference." in applied.page.body
    assert "Second source goal." in applied.page.body

    with pytest.raises(AtlasPageError) as duplicate:
        service.propose_promotion(
            owner_id=OWNER_A,
            project_id=PROJECT,
            sources=(
                PromotionSourceInput("note", first.note_id),
                PromotionSourceInput("note", first.note_id),
            ),
            slug=_slug("duplicate"),
        )
    assert duplicate.value.code == "promotion_source_duplicate"
    with pytest.raises(AtlasPageError) as too_many:
        service.propose_promotion(
            owner_id=OWNER_A,
            project_id=PROJECT,
            sources=tuple(PromotionSourceInput("note", str(uuid4())) for _ in range(5)),
            slug=_slug("too-many"),
        )
    assert too_many.value.code == "promotion_sources_out_of_bounds"


def test_expired_or_missing_destination_approvals_do_not_mutate(service, monkeypatch):
    note = _seed_note(service)
    with pytest.raises(AtlasNotFound) as missing:
        _proposal(
            service,
            source=PromotionSourceInput("note", note.note_id, note.revision),
            slug=_slug("missing"),
            mode="append",
        )
    assert missing.value.code == "promotion_destination_not_found"

    proposal = _proposal(
        service,
        source=PromotionSourceInput("note", note.note_id, note.revision),
        slug=_slug("expired"),
    )
    from personalization import atlas_service as service_module

    monkeypatch.setattr(service_module, "_now_ms", lambda: proposal.expires_at)
    with pytest.raises(AtlasPageError) as expired:
        _approve(service, proposal)
    assert expired.value.code == "promotion_expired"
    with pytest.raises(AtlasNotFound):
        service.get_page(
            owner_id=OWNER_A,
            project_id=PROJECT,
            reference=proposal.destination.slug,
        )


_SOURCE_MUTATIONS = ("forget", "correct", "memory_update", "memory_delete", "memory_supersede",
                     "memory_expire")


def _mutate_promotion_source(service, record, mutation, started, before_commit=None):
    with service.runtime.transaction() as transaction:
        started.put(transaction.fetch_one("SELECT pg_backend_pid() AS pid")["pid"])
        if mutation == "forget":
            service.notes.forget_explicit_note(
                transaction, owner_id=record.owner_id, note_id=record.note_id,
                expected_revision=record.revision,
            )
        elif mutation == "correct":
            prepared = service.notes.prepare_explicit_note(
                transaction, owner_id=record.owner_id, note_id=record.note_id,
                expected_revision=record.revision,
            )
            metadata = ExplicitNoteMetadata(
                **{key: value for key, value in asdict(record).items() if key != "ciphertext"}
            )
            encrypted = service.note_cipher.seal(
                replace(metadata, revision=record.revision + 1,
                        updated_at=prepared.observed_at_ms),
                "Corrected source content.",
            )
            service.notes.put_explicit_note(
                transaction, preparation=prepared,
                record=ExplicitNoteRecord(**asdict(encrypted.metadata),
                                          ciphertext=encrypted.ciphertext),
            )
        elif mutation == "memory_update":
            service.notes.update_memory_value(
                transaction, owner_id=record.owner_id, memory_id=record.memory_id,
                value="Corrected source content.", signature=None,
                updated_at=record.updated_at + 1, expected_updated_at=record.updated_at,
            )
        elif mutation == "memory_delete":
            service.notes.delete_memory(
                transaction, owner_id=record.owner_id, memory_id=record.memory_id,
                expected_updated_at=record.updated_at,
            )
        elif mutation == "memory_supersede":
            service.notes.supersede_memory(
                transaction, owner_id=record.owner_id, memory_id=record.memory_id,
                superseded_at=record.updated_at + 1,
            )
        else:
            service.notes.set_validity(
                transaction, owner_id=record.owner_id, memory_id=record.memory_id,
                valid_from=None, valid_to=record.created_at, ingested_at=None,
                updated_at=record.updated_at + 1, expected_updated_at=record.updated_at,
            )

        if before_commit is not None:
            prepared, release = before_commit
            prepared.set()
            assert release.wait(10)


def _wait_for_database_blocker(service, pending, pid, blocker):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = service.runtime.fetch_one(
            "SELECT pg_blocking_pids(%s) AS blockers", (pid,)
        )
        if blocker in row["blockers"]:
            return
        if pending.done():
            pending.result()
            pytest.fail("source mutation committed before promotion released its transaction")
        time.sleep(0.01)
    pytest.fail("source mutation did not reach its PostgreSQL lock")


@pytest.mark.parametrize("mutation", _SOURCE_MUTATIONS)
def test_promotion_keeps_sources_locked_through_destination_and_audit_commit(
    service, monkeypatch, mutation,
):
    note = mutation in {"forget", "correct"}
    record = _seed_note(service) if note else _seed_memory(service)
    source = PromotionSourceInput(
        "note" if note else "memory", record.note_id if note else record.memory_id,
        record.revision if note else record.updated_at,
    )
    proposal = _proposal(service, source=source, slug=_slug("source-lock"))
    loaded, release_load, audited, release_audit = Event(), Event(), Event(), Event()
    approval_pid, mutation_pid = Queue(), Queue()
    original_load = service._load_promotion_sources
    original_audit = service._audit_promotion_in_transaction

    def paused_load(transaction, **kwargs):
        result = original_load(transaction, **kwargs)
        approval_pid.put(transaction.fetch_one("SELECT pg_backend_pid() AS pid")["pid"])
        loaded.set()
        assert release_load.wait(10)
        return result

    def paused_audit(transaction, **kwargs):
        result = original_audit(transaction, **kwargs)
        if kwargs["action"] == "apply":
            audited.set()
            assert release_audit.wait(10)
        return result

    monkeypatch.setattr(service, "_load_promotion_sources", paused_load)
    monkeypatch.setattr(service, "_audit_promotion_in_transaction", paused_audit)
    with ThreadPoolExecutor(max_workers=2) as workers:
        approval = workers.submit(_approve, service, proposal)
        try:
            assert loaded.wait(10)
            blocker = approval_pid.get(timeout=10)
            mutation_future = workers.submit(
                _mutate_promotion_source, service, record, mutation, mutation_pid,
            )
            pid = mutation_pid.get(timeout=10)
            assert pid != blocker
            _wait_for_database_blocker(service, mutation_future, pid, blocker)
            release_load.set()
            assert audited.wait(10)
            _wait_for_database_blocker(service, mutation_future, pid, blocker)
        finally:
            release_load.set()
            release_audit.set()
        applied = approval.result(timeout=10)
        mutation_future.result(timeout=10)
    assert applied.page.revision == 1
    assert service.runtime.fetch_one(
        "SELECT count(*) AS total FROM audit_events WHERE event_id = %s",
        (proposal.proposal_id,),
    )["total"] == 1


@pytest.mark.parametrize("mutation", _SOURCE_MUTATIONS)
def test_source_mutation_committed_before_approval_refuses_destination(service, mutation):
    note = mutation in {"forget", "correct"}
    record = _seed_note(service) if note else _seed_memory(service)
    proposal = _proposal(
        service, slug=_slug("mutation-first"),
        source=PromotionSourceInput(
            "note" if note else "memory", record.note_id if note else record.memory_id,
            record.revision if note else record.updated_at,
        ),
    )
    _mutate_promotion_source(service, record, mutation, Queue())
    with pytest.raises((AtlasConflict, AtlasNotFound)):
        _approve(service, proposal)
    with pytest.raises(AtlasNotFound):
        service.get_page(owner_id=OWNER_A, project_id=PROJECT,
                         reference=proposal.destination.slug)
    assert service.runtime.fetch_one(
        "SELECT count(*) AS total FROM audit_events WHERE event_id = %s",
        (proposal.proposal_id,),
    )["total"] == 0


@pytest.mark.parametrize("mutation", _SOURCE_MUTATIONS)
def test_approval_waits_for_source_writer_then_refuses_its_stale_snapshot(
    service, monkeypatch, mutation,
):
    note = mutation in {"forget", "correct"}
    record = _seed_note(service) if note else _seed_memory(service)
    proposal = _proposal(
        service, slug=_slug("writer-first"),
        source=PromotionSourceInput(
            "note" if note else "memory", record.note_id if note else record.memory_id,
            record.revision if note else record.updated_at,
        ),
    )
    prepared, release = Event(), Event()
    mutation_pid, approval_pid = Queue(), Queue()
    original_lock = service.notes.lock_explicit_note_owner

    def observed_lock(transaction, **kwargs):
        approval_pid.put(transaction.fetch_one("SELECT pg_backend_pid() AS pid")["pid"])
        return original_lock(transaction, **kwargs)

    monkeypatch.setattr(service.notes, "lock_explicit_note_owner", observed_lock)
    with ThreadPoolExecutor(max_workers=2) as workers:
        mutation_future = workers.submit(
            _mutate_promotion_source, service, record, mutation, mutation_pid, (prepared, release)
        )
        try:
            assert prepared.wait(10)
            blocker = mutation_pid.get(timeout=10)
            approval = workers.submit(_approve, service, proposal)
            pid = approval_pid.get(timeout=10)
            assert pid != blocker
            _wait_for_database_blocker(service, approval, pid, blocker)
        finally:
            release.set()
        mutation_future.result(timeout=10)
        with pytest.raises((AtlasConflict, AtlasNotFound)):
            approval.result(timeout=10)
    with pytest.raises(AtlasNotFound):
        service.get_page(owner_id=OWNER_A, project_id=PROJECT,
                         reference=proposal.destination.slug)
    assert service.runtime.fetch_one(
        "SELECT count(*) AS total FROM audit_events WHERE event_id = %s",
        (proposal.proposal_id,),
    )["total"] == 0


def test_approval_locks_owner_before_sorted_memory_sources_and_preserves_proposal_order(
    service, monkeypatch,
):
    note = _seed_note(service)
    memories = sorted((_seed_memory(service), _seed_memory(service)),
                      key=lambda record: record.memory_id, reverse=True)
    inputs = (
        PromotionSourceInput("note", note.note_id, note.revision),
        *(PromotionSourceInput("memory", record.memory_id, record.updated_at)
          for record in memories),
    )
    proposal = service.propose_promotion(
        owner_id=OWNER_A, project_id=PROJECT, sources=inputs, slug=_slug("ordered-locks"),
    )
    observed = []
    original_owner, original_memory = service.notes.lock_explicit_note_owner, service.notes.get_memory

    def owner_lock(transaction, **kwargs):
        observed.append(("owner", kwargs["owner_id"]))
        return original_owner(transaction, **kwargs)

    def memory_read(transaction, **kwargs):
        if kwargs.get("for_update"):
            observed.append(("memory", kwargs["memory_id"]))
        return original_memory(transaction, **kwargs)

    monkeypatch.setattr(service.notes, "lock_explicit_note_owner", owner_lock)
    monkeypatch.setattr(service.notes, "get_memory", memory_read)
    applied = _approve(service, proposal)
    assert observed == [("owner", OWNER_A),
                        *(("memory", record.memory_id) for record in reversed(memories))]
    assert [(source.kind, source.source_id) for source in proposal.sources] == [
        (source.kind, source.source_id) for source in inputs
    ]
    assert applied.sources == proposal.source_references


@pytest.mark.parametrize("kind", ("note", "memory"))
def test_audit_rollback_releases_source_locks_and_leaves_no_promotion(service, monkeypatch, kind):
    record = _seed_note(service) if kind == "note" else _seed_memory(service)
    source = PromotionSourceInput(
        kind, record.note_id if kind == "note" else record.memory_id,
        record.revision if kind == "note" else record.updated_at,
    )
    proposal = _proposal(service, source=source, slug=_slug("rollback-lock"))
    audited, release = Event(), Event()
    approval_pid, mutation_pid = Queue(), Queue()
    original_audit = service._audit_promotion_in_transaction

    def failed_audit(transaction, **kwargs):
        if kwargs["action"] == "apply":
            approval_pid.put(transaction.fetch_one("SELECT pg_backend_pid() AS pid")["pid"])
            audited.set()
            assert release.wait(10)
            raise AtlasUnavailable("atlas_audit_unavailable")
        return original_audit(transaction, **kwargs)

    monkeypatch.setattr(service, "_audit_promotion_in_transaction", failed_audit)
    with ThreadPoolExecutor(max_workers=2) as workers:
        approval = workers.submit(_approve, service, proposal)
        try:
            assert audited.wait(10)
            blocker = approval_pid.get(timeout=10)
            mutation = workers.submit(
                _mutate_promotion_source, service, record,
                "correct" if kind == "note" else "memory_update", mutation_pid,
            )
            pid = mutation_pid.get(timeout=10)
            _wait_for_database_blocker(service, mutation, pid, blocker)
        finally:
            release.set()
        with pytest.raises(AtlasUnavailable, match="atlas_audit_unavailable"):
            approval.result(timeout=10)
        mutation.result(timeout=10)
    with pytest.raises(AtlasNotFound):
        service.get_page(owner_id=OWNER_A, project_id=PROJECT,
                         reference=proposal.destination.slug)
    assert service.runtime.fetch_one(
        "SELECT count(*) AS total FROM audit_events WHERE event_id = %s",
        (proposal.proposal_id,),
    )["total"] == 0
    with pytest.raises(AtlasConflict, match="promotion_source_stale"):
        _approve(service, proposal)


def test_source_owner_lock_does_not_block_another_owner_and_serializes_replay(service, monkeypatch):
    source = _seed_note(service)
    foreign = _seed_note(service, owner=OWNER_B)
    proposal = _proposal(
        service, slug=_slug("owner-lock"),
        source=PromotionSourceInput("note", source.note_id, source.revision),
    )
    loaded, release = Event(), Event()
    approval_pid, replay_pid = Queue(), Queue()
    original_load = service._load_promotion_sources
    original_lock = service.notes.lock_explicit_note_owner

    def paused_load(transaction, **kwargs):
        result = original_load(transaction, **kwargs)
        approval_pid.put(transaction.fetch_one("SELECT pg_backend_pid() AS pid")["pid"])
        loaded.set()
        assert release.wait(10)
        return result

    def observed_lock(transaction, **kwargs):
        replay_pid.put(transaction.fetch_one("SELECT pg_backend_pid() AS pid")["pid"])
        return original_lock(transaction, **kwargs)

    monkeypatch.setattr(service, "_load_promotion_sources", paused_load)
    with ThreadPoolExecutor(max_workers=2) as workers:
        approval = workers.submit(_approve, service, proposal)
        try:
            assert loaded.wait(10)
            blocker = approval_pid.get(timeout=10)
            workers.submit(_forget_note, service, OWNER_B, foreign).result(timeout=10)
            monkeypatch.setattr(service.notes, "lock_explicit_note_owner", observed_lock)
            replay = workers.submit(_approve, service, proposal)
            pid = replay_pid.get(timeout=10)
            _wait_for_database_blocker(service, replay, pid, blocker)
        finally:
            release.set()
        approval.result(timeout=10)
        with pytest.raises(AtlasConflict, match="promotion_already_applied"):
            replay.result(timeout=10)


@pytest.mark.parametrize("expired", ("note", "memory", "proposal"))
def test_approval_rechecks_expiry_after_waiting_for_source_lock(service, monkeypatch, expired):
    from personalization import atlas_service as service_module

    memory = _seed_memory(service)
    note = _seed_note(service, expires_after=60_000) if expired == "note" else None
    if expired == "memory":
        with service.runtime.transaction() as transaction:
            memory = service.notes.set_validity(
                transaction, owner_id=OWNER_A, memory_id=memory.memory_id,
                valid_from=None, valid_to=memory.created_at + 60_000, ingested_at=None,
                updated_at=memory.updated_at + 1, expected_updated_at=memory.updated_at,
            )
    inputs = [PromotionSourceInput("memory", memory.memory_id, memory.updated_at)]
    if note is not None:
        inputs.append(PromotionSourceInput("note", note.note_id, note.revision))
    proposal = service.propose_promotion(
        owner_id=OWNER_A, project_id=PROJECT, sources=inputs, slug=_slug("lock-expiry"),
    )
    clock = [service_module._now_ms()]
    monkeypatch.setattr(service_module, "_now_ms", lambda: clock[0])
    entered = Queue()
    original_read = service.notes.get_memory

    def observed_read(transaction, **kwargs):
        if kwargs.get("for_update"):
            entered.put(transaction.fetch_one("SELECT pg_backend_pid() AS pid")["pid"])
        return original_read(transaction, **kwargs)

    monkeypatch.setattr(service.notes, "get_memory", observed_read)
    with ThreadPoolExecutor(max_workers=1) as workers:
        with service.runtime.transaction() as holder:
            blocker = holder.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            original_read(holder, owner_id=OWNER_A, memory_id=memory.memory_id, for_update=True)
            approval = workers.submit(_approve, service, proposal)
            pid = entered.get(timeout=10)
            _wait_for_database_blocker(service, approval, pid, blocker)
            clock[0] = (note.expires_at if expired == "note" else
                        memory.valid_to if expired == "memory" else proposal.expires_at)
        with pytest.raises(AtlasPageError if expired == "proposal" else AtlasConflict):
            approval.result(timeout=10)
    with pytest.raises(AtlasNotFound):
        service.get_page(owner_id=OWNER_A, project_id=PROJECT,
                         reference=proposal.destination.slug)
    assert service.runtime.fetch_one(
        "SELECT count(*) AS total FROM audit_events WHERE event_id = %s",
        (proposal.proposal_id,),
    )["total"] == 0


@pytest.mark.parametrize("failure", ("owner_lock", "memory_lock"))
def test_source_lock_failure_rolls_back_and_allows_unchanged_proposal_retry(
    service, monkeypatch, failure,
):
    memory = _seed_memory(service)
    proposal = _proposal(
        service, slug=_slug("failed-lock"),
        source=PromotionSourceInput("memory", memory.memory_id, memory.updated_at),
    )
    original_read = service.notes.get_memory

    def failed_owner(*args, **kwargs):
        raise RuntimeError("synthetic owner lock failure")

    def failed_memory(transaction, **kwargs):
        result = original_read(transaction, **kwargs)
        if kwargs.get("for_update"):
            raise RuntimeError("synthetic memory lock failure")
        return result

    with monkeypatch.context() as patch:
        if failure == "owner_lock":
            patch.setattr(service.notes, "lock_explicit_note_owner", failed_owner)
        else:
            patch.setattr(service.notes, "get_memory", failed_memory)
        with pytest.raises(AtlasUnavailable, match="atlas_storage_failed"):
            _approve(service, proposal)
    with pytest.raises(AtlasNotFound):
        service.get_page(owner_id=OWNER_A, project_id=PROJECT,
                         reference=proposal.destination.slug)
    assert service.runtime.fetch_one(
        "SELECT count(*) AS total FROM audit_events WHERE event_id = %s",
        (proposal.proposal_id,),
    )["total"] == 0
    assert _approve(service, proposal).page.revision == 1


def _promotion_effects(service):
    return (
        service.runtime.fetch_all(
            "SELECT * FROM memory_item WHERE user_id = %s ORDER BY id", (OWNER_A,)
        ),
        service.runtime.fetch_all(
            "SELECT event_id FROM audit_events WHERE actor_user_id = %s "
            "AND action_type IN ('atlas_page_create', 'atlas_page_update', "
            "'atlas_promotion_apply') ORDER BY event_id", (OWNER_A,),
        ),
    )


def test_earlier_memory_expiry_is_rechecked_after_later_source_lock(service, monkeypatch):
    from personalization import atlas_service as service_module

    first, second = sorted((_seed_memory(service), _seed_memory(service)),
                           key=lambda record: record.memory_id)
    with service.runtime.transaction() as transaction:
        first = service.notes.set_validity(
            transaction, owner_id=OWNER_A, memory_id=first.memory_id,
            valid_from=None, valid_to=first.created_at + 60_000, ingested_at=None,
            updated_at=first.updated_at + 1, expected_updated_at=first.updated_at,
        )
    proposal = service.propose_promotion(
        owner_id=OWNER_A, project_id=PROJECT, slug=_slug("later-source-expiry"),
        sources=[PromotionSourceInput("memory", record.memory_id, record.updated_at)
                 for record in (second, first)],
    )
    assert first.memory_id < second.memory_id
    assert first.valid_to < proposal.expires_at
    before = _promotion_effects(service)
    clock = [service_module._now_ms()]
    monkeypatch.setattr(service_module, "_now_ms", lambda: clock[0])
    entered = Queue()
    original_read = service.notes.get_memory

    def observed_read(transaction, **kwargs):
        if kwargs.get("for_update") and kwargs["memory_id"] == second.memory_id:
            entered.put(transaction.fetch_one("SELECT pg_backend_pid() AS pid")["pid"])
        return original_read(transaction, **kwargs)

    monkeypatch.setattr(service.notes, "get_memory", observed_read)
    with ThreadPoolExecutor(max_workers=1) as workers:
        with service.runtime.transaction() as holder:
            blocker = holder.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            original_read(holder, owner_id=OWNER_A, memory_id=second.memory_id, for_update=True)
            approval = workers.submit(_approve, service, proposal)
            pid = entered.get(timeout=10)
            assert pid != blocker
            _wait_for_database_blocker(service, approval, pid, blocker)
            clock[0] = first.valid_to
        with pytest.raises(AtlasConflict, match="promotion_source_stale"):
            approval.result(timeout=10)
    assert _promotion_effects(service) == before
    with pytest.raises(AtlasNotFound):
        service.get_page(owner_id=OWNER_A, project_id=PROJECT,
                         reference=proposal.destination.slug)


@pytest.mark.parametrize("mode", ("create", "append"))
@pytest.mark.parametrize("expired", ("note", "memory", "proposal"))
def test_final_acceptance_refuses_expiry_during_audit_lock_wait(service, monkeypatch, mode, expired):
    from personalization import atlas_service as service_module

    note = _seed_note(service, expires_after=60_000) if expired == "note" else None
    memory = None if note else _seed_memory(service)
    if expired == "memory":
        with service.runtime.transaction() as transaction:
            memory = service.notes.set_validity(
                transaction, owner_id=OWNER_A, memory_id=memory.memory_id,
                valid_from=None, valid_to=memory.created_at + 60_000, ingested_at=None,
                updated_at=memory.updated_at + 1, expected_updated_at=memory.updated_at,
            )
    slug = _slug("audit-expiry")
    if mode == "append":
        service.create_page(
            owner_id=OWNER_A,
            page=PageInput(slug=slug, title="Owner instructions", body="Original owner page.",
                           project_id=PROJECT),
        )
    proposal = _proposal(
        service, slug=slug, mode=mode,
        source=PromotionSourceInput(
            "note" if note else "memory", note.note_id if note else memory.memory_id,
            note.revision if note else memory.updated_at,
        ),
    )
    deadline = (note.expires_at if note else
                memory.valid_to if expired == "memory" else proposal.expires_at)
    if expired != "proposal":
        assert deadline < proposal.expires_at
    before = _promotion_effects(service)
    clock = [service_module._now_ms()]
    monkeypatch.setattr(service_module, "_now_ms", lambda: clock[0])
    loaded = Queue()
    original_load = service._load_promotion_sources

    def observed_load(transaction, **kwargs):
        result = original_load(transaction, **kwargs)
        loaded.put(transaction.fetch_one("SELECT pg_backend_pid() AS pid")["pid"])
        return result

    monkeypatch.setattr(service, "_load_promotion_sources", observed_load)
    with ThreadPoolExecutor(max_workers=1) as workers:
        with service.runtime.transaction() as holder:
            blocker = holder.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            service._audit_in_transaction(
                holder, owner_id=OWNER_A, action="test_barrier",
                description="Hold audit chain for expiry verification", detail="synthetic",
            )
            approval = workers.submit(_approve, service, proposal)
            pid = loaded.get(timeout=10)
            assert pid != blocker
            _wait_for_database_blocker(service, approval, pid, blocker)
            clock[0] = deadline
        with pytest.raises(AtlasPageError if expired == "proposal" else AtlasConflict):
            approval.result(timeout=10)
    assert _promotion_effects(service) == before
    if mode == "create":
        with pytest.raises(AtlasNotFound):
            service.get_page(owner_id=OWNER_A, project_id=PROJECT, reference=slug)
    else:
        page = service.get_page(owner_id=OWNER_A, project_id=PROJECT, reference=slug)
        assert page.revision == 1 and page.body == "Original owner page."
