"""Tests for astralplane.repositories.harness_cleanup: verification and security cleanup
stay within their exact manifest boundary and never touch audit rows, rejecting
malformed run IDs or profiles.
"""

from __future__ import annotations

import pytest

from astralplane.repositories import RepositoryValidationError
from astralplane.repositories.harness_cleanup import (
    HarnessCleanupProfile,
    HarnessCleanupRepository,
)
from tests.repositories._support import Result, ScriptedTransaction


def test_verification_cleanup_escapes_exact_boundary_and_never_deletes_audit() -> None:
    transaction = ScriptedTransaction(execute=[Result(rowcount=index) for index in range(9)])
    report = HarnessCleanupRepository().purge_run(
        transaction,
        profile=HarnessCleanupProfile.VERIFICATION,
        run_id="run_1",
    )

    assert report.namespace_prefix == "__verif__run_1_"
    assert report.total_deleted == sum(range(9))
    assert tuple(item.table for item in report.tables) == (
        "message_attachment",
        "saved_components",
        "workspace_layout",
        "workspace_snapshot",
        "messages",
        "chats",
        "user_attachments",
        "draft_agents",
        "user_llm_config",
    )
    assert all("audit_events" not in statement for _, statement, _ in transaction.calls)
    assert transaction.calls[0][2] == (r"\_\_verif\_\_run\_1\_%",)
    assert all("ESCAPE '\\'" in statement for _, statement, _ in transaction.calls)


def test_security_cleanup_accepts_prefixed_id_and_uses_fixed_extended_manifest() -> None:
    transaction = ScriptedTransaction(execute=[Result(rowcount=1) for _ in range(10)])
    report = HarnessCleanupRepository().purge_run(
        transaction,
        profile=HarnessCleanupProfile.SECURITY_BENCHMARK,
        run_id="__bench__case-1",
    )

    assert report.namespace_prefix == "__bench__case-1__"
    assert report.total_deleted == 10
    assert tuple(item.table for item in report.tables[-2:]) == (
        "memory_item",
        "short_term_signal",
    )
    assert transaction.calls[0][2] == (r"\_\_bench\_\_case-1\_\_%",)


@pytest.mark.parametrize(
    "run_id",
    ("", "_leading", "contains space", "a/b", "__verif____verif__nested", "%"),
)
def test_cleanup_rejects_noncanonical_or_ambiguous_run_ids(run_id: str) -> None:
    transaction = ScriptedTransaction()
    with pytest.raises(RepositoryValidationError, match="run_id"):
        HarnessCleanupRepository().purge_run(
            transaction,
            profile=HarnessCleanupProfile.VERIFICATION,
            run_id=run_id,
        )
    assert transaction.calls == []


def test_cleanup_rejects_arbitrary_profile_and_invalid_command_metadata() -> None:
    with pytest.raises(RepositoryValidationError, match="profile"):
        HarnessCleanupRepository().purge_run(
            ScriptedTransaction(),
            profile="verification",  # type: ignore[arg-type]
            run_id="run-1",
        )

    invalid = ScriptedTransaction(execute=[Result(rowcount=-1)])
    with pytest.raises(RepositoryValidationError, match="command result"):
        HarnessCleanupRepository().purge_run(
            invalid,
            profile=HarnessCleanupProfile.VERIFICATION,
            run_id="run-1",
        )


@pytest.mark.parametrize("profile", tuple(HarnessCleanupProfile))
def test_canonical_issuer_scope_preserves_fixed_manifest_and_literal_boundary(profile):
    count = 9 if profile is HarnessCleanupProfile.VERIFICATION else 10
    transaction = ScriptedTransaction(execute=[Result(rowcount=1) for _ in range(count)])
    report = HarnessCleanupRepository().purge_run(
        transaction, profile=profile, run_id="run_1", owner_issuer_hash="0123456789abcdef"
    )
    namespace = "__verif__run_1_" if count == 9 else "__bench__run_1__"
    assert report.namespace_prefix == "oidc.0123456789abcdef." + namespace
    expected = "oidc.0123456789abcdef." + namespace.replace("_", r"\_") + "%"
    assert all(parameters == (expected,) for _, _, parameters in transaction.calls)
    assert all("audit" not in statement for _, statement, _ in transaction.calls)
    assert report.total_deleted == count


@pytest.mark.parametrize("profile", tuple(HarnessCleanupProfile))
@pytest.mark.parametrize(
    "issuer_hash",
    [
        "",
        "0" * 15,
        "0" * 17,
        "A" * 16,
        "g" * 16,
        "0" * 15 + "_",
        "0" * 15 + "%",
        " " * 16,
        1,
        False,
        [],
    ],
)
def test_invalid_issuer_scope_is_rejected_before_sql(profile, issuer_hash):
    transaction = ScriptedTransaction()
    with pytest.raises(RepositoryValidationError, match="issuer hash"):
        HarnessCleanupRepository().purge_run(
            transaction,
            profile=profile,
            run_id="run_1",
            owner_issuer_hash=issuer_hash,
        )
    assert transaction.calls == []
