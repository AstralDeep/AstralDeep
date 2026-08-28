"""Cross-repository evidence that AstralPlane owns the voice schema contract."""

from __future__ import annotations

import inspect
import re

from astralplane import create_repository_catalog
from astralplane.database.baseline import BASELINE_REQUIRED_TABLES
from astralplane.database.legacy_baseline_066 import LEGACY_BASELINE_SOURCE_BLOB
from astralplane.database.migrations import (
    CURRENT_DATA_PLANE_REVISION,
    CURRENT_SCHEMA_VERIFIER_CHECKSUM,
    MIGRATION_REGISTRY,
    PLANE_SCHEMA_074_004_MIGRATION,
)
from astralplane.database import migrations as plane_migrations
from astralplane.repositories.voice import VoiceRepository

from orchestrator.voice_sessions import VoiceSessionRepository

_EXPECTED_VOICE_RECORD_OPERATIONS = {
    "abandon_chat_turns",
    "abandon_unaccepted_session_turns",
    "abort_staged_chat_result_commits",
    "chat_exists",
    "clear_foreground_turns",
    "complete_announcement_claim",
    "delete_owned_chat",
    "get_activation_record",
    "get_chat_render_revision",
    "get_client_turn_record",
    "get_live_session_record",
    "get_session_record",
    "get_session_record_for_administration",
    "get_submission_record",
    "get_turn_record",
    "get_turn_record_for_administration",
    "has_turn_in_states",
    "insert_session_record",
    "insert_turn_record",
    "list_chat_session_records_for_update",
    "list_chat_turn_records_for_update",
    "list_expired_session_records_for_administration",
    "list_owned_live_session_records_for_administration",
    "list_renewable_control_session_records_for_administration",
    "list_true_idle_session_records_for_administration",
    "lock_identity",
    "max_client_playout_sequence",
    "patch_session_record",
    "patch_turn_record",
    "reconcile_ended_terminal_operation_turns_for_administration",
    "reconcile_ended_unaccepted_turns_for_administration",
    "release_control_lease_record",
}


def test_voice_schema_authority_is_pinned_to_current_plane_evidence() -> None:
    """Deep consumes, but does not recreate, Plane's guarded schema lineage."""

    assert CURRENT_DATA_PLANE_REVISION.schema_revision == "075.001"
    assert CURRENT_DATA_PLANE_REVISION.migration_digest == MIGRATION_REGISTRY.digest
    migration_075 = getattr(plane_migrations, "PLANE_SCHEMA_075_MIGRATION", None)
    assert migration_075 is not None
    assert migration_075.source_revisions == ("074.004",)
    assert migration_075.target_revision == "075.001"
    assert PLANE_SCHEMA_074_004_MIGRATION.target_revision == "074.004"
    assert PLANE_SCHEMA_074_004_MIGRATION.checksum == (
        "c46e2f8ca8060f7ed5ca48da8ac33d2f7078a1b141185d9c843ace66821f01df"
    )
    assert getattr(plane_migrations, "PLANE_SCHEMA_074_004_REGISTRY_DIGEST", None) == (
        "31495e9b916301e5d9d5011f256224e62e0a0822e25fdf3b9c339beb695eff50"
    )
    assert MIGRATION_REGISTRY.digest == (
        "755faecd45a7d8ca9956f25a239bed476802b885efdce29a36dc3b66981f94df"
    )
    assert CURRENT_SCHEMA_VERIFIER_CHECKSUM == (
        "bc32928ec26f75eec92c632a536cb9853d3e6db6e3fc45c271ea69abde5510fe"
    )
    assert LEGACY_BASELINE_SOURCE_BLOB == "39cdc1d328f17840305b88158a892f5fd09c96dd"
    assert {"voice_session", "voice_turn"} <= BASELINE_REQUIRED_TABLES
    assert isinstance(create_repository_catalog().voice, VoiceRepository)


def test_deep_voice_coordinator_matches_the_plane_catalog_exactly() -> None:
    """Every persistence call in Deep resolves to the pinned typed repository."""

    signature = inspect.signature(VoiceSessionRepository)
    assert tuple(signature.parameters) == (
        "plane_runtime",
        "plane_repositories",
        "uuid_factory",
        "control_lease_ttl_seconds",
    )
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )

    source = inspect.getsource(VoiceSessionRepository)
    operations = set(re.findall(r"self\._voice\.([a-z][a-z0-9_]*)", source))
    assert operations == _EXPECTED_VOICE_RECORD_OPERATIONS
    catalog_voice = create_repository_catalog().voice
    assert not (operations - set(dir(catalog_voice)))
    assert "shared.database" not in source
    assert ".cursor(" not in source
