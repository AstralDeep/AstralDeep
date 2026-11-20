"""Tests for src/astralplane/database/migrations.py and revision.py: repeatable,
concurrency-safe, compatibility-checked schema upgrades across the declared migration
registry.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from astralplane.database.migrations import (
    Migration,
    MigrationRegistry,
    MigrationRunner,
)
from astralplane.database.revision import DataPlaneRevision
from astralplane.errors import MigrationDefinitionError, SchemaRevisionError


def _checksum(name: str) -> str:
    return hashlib.sha256(name.encode("ascii")).hexdigest()


class FakeTransaction:
    def __init__(
        self,
        metadata: dict[str, str],
        objects: set[str],
        executions: list[str],
    ) -> None:
        self.metadata = metadata
        self.objects = objects
        self.executions = executions

    def fetch_one(self, statement: str, parameters: object = ()) -> dict[str, object]:
        assert statement == "SELECT pg_advisory_xact_lock(%s, %s)"
        assert parameters == (1095980114, 60001)
        self.executions.append("advisory-lock")
        return {"pg_advisory_xact_lock": None}

    def fetch_all(self, statement: str, parameters: object = ()) -> tuple[dict[str, str], ...]:
        assert "FROM schema_meta" in statement
        assert parameters == ("revision", "astralplane_migration_digest")
        self.executions.append("read-metadata")
        return tuple(
            {"key": key, "value": value}
            for key, value in sorted(self.metadata.items())
            if key in parameters
        )

    def execute(self, statement: str, parameters: object = ()) -> SimpleNamespace:
        if statement.startswith("CREATE TABLE IF NOT EXISTS schema_meta"):
            self.executions.append("ensure-metadata")
        elif statement.startswith("INSERT INTO schema_meta"):
            key, value = parameters  # type: ignore[misc]
            self.metadata[str(key)] = str(value)
            self.executions.append(f"write:{key}={value}")
        elif statement == "TEST ADD OBJECT":
            (name,) = parameters  # type: ignore[misc]
            self.objects.add(str(name))
            self.executions.append(f"add:{name}")
        elif statement == "TEST VERIFY OBJECTS":
            required = {str(name) for name in parameters}  # type: ignore[union-attr]
            missing = required.difference(self.objects)
            if missing:
                raise SchemaRevisionError(
                    "current schema structural verification failed",
                    metadata={"missing": ",".join(sorted(missing))},
                )
            self.executions.append("verify:" + ",".join(sorted(required)))
        else:
            raise AssertionError(f"unexpected SQL: {statement}")
        return SimpleNamespace(rowcount=1, status_message="OK", returned_records=())


class FakeDatabase:
    def __init__(
        self,
        *,
        revision: str | None = None,
        digest: str | None = None,
        objects: set[str] | None = None,
    ) -> None:
        self.metadata: dict[str, str] = {}
        if revision is not None:
            self.metadata["revision"] = revision
        if digest is not None:
            self.metadata["astralplane_migration_digest"] = digest
        self.objects = set(objects or ())
        self.executions: list[str] = []
        self.transactions = 0
        self.rollbacks = 0
        self._lock = threading.Lock()
        self.duplicate_metadata = False

    @contextmanager
    def transaction(self, **_: object) -> Iterator[FakeTransaction]:
        with self._lock:
            self.transactions += 1
            working_metadata = dict(self.metadata)
            working_objects = set(self.objects)
            working_executions: list[str] = []
            transaction = FakeTransaction(
                working_metadata,
                working_objects,
                working_executions,
            )
            if self.duplicate_metadata:
                original_fetch_all = transaction.fetch_all

                def duplicate_fetch_all(
                    statement: str, parameters: object = ()
                ) -> tuple[dict[str, str], ...]:
                    rows = original_fetch_all(statement, parameters)
                    return (*rows, {"key": "revision", "value": "065.001"})

                transaction.fetch_all = duplicate_fetch_all  # type: ignore[method-assign]
            try:
                yield transaction
            except BaseException:
                self.rollbacks += 1
                raise
            else:
                self.metadata = working_metadata
                self.objects = working_objects
                self.executions.extend(working_executions)


def _add_object(name: str):
    def operation(transaction: Any) -> None:
        transaction.execute("TEST ADD OBJECT", (name,))

    return operation


def _verify_objects(*names: str):
    def operation(transaction: Any) -> None:
        transaction.execute("TEST VERIFY OBJECTS", names)

    return operation


def _registry() -> MigrationRegistry:
    return MigrationRegistry(
        (
            Migration(
                name="empty-to-065",
                source_revisions=(None,),
                target_revision="065.001",
                checksum=_checksum("empty-to-065"),
                operation=_add_object("base-schema"),
            ),
            Migration(
                name="065-to-066",
                source_revisions=("065.001",),
                target_revision="066.001",
                checksum=_checksum("065-to-066"),
                operation=_add_object("messages-owner-index"),
            ),
        ),
        current_schema_verifier=_verify_objects("base-schema", "messages-owner-index"),
        current_schema_verifier_checksum=_checksum("verify-base-and-owner-index"),
    )


def _runner(database: FakeDatabase, registry: MigrationRegistry | None = None) -> MigrationRunner:
    selected_registry = registry or _registry()
    revision = DataPlaneRevision(
        schema_revision="066.001",
        read_compatible_from=("065.001",),
        migration_digest=selected_registry.digest,
    )
    return MigrationRunner(database, revision=revision, registry=selected_registry)  # type: ignore[arg-type]


def test_empty_database_runs_the_complete_declared_path() -> None:
    database = FakeDatabase()

    report = _runner(database).run(expected_revision="066.001")

    assert report.source_revision is None
    assert report.applied_steps == ("empty-to-065", "065-to-066")
    assert report.target_revision == "066.001"
    assert not report.already_current
    assert database.metadata["revision"] == "066.001"
    assert database.metadata["astralplane_migration_digest"] == report.migration_digest
    assert database.objects == {"base-schema", "messages-owner-index"}
    assert database.executions[0] == "advisory-lock"


def test_representative_legacy_state_is_preserved_during_upgrade() -> None:
    database = FakeDatabase(
        revision="065.001",
        objects={"base-schema", "legacy-owner-record", "legacy-voice-session"},
    )

    report = _runner(database).run(expected_revision="066.001")

    assert report.applied_steps == ("065-to-066",)
    assert database.objects == {
        "base-schema",
        "legacy-owner-record",
        "legacy-voice-session",
        "messages-owner-index",
    }


def test_repeated_run_is_already_current_and_does_not_reapply_steps() -> None:
    database = FakeDatabase()
    runner = _runner(database)
    first = runner.run(expected_revision="066.001")
    add_count = sum(item.startswith("add:") for item in database.executions)

    second = runner.run(expected_revision="066.001")

    assert first.applied_steps
    assert second.already_current
    assert second.applied_steps == ()
    assert sum(item.startswith("add:") for item in database.executions) == add_count


def test_current_metadata_cannot_hide_structural_corruption() -> None:
    database = FakeDatabase()
    runner = _runner(database)
    runner.run(expected_revision="066.001")
    database.objects.remove("messages-owner-index")

    with pytest.raises(SchemaRevisionError, match="structural verification"):
        runner.run(expected_revision="066.001")

    assert database.metadata["revision"] == "066.001"
    assert database.rollbacks == 1


def test_current_marker_without_digest_is_rejected_without_relabeling() -> None:
    database = FakeDatabase(revision="066.001", objects={"base-schema"})

    with pytest.raises(SchemaRevisionError, match="missing migration-set evidence"):
        _runner(database).run(expected_revision="066.001")

    assert database.metadata == {"revision": "066.001"}


def test_predecessor_with_unknown_digest_is_rejected_without_upgrade() -> None:
    database = FakeDatabase(revision="065.001", digest="f" * 64, objects={"legacy"})

    with pytest.raises(SchemaRevisionError, match="unrecognized migration-set digest"):
        _runner(database).run(expected_revision="066.001")

    assert database.metadata == {
        "revision": "065.001",
        "astralplane_migration_digest": "f" * 64,
    }
    assert database.objects == {"legacy"}


def _pinned_predecessor_runner(database: FakeDatabase) -> MigrationRunner:
    registry = MigrationRegistry(
        (
            Migration(
                name="067-to-074",
                source_revisions=("067.001",),
                target_revision="074.001",
                checksum=_checksum("067-to-074"),
                operation=_add_object("authority-schema"),
            ),
        ),
        current_schema_verifier=_verify_objects("authority-schema"),
        current_schema_verifier_checksum=_checksum("verify-authority-schema"),
    )
    revision = DataPlaneRevision(
        schema_revision="074.001",
        read_compatible_from=("067.001",),
        migration_digest=registry.digest,
        accepted_predecessor_digests=(("067.001", _checksum("067-registry")),),
    )
    return MigrationRunner(database, revision=revision, registry=registry)  # type: ignore[arg-type]


def _full_lineage_runner(database: FakeDatabase) -> MigrationRunner:
    registry = MigrationRegistry(
        (
            Migration(
                name="066-to-067",
                source_revisions=("066.001",),
                target_revision="067.001",
                checksum=_checksum("066-to-067"),
                operation=_add_object("recovery-schema"),
            ),
            Migration(
                name="067-to-074",
                source_revisions=("067.001",),
                target_revision="074.001",
                checksum=_checksum("067-to-074"),
                operation=_add_object("authority-schema"),
            ),
        ),
        current_schema_verifier=_verify_objects("recovery-schema", "authority-schema"),
        current_schema_verifier_checksum=_checksum("verify-recovery-and-authority"),
    )
    revision = DataPlaneRevision(
        schema_revision="074.001",
        read_compatible_from=("066.001", "067.001"),
        migration_digest=registry.digest,
        accepted_predecessor_digests=(("067.001", _checksum("067-registry")),),
    )
    return MigrationRunner(database, revision=revision, registry=registry)  # type: ignore[arg-type]


def test_pre_split_066_without_plane_digest_runs_both_current_edges() -> None:
    database = FakeDatabase(revision="066.001", objects={"legacy"})

    report = _full_lineage_runner(database).run(expected_revision="074.001")

    assert report.source_revision == "066.001"
    assert report.applied_steps == ("066-to-067", "067-to-074")
    assert database.objects == {"legacy", "recovery-schema", "authority-schema"}
    assert database.metadata == {
        "revision": "074.001",
        "astralplane_migration_digest": report.migration_digest,
    }


def test_pre_split_066_rejects_an_unrecognized_plane_digest() -> None:
    database = FakeDatabase(revision="066.001", digest="f" * 64, objects={"legacy"})

    with pytest.raises(SchemaRevisionError, match="unrecognized migration-set digest"):
        _full_lineage_runner(database).run(expected_revision="074.001")

    assert database.metadata == {
        "revision": "066.001",
        "astralplane_migration_digest": "f" * 64,
    }
    assert database.objects == {"legacy"}


def test_exact_pinned_predecessor_digest_is_accepted_and_replaced_atomically() -> None:
    database = FakeDatabase(
        revision="067.001",
        digest=_checksum("067-registry"),
        objects={"legacy"},
    )

    report = _pinned_predecessor_runner(database).run(expected_revision="074.001")

    assert report.applied_steps == ("067-to-074",)
    assert database.objects == {"legacy", "authority-schema"}
    assert database.metadata == {
        "revision": "074.001",
        "astralplane_migration_digest": report.migration_digest,
    }


@pytest.mark.parametrize("digest", [None, "f" * 64])
def test_pinned_predecessor_requires_exact_registry_evidence(digest: str | None) -> None:
    database = FakeDatabase(revision="067.001", digest=digest, objects={"legacy"})

    with pytest.raises(SchemaRevisionError, match=r"migration-set evidence|unrecognized"):
        _pinned_predecessor_runner(database).run(expected_revision="074.001")

    expected_metadata = {"revision": "067.001"}
    if digest is not None:
        expected_metadata["astralplane_migration_digest"] = digest
    assert database.metadata == expected_metadata
    assert database.objects == {"legacy"}
    assert database.rollbacks == 1


def test_concurrent_runners_apply_each_step_exactly_once() -> None:
    database = FakeDatabase()
    runner = _runner(database)

    with ThreadPoolExecutor(max_workers=10) as executor:
        reports = list(
            executor.map(
                lambda _: runner.run(expected_revision="066.001"),
                range(10),
            )
        )

    assert sum(not report.already_current for report in reports) == 1
    assert sum(item == "add:base-schema" for item in database.executions) == 1
    assert sum(item == "add:messages-owner-index" for item in database.executions) == 1
    assert database.transactions == 10


def test_partially_applied_repeat_safe_ddl_recovers_from_the_old_marker() -> None:
    database = FakeDatabase(
        revision="065.001",
        objects={"base-schema", "messages-owner-index"},
    )

    report = _runner(database).run(expected_revision="066.001")

    assert report.applied_steps == ("065-to-066",)
    assert database.objects == {"base-schema", "messages-owner-index"}
    assert database.metadata["revision"] == "066.001"


def _speech_backend_075_runner(database: FakeDatabase) -> MigrationRunner:
    registry = MigrationRegistry(
        (
            Migration(
                name="074-004-to-075-001",
                source_revisions=("074.004",),
                target_revision="075.001",
                checksum=_checksum("074-004-to-075-001"),
                operation=_add_object("voice-speech-backend"),
            ),
        ),
        current_schema_verifier=_verify_objects("voice-speech-backend"),
        current_schema_verifier_checksum=_checksum("verify-voice-speech-backend"),
    )
    revision = DataPlaneRevision(
        schema_revision="075.001",
        read_compatible_from=("074.004",),
        migration_digest=registry.digest,
        accepted_predecessor_digests=(("074.004", _checksum("074.004-registry")),),
    )
    return MigrationRunner(database, revision=revision, registry=registry)  # type: ignore[arg-type]


def test_075_requires_the_exact_074_004_registry_digest() -> None:
    database = FakeDatabase(
        revision="074.004",
        digest="f" * 64,
        objects={"legacy-voice-turn"},
    )

    with pytest.raises(SchemaRevisionError, match="unrecognized migration-set digest"):
        _speech_backend_075_runner(database).run(expected_revision="075.001")

    assert database.objects == {"legacy-voice-turn"}
    assert database.metadata["revision"] == "074.004"


def test_075_upgrade_and_repeat_preserve_predecessor_objects() -> None:
    database = FakeDatabase(
        revision="074.004",
        digest=_checksum("074.004-registry"),
        objects={"legacy-voice-turn"},
    )
    runner = _speech_backend_075_runner(database)

    first = runner.run(expected_revision="075.001")
    second = runner.run(expected_revision="075.001")

    assert first.applied_steps == ("074-004-to-075-001",)
    assert second.already_current
    assert database.objects == {"legacy-voice-turn", "voice-speech-backend"}
    assert database.metadata == {
        "revision": "075.001",
        "astralplane_migration_digest": first.migration_digest,
    }


def test_075_forward_recovery_accepts_repeat_safe_partial_ddl() -> None:
    database = FakeDatabase(
        revision="074.004",
        digest=_checksum("074.004-registry"),
        objects={"legacy-voice-turn", "voice-speech-backend"},
    )

    report = _speech_backend_075_runner(database).run(expected_revision="075.001")

    assert report.applied_steps == ("074-004-to-075-001",)
    assert database.objects == {"legacy-voice-turn", "voice-speech-backend"}
    assert database.metadata["revision"] == "075.001"


def test_incompatible_revision_is_rejected_without_mutation() -> None:
    database = FakeDatabase(revision="064.001", objects={"legacy"})
    before = (dict(database.metadata), set(database.objects))

    with pytest.raises(SchemaRevisionError, match="no declared migration path"):
        _runner(database).run(expected_revision="066.001")

    assert (database.metadata, database.objects) == before
    assert database.rollbacks == 1


def test_wrong_current_digest_is_rejected_without_relabeling_schema() -> None:
    database = FakeDatabase(revision="066.001", digest="f" * 64)

    with pytest.raises(SchemaRevisionError, match="different migration-set digest"):
        _runner(database).run(expected_revision="066.001")

    assert database.metadata["astralplane_migration_digest"] == "f" * 64


def test_migration_failure_rolls_back_schema_and_revision_together() -> None:
    def fail_after_write(transaction: Any) -> None:
        transaction.execute("TEST ADD OBJECT", ("must-roll-back",))
        raise RuntimeError("interrupted migration")

    registry = MigrationRegistry(
        (
            Migration(
                name="failing",
                source_revisions=("065.001",),
                target_revision="066.001",
                checksum=_checksum("failing"),
                operation=fail_after_write,
            ),
        ),
        current_schema_verifier=_verify_objects("must-roll-back"),
        current_schema_verifier_checksum=_checksum("verify-must-roll-back"),
    )
    database = FakeDatabase(revision="065.001", objects={"legacy"})

    with pytest.raises(RuntimeError, match="interrupted migration"):
        _runner(database, registry).run(expected_revision="066.001")

    assert database.metadata == {"revision": "065.001"}
    assert database.objects == {"legacy"}
    assert database.rollbacks == 1


def test_expected_revision_mismatch_fails_before_opening_transaction() -> None:
    database = FakeDatabase(revision="065.001")

    with pytest.raises(SchemaRevisionError, match="composition expected"):
        _runner(database).run(expected_revision="067.001")

    assert database.transactions == 0


def test_duplicate_schema_metadata_is_rejected_and_rolled_back() -> None:
    database = FakeDatabase(revision="065.001")
    database.duplicate_metadata = True

    with pytest.raises(SchemaRevisionError, match="duplicate keys"):
        _runner(database).run(expected_revision="066.001")

    assert database.rollbacks == 1


def test_registry_exposes_its_immutable_declared_steps() -> None:
    registry = _registry()
    assert tuple(migration.name for migration in registry.migrations) == (
        "empty-to-065",
        "065-to-066",
    )


def test_runner_rejects_registry_digest_mismatch_at_construction() -> None:
    database = FakeDatabase()
    registry = _registry()
    wrong_revision = DataPlaneRevision(
        schema_revision="066.001",
        read_compatible_from=("065.001",),
        migration_digest="f" * 64,
    )

    with pytest.raises(MigrationDefinitionError, match="does not match"):
        MigrationRunner(database, revision=wrong_revision, registry=registry)  # type: ignore[arg-type]


def test_runner_rejects_a_registry_without_structural_verification() -> None:
    registry = MigrationRegistry(
        (
            Migration(
                name="empty-to-066",
                source_revisions=(None,),
                target_revision="066.001",
                checksum=_checksum("empty-to-066"),
                operation=_add_object("base-schema"),
            ),
        )
    )
    revision = DataPlaneRevision(
        schema_revision="066.001",
        read_compatible_from=("065.001",),
        migration_digest=registry.digest,
    )
    with pytest.raises(MigrationDefinitionError, match="structural verifier"):
        MigrationRunner(FakeDatabase(), revision=revision, registry=registry)  # type: ignore[arg-type]


def test_registry_cycle_is_detected_and_rolled_back() -> None:
    registry = MigrationRegistry(
        (
            Migration(
                name="forward",
                source_revisions=("065.001",),
                target_revision="066.001",
                checksum=_checksum("forward"),
                operation=_add_object("forward"),
            ),
            Migration(
                name="backward",
                source_revisions=("066.001",),
                target_revision="065.001",
                checksum=_checksum("backward"),
                operation=_add_object("backward"),
            ),
        ),
        current_schema_verifier=_verify_objects("never-reached"),
        current_schema_verifier_checksum=_checksum("verify-never-reached"),
    )
    revision = DataPlaneRevision(
        schema_revision="067.001",
        read_compatible_from=("065.001",),
        migration_digest=registry.digest,
    )
    database = FakeDatabase(revision="065.001")
    runner = MigrationRunner(database, revision=revision, registry=registry)  # type: ignore[arg-type]

    with pytest.raises(MigrationDefinitionError, match="cycle"):
        runner.run(expected_revision="067.001")

    assert database.metadata == {"revision": "065.001"}


@pytest.mark.parametrize(
    "factory",
    [
        lambda: MigrationRegistry(()),
        lambda: MigrationRegistry([]),
        lambda: MigrationRegistry(
            (
                Migration("same", (None,), "065.001", _checksum("1"), _add_object("1")),
                Migration("same", ("065.001",), "066.001", _checksum("2"), _add_object("2")),
            )
        ),
        lambda: MigrationRegistry(
            (
                Migration("one", (None,), "065.001", _checksum("1"), _add_object("1")),
                Migration("two", (None,), "066.001", _checksum("2"), _add_object("2")),
            )
        ),
    ],
)
def test_ambiguous_registry_definitions_fail_closed(factory: Any) -> None:
    with pytest.raises(MigrationDefinitionError):
        factory()


@pytest.mark.parametrize(
    "migration",
    [
        ("", (None,), "065.001", _checksum("x"), _add_object("x")),
        ("bad-\N{SNOWMAN}", (None,), "065.001", _checksum("x"), _add_object("x")),
        ("none", (), "065.001", _checksum("x"), _add_object("x")),
        ("list", [None], "065.001", _checksum("x"), _add_object("x")),
        ("duplicate", (None, None), "065.001", _checksum("x"), _add_object("x")),
        ("same", ("065.001",), "065.001", _checksum("x"), _add_object("x")),
        ("checksum", (None,), "065.001", "A" * 64, _add_object("x")),
        ("callable", (None,), "065.001", _checksum("x"), None),
    ],
)
def test_invalid_migration_definitions_are_rejected(migration: tuple[object, ...]) -> None:
    with pytest.raises((MigrationDefinitionError, SchemaRevisionError)):
        Migration(*migration)  # type: ignore[arg-type]


def test_088_007_scheduler_policy_edge_is_pinned_to_the_exact_088_006_registry() -> None:
    import astralplane.database.migrations as canonical
    from astralplane.database.scheduler_policy_schema import SCHEDULER_POLICY_SCHEMA_STATEMENTS

    edge = canonical.PLANE_SCHEMA_088_007_MIGRATION
    assert edge in canonical.MIGRATION_REGISTRY.migrations
    assert canonical.MIGRATION_REGISTRY.migrations[-24] is edge
    assert edge.name == "astralplane-088-scheduler-policy"
    assert edge.source_revisions == ("088.006",)
    assert edge.target_revision == "088.007"
    assert edge.checksum == canonical._statements_checksum(SCHEDULER_POLICY_SCHEMA_STATEMENTS)
    assert len(SCHEDULER_POLICY_SCHEMA_STATEMENTS) == 4
    ddl = "\n".join(SCHEDULER_POLICY_SCHEMA_STATEMENTS)
    assert "CREATE TABLE scheduled_job_policy" in ddl
    assert "CREATE TABLE scheduled_occurrence_assignment" in ddl
    assert "scheduled_occurrence_assignment_unique UNIQUE(occurrence_id, owner_id)" in ddl
    assert "scheduled_job_policy_allowance" in ddl
    assert "REFERENCES persistent_assignment(id, owner_user_id) ON DELETE CASCADE" in ddl
    assert "IF NOT EXISTS" not in ddl
    assert canonical.PLANE_SCHEMA_088_006_REGISTRY_DIGEST == (
        "4f5783e676a12a4009959da2056c2c769418f8c101d67f533223a608158ed73a"
    )
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["088.006"] == (
        "aa8dd06daf08fb11b92dc528c57a2e68072698e437a5b2f83933fdaecebe9f8b",
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("088.006") == (
        canonical.PLANE_SCHEMA_088_006_REGISTRY_DIGEST
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("088.007") == (
        canonical.PLANE_SCHEMA_088_007_REGISTRY_DIGEST
    )
    query_tables = canonical.CURRENT_SCHEMA_STRUCTURE_QUERY
    assert "('scheduled_job_policy')" in query_tables
    assert "('scheduled_occurrence_assignment')" in query_tables
    accepted = dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)
    assert set(accepted) >= {"088.005", "088.006", "088.007"}
    with pytest.raises(SchemaRevisionError):
        canonical._verify_predecessor_plane_schema(_NoStructure(), "999.999")


def test_088_008_framework_credentials_edge_is_pinned_to_the_exact_088_007_registry() -> None:
    import astralplane.database.migrations as canonical
    from astralplane.database.framework_credential_schema import (
        FRAMEWORK_CREDENTIAL_SCHEMA_STATEMENTS,
    )

    edge = canonical.PLANE_SCHEMA_088_008_MIGRATION
    assert edge in canonical.MIGRATION_REGISTRY.migrations
    assert canonical.MIGRATION_REGISTRY.migrations[-23] is edge
    assert edge.name == "astralplane-088-framework-credentials"
    assert edge.source_revisions == ("088.007",)
    assert edge.target_revision == "088.008"
    assert edge.checksum == canonical._statements_checksum(FRAMEWORK_CREDENTIAL_SCHEMA_STATEMENTS)
    ddl = "\n".join(FRAMEWORK_CREDENTIAL_SCHEMA_STATEMENTS)
    assert "CREATE TABLE framework_credential" in ddl
    assert "token_hash TEXT NOT NULL UNIQUE CHECK(token_hash ~ '^[0-9a-f]{64}$')" in ddl
    assert (
        "issuer_kind TEXT NOT NULL CHECK(issuer_kind IN "
        "('session_incarnation','native_credential'))"
    ) in ddl
    assert "framework_credential_allowance CHECK(consumed_admissions <= max_admissions)" in ddl
    assert "ALTER TABLE user_offline_grant ADD COLUMN max_admissions INTEGER" in ddl
    assert "ALTER TABLE user_offline_grant ADD COLUMN consumed_admissions INTEGER" in ddl
    assert "IF NOT EXISTS" not in ddl
    assert canonical.PLANE_SCHEMA_088_007_REGISTRY_DIGEST == (
        "844d9f6629bc422013f84488b10ba6d2862f4805475c63b217639184146fca4d"
    )
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["088.007"] == (
        "eeb9ed13a85e58ce84be7f9d31324e815946b72a8168c67068257c0791e332db",
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("088.007") == (
        canonical.PLANE_SCHEMA_088_007_REGISTRY_DIGEST
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.schema_revision == "089.022"
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["089.001"] == (
        "4123d3bae2d73e369c65ca715ccaf47bdf3560e5ae09a3dc967fe26abd2517f7",
    )
    query_tables = canonical.CURRENT_SCHEMA_STRUCTURE_QUERY
    assert "('framework_credential')" in query_tables
    accepted = dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)
    assert set(accepted) >= {"088.006", "088.007", "089.001"}
    with pytest.raises(SchemaRevisionError):
        canonical._verify_predecessor_plane_schema(_NoStructure(), "999.999")


def test_089_001_typesafe_credential_edge_is_pinned_to_the_exact_088_008_registry() -> None:
    import astralplane.database.migrations as canonical
    from astralplane.database.typesafe_credential_schema import (
        TYPESAFE_CREDENTIAL_SCHEMA_STATEMENTS,
    )

    edge = canonical.PLANE_SCHEMA_089_001_MIGRATION
    assert edge in canonical.MIGRATION_REGISTRY.migrations
    assert canonical.MIGRATION_REGISTRY.migrations[-22] is edge
    assert edge.name == "astralplane-089-typesafe-credentials"
    assert edge.source_revisions == ("088.008",)
    assert edge.target_revision == "089.001"
    assert edge.checksum == canonical._statements_checksum(TYPESAFE_CREDENTIAL_SCHEMA_STATEMENTS)

    ddl = "\n".join(TYPESAFE_CREDENTIAL_SCHEMA_STATEMENTS)
    assert "CREATE TABLE user_typesafe_credential" in ddl
    assert "CREATE TABLE user_data_sharing_acknowledgment" in ddl
    assert "api_key_enc BYTEA NOT NULL" in ddl
    assert "key_fingerprint TEXT NOT NULL CHECK(key_fingerprint ~ '^[0-9a-f]{12}$')" in ddl
    assert (
        "CHECK(last_verification_outcome IN ('unverified','valid','rejected','unavailable'))"
    ) in ddl
    assert "ALTER TABLE" not in ddl
    assert "IF NOT EXISTS" not in ddl
    assert "system_typesafe" not in ddl
    assert "REFERENCES" not in ddl

    assert canonical.PLANE_SCHEMA_088_008_REGISTRY_DIGEST == (
        "4cddbddbc3eed66232f35bc24452451f92aa2b3e90968eb6fd6754f9a64358d8"
    )
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["088.008"] == (
        "c99faec61a4a8b4b362550cb12074aefb7e610e3775fa276daf9d2df1d7cbbe1",
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("088.008") == (
        canonical.PLANE_SCHEMA_088_008_REGISTRY_DIGEST
    )
    query_tables = canonical.CURRENT_SCHEMA_STRUCTURE_QUERY
    assert "('user_typesafe_credential')" in query_tables
    assert "('user_data_sharing_acknowledgment')" in query_tables


def test_089_002_mesh_enrollment_edge_is_pinned_to_the_exact_089_001_registry() -> None:
    import astralplane.database.migrations as canonical
    from astralplane.database.mesh_schema import MESH_SCHEMA_STATEMENTS

    edge = canonical.PLANE_SCHEMA_089_002_MIGRATION
    assert edge in canonical.MIGRATION_REGISTRY.migrations
    assert canonical.MIGRATION_REGISTRY.migrations[-21] is edge
    assert edge.name == "astralplane-089-mesh-enrollment-records"
    assert edge.source_revisions == ("089.001",)
    assert edge.target_revision == "089.002"
    assert edge.checksum == canonical._statements_checksum(MESH_SCHEMA_STATEMENTS)

    ddl = "\n".join(MESH_SCHEMA_STATEMENTS)
    for table in (
        "mesh_record",
        "mesh_member",
        "mesh_public_identity",
        "mesh_enrollment_challenge",
        "mesh_enrollment_invitation",
        "mesh_member_revocation",
    ):
        assert f"CREATE TABLE {table}" in ddl
    assert "member_kind TEXT NOT NULL CHECK(member_kind IN ('device','agent','companion'))" in ddl
    assert "membership_epoch BIGINT NOT NULL CHECK(membership_epoch >= 1)" in ddl
    assert "key_fingerprint TEXT NOT NULL CHECK(key_fingerprint ~ '^[0-9a-f]{64}$')" in ddl
    assert "REFERENCES mesh_member(mesh_id, member_id)" in ddl
    assert "issued_at BIGINT NOT NULL" in ddl
    assert "CHECK(expires_at > issued_at)" in ddl
    assert "ALTER TABLE" not in ddl
    assert "IF NOT EXISTS" not in ddl

    assert canonical.PLANE_SCHEMA_089_001_REGISTRY_DIGEST == (
        "35741bd0de148f836cd8b75b160531013836a61bd46b9e17e7790641412979d8"
    )
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["089.001"] == (
        "4123d3bae2d73e369c65ca715ccaf47bdf3560e5ae09a3dc967fe26abd2517f7",
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.001") == (
        canonical.PLANE_SCHEMA_089_001_REGISTRY_DIGEST
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.schema_revision == "089.022"
    assert canonical.CURRENT_SCHEMA_STRUCTURE_DIGEST == (
        "cb564e2d95f3b58fe9861227bde05e16d56e9852f89a2abdcc825e156b08cbc9"
    )
    query_tables = canonical.CURRENT_SCHEMA_STRUCTURE_QUERY
    for table in (
        "mesh_record",
        "mesh_member",
        "mesh_public_identity",
        "mesh_enrollment_challenge",
        "mesh_enrollment_invitation",
        "mesh_member_revocation",
    ):
        assert f"('{table}')" in query_tables
    accepted = dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)
    assert set(accepted) >= {"088.008", "089.001"}
    with pytest.raises(SchemaRevisionError):
        canonical._verify_predecessor_plane_schema(_NoStructure(), "999.999")


def test_089_004_completion_wake_edge_is_pinned_to_the_exact_089_003_registry() -> None:
    import astralplane.database.migrations as canonical
    from astralplane.database.completion_wake_schema import COMPLETION_WAKE_SCHEMA_STATEMENTS

    edge = canonical.PLANE_SCHEMA_089_004_MIGRATION
    assert edge in canonical.MIGRATION_REGISTRY.migrations
    assert canonical.MIGRATION_REGISTRY.migrations[-19] is edge
    assert edge.name == "astralplane-089-completion-wake"
    assert edge.source_revisions == ("089.003",)
    assert edge.target_revision == "089.004"
    assert edge.checksum == canonical._statements_checksum(COMPLETION_WAKE_SCHEMA_STATEMENTS)

    ddl = "\n".join(COMPLETION_WAKE_SCHEMA_STATEMENTS)
    for table in ("completion_subscription", "wake_receipt"):
        assert f"CREATE TABLE {table}" in ddl
    assert "ALTER TABLE" not in ddl
    assert "IF NOT EXISTS" not in ddl

    assert canonical.PLANE_SCHEMA_089_003_REGISTRY_DIGEST == (
        "773f574efc9657de7692d6ae27352edbb07ef93ed967f4d5ca121ab5c0e1ac7e"
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.003") == (
        canonical.PLANE_SCHEMA_089_003_REGISTRY_DIGEST
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.schema_revision == "089.022"
    assert canonical.CURRENT_SCHEMA_STRUCTURE_DIGEST == (
        "cb564e2d95f3b58fe9861227bde05e16d56e9852f89a2abdcc825e156b08cbc9"
    )
    query_tables = canonical.CURRENT_SCHEMA_STRUCTURE_QUERY
    for table in ("completion_subscription", "wake_receipt"):
        assert f"('{table}')" in query_tables


def test_089_006_atlas_page_edge_is_pinned_to_the_exact_089_005_registry() -> None:
    import astralplane.database.migrations as canonical
    from astralplane.database.atlas_page_schema import ATLAS_PAGE_SCHEMA_STATEMENTS

    edge = canonical.PLANE_SCHEMA_089_006_MIGRATION
    assert edge in canonical.MIGRATION_REGISTRY.migrations
    assert canonical.MIGRATION_REGISTRY.migrations[-17] is edge
    assert edge.name == "astralplane-089-atlas-pages"
    assert edge.source_revisions == ("089.005",)
    assert edge.target_revision == "089.006"
    assert edge.checksum == canonical._statements_checksum(ATLAS_PAGE_SCHEMA_STATEMENTS)

    ddl = "\n".join(ATLAS_PAGE_SCHEMA_STATEMENTS)
    for table in ("atlas_page", "atlas_page_command"):
        assert f"CREATE TABLE {table}" in ddl
    assert "CREATE UNIQUE INDEX atlas_page_live_slug" in ddl
    assert "COALESCE(project_id,'')" in ddl
    assert "atlas_page_lifecycle" in ddl
    assert "atlas_page_command_immutable" in ddl
    assert "ALTER TABLE" not in ddl
    assert "IF NOT EXISTS" not in ddl

    assert canonical.PLANE_SCHEMA_089_005_REGISTRY_DIGEST == (
        "98a9082bb3317192cd6e48df3b63c76dbb9404814cb4a0180eb997f8737e37b0"
    )
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["089.005"] == (
        "2aae4fa6281bda3ca690ce9eb174a5e43b454f0862b0544129bd537eb67bb798",
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.005") == (
        canonical.PLANE_SCHEMA_089_005_REGISTRY_DIGEST
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.schema_revision == "089.022"
    query_tables = canonical.CURRENT_SCHEMA_STRUCTURE_QUERY
    for table in ("atlas_page", "atlas_page_command"):
        assert f"('{table}')" in query_tables


def test_089_007_atlas_revision_edge_is_pinned_to_the_exact_089_006_registry() -> None:
    import astralplane.database.migrations as canonical
    from astralplane.database.atlas_revision_schema import ATLAS_REVISION_SCHEMA_STATEMENTS

    edge = canonical.PLANE_SCHEMA_089_007_MIGRATION
    assert edge in canonical.MIGRATION_REGISTRY.migrations
    assert canonical.MIGRATION_REGISTRY.migrations[-16] is edge
    assert edge.name == "astralplane-089-atlas-revisions"
    assert edge.source_revisions == ("089.006",)
    assert edge.target_revision == "089.007"
    assert edge.checksum == canonical._statements_checksum(ATLAS_REVISION_SCHEMA_STATEMENTS)

    ddl = "\n".join(ATLAS_REVISION_SCHEMA_STATEMENTS)
    assert "CREATE TABLE atlas_page_revision" in ddl
    assert "CREATE INDEX atlas_page_revision_history" in ddl
    assert "atlas_page_revision_immutable" in ddl
    assert "atlas_page_head_revision" in ddl
    assert "atlas_page_command_expected_revision_check" in ddl
    assert "DROP CONSTRAINT atlas_page_command_owner_id_page_id_command_key" in ddl
    assert "INSERT INTO atlas_page_revision" in ddl
    assert "IF NOT EXISTS" not in ddl

    assert canonical.PLANE_SCHEMA_089_006_REGISTRY_DIGEST == (
        "1176ee5a65f74e0447abe2e63bb90808acc40289acd1a6a09ec6a10ae1a967b1"
    )
    assert canonical.PLANE_SCHEMA_089_006_SCHEMA_VERIFIER_CHECKSUM == (
        "6911bade08880589f3a7de5a5654412ce14baa207b451d611fa41a1c27aaabed"
    )
    assert canonical.PLANE_SCHEMA_089_006_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM == (
        "6415846f481719dc158cde3940668df747b7f3b80a49474e0d8ce3cbca640d48"
    )
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["089.006"] == (
        "88896cd0498562ecd0b4d610c4453f7ed01f527e0845cacf7300d88374578e48",
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.006") == (
        canonical.PLANE_SCHEMA_089_006_REGISTRY_DIGEST
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.schema_revision == "089.022"
    query_tables = canonical.CURRENT_SCHEMA_STRUCTURE_QUERY
    assert "('atlas_page_revision')" in query_tables


def test_089_008_atlas_link_edge_is_pinned_to_the_exact_089_007_registry() -> None:
    import astralplane.database.migrations as canonical
    from astralplane.database.atlas_link_schema import ATLAS_LINK_SCHEMA_STATEMENTS

    edge = canonical.PLANE_SCHEMA_089_008_MIGRATION
    assert edge in canonical.MIGRATION_REGISTRY.migrations
    assert canonical.MIGRATION_REGISTRY.migrations[-15] is edge
    assert edge.name == "astralplane-089-atlas-links"
    assert edge.source_revisions == ("089.007",)
    assert edge.target_revision == "089.008"
    assert edge.checksum == canonical._statements_checksum(ATLAS_LINK_SCHEMA_STATEMENTS)

    ddl = "\n".join(ATLAS_LINK_SCHEMA_STATEMENTS)
    for table in ("atlas_page_link", "atlas_page_link_head", "atlas_page_link_command"):
        assert f"CREATE TABLE {table}" in ddl
    assert "CREATE INDEX atlas_page_link_outbound" in ddl
    assert "CREATE INDEX atlas_page_link_backlinks" in ddl
    assert "atlas_page_link_immutable" in ddl
    assert "atlas_page_link_head_advance" in ddl
    assert "atlas_page_link_command_immutable" in ddl
    assert "ALTER TABLE" not in ddl
    assert "IF NOT EXISTS" not in ddl

    assert canonical.PLANE_SCHEMA_089_007_REGISTRY_DIGEST == (
        "fe60420679846e7ea06ca5da3cef250cde5ad75a4ca47b6eb15739893a8ebd09"
    )
    assert canonical.PLANE_SCHEMA_089_007_SCHEMA_VERIFIER_CHECKSUM == (
        "d9260f27198ba19c1c9d79d25b5c5aed04c4c2dcd5dc845823043928ce8c3a7a"
    )
    assert canonical.PLANE_SCHEMA_089_007_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM == (
        "b97a96083497b737bf9051ff132353edd6bf036f43f29e4af76b4c17d6178fd8"
    )
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["089.007"] == (
        "939f224d8a11622964a53b37a63dde6ec37095636145b018486883afe70e9dff",
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.007") == (
        canonical.PLANE_SCHEMA_089_007_REGISTRY_DIGEST
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.schema_revision == "089.022"
    query_tables = canonical.CURRENT_SCHEMA_STRUCTURE_QUERY
    for table in ("atlas_page_link", "atlas_page_link_head", "atlas_page_link_command"):
        assert f"('{table}')" in query_tables


class _NoStructure:
    def fetch_all(self, statement: str, parameters: object = ()) -> tuple[()]:
        raise AssertionError("no predecessor evidence exists for the current revision")

    def fetch_one(self, statement: str, parameters: object = ()) -> None:
        return None

    def execute(self, statement: str, parameters: object = ()) -> None:
        return None


def test_089_009_capability_publication_edge_is_pinned_to_the_exact_089_008_registry() -> None:
    import astralplane.database.migrations as canonical
    from astralplane.database.capability_publication_schema import (
        CAPABILITY_PUBLICATION_SCHEMA_STATEMENTS,
    )

    edge = canonical.PLANE_SCHEMA_089_009_MIGRATION
    assert edge in canonical.MIGRATION_REGISTRY.migrations
    assert canonical.MIGRATION_REGISTRY.migrations[-14] is edge
    assert edge.name == "astralplane-089-capability-publications"
    assert edge.source_revisions == ("089.008",)
    assert edge.target_revision == "089.009"
    assert edge.checksum == canonical._statements_checksum(CAPABILITY_PUBLICATION_SCHEMA_STATEMENTS)

    ddl = "\n".join(CAPABILITY_PUBLICATION_SCHEMA_STATEMENTS)
    assert "CREATE TABLE capability_publication" in ddl
    assert "PRIMARY KEY (owner_id, capability_id, audience_id)" in ddl
    assert "metadata_ciphertext TEXT NOT NULL" in ddl
    assert "metadata_digest TEXT NOT NULL CHECK(metadata_digest ~ '^[0-9a-f]{64}$')" in ddl
    assert "publication_state IN ('published', 'withdrawn', 'expired')" in ddl
    assert "isfinite(published_at) AND isfinite(expires_at) AND expires_at > published_at" in ddl
    assert "CREATE INDEX capability_publication_owner_live" in ddl
    assert "ALTER TABLE" not in ddl
    assert "IF NOT EXISTS" not in ddl
    assert "REFERENCES" not in ddl

    assert canonical.PLANE_SCHEMA_089_008_REGISTRY_DIGEST == (
        "3845856d7280e5ebd5e516135344b660cde8e3cff2a86f2ab50b0d4e0cf7999d"
    )
    assert canonical.PLANE_SCHEMA_089_008_SCHEMA_VERIFIER_CHECKSUM == (
        "a18e07c3dcb6bc449a4b182549e53aa6dce1ff02cce9dcf8e48fe1a7af965388"
    )
    assert canonical.PLANE_SCHEMA_089_008_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM == (
        "9a19be2c191583eb71180aaae46da1e66fa4e4190cd8d5d8f8859a99da3fb899"
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.008") == (
        canonical.PLANE_SCHEMA_089_008_REGISTRY_DIGEST
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.schema_revision == "089.022"
    assert canonical.CURRENT_SCHEMA_STRUCTURE_DIGEST == (
        "cb564e2d95f3b58fe9861227bde05e16d56e9852f89a2abdcc825e156b08cbc9"
    )
    query_tables = canonical.CURRENT_SCHEMA_STRUCTURE_QUERY
    assert "('capability_publication')" in query_tables
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["089.008"] == (
        "880dedb270bb3b151685136edbae32f9b4ee42f35d1286af7a395d5532f02d70",
    )


def test_089_010_harness_binding_edge_is_pinned_to_the_exact_089_009_registry() -> None:
    import astralplane.database.migrations as canonical
    from astralplane.database.harness_binding_schema import (
        HARNESS_BINDING_SCHEMA_STATEMENTS,
    )

    edge = canonical.PLANE_SCHEMA_089_010_MIGRATION
    assert edge in canonical.MIGRATION_REGISTRY.migrations
    assert canonical.MIGRATION_REGISTRY.migrations[-13] is edge
    assert edge.name == "astralplane-089-operation-harness-bindings"
    assert edge.source_revisions == ("089.009",)
    assert edge.target_revision == "089.010"
    assert edge.checksum == canonical._statements_checksum(HARNESS_BINDING_SCHEMA_STATEMENTS)

    ddl = "\n".join(HARNESS_BINDING_SCHEMA_STATEMENTS)
    assert "CREATE TABLE operation_harness_binding" in ddl
    assert "REFERENCES operation_record(operation_id) ON DELETE CASCADE" in ddl
    assert "artifact_digest ~ '^[0-9a-f]{64}$'" in ddl
    assert "CREATE TRIGGER operation_harness_binding_immutable" in ddl
    assert "operation harness bindings are immutable" in ddl
    assert "ALTER TABLE" not in ddl
    assert "IF NOT EXISTS" not in ddl
    assert "('operation_harness_binding')" in canonical.CURRENT_SCHEMA_STRUCTURE_QUERY

    assert canonical.PLANE_SCHEMA_089_009_REGISTRY_DIGEST == (
        "c3e2b3d2d2a2f88a7b995eb6525f6f01df463bad5129a989f7ed70be5e95ed4b"
    )
    assert canonical.PLANE_SCHEMA_089_009_SCHEMA_VERIFIER_CHECKSUM == (
        "b90a854e8d71b8193c352910d49b774b3c3c51bf74c17f50ef750f4e95782498"
    )
    assert canonical.PLANE_SCHEMA_089_009_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM == (
        "004f82523616cb70da25657815e55f04c31520fc57ed5527403771c99ae438bf"
    )
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["089.009"] == (
        "50c157e6451628840bb6b2a7ae245dfbd281a9ef1e3a5248831de4ce776ad3a5",
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.009") == (
        canonical.PLANE_SCHEMA_089_009_REGISTRY_DIGEST
    )
    assert canonical.CURRENT_SCHEMA_STRUCTURE_DIGEST == (
        "cb564e2d95f3b58fe9861227bde05e16d56e9852f89a2abdcc825e156b08cbc9"
    )


def test_089_012_observation_edge_is_pinned_to_the_exact_089_011_registry() -> None:
    import astralplane.database.migrations as canonical
    from astralplane.database.observation_schema import OBSERVATION_SCHEMA_STATEMENTS

    edge = canonical.PLANE_SCHEMA_089_012_MIGRATION
    assert edge in canonical.MIGRATION_REGISTRY.migrations
    assert canonical.MIGRATION_REGISTRY.migrations[-11] is edge
    assert edge.name == "astralplane-089-observation-records"
    assert edge.source_revisions == ("089.011",)
    assert edge.target_revision == "089.012"
    assert edge.checksum == canonical._statements_checksum(OBSERVATION_SCHEMA_STATEMENTS)

    ddl = "\n".join(OBSERVATION_SCHEMA_STATEMENTS)
    for table in ("tool_observation", "tool_observation_page"):
        assert f"CREATE TABLE {table}" in ddl
    assert "CREATE UNIQUE INDEX tool_observation_recall" in ddl
    assert "CREATE INDEX tool_observation_expiry" in ddl
    assert "PRIMARY KEY(owner_id,conversation_id,observation_id)" in ddl
    assert "UNIQUE(owner_id,conversation_id,capture_seq)" in ddl
    assert "tool_observation_retention_window CHECK(" in ddl
    assert "expires_at<=created_at+86400" in ddl
    assert "octet_length(content) BETWEEN 1 AND 16384" in ddl
    assert "observation_octets BETWEEN 0 AND 8388608" in ddl
    assert "tool_observation_lifecycle" in ddl
    assert "tool_observation_delete" in ddl
    assert "tool_observation_page_immutable" in ddl
    assert "tool_observation_page_purge" in ddl
    assert "ALTER TABLE" not in ddl
    assert "IF NOT EXISTS" not in ddl

    assert canonical.PLANE_SCHEMA_089_011_REGISTRY_DIGEST == (
        "8677b4e37ea98a7471ca97a82bc0182aed0f68002cace976ecfaacf8487d04fc"
    )
    assert canonical.PLANE_SCHEMA_089_011_SCHEMA_VERIFIER_CHECKSUM == (
        "a2f00c314f47ff36e9f522db4e332cad4ac7af3d1a82622909a390eed22f0b7a"
    )
    assert canonical.PLANE_SCHEMA_089_011_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM == (
        "136f65bce0a5c757ebc3a035da0d5b7426c750b13d40023dc8999df880bd58d1"
    )
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["089.011"] == (
        "f986f26b756d0611d52007f73888672b191b5426a8404ec5bd7da3152639599d",
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.011") == (
        canonical.PLANE_SCHEMA_089_011_REGISTRY_DIGEST
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.schema_revision == "089.022"
    assert canonical.CURRENT_SCHEMA_STRUCTURE_DIGEST == (
        "cb564e2d95f3b58fe9861227bde05e16d56e9852f89a2abdcc825e156b08cbc9"
    )
    query_tables = canonical.CURRENT_SCHEMA_STRUCTURE_QUERY
    for table in ("tool_observation", "tool_observation_page"):
        assert f"('{table}')" in query_tables


def test_project_edge_preserves_predecessor_identity_and_attests_catalog():
    from astralplane.database import migrations as canonical
    from astralplane.database.project_schema import PROJECT_SCHEMA_STATEMENTS

    edge = canonical.PLANE_SCHEMA_089_013_MIGRATION
    assert canonical.MIGRATION_REGISTRY.migrations[-10] is edge
    assert edge.source_revisions == ("089.012",)
    assert edge.target_revision == "089.013"
    assert edge.checksum == canonical._statements_checksum(PROJECT_SCHEMA_STATEMENTS)
    assert canonical.PLANE_SCHEMA_089_012_REGISTRY_DIGEST == (
        "58ba65b80bf3c1019c6c110d59cc618813668f7916f1621d851e58199738dc9e"
    )
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["089.012"] == (
        "e506006358926677d9dba3261e0a17b4c56815132f747953048f6fcca1e1e38d",
    )
    assert "IF NOT EXISTS" not in "\n".join(PROJECT_SCHEMA_STATEMENTS)


def test_089_016_chat_queue_edge_is_pinned_to_the_exact_089_015_registry():
    import astralplane.database.migrations as canonical
    from astralplane.database.chat_queue_schema import CHAT_QUEUE_SCHEMA_STATEMENTS

    edge = canonical.PLANE_SCHEMA_089_016_MIGRATION
    assert edge in canonical.MIGRATION_REGISTRY.migrations
    assert canonical.MIGRATION_REGISTRY.migrations[-7] is edge
    assert edge.name == "astralplane-089-chat-queue"
    assert edge.source_revisions == ("089.015",)
    assert edge.target_revision == "089.016"
    assert edge.checksum == canonical._statements_checksum(CHAT_QUEUE_SCHEMA_STATEMENTS)

    ddl = "\n".join(CHAT_QUEUE_SCHEMA_STATEMENTS)
    assert "CREATE TABLE chat_queue_item" in ddl
    assert "REFERENCES chats(id) ON DELETE CASCADE" in ddl
    assert "CONSTRAINT chat_queue_item_chat_order UNIQUE(chat_id, position)" in ddl
    assert "DEFERRABLE INITIALLY IMMEDIATE" in ddl
    assert "ALTER TABLE" not in ddl
    assert "IF NOT EXISTS" not in ddl

    assert canonical.PLANE_SCHEMA_089_015_REGISTRY_DIGEST == (
        "d609f1cf1d02c744ff8b35115f8c9be2ebb873a7fa987931c08154d2ecfcb857"
    )
    assert canonical.PLANE_SCHEMA_089_015_SCHEMA_VERIFIER_CHECKSUM == (
        "1f9ec8b14a9896084d983e2b67753fdf7515097d74e916c2315c365c3292a45f"
    )
    assert canonical.PLANE_SCHEMA_089_015_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM == (
        "cd117c845e11e3ee4f7c9bb6f858a2b228e6afa104c5923441e0fabb22c304db"
    )
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["089.015"] == (
        "5a2943f2bc607fa8a9e88d4123abc866348cfe95164f22b87a44fca82c1986af",
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.015") == (
        canonical.PLANE_SCHEMA_089_015_REGISTRY_DIGEST
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.schema_revision == "089.022"
    assert canonical.CURRENT_SCHEMA_STRUCTURE_DIGEST == (
        "cb564e2d95f3b58fe9861227bde05e16d56e9852f89a2abdcc825e156b08cbc9"
    )
    query_tables = canonical.CURRENT_SCHEMA_STRUCTURE_QUERY
    assert "('chat_queue_item')" in query_tables
    with pytest.raises(SchemaRevisionError):
        canonical._verify_predecessor_plane_schema(_NoStructure(), "999.999")


def test_conversation_branch_edge_preserves_predecessor_identity_and_attests_catalog():
    from astralplane.database import migrations as canonical
    from astralplane.database.conversation_branch_schema import (
        CONVERSATION_BRANCH_SCHEMA_STATEMENTS,
    )

    edge = canonical.PLANE_SCHEMA_089_017_MIGRATION
    assert canonical.MIGRATION_REGISTRY.migrations[-6] is edge
    assert edge.name == "astralplane-089-conversation-branches"
    assert edge.source_revisions == ("089.016",)
    assert edge.target_revision == "089.017"
    assert edge.checksum == canonical._statements_checksum(CONVERSATION_BRANCH_SCHEMA_STATEMENTS)
    assert canonical.PLANE_SCHEMA_089_016_REGISTRY_DIGEST == (
        "fd43a7ce895389d9ca951723e7be6f0eb97551d7b7d5e1b0f4897a99ede4ed6c"
    )
    assert canonical.PLANE_SCHEMA_089_016_SCHEMA_VERIFIER_CHECKSUM == (
        "059c16e9ba8c3544e6671e165472ccdb99f425ddc315717f4b3b4650e9dbce64"
    )
    assert canonical.PLANE_SCHEMA_089_016_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM == (
        "b3e0f3867df38951b63923df5f6259a18de963e3984fee0be8802ed03966db33"
    )
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["089.016"] == (
        "075f10522138154d770bc77898a9b43e6fddbdfca3ff0549c0ef77b983b7ea7e",
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.016") == (
        canonical.PLANE_SCHEMA_089_016_REGISTRY_DIGEST
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.schema_revision == "089.022"
    assert canonical.CURRENT_SCHEMA_STRUCTURE_DIGEST == (
        "cb564e2d95f3b58fe9861227bde05e16d56e9852f89a2abdcc825e156b08cbc9"
    )
    ddl = "\n".join(CONVERSATION_BRANCH_SCHEMA_STATEMENTS)
    assert "ALTER TABLE messages ADD COLUMN parent_message_id BIGINT" in ddl
    assert "CREATE TABLE chat_branch_head" in ddl
    assert "REFERENCES chats(user_id, id) ON DELETE CASCADE" in ddl
    assert "REFERENCES messages(id) ON DELETE SET NULL" in ddl
    assert "REFERENCES workspace_snapshot(id) ON DELETE SET NULL" in ddl
    assert "ALTER TABLE chats ADD COLUMN fork_source_chat_id TEXT" in ddl
    assert "ALTER TABLE chats ADD COLUMN fork_source_message_id BIGINT" in ddl
    assert "IF NOT EXISTS" not in ddl
    query_tables = canonical.CURRENT_SCHEMA_STRUCTURE_QUERY
    assert "('chat_branch_head')" in query_tables


def test_089_020_chat_read_receipt_edge_is_pinned_to_the_exact_089_019_registry():
    import astralplane.database.migrations as canonical
    from astralplane.database.chat_read_receipt_schema import (
        CHAT_READ_RECEIPT_SCHEMA_STATEMENTS,
    )

    edge = canonical.PLANE_SCHEMA_089_020_MIGRATION
    assert edge in canonical.MIGRATION_REGISTRY.migrations
    assert canonical.MIGRATION_REGISTRY.migrations[-3] is edge
    assert edge.name == "astralplane-089-chat-read-receipts"
    assert edge.source_revisions == ("089.019",)
    assert edge.target_revision == "089.020"
    assert edge.checksum == canonical._statements_checksum(CHAT_READ_RECEIPT_SCHEMA_STATEMENTS)

    ddl = "\n".join(CHAT_READ_RECEIPT_SCHEMA_STATEMENTS)
    assert "CREATE TABLE chat_read_receipt" in ddl
    assert "REFERENCES chats(id) ON DELETE CASCADE" in ddl
    assert "CONSTRAINT chat_read_receipt_chat_identity PRIMARY KEY(owner_id, chat_id)" in ddl
    assert "ALTER TABLE" not in ddl
    assert "IF NOT EXISTS" not in ddl

    assert canonical.PLANE_SCHEMA_089_019_REGISTRY_DIGEST == (
        "5c81191286b6cad6ef12d3e1005af7577b908f0a1b5ecbe63606c7e34a553746"
    )
    assert canonical.PLANE_SCHEMA_089_019_SCHEMA_VERIFIER_CHECKSUM == (
        "908c445c4722c9dc9c1bc9e30a5beb855701d25e5accbbe78ada05cb11447ea8"
    )
    assert canonical.PLANE_SCHEMA_089_019_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM == (
        "5ca27137697062b307834d4c41ee970dad04ef5058f8c971acf021c99548e7e6"
    )
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["089.019"] == (
        "78774d997f0f9403dd19b1649ef48e230de62bc6c4954416a647e259d11a0203",
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.019") == (
        canonical.PLANE_SCHEMA_089_019_REGISTRY_DIGEST
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.schema_revision == "089.022"
    assert canonical.CURRENT_SCHEMA_STRUCTURE_DIGEST == (
        "cb564e2d95f3b58fe9861227bde05e16d56e9852f89a2abdcc825e156b08cbc9"
    )
    query_tables = canonical.CURRENT_SCHEMA_STRUCTURE_QUERY
    assert "('chat_read_receipt')" in query_tables
    with pytest.raises(SchemaRevisionError):
        canonical._verify_predecessor_plane_schema(_NoStructure(), "999.999")


def test_089_021_mesh_event_edge_is_pinned_to_the_exact_089_020_registry():
    import astralplane.database.migrations as canonical
    from astralplane.database.mesh_event_schema import MESH_EVENT_SCHEMA_STATEMENTS

    edge = canonical.PLANE_SCHEMA_089_021_MIGRATION
    assert edge in canonical.MIGRATION_REGISTRY.migrations
    assert canonical.MIGRATION_REGISTRY.migrations[-2] is edge
    assert edge.name == "astralplane-089-mesh-event-store"
    assert edge.source_revisions == ("089.020",)
    assert edge.target_revision == "089.021"
    assert edge.checksum == canonical._statements_checksum(MESH_EVENT_SCHEMA_STATEMENTS)

    ddl = "\n".join(MESH_EVENT_SCHEMA_STATEMENTS)
    for table in (
        "event_mesh",
        "event_feed",
        "event_entry",
        "event_fork_evidence",
        "event_body",
        "event_roster_entry",
        "event_checkpoint",
        "event_checkpoint_countersignature",
    ):
        assert f"CREATE TABLE {table} (" in ddl
        assert f"('{table}')" in canonical.CURRENT_SCHEMA_STRUCTURE_QUERY
    assert "ALTER TABLE chats ADD COLUMN event_id TEXT" in ddl
    assert "ALTER TABLE messages ADD COLUMN event_id TEXT" in ddl
    assert "IF NOT EXISTS" not in ddl

    assert canonical.PLANE_SCHEMA_089_020_REGISTRY_DIGEST == (
        "82e6c45424227803221853d05c7fed7496de11b1c7025124f1c745a7736a793f"
    )
    assert canonical.PLANE_SCHEMA_089_020_SCHEMA_VERIFIER_CHECKSUM == (
        "d75843ce5c5dbb94710db9686ea044ff187d2db2182ec474c203fceeed08e58f"
    )
    assert canonical.PLANE_SCHEMA_089_020_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM == (
        "3af8aea2946271a51338c96c6db3dc944332d9044168e02665b2fc6d1bcffb60"
    )
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["089.020"] == (
        "07bc6c360942f7890b2c77fbf6a36a4e26f4bf74ee45005f1bee003371d220ef",
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.020") == (
        canonical.PLANE_SCHEMA_089_020_REGISTRY_DIGEST
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.schema_revision == "089.022"
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["089.021"] == (
        "6d5601534ee90bbd5d45916b248cffe1da44f64768a09ce6efd084cb5d199365",
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.021") == (
        canonical.PLANE_SCHEMA_089_021_REGISTRY_DIGEST
    )


def test_089_022_run_and_ledger_edge_is_pinned_to_the_exact_089_021_registry():
    import astralplane.database.migrations as canonical
    from astralplane.database.run_ledger_schema import RUN_LEDGER_SCHEMA_STATEMENTS

    edge = canonical.PLANE_SCHEMA_089_022_MIGRATION
    assert edge in canonical.MIGRATION_REGISTRY.migrations
    assert canonical.MIGRATION_REGISTRY.migrations[-1] is edge
    assert edge.name == "astralplane-089-run-and-learning-ledger"
    assert edge.source_revisions == ("089.021",)
    assert edge.target_revision == "089.022"
    assert edge.checksum == canonical._statements_checksum(RUN_LEDGER_SCHEMA_STATEMENTS)

    ddl = "\n".join(RUN_LEDGER_SCHEMA_STATEMENTS)
    for table in ("run_lease_entry", "run_goal_transition", "learning_ledger_entry"):
        assert f"CREATE TABLE {table} (" in ddl
        assert f"('{table}')" in canonical.CURRENT_SCHEMA_STRUCTURE_QUERY
    assert "ALTER TABLE" not in ddl
    assert "IF NOT EXISTS" not in ddl

    assert canonical.PLANE_SCHEMA_089_021_REGISTRY_DIGEST == (
        "c7bdb010951e3d27b13b9f6fdc7e3d8a4e70ac34478807df12c2727b6b3739e6"
    )
    assert canonical.PLANE_SCHEMA_089_021_SCHEMA_VERIFIER_CHECKSUM == (
        "3dd7fe6a7dbdaa793a34ee6bafdca64cdd9fcf3a66e6985175adf4d06b18b53b"
    )
    assert canonical.PLANE_SCHEMA_089_021_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM == (
        "5eff29adc4c4e8ae9d620c1e49b3bdf049ee6520848a211731f55f2d0ec2b749"
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.schema_revision == "089.022"
    assert canonical.CURRENT_SCHEMA_STRUCTURE_DIGEST == (
        "cb564e2d95f3b58fe9861227bde05e16d56e9852f89a2abdcc825e156b08cbc9"
    )
