"""Exercises the guarded 089.007-to-089.008 Atlas link edge on populated PostgreSQL data.
Existing pages and prior records stay unchanged while repeat, interruption, namesake, and
catalog-damage cases fail closed or retry forward without adopting foreign objects.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from astralplane.repositories.atlas_links import AtlasLinkSetCommand
from astralplane.repositories.atlas_pages import AtlasPageRepository
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_atlas_pages_upgrade import page_command, seed_populated
from tests.integration.test_session_issuer_upgrade import retained_rows

OWNER = "atlas-link-upgrade-owner"
PAGE_ONE = "40000000-0000-4000-8000-000000000001"
PAGE_TWO = "50000000-0000-4000-8000-000000000002"


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.007"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.007"),
        current_schema_verifier_checksum=m.PLANE_SCHEMA_089_007_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            m.PLANE_SCHEMA_089_007_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    )
    assert registry.digest == m.PLANE_SCHEMA_089_007_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.007",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            value
            for value in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from
            if value < "089.007"
        ),
        accepted_predecessor_digests=tuple(
            pair
            for pair in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if pair[0] < "089.007"
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


def seed_pages(tx):
    pages = AtlasPageRepository()
    first = pages.create_page(tx, command=page_command())
    second = pages.create_page(
        tx,
        command=replace(
            page_command(),
            page_id=PAGE_TWO,
            slug="upgrade-page-two",
        ),
    )
    return first.page, second.page


def link_command(**changes):
    values = dict(
        owner_id=OWNER,
        command_id="60000000-0000-4000-8000-000000000001",
        page_id=PAGE_ONE,
        expected_page_revision=1,
        expected_link_revision=0,
        targets=(PAGE_TWO,),
        updated_at=40,
    )
    values.update(changes)
    return AtlasLinkSetCommand(**values)


def test_populated_089_008_upgrade_preserves_pages_and_adds_only_link_tables(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = seed_populated(tx)
        first, second = seed_pages(tx)
        before = retained_rows(tx, tables)
        pages_before = retained_rows(tx, ("atlas_page", "atlas_page_command"))
    report = current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    assert report.applied_steps == (
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
        assert retained_rows(tx, ("atlas_page", "atlas_page_command")) == pages_before
        assert tx.fetch_all("SELECT * FROM atlas_page_link") == ()
        assert tx.fetch_all("SELECT * FROM atlas_page_link_head") == ()
        assert tx.fetch_all("SELECT * FROM atlas_page_link_command") == ()
    assert (
        current_runner(db)
        .run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
        .already_current
    )
    from astralplane.api import create_repository_catalog

    with db.transaction() as tx:
        changed = create_repository_catalog().atlas_links.set_links(
            tx,
            command=link_command(owner_id=first.owner_id),
        )
        assert [link.target_page_id for link in changed.links] == [second.page_id]
    assert (
        current_runner(db)
        .run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
        .already_current
    )
    with pytest.raises(SchemaRevisionError):
        prior_runner(db).run(expected_revision="089.007")


def test_interrupted_atlas_link_edge_rolls_back_and_retries(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = seed_populated(tx)
        first, _ = seed_pages(tx)
        before = retained_rows(tx, tables)
        pages_before = retained_rows(tx, ("atlas_page", "atlas_page_command"))
    with pytest.raises(RuntimeError, match="injected atlas link failure"), db.transaction() as tx:
        m.PLANE_SCHEMA_089_008_MIGRATION.apply(tx)
        raise RuntimeError("injected atlas link failure")
    with db.transaction() as tx:
        assert retained_rows(tx, tables) == before
        assert retained_rows(tx, ("atlas_page", "atlas_page_command")) == pages_before
        assert tx.fetch_one("SELECT to_regclass('atlas_page_link') AS relation")["relation"] is None
        assert (
            tx.fetch_one("SELECT to_regclass('atlas_page_link_head') AS relation")["relation"]
            is None
        )
        assert (
            tx.fetch_one("SELECT to_regclass('atlas_page_link_command') AS relation")["relation"]
            is None
        )
        assert tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == (
            "089.007"
        )
    assert current_runner(db).run(
        expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
    ).applied_steps == (
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
        assert retained_rows(tx, ("atlas_page", "atlas_page_command")) == pages_before
        assert tx.fetch_one("SELECT count(*) AS count FROM atlas_page_link")["count"] == 0
        assert first.page_id == PAGE_ONE


def test_atlas_link_namesake_or_damaged_predecessor_is_refused(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tx.execute("CREATE TABLE atlas_page_link (owner_id TEXT PRIMARY KEY)")
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    with db.transaction() as tx:
        assert tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == (
            "089.007"
        )
        assert (
            tx.fetch_one("SELECT to_regclass('atlas_page_link_head') AS relation")["relation"]
            is None
        )
        assert (
            tx.fetch_one("SELECT to_regclass('atlas_page_link_command') AS relation")["relation"]
            is None
        )


def test_damaged_atlas_page_predecessor_is_refused_without_link_adoption(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tx.execute("DROP INDEX atlas_page_live_listing")
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    with db.transaction() as tx:
        assert tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == (
            "089.007"
        )
        assert tx.fetch_one("SELECT to_regclass('atlas_page_link') AS relation")["relation"] is None


@pytest.mark.parametrize(
    "corruption",
    [
        "DROP INDEX atlas_page_link_outbound",
        "DROP INDEX atlas_page_link_backlinks",
        "DROP TRIGGER atlas_page_link_immutable ON atlas_page_link",
        "DROP TRIGGER atlas_page_link_head_advance ON atlas_page_link_head",
        "DROP TRIGGER atlas_page_link_command_immutable ON atlas_page_link_command",
        "ALTER TABLE atlas_page_link DROP CONSTRAINT atlas_page_link_not_reflexive",
        "ALTER TABLE atlas_page_link_command DROP CONSTRAINT atlas_page_link_command_check",
    ],
)
def test_current_atlas_link_catalog_damage_is_refused(migrated_clone, corruption):
    db = migrated_clone.database
    with db.transaction() as tx:
        tx.execute(corruption)
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
