"""Deletes only the fixed, synthetic user namespaces used by the
verification/security-benchmark harnesses, in a caller-owned transaction. Used by
security_benchmark/isolation.py and verification/isolation.py to tear down test
fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from astralplane.contracts import Transaction
from astralplane.repositories import RepositoryValidationError

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,126}$")
_ISSUER_HASH = re.compile(r"^[0-9a-f]{16}$")

_VERIFICATION_TABLES: Final = (
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
_SECURITY_BENCHMARK_TABLES: Final = (
    "message_attachment",
    "saved_components",
    "workspace_layout",
    "workspace_snapshot",
    "messages",
    "chats",
    "user_attachments",
    "draft_agents",
    "memory_item",
    "short_term_signal",
)


class HarnessCleanupProfile(StrEnum):
    VERIFICATION = "verification"
    SECURITY_BENCHMARK = "security_benchmark"


@dataclass(frozen=True, slots=True)
class HarnessCleanupTableResult:
    table: str
    deleted_rows: int


@dataclass(frozen=True, slots=True)
class HarnessCleanupReport:
    profile: HarnessCleanupProfile
    namespace_prefix: str
    tables: tuple[HarnessCleanupTableResult, ...]

    @property
    def total_deleted(self) -> int:
        return sum(item.deleted_rows for item in self.tables)


def _literal_like_prefix(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _bare_run_id(run_id: object, namespace: str) -> str:
    if not isinstance(run_id, str):
        raise RepositoryValidationError("harness run_id must be a canonical string")
    bare = run_id[len(namespace) :] if run_id.startswith(namespace) else run_id
    if namespace in bare or _RUN_ID.fullmatch(bare) is None:
        raise RepositoryValidationError("harness run_id must be a canonical string")
    return bare


class HarnessCleanupRepository:
    def purge_run(
        self,
        transaction: Transaction,
        *,
        profile: HarnessCleanupProfile,
        run_id: str,
        owner_issuer_hash: str | None = None,
    ) -> HarnessCleanupReport:
        if not isinstance(profile, HarnessCleanupProfile):
            raise RepositoryValidationError("harness cleanup profile is unsupported")
        if owner_issuer_hash is not None and (
            not isinstance(owner_issuer_hash, str)
            or _ISSUER_HASH.fullmatch(owner_issuer_hash) is None
        ):
            raise RepositoryValidationError(
                "harness owner issuer hash must be 16 lowercase hex digits"
            )
        if profile is HarnessCleanupProfile.VERIFICATION:
            namespace = "__verif__"
            bare = _bare_run_id(run_id, namespace)
            literal_prefix = f"{namespace}{bare}_"
            tables = _VERIFICATION_TABLES
        else:
            namespace = "__bench__"
            bare = _bare_run_id(run_id, namespace)
            literal_prefix = f"{namespace}{bare}__"
            tables = _SECURITY_BENCHMARK_TABLES
        if owner_issuer_hash is not None:
            literal_prefix = f"oidc.{owner_issuer_hash}.{literal_prefix}"
        pattern = _literal_like_prefix(literal_prefix)
        results: list[HarnessCleanupTableResult] = []
        for table in tables:
            result = transaction.execute(
                f"DELETE FROM {table} WHERE user_id LIKE %s ESCAPE '\\'",
                (pattern,),
            )
            rowcount = result.rowcount
            if not isinstance(rowcount, int) or rowcount < 0:
                raise RepositoryValidationError(
                    "harness cleanup received an invalid command result",
                    metadata={"table": table},
                )
            results.append(HarnessCleanupTableResult(table=table, deleted_rows=rowcount))
        return HarnessCleanupReport(
            profile=profile,
            namespace_prefix=literal_prefix,
            tables=tuple(results),
        )


__all__ = (
    "HarnessCleanupProfile",
    "HarnessCleanupReport",
    "HarnessCleanupRepository",
    "HarnessCleanupTableResult",
)
