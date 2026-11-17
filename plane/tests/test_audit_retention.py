"""Tests for src/astralplane/audit_retention.py: hash-chain verification from an
anchored boundary, tamper/mismatch refusal, and anchor-then-delete pruning.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from astralplane.audit_retention import (
    AuditRetentionError,
    AuditRetentionRepository,
    HMACAnchorAuthenticator,
    build_anchor,
    canonical_anchor_bytes,
    verify_anchor,
)
from astralplane.repositories.audit import (
    GENESIS_DIGEST,
    AuditEvent,
    AuditRepository,
    canonical_event_bytes,
)
from tests.repositories._support import ScriptedTransaction

NOW = datetime(2026, 8, 13, 20, tzinfo=UTC)
EVENT_KEY = b"e" * 32
ANCHOR_KEY = b"a" * 32
POLICY_DIGEST = hashlib.sha256(b"six-year-retention").digest()


def authenticate_event(key_id: str, payload: bytes) -> bytes:
    assert key_id == "event-key"
    return hmac.new(EVENT_KEY, payload, hashlib.sha256).digest()


AUTHENTICATOR = HMACAnchorAuthenticator(
    lambda key_id: ANCHOR_KEY if key_id == "anchor-key" else b""
)


def event(event_id: str) -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        chain_id="owner-1",
        auth_principal="principal-1",
        agent_id=None,
        event_class="agent_lifecycle",
        action_type="agent.update",
        description="bounded",
        conversation_id=None,
        correlation_id="correlation-1",
        outcome="success",
        outcome_detail=None,
        inputs_json="{}",
        outputs_json="{}",
        artifact_pointers_json="[]",
        started_at=NOW,
        completed_at=NOW,
        key_id="event-key",
        schema_version=1,
    )


def event_row(
    value: AuditEvent,
    sequence: int,
    previous: bytes,
) -> dict[str, object]:
    entry = authenticate_event(
        value.key_id,
        previous + canonical_event_bytes(value, sequence),
    )
    return {
        "event_id": value.event_id,
        "actor_user_id": value.chain_id,
        "auth_principal": value.auth_principal,
        "agent_id": None,
        "event_class": value.event_class,
        "action_type": value.action_type,
        "description": value.description,
        "conversation_id": None,
        "correlation_id": value.correlation_id,
        "outcome": value.outcome,
        "outcome_detail": None,
        "inputs_meta": {},
        "outputs_meta": {},
        "artifact_pointers": [],
        "started_at": NOW,
        "completed_at": NOW,
        "key_id": "event-key",
        "schema_version": value.schema_version,
        "chain_sequence": sequence,
        "recorded_at": NOW + timedelta(seconds=sequence),
        "prev_hash": previous,
        "entry_hash": entry,
    }


def anchor_row(anchor: object) -> dict[str, object]:
    return {
        "anchor_id": anchor.anchor_id,
        "owner_or_chain": anchor.chain_id,
        "first_retained_sequence": anchor.first_retained_sequence,
        "previous_entry_digest": anchor.previous_entry_digest,
        "retention_policy_digest": anchor.retention_policy_digest,
        "created_at": anchor.created_at,
        "key_id": anchor.key_id,
        "signature_or_mac": anchor.authentication,
    }


def chain_rows() -> tuple[dict[str, object], dict[str, object]]:
    first = event_row(event("event-1"), 1, GENESIS_DIGEST)
    second = event_row(event("event-2"), 2, first["entry_hash"])  # type: ignore[arg-type]
    return first, second


def test_genesis_chain_verifies_without_an_anchor() -> None:
    first, second = chain_rows()
    transaction = ScriptedTransaction(all_rows=[(first, second)])

    result = AuditRetentionRepository().verify_retained_chain(
        transaction,
        chain_id="owner-1",
        audit_repository=AuditRepository(),
        authenticate_event=authenticate_event,
        authenticate_anchor=AUTHENTICATOR,
    )

    assert result.valid
    assert result.last_sequence == 2


def test_empty_genesis_is_a_valid_chain() -> None:
    result = AuditRetentionRepository().verify_retained_chain(
        ScriptedTransaction(all_rows=[()]),
        chain_id="owner-1",
        audit_repository=AuditRepository(),
        authenticate_event=authenticate_event,
        authenticate_anchor=AUTHENTICATOR,
    )
    assert result.valid
    assert result.last_digest == GENESIS_DIGEST


def test_retained_prefix_verifies_from_authenticated_boundary() -> None:
    first, second = chain_rows()
    anchor = build_anchor(
        anchor_id="anchor-1",
        chain_id="owner-1",
        first_retained_sequence=2,
        previous_entry_digest=first["entry_hash"],  # type: ignore[arg-type]
        retention_policy_digest=POLICY_DIGEST,
        created_at=NOW,
        key_id="anchor-key",
        authenticator=AUTHENTICATOR,
    )
    transaction = ScriptedTransaction(all_rows=[(second,)], one=[anchor_row(anchor)])

    result = AuditRetentionRepository().verify_retained_chain(
        transaction,
        chain_id="owner-1",
        audit_repository=AuditRepository(),
        authenticate_event=authenticate_event,
        authenticate_anchor=AUTHENTICATOR,
    )

    assert result.valid
    assert result.last_sequence == 2
    assert verify_anchor(anchor, AUTHENTICATOR)
    assert b"signature" not in canonical_anchor_bytes(anchor)


def test_tampered_anchor_fails_closed() -> None:
    first, second = chain_rows()
    anchor = build_anchor(
        anchor_id="anchor-1",
        chain_id="owner-1",
        first_retained_sequence=2,
        previous_entry_digest=first["entry_hash"],  # type: ignore[arg-type]
        retention_policy_digest=POLICY_DIGEST,
        created_at=NOW,
        key_id="anchor-key",
        authenticator=AUTHENTICATOR,
    )
    tampered = replace(anchor, retention_policy_digest=b"x" * 32)
    transaction = ScriptedTransaction(all_rows=[(second,)], one=[anchor_row(tampered)])
    result = AuditRetentionRepository().verify_retained_chain(
        transaction,
        chain_id="owner-1",
        audit_repository=AuditRepository(),
        authenticate_event=authenticate_event,
        authenticate_anchor=AUTHENTICATOR,
    )
    assert not result.valid
    assert result.reason == "invalid_retention_anchor"


def test_missing_anchor_fails_closed() -> None:
    _, second = chain_rows()
    result = AuditRetentionRepository().verify_retained_chain(
        ScriptedTransaction(all_rows=[(second,)], one=[None]),
        chain_id="owner-1",
        audit_repository=AuditRepository(),
        authenticate_event=authenticate_event,
        authenticate_anchor=AUTHENTICATOR,
    )
    assert not result.valid
    assert result.reason == "missing_retention_anchor"


def test_anchor_boundary_mismatch_fails_closed() -> None:
    first, second = chain_rows()
    anchor = build_anchor(
        anchor_id="anchor-1",
        chain_id="owner-1",
        first_retained_sequence=2,
        previous_entry_digest=b"q" * 32,
        retention_policy_digest=POLICY_DIGEST,
        created_at=NOW,
        key_id="anchor-key",
        authenticator=AUTHENTICATOR,
    )
    assert first["entry_hash"] != anchor.previous_entry_digest
    result = AuditRetentionRepository().verify_retained_chain(
        ScriptedTransaction(all_rows=[(second,)], one=[anchor_row(anchor)]),
        chain_id="owner-1",
        audit_repository=AuditRepository(),
        authenticate_event=authenticate_event,
        authenticate_anchor=AUTHENTICATOR,
    )
    assert not result.valid
    assert result.reason == "retention_anchor_mismatch"


def test_prune_persists_anchor_before_delete_and_reports_count() -> None:
    first, second = chain_rows()
    anchor = build_anchor(
        anchor_id="anchor-1",
        chain_id="owner-1",
        first_retained_sequence=2,
        previous_entry_digest=first["entry_hash"],  # type: ignore[arg-type]
        retention_policy_digest=POLICY_DIGEST,
        created_at=NOW,
        key_id="anchor-key",
        authenticator=AUTHENTICATOR,
    )
    transaction = ScriptedTransaction(
        one=[
            {"locked": True},
            {"chain_sequence": 2, "prev_hash": first["entry_hash"]},
            {"entry_hash": first["entry_hash"]},
            anchor_row(anchor),
        ],
        all_rows=[({"event_id": "event-1"},)],
    )

    result = AuditRetentionRepository().prune_prefix(
        transaction,
        chain_id="owner-1",
        first_retained_sequence=2,
        anchor_id="anchor-1",
        policy_digest=POLICY_DIGEST,
        created_at=NOW,
        key_id="anchor-key",
        authenticator=AUTHENTICATOR,
    )

    assert result.deleted_events == 1
    actions = [kind for kind, _, _ in transaction.calls]
    assert actions[-2:] == ["execute", "all"]
    assert "INSERT INTO audit_retention_anchor" in transaction.calls[-3][1]
    assert second["prev_hash"] == result.anchor.previous_entry_digest


def test_prune_failure_propagates_for_caller_rollback() -> None:
    first, _ = chain_rows()
    anchor = build_anchor(
        anchor_id="anchor-1",
        chain_id="owner-1",
        first_retained_sequence=2,
        previous_entry_digest=first["entry_hash"],  # type: ignore[arg-type]
        retention_policy_digest=POLICY_DIGEST,
        created_at=NOW,
        key_id="anchor-key",
        authenticator=AUTHENTICATOR,
    )
    transaction = ScriptedTransaction(
        one=[
            {"locked": True},
            {"chain_sequence": 2, "prev_hash": first["entry_hash"]},
            {"entry_hash": first["entry_hash"]},
            anchor_row(anchor),
        ],
        all_rows=[RuntimeError("delete failed")],
    )
    with pytest.raises(RuntimeError, match="delete failed"):
        AuditRetentionRepository().prune_prefix(
            transaction,
            chain_id="owner-1",
            first_retained_sequence=2,
            anchor_id="anchor-1",
            policy_digest=POLICY_DIGEST,
            created_at=NOW,
            key_id="anchor-key",
            authenticator=AUTHENTICATOR,
        )
    assert "INSERT INTO audit_retention_anchor" in transaction.calls[-3][1]
    assert "DELETE FROM audit_events" in transaction.calls[-1][1]


def test_repeat_prune_requires_and_reuses_valid_anchor() -> None:
    first, _ = chain_rows()
    anchor = build_anchor(
        anchor_id="anchor-1",
        chain_id="owner-1",
        first_retained_sequence=2,
        previous_entry_digest=first["entry_hash"],  # type: ignore[arg-type]
        retention_policy_digest=POLICY_DIGEST,
        created_at=NOW,
        key_id="anchor-key",
        authenticator=AUTHENTICATOR,
    )
    transaction = ScriptedTransaction(
        one=[
            {"locked": True},
            {"chain_sequence": 2, "prev_hash": first["entry_hash"]},
            None,
            anchor_row(anchor),
        ]
    )
    result = AuditRetentionRepository().prune_prefix(
        transaction,
        chain_id="owner-1",
        first_retained_sequence=2,
        anchor_id="ignored-on-replay",
        policy_digest=POLICY_DIGEST,
        created_at=NOW,
        key_id="anchor-key",
        authenticator=AUTHENTICATOR,
    )
    assert result.deleted_events == 0
    assert result.anchor == anchor

    with pytest.raises(AuditRetentionError) as raised:
        AuditRetentionRepository().prune_prefix(
            ScriptedTransaction(
                one=[
                    {"locked": True},
                    {"chain_sequence": 2, "prev_hash": first["entry_hash"]},
                    None,
                    None,
                ]
            ),
            chain_id="owner-1",
            first_retained_sequence=2,
            anchor_id="anchor-1",
            policy_digest=POLICY_DIGEST,
            created_at=NOW,
            key_id="anchor-key",
            authenticator=AUTHENTICATOR,
        )
    assert raised.value.code == "audit_retention_anchor_missing"


def test_prune_rejects_missing_or_tampered_boundaries() -> None:
    repository = AuditRetentionRepository()
    with pytest.raises(ValueError):
        repository.prune_prefix(
            ScriptedTransaction(),
            chain_id="owner-1",
            first_retained_sequence=1,
            anchor_id="anchor-1",
            policy_digest=POLICY_DIGEST,
            created_at=NOW,
            key_id="anchor-key",
            authenticator=AUTHENTICATOR,
        )
    with pytest.raises(AuditRetentionError) as raised:
        repository.prune_prefix(
            ScriptedTransaction(one=[{"locked": True}, None]),
            chain_id="owner-1",
            first_retained_sequence=2,
            anchor_id="anchor-1",
            policy_digest=POLICY_DIGEST,
            created_at=NOW,
            key_id="anchor-key",
            authenticator=AUTHENTICATOR,
        )
    assert raised.value.code == "audit_retention_boundary_missing"
    with pytest.raises(AuditRetentionError) as raised:
        repository.prune_prefix(
            ScriptedTransaction(
                one=[
                    {"locked": True},
                    {"chain_sequence": 2, "prev_hash": b"x" * 32},
                    {"entry_hash": b"y" * 32},
                ]
            ),
            chain_id="owner-1",
            first_retained_sequence=2,
            anchor_id="anchor-1",
            policy_digest=POLICY_DIGEST,
            created_at=NOW,
            key_id="anchor-key",
            authenticator=AUTHENTICATOR,
        )
    assert raised.value.code == "audit_retention_boundary_tampered"


def test_authenticator_rejects_short_keys_and_verifier_failures() -> None:
    short = HMACAnchorAuthenticator(lambda _key_id: b"short")
    with pytest.raises(ValueError, match="at least 32"):
        build_anchor(
            anchor_id="anchor-1",
            chain_id="owner-1",
            first_retained_sequence=2,
            previous_entry_digest=b"p" * 32,
            retention_policy_digest=POLICY_DIGEST,
            created_at=NOW,
            key_id="anchor-key",
            authenticator=short,
        )

    class BrokenAuthenticator:
        def sign(self, key_id: str, payload: bytes) -> bytes:
            return b"x" * 32

        def verify(self, key_id: str, payload: bytes, authentication: bytes) -> bool:
            raise RuntimeError("trust store unavailable")

    anchor = build_anchor(
        anchor_id="anchor-1",
        chain_id="owner-1",
        first_retained_sequence=2,
        previous_entry_digest=b"p" * 32,
        retention_policy_digest=POLICY_DIGEST,
        created_at=NOW,
        key_id="anchor-key",
        authenticator=BrokenAuthenticator(),
    )
    assert not verify_anchor(anchor, BrokenAuthenticator())
