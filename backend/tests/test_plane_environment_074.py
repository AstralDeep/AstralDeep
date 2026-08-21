"""Host-configuration tests for the application-scoped Plane runtime."""

from __future__ import annotations

from pathlib import Path

import pytest

import orchestrator.plane_composition as composition
from orchestrator.plane_composition import (
    PlaneCompositionError,
    compose_plane_from_environment,
    resolve_personal_agent_artifact_root,
    resolve_plane_blob_root,
    resolve_plane_database_url,
)


def test_database_url_is_shared_verbatim_when_explicit() -> None:
    value = "postgresql://user:secret@example.invalid:5432/data?sslmode=require"
    assert resolve_plane_database_url({"DATABASE_URL": value}) == value


def test_split_database_configuration_is_escaped_and_ipv6_safe() -> None:
    assert resolve_plane_database_url(
        {
            "DB_HOST": "2001:db8::1",
            "DB_PORT": "5544",
            "DB_NAME": "astral/data",
            "DB_USER": "deep user",
            "DB_PASSWORD": "p@ss/word",
        }
    ) == (
        "postgresql://deep%20user:p%40ss%2Fword@[2001:db8::1]:5544/"
        "astral%2Fdata"
    )


def test_blob_root_is_explicit_absolute_and_defaults_to_backend_tmp(
    tmp_path: Path,
) -> None:
    configured = (tmp_path / "uploads").resolve()
    assert resolve_plane_blob_root({"ATTACHMENT_UPLOAD_ROOT": str(configured)}) == configured
    default = resolve_plane_blob_root({})
    assert default.is_absolute()
    assert default.name == "tmp"
    assert default.parent.name == "backend"

    with pytest.raises(PlaneCompositionError, match="non-empty"):
        resolve_plane_blob_root({"ATTACHMENT_UPLOAD_ROOT": ""})
    with pytest.raises(PlaneCompositionError, match="absolute"):
        resolve_plane_blob_root({"ATTACHMENT_UPLOAD_ROOT": "relative/uploads"})


def test_personal_agent_artifact_root_is_absolute_and_defaults_to_backend_data(
    tmp_path: Path,
) -> None:
    configured = (tmp_path / "personal-agent-artifacts").resolve()
    assert resolve_personal_agent_artifact_root(
        {"PERSONAL_AGENT_ARTIFACT_ROOT": str(configured)}
    ) == configured

    default = resolve_personal_agent_artifact_root({})
    assert default.is_absolute()
    assert default.name == "personal-agent-artifacts"
    assert default.parent.name == "data"
    assert default.parent.parent.name == "backend"

    with pytest.raises(PlaneCompositionError, match="non-empty"):
        resolve_personal_agent_artifact_root(
            {"PERSONAL_AGENT_ARTIFACT_ROOT": ""}
        )
    with pytest.raises(PlaneCompositionError, match="absolute"):
        resolve_personal_agent_artifact_root(
            {"PERSONAL_AGENT_ARTIFACT_ROOT": "relative/artifacts"}
        )


def test_compose_from_environment_binds_one_exact_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expectation = object()
    initialized = object()
    calls: dict[str, object] = {}
    monkeypatch.setattr(composition, "load_plane_expectation", lambda path: expectation)

    def compose(received: object, **values: object) -> object:
        calls["expectation"] = received
        calls.update(values)
        return initialized

    monkeypatch.setattr(composition, "compose_plane_runtime", compose)
    manifest = tmp_path / "composition.json"
    result = compose_plane_from_environment(
        manifest,
        environ={
            "DATABASE_URL": "postgresql://redacted",
            "ATTACHMENT_UPLOAD_ROOT": str((tmp_path / "uploads").resolve()),
            "PERSONAL_AGENT_ARTIFACT_ROOT": str(
                (tmp_path / "personal-agent-artifacts").resolve()
            ),
            "ASTRALPLANE_IDENTITY": "deep-replica-a",
            "DB_POOL_MIN": "1",
            "DB_POOL_MAX": "4",
            "DB_POOL_ACQUIRE_TIMEOUT_SECONDS": "2.5",
            "DB_CONNECT_TIMEOUT_SECONDS": "3",
        },
    )

    assert result is initialized
    assert calls == {
        "expectation": expectation,
        "database_url": "postgresql://redacted",
        "blob_root": (tmp_path / "uploads").resolve(),
        "personal_agent_artifact_root": (
            tmp_path / "personal-agent-artifacts"
        ).resolve(),
        "identity": "deep-replica-a",
        "minimum_connections": 1,
        "maximum_connections": 4,
        "acquire_timeout_seconds": 2.5,
        "connect_timeout_seconds": 3,
    }


@pytest.mark.parametrize(
    "environ",
    (
        {"DATABASE_URL": ""},
        {"DB_PORT": "0"},
        {"DB_POOL_MIN": "4", "DB_POOL_MAX": "3"},
        {"DB_POOL_ACQUIRE_TIMEOUT_SECONDS": "nan"},
        {"ASTRALPLANE_IDENTITY": ""},
        {"PERSONAL_AGENT_ARTIFACT_ROOT": ""},
        {"PERSONAL_AGENT_ARTIFACT_ROOT": "relative/artifacts"},
    ),
)
def test_invalid_plane_environment_fails_without_echoing_values(
    environ: dict[str, str],
    tmp_path: Path,
) -> None:
    with pytest.raises(PlaneCompositionError) as caught:
        compose_plane_from_environment(tmp_path / "composition.json", environ=environ)
    assert "postgresql://" not in str(caught.value)
