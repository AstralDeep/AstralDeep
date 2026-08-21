"""Static guards for feature-074 operational data-plane retirement."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_deep_legacy_database_migration_commands_are_absent() -> None:
    for relative in (
        "backend/scripts/migrate_sqlite_to_postgres.py",
        "backend/scripts/migrate_user_ids.py",
        "backend/scripts/migrate_agent_ownership.py",
    ):
        assert not (REPO_ROOT / relative).exists()


def test_container_startup_preserves_legacy_inputs_and_fails_closed() -> None:
    source = _source("backend/start-docker.sh")
    assert "exit 78" in source
    assert "The files were not modified" in source
    assert "exec python start.py" in source
    for forbidden in ("psycopg", "migrate_sqlite", "migrate_agent_ownership"):
        assert forbidden not in source


def test_operational_surfaces_do_not_open_deep_database_or_execute_sql() -> None:
    paths = (
        "scripts/run_candidate_staging.py",
        "backend/shared/attachment_resolver.py",
        "backend/shared/attachment_materializer.py",
        "backend/agents/general/general_agent.py",
        "backend/agents/remote_compute/remote_compute_agent.py",
    )
    for relative in paths:
        source = _source(relative)
        assert "from shared.database import Database" not in source
        assert "psycopg" not in source
        assert '"psql"' not in source


def test_orchestrator_binds_file_tools_to_the_application_plane() -> None:
    source = _source("backend/orchestrator/orchestrator.py")
    assert "register_database" not in source
    assert "from agents.general.file_tools import register_plane_dependencies" in source
    assert "register_plane_dependencies(" in source
    assert "plane.runtime," in source
    assert "plane.repositories," in source
    assert "plane.blobs," in source


def test_candidate_compose_has_no_deep_owned_schema_loader() -> None:
    source = _source("docker-compose.staging.yml")
    assert "schema-baseline" not in source
    assert "from shared.database import Database" not in source


def test_candidate_deploy_retirement_is_explicit_and_precedes_compose_mutation() -> None:
    source = _source("scripts/run_candidate_staging.py")
    call = source.index("    _require_plane_fixture_importer()", source.index("def _deploy"))
    cleanup = source.index("\ndef _cleanup", call)
    deploy_tail = source[call:cleanup]
    assert "_run(" not in deploy_tail
    assert "_compose(" not in deploy_tail
    assert '"has no AstralPlane-owned exact-fixture import contract. No staging "' in source
    assert '"namespace was created.' in source
