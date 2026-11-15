"""Exercises the guarded 089.006-to-089.007 Atlas revision edge on populated PostgreSQL data.
Existing live heads are backfilled into immutable revisions, deleted pages stay content-free,
repeat startup is write-free, and interrupted or collided upgrades fail closed without adoption.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.atlas_pages import (
    AtlasPageEditCommand,
    AtlasPageRepository,
)
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_atlas_pages_upgrade import seed_populated
from tests.integration.test_session_issuer_upgrade import retained_rows

OWNER = "atlas-revision-upgrade-owner"
FOREIGN = "atlas-revision-upgrade-foreign"
PROJECT = "atlas-revision-upgrade-project"
PAGE_LIVE = "40000000-0000-4000-8000-000000000011"
PAGE_DELETED = "40000000-0000-4000-8000-000000000012"
COMMAND_LIVE = "50000000-0000-4000-8000-000000000011"
COMMAND_DELETED_CREATE = "50000000-0000-4000-8000-000000000012"
COMMAND_DELETED = "50000000-0000-4000-8000-000000000013"
COMMAND_EDIT = "50000000-0000-4000-8000-000000000014"
LIVE_CONTENT = b"synthetic-existing-live-head-ciphertext"
DELETED_CONTENT = b"synthetic-existing-deleted-head-ciphertext"


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge
            for edge in m.MIGRATION_REGISTRY.migrations
            if edge.target_revision <= "089.006"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.006"),
        current_schema_verifier_checksum=m.PLANE_SCHEMA_089_006_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            m.PLANE_SCHEMA_089_006_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    )
    assert registry.digest == m.PLANE_SCHEMA_089_006_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.006",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            value
            for value in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from
            if value < "089.006"
        ),
        accepted_predecessor_digests=tuple(
            pair
            for pair in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if pair[0] < "089.006"
        ),
    )
    return m.MigrationRunner(database, revision=revision, registry=registry)


def current_runner(database):
    return m.MigrationRunner(
        database, revision=m.CURRENT_DATA_PLANE_REVISION, registry=m.MIGRATION_REGISTRY
    )


@pytest.fixture(scope="module")
def predecessor_template(lineage_templates):
    return lineage_templates.template(prior_runner)


@pytest.fixture
def predecessor(predecessor_template):
    with bound_clone(predecessor_template) as clone:
        yield clone


def seed_existing_pages(tx):
    tx.execute(
        "INSERT INTO atlas_page(owner_id,page_id,project_id,slug,revision,sources,"
        "content,content_sha256,created_at,updated_at,deleted_at)"
        " VALUES(%s,%s,%s,%s,1,%s::jsonb,%s,%s,10,10,NULL)",
        (
            OWNER,
            PAGE_LIVE,
            PROJECT,
            "existing-live",
            '["synthetic/legacy-live"]',
            LIVE_CONTENT,
            hashlib.sha256(LIVE_CONTENT).hexdigest(),
        ),
    )
    tx.execute(
        "INSERT INTO atlas_page(owner_id,page_id,project_id,slug,revision,sources,"
        "content,content_sha256,created_at,updated_at,deleted_at)"
        " VALUES(%s,%s,%s,%s,2,'[]'::jsonb,NULL,NULL,10,20,20)",
        (OWNER, PAGE_DELETED, PROJECT, "existing-deleted"),
    )
    tx.execute(
        "INSERT INTO atlas_page_command(owner_id,command_id,page_id,command,"
        "request_digest,result_revision,created_at)"
        " VALUES(%s,%s,%s,'create',%s,1,10),"
        " (%s,%s,%s,'create',%s,1,10),"
        " (%s,%s,%s,'delete',%s,2,20)",
        (
            OWNER,
            COMMAND_LIVE,
            PAGE_LIVE,
            "a" * 64,
            OWNER,
            COMMAND_DELETED_CREATE,
            PAGE_DELETED,
            "b" * 64,
            OWNER,
            COMMAND_DELETED,
            PAGE_DELETED,
            "c" * 64,
        ),
    )


def test_populated_089_007_upgrade_backfills_live_heads_and_preserves_deleted_pages(
    predecessor,
):
    db = predecessor.database
    with db.transaction() as tx:
        tables = seed_populated(tx)
        seed_existing_pages(tx)
        before = retained_rows(tx, tables)
    report = current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    assert report.applied_steps == (
        "astralplane-089-atlas-revisions",
        "astralplane-089-atlas-links",
        "astralplane-089-capability-publications",
                                       "astralplane-089-operation-harness-bindings",
                                       "astralplane-089-push-deliveries",
                                       "astralplane-089-observation-records",
                                       "astralplane-089-projects",
                                       "astralplane-089-harness-captures",
                                       "astralplane-089-audit-conversation-index",
                                       "astralplane-089-chat-queue",
                                       "astralplane-089-conversation-branches",
                                       "astralplane-089-computer-use",
                                       "astralplane-089-verification-credentials",
                                       "astralplane-089-chat-read-receipts",
                                       "astralplane-089-mesh-event-store",
                                       "astralplane-089-run-and-learning-ledger",
                                   )
    with db.transaction() as tx:
        assert retained_rows(tx, tables) == before
        revisions = tx.fetch_all(
            "SELECT owner_id,page_id,revision,sources,content,content_sha256,created_at"
            " FROM atlas_page_revision ORDER BY revision"
        )
        assert len(revisions) == 1
        assert revisions[0] == {
            "owner_id": OWNER,
            "page_id": PAGE_LIVE,
            "revision": 1,
            "sources": ("synthetic/legacy-live",),
            "content": LIVE_CONTENT,
            "content_sha256": hashlib.sha256(LIVE_CONTENT).hexdigest(),
            "created_at": 10,
        }
        commands = tx.fetch_all(
            "SELECT owner_id,page_id,command,result_revision,expected_revision"
            " FROM atlas_page_command ORDER BY page_id,command"
        )
        assert commands == (
            {
                "owner_id": OWNER,
                "page_id": PAGE_LIVE,
                "command": "create",
                "result_revision": 1,
                "expected_revision": None,
            },
            {
                "owner_id": OWNER,
                "page_id": PAGE_DELETED,
                "command": "create",
                "result_revision": 1,
                "expected_revision": None,
            },
            {
                "owner_id": OWNER,
                "page_id": PAGE_DELETED,
                "command": "delete",
                "result_revision": 2,
                "expected_revision": None,
            },
        )
        repo = AtlasPageRepository()
        edited = repo.edit_page(
            tx,
            command=AtlasPageEditCommand(
                owner_id=OWNER,
                command_id=COMMAND_EDIT,
                page_id=PAGE_LIVE,
                expected_revision=1,
                sources=("synthetic/after-upgrade",),
                content=b"synthetic-after-upgrade-ciphertext",
                updated_at=30,
            ),
        )
        assert edited.page.revision == 2
        assert repo.get_revision(tx, owner_id=OWNER, page_id=PAGE_LIVE, revision=1).content == (
            LIVE_CONTENT
        )
        assert repo.get_revision(
            tx, owner_id=OWNER, page_id=PAGE_DELETED, revision=1, include_deleted=True
        ) is None
        with pytest.raises(RepositoryConflictError, match="deleted"):
            repo.edit_page(
                tx,
                command=AtlasPageEditCommand(
                    owner_id=OWNER,
                    command_id="50000000-0000-4000-8000-000000000015",
                    page_id=PAGE_DELETED,
                    expected_revision=2,
                    sources=(),
                    content=b"synthetic-resurrection-ciphertext",
                    updated_at=30,
                ),
            )
        assert repo.get_page(tx, owner_id=FOREIGN, page_id=PAGE_LIVE) is None
    repeat = current_runner(db).run(
        expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
    )
    assert repeat.already_current and repeat.applied_steps == ()
    with db.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_revision")["count"] == 2
        assert tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == (
            m.CURRENT_DATA_PLANE_REVISION.schema_revision
        )


def test_interrupted_089_007_edge_rolls_back_and_retries(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        seed_existing_pages(tx)
    with (
        pytest.raises(RuntimeError, match="injected atlas revision failure"),
        db.transaction() as tx,
    ):
        m.PLANE_SCHEMA_089_007_MIGRATION.apply(tx)
        raise RuntimeError("injected atlas revision failure")
    with db.transaction() as tx:
        assert (
            tx.fetch_one("SELECT to_regclass('atlas_page_revision') AS relation")[
                "relation"
            ]
            is None
        )
        assert tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == (
            "089.006"
        )
        assert tx.fetch_one(
            "SELECT count(*) AS count FROM atlas_page WHERE page_id=%s",
            (PAGE_LIVE,),
        )["count"] == 1
        assert tx.fetch_one(
            "SELECT count(*) AS count FROM information_schema.columns"
            " WHERE table_name='atlas_page_command' AND column_name='expected_revision'"
        )["count"] == 0
    assert current_runner(db).run(
        expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
    ).applied_steps == (
        "astralplane-089-atlas-revisions",
        "astralplane-089-atlas-links",
        "astralplane-089-capability-publications",
                           "astralplane-089-operation-harness-bindings",
                           "astralplane-089-push-deliveries",
                           "astralplane-089-observation-records",
                           "astralplane-089-projects",
                           "astralplane-089-harness-captures",
                           "astralplane-089-audit-conversation-index",
                           "astralplane-089-chat-queue",
                           "astralplane-089-conversation-branches",
                           "astralplane-089-computer-use",
                           "astralplane-089-verification-credentials",
                           "astralplane-089-chat-read-receipts",
                           "astralplane-089-mesh-event-store",
                           "astralplane-089-run-and-learning-ledger",
                       )


def test_atlas_revision_namesake_or_damaged_predecessor_is_refused(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        seed_existing_pages(tx)
        tx.execute("CREATE TABLE atlas_page_revision (owner_id TEXT PRIMARY KEY)")
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    with db.transaction() as tx:
        assert tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == (
            "089.006"
        )
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_revision")["count"] == 0
        assert tx.fetch_one(
            "SELECT count(*) AS count FROM pg_constraint"
            " WHERE conname='atlas_page_command_expected_revision_check'"
        )["count"] == 0


def test_damaged_atlas_predecessor_is_refused_without_revision_adoption(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        seed_existing_pages(tx)
        tx.execute("DROP TRIGGER atlas_page_lifecycle ON atlas_page")
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    with db.transaction() as tx:
        assert tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == (
            "089.006"
        )
        assert (
            tx.fetch_one("SELECT to_regclass('atlas_page_revision') AS relation")[
                "relation"
            ]
            is None
        )
