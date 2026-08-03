#!/usr/bin/env python3
"""Deploy and clean one fail-closed, request-scoped feature-060 staging stack."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import importlib.util
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = REPO_ROOT / "docker-compose.staging.yml"
LIVEKIT_STAGING_CONFIG_PATH = REPO_ROOT / "deploy/livekit/livekit.staging.yaml"
TRUST_SCHEMA_PATH = (
    REPO_ROOT
    / "specs/060-runtime-reliability-hardening/contracts/release-trust.schema.json"
)
WORKFLOW_PATH_RE = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/\.github/workflows/[A-Za-z0-9_.-]+$"
)
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ARTIFACT_ID_RE = re.compile(r"^[1-9][0-9]*$")
STAGE_OUTPUTS_ARTIFACT_NAME_RE = re.compile(
    r"^stage-outputs-[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
)
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_RE = re.compile(
    r"^[A-Za-z0-9.-]+(?::[0-9]{1,5})?/[A-Za-z0-9._/-]+"
    r"(?::[A-Za-z0-9_][A-Za-z0-9._-]{0,127})?@sha256:[0-9a-f]{64}$"
)
ENVIRONMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
SECRET_KEY_RE = re.compile(
    r"(?i)(?:secret|password|api[_-]?key|access[_-]?token|refresh[_-]?token)"
)
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_READY_BYTES = 4 * 1024
SPEECH_PROFILE = {
    "asr_model": "Systran/faster-whisper-large-v3",
    "tts_model": "speaches-ai/Kokoro-82M-v1.0-ONNX",
    "voice": "af_heart",
    "sample_rate_hz": 24000,
}
SPEECH_PROFILE_SHA256 = hashlib.sha256(
    json.dumps(
        SPEECH_PROFILE,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
).hexdigest()
PINNED_LIVEKIT_IMAGE = (
    "livekit/livekit-server:v1.13.5@sha256:"
    "3497163e15c48fef6e7830c78716f9e9d5edc28abf7aa90b61c86e93bbc306b1"
)
LIVEKIT_TURN_TLS_PUBLIC_PORT = 443
LIVEKIT_TURN_TLS_TERMINATOR_UPSTREAM_HOST = "127.0.0.1"
LIVEKIT_TURN_TLS_TERMINATOR_UPSTREAM_PORT = 15349
LIVEKIT_TURN_TLS_LISTENER_PORT = 5349
COMPOSE_OUTPUT_MAX_BYTES = 4 * 1024 * 1024
POSITIVE_DECIMAL_RE = re.compile(r"^[1-9][0-9]*$")
STAGING_OWNERSHIP_LABEL_PREFIX = "com.astraldeep.staging"
STAGING_OWNERSHIP_LABEL_KEYS = {
    "managed": f"{STAGING_OWNERSHIP_LABEL_PREFIX}.managed",
    "project": f"{STAGING_OWNERSHIP_LABEL_PREFIX}.project",
    "environment_id": f"{STAGING_OWNERSHIP_LABEL_PREFIX}.environment-id",
    "run_id": f"{STAGING_OWNERSHIP_LABEL_PREFIX}.run-id",
    "run_attempt": f"{STAGING_OWNERSHIP_LABEL_PREFIX}.run-attempt",
}
STAGING_COMPOSE_SERVICES = frozenset(
    {
        "postgres",
        "keycloak-postgres",
        "keycloak",
        "livekit",
        "schema-baseline",
        "astraldeep",
        "voice-worker",
    }
)
STAGING_COMPOSE_VOLUMES = frozenset({"product-postgres", "keycloak-postgres"})
STAGING_RUNNING_SERVICES = STAGING_COMPOSE_SERVICES - {"schema-baseline"}


class StagingError(ValueError):
    """Raised when candidate staging cannot satisfy its qualifying contract."""


def _strict_json(path: Path) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise StagingError(f"duplicate JSON key {key!r} in {path}")
            result[key] = value
        return result

    def nonfinite(value: str) -> None:
        raise StagingError(f"non-finite JSON value {value!r} in {path}")

    try:
        size = path.stat().st_size
        if size <= 0 or size > MAX_JSON_BYTES:
            raise StagingError(f"JSON size is invalid for {path}")
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=pairs,
            parse_constant=nonfinite,
        )
    except StagingError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StagingError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StagingError(f"{path} must contain one JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise StagingError(f"cannot hash fixture {path}: {exc}") from exc
    return digest.hexdigest()


def _assert_no_secret_values(value: Any, *, location: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if SECRET_KEY_RE.search(key) and child not in (None, "", [], {}):
                raise StagingError(f"fixture contains a secret-bearing value at {location}.{key}")
            _assert_no_secret_values(child, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_secret_values(child, location=f"{location}[{index}]")


def validate_fixtures(manifest_path: str | Path) -> dict[str, Any]:
    """Validate the tracked synthetic 057 fixture and return public fingerprints."""

    manifest_path = Path(manifest_path).resolve(strict=True)
    manifest = _strict_json(manifest_path)
    root = manifest_path.parent
    if manifest.get("schema_version") != 1:
        raise StagingError("fixture manifest schema version is unsupported")
    if manifest.get("source_schema_revision") != "057.001":
        raise StagingError("fixture source schema revision must be 057.001")
    if manifest.get("provenance") != {
        "classification": "synthetic",
        "source": "feature-060",
    }:
        raise StagingError("fixture provenance is not the reviewed synthetic source")
    if manifest.get("sanitization") != {
        "contains_real_user_data": False,
        "contains_credentials": False,
        "reviewed": True,
    }:
        raise StagingError("fixture sanitization contract is incomplete")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise StagingError("fixture manifest has no file fingerprints")
    for relative, record in files.items():
        if (
            not isinstance(relative, str)
            or relative.startswith("/")
            or ".." in Path(relative).parts
            or not isinstance(record, dict)
            or set(record) != {"sha256", "size_bytes"}
        ):
            raise StagingError(f"fixture manifest entry is invalid: {relative!r}")
        path = (root / relative).resolve(strict=True)
        if not path.is_file() or not path.is_relative_to(root.resolve()):
            # A diagnostic copied manifest may point through a symlink; retain
            # fingerprint checking but never accept it for deployment below.
            if manifest_path.parent == (
                REPO_ROOT
                / "backend/tests/fixtures/runtime_reliability_060/staging"
            ).resolve():
                raise StagingError(f"tracked fixture escapes its root: {relative}")
        actual_digest = _sha256(path)
        actual_size = path.stat().st_size
        if actual_digest != record.get("sha256") or actual_size != record.get(
            "size_bytes"
        ):
            raise StagingError(f"fixture fingerprint drift: {relative}")

    sql_path = root / "representative-057.sql"
    realm_path = root / "keycloak-realm.json"
    try:
        sql = sql_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StagingError(f"cannot read representative SQL: {exc}") from exc
    if (
        "requires schema revision 057.001" not in sql
        or "BEGIN;" not in sql
        or "COMMIT;" not in sql
        or re.search(r"(?i)\b(?:password|access_token|refresh_token)\b", sql)
    ):
        raise StagingError("representative SQL is not a sanitized 057 transaction")
    realm = _strict_json(realm_path)
    if realm.get("users", []) != []:
        raise StagingError("tracked Keycloak realm must contain no runtime users")
    clients = realm.get("clients")
    if not isinstance(clients, list) or not clients:
        raise StagingError("tracked Keycloak realm has no public PKCE clients")
    for client in clients:
        if not isinstance(client, dict):
            raise StagingError("Keycloak client fixture is malformed")
        if client.get("publicClient") is not True or client.get("secret"):
            raise StagingError("Keycloak fixture contains a confidential client")
    _assert_no_secret_values(realm)
    return {
        "source_schema_revision": "057.001",
        "synthetic": True,
        "contains_credentials": False,
        "fixture_manifest_sha256": _sha256(manifest_path),
        "representative_dataset_sha256": _sha256(sql_path),
        "keycloak_realm_sha256": _sha256(realm_path),
    }


def validate_endpoint(endpoint: str) -> str:
    """Validate one archived non-loopback HTTPS staging endpoint."""

    try:
        parsed = urlsplit(endpoint)
    except ValueError as exc:
        raise StagingError(f"staging endpoint is malformed: {exc}") from exc
    if parsed.scheme != "https":
        raise StagingError("staging endpoint must use HTTPS")
    if not parsed.hostname:
        raise StagingError("staging endpoint has no host")
    if parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}:
        raise StagingError("staging endpoint cannot use a loopback host")
    if parsed.username is not None or parsed.password is not None:
        raise StagingError("staging endpoint cannot contain userinfo")
    if parsed.query:
        raise StagingError("staging endpoint cannot contain a query")
    if parsed.fragment:
        raise StagingError("staging endpoint cannot contain a fragment")
    if any(ord(character) <= 32 for character in endpoint):
        raise StagingError("staging endpoint contains whitespace/control bytes")
    return endpoint.rstrip("/")


def validate_image_reference(reference: str) -> str:
    """Require a registry image reference pinned by lowercase SHA-256 digest."""

    if not IMAGE_RE.fullmatch(reference):
        raise StagingError(f"image is not digest-qualified: {reference}")
    return reference


def _validate_sha256_input(value: str, *, option: str) -> str:
    if not SHA256_RE.fullmatch(value):
        raise StagingError(f"{option} must be one lowercase SHA-256 digest")
    return value


def _voice_runtime(args: argparse.Namespace) -> dict[str, Any]:
    """Build the public, pinned voice identity from explicit protected inputs."""

    voice_worker_image = validate_image_reference(args.voice_worker_image)
    livekit_image = validate_image_reference(args.livekit_image)
    if livekit_image != PINNED_LIVEKIT_IMAGE:
        raise StagingError("livekit-image differs from the approved immutable server image")
    livekit_config_sha256 = _validate_sha256_input(
        args.livekit_config_sha256, option="livekit-config-sha256"
    )
    inventory_sha256 = _validate_sha256_input(
        args.speech_inventory_sha256, option="speech-inventory-sha256"
    )
    profile_sha256 = _validate_sha256_input(
        args.speech_profile_sha256, option="speech-profile-sha256"
    )
    if profile_sha256 != SPEECH_PROFILE_SHA256:
        raise StagingError(
            "speech-profile-sha256 differs from the exact launch speech profile"
        )
    return {
        "voice_worker_image_reference": voice_worker_image,
        "voice_worker_image_sha256": voice_worker_image.rsplit("@sha256:", 1)[1],
        "livekit_image_reference": livekit_image,
        "livekit_image_sha256": livekit_image.rsplit("@sha256:", 1)[1],
        "livekit_config_sha256": livekit_config_sha256,
        "speech_profile": {
            **SPEECH_PROFILE,
            "inventory_sha256": inventory_sha256,
            "profile_sha256": profile_sha256,
        },
    }


def _project_name(environment_id: str) -> str:
    if not ENVIRONMENT_RE.fullmatch(environment_id):
        raise StagingError("environment-id is not a bounded deployment identity")
    normalized = re.sub(r"[^a-z0-9_-]", "-", environment_id.lower())
    return f"astral060-{normalized}"[:63]


def _run_scoped_environment_id(protected: Mapping[str, str]) -> str:
    """Return the only staging identity that the current protected run may own."""

    run_id = protected["GITHUB_RUN_ID"]
    run_attempt = protected["GITHUB_RUN_ATTEMPT"]
    if not POSITIVE_DECIMAL_RE.fullmatch(run_id):
        raise StagingError("GITHUB_RUN_ID must be one positive decimal identifier")
    if not POSITIVE_DECIMAL_RE.fullmatch(run_attempt):
        raise StagingError("GITHUB_RUN_ATTEMPT must be one positive decimal identifier")
    environment_id = f"rr-{run_id}-{run_attempt}"
    # This identity is deliberately canonical: no normalization or truncation
    # may make two protected runs share a Compose namespace.
    if _project_name(environment_id) != f"astral060-{environment_id}":
        raise StagingError("protected run identity cannot form an exact Compose project")
    return environment_id


def _staging_ownership_labels(
    *,
    project: str,
    environment_id: str,
    run_id: str,
    run_attempt: str,
) -> dict[str, str]:
    """Build the non-secret ownership labels required on every staged resource."""

    return {
        STAGING_OWNERSHIP_LABEL_KEYS["managed"]: "true",
        STAGING_OWNERSHIP_LABEL_KEYS["project"]: project,
        STAGING_OWNERSHIP_LABEL_KEYS["environment_id"]: environment_id,
        STAGING_OWNERSHIP_LABEL_KEYS["run_id"]: run_id,
        STAGING_OWNERSHIP_LABEL_KEYS["run_attempt"]: run_attempt,
    }


def _required_environment(*, for_deploy: bool = True) -> dict[str, str]:
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get(
        "ASTRAL_STAGING_RUNNER_TRUSTED"
    ) != "true":
        raise StagingError("deploy/cleanup requires the configured trusted staging runner")
    names = [
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_JOB",
        "RUNNER_NAME",
        "ASTRAL_STAGING_EXPECTED_RUNNER_NAME",
    ]
    if for_deploy:
        names.extend(
            (
                "ASTRAL_STAGING_ENDPOINT",
                "ASTRAL_STAGING_PROBE_TOKEN",
                "STAGING_POSTGRES_IMAGE",
                "STAGING_KEYCLOAK_IMAGE",
                "STAGING_SCHEMA_BASELINE_IMAGE",
                "STAGING_RUNTIME_ENV_FILE",
                "STAGING_DB_USER",
                "STAGING_DB_PASSWORD",
                "STAGING_DB_NAME",
                "STAGING_KEYCLOAK_DB_USER",
                "STAGING_KEYCLOAK_DB_PASSWORD",
                "STAGING_KEYCLOAK_DB_NAME",
                "STAGING_KEYCLOAK_ADMIN_USER",
                "STAGING_KEYCLOAK_ADMIN_PASSWORD",
                "STAGING_BIND_PORT",
                "LIVEKIT_PUBLIC_URL",
                "LIVEKIT_API_KEY",
                "LIVEKIT_API_SECRET",
                "LIVEKIT_TURN_DOMAIN",
                "VOICE_CONTROL_SECRET",
                "OPENAI_BASE_URL",
                "OPENAI_API_KEY",
            )
        )
    values = {name: os.environ.get(name, "") for name in names}
    missing = [name for name, value in values.items() if not value.strip()]
    if missing:
        raise StagingError(f"required protected staging input is absent: {', '.join(missing)}")
    if values["RUNNER_NAME"] != values["ASTRAL_STAGING_EXPECTED_RUNNER_NAME"]:
        raise StagingError("protected staging job is running on the wrong runner")
    if for_deploy:
        for name in (
            "STAGING_POSTGRES_IMAGE",
            "STAGING_KEYCLOAK_IMAGE",
            "STAGING_SCHEMA_BASELINE_IMAGE",
        ):
            validate_image_reference(values[name])
        runtime_env = Path(values["STAGING_RUNTIME_ENV_FILE"])
        if not runtime_env.is_absolute() or not runtime_env.is_file():
            raise StagingError(
                "STAGING_RUNTIME_ENV_FILE must be an existing absolute protected file"
            )
        try:
            mode = runtime_env.stat().st_mode & 0o777
        except OSError as exc:
            raise StagingError(
                f"cannot stat protected runtime environment file: {exc}"
            ) from exc
        if mode & 0o077:
            raise StagingError(
                "protected runtime environment file must not be group/world accessible"
            )
        validate_endpoint(values["ASTRAL_STAGING_ENDPOINT"])
        public_url = urlsplit(values["LIVEKIT_PUBLIC_URL"])
        if (
            public_url.scheme != "wss"
            or not public_url.hostname
            or public_url.username is not None
            or public_url.password is not None
            or public_url.query
            or public_url.fragment
            or public_url.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
        ):
            raise StagingError("LIVEKIT_PUBLIC_URL must be credential-free non-loopback WSS")
        speech_url = urlsplit(values["OPENAI_BASE_URL"].rstrip("/"))
        if (
            speech_url.scheme != "https"
            or not speech_url.hostname
            or speech_url.username is not None
            or speech_url.password is not None
            or speech_url.query
            or speech_url.fragment
        ):
            raise StagingError("OPENAI_BASE_URL must be credential-free HTTPS")
        if not re.fullmatch(
            r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
            r"[A-Za-z]{2,63}",
            values["LIVEKIT_TURN_DOMAIN"],
        ):
            raise StagingError("LIVEKIT_TURN_DOMAIN must be a public DNS hostname")
        for name in (
            "LIVEKIT_API_KEY",
            "LIVEKIT_API_SECRET",
            "VOICE_CONTROL_SECRET",
            "OPENAI_API_KEY",
        ):
            if any(ord(character) <= 32 for character in values[name]):
                raise StagingError(f"{name} contains whitespace/control bytes")
    return values


def _run(
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str],
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            list(arguments),
            cwd=REPO_ROOT,
            env=dict(environment),
            input=input_bytes,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", b"")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        message = str(stderr).strip()
        for key, value in environment.items():
            if value and SECRET_KEY_RE.search(key):
                message = message.replace(value, "<redacted>")
        raise StagingError(
            f"command failed without producing staging evidence: {message}"
        ) from exc


def _compose(environment: Mapping[str, str], project: str, *arguments: str) -> list[str]:
    del environment
    return [
        "docker",
        "compose",
        "--file",
        str(COMPOSE_PATH),
        "--project-name",
        project,
        *arguments,
    ]


def _git_identity(candidate_sha: str, source_root: str | Path) -> None:
    if not GIT_SHA_RE.fullmatch(candidate_sha):
        raise StagingError("candidate-sha must be one lowercase 40-character Git SHA")
    try:
        source = Path(source_root).resolve(strict=True)
    except OSError as exc:
        raise StagingError("candidate source root does not exist") from exc
    if not source.is_dir():
        raise StagingError("candidate source root is not a directory")
    actual = _run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], environment=os.environ
    ).stdout.decode("ascii").strip()
    if actual != candidate_sha:
        raise StagingError(f"checked-out source {actual} differs from candidate {candidate_sha}")
    dirty = _run(
        [
            "git",
            "-C",
            str(source),
            "status",
            "--porcelain",
            "--untracked-files=all",
        ],
        environment=os.environ,
    ).stdout
    if dirty:
        raise StagingError("qualifying candidate staging requires a clean checkout")


def _strict_json_bytes(payload: bytes, *, purpose: str) -> dict[str, Any]:
    """Parse one bounded strict JSON object without reflecting its content."""

    if not payload or len(payload) > MAX_JSON_BYTES:
        raise StagingError(f"{purpose} response is absent or oversized")

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise StagingError(f"{purpose} response contains duplicate JSON keys")
            result[key] = value
        return result

    try:
        document = json.loads(
            payload,
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                StagingError(f"{purpose} response contains a non-finite JSON value")
            ),
        )
    except StagingError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StagingError(f"{purpose} response is not strict JSON") from exc
    if not isinstance(document, dict):
        raise StagingError(f"{purpose} response is not one JSON object")
    return document


def _endpoint_path(endpoint: str, suffix: str) -> tuple[Any, str]:
    parsed = urlsplit(validate_endpoint(endpoint))
    base_path = parsed.path.rstrip("/")
    return parsed, f"{base_path}{suffix}" or suffix


def _authenticated_json_request(
    endpoint: str,
    token: str,
    *,
    method: str,
    suffix: str,
    expected_status: int,
    purpose: str,
) -> dict[str, Any]:
    """Make one bounded, no-redirect authenticated staging request."""

    parsed, path = _endpoint_path(endpoint, suffix)
    connection = http.client.HTTPSConnection(
        parsed.hostname,
        parsed.port or 443,
        timeout=10,
        context=ssl.create_default_context(),
    )
    try:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Cache-Control": "no-store",
        }
        body = b"" if method == "POST" else None
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read(MAX_JSON_BYTES + 1)
        if 300 <= response.status < 400:
            raise StagingError(f"{purpose} redirects are forbidden")
        if response.status != expected_status:
            raise StagingError(f"{purpose} returned HTTP {response.status}")
        return _strict_json_bytes(payload, purpose=purpose)
    except StagingError:
        raise
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise StagingError(f"{purpose} request failed") from exc
    finally:
        connection.close()


def _strict_uuid4(value: Any, *, purpose: str) -> str:
    if not isinstance(value, str):
        raise StagingError(f"{purpose} did not return one canonical UUID4")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise StagingError(f"{purpose} did not return one canonical UUID4") from exc
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122 or str(parsed) != value:
        raise StagingError(f"{purpose} did not return one canonical UUID4")
    return value


def _local_chat_row_count(
    *,
    environment: Mapping[str, str],
    project: str,
    chat_id: str,
) -> int:
    """Count one strict probe UUID in this Compose project's PostgreSQL."""

    _strict_uuid4(chat_id, purpose="public route probe")
    statement = f"SELECT COUNT(*) FROM chats WHERE id = '{chat_id}';\n".encode("ascii")
    try:
        output = _run(
            _compose(
                environment,
                project,
                "exec",
                "--no-TTY",
                "postgres",
                "psql",
                "--quiet",
                "--tuples-only",
                "--no-align",
                "--username",
                environment["STAGING_DB_USER"],
                "--dbname",
                environment["STAGING_DB_NAME"],
                "--set",
                "ON_ERROR_STOP=1",
            ),
            environment=environment,
            input_bytes=statement,
        ).stdout
    except (KeyError, StagingError) as exc:
        raise StagingError("public route local database proof failed") from exc
    try:
        value = output.decode("ascii").strip()
    except UnicodeError as exc:
        raise StagingError("public route local database proof is malformed") from exc
    if value not in {"0", "1"}:
        raise StagingError("public route local database proof is malformed")
    return int(value)


def _remove_local_empty_probe_chat(
    *,
    environment: Mapping[str, str],
    project: str,
    chat_id: str,
) -> None:
    """Remove only the exact content-free probe row and prove it is absent."""

    _strict_uuid4(chat_id, purpose="public route probe cleanup")
    statement = (
        "DELETE FROM chats AS probe "
        f"WHERE probe.id = '{chat_id}' "
        "AND probe.title = 'New Chat' "
        "AND probe.agent_id IS NULL "
        "AND probe.has_saved_components IS NOT TRUE "
        "AND NOT EXISTS (SELECT 1 FROM messages WHERE chat_id = probe.id) "
        "AND NOT EXISTS (SELECT 1 FROM saved_components WHERE chat_id = probe.id) "
        "AND NOT EXISTS (SELECT 1 FROM chat_files WHERE chat_id = probe.id) "
        "AND NOT EXISTS (SELECT 1 FROM workspace_layout WHERE chat_id = probe.id); "
        f"SELECT COUNT(*) FROM chats WHERE id = '{chat_id}';\n"
    ).encode("ascii")
    try:
        output = _run(
            _compose(
                environment,
                project,
                "exec",
                "--no-TTY",
                "postgres",
                "psql",
                "--quiet",
                "--tuples-only",
                "--no-align",
                "--username",
                environment["STAGING_DB_USER"],
                "--dbname",
                environment["STAGING_DB_NAME"],
                "--set",
                "ON_ERROR_STOP=1",
            ),
            environment=environment,
            input_bytes=statement,
        ).stdout
    except (KeyError, StagingError) as exc:
        raise StagingError("local probe cleanup could not be verified") from exc
    try:
        remaining = output.decode("ascii").strip()
    except UnicodeError as exc:
        raise StagingError("local probe cleanup proof is malformed") from exc
    if remaining != "0":
        raise StagingError("local probe cleanup refused a non-empty or mismatched row")


def _verify_public_endpoint_binding(
    endpoint: str,
    token: str,
    *,
    environment: Mapping[str, str],
    project: str,
) -> None:
    """Bind the public route to this project's DB using a transient empty chat."""

    chat_id: str | None = None
    failure: StagingError | None = None
    cleanup_failure: StagingError | None = None
    try:
        created = _authenticated_json_request(
            endpoint,
            token,
            method="POST",
            suffix="/api/chats",
            expected_status=201,
            purpose="public route create probe",
        )
        chat_id = _strict_uuid4(created.get("chat_id"), purpose="public route create probe")
        if set(created) != {"chat_id", "agent_id", "message"}:
            raise StagingError("public route create probe response shape is invalid")
        if created.get("agent_id") is not None or not isinstance(created.get("message"), str):
            raise StagingError("public route create probe response shape is invalid")
        if _local_chat_row_count(
            environment=environment,
            project=project,
            chat_id=chat_id,
        ) != 1:
            raise StagingError("public endpoint is not bound to the current staging database")

        deleted = _authenticated_json_request(
            endpoint,
            token,
            method="DELETE",
            suffix=f"/api/chats/{chat_id}",
            expected_status=200,
            purpose="public route delete probe",
        )
        if set(deleted) != {"success", "message"} or deleted.get("success") is not True:
            raise StagingError("public route delete probe response shape is invalid")
        if _local_chat_row_count(
            environment=environment,
            project=project,
            chat_id=chat_id,
        ) != 0:
            raise StagingError("public endpoint deletion did not reach the current staging database")
    except StagingError as exc:
        failure = exc
    finally:
        if chat_id is not None and failure is not None:
            try:
                _authenticated_json_request(
                    endpoint,
                    token,
                    method="DELETE",
                    suffix=f"/api/chats/{chat_id}",
                    expected_status=200,
                    purpose="public route cleanup probe",
                )
            except StagingError:
                pass
            try:
                _remove_local_empty_probe_chat(
                    environment=environment,
                    project=project,
                    chat_id=chat_id,
                )
            except StagingError as exc:
                cleanup_failure = exc
    if failure is not None:
        if cleanup_failure is not None:
            raise StagingError(f"{failure}; {cleanup_failure}") from failure
        raise failure


def _probe(endpoint: str, token: str, *, timeout_seconds: int = 180) -> dict[str, Any]:
    parsed = urlsplit(endpoint)
    context = ssl.create_default_context()
    deadline = time.monotonic() + timeout_seconds
    last_error = "not attempted"
    while time.monotonic() < deadline:
        connection = http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port or 443,
            timeout=10,
            context=context,
        )
        try:
            ready_path = f"{parsed.path.rstrip('/')}/readyz" or "/readyz"
            connection.request("GET", ready_path)
            response = connection.getresponse()
            ready_body = response.read(MAX_READY_BYTES + 1)
            if response.status != 200:
                raise StagingError(f"readiness returned HTTP {response.status}")
            if len(ready_body) > MAX_READY_BYTES:
                raise StagingError("readiness response is oversized")
            dashboard_path = f"{parsed.path.rstrip('/')}/api/dashboard" or "/api/dashboard"
            connection.request(
                "GET", dashboard_path, headers={"Authorization": f"Bearer {token}"}
            )
            dashboard_response = connection.getresponse()
            body = dashboard_response.read(MAX_JSON_BYTES + 1)
            if dashboard_response.status != 200 or len(body) > MAX_JSON_BYTES:
                raise StagingError(
                    f"authenticated dashboard returned HTTP {dashboard_response.status}"
                )
            dashboard = _strict_json_bytes(body, purpose="authenticated dashboard")
            capability = dashboard.get("capabilities", {}).get(
                "personal_agent_host", {}
            ).get("macos")
            if not isinstance(capability, dict):
                raise StagingError("candidate dashboard lacks the macOS host capability map")
            return capability
        except (OSError, ValueError, http.client.HTTPException, StagingError) as exc:
            last_error = str(exc)
            time.sleep(3)
        finally:
            connection.close()
    raise StagingError(f"staging endpoint did not become reachable: {last_error}")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".staging-output-", dir=path.parent) as temp:
        temporary = Path(temp) / "outputs.json"
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)


def _load_evidence_validator() -> Any:
    """Import scripts/validate_release_evidence.py for trust-schema validation."""

    path = Path(__file__).resolve().parent / "validate_release_evidence.py"
    spec = importlib.util.spec_from_file_location(
        "candidate_staging_release_validator", path
    )
    if spec is None or spec.loader is None:
        raise StagingError(f"cannot import release-evidence validator at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _trusted_manifest_identity(protected: Mapping[str, str]) -> dict[str, Any]:
    """Collect fail-closed GitHub identity for the trusted stage-deploy manifest."""

    if protected["GITHUB_JOB"] != "stage-deploy":
        raise StagingError("trusted manifest generation requires the stage-deploy GitHub job")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not REPOSITORY_RE.fullmatch(repository):
        raise StagingError("trusted manifest requires GITHUB_REPOSITORY as owner/repo")
    workflow_name = os.environ.get("GITHUB_WORKFLOW", "")
    if not workflow_name:
        raise StagingError("trusted manifest requires GITHUB_WORKFLOW")
    workflow_path = os.environ.get("GITHUB_WORKFLOW_REF", "").partition("@")[0]
    workflow_sha = os.environ.get("GITHUB_WORKFLOW_SHA", "")
    if not WORKFLOW_PATH_RE.fullmatch(workflow_path) or not GIT_SHA_RE.fullmatch(
        workflow_sha
    ):
        raise StagingError(
            "trusted manifest requires GITHUB_WORKFLOW_REF and a 40-hex GITHUB_WORKFLOW_SHA"
        )
    builder_sha = os.environ.get("RELEASE_TRUSTED_BUILDER_SHA", "")
    builder_identity = os.environ.get("RELEASE_TRUSTED_BUILDER_IDENTITY", "")
    if not GIT_SHA_RE.fullmatch(builder_sha) or not builder_identity:
        raise StagingError(
            "trusted manifest requires RELEASE_TRUSTED_BUILDER_SHA and "
            "RELEASE_TRUSTED_BUILDER_IDENTITY"
        )
    if workflow_sha != builder_sha:
        raise StagingError(
            "trusted manifest workflow SHA differs from the installed protected commit"
        )
    try:
        run_attempt = int(protected["GITHUB_RUN_ATTEMPT"])
    except ValueError as exc:
        raise StagingError("GITHUB_RUN_ATTEMPT must be an integer") from exc
    return {
        "repository": repository,
        "workflow_name": workflow_name,
        "workflow_ref": f"{workflow_path}@{workflow_sha}",
        "builder_sha": builder_sha,
        "builder_identity": builder_identity,
        "run_attempt": run_attempt,
    }


def _write_trusted_manifest(
    *,
    path: Path,
    identity: Mapping[str, Any],
    protected: Mapping[str, str],
    candidate_sha: str,
    environment_id: str,
    output: Mapping[str, Any],
    outputs_path: Path,
    stage_outputs_artifact_id: str,
    stage_outputs_artifact_name: str,
    stage_outputs_member: str,
) -> None:
    """Emit a schema-valid manifest after immutable stage-output upload.

    The self-declared artifact/builder values are never a trust root: the
    protected trusted-builder workflow reconstructs run/job/artifact identity
    from the GitHub API for the current run and ignores producer-uploaded
    bytes as authority (release-trust.schema.json $comment).
    """

    repository = str(identity["repository"])
    run_id = protected["GITHUB_RUN_ID"]
    if not ARTIFACT_ID_RE.fullmatch(stage_outputs_artifact_id):
        raise StagingError("stage-outputs-artifact-id must be a positive integer")
    if not STAGE_OUTPUTS_ARTIFACT_NAME_RE.fullmatch(stage_outputs_artifact_name):
        raise StagingError("stage-outputs-artifact-name is not canonical")
    if (
        stage_outputs_member != outputs_path.name
        or Path(stage_outputs_member).name != stage_outputs_member
    ):
        raise StagingError("stage-outputs-member must name the exact uploaded output file")
    # The trust deployment identity is the deploy output minus the two
    # evidence-only fields (deployed_at, macos_personal_agent_host).
    deployment = {
        key: value
        for key, value in output.items()
        if key not in {"deployed_at", "macos_personal_agent_host"}
    }
    manifest = {
        "document_type": "trusted_stage_deploy",
        "schema_version": 1,
        "manifest_id": str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "astraldeep:060:trusted-stage-deploy:"
                f"{repository}:{run_id}:{identity['run_attempt']}:{environment_id}",
            )
        ),
        "repository": repository,
        "candidate_sha": candidate_sha,
        "workflow": {
            "name": identity["workflow_name"],
            "run_id": run_id,
            "run_attempt": identity["run_attempt"],
            "job_id": protected["GITHUB_JOB"],
        },
        "workflow_ref": identity["workflow_ref"],
        "runner": {
            "os": os.environ.get("ASTRAL_RUNNER_OS", "linux"),
            "architecture": os.environ.get("ASTRAL_RUNNER_ARCH", "x86_64"),
            "runner_image": os.environ.get("ImageOS", "self-hosted-staging"),
            "runner_name": protected["RUNNER_NAME"],
            "runner_environment": os.environ.get(
                "ASTRAL_RUNNER_ENVIRONMENT", "self_hosted"
            ),
        },
        "trusted_builder": {
            "repository": repository,
            "workflow_path": ".github/workflows/release-trusted-builder.yml",
            "signer_digest": identity["builder_sha"],
            "certificate_identity": identity["builder_identity"],
        },
        "generated_at": output["deployed_at"],
        "stage_outputs_artifact": {
            "kind": "github_actions_artifact_member",
            "repository": repository,
            "run_id": run_id,
            "run_attempt": identity["run_attempt"],
            "artifact_id": stage_outputs_artifact_id,
            "artifact_name": stage_outputs_artifact_name,
            "member": stage_outputs_member,
            "immutable_reference": (
                f"gh://{repository}/runs/{run_id}/attempts/"
                f"{identity['run_attempt']}/artifacts/{stage_outputs_artifact_id}/"
                f"members/{stage_outputs_member}"
            ),
            "sha256": _sha256(outputs_path),
        },
        "deployment": deployment,
    }
    validator = _load_evidence_validator()
    trust_schema = validator.load_json_document(TRUST_SCHEMA_PATH)
    try:
        validator.validate_document(manifest, trust_schema)
    except validator.ReleaseEvidenceError as exc:
        raise StagingError(
            f"trusted stage-deploy manifest is schema-invalid: {exc}"
        ) from exc
    _atomic_json(path, manifest)


def _write_trusted_manifest_command(args: argparse.Namespace) -> int:
    """Write the stage manifest only after Actions returns the immutable ID."""

    protected = _required_environment(for_deploy=False)
    identity = _trusted_manifest_identity(protected)
    if not GIT_SHA_RE.fullmatch(args.candidate_sha):
        raise StagingError("candidate-sha must be one lowercase 40-character Git SHA")
    expected_environment_id = _run_scoped_environment_id(protected)
    if args.environment_id != expected_environment_id:
        raise StagingError(
            "trusted manifest environment-id differs from the current protected run"
        )
    outputs_path = Path(args.outputs)
    if outputs_path.is_symlink() or not outputs_path.is_file():
        raise StagingError("trusted manifest requires one regular stage outputs file")
    output = _strict_json(outputs_path)
    expected_project = _project_name(expected_environment_id)
    if (
        output.get("environment_id") != expected_environment_id
        or output.get("request_namespace") != expected_project
        or output.get("deployment_run_id") != protected["GITHUB_RUN_ID"]
    ):
        raise StagingError("stage outputs differ from the current protected deployment")
    _assert_no_secret_values(output, location="$staging_output")
    _write_trusted_manifest(
        path=Path(args.trusted_manifest),
        identity=identity,
        protected=protected,
        candidate_sha=args.candidate_sha,
        environment_id=args.environment_id,
        output=output,
        outputs_path=outputs_path,
        stage_outputs_artifact_id=args.stage_outputs_artifact_id,
        stage_outputs_artifact_name=args.stage_outputs_artifact_name,
        stage_outputs_member=args.stage_outputs_member,
    )
    print(
        json.dumps(
            {
                "candidate_sha": args.candidate_sha,
                "environment_id": args.environment_id,
                "stage_outputs_artifact_id": args.stage_outputs_artifact_id,
                "trusted_manifest": str(Path(args.trusted_manifest)),
            },
            sort_keys=True,
        )
    )
    return 0


def _assert_ownership_labels(
    labels: Any,
    *,
    expected: Mapping[str, str],
    project: str,
    resource: str,
    require_compose_project: bool = False,
) -> None:
    """Reject a staged resource that is not owned by this exact protected run."""

    if not isinstance(labels, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in labels.items()
    ):
        raise StagingError(f"staging {resource} has malformed ownership labels")
    compose_project = labels.get("com.docker.compose.project")
    if require_compose_project and compose_project != project:
        raise StagingError(f"staging {resource} lacks the exact Compose project label")
    if not require_compose_project and compose_project not in (None, project):
        raise StagingError(f"staging {resource} belongs to a different Compose project")
    mismatched = [key for key, value in expected.items() if labels.get(key) != value]
    if mismatched:
        raise StagingError(
            f"staging {resource} has missing or mismatched protected ownership labels"
        )


def _running_compose_services(
    payload: bytes, *, expected_images: Mapping[str, str]
) -> set[str]:
    """Return running service names from bounded Compose JSON/NDJSON output."""

    if not payload or len(payload) > COMPOSE_OUTPUT_MAX_BYTES:
        raise StagingError("Compose service identity output is absent or oversized")
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise StagingError("Compose service identity output is not UTF-8") from exc
    try:
        document = json.loads(text)
        records = document if isinstance(document, list) else [document]
    except json.JSONDecodeError:
        try:
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise StagingError("Compose service identity output is not JSON") from exc
    if not records or any(not isinstance(record, dict) for record in records):
        raise StagingError("Compose service identity output has no service records")
    services: dict[str, tuple[str, str]] = {}
    for record in records:
        service = record.get("Service")
        state = record.get("State")
        image = record.get("Image")
        if (
            not isinstance(service, str)
            or not service
            or not isinstance(state, str)
            or not isinstance(image, str)
            or not image
        ):
            raise StagingError("Compose service identity record is malformed")
        if service in services:
            raise StagingError(f"Compose service identity duplicates {service}")
        services[service] = (state.lower(), image)
    if set(expected_images) != set(STAGING_COMPOSE_SERVICES):
        raise StagingError("approved staging image identity has the wrong service set")
    for reference in expected_images.values():
        validate_image_reference(reference)
    required = STAGING_RUNNING_SERVICES
    missing = required - services.keys()
    if missing:
        raise StagingError(
            "qualifying staging lacks required services: " + ", ".join(sorted(missing))
        )
    stopped = {
        service for service in required if services[service][0] != "running"
    }
    if stopped:
        raise StagingError(
            "qualifying staging has non-running services: " + ", ".join(sorted(stopped))
        )
    for service in required:
        if services[service][1] != expected_images[service]:
            raise StagingError(
                f"running {service} container differs from its approved image reference"
            )
    return set(services)


def _compose_runtime_identity(
    payload: bytes,
    *,
    project: str,
    expected_images: Mapping[str, str],
    livekit_turn_domain: str,
    ownership_labels: Mapping[str, str],
    staging_bind_port: str,
) -> dict[str, Any]:
    """Validate the rendered public deployment model and return its safe identity."""

    if not payload or len(payload) > COMPOSE_OUTPUT_MAX_BYTES:
        raise StagingError("rendered Compose model is absent or oversized")

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise StagingError(f"rendered Compose model duplicates key {key!r}")
            result[key] = value
        return result

    try:
        document = json.loads(
            payload,
            object_pairs_hook=pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                StagingError(f"rendered Compose model contains {value!r}")
            ),
        )
    except StagingError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StagingError("rendered Compose model is not strict JSON") from exc
    if not isinstance(document, dict) or document.get("name") != project:
        raise StagingError("rendered Compose model has the wrong project identity")
    services = document.get("services")
    if not isinstance(services, dict):
        raise StagingError("rendered Compose model has no service map")
    if set(services) != set(STAGING_COMPOSE_SERVICES):
        raise StagingError("rendered Compose model has an unexpected service set")
    for service, model in services.items():
        if not isinstance(model, dict):
            raise StagingError(f"rendered Compose service {service} is malformed")
        _assert_ownership_labels(
            model.get("labels"),
            expected=ownership_labels,
            project=project,
            resource=f"service {service}",
        )
    volumes = document.get("volumes")
    if not isinstance(volumes, dict) or set(volumes) != set(STAGING_COMPOSE_VOLUMES):
        raise StagingError("rendered Compose model has an unexpected volume set")
    for volume, model in volumes.items():
        if not isinstance(model, dict):
            raise StagingError(f"rendered Compose volume {volume} is malformed")
        _assert_ownership_labels(
            model.get("labels"),
            expected=ownership_labels,
            project=project,
            resource=f"volume {volume}",
        )
    networks = document.get("networks")
    default_network = networks.get("default") if isinstance(networks, dict) else None
    if not isinstance(default_network, dict) or set(networks) != {"default"}:
        raise StagingError("rendered Compose model has an unexpected network set")
    _assert_ownership_labels(
        default_network.get("labels"),
        expected=ownership_labels,
        project=project,
        resource="default network",
    )
    if set(expected_images) != set(STAGING_COMPOSE_SERVICES):
        raise StagingError("approved staging image identity has the wrong service set")
    for service, expected_image in expected_images.items():
        validate_image_reference(expected_image)
        model = services.get(service)
        if model.get("image") != expected_image:
            raise StagingError(
                f"rendered Compose model does not bind {service} to its approved image"
            )
    try:
        published_port = int(staging_bind_port)
    except ValueError as exc:
        raise StagingError("STAGING_BIND_PORT must be one decimal TCP port") from exc
    if not 1 <= published_port <= 65535 or str(published_port) != staging_bind_port:
        raise StagingError("STAGING_BIND_PORT must be one canonical TCP port")
    astraldeep_ports = services["astraldeep"].get("ports")
    expected_port = {
        "host_ip": "127.0.0.1",
        "target": 8001,
        "published": str(published_port),
        "protocol": "tcp",
        "mode": "ingress",
    }
    if not isinstance(astraldeep_ports, list) or len(astraldeep_ports) != 1:
        raise StagingError("rendered AstralDeep service has an ambiguous host-port route")
    route = astraldeep_ports[0]
    if not isinstance(route, dict) or any(
        route.get(key) != value for key, value in expected_port.items()
    ):
        raise StagingError(
            "rendered AstralDeep host-port route differs from the protected loopback binding"
        )
    livekit = services["livekit"]
    if livekit.get("command") != ["--config", "/etc/livekit.yaml"]:
        raise StagingError("rendered LiveKit command does not use the tracked config mount")
    volumes = livekit.get("volumes")
    if not isinstance(volumes, list):
        raise StagingError("rendered LiveKit service has no config mount")
    expected_source = LIVEKIT_STAGING_CONFIG_PATH.resolve(strict=True)
    matches = [
        volume
        for volume in volumes
        if isinstance(volume, dict)
        and volume.get("type") == "bind"
        and volume.get("target") == "/etc/livekit.yaml"
    ]
    if len(matches) != 1:
        raise StagingError("rendered LiveKit service has an ambiguous config mount")
    mount = matches[0]
    try:
        source = Path(str(mount.get("source", ""))).resolve(strict=True)
    except OSError as exc:
        raise StagingError("rendered LiveKit config source is not a real file") from exc
    if source != expected_source or mount.get("read_only") is not True:
        raise StagingError("rendered LiveKit config mount differs from the tracked read-only file")
    livekit_ports = livekit.get("ports")
    if not isinstance(livekit_ports, list):
        raise StagingError("rendered LiveKit service has no host-port routes")
    expected_turn_upstream = {
        "host_ip": LIVEKIT_TURN_TLS_TERMINATOR_UPSTREAM_HOST,
        "target": LIVEKIT_TURN_TLS_LISTENER_PORT,
        "published": str(LIVEKIT_TURN_TLS_TERMINATOR_UPSTREAM_PORT),
        "protocol": "tcp",
        "mode": "ingress",
    }
    turn_upstreams = [
        route
        for route in livekit_ports
        if isinstance(route, dict)
        and route.get("target") == LIVEKIT_TURN_TLS_LISTENER_PORT
        and route.get("protocol") == "tcp"
    ]
    if len(turn_upstreams) != 1 or any(
        turn_upstreams[0].get(key) != value
        for key, value in expected_turn_upstream.items()
    ):
        raise StagingError(
            "rendered LiveKit TURN/TLS upstream differs from the protected loopback route"
        )
    livekit_turn_tls = {
        "advertised_uri": (
            f"turns:{livekit_turn_domain}:"
            f"{LIVEKIT_TURN_TLS_PUBLIC_PORT}?transport=tcp"
        ),
        "public_port": LIVEKIT_TURN_TLS_PUBLIC_PORT,
        "external_tls": True,
        "terminator_upstream_host": LIVEKIT_TURN_TLS_TERMINATOR_UPSTREAM_HOST,
        "terminator_upstream_port": LIVEKIT_TURN_TLS_TERMINATOR_UPSTREAM_PORT,
        "livekit_listener_port": LIVEKIT_TURN_TLS_LISTENER_PORT,
    }
    return {
        "images": dict(expected_images),
        "livekit_config": {
            "sha256": _sha256(expected_source),
            "target": "/etc/livekit.yaml",
            "read_only": True,
        },
        "livekit_turn_tls": livekit_turn_tls,
        "astraldeep_host_route": expected_port,
    }


def _bounded_resource_identifiers(payload: bytes, *, resource: str) -> list[str]:
    if len(payload) > COMPOSE_OUTPUT_MAX_BYTES:
        raise StagingError(f"staging {resource} inventory is oversized")
    try:
        identifiers = payload.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise StagingError(f"staging {resource} inventory is not UTF-8") from exc
    identifiers = [identifier.strip() for identifier in identifiers if identifier.strip()]
    if len(identifiers) > 64 or len(set(identifiers)) != len(identifiers):
        raise StagingError(f"staging {resource} inventory is duplicated or unbounded")
    if any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}", identifier)
        for identifier in identifiers
    ):
        raise StagingError(f"staging {resource} inventory contains an invalid identifier")
    return identifiers


def _label_documents(payload: bytes, *, expected_count: int, resource: str) -> list[dict[str, str]]:
    if not payload or len(payload) > COMPOSE_OUTPUT_MAX_BYTES:
        raise StagingError(f"staging {resource} label evidence is absent or oversized")

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise StagingError(f"staging {resource} label evidence duplicates {key!r}")
            result[key] = value
        return result

    try:
        documents = [
            json.loads(line, object_pairs_hook=pairs)
            for line in payload.decode("utf-8").splitlines()
            if line.strip()
        ]
    except StagingError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StagingError(f"staging {resource} label evidence is not strict JSON") from exc
    if len(documents) != expected_count or any(
        not isinstance(document, dict) for document in documents
    ):
        raise StagingError(f"staging {resource} label evidence has the wrong cardinality")
    return documents


def _validate_cleanup_resource_ownership(
    *,
    environment: Mapping[str, str],
    project: str,
    ownership_labels: Mapping[str, str],
) -> int:
    """Verify every Compose-targeted resource before destructive cleanup."""

    resource_commands = {
        "container": (
            ["docker", "container", "ls", "--all", "--quiet"],
            "{{json .Config.Labels}}",
        ),
        "volume": (["docker", "volume", "ls", "--quiet"], "{{json .Labels}}"),
        "network": (["docker", "network", "ls", "--quiet"], "{{json .Labels}}"),
    }
    discovered = 0
    for resource, (inventory_command, label_format) in resource_commands.items():
        inventory = _run(
            [
                *inventory_command,
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            environment=environment,
        ).stdout
        identifiers = _bounded_resource_identifiers(inventory, resource=resource)
        if not identifiers:
            continue
        labels = _run(
            [
                "docker",
                resource,
                "inspect",
                "--format",
                label_format,
                *identifiers,
            ],
            environment=environment,
        ).stdout
        documents = _label_documents(
            labels,
            expected_count=len(identifiers),
            resource=resource,
        )
        for index, document in enumerate(documents):
            _assert_ownership_labels(
                document,
                expected=ownership_labels,
                project=project,
                resource=f"{resource} {identifiers[index]}",
                require_compose_project=True,
            )
        discovered += len(identifiers)
    return discovered


def _deploy(args: argparse.Namespace) -> int:
    protected = _required_environment()
    if not args.leave_running:
        raise StagingError("qualifying deploy must use --leave-running until matrix cleanup")
    candidate_image = validate_image_reference(args.candidate_image)
    voice_runtime = _voice_runtime(args)
    voice_runtime.update(
        {
            "livekit_public_url": protected["LIVEKIT_PUBLIC_URL"],
            "livekit_turn_domain": protected["LIVEKIT_TURN_DOMAIN"],
        }
    )
    tracked_livekit_config_sha256 = _sha256(LIVEKIT_STAGING_CONFIG_PATH)
    if voice_runtime["livekit_config_sha256"] != tracked_livekit_config_sha256:
        raise StagingError(
            "livekit-config-sha256 differs from the tracked staging configuration"
        )
    _git_identity(args.candidate_sha, args.candidate_source_root)
    fixtures = validate_fixtures(args.fixture_manifest)
    tracked_fixture_root = (
        REPO_ROOT / "backend/tests/fixtures/runtime_reliability_060/staging"
    ).resolve()
    if Path(args.fixture_manifest).resolve().parent != tracked_fixture_root:
        raise StagingError("qualifying deploy must use the tracked fixture root")
    project = _project_name(args.environment_id)
    ownership_labels = _staging_ownership_labels(
        project=project,
        environment_id=args.environment_id,
        run_id=protected["GITHUB_RUN_ID"],
        run_attempt=protected["GITHUB_RUN_ATTEMPT"],
    )
    environment = dict(os.environ)
    environment.update(
        {
            "STAGING_PROJECT_NAME": project,
            "STAGING_ENVIRONMENT_ID": args.environment_id,
            "STAGING_RUN_ID": protected["GITHUB_RUN_ID"],
            "STAGING_RUN_ATTEMPT": protected["GITHUB_RUN_ATTEMPT"],
            "STAGING_BIND_PORT": protected["STAGING_BIND_PORT"],
            "ASTRAL_CANDIDATE_IMAGE": candidate_image,
            "ASTRAL_VOICE_WORKER_IMAGE": voice_runtime[
                "voice_worker_image_reference"
            ],
            "STAGING_POSTGRES_IMAGE": protected["STAGING_POSTGRES_IMAGE"],
            "STAGING_KEYCLOAK_IMAGE": protected["STAGING_KEYCLOAK_IMAGE"],
            "STAGING_SCHEMA_BASELINE_IMAGE": protected[
                "STAGING_SCHEMA_BASELINE_IMAGE"
            ],
        }
    )
    expected_images = {
        "postgres": protected["STAGING_POSTGRES_IMAGE"],
        "keycloak-postgres": protected["STAGING_POSTGRES_IMAGE"],
        "keycloak": protected["STAGING_KEYCLOAK_IMAGE"],
        "livekit": voice_runtime["livekit_image_reference"],
        "schema-baseline": protected["STAGING_SCHEMA_BASELINE_IMAGE"],
        "astraldeep": candidate_image,
        "voice-worker": voice_runtime["voice_worker_image_reference"],
    }
    compose_model = _run(
        _compose(
            environment,
            project,
            "--profile",
            "bootstrap",
            "config",
            "--format",
            "json",
        ),
        environment=environment,
    ).stdout
    service_identity = _compose_runtime_identity(
        compose_model,
        project=project,
        expected_images=expected_images,
        livekit_turn_domain=protected["LIVEKIT_TURN_DOMAIN"],
        ownership_labels=ownership_labels,
        staging_bind_port=protected["STAGING_BIND_PORT"],
    )
    _run(
        _compose(
            environment,
            project,
            "up",
            "--detach",
            "postgres",
            "keycloak-postgres",
            "keycloak",
            "livekit",
        ),
        environment=environment,
    )
    _run(
        _compose(
            environment,
            project,
            "--profile",
            "bootstrap",
            "run",
            "--rm",
            "schema-baseline",
        ),
        environment=environment,
    )
    fixture_bytes = (
        Path(args.fixture_manifest).resolve().parent / "representative-057.sql"
    ).read_bytes()
    _run(
        _compose(
            environment,
            project,
            "exec",
            "--no-TTY",
            "postgres",
            "psql",
            "--username",
            protected["STAGING_DB_USER"],
            "--dbname",
            protected["STAGING_DB_NAME"],
            "--set",
            "ON_ERROR_STOP=1",
        ),
        environment=environment,
        input_bytes=fixture_bytes,
    )
    _run(
        _compose(
            environment,
            project,
            "up",
            "--detach",
            "astraldeep",
            "voice-worker",
        ),
        environment=environment,
    )
    capability = _probe(
        protected["ASTRAL_STAGING_ENDPOINT"],
        protected["ASTRAL_STAGING_PROBE_TOKEN"],
    )
    _verify_public_endpoint_binding(
        protected["ASTRAL_STAGING_ENDPOINT"],
        protected["ASTRAL_STAGING_PROBE_TOKEN"],
        environment=environment,
        project=project,
    )
    revision = _run(
        _compose(
            environment,
            project,
            "exec",
            "--no-TTY",
            "postgres",
            "psql",
            "--tuples-only",
            "--no-align",
            "--username",
            protected["STAGING_DB_USER"],
            "--dbname",
            protected["STAGING_DB_NAME"],
            "--command",
            "SELECT value FROM schema_meta WHERE key='revision';",
        ),
        environment=environment,
    ).stdout.decode("utf-8").strip()
    if revision != "060.004":
        raise StagingError(f"candidate normal startup ended at schema {revision!r}")
    ps_bytes = _run(
        _compose(environment, project, "ps", "--format", "json"),
        environment=environment,
    ).stdout
    running_services = _running_compose_services(
        ps_bytes,
        expected_images=service_identity["images"],
    )
    service_identity["running_services"] = sorted(running_services)
    service_identity_bytes = json.dumps(
        service_identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    candidate_digest = candidate_image.rsplit("@sha256:", 1)[1]
    capability_bytes = json.dumps(
        capability,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    deployed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    output = {
        "environment_id": args.environment_id,
        "request_namespace": project,
        "topology": "shared_reachable_ephemeral",
        "deployment_run_id": protected["GITHUB_RUN_ID"],
        "deployed_at": deployed_at,
        "endpoint": validate_endpoint(protected["ASTRAL_STAGING_ENDPOINT"]),
        "candidate_image_reference": candidate_image,
        "candidate_image_sha256": candidate_digest,
        "representative_dataset_sha256": fixtures[
            "representative_dataset_sha256"
        ],
        "fixture_manifest_sha256": fixtures["fixture_manifest_sha256"],
        "keycloak_realm_sha256": fixtures["keycloak_realm_sha256"],
        "source_schema_revision": "057.001",
        "migrated_schema_revision": "060.004",
        "authentication_posture": "real_keycloak_oidc",
        "database_posture": "representative_postgresql",
        "worker_paths": ["background", "scheduler", "maintenance", "voice"],
        "voice_runtime": voice_runtime,
        "service_image_references": service_identity["images"],
        "livekit_turn_tls": service_identity["livekit_turn_tls"],
        "macos_personal_agent_host": {
            **capability,
            "source": "candidate_capability_map",
            "manifest_sha256": hashlib.sha256(capability_bytes).hexdigest(),
        },
        "capability_manifest_sha256": hashlib.sha256(capability_bytes).hexdigest(),
        "service_identity_sha256": hashlib.sha256(service_identity_bytes).hexdigest(),
    }
    _assert_no_secret_values(output, location="$staging_output")
    _atomic_json(Path(args.outputs), output)
    print(
        json.dumps(
            {
                "candidate_sha": args.candidate_sha,
                "environment_id": args.environment_id,
                "output": str(Path(args.outputs)),
                "qualifying_decision": False,
                "requires_protected_attestation": True,
            },
            sort_keys=True,
        )
    )
    return 0


def _cleanup(args: argparse.Namespace) -> int:
    protected = _required_environment(for_deploy=False)
    if protected["GITHUB_JOB"] != "stage-cleanup":
        raise StagingError("cleanup requires the exact stage-cleanup GitHub job")
    expected_environment_id = _run_scoped_environment_id(protected)
    if args.environment_id != expected_environment_id:
        raise StagingError(
            "cleanup environment-id differs from the current protected run"
        )
    project = _project_name(args.environment_id)
    ownership_labels = _staging_ownership_labels(
        project=project,
        environment_id=args.environment_id,
        run_id=protected["GITHUB_RUN_ID"],
        run_attempt=protected["GITHUB_RUN_ATTEMPT"],
    )
    environment = dict(os.environ)
    cleanup_image = (
        "registry.invalid/astraldeep/cleanup@sha256:"
        "0000000000000000000000000000000000000000000000000000000000000000"
    )
    environment.update(
        {
            "STAGING_PROJECT_NAME": project,
            "STAGING_ENVIRONMENT_ID": args.environment_id,
            "STAGING_RUN_ID": protected["GITHUB_RUN_ID"],
            "STAGING_RUN_ATTEMPT": protected["GITHUB_RUN_ATTEMPT"],
            # Compose interpolates the complete model even for `down`. These
            # bounded non-secret sentinels can never be launched by this path.
            "ASTRAL_CANDIDATE_IMAGE": cleanup_image,
            "ASTRAL_VOICE_WORKER_IMAGE": cleanup_image,
            "STAGING_POSTGRES_IMAGE": cleanup_image,
            "STAGING_KEYCLOAK_IMAGE": cleanup_image,
            "STAGING_SCHEMA_BASELINE_IMAGE": cleanup_image,
            "STAGING_RUNTIME_ENV_FILE": "/dev/null",
            "STAGING_DB_USER": "cleanup",
            "STAGING_DB_PASSWORD": "cleanup",
            "STAGING_DB_NAME": "cleanup",
            "STAGING_KEYCLOAK_DB_USER": "cleanup",
            "STAGING_KEYCLOAK_DB_PASSWORD": "cleanup",
            "STAGING_KEYCLOAK_DB_NAME": "cleanup",
            "STAGING_KEYCLOAK_ADMIN_USER": "cleanup",
            "STAGING_KEYCLOAK_ADMIN_PASSWORD": "cleanup",
            "STAGING_BIND_PORT": "18061",
            "LIVEKIT_PUBLIC_URL": "wss://cleanup.invalid",
            "LIVEKIT_API_KEY": "cleanup-key",
            "LIVEKIT_API_SECRET": "cleanup-secret",
            "LIVEKIT_TURN_DOMAIN": "turn.cleanup.invalid",
            "VOICE_CONTROL_SECRET": "cleanup-control",
            "OPENAI_BASE_URL": "https://speech.cleanup.invalid/v1",
            "OPENAI_API_KEY": "cleanup-speech",
        }
    )
    resource_count = _validate_cleanup_resource_ownership(
        environment=environment,
        project=project,
        ownership_labels=ownership_labels,
    )
    # Project scoping is mandatory: no global container, image, or volume cleanup.
    _run(
        _compose(
            environment,
            project,
            "down",
            "--volumes",
            "--remove-orphans",
            "--timeout",
            "30",
        ),
        environment=environment,
    )
    print(
        json.dumps(
            {
                "environment_id": args.environment_id,
                "removed": True,
                "validated_resource_count": resource_count,
            },
            sort_keys=True,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fixtures = commands.add_parser("validate-fixtures")
    fixtures.add_argument("--manifest", required=True)
    deploy = commands.add_parser("deploy")
    deploy.add_argument("--candidate-sha", required=True)
    deploy.add_argument("--candidate-source-root", required=True)
    deploy.add_argument("--candidate-image", required=True)
    deploy.add_argument("--voice-worker-image", required=True)
    deploy.add_argument("--livekit-image", required=True)
    deploy.add_argument("--livekit-config-sha256", required=True)
    deploy.add_argument("--speech-inventory-sha256", required=True)
    deploy.add_argument("--speech-profile-sha256", required=True)
    deploy.add_argument("--fixture-manifest", required=True)
    deploy.add_argument("--environment-id", required=True)
    deploy.add_argument("--outputs", required=True)
    deploy.add_argument("--leave-running", action="store_true")
    manifest = commands.add_parser("write-trusted-manifest")
    manifest.add_argument("--candidate-sha", required=True)
    manifest.add_argument("--environment-id", required=True)
    manifest.add_argument("--outputs", required=True)
    manifest.add_argument("--trusted-manifest", required=True)
    manifest.add_argument("--stage-outputs-artifact-id", required=True)
    manifest.add_argument("--stage-outputs-artifact-name", required=True)
    manifest.add_argument("--stage-outputs-member", required=True)
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--environment-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Execute fixture validation, protected deploy, or exact-namespace cleanup."""

    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-fixtures":
            print(json.dumps(validate_fixtures(args.manifest), sort_keys=True))
            return 0
        if args.command == "deploy":
            return _deploy(args)
        if args.command == "write-trusted-manifest":
            return _write_trusted_manifest_command(args)
        if args.command == "cleanup":
            return _cleanup(args)
        raise StagingError(f"unknown staging command: {args.command}")
    except StagingError as exc:
        print(f"candidate staging rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
