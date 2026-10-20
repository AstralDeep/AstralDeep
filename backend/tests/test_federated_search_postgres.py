"""End-to-end tests for personalization/federated_search.py and search_api.py over a real
PostgreSQL-backed AstralPlane runtime, host encryption, history, Atlas pages, and HTTP auth:
mixed sources, filters, ranking, cursors, deletion, partial failure, sanitization, and audit.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from audit.recorder import Recorder, set_recorder
from audit.repository import AuditRepository
from orchestrator.credential_manager import CredentialManager
from personalization.atlas_service import (
    AtlasPageError,
    AtlasPageService,
    AtlasUnavailable,
    PageInput,
)
from personalization.explicit_notes import (
    ExplicitNoteCipher,
    ExplicitNoteMetadata,
)
from personalization.federated_search import (
    FederatedSearchService,
    SearchQueryInvalid,
    hit_payload,
)
from personalization.repository import PersonalizationRepository
from tests.helpers.voice_plane_runtime import isolated_plane_runtime


class _CleanGate:
    def contains_phi(self, value):
        return False


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("federated_search") as runtime:
        yield runtime


@pytest.fixture(scope="module")
def credential_manager(plane_runtime, tmp_path_factory):
    return CredentialManager(
        plane_runtime=plane_runtime,
        data_dir=str(tmp_path_factory.mktemp("federated-search-keys")),
    )


@pytest.fixture(scope="module")
def audit_repo(plane_runtime):
    return AuditRepository(
        plane_runtime=plane_runtime, plane_repositories=plane_runtime.repositories
    )


@pytest.fixture(scope="module")
def repository(plane_runtime):
    return PersonalizationRepository(
        None, plane_runtime=plane_runtime, plane_repositories=plane_runtime.repositories
    )


@pytest.fixture(scope="module")
def atlas(plane_runtime, credential_manager, audit_repo):
    return AtlasPageService(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
        audit_repo=audit_repo,
        fernet=credential_manager._fernet,
        phi_gate=_CleanGate(),
    )


@pytest.fixture
def orchestrator(plane_runtime, credential_manager, audit_repo, repository, atlas):
    return SimpleNamespace(
        runtime_composition=SimpleNamespace(
            plane=SimpleNamespace(
                runtime=plane_runtime, repositories=plane_runtime.repositories
            )
        ),
        credential_manager=credential_manager,
        audit_repo=audit_repo,
        personalization_service=SimpleNamespace(repo=repository),
        atlas_pages=atlas,
    )


def _put_note(plane_runtime, credential_manager, owner, value, *, category="context"):
    cipher = ExplicitNoteCipher(credential_manager._fernet)
    note_id = str(uuid4())
    from astralplane.repositories.guidance_models import ExplicitNoteRecord

    with plane_runtime.transaction() as transaction:
        notes = plane_runtime.repositories.preferences.personalization
        prepared = notes.prepare_explicit_note(
            transaction, owner_id=owner, note_id=note_id, expected_revision=0
        )
        now = prepared.observed_at_ms
        metadata = ExplicitNoteMetadata(
            owner_id=owner, note_id=note_id, revision=1, format_version=1,
            category=category, enabled=True, created_at=now, updated_at=now,
        )
        encrypted = cipher.seal(metadata, value)
        record = ExplicitNoteRecord(**asdict(metadata), ciphertext=encrypted.ciphertext)
        notes.put_explicit_note(transaction, preparation=prepared, record=record)
    return note_id


def _put_conversation(plane_runtime, owner, title, message):
    stamp = int(time.time() * 1000)
    conversation_id = str(uuid4())
    with plane_runtime.transaction() as transaction:
        history = plane_runtime.repositories.history
        history.conversations.create(
            transaction, conversation_id=conversation_id, owner_id=owner,
            title=title, agent_id=None, created_at=stamp,
        )
        saved = history.messages.append(
            transaction, owner_id=owner, conversation_id=conversation_id,
            role="user", content=message, timestamp=stamp,
        )
    return conversation_id, saved.message_id


def _put_atlas(atlas, owner, term, *, project_id=None):
    slug = f"{term.lower()}-{uuid4().hex[:8]}"
    return atlas.create_page(owner_id=owner, page=PageInput(
        slug=slug, title=f"{term} atlas page", body=f"Atlas body mentions {term}.",
        project_id=project_id,
    ))


def _seed_corpus(plane_runtime, credential_manager, repository, atlas, owner, term):
    note_id = _put_note(
        plane_runtime, credential_manager, owner,
        f'Keep <script>alert("x")</script> {term} note tidy',
    )
    memory = repository.create_memory(
        owner, "preference", f"prefers {term} summaries", source="explicit",
        keywords=term.lower(),
    )
    conversation_id, message_id = _put_conversation(
        plane_runtime, owner, f"{term} planning",
        f"we agreed the {term} protocol ships first",
    )
    page = _put_atlas(atlas, owner, term)
    return note_id, memory["id"], (conversation_id, message_id), page.page_id


@pytest.mark.asyncio
async def test_mixed_sources_are_owner_scoped_ranked_sanitized_and_deterministic(
    plane_runtime, credential_manager, repository, atlas, orchestrator
):
    term = f"quartz{uuid4().hex[:8]}"
    owner = f"search-owner-{uuid4().hex}"
    other = f"search-other-{uuid4().hex}"
    ids = _seed_corpus(plane_runtime, credential_manager, repository, atlas, owner, term)
    _seed_corpus(plane_runtime, credential_manager, repository, atlas, other, term)
    service = FederatedSearchService(orchestrator)
    first = await service.search(owner_id=owner, query=term, limit=20)
    second = await service.search(owner_id=owner, query=term, limit=20)
    assert first.any_available and not first.partial, first.reports
    assert {hit.source for hit in first.hits} == {"notes", "memory", "conversations", "atlas"}
    assert {hit.ref.get("note_id") for hit in first.hits if hit.source == "notes"} == {ids[0]}
    assert {hit.ref.get("memory_id") for hit in first.hits if hit.source == "memory"} == {ids[1]}
    assert {hit.ref.get("conversation_id") for hit in first.hits
            if hit.source == "conversations"} == {ids[2][0]}
    assert {hit.ref.get("page_id") for hit in first.hits if hit.source == "atlas"} == {ids[3]}
    assert [hit_payload(hit) for hit in first.hits] == [hit_payload(hit) for hit in second.hits]
    assert all("<script>" not in hit.snippet for hit in first.hits)
    assert all(hit.score >= 0 for hit in first.hits)
    assert all(report.available and report.complete for report in first.reports)


@pytest.mark.asyncio
async def test_filters_and_bounded_cursor_pages_have_no_duplicates(
    plane_runtime, credential_manager, repository, atlas, orchestrator
):
    term = f"cursor{uuid4().hex[:8]}"
    owner = f"search-owner-{uuid4().hex}"
    _seed_corpus(plane_runtime, credential_manager, repository, atlas, owner, term)
    project = f"project-{uuid4().hex}"
    _put_atlas(atlas, owner, term, project_id=project)
    service = FederatedSearchService(orchestrator)

    memory_only = await service.search(
        owner_id=owner, query=term, sources="memory", types="preference"
    )
    assert memory_only.hits and all(hit.source == "memory" for hit in memory_only.hits)
    assert [report.source for report in memory_only.reports] == ["memory"]
    projected = await service.search(owner_id=owner, query=term, sources="atlas",
                                     project_id=project)
    assert {hit.ref["project_id"] for hit in projected.hits} == {project}
    all_atlas = await service.search(owner_id=owner, query=term, sources="atlas")
    assert {hit.ref["project_id"] for hit in all_atlas.hits} >= {project, None}

    collected = []
    cursor = None
    for _ in range(20):
        page = await service.search(owner_id=owner, query=term, limit=2, cursor=cursor)
        collected.extend(hit_payload(hit)["ref"] for hit in page.hits)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert collected and len(collected) == len({repr(ref) for ref in collected})
    assert len(collected) == page.total


@pytest.mark.asyncio
async def test_wrong_owner_cursor_and_deleted_source_are_refused_or_removed(
    plane_runtime, credential_manager, repository, orchestrator
):
    term = f"delete{uuid4().hex[:8]}"
    owner = f"search-owner-{uuid4().hex}"
    other = f"search-other-{uuid4().hex}"
    for index in range(3):
        repository.create_memory(
            owner, "preference", f"{term} memory {index}", source="explicit",
            keywords=term,
        )
    service = FederatedSearchService(orchestrator)
    page = await service.search(owner_id=owner, query=term, sources="memory", limit=1)
    assert page.next_cursor
    with pytest.raises(SearchQueryInvalid) as refused:
        await service.search(owner_id=other, query=term, sources="memory", limit=1,
                             cursor=page.next_cursor)
    assert refused.value.code == "search_cursor_scope_invalid"
    with pytest.raises(SearchQueryInvalid):
        await service.search(owner_id=owner, query=term, sources="memory", limit=1,
                             types="goal", cursor=page.next_cursor)
    target = next(hit.ref["memory_id"] for hit in page.hits)
    assert repository.delete_memory(owner, target)
    rest = await service.search(owner_id=owner, query=term, sources="memory", limit=10,
                                cursor=page.next_cursor)
    assert target not in {hit.ref["memory_id"] for hit in rest.hits}
    assert rest.total == 2


@pytest.mark.asyncio
async def test_partial_source_failure_keeps_other_authorized_results(
    plane_runtime, credential_manager, repository, atlas, orchestrator, monkeypatch
):
    term = f"partial{uuid4().hex[:8]}"
    owner = f"search-owner-{uuid4().hex}"
    _seed_corpus(plane_runtime, credential_manager, repository, atlas, owner, term)
    service = FederatedSearchService(orchestrator)

    def broken_atlas(*args, **kwargs):
        raise RuntimeError("atlas offline")

    monkeypatch.setattr(service, "_scan_atlas", broken_atlas)
    outcome = await service.search(owner_id=owner, query=term)
    reports = {report.source: report for report in outcome.reports}
    assert not reports["atlas"].available
    assert outcome.hits and {hit.source for hit in outcome.hits} == {
        "notes", "memory", "conversations"
    }
    assert outcome.partial


@pytest.mark.asyncio
async def test_cancellation_stops_search_without_delivery(orchestrator, monkeypatch):
    import asyncio

    service = FederatedSearchService(orchestrator)
    started = asyncio.Event()

    def blocking(*args, **kwargs):
        import time as time_module
        time_module.sleep(0.05)
        started.set()
        return [], SimpleNamespace()

    monkeypatch.setattr(service, "_scan_memory", blocking)
    task = asyncio.create_task(service.search(
        owner_id=f"search-owner-{uuid4().hex}", query="cancel", sources="memory"
    ))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _client(orchestrator, owner):
    from orchestrator.auth import get_current_user_payload, require_user_id
    from personalization.search_api import search_router

    identity = {"owner": owner}

    app = FastAPI(title="Federated Search Test App")
    app.include_router(search_router)
    app.state.orchestrator = orchestrator
    app.dependency_overrides[require_user_id] = lambda: identity["owner"]
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": identity["owner"], "preferred_username": identity["owner"],
    }
    return TestClient(app), identity


def test_http_search_filters_pages_denies_wrong_owner_cursor_and_audits(
    plane_runtime, credential_manager, repository, atlas, orchestrator, audit_repo
):
    term = f"http{uuid4().hex[:8]}"
    owner = f"search-owner-{uuid4().hex}"
    other = f"search-other-{uuid4().hex}"
    _seed_corpus(plane_runtime, credential_manager, repository, atlas, owner, term)
    client, identity = _client(orchestrator, owner)
    recorder = Recorder(audit_repo)
    set_recorder(recorder)
    try:
        with client:
            response = client.get("/api/search", params={"q": term, "limit": 2})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["count"] == 2 and body["next_cursor"]
            assert body["partial"] is False, body["reports"]
            assert all("<script>" not in hit["snippet"] for hit in body["hits"])
            memory = client.get("/api/search", params={
                "q": term, "source": "memory", "type": "preference",
            })
            assert memory.status_code == 200
            assert {hit["source"] for hit in memory.json()["hits"]} == {"memory"}
            cursor = body["next_cursor"]
            identity["owner"] = other
            denied = client.get("/api/search", params={
                "q": term, "limit": 2, "cursor": cursor,
            })
            assert denied.status_code == 422
            assert denied.json()["detail"] == "search_cursor_scope_invalid"
            identity["owner"] = owner
            for params, detail in (
                ({"q": "x", "source": "bogus"}, "search_source_invalid"),
                ({"q": "x", "type": "bogus"}, "search_type_invalid"),
                ({"q": "x", "cursor": "!!!!"}, "search_cursor_invalid"),
            ):
                bad = client.get("/api/search", params=params)
                assert bad.status_code == 422 and bad.json()["detail"] == detail
            assert client.get("/api/search", params={"q": "x", "limit": 0}).status_code == 422
        row = plane_runtime.fetch_one(
            "SELECT inputs_meta, outputs_meta FROM audit_events "
            "WHERE actor_user_id=%s AND action_type='personalization.search' "
            "ORDER BY started_at DESC LIMIT 1",
            (owner,),
        )
        assert row is not None
        assert term not in str(row["inputs_meta"]) and term not in str(row["outputs_meta"])
        assert "query_chars" in row["inputs_meta"]
    finally:
        set_recorder(None)


def test_http_search_reports_total_source_failure_as_503(
    plane_runtime, credential_manager, audit_repo
):
    from personalization.search_api import search_router

    owner = f"search-owner-{uuid4().hex}"
    broken = SimpleNamespace(
        runtime_composition=SimpleNamespace(plane=SimpleNamespace(
            runtime=plane_runtime, repositories=plane_runtime.repositories,
        )),
        credential_manager=None,
        audit_repo=None,
        personalization_service=None,
        atlas_pages=None,
    )
    from orchestrator.auth import get_current_user_payload, require_user_id

    app = FastAPI(title="Federated Search Failure App")
    app.include_router(search_router)
    app.state.orchestrator = broken
    app.dependency_overrides[require_user_id] = lambda: owner
    app.dependency_overrides[get_current_user_payload] = lambda: {"sub": owner}
    with TestClient(app) as client:
        response = client.get("/api/search", params={"q": "anything", "source": "atlas"})
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "search_sources_unavailable"


def test_http_search_reports_unbound_orchestrator_as_503():
    from orchestrator.auth import get_current_user_payload, require_user_id
    from personalization.search_api import search_router

    app = FastAPI(title="Federated Search Unbound App")
    app.include_router(search_router)
    app.dependency_overrides[require_user_id] = lambda: "owner"
    app.dependency_overrides[get_current_user_payload] = lambda: {"sub": "owner"}
    with TestClient(app) as client:
        missing = client.get("/api/search", params={"q": "anything"})
    assert missing.status_code == 503 and missing.json()["detail"] == "search_unavailable"

    app.state.orchestrator = SimpleNamespace()
    with TestClient(app) as client:
        broken = client.get("/api/search", params={"q": "anything"})
    assert broken.status_code == 503 and broken.json()["detail"] == "search_unavailable"


def test_atlas_scan_rejects_invalid_scope_and_limit(atlas):
    owner = f"search-owner-{uuid4().hex}"
    with pytest.raises(AtlasPageError) as scope:
        atlas.scan_pages(owner_id=owner, all_projects="yes")
    assert scope.value.code == "project_scope_invalid"
    with pytest.raises(AtlasPageError) as limit:
        atlas.scan_pages(owner_id=owner, limit=0)
    assert limit.value.code == "limit_out_of_bounds"


def test_atlas_scan_bounds_heads_and_refuses_unreadable_identity(
    plane_runtime, repository, atlas
):
    owner = f"search-owner-{uuid4().hex}"
    _put_atlas(atlas, owner, "bound", project_id=None)
    _put_atlas(atlas, owner, "bound", project_id=None)
    scan = atlas.scan_pages(owner_id=owner, all_projects=True, limit=1)
    assert len(scan.pages) == 1 and scan.complete is False
    bad = repository.create_memory(
        owner, "context", "placeholder", source="explicit", keywords="",
    )
    envelope = json.dumps({
        "body": "x", "format": "astral.atlas-page/v1", "kind": "page",
        "page_id": bad["id"], "owner_id": "another-owner", "revision": 1,
    })
    with plane_runtime.transaction() as transaction:
        plane_runtime.repositories.preferences.personalization.update_memory_value(
            transaction, owner_id=owner, memory_id=bad["id"], value=envelope,
            signature=None, expected_updated_at=bad["updated_at"],
            updated_at=bad["updated_at"] + 1,
        )
    with pytest.raises(AtlasUnavailable):
        atlas.scan_pages(owner_id=owner, all_projects=True)
