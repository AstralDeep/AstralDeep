"""Canonical, content-free evidence for LETS-protected tool effects.

Raw post-rewrite arguments remain local. They are guarded by a per-context keyed
mutation snapshot, while LETS-bound evidence is derived only from a reviewed,
content-free authorization projection.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from .lets_scope_profile import (
    SCOPE_PROFILE_SHA256,
    ScopeProfileError,
    binding_for_scope,
)

EVIDENCE_TYPE: Final = "astral.tool-effect/v1"
CONTEXT_TYPE: Final = "astral.protected-effect-context/v1"
NONCE_BYTES: Final = 16
SNAPSHOT_KEY_BYTES: Final = 32
MAX_CANONICAL_BYTES: Final = 65_536
MAX_STRING_BYTES: Final = 8_192
MAX_KEY_BYTES: Final = 128
MAX_CONTAINER_ITEMS: Final = 256
MAX_DEPTH: Final = 12
MAX_NODES: Final = 4_096
MAX_IDENTIFIER_BYTES: Final = 512

PROTECTED_CHANNELS: Final = frozenset(
    {"rest", "websocket", "a2a", "mcp", "background", "scheduled", "chained", "stream"}
)

_CLASSIFICATION = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_EFFECT_CLASSES: Final = frozenset(
    {"read", "write", "search", "system", "files", "execute"}
)
_AUTHORIZED_CLASSIFICATIONS: Final = MappingProxyType(
    {
        "target_class": frozenset(
            {
                "agent",
                "database",
                "endpoint",
                "filesystem",
                "local_runtime",
                "process",
                "stream",
                "workspace",
            }
        ),
        "data_classification": frozenset(
            {"public", "internal", "confidential", "restricted", "phi"}
        ),
        "egress_class": frozenset({"none", "internal", "approved_external"}),
        "credential_class": frozenset(
            {"none", "delegated_user", "service_identity", "system_identity"}
        ),
        "idempotency_class": frozenset(
            {"none", "read_only", "domain_key", "transactional"}
        ),
        "confirmation_class": frozenset({"not_required", "confirmed"}),
    }
)
_AUTHORIZED_BOOLEANS: Final = frozenset(
    {"writes_state", "network_effect", "external_actuator"}
)
# These digests bind reviewed, non-content identities. Callers must never hash
# prompts, PHI, credential values, or arbitrary user input into these fields.
_AUTHORIZED_BINDING_DIGESTS: Final = frozenset(
    {"target_binding_sha256", "executor_binding_sha256"}
)
AUTHORIZED_EFFECT_FIELDS: Final = frozenset(
    {"effect_class"}
    | set(_AUTHORIZED_CLASSIFICATIONS)
    | set(_AUTHORIZED_BOOLEANS)
    | set(_AUTHORIZED_BINDING_DIGESTS)
)


class ProtectedDispatchError(ValueError):
    """A protected effect cannot be represented without ambiguity or disclosure."""


def _canonical_string(
    value: str, label: str, *, maximum: int = MAX_STRING_BYTES
) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ProtectedDispatchError(f"{label} must use canonical NFC text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ProtectedDispatchError(f"{label} contains invalid Unicode") from exc
    if len(encoded) > maximum:
        raise ProtectedDispatchError(f"{label} exceeds the canonical size limit")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProtectedDispatchError(f"{label} must be one canonical non-empty string")
    checked = _canonical_string(value, label, maximum=MAX_IDENTIFIER_BYTES)
    if any(
        character.isspace() or unicodedata.category(character).startswith("C")
        for character in checked
    ):
        raise ProtectedDispatchError(
            f"{label} must not contain whitespace or control characters"
        )
    return checked


def _freeze_json(
    value: object,
    *,
    active: set[int],
    nodes: list[int],
    depth: int,
) -> Any:
    nodes[0] += 1
    if nodes[0] > MAX_NODES:
        raise ProtectedDispatchError("canonical input contains too many values")
    if depth > MAX_DEPTH:
        raise ProtectedDispatchError("canonical input is nested too deeply")

    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not -(2**63) <= value <= 2**63 - 1:
            raise ProtectedDispatchError(
                "canonical integers must fit signed 64-bit range"
            )
        return value
    if type(value) is float:
        raise ProtectedDispatchError(
            "floating-point values are not canonical protected input"
        )
    if type(value) is str:
        return _canonical_string(value, "canonical string")

    if type(value) is dict:
        identity = id(value)
        if identity in active:
            raise ProtectedDispatchError("cyclic protected input is not canonical")
        if len(value) > MAX_CONTAINER_ITEMS:
            raise ProtectedDispatchError("canonical mapping contains too many entries")
        active.add(identity)
        try:
            keys = list(value)
            if not all(type(key) is str for key in keys):
                raise ProtectedDispatchError("canonical mapping keys must be strings")
            for key in keys:
                _canonical_string(key, "canonical mapping key", maximum=MAX_KEY_BYTES)
            frozen = {
                key: _freeze_json(
                    value[key],
                    active=active,
                    nodes=nodes,
                    depth=depth + 1,
                )
                for key in sorted(keys)
            }
        except (KeyError, RuntimeError) as exc:
            raise ProtectedDispatchError(
                "protected input mutated during snapshot"
            ) from exc
        finally:
            active.remove(identity)
        return frozen

    if type(value) is list:
        identity = id(value)
        if identity in active:
            raise ProtectedDispatchError("cyclic protected input is not canonical")
        if len(value) > MAX_CONTAINER_ITEMS:
            raise ProtectedDispatchError("canonical sequence contains too many entries")
        active.add(identity)
        try:
            frozen = [
                _freeze_json(
                    item,
                    active=active,
                    nodes=nodes,
                    depth=depth + 1,
                )
                for item in value
            ]
        except RuntimeError as exc:
            raise ProtectedDispatchError(
                "protected input mutated during snapshot"
            ) from exc
        finally:
            active.remove(identity)
        return frozen

    raise ProtectedDispatchError(
        "protected input contains a noncanonical or unordered value"
    )


def _canonical_bytes(value: object) -> bytes:
    try:
        frozen = _freeze_json(value, active=set(), nodes=[0], depth=0)
        encoded = json.dumps(
            frozen,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (RecursionError, ValueError) as exc:
        if isinstance(exc, ProtectedDispatchError):
            raise
        raise ProtectedDispatchError("protected input cannot be canonicalized") from exc
    if len(encoded) > MAX_CANONICAL_BYTES:
        raise ProtectedDispatchError("canonical protected input exceeds the byte limit")
    return encoded


def _stable_canonical_bytes(value: object) -> bytes:
    first = _canonical_bytes(value)
    second = _canonical_bytes(value)
    if first != second:
        raise ProtectedDispatchError("protected input mutated during snapshot")
    return first


def _authorized_effect_digest(value: object, *, expected_effect_class: str) -> str:
    if type(value) is not dict or not value:
        raise ProtectedDispatchError(
            "authorized effect must be one non-empty canonical mapping"
        )
    if set(value) - AUTHORIZED_EFFECT_FIELDS:
        raise ProtectedDispatchError(
            "authorized effect contains an unsafe or unreviewed field"
        )
    if value.get("effect_class") != expected_effect_class:
        raise ProtectedDispatchError(
            "authorized effect class must match the reviewed scope"
        )

    checked: dict[str, str | bool] = {"effect_class": expected_effect_class}
    for key, item in value.items():
        if key == "effect_class":
            continue
        if key in _AUTHORIZED_CLASSIFICATIONS:
            if type(item) is not str or item not in _AUTHORIZED_CLASSIFICATIONS[key]:
                raise ProtectedDispatchError(
                    f"authorized effect {key} is not an approved classification"
                )
            checked[key] = item
        elif key in _AUTHORIZED_BOOLEANS:
            if type(item) is not bool:
                raise ProtectedDispatchError(f"authorized effect {key} must be boolean")
            checked[key] = item
        elif key in _AUTHORIZED_BINDING_DIGESTS:
            if type(item) is not str or _SHA256.fullmatch(item) is None:
                raise ProtectedDispatchError(
                    f"authorized effect {key} must be one lowercase non-content SHA-256"
                )
            checked[key] = item
        else:  # Defensive if the reviewed field sets ever drift apart.
            raise ProtectedDispatchError(
                "authorized effect field has no reviewed validator"
            )
    return hashlib.sha256(_stable_canonical_bytes(checked)).hexdigest()


def _effect_digest(document: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_bytes(dict(document))).hexdigest()


class _LocalArgumentSnapshot:
    """Non-exportable, per-context HMAC for local mutation detection only."""

    __slots__ = ("__key", "__mac")

    def __init__(self, key: bytes, canonical_arguments: bytes) -> None:
        self.__key = key
        self.__mac = hmac.digest(key, canonical_arguments, "sha256")

    def matches(self, final_arguments: object) -> bool:
        candidate = hmac.digest(
            self.__key, _stable_canonical_bytes(final_arguments), "sha256"
        )
        return hmac.compare_digest(candidate, self.__mac)

    def __repr__(self) -> str:
        return "<opaque-local-argument-snapshot>"

    def __copy__(self) -> None:
        raise ProtectedDispatchError("local argument snapshots are not exportable")

    def __deepcopy__(self, _memo: object) -> None:
        raise ProtectedDispatchError("local argument snapshots are not exportable")

    def __reduce__(self) -> None:
        raise ProtectedDispatchError("local argument snapshots are not persistable")


@dataclass(frozen=True, slots=True)
class ProtectedDispatchContext:
    """Immutable host binding and content-free LETS evidence for one effect attempt."""

    operation_id: str
    agent_id: str
    runtime_id: str
    tool_id: str
    scope: str
    capability: str
    transition: str
    resource_dimension: int
    executor_audience: str
    channel: str
    audit_correlation_id: str
    expected_sequence: int
    nonce: str
    authorized_effect_sha256: str
    effect_sha256: str
    # Executor-only mutation fence.  This value is transported only alongside
    # the arguments to the actuator; it is deliberately excluded from
    # ``lets_evidence`` so user content, PHI, and credential-derived bytes are
    # never disclosed to the warden.
    wire_arguments_sha256: str
    _local_arguments: _LocalArgumentSnapshot = field(repr=False, compare=False)

    def lets_evidence(self) -> Mapping[str, str | int]:
        """Return immutable LETS evidence containing no arguments or raw descriptors."""

        return MappingProxyType(
            {
                "type": EVIDENCE_TYPE,
                "operation_id": self.operation_id,
                "agent_id": self.agent_id,
                "runtime_id": self.runtime_id,
                "tool_id": self.tool_id,
                "scope": self.scope,
                "capability": self.capability,
                "transition": self.transition,
                "resource_dimension": self.resource_dimension,
                "executor_audience": self.executor_audience,
                "channel": self.channel,
                "audit_correlation_id": self.audit_correlation_id,
                "scope_profile_sha256": SCOPE_PROFILE_SHA256,
                "authorized_effect_sha256": self.authorized_effect_sha256,
                "effect_sha256": self.effect_sha256,
            }
        )

    def assert_snapshot_matches(
        self,
        *,
        final_arguments: Mapping[str, object],
        authorized_effect: Mapping[str, object],
    ) -> None:
        """Fail closed if mutable arguments or the safe projection changed after snapshot."""

        if type(final_arguments) is not dict:
            raise ProtectedDispatchError(
                "final arguments must be one canonical mapping snapshot"
            )
        current_authorized_sha256 = _authorized_effect_digest(
            authorized_effect,
            expected_effect_class=self.scope.removeprefix("tools:"),
        )
        if not self._local_arguments.matches(
            final_arguments
        ) or not hmac.compare_digest(
            current_authorized_sha256, self.authorized_effect_sha256
        ):
            raise ProtectedDispatchError(
                "protected effect mutated after its canonical snapshot"
            )


def build_protected_dispatch_context(
    *,
    operation_id: str,
    agent_id: str,
    runtime_id: str,
    tool_id: str,
    scope: str,
    executor_audience: str,
    channel: str,
    audit_correlation_id: str,
    expected_sequence: int,
    final_arguments: Mapping[str, object],
    authorized_effect: Mapping[str, object],
    nonce: str | None = None,
) -> ProtectedDispatchContext:
    """Snapshot one rewritten effect and return strictly content-free LETS evidence."""

    operation = _identifier(operation_id, "operation ID")
    agent = _identifier(agent_id, "agent ID")
    runtime = _identifier(runtime_id, "runtime ID")
    tool = _identifier(tool_id, "tool ID")
    audience = _identifier(executor_audience, "executor audience")
    correlation = _identifier(audit_correlation_id, "audit correlation ID")
    if not isinstance(channel, str) or channel not in PROTECTED_CHANNELS:
        raise ProtectedDispatchError("unknown protected dispatch channel")
    if type(expected_sequence) is not int or expected_sequence < 0:
        raise ProtectedDispatchError("expected sequence must be a non-negative integer")
    if type(final_arguments) is not dict:
        raise ProtectedDispatchError(
            "final arguments must be one canonical mapping snapshot"
        )
    try:
        binding = binding_for_scope(scope)
    except ScopeProfileError as exc:
        raise ProtectedDispatchError("unknown protected tool scope") from exc

    canonical_arguments = _stable_canonical_bytes(final_arguments)
    wire_arguments_sha256 = hashlib.sha256(canonical_arguments).hexdigest()
    authorized_effect_sha256 = _authorized_effect_digest(
        authorized_effect,
        expected_effect_class=binding.scope.removeprefix("tools:"),
    )
    nonce_bytes = secrets.token_bytes(NONCE_BYTES) if nonce is None else None
    snapshot_key = secrets.token_bytes(SNAPSHOT_KEY_BYTES)
    if nonce is None:
        if type(nonce_bytes) is not bytes or len(nonce_bytes) != NONCE_BYTES:
            raise ProtectedDispatchError("secure nonce generation failed")
        protected_nonce = nonce_bytes.hex()
    elif not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
        raise ProtectedDispatchError(
            "supplied nonce must encode exactly 128 bits as lowercase hexadecimal"
        )
    else:
        protected_nonce = nonce
    if type(snapshot_key) is not bytes or len(snapshot_key) != SNAPSHOT_KEY_BYTES:
        raise ProtectedDispatchError("secure local snapshot generation failed")

    digest_document: dict[str, object] = {
        "type": CONTEXT_TYPE,
        "operation_id": operation,
        "agent_id": agent,
        "runtime_id": runtime,
        "tool_id": tool,
        "scope": binding.scope,
        "capability": binding.capability,
        "transition": binding.transition,
        "resource_dimension": binding.resource_dimension,
        "unit_cost": list(binding.unit_cost()),
        "executor_audience": audience,
        "channel": channel,
        "audit_correlation_id": correlation,
        "expected_sequence": expected_sequence,
        "nonce": protected_nonce,
        "authorized_effect_sha256": authorized_effect_sha256,
        "scope_profile_sha256": SCOPE_PROFILE_SHA256,
    }
    effect_sha256 = _effect_digest(digest_document)
    return ProtectedDispatchContext(
        operation_id=operation,
        agent_id=agent,
        runtime_id=runtime,
        tool_id=tool,
        scope=binding.scope,
        capability=binding.capability,
        transition=binding.transition,
        resource_dimension=binding.resource_dimension,
        executor_audience=audience,
        channel=channel,
        audit_correlation_id=correlation,
        expected_sequence=expected_sequence,
        nonce=protected_nonce,
        authorized_effect_sha256=authorized_effect_sha256,
        effect_sha256=effect_sha256,
        wire_arguments_sha256=wire_arguments_sha256,
        _local_arguments=_LocalArgumentSnapshot(snapshot_key, canonical_arguments),
    )


def canonical_wire_arguments_sha256(final_arguments: Mapping[str, object]) -> str:
    """Return the executor-local digest for the exact transported arguments.

    The digest is not LETS evidence and must not be logged or sent to the
    warden.  It exists so a remote protected executor can recompute the same
    mutation fence immediately before invoking its actuator.
    """

    if type(final_arguments) is not dict:
        raise ProtectedDispatchError(
            "final arguments must be one canonical mapping snapshot"
        )
    return hashlib.sha256(_stable_canonical_bytes(final_arguments)).hexdigest()


def recompute_effect_sha256_from_evidence(
    evidence: Mapping[str, object],
    *,
    expected_sequence: int,
    nonce: str,
) -> str:
    """Recompute the protected-context digest at a remote executor.

    ``lets_evidence`` intentionally omits the receipt nonce and expected
    sequence because LETS receives those as first-class authorization fields.
    The permit transport supplies them separately so the executor can bind the
    signed receipt back to the exact canonical context.
    """

    if not isinstance(evidence, Mapping):
        raise ProtectedDispatchError("protected evidence must be a mapping")
    dimension = evidence.get("resource_dimension")
    if type(dimension) is not int or not 0 <= dimension < 6:
        raise ProtectedDispatchError("protected resource dimension is invalid")
    if type(expected_sequence) is not int or expected_sequence < 0:
        raise ProtectedDispatchError("expected sequence must be non-negative")
    checked_nonce = _identifier(nonce, "nonce")
    required = {
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
    if set(evidence) != required or evidence.get("type") != EVIDENCE_TYPE:
        raise ProtectedDispatchError("protected evidence shape is invalid")
    unit_cost = [0] * 6
    unit_cost[dimension] = 1
    document: dict[str, object] = {
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
        "nonce": checked_nonce,
        "authorized_effect_sha256": evidence["authorized_effect_sha256"],
        "scope_profile_sha256": evidence["scope_profile_sha256"],
    }
    return _effect_digest(document)


__all__ = (
    "AUTHORIZED_EFFECT_FIELDS",
    "CONTEXT_TYPE",
    "EVIDENCE_TYPE",
    "NONCE_BYTES",
    "PROTECTED_CHANNELS",
    "ProtectedDispatchContext",
    "ProtectedDispatchError",
    "build_protected_dispatch_context",
    "canonical_wire_arguments_sha256",
    "recompute_effect_sha256_from_evidence",
)
