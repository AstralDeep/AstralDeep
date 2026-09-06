"""Cross-repository evidence that AstralPlane owns the voice schema contract."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import astralplane
import pytest
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

REPO_ROOT = Path(__file__).resolve().parents[2]
EMBEDDED_PLANE_SOURCE = (REPO_ROOT / "components/AstralPlane/src").resolve()
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


def _require_embedded_plane_source(module_file: str | None) -> Path:
    if module_file is None:
        raise AssertionError("astralplane has no import source")
    resolved = Path(module_file).resolve(strict=True)
    try:
        resolved.relative_to(EMBEDDED_PLANE_SOURCE)
    except ValueError as exc:
        raise AssertionError(
            f"astralplane resolved outside embedded source: {resolved}"
        ) from exc
    return resolved


def test_voice_schema_authority_is_pinned_to_current_plane_evidence() -> None:
    """Deep consumes, but does not recreate, Plane's guarded schema lineage."""

    _require_embedded_plane_source(astralplane.__file__)
    assert CURRENT_DATA_PLANE_REVISION.schema_revision == "079.001"
    assert CURRENT_DATA_PLANE_REVISION.migration_digest == MIGRATION_REGISTRY.digest
    migration_075 = getattr(plane_migrations, "PLANE_SCHEMA_075_MIGRATION", None)
    assert migration_075 is not None
    assert migration_075.source_revisions == ("074.004",)
    assert migration_075.target_revision == "075.001"
    migration_079 = getattr(plane_migrations, "PLANE_SCHEMA_079_MIGRATION", None)
    assert migration_079 is not None
    assert migration_079.source_revisions == ("075.001",)
    assert migration_079.target_revision == "079.001"
    assert migration_079.checksum == (
        "ff1d672518527884cb2cb09eec22aee3f5d2312f9e8dbaaa3d159ba1ce13d55b"
    )
    assert PLANE_SCHEMA_074_004_MIGRATION.target_revision == "074.004"
    assert PLANE_SCHEMA_074_004_MIGRATION.checksum == (
        "c46e2f8ca8060f7ed5ca48da8ac33d2f7078a1b141185d9c843ace66821f01df"
    )
    assert getattr(plane_migrations, "PLANE_SCHEMA_074_004_REGISTRY_DIGEST", None) == (
        "31495e9b916301e5d9d5011f256224e62e0a0822e25fdf3b9c339beb695eff50"
    )
    assert getattr(plane_migrations, "PLANE_SCHEMA_075_REGISTRY_DIGEST", None) == (
        "755faecd45a7d8ca9956f25a239bed476802b885efdce29a36dc3b66981f94df"
    )
    assert MIGRATION_REGISTRY.digest == (
        "2353261227ed72d030ab2426b1a7229c8a1302c669a241dc6b84e3e77e003cad"
    )
    assert CURRENT_SCHEMA_VERIFIER_CHECKSUM == (
        "1987a3e7b27787ef5c4dcc4552e2713b1627b82aaf0760d8ccb881e5a4f30017"
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


def test_imported_astralplane_is_bound_to_embedded_source() -> None:
    source = _require_embedded_plane_source(astralplane.__file__)
    assert source == EMBEDDED_PLANE_SOURCE / "astralplane/__init__.py"
    with pytest.raises(AssertionError, match="outside embedded source"):
        _require_embedded_plane_source(re.__file__)
