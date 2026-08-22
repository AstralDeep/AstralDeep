"""Fail-closed host configuration for the external LETS warden.

This module is intentionally limited to local configuration and readiness
posture.  It performs no network I/O and does not construct a LETS client.
Secret files are represented by validated references; token and private-key
contents are never read here.  A caller must provide the pinned LETS manifest
signature verifier before an active configuration can become ready.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, TypeAlias, cast
from urllib.parse import urlsplit

from orchestrator.lets_scope_profile import (
    RESOURCE_DIMENSIONS,
    SCOPE_PROFILE_VERSION,
    validate_allocation,
)

LetsMode: TypeAlias = Literal["off", "shadow", "enforce"]
ManifestAuthenticator: TypeAlias = Callable[[bytes, Mapping[str, Any]], bool]

LETS_ENFORCEMENT_CONTRACT: Final = "astral.lets-enforcement/v1"
LETS_RELEASE: Final = "v1.0.11"
LETS_RECEIPT_WIRE_TYPE: Final = "lets.receipt/v1"
INITIAL_GOVERNED_COHORTS: Final = ("server_dynamic", "byo_user")
MAX_TRUST_MANIFEST_BYTES: Final = 16 * 1024 * 1024
MAX_REQUEST_TIMEOUT_SECONDS: Final = 30.0
MAX_REQUEST_ATTEMPTS: Final = 3
_MAX_RESOURCE: Final = (1 << 63) - 1
_ACTIVE_MODES: Final = frozenset({"shadow", "enforce"})
_DEVELOPMENT_ENVIRONMENTS: Final = frozenset({"dev", "development", "test"})
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_POSITIVE_INTEGER = re.compile(r"^[1-9][0-9]*$")
_NON_NEGATIVE_INTEGER = re.compile(r"^(?:0|[1-9][0-9]*)$")
_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_DNS_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_COHORTS = frozenset(INITIAL_GOVERNED_COHORTS)
_MANIFEST_FIELDS = frozenset(
    {
        "api_version",
        "tenant_id",
        "envelope_id",
        "config_epoch",
        "created_at",
        "resources",
        "initial_budget",
        "wardens",
        "policies",
        "extensions",
        "signatures",
    }
)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class LetsConfigError(ValueError):
    """A stable, value-free configuration denial safe for logs."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SecretFileReference:
    """A nonempty regular-file reference whose contents remain unread."""

    path: Path = field(repr=False)
    size_bytes: int = field(repr=False)

    def redacted(self) -> str:
        return "<configured>"


@dataclass(frozen=True, slots=True)
class AuthenticatedTrustManifest:
    """Safe metadata retained after external signature authentication."""

    path: Path = field(repr=False)
    sha256: str
    tenant_id: str
    envelope_id: str
    config_epoch: int
    warden_id: str
    policy_digest: str
    machine_digest: str
    max_lease_ttl_ns: int


@dataclass(frozen=True, slots=True)
class LetsHostConfig:
    """Validated local LETS configuration with no plaintext credentials."""

    master_enabled: bool
    mode: LetsMode
    environment: str
    governed_cohorts: tuple[str, ...]
    governed_agent_allowlist: tuple[str, ...]
    warden_origin: str | None = None
    service_token_file: SecretFileReference | None = field(default=None, repr=False)
    ca_bundle: SecretFileReference | None = field(default=None, repr=False)
    client_cert_file: SecretFileReference | None = field(default=None, repr=False)
    client_key_file: SecretFileReference | None = field(default=None, repr=False)
    tenant_id: str | None = None
    envelope_id: str | None = None
    policy_digest: str | None = None
    machine_digest: str | None = None
    default_allocation: tuple[int, ...] | None = None
    default_ttl_seconds: int | None = None
    request_timeout_seconds: float | None = None
    request_attempts: int | None = None
    trust_manifest: AuthenticatedTrustManifest | None = field(default=None, repr=False)
    executor_instance_id: str | None = None
    executor_db_root: Path | None = field(default=None, repr=False)
    executor_authority_root: Path | None = field(default=None, repr=False)
    follow_redirects: Literal[False] = field(default=False, init=False)
    tls_verification_required: Literal[True] = field(default=True, init=False)
    scope_profile_version: str = field(default=SCOPE_PROFILE_VERSION, init=False)
    enforcement_contract: str = field(default=LETS_ENFORCEMENT_CONTRACT, init=False)
    lets_release: str = field(default=LETS_RELEASE, init=False)
    receipt_wire_type: str = field(default=LETS_RECEIPT_WIRE_TYPE, init=False)

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        authenticate_manifest: ManifestAuthenticator | None = None,
    ) -> "LetsHostConfig":
        """Strictly parse one environment mapping.

        Only the mode and master flag are relevant while mode is ``off``;
        dormant LETS variables are deliberately ignored to preserve flag-off
        behavior.  Active modes require the complete local trust and executor
        posture.
        """

        values = os.environ if environ is None else environ
        master_enabled = _master_flag(values)
        mode = _mode(values)
        environment = _environment(values)
        if mode != "off" and not master_enabled:
            raise LetsConfigError("master_flag_disabled")
        if mode == "off":
            return cls(
                master_enabled=master_enabled,
                mode=mode,
                environment=environment,
                governed_cohorts=INITIAL_GOVERNED_COHORTS,
                governed_agent_allowlist=(),
            )

        cohorts = _governed_cohorts(values)
        agent_allowlist = _agent_allowlist(values)
        production = environment not in _DEVELOPMENT_ENVIRONMENTS
        warden_origin = _warden_origin(
            _required(values, "LETS_WARDEN_URL", "missing_warden_url"),
            production=production,
        )
        service_token = _required_file(
            values,
            "LETS_SERVICE_TOKEN_FILE",
            missing_code="missing_service_token_file",
            invalid_code="invalid_service_token_file",
        )
        ca_bundle = _optional_file(
            values,
            "LETS_CA_BUNDLE",
            invalid_code="invalid_ca_bundle",
        )
        client_cert = _optional_file(
            values,
            "LETS_CLIENT_CERT_FILE",
            invalid_code="invalid_client_certificate_file",
        )
        client_key = _optional_file(
            values,
            "LETS_CLIENT_KEY_FILE",
            invalid_code="invalid_client_key_file",
        )
        if (client_cert is None) != (client_key is None):
            raise LetsConfigError("incomplete_mtls_identity")

        tenant_id = _identifier(values, "LETS_TENANT_ID", "invalid_tenant_id")
        envelope_id = _identifier(values, "LETS_ENVELOPE_ID", "invalid_envelope_id")
        policy_digest = _digest(values, "LETS_POLICY_DIGEST", "invalid_policy_digest")
        machine_digest = _digest(
            values, "LETS_MACHINE_DIGEST", "invalid_machine_digest"
        )
        allocation = _allocation(values)
        ttl_seconds = _positive_integer(
            values,
            "LETS_DEFAULT_TTL_SECONDS",
            "invalid_default_ttl",
            maximum=_MAX_RESOURCE // 1_000_000_000,
        )
        request_timeout = _request_timeout(values)
        request_attempts = _positive_integer(
            values,
            "LETS_REQUEST_ATTEMPTS",
            "invalid_request_attempts",
            maximum=MAX_REQUEST_ATTEMPTS,
        )
        executor_instance = _identifier(
            values,
            "LETS_EXECUTOR_INSTANCE_ID",
            "invalid_executor_instance_id",
            maximum=128,
        )
        executor_db_root = _required_data_root(
            values,
            "LETS_EXECUTOR_DB_ROOT",
            "missing_executor_db_root",
            "invalid_executor_db_root",
        )
        executor_authority_root = _optional_data_root(
            values,
            "LETS_EXECUTOR_AUTHORITY_ROOT",
            "invalid_executor_authority_root",
        )
        if production and executor_authority_root is None:
            raise LetsConfigError("missing_executor_authority_root")

        manifest_path = _required_path_text(
            values,
            "LETS_SIGNED_TRUST_MANIFEST",
            "missing_signed_trust_manifest",
        )
        if authenticate_manifest is None:
            # Production startup obtains manifest authority only from a
            # separately mounted operator trust bundle.  Tests/compositions
            # may still inject an equivalent authenticator explicitly.
            from orchestrator.lets_manifest import (
                OperatorTrustError,
                build_manifest_authenticator,
            )

            try:
                authenticate_manifest = build_manifest_authenticator(values)
            except OperatorTrustError:
                raise LetsConfigError("invalid_operator_trust_bundle") from None
        manifest = _load_authenticated_manifest(
            manifest_path,
            authenticate_manifest=authenticate_manifest,
            production=production,
            expected_warden_origin=warden_origin,
            expected_tenant_id=tenant_id,
            expected_envelope_id=envelope_id,
            expected_policy_digest=policy_digest,
            expected_machine_digest=machine_digest,
        )
        if ttl_seconds * 1_000_000_000 > manifest.max_lease_ttl_ns:
            raise LetsConfigError("default_ttl_exceeds_policy")

        return cls(
            master_enabled=master_enabled,
            mode=mode,
            environment=environment,
            governed_cohorts=cohorts,
            governed_agent_allowlist=agent_allowlist,
            warden_origin=warden_origin,
            service_token_file=service_token,
            ca_bundle=ca_bundle,
            client_cert_file=client_cert,
            client_key_file=client_key,
            tenant_id=tenant_id,
            envelope_id=envelope_id,
            policy_digest=policy_digest,
            machine_digest=machine_digest,
            default_allocation=allocation,
            default_ttl_seconds=ttl_seconds,
            request_timeout_seconds=request_timeout,
            request_attempts=request_attempts,
            trust_manifest=manifest,
            executor_instance_id=executor_instance,
            executor_db_root=executor_db_root,
            executor_authority_root=executor_authority_root,
        )

    def redacted(self) -> dict[str, object]:
        """Return diagnostics that contain no file paths or secret material."""

        return {
            "master_enabled": self.master_enabled,
            "mode": self.mode,
            "environment": self.environment,
            "governed_cohorts": self.governed_cohorts,
            "governed_agent_allowlist_count": len(self.governed_agent_allowlist),
            "warden_origin": self.warden_origin,
            "service_token_file": _configured(self.service_token_file),
            "ca_bundle": _configured(self.ca_bundle),
            "mtls_identity": self.client_cert_file is not None,
            "trust_manifest": _configured(self.trust_manifest),
            "executor_db_root": _configured(self.executor_db_root),
            "executor_authority_root": _configured(self.executor_authority_root),
            "follow_redirects": self.follow_redirects,
            "tls_verification_required": self.tls_verification_required,
            "scope_profile_version": self.scope_profile_version,
        }


@dataclass(frozen=True, slots=True)
class LetsReadiness:
    """No-network configuration posture for application readiness decisions."""

    mode: LetsMode
    status: Literal["disabled", "configured", "degraded", "blocked"]
    reason: str
    application_ready: bool
    lets_configured: bool
    governed_effects_permitted: bool
    diagnostic_only: bool


@dataclass(frozen=True, slots=True)
class LetsConfigLoad:
    """Configuration plus the mode-specific response to a local denial."""

    config: LetsHostConfig | None
    readiness: LetsReadiness


def load_lets_config(
    environ: Mapping[str, str] | None = None,
    *,
    authenticate_manifest: ManifestAuthenticator | None = None,
) -> LetsConfigLoad:
    """Load config and project invalid active config into readiness posture.

    Invalid mode, invalid master-flag syntax, and an active mode behind a false
    master flag remain startup errors.  Other active configuration errors are
    represented as shadow degradation or enforce blocking without exposing the
    rejected value.
    """

    values = os.environ if environ is None else environ
    master_enabled = _master_flag(values)
    mode = _mode(values)
    if mode != "off" and not master_enabled:
        raise LetsConfigError("master_flag_disabled")
    try:
        config = LetsHostConfig.from_environ(
            values,
            authenticate_manifest=authenticate_manifest,
        )
    except LetsConfigError as exc:
        if mode == "shadow":
            return LetsConfigLoad(
                config=None,
                readiness=LetsReadiness(
                    mode=mode,
                    status="degraded",
                    reason=exc.code,
                    application_ready=True,
                    lets_configured=False,
                    governed_effects_permitted=True,
                    diagnostic_only=True,
                ),
            )
        if mode == "enforce":
            return LetsConfigLoad(
                config=None,
                readiness=LetsReadiness(
                    mode=mode,
                    status="blocked",
                    reason=exc.code,
                    application_ready=False,
                    lets_configured=False,
                    governed_effects_permitted=False,
                    diagnostic_only=False,
                ),
            )
        raise

    if mode == "off":
        readiness = LetsReadiness(
            mode=mode,
            status="disabled",
            reason="lets_disabled",
            application_ready=True,
            lets_configured=False,
            governed_effects_permitted=True,
            diagnostic_only=False,
        )
    else:
        readiness = LetsReadiness(
            mode=mode,
            status="configured",
            reason=f"lets_{mode}_configured",
            application_ready=True,
            lets_configured=True,
            governed_effects_permitted=True,
            diagnostic_only=mode == "shadow",
        )
    return LetsConfigLoad(config=config, readiness=readiness)


def _master_flag(values: Mapping[str, str]) -> bool:
    if "FF_LETS_EXTERNAL_WARDEN" not in values:
        return False
    raw = values.get("FF_LETS_EXTERNAL_WARDEN")
    if not isinstance(raw, str):
        raise LetsConfigError("invalid_master_flag")
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise LetsConfigError("invalid_master_flag")


def _mode(values: Mapping[str, str]) -> LetsMode:
    raw = values.get("LETS_MODE", "off")
    if not isinstance(raw, str) or raw not in {"off", "shadow", "enforce"}:
        raise LetsConfigError("invalid_lets_mode")
    return cast(LetsMode, raw)


def _environment(values: Mapping[str, str]) -> str:
    raw = values.get("ASTRAL_ENV", "")
    if not isinstance(raw, str):
        return "production"
    return raw.strip().lower() or "production"


def _required(values: Mapping[str, str], name: str, code: str) -> str:
    raw = values.get(name)
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise LetsConfigError(code)
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw):
        raise LetsConfigError(code)
    return raw


def _governed_cohorts(values: Mapping[str, str]) -> tuple[str, ...]:
    raw = values.get("LETS_GOVERNED_COHORTS", ",".join(INITIAL_GOVERNED_COHORTS))
    if not isinstance(raw, str):
        raise LetsConfigError("invalid_governed_cohorts")
    cohorts = tuple(part.strip() for part in raw.split(","))
    if (
        not cohorts
        or any(not cohort or cohort not in _COHORTS for cohort in cohorts)
        or len(cohorts) != len(set(cohorts))
    ):
        raise LetsConfigError("invalid_governed_cohorts")
    return cohorts


def _agent_allowlist(values: Mapping[str, str]) -> tuple[str, ...]:
    raw = values.get("LETS_GOVERNED_AGENT_ALLOWLIST", "")
    if not isinstance(raw, str):
        raise LetsConfigError("invalid_governed_agent_allowlist")
    if not raw.strip():
        return ()
    agents = tuple(part.strip() for part in raw.split(","))
    if len(agents) != len(set(agents)):
        raise LetsConfigError("invalid_governed_agent_allowlist")
    for agent in agents:
        _require_identifier_value(agent, "invalid_governed_agent_allowlist")
    return agents


def _warden_origin(value: str, *, production: bool) -> str:
    if any(character.isspace() or character == "\\" for character in value):
        raise LetsConfigError("invalid_warden_url")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise LetsConfigError("invalid_warden_url") from exc
    if parsed.scheme not in {"http", "https"}:
        raise LetsConfigError("invalid_warden_url")
    if production and parsed.scheme != "https":
        raise LetsConfigError("insecure_warden_url")
    if (
        parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise LetsConfigError("invalid_warden_url")
    host = parsed.hostname
    if not host.isascii():
        raise LetsConfigError("invalid_warden_url")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (
            len(host) > 253
            or all(part.isdigit() for part in host.split("."))
            or any(_DNS_LABEL.fullmatch(part) is None for part in host.split("."))
        ):
            raise LetsConfigError("invalid_warden_url") from None
        canonical_host = host.lower()
    else:
        canonical_host = address.compressed
        if address.version == 6:
            canonical_host = f"[{canonical_host}]"
    default_port = 443 if parsed.scheme == "https" else 80
    port_suffix = "" if port is None or port == default_port else f":{port}"
    return f"{parsed.scheme}://{canonical_host}{port_suffix}"


def _required_file(
    values: Mapping[str, str],
    name: str,
    *,
    missing_code: str,
    invalid_code: str,
) -> SecretFileReference:
    raw = values.get(name)
    if not isinstance(raw, str) or not raw.strip():
        raise LetsConfigError(missing_code)
    return _file_reference(raw, invalid_code)


def _optional_file(
    values: Mapping[str, str],
    name: str,
    *,
    invalid_code: str,
) -> SecretFileReference | None:
    raw = values.get(name)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if not isinstance(raw, str):
        raise LetsConfigError(invalid_code)
    return _file_reference(raw, invalid_code)


def _file_reference(raw: str, code: str) -> SecretFileReference:
    try:
        path = Path(raw)
        if raw != raw.strip() or not path.is_absolute():
            raise OSError
        metadata = path.stat()
    except (OSError, ValueError):
        raise LetsConfigError(code) from None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise LetsConfigError(code)
    return SecretFileReference(path=path, size_bytes=metadata.st_size)


def _identifier(
    values: Mapping[str, str],
    name: str,
    code: str,
    *,
    maximum: int = 512,
) -> str:
    return _require_identifier_value(
        _required(values, name, code),
        code,
        maximum=maximum,
    )


def _require_identifier_value(value: str, code: str, *, maximum: int = 512) -> str:
    if (
        not value
        or len(value) > maximum
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise LetsConfigError(code)
    return value


def _digest(values: Mapping[str, str], name: str, code: str) -> str:
    raw = values.get(name)
    if not isinstance(raw, str) or _DIGEST.fullmatch(raw) is None:
        raise LetsConfigError(code)
    return raw


def _allocation(values: Mapping[str, str]) -> tuple[int, ...]:
    raw = values.get("LETS_DEFAULT_ALLOCATION")
    if not isinstance(raw, str):
        raise LetsConfigError("invalid_default_allocation")
    parts = tuple(part.strip() for part in raw.split(","))
    if len(parts) != RESOURCE_DIMENSIONS or any(
        _NON_NEGATIVE_INTEGER.fullmatch(part) is None for part in parts
    ):
        raise LetsConfigError("invalid_default_allocation")
    integers = tuple(int(part) for part in parts)
    if any(value > _MAX_RESOURCE for value in integers) or not any(integers):
        raise LetsConfigError("invalid_default_allocation")
    try:
        return validate_allocation(integers)
    except ValueError as exc:
        raise LetsConfigError("invalid_default_allocation") from exc


def _positive_integer(
    values: Mapping[str, str],
    name: str,
    code: str,
    *,
    maximum: int,
) -> int:
    raw = values.get(name)
    if not isinstance(raw, str) or _POSITIVE_INTEGER.fullmatch(raw) is None:
        raise LetsConfigError(code)
    value = int(raw)
    if value > maximum:
        raise LetsConfigError(code)
    return value


def _request_timeout(values: Mapping[str, str]) -> float:
    raw = values.get("LETS_REQUEST_TIMEOUT_SECONDS")
    if not isinstance(raw, str) or _DECIMAL.fullmatch(raw) is None:
        raise LetsConfigError("invalid_request_timeout")
    value = float(raw)
    if not math.isfinite(value) or not 0 < value <= MAX_REQUEST_TIMEOUT_SECONDS:
        raise LetsConfigError("invalid_request_timeout")
    return value


def _required_path_text(values: Mapping[str, str], name: str, code: str) -> Path:
    raw = values.get(name)
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise LetsConfigError(code)
    path = Path(raw)
    if not path.is_absolute():
        raise LetsConfigError(code)
    return path


def _required_data_root(
    values: Mapping[str, str],
    name: str,
    missing_code: str,
    invalid_code: str,
) -> Path:
    raw = values.get(name)
    if not isinstance(raw, str) or not raw.strip():
        raise LetsConfigError(missing_code)
    return _data_root(raw, invalid_code)


def _optional_data_root(
    values: Mapping[str, str],
    name: str,
    invalid_code: str,
) -> Path | None:
    raw = values.get(name)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if not isinstance(raw, str):
        raise LetsConfigError(invalid_code)
    return _data_root(raw, invalid_code)


def _data_root(raw: str, code: str) -> Path:
    try:
        path = Path(raw)
        if raw != raw.strip() or not path.is_absolute():
            raise OSError
        resolved = path.resolve(strict=True)
    except (OSError, ValueError):
        raise LetsConfigError(code) from None
    if (
        not resolved.is_dir()
        or resolved == _REPOSITORY_ROOT
        or resolved.is_relative_to(_REPOSITORY_ROOT)
    ):
        raise LetsConfigError(code)
    return resolved


def _load_authenticated_manifest(
    path: Path,
    *,
    authenticate_manifest: ManifestAuthenticator | None,
    production: bool,
    expected_warden_origin: str,
    expected_tenant_id: str,
    expected_envelope_id: str,
    expected_policy_digest: str,
    expected_machine_digest: str,
) -> AuthenticatedTrustManifest:
    if authenticate_manifest is None:
        raise LetsConfigError("trust_manifest_authenticator_required")
    try:
        metadata = path.stat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > MAX_TRUST_MANIFEST_BYTES
        ):
            raise OSError
        raw = path.read_bytes()
    except (OSError, ValueError):
        raise LetsConfigError("invalid_signed_trust_manifest") from None
    if not raw or len(raw) > MAX_TRUST_MANIFEST_BYTES:
        raise LetsConfigError("invalid_signed_trust_manifest")
    document = _strict_json_object(raw)
    if set(document) - _MANIFEST_FIELDS:
        raise LetsConfigError("invalid_signed_trust_manifest")
    if document.get("api_version") != "lets.manifest/v1":
        raise LetsConfigError("invalid_signed_trust_manifest")
    signatures = document.get("signatures")
    if not isinstance(signatures, list) or not signatures:
        raise LetsConfigError("unsigned_trust_manifest")
    if any(
        not isinstance(item, dict)
        or item.get("algorithm") != "Ed25519"
        or not isinstance(item.get("key_id"), str)
        or not isinstance(item.get("signature"), str)
        for item in signatures
    ):
        raise LetsConfigError("invalid_signed_trust_manifest")
    try:
        authenticated = authenticate_manifest(raw, document)
    except Exception:
        raise LetsConfigError("trust_manifest_authentication_failed") from None
    if authenticated is not True:
        raise LetsConfigError("trust_manifest_authentication_failed")

    if document.get("tenant_id") != expected_tenant_id:
        raise LetsConfigError("trust_manifest_tenant_mismatch")
    if document.get("envelope_id") != expected_envelope_id:
        raise LetsConfigError("trust_manifest_envelope_mismatch")
    config_epoch = document.get("config_epoch")
    if (
        isinstance(config_epoch, bool)
        or not isinstance(config_epoch, int)
        or not 0 < config_epoch <= _MAX_RESOURCE
    ):
        raise LetsConfigError("invalid_signed_trust_manifest")
    resources = document.get("resources")
    if (
        not isinstance(resources, list)
        or len(resources) != RESOURCE_DIMENSIONS
        or any(not isinstance(item, dict) for item in resources)
    ):
        raise LetsConfigError("trust_manifest_resource_mismatch")

    warden_id = _manifest_warden(
        document.get("wardens"),
        expected_warden_origin,
        production=production,
    )
    policy, max_lease_ttl_ns = _manifest_policy(
        document.get("policies"),
        expected_policy_digest,
        expected_machine_digest,
    )
    del policy
    return AuthenticatedTrustManifest(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        tenant_id=expected_tenant_id,
        envelope_id=expected_envelope_id,
        config_epoch=config_epoch,
        warden_id=warden_id,
        policy_digest=expected_policy_digest,
        machine_digest=expected_machine_digest,
        max_lease_ttl_ns=max_lease_ttl_ns,
    )


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def reject_constant(_value: str) -> None:
        raise ValueError

    def object_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=object_pairs,
        )
    except (UnicodeDecodeError, ValueError, TypeError):
        raise LetsConfigError("invalid_signed_trust_manifest") from None
    if not isinstance(value, dict):
        raise LetsConfigError("invalid_signed_trust_manifest")
    return value


def _manifest_warden(
    raw_wardens: object,
    expected_origin: str,
    *,
    production: bool,
) -> str:
    if not isinstance(raw_wardens, list) or not raw_wardens:
        raise LetsConfigError("invalid_signed_trust_manifest")
    matches: list[str] = []
    for item in raw_wardens:
        if not isinstance(item, dict):
            raise LetsConfigError("invalid_signed_trust_manifest")
        endpoint = item.get("client_endpoint")
        warden_id = item.get("warden_id")
        if not isinstance(endpoint, str) or not isinstance(warden_id, str):
            raise LetsConfigError("invalid_signed_trust_manifest")
        try:
            canonical_endpoint = _warden_origin(endpoint, production=production)
        except LetsConfigError:
            raise LetsConfigError("invalid_signed_trust_manifest") from None
        if canonical_endpoint == expected_origin:
            _require_identifier_value(
                warden_id, "invalid_signed_trust_manifest", maximum=128
            )
            matches.append(warden_id)
    if len(matches) != 1:
        raise LetsConfigError("trust_manifest_warden_mismatch")
    return matches[0]


def _manifest_policy(
    raw_policies: object,
    expected_policy_digest: str,
    expected_machine_digest: str,
) -> tuple[Mapping[str, Any], int]:
    if not isinstance(raw_policies, list) or not raw_policies:
        raise LetsConfigError("invalid_signed_trust_manifest")
    matches: list[Mapping[str, Any]] = []
    for item in raw_policies:
        if not isinstance(item, dict):
            raise LetsConfigError("invalid_signed_trust_manifest")
        if item.get("policy_digest") == expected_policy_digest:
            matches.append(item)
    if len(matches) != 1:
        raise LetsConfigError("trust_manifest_policy_mismatch")
    policy = matches[0]
    machine = policy.get("machine")
    machine_digest = policy.get("machine_digest")
    if machine_digest is None and isinstance(machine, dict):
        machine_digest = machine.get("machine_digest")
    if machine_digest != expected_machine_digest:
        raise LetsConfigError("trust_manifest_machine_mismatch")
    resources = policy.get("resources")
    if not isinstance(resources, list) or len(resources) != RESOURCE_DIMENSIONS:
        raise LetsConfigError("trust_manifest_resource_mismatch")
    max_ttl = policy.get("max_lease_ttl_ns")
    if (
        isinstance(max_ttl, bool)
        or not isinstance(max_ttl, int)
        or not 0 < max_ttl <= _MAX_RESOURCE
    ):
        raise LetsConfigError("invalid_signed_trust_manifest")
    return policy, max_ttl


def _configured(value: object | None) -> str:
    return "<configured>" if value is not None else "<unset>"


__all__ = (
    "AuthenticatedTrustManifest",
    "INITIAL_GOVERNED_COHORTS",
    "LETS_ENFORCEMENT_CONTRACT",
    "LETS_RECEIPT_WIRE_TYPE",
    "LETS_RELEASE",
    "LetsConfigError",
    "LetsConfigLoad",
    "LetsHostConfig",
    "LetsMode",
    "LetsReadiness",
    "ManifestAuthenticator",
    "SecretFileReference",
    "load_lets_config",
)
