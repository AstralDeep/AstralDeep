"""AttachmentRepository owner-scoped read adapter."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from orchestrator.attachments.repository import AttachmentRepository
from .conftest import insert_sample


class _PlaneRuntime:
    @contextmanager
    def transaction(self):
        yield object()


def _repository(stub_db) -> AttachmentRepository:
    return AttachmentRepository.from_plane_source(
        SimpleNamespace(
            plane_runtime=_PlaneRuntime(),
            plane_repositories=stub_db.plane_repositories,
        )
    )


def test_from_plane_source_requires_and_uses_application_binding(stub_db):
    runtime = _PlaneRuntime()
    source = SimpleNamespace(
        plane_runtime=runtime,
        plane_repositories=stub_db.plane_repositories,
    )
    repo = AttachmentRepository.from_plane_source(source)
    attachment_id = insert_sample(repo, user_id="alice")
    assert repo.db is None
    assert repo.get_by_id(attachment_id, "alice") is not None

    with pytest.raises(RuntimeError, match="application Plane runtime"):
        AttachmentRepository.from_plane_source(SimpleNamespace())


def test_seeded_fixture_get_by_id(stub_db):
    repo = _repository(stub_db)
    aid = insert_sample(repo, user_id="alice")
    got = repo.get_by_id(aid, "alice")
    assert got is not None
    assert got.attachment_id == aid
    assert got.user_id == "alice"


def test_get_by_id_returns_none_for_foreign_user(stub_db):
    repo = _repository(stub_db)
    aid = insert_sample(repo, user_id="alice")
    assert repo.get_by_id(aid, "bob") is None


def test_list_filters_by_user_and_category(stub_db):
    repo = _repository(stub_db)
    insert_sample(repo, user_id="alice", category="document", extension="pdf")
    insert_sample(repo, user_id="alice", category="image", extension="png")
    insert_sample(repo, user_id="bob", category="document", extension="pdf")

    alice_all, _ = repo.list_for_user("alice")
    assert len(alice_all) == 2

    alice_docs, _ = repo.list_for_user("alice", category="document")
    assert len(alice_docs) == 1
    assert alice_docs[0].category == "document"

    # Bob's listing must never include alice's rows.
    bob_all, _ = repo.list_for_user("bob")
    assert len(bob_all) == 1
    assert bob_all[0].user_id == "bob"


def test_list_pagination_cursor(stub_db):
    repo = _repository(stub_db)
    ids = [insert_sample(repo, user_id="alice") for _ in range(5)]
    page1, cursor = repo.list_for_user("alice", limit=2)
    assert len(page1) == 2
    assert cursor is not None
    page2, cursor2 = repo.list_for_user("alice", limit=2, cursor=cursor)
    assert len(page2) == 2
    page3, cursor3 = repo.list_for_user("alice", limit=2, cursor=cursor2)
    assert len(page3) == 1
    assert cursor3 is None
    seen = {a.attachment_id for a in (*page1, *page2, *page3)}
    assert seen == set(ids)


def test_public_adapter_exposes_no_metadata_or_blob_mutation_bypass(stub_db):
    repo = _repository(stub_db)
    for method in (
        "insert",
        "ainsert",
        "soft_delete",
        "asoft_delete",
        "soft_delete_all_for_user",
        "asoft_delete_all_for_user",
    ):
        assert not hasattr(repo, method)
