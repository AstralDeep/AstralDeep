"""Canonicalization and privacy-boundary tests for protected dispatch."""

from __future__ import annotations

import copy
import dataclasses
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from orchestrator import protected_dispatch as protected  # noqa: E402
from orchestrator.lets_scope_profile import SCOPE_BINDINGS, SCOPE_PROFILE_SHA256  # noqa: E402

AUTHORIZED = {
    "effect_class": "read",
    "target_class": "database",
    "data_classification": "phi",
    "egress_class": "none",
    "credential_class": "service_identity",
    "idempotency_class": "read_only",
    "confirmation_class": "confirmed",
    "writes_state": False,
    "network_effect": False,
    "external_actuator": False,
    "target_binding_sha256": "a" * 64,
}

BASE = {
    "operation_id": "operation-123",
    "agent_id": "agent-123",
    "runtime_id": "runtime-7",
    "tool_id": "clinical.search_v2",
    "scope": "tools:read",
    "executor_audience": "astral-gateway-1",
    "channel": "rest",
    "audit_correlation_id": "audit-123",
    "expected_sequence": 41,
    "final_arguments": {"limit": 5, "filters": ["active", "recent"]},
    "authorized_effect": AUTHORIZED,
}


@pytest.fixture(autouse=True)
def fixed_randomness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protected.secrets, "token_bytes", lambda size: b"\xa5" * size)


def _build(**changes: object) -> protected.ProtectedDispatchContext:
    values = BASE | changes
    return protected.build_protected_dispatch_context(**values)  # type: ignore[arg-type]


def test_lets_evidence_is_exact_immutable_and_content_free() -> None:
    context = _build()
    evidence = context.lets_evidence()
    assert evidence == {
        "type": "astral.tool-effect/v1",
        "operation_id": "operation-123",
        "agent_id": "agent-123",
        "runtime_id": "runtime-7",
        "tool_id": "clinical.search_v2",
        "scope": "tools:read",
        "capability": "astral.tools.read",
        "transition": "tool_read",
        "resource_dimension": 0,
        "executor_audience": "astral-gateway-1",
        "channel": "rest",
        "audit_correlation_id": "audit-123",
        "scope_profile_sha256": SCOPE_PROFILE_SHA256,
        "authorized_effect_sha256": context.authorized_effect_sha256,
        "effect_sha256": context.effect_sha256,
    }
    assert set(evidence) <= {
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
    with pytest.raises(TypeError):
        evidence["effect_sha256"] = "0" * 64  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.tool_id = "changed"  # type: ignore[misc]


def test_raw_secrets_phi_and_user_content_never_influence_lets_evidence() -> None:
    first_arguments = {
        "api_token": "token-super-secret-123",
        "patient_note": "Patient Jane Doe has a private diagnosis",
        "query": "user supplied search content",
    }
    changed_arguments = {
        "api_token": "different-secret",
        "patient_note": "Different patient and diagnosis",
        "query": "different user content",
    }
    first = _build(final_arguments=first_arguments)
    changed = _build(final_arguments=changed_arguments)

    assert first.effect_sha256 == changed.effect_sha256
    assert first.authorized_effect_sha256 == changed.authorized_effect_sha256
    assert dict(first.lets_evidence()) == dict(changed.lets_evidence())
    serialized = repr(first) + repr(dict(first.lets_evidence()))
    assert "token-super-secret-123" not in serialized
    assert "Patient Jane Doe" not in serialized
    assert "user supplied search content" not in serialized
    assert "_local_arguments" not in repr(first)

    first.assert_snapshot_matches(
        final_arguments=first_arguments,
        authorized_effect=AUTHORIZED,
    )
    with pytest.raises(protected.ProtectedDispatchError, match="mutated after"):
        first.assert_snapshot_matches(
            final_arguments=changed_arguments,
            authorized_effect=AUTHORIZED,
        )


def test_local_argument_snapshot_cannot_be_copied_persisted_or_asdict_exported() -> (
    None
):
    context = _build(final_arguments={"api_token": "low-entropy-secret"})
    with pytest.raises(protected.ProtectedDispatchError, match="not exportable"):
        copy.copy(context._local_arguments)  # noqa: SLF001
    with pytest.raises(protected.ProtectedDispatchError, match="not exportable"):
        copy.deepcopy(context._local_arguments)  # noqa: SLF001
    with pytest.raises(protected.ProtectedDispatchError, match="not exportable"):
        dataclasses.asdict(context)
    with pytest.raises(protected.ProtectedDispatchError, match="not persistable"):
        context._local_arguments.__reduce__()  # noqa: SLF001


def test_canonical_order_is_stable_for_arguments_and_authorized_projection() -> None:
    arguments = {"alpha": 1, "nested": {"x": True, "y": None}}
    reordered_arguments = {"nested": {"y": None, "x": True}, "alpha": 1}
    authorized = dict(reversed(list(AUTHORIZED.items())))
    first = _build(final_arguments=arguments)
    reordered = _build(
        final_arguments=reordered_arguments,
        authorized_effect=authorized,
    )
    assert first.effect_sha256 == reordered.effect_sha256
    assert first.authorized_effect_sha256 == reordered.authorized_effect_sha256
    first.assert_snapshot_matches(
        final_arguments=reordered_arguments,
        authorized_effect=authorized,
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("operation_id", "operation-456"),
        ("agent_id", "agent-456"),
        ("runtime_id", "runtime-8"),
        ("tool_id", "clinical.write_v2"),
        ("executor_audience", "astral-gateway-2"),
        ("channel", "mcp"),
        ("audit_correlation_id", "audit-456"),
        ("expected_sequence", 42),
    ],
)
def test_every_host_binding_changes_the_effect_digest(
    field: str, replacement: object
) -> None:
    assert _build().effect_sha256 != _build(**{field: replacement}).effect_sha256


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("target_class", "endpoint"),
        ("data_classification", "restricted"),
        ("egress_class", "approved_external"),
        ("credential_class", "delegated_user"),
        ("idempotency_class", "domain_key"),
        ("confirmation_class", "not_required"),
        ("writes_state", True),
        ("network_effect", True),
        ("external_actuator", True),
        ("target_binding_sha256", "b" * 64),
        ("executor_binding_sha256", "c" * 64),
    ],
)
def test_each_safe_authorized_descriptor_changes_lets_effect(
    key: str, value: object
) -> None:
    changed = AUTHORIZED | {key: value}
    assert _build().effect_sha256 != _build(authorized_effect=changed).effect_sha256


def test_authorized_projection_mutation_is_detected() -> None:
    authorized = dict(AUTHORIZED)
    context = _build(authorized_effect=authorized)
    authorized["egress_class"] = "approved_external"
    with pytest.raises(protected.ProtectedDispatchError, match="mutated after"):
        context.assert_snapshot_matches(
            final_arguments=BASE["final_arguments"],  # type: ignore[arg-type]
            authorized_effect=authorized,
        )


def test_all_six_scopes_reuse_the_exact_reviewed_bindings() -> None:
    for binding in SCOPE_BINDINGS:
        effect_class = binding.scope.removeprefix("tools:")
        context = _build(
            scope=binding.scope,
            authorized_effect=AUTHORIZED | {"effect_class": effect_class},
        )
        assert (
            context.scope,
            context.capability,
            context.transition,
            context.resource_dimension,
        ) == (
            binding.scope,
            binding.capability,
            binding.transition,
            binding.resource_dimension,
        )


@pytest.mark.parametrize("scope", ["tools:admin", "", " tools:read", 7])
def test_unknown_or_noncanonical_scope_fails_closed(scope: object) -> None:
    with pytest.raises(
        protected.ProtectedDispatchError, match="unknown protected tool scope"
    ):
        _build(scope=scope)


@pytest.mark.parametrize("channel", sorted(protected.PROTECTED_CHANNELS))
def test_exact_dispatch_channels_are_accepted(channel: str) -> None:
    assert _build(channel=channel).channel == channel


@pytest.mark.parametrize("channel", ["internal", "parallel", "REST", " rest", "", None])
def test_unknown_dispatch_channel_fails_closed(channel: object) -> None:
    with pytest.raises(
        protected.ProtectedDispatchError, match="unknown protected dispatch channel"
    ):
        _build(channel=channel)


def test_nonce_uses_128_bits_and_local_snapshot_uses_an_independent_256_bit_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[int] = []

    def generate(size: int) -> bytes:
        requested.append(size)
        return bytes(range(size))

    monkeypatch.setattr(protected.secrets, "token_bytes", generate)
    context = _build()
    assert requested == [protected.NONCE_BYTES, protected.SNAPSHOT_KEY_BYTES]
    assert protected.NONCE_BYTES >= 16
    assert protected.SNAPSHOT_KEY_BYTES >= 32
    assert context.nonce == bytes(range(protected.NONCE_BYTES)).hex()


def test_retry_attempt_can_supply_its_exact_128_bit_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[int] = []

    def generate(size: int) -> bytes:
        requested.append(size)
        return b"k" * size

    monkeypatch.setattr(protected.secrets, "token_bytes", generate)
    nonce = "0123456789abcdef" * 2
    context = _build(nonce=nonce)

    assert context.nonce == nonce
    assert requested == [protected.SNAPSHOT_KEY_BYTES]


@pytest.mark.parametrize(
    "nonce",
    ["", "a" * 31, "a" * 33, "A" * 32, "g" * 32, b"a" * 32],
)
def test_supplied_nonce_must_be_exact_lowercase_128_bit_hex(nonce: object) -> None:
    with pytest.raises(protected.ProtectedDispatchError, match="128 bits"):
        _build(nonce=nonce)


@pytest.mark.parametrize("generated", [b"short", b"x" * 17, "x" * 16])
def test_broken_nonce_generator_fails_closed(
    monkeypatch: pytest.MonkeyPatch, generated: object
) -> None:
    monkeypatch.setattr(protected.secrets, "token_bytes", lambda _size: generated)
    with pytest.raises(
        protected.ProtectedDispatchError, match="nonce generation failed"
    ):
        _build()


@pytest.mark.parametrize("generated", [b"short", b"x" * 31, "x" * 32])
def test_broken_snapshot_key_generator_fails_closed(
    monkeypatch: pytest.MonkeyPatch, generated: object
) -> None:
    def token_bytes(size: int) -> object:
        return b"n" * size if size == protected.NONCE_BYTES else generated

    monkeypatch.setattr(protected.secrets, "token_bytes", token_bytes)
    with pytest.raises(
        protected.ProtectedDispatchError, match="snapshot generation failed"
    ):
        _build()


@pytest.mark.parametrize(
    "arguments",
    [
        {"value": 1.5},
        {"value": float("nan")},
        {"value": {"unordered"}},
        {"value": ("tuple",)},
        {"value": b"bytes"},
        {"value": object()},
        {1: "non-string key"},
        {"value": 2**63},
        {"value": "e\u0301"},
        {"value": "\ud800"},
    ],
)
def test_noncanonical_argument_values_fail_closed(arguments: object) -> None:
    with pytest.raises(protected.ProtectedDispatchError):
        _build(final_arguments=arguments)


def test_cycles_fail_closed() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(protected.ProtectedDispatchError, match="cyclic"):
        _build(final_arguments={"cycle": cyclic})


@pytest.mark.parametrize(
    "arguments",
    [
        {"huge": "x" * (protected.MAX_STRING_BYTES + 1)},
        {"many": list(range(protected.MAX_CONTAINER_ITEMS + 1))},
        {f"key_{index}": index for index in range(protected.MAX_CONTAINER_ITEMS + 1)},
        {"total": ["x" * 300 for _ in range(protected.MAX_CONTAINER_ITEMS)]},
        {"nodes": [list(range(protected.MAX_CONTAINER_ITEMS)) for _ in range(20)]},
    ],
)
def test_oversized_inputs_fail_closed(arguments: dict[str, object]) -> None:
    with pytest.raises(protected.ProtectedDispatchError, match="too many|exceeds"):
        _build(final_arguments=arguments)


def test_excessive_depth_fails_closed() -> None:
    nested: object = "leaf"
    for _ in range(protected.MAX_DEPTH + 2):
        nested = [nested]
    with pytest.raises(protected.ProtectedDispatchError, match="deeply"):
        _build(final_arguments={"nested": nested})


@pytest.mark.parametrize(
    "unsafe_key",
    [
        "api_token",
        "password_sha256",
        "credential_sha256",
        "content_sha256",
        "patient_digest",
        "prompt",
        "user_text",
        "arguments_sha256",
    ],
)
def test_secret_content_and_unreviewed_authorized_fields_fail_closed(
    unsafe_key: str,
) -> None:
    with pytest.raises(protected.ProtectedDispatchError, match="unsafe|unreviewed"):
        _build(authorized_effect=AUTHORIZED | {unsafe_key: "a" * 64})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("effect_class", "write"),
        ("target_class", "patient_name"),
        ("data_classification", "diagnosis"),
        ("egress_class", "arbitrary_url"),
        ("credential_class", "raw_token"),
        ("idempotency_class", "maybe"),
        ("confirmation_class", "user_said_yes"),
        ("writes_state", 1),
        ("target_binding_sha256", "not-a-digest"),
    ],
)
def test_authorized_values_must_match_reviewed_classifications(
    key: str, value: object
) -> None:
    with pytest.raises(protected.ProtectedDispatchError, match="authorized effect"):
        _build(authorized_effect=AUTHORIZED | {key: value})


@pytest.mark.parametrize("authorized", [{}, [], None, {"target_class": "database"}])
def test_authorized_effect_must_be_nonempty_and_scope_bound(authorized: object) -> None:
    with pytest.raises(protected.ProtectedDispatchError, match="authorized effect"):
        _build(authorized_effect=authorized)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation_id", ""),
        ("agent_id", " agent"),
        ("runtime_id", "runtime id"),
        ("tool_id", "tool\nname"),
        ("executor_audience", "audience\x00"),
        ("audit_correlation_id", "x" * (protected.MAX_IDENTIFIER_BYTES + 1)),
    ],
)
def test_noncanonical_identifiers_fail_closed(field: str, value: str) -> None:
    with pytest.raises(protected.ProtectedDispatchError):
        _build(**{field: value})


@pytest.mark.parametrize("sequence", [-1, True, 1.5, 2**63])
def test_noncanonical_expected_sequence_fails_closed(sequence: object) -> None:
    with pytest.raises(protected.ProtectedDispatchError):
        _build(expected_sequence=sequence)


@pytest.mark.parametrize("arguments", [[], None])
def test_top_level_arguments_must_be_a_canonical_mapping(arguments: object) -> None:
    with pytest.raises(protected.ProtectedDispatchError, match="mapping snapshot"):
        _build(final_arguments=arguments)
