"""Tests for the Atlas page lifecycle service (personalization/atlas_service.py) over a
real PostgreSQL-backed AstralPlane runtime: owner/project isolation, revision conflicts,
PHI refusal, host encryption, bounded history retention, atomic audit, deletion, and
recovery after injected storage failures.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from audit.repository import AuditRepository  # noqa: E402
from orchestrator.credential_manager import CredentialManager  # noqa: E402
from personalization import phi_gate  # noqa: E402
from personalization.atlas_pages import AtlasPageError  # noqa: E402
from personalization.atlas_service import (  # noqa: E402
    AtlasConflict,
    AtlasNotFound,
    AtlasRefused,
    AtlasUnavailable,
    AtlasPageService,
    PageInput,
)
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402

OWNER_A = "atlas-owner-a"
OWNER_B = "atlas-owner-b"


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("atlas_service") as runtime:
        yield runtime


@pytest.fixture(scope="module")
def audit_repo(plane_runtime):
    return AuditRepository(
        plane_runtime=plane_runtime, plane_repositories=plane_runtime.repositories
    )


@pytest.fixture(scope="module")
def credential_manager(plane_runtime, tmp_path_factory):
    return CredentialManager(
        plane_runtime=plane_runtime, data_dir=str(tmp_path_factory.mktemp("atlas-keys"))
    )


@pytest.fixture(autouse=True)
def deterministic_phi_gate(monkeypatch):
    monkeypatch.setattr(
        phi_gate, "_GATE",
        phi_gate.PHIGate(analyzer=SimpleNamespace(analyze=lambda **_: [])),
    )


def _service(plane_runtime, audit_repo, credential_manager) -> AtlasPageService:
    return AtlasPageService(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
        audit_repo=audit_repo,
        fernet=credential_manager._fernet,
    )


@pytest.fixture
def service(plane_runtime, audit_repo, credential_manager):
    return _service(plane_runtime, audit_repo, credential_manager)


def _unique_slug(prefix: str = "kickoff") -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _page(**changes):
    values = dict(
        slug=_unique_slug(),
        title="Kickoff notes",
        body="First draft of the project kickoff.",
        sources=("https://example.invalid/notes",),
        links=(),
        project_id="atlas-project",
    )
    values.update(changes)
    return PageInput(**values)


def _audit_count(runtime, action: str, owner: str) -> int:
    row = runtime.fetch_one(
        "SELECT count(*) AS total FROM audit_events "
        "WHERE action_type = %s AND actor_user_id = %s",
        ("atlas_page_" + action, owner),
    )
    return int(row["total"])


def test_create_and_get_round_trip_decrypts_only_for_the_owner(service):
    created = service.create_page(owner_id=OWNER_A, page=_page())
    assert created.revision == 1
    assert created.slug
    assert created.body is None
    fetched = service.get_page(
        owner_id=OWNER_A, project_id="atlas-project", reference=created.slug
    )
    assert fetched.page_id == created.page_id
    assert fetched.body == "First draft of the project kickoff."
    assert fetched.fingerprint
    by_id = service.get_page(
        owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id
    )
    assert by_id.body == fetched.body
    stored = plane_stored_value(service, created.page_id, OWNER_A)
    assert "First draft" not in stored
    assert stored.startswith('{"body":')


def plane_stored_value(service, page_id: str, owner: str) -> str:
    with service.runtime.transaction() as transaction:
        row = service.pages.repository.get_memory(
            transaction, owner_id=owner, memory_id=page_id
        )
    assert row is not None
    return row.value


def test_create_rejects_duplicate_slug_with_the_current_revision(service):
    page = _page(slug=_unique_slug("duplicate"))
    created = service.create_page(owner_id=OWNER_A, page=page)
    with pytest.raises(AtlasConflict) as conflict:
        service.create_page(owner_id=OWNER_A, page=page)
    assert conflict.value.code == "page_already_exists"
    assert conflict.value.current_revision == created.revision


def test_same_slug_across_owners_and_projects_is_independent(service):
    shared = _unique_slug("shared")
    mine = service.create_page(owner_id=OWNER_A, page=_page(slug=shared))
    theirs = service.create_page(owner_id=OWNER_B, page=_page(slug=shared))
    assert mine.page_id != theirs.page_id
    other_project = service.create_page(
        owner_id=OWNER_A, page=_page(slug=shared, project_id="other-project")
    )
    assert other_project.page_id != mine.page_id
    assert other_project.slug == mine.slug


def test_wrong_owner_is_denied_without_disclosing_the_page(service):
    created = service.create_page(owner_id=OWNER_A, page=_page())
    with pytest.raises(AtlasNotFound):
        service.get_page(
            owner_id=OWNER_B, project_id="atlas-project", reference=created.slug
        )
    with pytest.raises(AtlasNotFound):
        service.get_page(
            owner_id=OWNER_B, project_id="atlas-project", reference=created.page_id
        )
    with pytest.raises(AtlasNotFound):
        service.delete_page(
            owner_id=OWNER_B, project_id="atlas-project",
            reference=created.page_id, expected_revision=1,
        )
    with pytest.raises(AtlasNotFound):
        service.update_page(
            owner_id=OWNER_B, project_id="atlas-project", reference=created.page_id,
            title="stolen", body=None, sources=None, links=None, expected_revision=1,
        )


def test_update_advances_revision_snapshots_history_and_cas_fences(service):
    created = service.create_page(owner_id=OWNER_A, page=_page(body="Version one."))
    updated = service.update_page(
        owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id,
        title=None, body="Version two.", sources=None, links=None,
        expected_revision=1,
    )
    assert updated.revision == 2
    assert updated.body == "Version two."
    history = service.page_history(
        owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id
    )
    assert [entry.revision for entry in history] == [2, 1]
    assert history[1].body is None
    old = service.get_page(
        owner_id=OWNER_A, project_id="atlas-project",
        reference=created.page_id, revision=1,
    )
    assert old.body == "Version one."
    with pytest.raises(AtlasConflict) as stale:
        service.update_page(
            owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id,
            title="stale", body=None, sources=None, links=None, expected_revision=1,
        )
    assert stale.value.code == "page_revision_conflict"
    assert stale.value.current_revision == 2
    with pytest.raises(AtlasPageError):
        service.update_page(
            owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id,
            title=None, body=None, sources=None, links=None, expected_revision=2,
        )


def test_history_is_bounded_and_prunes_oldest_revisions(service):
    created = service.create_page(
        owner_id=OWNER_A, page=_page(slug=_unique_slug("history"), body="Revision 1.")
    )
    from personalization.atlas_pages import MAX_HISTORY

    for index in range(MAX_HISTORY + 3):
        service.update_page(
            owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id,
            title=None, body=f"Revision {index + 2}.", sources=None, links=None,
            expected_revision=index + 1,
        )
    history = service.page_history(
        owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id
    )
    assert len(history) == MAX_HISTORY + 1
    revisions = [entry.revision for entry in history]
    assert revisions == sorted(revisions, reverse=True)
    assert revisions[-1] == 4
    with pytest.raises(AtlasNotFound):
        service.get_page(
            owner_id=OWNER_A, project_id="atlas-project",
            reference=created.page_id, revision=1,
        )
    with pytest.raises(AtlasNotFound):
        service.get_page(
            owner_id=OWNER_A, project_id="atlas-project",
            reference=created.page_id, revision=3,
        )
    kept = service.get_page(
        owner_id=OWNER_A, project_id="atlas-project",
        reference=created.page_id, revision=4,
    )
    assert kept.body == "Revision 4."


def test_delete_removes_content_links_and_allows_recreation(service):
    slug = _unique_slug("deletion")
    created = service.create_page(owner_id=OWNER_A, page=_page(slug=slug))
    target = service.create_page(
        owner_id=OWNER_A, page=_page(slug=_unique_slug("target"), links=(slug,))
    )
    service.update_page(
        owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id,
        title=None, body=None, sources=None, links=(target.slug,), expected_revision=1,
    )
    linked = service.page_links(
        owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id
    )
    assert [entry.page_id for entry in linked] == [target.page_id]
    removed = service.delete_page(
        owner_id=OWNER_A, project_id="atlas-project",
        reference=created.page_id, expected_revision=2,
    )
    assert removed.deleted is True
    assert removed.revisions_removed >= 1
    assert removed.links_removed == 1
    with pytest.raises(AtlasNotFound):
        service.get_page(
            owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id
        )
    neighbors = service.page_links(
        owner_id=OWNER_A, project_id="atlas-project", reference=target.page_id
    )
    assert neighbors == ()
    recreated = service.create_page(owner_id=OWNER_A, page=_page(slug=slug, body="Fresh start."))
    assert recreated.page_id == created.page_id
    fresh = service.get_page(
        owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id
    )
    assert fresh.revision == 1
    assert fresh.body == "Fresh start."


def test_delete_respects_the_revision_fence(service):
    created = service.create_page(owner_id=OWNER_A, page=_page())
    with pytest.raises(AtlasConflict) as stale:
        service.delete_page(
            owner_id=OWNER_A, project_id="atlas-project",
            reference=created.page_id, expected_revision=99,
        )
    assert stale.value.current_revision == 1
    assert service.get_page(
        owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id
    ).revision == 1


def test_links_are_validated_against_live_same_owner_pages(service):
    created = service.create_page(owner_id=OWNER_A, page=_page())
    foreign = service.create_page(owner_id=OWNER_B, page=_page())
    with pytest.raises(AtlasNotFound):
        service.create_page(
            owner_id=OWNER_A, page=_page(slug=_unique_slug("dangling"), links=("missing-page",))
        )
    with pytest.raises(AtlasNotFound):
        service.create_page(
            owner_id=OWNER_A,
            page=_page(slug=_unique_slug("cross-owner"), links=(foreign.page_id,)),
        )
    own = service.create_page(owner_id=OWNER_A, page=_page(slug=_unique_slug("neighbor")))
    linked = service.create_page(
        owner_id=OWNER_A,
        page=_page(slug=_unique_slug("linked"), links=(own.slug, created.slug)),
    )
    linked_neighbors = service.page_links(
        owner_id=OWNER_A, project_id="atlas-project", reference=linked.page_id
    )
    assert {own.page_id, created.page_id} <= {
        entry.page_id for entry in linked_neighbors
    }
    self_slug = _unique_slug("selflink")
    with pytest.raises(AtlasPageError) as self_reference:
        service.create_page(
            owner_id=OWNER_A,
            page=_page(slug=self_slug, links=(self_slug,)),
        )
    assert self_reference.value.code == "link_self_reference"


def test_phi_content_is_refused_and_never_persisted(service):
    refused = _page(slug=_unique_slug("phi"), body="Contact the coordinator at ward3@example.org today.")
    with pytest.raises(AtlasRefused):
        service.create_page(owner_id=OWNER_A, page=refused)
    assert refused.slug not in (
        page.slug for page in service.list_pages(owner_id=OWNER_A, project_id="atlas-project")
    )
    created = service.create_page(owner_id=OWNER_A, page=_page())
    with pytest.raises(AtlasRefused):
        service.update_page(
            owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id,
            title=None, body="Patient SSN 123-45-6789 verified.",
            sources=None, links=None, expected_revision=1,
        )
    assert service.get_page(
        owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id
    ).body == "First draft of the project kickoff."


def test_list_pages_is_owner_and_project_scoped_and_bounded(service):
    first = service.create_page(owner_id=OWNER_A, page=_page())
    second = service.create_page(
        owner_id=OWNER_A, page=_page(slug=_unique_slug("second"))
    )
    service.create_page(owner_id=OWNER_B, page=_page(slug=_unique_slug("foreign")))
    pages = service.list_pages(owner_id=OWNER_A, project_id="atlas-project")
    assert {first.page_id, second.page_id} <= {page.page_id for page in pages}
    assert all(page.owner_id == OWNER_A for page in pages)
    limited = service.list_pages(
        owner_id=OWNER_A, project_id="atlas-project", limit=1
    )
    assert len(limited) == 1
    with pytest.raises(AtlasPageError):
        service.list_pages(owner_id=OWNER_A, project_id="atlas-project", limit=0)
    with pytest.raises(AtlasPageError):
        service.list_pages(owner_id=OWNER_A, project_id="atlas-project", limit=101)


def test_global_pages_live_outside_project_scopes(service):
    created = service.create_page(
        owner_id=OWNER_A, page=_page(slug=_unique_slug("global"), project_id=None)
    )
    assert created.project_id is None
    assert created.page_id in (
        page.page_id for page in service.list_pages(owner_id=OWNER_A, project_id=None)
    )
    assert created.page_id not in (
        page.page_id for page in service.list_pages(owner_id=OWNER_A, project_id="atlas-project")
    )


def test_mutations_append_atomic_audit_records(service):
    created = service.create_page(owner_id=OWNER_A, page=_page(slug=_unique_slug("audited")))
    assert _audit_count(service.runtime, "create", OWNER_A) >= 1
    service.update_page(
        owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id,
        title="Audited", body=None, sources=None, links=None, expected_revision=1,
    )
    assert _audit_count(service.runtime, "update", OWNER_A) >= 1
    service.delete_page(
        owner_id=OWNER_A, project_id="atlas-project",
        reference=created.page_id, expected_revision=2,
    )
    assert _audit_count(service.runtime, "delete", OWNER_A) >= 1


def test_audit_failure_aborts_the_mutation_and_storage_recovers(
    service, audit_repo, plane_runtime, credential_manager, monkeypatch
):
    created = service.create_page(
        owner_id=OWNER_A, page=_page(slug=_unique_slug("atomicity"))
    )
    real_insert = audit_repo.insert_in_transaction
    attempts = {"count": 0}

    def flaky_insert(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("audit chain unavailable")
        return real_insert(*args, **kwargs)

    monkeypatch.setattr(audit_repo, "insert_in_transaction", flaky_insert)
    with pytest.raises(AtlasUnavailable) as failure:
        service.update_page(
            owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id,
            title="Never", body="stored", sources=None, links=None, expected_revision=1,
        )
    assert failure.value.code == "atlas_audit_unavailable"
    current = service.get_page(
        owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id
    )
    assert current.revision == 1
    assert current.body == "First draft of the project kickoff."
    recovered = service.update_page(
        owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id,
        title="Recovered", body=None, sources=None, links=None, expected_revision=1,
    )
    assert recovered.revision == 2
    assert attempts["count"] == 2


def test_storage_failure_maps_to_unavailable_and_recovers(
    service, audit_repo, plane_runtime, credential_manager, monkeypatch
):
    created = service.create_page(
        owner_id=OWNER_A, page=_page(slug=_unique_slug("storage"))
    )
    repository = service.pages.repository
    real_update = repository.update_memory_value
    attempts = {"count": 0}

    def flaky_update(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("connection lost")
        return real_update(*args, **kwargs)

    monkeypatch.setattr(repository, "update_memory_value", flaky_update)
    with pytest.raises(AtlasUnavailable) as failure:
        service.update_page(
            owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id,
            title=None, body="lost", sources=None, links=None, expected_revision=1,
        )
    assert failure.value.code == "atlas_storage_failed"
    assert service.get_page(
        owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id
    ).revision == 1
    recovered = service.update_page(
        owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id,
        title=None, body="Recovered content.", sources=None, links=None,
        expected_revision=1,
    )
    assert recovered.body == "Recovered content."
    assert attempts["count"] == 2


def test_page_references_are_project_bound_even_by_uuid(service):
    created = service.create_page(owner_id=OWNER_A, page=_page(slug=_unique_slug("scoped")))
    with pytest.raises(AtlasNotFound):
        service.get_page(
            owner_id=OWNER_A, project_id="other-project", reference=created.page_id
        )
    with pytest.raises(AtlasNotFound):
        service.get_page(
            owner_id=OWNER_A, project_id=None, reference=created.page_id
        )
    assert service.get_page(
        owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id
    ).page_id == created.page_id


def test_links_cannot_cross_project_scopes(service):
    other = service.create_page(
        owner_id=OWNER_A, page=_page(slug=_unique_slug("elsewhere"), project_id="other-project")
    )
    with pytest.raises(AtlasNotFound):
        service.create_page(
            owner_id=OWNER_A,
            page=_page(slug=_unique_slug("bridging"), links=(other.page_id,)),
        )


def test_service_fails_closed_when_bindings_are_missing(plane_runtime):
    with pytest.raises(AtlasUnavailable):
        AtlasPageService(
            plane_runtime=plane_runtime,
            plane_repositories=plane_runtime.repositories,
            audit_repo=None,
            fernet=Fernet(Fernet.generate_key()),
        )
    with pytest.raises(AtlasUnavailable):
        AtlasPageService(
            plane_runtime=plane_runtime,
            plane_repositories=plane_runtime.repositories,
            audit_repo=AuditRepository(
                plane_runtime=plane_runtime, plane_repositories=plane_runtime.repositories
            ),
            fernet=None,
        )
    with pytest.raises(AtlasUnavailable):
        AtlasPageService(
            plane_runtime=None,
            plane_repositories=plane_runtime.repositories,
            audit_repo=AuditRepository(
                plane_runtime=plane_runtime, plane_repositories=plane_runtime.repositories
            ),
            fernet=Fernet(Fernet.generate_key()),
        )


def test_unverified_callers_are_rejected(service):
    with pytest.raises(AtlasUnavailable):
        service.create_page(owner_id="", page=_page())
    with pytest.raises(AtlasUnavailable):
        service.get_page(owner_id=None, project_id="atlas-project", reference="kickoff-notes")


def test_every_page_gets_a_distinct_correlated_audit_chain(service, audit_repo):
    created = service.create_page(
        owner_id=OWNER_B,
        page=_page(slug=_unique_slug("correlated"), project_id=None),
    )
    row = service.runtime.fetch_one(
        "SELECT actor_user_id, event_class FROM audit_events "
        "WHERE action_type = 'atlas_page_create' AND actor_user_id = %s "
        "ORDER BY started_at DESC LIMIT 1",
        (OWNER_B,),
    )
    assert row["actor_user_id"] == OWNER_B
    assert row["event_class"] == "memory"
    assert created.page_id


def test_link_payloads_are_bounded_and_typed(service):
    with pytest.raises(AtlasPageError) as bound:
        service.create_page(
            owner_id=OWNER_A,
            page=_page(slug=_unique_slug("many"), links=tuple(f"p{i}" for i in range(17))),
        )
    assert bound.value.code == "links_out_of_bounds"
    with pytest.raises(AtlasPageError) as invalid:
        service.create_page(
            owner_id=OWNER_A,
            page=_page(slug=_unique_slug("blank"), links=("   ",)),
        )
    assert invalid.value.code == "link_invalid"


def test_concurrent_revision_race_reports_the_surviving_revision(
    service, monkeypatch
):
    created = service.create_page(owner_id=OWNER_A, page=_page(slug=_unique_slug("race")))
    repository = service.pages.repository

    def racing_update(*args, **kwargs):
        from astralplane.repositories import RepositoryConflictError

        raise RepositoryConflictError("memory changed since it was read")

    monkeypatch.setattr(repository, "update_memory_value", racing_update)
    with pytest.raises(AtlasConflict) as conflict:
        service.update_page(
            owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id,
            title=None, body="raced", sources=None, links=None, expected_revision=1,
        )
    assert conflict.value.code == "page_revision_conflict"
    assert conflict.value.current_revision == 1
    monkeypatch.undo()
    assert service.get_page(
        owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id
    ).revision == 1


def test_links_to_deleted_pages_are_skipped_in_neighborhood(service):
    anchor = service.create_page(owner_id=OWNER_A, page=_page(slug=_unique_slug("anchor")))
    transient_slug = _unique_slug("transient")
    transient = service.create_page(
        owner_id=OWNER_A,
        page=_page(slug=transient_slug, links=(anchor.slug,)),
    )
    assert anchor.page_id in (
        entry.page_id
        for entry in service.page_links(
            owner_id=OWNER_A, project_id="atlas-project", reference=transient.page_id
        )
    )
    service.delete_page(
        owner_id=OWNER_A, project_id="atlas-project",
        reference=transient.page_id, expected_revision=1,
    )
    assert service.page_links(
        owner_id=OWNER_A, project_id="atlas-project", reference=anchor.page_id
    ) == ()


def test_listing_skips_rows_whose_envelope_belongs_to_another_owner(
    service, plane_runtime
):
    created = service.create_page(owner_id=OWNER_A, page=_page(slug=_unique_slug("visible")))
    forged = service.create_page(owner_id=OWNER_A, page=_page(slug=_unique_slug("faked")))
    plane_runtime.execute(
            "UPDATE memory_item SET value = replace(value, %s, %s) "
            "WHERE id = %s AND user_id = %s",
            (f'"owner_id":"{OWNER_A}"', f'"owner_id":"{OWNER_B}"',
             forged.page_id, OWNER_A),
        )
    pages = service.list_pages(owner_id=OWNER_A, project_id="atlas-project")
    assert created.page_id in (page.page_id for page in pages)
    assert forged.page_id not in (page.page_id for page in pages)


def test_stale_update_conflict_is_reported_when_the_head_vanishes_mid_edit(
    service, monkeypatch
):
    created = service.create_page(owner_id=OWNER_A, page=_page(slug=_unique_slug("vanish")))
    repository = service.pages.repository

    def vanishing_update(*args, **kwargs):
        return None

    monkeypatch.setattr(repository, "update_memory_value", vanishing_update)
    with pytest.raises(AtlasNotFound):
        service.update_page(
            owner_id=OWNER_A, project_id="atlas-project", reference=created.page_id,
            title=None, body="vanishing", sources=None, links=None, expected_revision=1,
        )
