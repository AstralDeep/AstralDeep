"""Tests for tests/fixtures/migrated_template.py: current and predecessor lineage clones start at
their declared revision and never see each other's writes, a chained lineage equals one built from
empty, adopted templates are never dropped, and failed builds leave no database behind.
"""

from __future__ import annotations

import os

import psycopg2
import pytest
from psycopg2.extensions import make_dsn

from astralplane.database import migrations as m
from astralplane.database.baseline import (
    BaselineCompatibilityState,
    inspect_baseline_compatibility,
)
from astralplane.database.migrations import (
    CURRENT_DATA_PLANE_REVISION,
    MIGRATION_REGISTRY,
    MigrationRunner,
)
from astralplane.database.transaction import PlaneDatabase
from tests.fixtures.migrated_template import (
    SHARED_CURRENT_TEMPLATE_ENV,
    TEMPLATE_SCHEMA,
    DatabaseTemplate,
    LineageTemplates,
    MigratedDatabase,
    bound_clone,
    current_lineages,
    process_lineages,
    session_lineages,
)
from tests.fixtures.pre_split.loader import TEST_DATABASE_ENV
from tests.integration.test_empty_database_startup import (
    historical_074_003_runner,
    lineage_074_004_runner,
)

_INSERT_CHAT = (
    "INSERT INTO chats (id, user_id, title, created_at, updated_at) VALUES (%s, %s, 't', 1, 1)"
)


def _database_names(dsn: str) -> set[str]:
    connection = psycopg2.connect(dsn)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT datname FROM pg_database")
            return {str(row[0]) for row in cursor.fetchall()}
    finally:
        connection.close()


def _chat_ids(database: PlaneDatabase) -> tuple[str, ...]:
    with database.transaction() as transaction:
        rows = transaction.fetch_all("SELECT id FROM chats ORDER BY id")
    return tuple(str(row["id"]) for row in rows)


def _lineage_evidence(clone: MigratedDatabase) -> tuple[dict[str, str], str]:
    with clone.database.transaction() as transaction:
        metadata = {
            str(row["key"]): str(row["value"])
            for row in transaction.fetch_all("SELECT key, value FROM schema_meta ORDER BY key")
        }
        structure = m._schema_structure_digest(
            tuple(transaction.fetch_all(m.CURRENT_SCHEMA_STRUCTURE_QUERY))
        )
    return metadata, structure


def test_clone_is_a_current_catalog_that_the_real_verifier_accepts(
    migrated_template: DatabaseTemplate,
    migrated_clone: MigratedDatabase,
) -> None:
    compatibility = inspect_baseline_compatibility(migrated_clone.database)
    report = MigrationRunner(
        migrated_clone.database,
        revision=CURRENT_DATA_PLANE_REVISION,
        registry=MIGRATION_REGISTRY,
    ).run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)
    with migrated_clone.database.transaction() as transaction:
        location = dict(
            transaction.fetch_one(
                "SELECT current_database() AS database, current_schema() AS schema"
            )
        )

    assert compatibility.state is BaselineCompatibilityState.COMPATIBLE
    assert compatibility.observed_revision == CURRENT_DATA_PLANE_REVISION.schema_revision
    assert not compatibility.missing_required_tables
    assert report.already_current
    assert report.applied_steps == ()
    assert location == {"database": migrated_clone.name, "schema": TEMPLATE_SCHEMA}
    assert migrated_clone.name != migrated_template.name
    assert os.environ[TEST_DATABASE_ENV] == migrated_clone.dsn
    template_dsn = make_dsn(migrated_template.administrator_dsn, dbname=migrated_template.name)
    with pytest.raises(psycopg2.OperationalError, match="not currently accepting connections"):
        psycopg2.connect(template_dsn)


def test_clone_writes_are_invisible_to_every_other_clone(
    migrated_template: DatabaseTemplate,
    migrated_clone: MigratedDatabase,
) -> None:
    with migrated_clone.database.transaction() as transaction:
        transaction.execute(_INSERT_CHAT, ("first-clone-chat", "first-clone-owner"))

    with bound_clone(migrated_template) as second:
        assert os.environ[TEST_DATABASE_ENV] == second.dsn
        assert _chat_ids(second.database) == ()
        with second.database.transaction() as transaction:
            transaction.execute(_INSERT_CHAT, ("second-clone-chat", "second-clone-owner"))
        assert _chat_ids(second.database) == ("second-clone-chat",)
        second_name = second.name

    assert os.environ[TEST_DATABASE_ENV] == migrated_clone.dsn
    assert _chat_ids(migrated_clone.database) == ("first-clone-chat",)
    assert second_name not in _database_names(migrated_clone.dsn)
    with migrated_template.clone() as third:
        assert _chat_ids(third.database) == ()


def test_failed_template_build_leaves_no_database_behind(postgres_administrator_dsn: str) -> None:
    def fail(_database: PlaneDatabase) -> None:
        raise RuntimeError("injected template migration failure")

    before = _database_names(postgres_administrator_dsn)
    with pytest.raises(RuntimeError, match="injected template migration failure"):
        DatabaseTemplate.build(postgres_administrator_dsn, fail)

    assert _database_names(postgres_administrator_dsn) == before


def test_lineage_clones_start_at_their_revision_and_never_share_writes(
    lineage_templates: LineageTemplates,
) -> None:
    template = lineage_templates.template(lineage_074_004_runner)

    assert lineage_templates.template(lineage_074_004_runner) is template
    with bound_clone(template) as first, bound_clone(template) as second:
        for clone in (first, second):
            metadata, _ = _lineage_evidence(clone)
            assert metadata == {
                "astralplane_migration_digest": m.PLANE_SCHEMA_074_004_REGISTRY_DIGEST,
                "revision": "074.004",
            }
            assert (
                lineage_074_004_runner(clone.database)
                .run(expected_revision="074.004")
                .already_current
            )
            with clone.database.transaction() as transaction:
                m._verify_predecessor_plane_schema(transaction, "074.004")
                relation = transaction.fetch_one(
                    "SELECT to_regclass('persistent_assignment') AS relation"
                )
                assert relation["relation"] is None
        with first.database.transaction() as transaction:
            transaction.execute(_INSERT_CHAT, ("lineage-first-chat", "lineage-owner"))
        assert _chat_ids(second.database) == ()
        with second.database.transaction() as transaction:
            transaction.execute(_INSERT_CHAT, ("lineage-second-chat", "lineage-owner"))
        assert _chat_ids(first.database) == ("lineage-first-chat",)
        assert _chat_ids(second.database) == ("lineage-second-chat",)
    with template.clone() as third:
        assert _chat_ids(third.database) == ()


def test_chained_lineage_equals_the_same_lineage_built_from_empty(
    postgres_administrator_dsn: str,
) -> None:
    chained = LineageTemplates(postgres_administrator_dsn)
    direct = LineageTemplates(postgres_administrator_dsn)
    try:
        assert chained.source_for(lineage_074_004_runner) is None
        source = chained.template(historical_074_003_runner)
        assert chained.source_for(lineage_074_004_runner) is source
        assert chained.source_for(historical_074_003_runner) is None
        with chained.template(lineage_074_004_runner).clone() as built_by_chain:
            chain_evidence = _lineage_evidence(built_by_chain)
        with direct.template(lineage_074_004_runner).clone() as built_from_empty:
            empty_evidence = _lineage_evidence(built_from_empty)
    finally:
        chained.close()
        direct.close()

    assert chain_evidence == empty_evidence
    assert chain_evidence[0]["revision"] == "074.004"


def test_adopted_current_template_is_cloned_and_never_dropped(
    postgres_administrator_dsn: str,
    migrated_template: DatabaseTemplate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SHARED_CURRENT_TEMPLATE_ENV, migrated_template.name)
    adopted = process_lineages(postgres_administrator_dsn)

    assert adopted.current().name == migrated_template.name
    assert adopted.current().owned is False
    with pytest.raises(RuntimeError, match="already registered"):
        adopted.adopt_current(migrated_template.name)
    with adopted.current().clone() as clone:
        assert (
            MigrationRunner(
                clone.database,
                revision=CURRENT_DATA_PLANE_REVISION,
                registry=MIGRATION_REGISTRY,
            )
            .run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)
            .already_current
        )
    adopted.close()

    assert migrated_template.name in _database_names(postgres_administrator_dsn)
    monkeypatch.delenv(SHARED_CURRENT_TEMPLATE_ENV)
    unshared = process_lineages(postgres_administrator_dsn)
    assert unshared.source_for(historical_074_003_runner) is None


def test_session_lineages_are_single_per_process_and_probes_never_connect(
    lineage_templates: LineageTemplates,
    postgres_administrator_dsn: str,
) -> None:
    assert current_lineages() is lineage_templates
    with (
        pytest.raises(RuntimeError, match="already open"),
        session_lineages(postgres_administrator_dsn),
    ):
        pass
    assert current_lineages() is lineage_templates
    with pytest.raises(RuntimeError, match="must not touch a database"):
        LineageTemplates.lineage_of(lambda database: database.transaction())
    with pytest.raises(ValueError, match="unexpected identifier"):
        DatabaseTemplate.adopt(postgres_administrator_dsn, "template1")


def test_failed_chained_build_leaves_no_database_behind(
    postgres_administrator_dsn: str,
    migrated_template: DatabaseTemplate,
) -> None:
    def fail(_database: PlaneDatabase) -> None:
        raise RuntimeError("injected chained template failure")

    before = _database_names(postgres_administrator_dsn)
    with pytest.raises(RuntimeError, match="injected chained template failure"):
        DatabaseTemplate.build(postgres_administrator_dsn, fail, source=migrated_template)

    assert _database_names(postgres_administrator_dsn) == before
