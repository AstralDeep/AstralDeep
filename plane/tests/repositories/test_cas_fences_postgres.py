"""Real-PostgreSQL regressions for draft and machine-credential compare-and-set fences:
malformed revision inputs are refused with a typed validation error before any statement,
while valid, stale, and foreign-owner fences keep their owner-scoped outcomes.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from test_assignments_postgres import database as database
from test_assignments_postgres import uid

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.credentials import CredentialRepository
from astralplane.repositories.drafts import DraftAgentRepository
from astralplane.repositories.remote import RemoteMachine, RemoteRepository


class _TruncatingFence:
    def __int__(self) -> int:
        return int(1.9)


MALFORMED_FENCES = (
    1.9,
    float("inf"),
    float("nan"),
    Decimal("1.9"),
    Decimal("Infinity"),
    True,
    _TruncatingFence(),
    "1.9",
)


def _registered_machine(tx, owner: str, machine: str) -> None:
    RemoteRepository().create_machine(
        tx,
        RemoteMachine(
            machine_id=machine,
            owner_id=owner,
            label="fence-lab",
            address="fence-lab.internal",
            port=22,
            username="runner",
            os_family="linux",
            role="cluster",
            host_key_type=None,
            host_key_fingerprint=None,
            host_key_blob=None,
            last_verdict=None,
            last_checked_at=None,
            created_at=1,
            updated_at=1,
        ),
    )


def _draft_credential(tx, owner: str) -> tuple[DraftAgentRepository, object, int]:
    repository = DraftAgentRepository()
    draft = repository.create_draft(
        tx,
        draft_id=uid(),
        owner_id=owner,
        agent_name="Fence Agent",
        agent_slug=uid(),
        description="Fence draft",
        observed_at=1,
    )
    return repository, draft, draft.state_revision


def test_draft_cas_refuses_malformed_fences_before_any_statement(database) -> None:
    with database.transaction() as tx:
        repository, draft, revision = _draft_credential(tx, "fence-owner")
        for fence in MALFORMED_FENCES:
            with pytest.raises(RepositoryValidationError):
                repository.compare_and_set_draft(
                    tx,
                    owner_id="fence-owner",
                    draft_id=draft.draft_id,
                    expected_revision=fence,
                    updates={"description": "malformed"},
                    updated_at=2,
                )
            with pytest.raises(RepositoryValidationError):
                repository.bind_attachment_provenance(
                    tx,
                    owner_id="fence-owner",
                    draft_id=draft.draft_id,
                    expected_revision=fence,
                    source_chat_id="chat-1",
                    gap_fingerprint="gap-1",
                    source_attachment_id="attachment-1",
                    updated_at=2,
                )
        assert (
            repository.get_draft(tx, owner_id="fence-owner", draft_id=draft.draft_id).state_revision
            == revision
        )
        updated = repository.compare_and_set_draft(
            tx,
            owner_id="fence-owner",
            draft_id=draft.draft_id,
            expected_revision=revision,
            updates={"description": "applied"},
            updated_at=2,
        )
        assert updated.state_revision == revision + 1


def test_draft_cas_keeps_stale_and_foreign_owner_fences_typed(database) -> None:
    with database.transaction() as tx:
        repository, draft, revision = _draft_credential(tx, "fence-owner")

        with pytest.raises(RepositoryConflictError):
            repository.compare_and_set_draft(
                tx,
                owner_id="fence-owner",
                draft_id=draft.draft_id,
                expected_revision=revision + 3,
                updates={"description": "stale"},
                updated_at=2,
            )

        with pytest.raises(RepositoryNotFoundError):
            repository.compare_and_set_draft(
                tx,
                owner_id="foreign-owner",
                draft_id=draft.draft_id,
                expected_revision=revision,
                updates={"description": "foreign"},
                updated_at=2,
            )
        with pytest.raises(RepositoryNotFoundError):
            repository.bind_attachment_provenance(
                tx,
                owner_id="foreign-owner",
                draft_id=draft.draft_id,
                expected_revision=revision,
                source_chat_id="chat-1",
                gap_fingerprint="gap-1",
                source_attachment_id="attachment-1",
                updated_at=2,
            )

        assert (
            repository.get_draft(tx, owner_id="fence-owner", draft_id=draft.draft_id).state_revision
            == revision
        )


def test_machine_credential_cas_refuses_malformed_fences_before_any_statement(database) -> None:
    repository = CredentialRepository()
    machine = uid()
    with database.transaction() as tx:
        _registered_machine(tx, "fence-owner", machine)
        repository.create_machine_credential(
            tx,
            owner_id="fence-owner",
            machine_id=machine,
            credential_type="password",
            encrypted_secret="ciphertext-1",
            encrypted_passphrase=None,
            created_at=1,
        )
        for fence in MALFORMED_FENCES:
            with pytest.raises(RepositoryValidationError):
                repository.compare_and_set_machine_credential(
                    tx,
                    owner_id="fence-owner",
                    machine_id=machine,
                    expected_updated_at=fence,
                    credential_type="password",
                    encrypted_secret="ciphertext-2",
                    encrypted_passphrase=None,
                    updated_at=2,
                )
        assert (
            repository.get_machine_credential(
                tx, owner_id="fence-owner", machine_id=machine
            ).updated_at
            == 1
        )
        rotated = repository.compare_and_set_machine_credential(
            tx,
            owner_id="fence-owner",
            machine_id=machine,
            expected_updated_at=1,
            credential_type="password",
            encrypted_secret="ciphertext-2",
            encrypted_passphrase=None,
            updated_at=2,
        )
        assert rotated.updated_at == 2


def test_machine_credential_cas_keeps_stale_and_foreign_owner_fences_typed(database) -> None:
    repository = CredentialRepository()
    machine = uid()
    with database.transaction() as tx:
        _registered_machine(tx, "fence-owner", machine)
        repository.create_machine_credential(
            tx,
            owner_id="fence-owner",
            machine_id=machine,
            credential_type="password",
            encrypted_secret="ciphertext-1",
            encrypted_passphrase=None,
            created_at=1,
        )

        with pytest.raises(RepositoryConflictError):
            repository.compare_and_set_machine_credential(
                tx,
                owner_id="fence-owner",
                machine_id=machine,
                expected_updated_at=1,
                credential_type="password",
                encrypted_secret="ciphertext-2",
                encrypted_passphrase=None,
                updated_at=1,
            )
        with pytest.raises(RepositoryConflictError):
            repository.compare_and_set_machine_credential(
                tx,
                owner_id="foreign-owner",
                machine_id=machine,
                expected_updated_at=1,
                credential_type="password",
                encrypted_secret="ciphertext-2",
                encrypted_passphrase=None,
                updated_at=2,
            )

        assert (
            repository.get_machine_credential(
                tx, owner_id="fence-owner", machine_id=machine
            ).updated_at
            == 1
        )
