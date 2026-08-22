"""Fail-closed LETS protected-executor boundary for generated agent runtimes.

Generated server and desktop runtimes use the same reviewed adapter.  It
implements only Astral's small, versioned transport envelope while delegating
receipt parsing, signature verification, clock checks, persistent replay
defense, and rollback protection to the public LETS v1.0.11 executor API.

Configuration is local operator state.  Raw receipts, operator keys, tool
arguments, and filesystem paths are never returned in exceptions or card
metadata.
"""
from __future__ import annotations

import atexit
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import threading
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from lets.canonical import b64url_decode, canonical_digest
from lets.crypto import PublicKeyRegistry
from lets.errors import (
    AuthorityAnchorTransportError,
    ClockUncertainError,
    PolicyError,
    ReplayError,
    SignatureError,
    StorageError,
    ValidationError,
)
from lets.executor import (
    ExecutorPolicy,
    ReceiptVerifier,
    SQLiteReceiptReplayStore,
    executor_replay_identity,
)
from lets.executor_authority import ProcessFileExecutorAuthorityAnchor
from lets.manifest import ClusterManifest
from lets.models import Receipt


LETS_CALLER_CAPABILITY: Final = "astraldeep.lets/v1"
PERMIT_TYPE: Final = "astraldeep.protected-permit/v1"
LETS_RELEASE: Final = "v1.0.11"
RECEIPT_WIRE_TYPE: Final = "lets.receipt/v1"
EVIDENCE_TYPE: Final = "astral.tool-effect/v1"
CONTEXT_TYPE: Final = "astral.protected-effect-context/v1"
OPERATOR_TRUST_TYPE: Final = "astraldeep.lets-operator-trust/v1"

_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_OPERATOR_TRUST_BYTES = 64 * 1024
_MAX_CANONICAL_BYTES = 65_536
_MAX_STRING_BYTES = 8_192
_MAX_KEY_BYTES = 128
_MAX_CONTAINER_ITEMS = 256
_MAX_DEPTH = 12
_MAX_NODES = 4_096
_MAX_RESOURCE = (1 << 63) - 1
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RAW_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_POSITIVE_INTEGER = re.compile(r"[1-9][0-9]*\Z")

_SCOPE_BINDINGS: Final = (
    ("tools:read", "astral.tools.read", "tool_read", 0),
    ("tools:write", "astral.tools.write", "tool_write", 1),
    ("tools:search", "astral.tools.search", "tool_search", 2),
    ("tools:system", "astral.tools.system", "tool_system", 3),
    ("tools:files", "astral.tools.files", "tool_files", 4),
    ("tools:execute", "astral.tools.execute", "tool_execute", 5),
)
_SCOPE_BY_NAME: Final = {item[0]: item for item in _SCOPE_BINDINGS}


def _scope_profile_sha256() -> str:
    entries = [
        {
            "scope": scope,
            "capability": capability,
            "transition": transition,
            "resource_dimension": dimension,
        }
        for scope, capability, transition, dimension in _SCOPE_BINDINGS
    ]
    raw = json.dumps(
        {"entries": entries, "profile": "astral.tools/v1"},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


SCOPE_PROFILE_SHA256: Final = _scope_profile_sha256()


class ProtectedExecutorError(RuntimeError):
    """A value-free denial safe to return across the MCP boundary."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class ProtectedExecutorConfigurationError(ProtectedExecutorError):
    """The local executor trust or identity posture is incomplete."""


@dataclass(frozen=True, slots=True)
class HostBinding:
    owner_id: str
    binding_id: str
    lease_id: str
    lineage_id: str
    agent_id: str
    runtime_id: str
    runtime_generation: int
    executor_audience: str


@dataclass(slots=True)
class ProtectedExecutorRuntime:
    """One process-lifetime executor verifier and its persistent authorities."""

    mode: str
    host: HostBinding | None = None
    verifier: ReceiptVerifier | None = field(default=None, repr=False)
    replay_store: SQLiteReceiptReplayStore | None = field(default=None, repr=False)
    authority_anchor: ProcessFileExecutorAuthorityAnchor | None = field(
        default=None, repr=False
    )
    _claim_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def requires_permit(self) -> bool:
        return self.mode == "enforce"

    def card_metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "contract": LETS_CALLER_CAPABILITY,
            "lets_release": LETS_RELEASE,
            "receipt_wire_type": RECEIPT_WIRE_TYPE,
            "mode": self.mode,
            "ready": self.mode != "enforce" or self.verifier is not None,
        }
        if self.host is not None:
            metadata["executor_audience"] = self.host.executor_audience
            metadata["runtime_id"] = self.host.runtime_id
            metadata["runtime_generation"] = self.host.runtime_generation
        return metadata

    def close(self) -> None:
        if self.authority_anchor is not None:
            self.authority_anchor.close()

    def verify_and_claim(
        self,
        *,
        metadata: object,
        final_arguments: Mapping[str, object],
        tool_id: str,
        tool_scope: str,
    ) -> None:
        """Validate the exact host/effect binding and atomically claim once."""

        if self.mode != "enforce" or self.host is None or self.verifier is None:
            raise ProtectedExecutorError("protected_executor_not_enforcing")
        envelope = _parse_permit(metadata)
        context = envelope["context"]
        receipt = envelope["receipt"]
        host = self.host
        try:
            scope, capability, transition, dimension = _SCOPE_BY_NAME[tool_scope]
        except (KeyError, TypeError):
            raise ProtectedExecutorError("unmapped_protected_tool_scope") from None

        expected_context = {
            "type",
            "operation_id",
            "agent_id",
            "runtime_id",
            "tool_id",
            "scope",
            "capability",
            "transition",
            "resource_dimension",
            "executor_audience",
            "channel",
            "audit_correlation_id",
            "scope_profile_sha256",
            "authorized_effect_sha256",
            "effect_sha256",
        }
        if set(context) != expected_context or context.get("type") != EVIDENCE_TYPE:
            raise ProtectedExecutorError("invalid_protected_context")
        checks = (
            (envelope["owner_id"], host.owner_id),
            (envelope["binding_id"], host.binding_id),
            (receipt.lease_id, host.lease_id),
            (receipt.lineage_id, host.lineage_id),
            (envelope["runtime_generation"], host.runtime_generation),
            (context.get("agent_id"), host.agent_id),
            (context.get("runtime_id"), host.runtime_id),
            (context.get("tool_id"), tool_id),
            (context.get("scope"), scope),
            (context.get("capability"), capability),
            (context.get("transition"), transition),
            (context.get("resource_dimension"), dimension),
            (context.get("scope_profile_sha256"), SCOPE_PROFILE_SHA256),
            (context.get("executor_audience"), host.executor_audience),
            (receipt.request_id, context.get("operation_id")),
            (receipt.subject_id, host.agent_id),
            (receipt.executor_audience, host.executor_audience),
            (receipt.transition, transition),
            (receipt.source_state, "ready"),
            (receipt.target_state, "ready"),
            (receipt.nonce, envelope["nonce"]),
            (receipt.resulting_sequence, envelope["expected_sequence"] + 1),
        )
        if any(actual != expected for actual, expected in checks):
            raise ProtectedExecutorError("executor_host_binding_mismatch")
        cost = tuple(receipt.cost)
        expected_cost = tuple(
            1 if index == dimension else 0
            for index in range(len(_SCOPE_BINDINGS))
        )
        if cost != expected_cost:
            raise ProtectedExecutorError("executor_cost_mismatch")
        if receipt.evidence_digest != canonical_digest(dict(context)):
            raise ProtectedExecutorError("executor_evidence_mismatch")
        recomputed = _recompute_effect_sha256(
            context,
            expected_sequence=envelope["expected_sequence"],
            nonce=envelope["nonce"],
        )
        if not hmac.compare_digest(recomputed, str(context.get("effect_sha256", ""))):
            raise ProtectedExecutorError("executor_effect_digest_mismatch")
        arguments_digest = hashlib.sha256(_stable_canonical_bytes(final_arguments)).hexdigest()
        if not hmac.compare_digest(arguments_digest, envelope["wire_arguments_sha256"]):
            raise ProtectedExecutorError("executor_arguments_mutated")

        # No asynchronous or remote operation belongs between this durable
        # claim and the actuator call in agent.dispatch().  The process lock
        # also preserves per-binding order when aiohttp workers overlap.
        with self._claim_lock:
            try:
                self.verifier.verify_and_claim(receipt)
            except ReplayError:
                raise ProtectedExecutorError("receipt_replayed") from None
            except ClockUncertainError:
                raise ProtectedExecutorError("clock_uncertain", retryable=True) from None
            except SignatureError:
                raise ProtectedExecutorError("receipt_signature_invalid") from None
            except PolicyError:
                raise ProtectedExecutorError("receipt_policy_invalid") from None
            except (StorageError, AuthorityAnchorTransportError):
                raise ProtectedExecutorError(
                    "replay_store_unavailable", retryable=True
                ) from None
            except (ValidationError, TypeError, ValueError):
                raise ProtectedExecutorError("receipt_invalid") from None


def _parse_permit(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtectedExecutorError("missing_protected_permit")
    expected = {
        "type",
        "binding_id",
        "owner_id",
        "runtime_generation",
        "context",
        "expected_sequence",
        "nonce",
        "wire_arguments_sha256",
        "receipt",
    }
    if set(value) != expected or value.get("type") != PERMIT_TYPE:
        raise ProtectedExecutorError("invalid_protected_permit")
    binding_id = _identifier_value(value.get("binding_id"), "invalid_binding_id")
    owner_id = _identifier_value(value.get("owner_id"), "invalid_owner_id")
    generation = value.get("runtime_generation")
    sequence = value.get("expected_sequence")
    nonce = value.get("nonce")
    wire_digest = value.get("wire_arguments_sha256")
    context = value.get("context")
    if type(generation) is not int or generation < 1:
        raise ProtectedExecutorError("invalid_runtime_generation")
    if type(sequence) is not int or sequence < 0:
        raise ProtectedExecutorError("invalid_expected_sequence")
    if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
        raise ProtectedExecutorError("invalid_nonce")
    if not isinstance(wire_digest, str) or _RAW_DIGEST.fullmatch(wire_digest) is None:
        raise ProtectedExecutorError("invalid_wire_arguments_digest")
    if not isinstance(context, Mapping):
        raise ProtectedExecutorError("invalid_protected_context")
    if any(
        not isinstance(key, str)
        or not isinstance(item, (str, int))
        or isinstance(item, bool)
        for key, item in context.items()
    ):
        raise ProtectedExecutorError("invalid_protected_context")
    raw_receipt = value.get("receipt")
    if not isinstance(raw_receipt, Mapping):
        raise ProtectedExecutorError("invalid_receipt")
    try:
        receipt = Receipt.from_dict(dict(raw_receipt))
    except (TypeError, ValueError, ValidationError):
        raise ProtectedExecutorError("invalid_receipt") from None
    return {
        "binding_id": binding_id,
        "owner_id": owner_id,
        "runtime_generation": generation,
        "context": dict(context),
        "expected_sequence": sequence,
        "nonce": nonce,
        "wire_arguments_sha256": wire_digest,
        "receipt": receipt,
    }


def _strict_json(path: Path, maximum: int, code: str) -> tuple[bytes, dict[str, Any]]:
    try:
        metadata = path.stat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > maximum
        ):
            raise OSError
        raw = path.read_bytes()
    except (OSError, ValueError):
        raise ProtectedExecutorConfigurationError(code) from None

    def pairs(items: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise ValueError
            result[key] = item
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError

    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
        raise ProtectedExecutorConfigurationError(code) from None
    if not isinstance(document, dict):
        raise ProtectedExecutorConfigurationError(code)
    return raw, document


def _operator_trust(path: Path) -> tuple[dict[str, bytes], int]:
    _raw, document = _strict_json(
        path, _MAX_OPERATOR_TRUST_BYTES, "invalid_operator_trust_bundle"
    )
    if set(document) != {"api_version", "threshold", "keys"}:
        raise ProtectedExecutorConfigurationError("invalid_operator_trust_bundle")
    threshold = document.get("threshold")
    keys = document.get("keys")
    if (
        document.get("api_version") != OPERATOR_TRUST_TYPE
        or type(threshold) is not int
        or threshold < 1
        or not isinstance(keys, list)
        or not 1 <= len(keys) <= 16
        or threshold > len(keys)
    ):
        raise ProtectedExecutorConfigurationError("invalid_operator_trust_bundle")
    trusted: dict[str, bytes] = {}
    materials: set[bytes] = set()
    for item in keys:
        if (
            not isinstance(item, dict)
            or set(item) != {"key_id", "algorithm", "public_key"}
            or item.get("algorithm") != "Ed25519"
            or not isinstance(item.get("key_id"), str)
            or not isinstance(item.get("public_key"), str)
        ):
            raise ProtectedExecutorConfigurationError("invalid_operator_trust_bundle")
        key_id = item["key_id"]
        try:
            public_key = b64url_decode(item["public_key"])
        except Exception:
            raise ProtectedExecutorConfigurationError(
                "invalid_operator_trust_bundle"
            ) from None
        if (
            not key_id
            or key_id in trusted
            or len(public_key) != 32
            or public_key in materials
        ):
            raise ProtectedExecutorConfigurationError("invalid_operator_trust_bundle")
        trusted[key_id] = public_key
        materials.add(public_key)
    return trusted, threshold


def _load_manifest(values: Mapping[str, str]) -> tuple[ClusterManifest, object, object]:
    manifest_path = _required_path(values, "LETS_SIGNED_TRUST_MANIFEST")
    operator_path = _required_path(values, "LETS_MANIFEST_OPERATOR_KEYS_FILE")
    _raw, document = _strict_json(
        manifest_path, _MAX_MANIFEST_BYTES, "invalid_signed_trust_manifest"
    )
    trusted, threshold = _operator_trust(operator_path)
    allow_insecure = values.get("ASTRAL_ENV", "").strip().lower() in {
        "dev",
        "development",
        "test",
    }
    try:
        manifest = ClusterManifest.from_dict(
            document, allow_insecure_http=allow_insecure
        )
        manifest.verify_signatures(trusted, threshold=threshold)
        warden = manifest.warden(_required(values, "LETS_WARDEN_ID"))
        policy_digest = _required_digest(values, "LETS_POLICY_DIGEST")
        machine_digest = _required_digest(values, "LETS_MACHINE_DIGEST")
        policies = tuple(item for item in manifest.policies if item.digest == policy_digest)
        if len(policies) != 1 or policies[0].machine.digest != machine_digest:
            raise ValueError
        policy = policies[0]
    except (SignatureError, ValidationError, KeyError, TypeError, ValueError):
        raise ProtectedExecutorConfigurationError(
            "trust_manifest_authentication_failed"
        ) from None
    if manifest.tenant_id != _required(values, "LETS_TENANT_ID"):
        raise ProtectedExecutorConfigurationError("trust_manifest_tenant_mismatch")
    if manifest.envelope_id != _required(values, "LETS_ENVELOPE_ID"):
        raise ProtectedExecutorConfigurationError("trust_manifest_envelope_mismatch")
    return manifest, warden, policy


def load_protected_executor(
    environ: Mapping[str, str] | None = None,
    *,
    agent_id: str,
) -> ProtectedExecutorRuntime:
    """Build one executor from local state, or a no-op off/shadow posture."""

    values = os.environ if environ is None else environ
    mode = values.get("LETS_MODE", "off")
    if mode not in {"off", "shadow", "enforce"}:
        raise ProtectedExecutorConfigurationError("invalid_lets_mode")
    if mode != "enforce":
        return ProtectedExecutorRuntime(mode=mode)
    if values.get("FF_LETS_EXTERNAL_WARDEN", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise ProtectedExecutorConfigurationError("master_flag_disabled")

    manifest, warden, policy_spec = _load_manifest(values)
    host = HostBinding(
        owner_id=_required_identifier(values, "ASTRAL_AUTHORITY_OWNER_ID"),
        binding_id=_required_identifier(values, "ASTRAL_AUTHORITY_BINDING_ID"),
        lease_id=_required_identifier(values, "ASTRAL_AUTHORITY_LEASE_ID"),
        lineage_id=_required_identifier(values, "ASTRAL_AUTHORITY_LINEAGE_ID"),
        agent_id=_identifier_value(agent_id, "invalid_agent_id"),
        runtime_id=_required_identifier(values, "ASTRAL_RUNTIME_ID"),
        runtime_generation=_required_positive_int(values, "ASTRAL_RUNTIME_GENERATION"),
        executor_audience=_required_identifier(values, "LETS_EXECUTOR_INSTANCE_ID"),
    )
    database_root = _required_directory(values, "LETS_EXECUTOR_DB_ROOT")
    authority_root = _optional_directory(values, "LETS_EXECUTOR_AUTHORITY_ROOT")
    production = values.get("ASTRAL_ENV", "").strip().lower() not in {
        "dev",
        "development",
        "test",
    }
    if production and authority_root is None:
        raise ProtectedExecutorConfigurationError("missing_executor_authority_root")
    if authority_root == database_root:
        raise ProtectedExecutorConfigurationError("executor_authority_not_independent")

    registry = PublicKeyRegistry()
    for key in warden.keys:
        registry.register(
            warden.warden_id,
            key.key_id,
            key.public_key,
            not_before_ns=key.not_before_ns,
            not_after_ns=key.not_after_ns,
        )
    policy = ExecutorPolicy(
        audience=host.executor_audience,
        tenant_id=manifest.tenant_id,
        envelope_id=manifest.envelope_id,
        config_epoch=manifest.config_epoch,
        allowed_policy_digests=frozenset({policy_spec.digest}),
        allowed_machine_digests=frozenset({policy_spec.machine.digest}),
        trusted_wardens=frozenset({warden.warden_id}),
        max_clock_uncertainty_ns=policy_spec.max_clock_uncertainty_ns,
    )
    instance_key = hashlib.sha256(host.executor_audience.encode("utf-8")).hexdigest()
    database_path = database_root / f"{instance_key}.sqlite3"
    authority_anchor: ProcessFileExecutorAuthorityAnchor | None = None
    try:
        if authority_root is not None:
            helper_command = (
                (sys.executable, "--lets-executor-authority-helper")
                if getattr(sys, "frozen", False)
                else None
            )
            authority_anchor = ProcessFileExecutorAuthorityAnchor(
                authority_root / f"{instance_key}.anchor",
                helper_command=helper_command,
            )
        if database_path.exists():
            replay_store = SQLiteReceiptReplayStore(
                database_path,
                authority_anchor=authority_anchor,
                allow_unanchored=authority_anchor is None,
            )
        else:
            replay_store = SQLiteReceiptReplayStore.initialize(
                database_path,
                authority_anchor=authority_anchor,
                allow_unanchored=authority_anchor is None,
                identity=executor_replay_identity(policy, registry),
            )
        verifier = ReceiptVerifier(registry, replay_store, policy)
    except Exception:
        if authority_anchor is not None:
            authority_anchor.close()
        raise ProtectedExecutorConfigurationError("executor_initialization_failed") from None
    runtime = ProtectedExecutorRuntime(
        mode=mode,
        host=host,
        verifier=verifier,
        replay_store=replay_store,
        authority_anchor=authority_anchor,
    )
    atexit.register(runtime.close)
    return runtime


def extract_permit(caller_capabilities: object) -> object | None:
    if not isinstance(caller_capabilities, Mapping):
        return None
    return caller_capabilities.get(LETS_CALLER_CAPABILITY)


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProtectedExecutorConfigurationError(f"missing_{name.lower()}")
    return value


def _identifier_value(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 512
        or any(character.isspace() or unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ProtectedExecutorError(code)
    return value


def _required_identifier(values: Mapping[str, str], name: str) -> str:
    try:
        return _identifier_value(_required(values, name), f"invalid_{name.lower()}")
    except ProtectedExecutorError as exc:
        raise ProtectedExecutorConfigurationError(exc.code) from None


def _required_positive_int(values: Mapping[str, str], name: str) -> int:
    value = _required(values, name)
    if _POSITIVE_INTEGER.fullmatch(value) is None or int(value) > _MAX_RESOURCE:
        raise ProtectedExecutorConfigurationError(f"invalid_{name.lower()}")
    return int(value)


def _required_digest(values: Mapping[str, str], name: str) -> str:
    value = _required(values, name)
    if _DIGEST.fullmatch(value) is None:
        raise ProtectedExecutorConfigurationError(f"invalid_{name.lower()}")
    return value


def _required_path(values: Mapping[str, str], name: str) -> Path:
    value = _required(values, name)
    path = Path(value)
    if not path.is_absolute():
        raise ProtectedExecutorConfigurationError(f"invalid_{name.lower()}")
    return path


def _required_directory(values: Mapping[str, str], name: str) -> Path:
    value = _required(values, name)
    try:
        path = Path(value)
        if not path.is_absolute():
            raise OSError
        path = path.resolve(strict=True)
    except (OSError, ValueError):
        raise ProtectedExecutorConfigurationError(f"invalid_{name.lower()}") from None
    if not path.is_dir():
        raise ProtectedExecutorConfigurationError(f"invalid_{name.lower()}")
    return path


def _optional_directory(values: Mapping[str, str], name: str) -> Path | None:
    if not values.get(name):
        return None
    return _required_directory(values, name)


def _canonical_string(value: str, *, maximum: int) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ProtectedExecutorError("noncanonical_protected_input")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ProtectedExecutorError("noncanonical_protected_input") from None
    if len(encoded) > maximum:
        raise ProtectedExecutorError("protected_input_too_large")
    return value


def _freeze_json(
    value: object, *, active: set[int], nodes: list[int], depth: int
) -> Any:
    nodes[0] += 1
    if nodes[0] > _MAX_NODES or depth > _MAX_DEPTH:
        raise ProtectedExecutorError("protected_input_too_large")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not -(2**63) <= value <= 2**63 - 1:
            raise ProtectedExecutorError("noncanonical_protected_input")
        return value
    if type(value) is float:
        raise ProtectedExecutorError("noncanonical_protected_input")
    if type(value) is str:
        return _canonical_string(value, maximum=_MAX_STRING_BYTES)
    if type(value) is dict:
        identity = id(value)
        if identity in active or len(value) > _MAX_CONTAINER_ITEMS:
            raise ProtectedExecutorError("noncanonical_protected_input")
        active.add(identity)
        try:
            keys = list(value)
            if not all(type(key) is str for key in keys):
                raise ProtectedExecutorError("noncanonical_protected_input")
            for key in keys:
                _canonical_string(key, maximum=_MAX_KEY_BYTES)
            return {
                key: _freeze_json(
                    value[key], active=active, nodes=nodes, depth=depth + 1
                )
                for key in sorted(keys)
            }
        finally:
            active.remove(identity)
    if type(value) is list:
        identity = id(value)
        if identity in active or len(value) > _MAX_CONTAINER_ITEMS:
            raise ProtectedExecutorError("noncanonical_protected_input")
        active.add(identity)
        try:
            return [
                _freeze_json(item, active=active, nodes=nodes, depth=depth + 1)
                for item in value
            ]
        finally:
            active.remove(identity)
    raise ProtectedExecutorError("noncanonical_protected_input")


def _canonical_bytes(value: object) -> bytes:
    frozen = _freeze_json(value, active=set(), nodes=[0], depth=0)
    encoded = json.dumps(
        frozen,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    if len(encoded) > _MAX_CANONICAL_BYTES:
        raise ProtectedExecutorError("protected_input_too_large")
    return encoded


def _stable_canonical_bytes(value: object) -> bytes:
    first = _canonical_bytes(value)
    second = _canonical_bytes(value)
    if first != second:
        raise ProtectedExecutorError("protected_input_mutated")
    return first


def _recompute_effect_sha256(
    evidence: Mapping[str, object], *, expected_sequence: int, nonce: str
) -> str:
    dimension = evidence.get("resource_dimension")
    if type(dimension) is not int or not 0 <= dimension < len(_SCOPE_BINDINGS):
        raise ProtectedExecutorError("invalid_protected_context")
    if type(expected_sequence) is not int or expected_sequence < 0:
        raise ProtectedExecutorError("invalid_protected_context")
    if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
        raise ProtectedExecutorError("invalid_protected_context")
    unit_cost = [0] * len(_SCOPE_BINDINGS)
    unit_cost[dimension] = 1
    document = {
        "type": CONTEXT_TYPE,
        "operation_id": evidence["operation_id"],
        "agent_id": evidence["agent_id"],
        "runtime_id": evidence["runtime_id"],
        "tool_id": evidence["tool_id"],
        "scope": evidence["scope"],
        "capability": evidence["capability"],
        "transition": evidence["transition"],
        "resource_dimension": dimension,
        "unit_cost": unit_cost,
        "executor_audience": evidence["executor_audience"],
        "channel": evidence["channel"],
        "audit_correlation_id": evidence["audit_correlation_id"],
        "expected_sequence": expected_sequence,
        "nonce": nonce,
        "authorized_effect_sha256": evidence["authorized_effect_sha256"],
        "scope_profile_sha256": evidence["scope_profile_sha256"],
    }
    return hashlib.sha256(_canonical_bytes(document)).hexdigest()


__all__ = (
    "LETS_CALLER_CAPABILITY",
    "LETS_RELEASE",
    "ProtectedExecutorConfigurationError",
    "ProtectedExecutorError",
    "ProtectedExecutorRuntime",
    "SCOPE_PROFILE_SHA256",
    "extract_permit",
    "load_protected_executor",
)
