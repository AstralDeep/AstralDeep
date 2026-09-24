"""Tests that the sanitized candidate-staging fixture corpus is exact, deterministic,
and free of secrets: the manifest contract, legacy agent bundle binding,
representative SQL, and the Keycloak realm's PKCE-only shape.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = (
    REPO_ROOT
    / "backend"
    / "tests"
    / "fixtures"
    / "runtime_reliability_060"
    / "staging"
)
SQL_PATH = FIXTURE_ROOT / "representative-057.sql"
REALM_PATH = FIXTURE_ROOT / "keycloak-realm.json"
MANIFEST_PATH = FIXTURE_ROOT / "fixture-manifest.json"
LEGACY_AGENT_ROOT = FIXTURE_ROOT / "legacy-agent-root"
LEGACY_BUNDLE_DIR = LEGACY_AGENT_ROOT / "synthetic-same-name"
LEGACY_BUNDLE_FILES = {
    "legacy-agent-root/synthetic-same-name/.draft": LEGACY_BUNDLE_DIR / ".draft",
    "legacy-agent-root/synthetic-same-name/__init__.py": (
        LEGACY_BUNDLE_DIR / "__init__.py"
    ),
    "legacy-agent-root/synthetic-same-name/mcp_tools.py": (
        LEGACY_BUNDLE_DIR / "mcp_tools.py"
    ),
}

REPRESENTATIVE_TABLE_COUNTS = {
    "users": 2,
    "chats": 2,
    "messages": 3,
    "saved_components": 2,
    "workspace_snapshot": 1,
    "draft_agents": 2,
    "user_agent": 3,
    "scheduled_job": 2,
    "job_run": 2,
    "background_task": 3,
    "agent_ownership": 3,
    "interaction_log": 3,
}
PKCE_CLIENTS = {
    "astral-frontend",
    "astral-desktop",
    "astral-mobile",
    "astral-watch",
}
NONDETERMINISTIC_SQL = re.compile(
    r"(?i)\b(?:now|current_timestamp|clock_timestamp|statement_timestamp|"
    r"transaction_timestamp|random|gen_random_uuid|uuid_generate_v[0-9]+)\s*\(?"
)
EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@([A-Z0-9.-]+)\.[A-Z]{2,}\b", re.I)
FORBIDDEN_SECRET_TEXT = re.compile(
    r"(?i)(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|"
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|\bsk-[A-Za-z0-9]{20,}\b|"
    r"ai\.uky\.edu)"
)
SECRET_KEY = re.compile(
    r"(?i)(?:secret|password|api[_-]?key|access[_-]?token|refresh[_-]?token)"
)


def _strict_json(path: Path) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r} in {path}")
            result[key] = value
        return result

    def reject_nonfinite(value: str) -> None:
        raise ValueError(f"non-finite value {value!r} in {path}")

    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_nonfinite,
    )
    assert isinstance(value, dict), f"{path} must contain one JSON object"
    return value


@pytest.fixture(scope="module")
def fixture_files() -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    missing = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in (SQL_PATH, REALM_PATH, MANIFEST_PATH, *LEGACY_BUNDLE_FILES.values())
        if not path.is_file()
    ]
    assert not missing, f"T001 must create staging fixtures: {missing}"
    sql_bytes = SQL_PATH.read_bytes()
    return sql_bytes, _strict_json(REALM_PATH), _strict_json(MANIFEST_PATH)


def _walk_json(value: Any, *, path: str = "$") -> list[tuple[str, Any]]:
    walked = [(path, value)]
    if isinstance(value, dict):
        for key, child in value.items():
            walked.extend(_walk_json(child, path=f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            walked.extend(_walk_json(child, path=f"{path}[{index}]"))
    return walked


def _assert_file_record(name: str, content: bytes, record: dict[str, Any]) -> None:
    assert set(record) == {"sha256", "size_bytes"}
    assert record["sha256"] == hashlib.sha256(content).hexdigest(), (
        f"fingerprint drift for {name}"
    )
    assert record["size_bytes"] == len(content), f"size drift for {name}"


def test_fixture_manifest_is_exact_synthetic_sanitized_contract(
    fixture_files: tuple[bytes, dict[str, Any], dict[str, Any]],
) -> None:
    sql_bytes, _, manifest = fixture_files
    assert set(manifest) == {
        "schema_version",
        "source_schema_revision",
        "provenance",
        "sanitization",
        "files",
        "legacy_agent_root",
        "representative_tables",
    }
    assert manifest["schema_version"] == 1
    assert manifest["source_schema_revision"] == "057.001"
    assert manifest["provenance"] == {
        "classification": "synthetic",
        "source": "feature-060",
    }
    assert manifest["sanitization"] == {
        "contains_real_user_data": False,
        "contains_credentials": False,
        "reviewed": True,
    }
    assert set(manifest["files"]) == {
        "representative-057.sql",
        "keycloak-realm.json",
        *LEGACY_BUNDLE_FILES,
    }
    _assert_file_record(
        "representative-057.sql",
        sql_bytes,
        manifest["files"]["representative-057.sql"],
    )
    _assert_file_record(
        "keycloak-realm.json",
        REALM_PATH.read_bytes(),
        manifest["files"]["keycloak-realm.json"],
    )
    for relative_path, path in LEGACY_BUNDLE_FILES.items():
        _assert_file_record(
            relative_path,
            path.read_bytes(),
            manifest["files"][relative_path],
        )


def test_legacy_server_agent_bundle_is_exact_and_unambiguously_bound(
    fixture_files: tuple[bytes, dict[str, Any], dict[str, Any]],
) -> None:
    sql_bytes, _, manifest = fixture_files
    binding = manifest["legacy_agent_root"]
    assert binding == {
        "relative_path": "legacy-agent-root",
        "server_agent_id": "fixture-server-agent",
        "draft_id": "06000000-0000-4000-8000-000000000301",
        "agent_slug": "synthetic-same-name",
    }
    assert LEGACY_AGENT_ROOT.resolve().is_relative_to(FIXTURE_ROOT.resolve())
    assert {
        path.relative_to(FIXTURE_ROOT).as_posix()
        for path in LEGACY_BUNDLE_DIR.iterdir()
    } == set(LEGACY_BUNDLE_FILES)
    assert not any(path.is_symlink() for path in LEGACY_BUNDLE_FILES.values())
    assert LEGACY_BUNDLE_FILES[
        "legacy-agent-root/synthetic-same-name/.draft"
    ].read_text(encoding="utf-8") == f"{binding['draft_id']}\n"
    compile(
        LEGACY_BUNDLE_FILES[
            "legacy-agent-root/synthetic-same-name/mcp_tools.py"
        ].read_text(encoding="utf-8"),
        "mcp_tools.py",
        "exec",
    )

    sql = sql_bytes.decode("utf-8")
    assert binding["server_agent_id"] in sql
    assert binding["draft_id"] in sql
    assert sql.count(binding["agent_slug"]) == 2
    assert "fixture-host-agent" in sql
    assert "fixture-desktop-host" in sql


def test_representative_sql_is_repeatable_synthetic_057_state(
    fixture_files: tuple[bytes, dict[str, Any], dict[str, Any]],
) -> None:
    sql_bytes, _, manifest = fixture_files
    sql = sql_bytes.decode("utf-8")
    assert sql.endswith("\n"), "tracked SQL must end with one text newline"
    assert "057.001" in sql
    assert re.search(r"(?im)^\s*BEGIN\s*;", sql)
    assert re.search(r"(?im)^\s*COMMIT\s*;", sql)
    assert NONDETERMINISTIC_SQL.search(sql) is None
    assert "${" not in sql and "{{" not in sql

    table_counts = manifest["representative_tables"]
    assert table_counts == REPRESENTATIVE_TABLE_COUNTS
    for table, expected_count in REPRESENTATIVE_TABLE_COUNTS.items():
        assert expected_count >= 1
        assert re.search(
            rf"(?i)\bINSERT\s+INTO\s+(?:public\.)?{re.escape(table)}\b", sql
        ), f"fixture declares {table!r} but does not insert it"


def test_keycloak_realm_is_public_pkce_only_and_contains_no_users(
    fixture_files: tuple[bytes, dict[str, Any], dict[str, Any]],
) -> None:
    _, realm, _ = fixture_files
    assert realm.get("realm") == "Astral-060-Staging"
    assert realm.get("enabled") is True
    assert realm.get("users", []) == []
    clients = realm.get("clients")
    assert isinstance(clients, list) and clients
    by_id = {client.get("clientId"): client for client in clients}
    assert PKCE_CLIENTS <= set(by_id)

    for client_id in PKCE_CLIENTS:
        client = by_id[client_id]
        assert client.get("publicClient") is True
        assert client.get("standardFlowEnabled") is True
        assert client.get("directAccessGrantsEnabled") is False
        assert client.get("serviceAccountsEnabled") is False
        assert client.get("authorizationServicesEnabled", False) is False
        assert client.get("attributes", {}).get("pkce.code.challenge.method") == "S256"
        assert not client.get("secret")
        redirect_uris = client.get("redirectUris")
        assert isinstance(redirect_uris, list) and redirect_uris
        for uri in redirect_uris:
            parsed = urlsplit(uri)
            assert parsed.scheme
            assert parsed.username is None and parsed.password is None
            assert not parsed.query and not parsed.fragment


def test_fixture_corpus_contains_no_secret_or_real_identity_markers(
    fixture_files: tuple[bytes, dict[str, Any], dict[str, Any]],
) -> None:
    sql_bytes, realm, manifest = fixture_files
    corpus = "\n".join(
        [
            sql_bytes.decode("utf-8"),
            json.dumps(realm, sort_keys=True),
            json.dumps(manifest, sort_keys=True),
            *(path.read_text(encoding="utf-8") for path in LEGACY_BUNDLE_FILES.values()),
        ]
    )
    assert FORBIDDEN_SECRET_TEXT.search(corpus) is None
    for match in EMAIL.finditer(corpus):
        assert match.group(1).lower() in {"example.invalid", "example.test"}

    for path, value in _walk_json(realm):
        key = path.rsplit(".", 1)[-1]
        if SECRET_KEY.search(key):
            assert value in (None, "", [], {}), f"secret-bearing realm value at {path}"


def test_manifest_fingerprint_detects_fixture_tampering(
    fixture_files: tuple[bytes, dict[str, Any], dict[str, Any]],
) -> None:
    sql_bytes, _, manifest = fixture_files
    with pytest.raises(AssertionError, match="fingerprint drift"):
        _assert_file_record(
            "representative-057.sql",
            sql_bytes + b"-- tampered\n",
            manifest["files"]["representative-057.sql"],
        )
