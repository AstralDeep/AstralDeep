"""The guarded audit conversation-index edge preserves a populated 089.014 audit chain and
refuses namesake or damaged indexes.
Normal registry recovery, repeat verification and old-reader denial remain authoritative.
"""

import hashlib
import hmac
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from astralplane.repositories.audit import AuditEvent, AuditRepository, verify_segment_proof
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_atlas_pages_upgrade import seed_populated
from tests.integration.test_session_issuer_upgrade import retained_rows

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)
INDEX = "idx_audit_user_conversation_recorded"


def _authenticate(key_id, payload):
    return hmac.new(key_id.encode("utf-8") * 4, payload, hashlib.sha256).digest()


def _seed_audit(tx):
    repository = AuditRepository()
    for number, (owner, conversation) in enumerate(
        (
            ("audit-upgrade-owner", "chat-upgrade-a"),
            ("audit-upgrade-owner", "chat-upgrade-b"),
            ("audit-upgrade-owner", None),
            ("audit-upgrade-foreign", "chat-upgrade-a"),
        ),
        start=1,
    ):
        repository.append(
            tx,
            AuditEvent(
                event_id=f"00000000-0000-4000-8000-{number:012d}",
                chain_id=owner,
                auth_principal=owner,
                agent_id=None,
                event_class="conversation",
                action_type="conversation.message",
                description="Synthetic upgrade event",
                conversation_id=conversation,
                correlation_id=f"00000000-0000-4000-9000-{number:012d}",
                outcome="success",
                outcome_detail=None,
                inputs_json="{}",
                outputs_json="{}",
                artifact_pointers_json="[]",
                started_at=NOW + timedelta(seconds=number),
                completed_at=NOW + timedelta(seconds=number),
                key_id="upgrade-key",
            ),
            _authenticate,
        )


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.014"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.014"),
        current_schema_verifier_checksum=m.PLANE_SCHEMA_089_014_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=m.PLANE_SCHEMA_089_014_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    )
    assert registry.digest == m.PLANE_SCHEMA_089_014_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.014",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            v for v in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from if v < "089.014"
        ),
        accepted_predecessor_digests=tuple(
            p
            for p in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if p[0] < "089.014"
        ),
    )
    return m.MigrationRunner(database, revision=revision, registry=registry)


@pytest.fixture(scope="module")
def predecessor_template(lineage_templates):
    return lineage_templates.template(prior_runner)


@pytest.fixture
def predecessor(predecessor_template):
    with bound_clone(predecessor_template) as clone:
        yield clone


def runner(database, registry=m.MIGRATION_REGISTRY):
    return m.MigrationRunner(database, revision=m.CURRENT_DATA_PLANE_REVISION, registry=registry)


def _index_definition(tx):
    row = tx.fetch_one(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND indexname = %s",
        (INDEX,),
    )
    return None if row is None else row["indexdef"]


def test_populated_upgrade_repeat_and_old_reader_denial(predecessor):
    database = predecessor.database
    with database.transaction() as tx:
        tables = (*seed_populated(tx), "audit_events")
        _seed_audit(tx)
        before = retained_rows(tx, tables)
        assert _index_definition(tx) is None
    report = runner(database).run(expected_revision="089.022")
    assert report.applied_steps == (
        "astralplane-089-audit-conversation-index",
        "astralplane-089-chat-queue",
        "astralplane-089-conversation-branches",
        "astralplane-089-computer-use",
        "astralplane-089-verification-credentials",
        "astralplane-089-chat-read-receipts",
        "astralplane-089-mesh-event-store",
        "astralplane-089-run-and-learning-ledger",
    )
    with database.transaction() as tx:
        assert retained_rows(tx, tables) == before
        assert _index_definition(tx) == (
            f"CREATE INDEX {INDEX} ON {predecessor.schema}.audit_events USING btree "
            "(actor_user_id, conversation_id, recorded_at DESC, event_id DESC) "
            "WHERE (conversation_id IS NOT NULL)"
        )
        page = AuditRepository().list_page(
            tx, owner_id="audit-upgrade-owner", conversation_id="chat-upgrade-a"
        )
        conversation_range = AuditRepository().list_conversation_range(
            tx, owner_id="audit-upgrade-owner", conversation_id="chat-upgrade-a"
        )
        proof = AuditRepository().load_conversation_segment(
            tx, owner_id="audit-upgrade-owner", conversation_id="chat-upgrade-a"
        )
    assert [record.event.event_id for record in page.records] == [
        "00000000-0000-4000-8000-000000000001"
    ]
    assert [record.event.event_id for record in conversation_range.records] == [
        "00000000-0000-4000-8000-000000000001"
    ]
    assert [record.sequence for run in proof.runs for record in run.records] == [1]
    assert verify_segment_proof(proof, authenticate=_authenticate).valid
    assert runner(database).run(expected_revision="089.022").applied_steps == ()
    with pytest.raises(SchemaRevisionError):
        prior_runner(database).run(expected_revision="089.014")


def test_interrupted_edge_rolls_back_then_normal_runner_recovers(predecessor):
    def interrupt(tx):
        m.PLANE_SCHEMA_089_015_MIGRATION.operation(tx)
        raise RuntimeError("synthetic interruption")

    registry = m.MigrationRegistry(
        tuple(
            replace(edge, operation=interrupt) if edge.target_revision == "089.015" else edge
            for edge in m.MIGRATION_REGISTRY.migrations
        ),
        current_schema_verifier=m._verify_current_plane_schema,
        current_schema_verifier_checksum=m.CURRENT_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=m.PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    )
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        runner(predecessor.database, registry).run(expected_revision="089.022")
    with predecessor.database.transaction() as tx:
        assert _index_definition(tx) is None
    assert runner(predecessor.database).run(expected_revision="089.022").applied_steps == (
        "astralplane-089-audit-conversation-index",
        "astralplane-089-chat-queue",
        "astralplane-089-conversation-branches",
        "astralplane-089-computer-use",
        "astralplane-089-verification-credentials",
        "astralplane-089-chat-read-receipts",
        "astralplane-089-mesh-event-store",
        "astralplane-089-run-and-learning-ledger",
    )


@pytest.mark.parametrize(
    "namesake",
    (
        f"CREATE INDEX {INDEX} ON audit_events (actor_user_id, conversation_id)",
        f"CREATE INDEX {INDEX} ON audit_events "
        "(actor_user_id, conversation_id, recorded_at DESC, event_id DESC)",
    ),
)
def test_namesake_predecessor_index_fails_closed(predecessor, namesake):
    with predecessor.database.transaction() as tx:
        tx.execute(namesake)
    with pytest.raises(SchemaRevisionError):
        runner(predecessor.database).run(expected_revision="089.022")


def test_dropped_current_index_fails_closed(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        tx.execute(f"DROP INDEX {INDEX}")
    with pytest.raises(SchemaRevisionError):
        runner(migrated_clone.database).run(expected_revision="089.022")
