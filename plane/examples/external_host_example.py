"""Standalone external-host example that boots AstralPlane through its public facade only:
guarded boot migrations and reconciliation, an explicit absolute blob root, owner-scoped
transactions, idempotent replay, compare-and-set fences, typed failures, and clean shutdown,
with no import of any Astral product. docs/external-host-example.md maps every scenario to the
public API stability boundary and explains where an MCP or A2A host maps its protocol identities
to the neutral owner identifiers Plane receives. Run it against an empty PostgreSQL database you
own with --database-url, an absolute --blob-root outside every source tree, and optionally
--refusal-database-url pointing at a second non-Plane database to observe the fail-closed boot
refusal."""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from astralplane import SCHEMA_REVISION
from astralplane.api import (
    PlaneRuntime,
    RepositoryCatalog,
    create_postgres_runtime,
    create_repository_catalog,
    create_streaming_blob_store,
)
from astralplane.errors import (
    InitializationError,
    PlaneError,
    SchemaRevisionError,
    SQLContractError,
)
from astralplane.repositories import RepositoryConflictError, RepositoryNotFoundError
from astralplane.repositories.drafts import DraftAgentRecord

BOOT: Final = "boot"
EXPLICIT_ROOT: Final = "explicit_durable_root"
OWNER_TRANSACTIONS: Final = "owner_scoped_transactions"
IDEMPOTENT_REPLAY: Final = "idempotent_replay"
COMPARE_AND_SET: Final = "compare_and_set_fence"
CALLER_ROLLBACK: Final = "caller_rollback"
EXPLICIT_RECONCILIATION: Final = "explicit_reconciliation"
BOOT_REFUSAL: Final = "boot_refusal"
SHUTDOWN: Final = "shutdown"

BOOT_IDENTITY: Final = "external-host-example"
REFUSAL_IDENTITY: Final = "external-host-example-refusal"
OTHER_OWNER: Final = "ext-host-owner-other"
BLOB_OWNER: Final = "ext-host-example-owner"
BLOB_KEY: Final = "example/absent-probe.txt"


class HostAbortError(RuntimeError):
    pass


class HostReconciler:
    def __init__(self) -> None:
        self._checked = f"draft_agents@{SCHEMA_REVISION}"

    @property
    def name(self) -> str:
        return "external-host-example"

    @property
    def version(self) -> str:
        return "v1"

    def reconcile(self, context: Mapping[str, object]) -> Mapping[str, object]:
        return {"host": BOOT_IDENTITY, "verified": self._checked}


@dataclass(frozen=True, slots=True)
class ScenarioOutcome:
    scenario: str
    detail: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {"scenario": self.scenario, "detail": dict(self.detail)}


def _error_shape(error: PlaneError) -> dict[str, object]:
    return {"error_type": type(error).__name__, "code": error.code}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def boot(database_url: str) -> tuple[PlaneRuntime, RepositoryCatalog, ScenarioOutcome]:
    runtime = create_postgres_runtime(
        database_url,
        identity=f"{BOOT_IDENTITY}-{uuid.uuid4().hex}",
        reconcilers=(HostReconciler(),),
        reconciliation_context={"example": BOOT_IDENTITY},
        application_name="external-host-example",
    )
    try:
        report = runtime.initialize()
        health = runtime.health()
        _require(
            health.ready,
            "boot did not publish ready health after migration and reconciliation",
        )
        outcome = ScenarioOutcome(
            scenario=BOOT,
            detail={
                "application_schema_revision": SCHEMA_REVISION,
                "migrated_to": report.migration.target_revision,
                "applied_steps": len(report.migration.applied_steps),
                "reconciler": report.reconciliation.hooks[0].hook.name,
                "reconciliation_complete": report.reconciliation.durably_complete,
                "ready": health.ready,
            },
        )
        return runtime, create_repository_catalog(), outcome
    except BaseException:
        runtime.close()
        raise


def demonstrate_explicit_root(blob_root: str) -> ScenarioOutcome:
    refusal: dict[str, object] = {}
    try:
        create_streaming_blob_store(root="external-host-example-blobs")
    except SQLContractError as error:
        refusal.update(_error_shape(error))
    _require(refusal.get("code") == "sql_contract", "a relative blob root was not refused")
    store = create_streaming_blob_store(root=Path(blob_root), create_root=True)
    try:
        probe_absent = store.is_absent(owner_id=BLOB_OWNER, key=BLOB_KEY)
    finally:
        store.close()
    return ScenarioOutcome(
        scenario=EXPLICIT_ROOT,
        detail={
            "root": str(Path(blob_root).resolve()),
            "relative_root_refusal": refusal,
            "unset_key_absent": probe_absent,
        },
    )


def demonstrate_owner_transactions(
    runtime: PlaneRuntime, catalog: RepositoryCatalog, owner: str
) -> tuple[ScenarioOutcome, DraftAgentRecord]:
    repository = catalog.draft_agents
    now = int(time.time())
    draft_uuid = str(uuid.uuid4())
    target_agent_id = str(uuid.uuid4())
    with runtime.transaction() as transaction:
        created = repository.create_draft(
            transaction,
            draft_id=f"ext-host-draft-{uuid.uuid4().hex}",
            owner_id=owner,
            agent_name="External Host Agent",
            agent_slug=f"ext-host-agent-{uuid.uuid4().hex[:12]}",
            description="Draft written by the standalone external-host example",
            observed_at=now,
            draft_uuid=draft_uuid,
            target_agent_id=target_agent_id,
        )
        foreign_view = repository.get_draft(
            transaction, owner_id=OTHER_OWNER, draft_id=created.draft_id
        )
    with runtime.transaction() as transaction:
        observed = repository.get_draft(transaction, owner_id=owner, draft_id=created.draft_id)
    _require(observed == created, "the committed owner-scoped draft was not readable later")
    _require(foreign_view is None, "a foreign owner read another owner's draft")
    return (
        ScenarioOutcome(
            scenario=OWNER_TRANSACTIONS,
            detail={
                "owner": owner,
                "draft_revision": created.state_revision,
                "foreign_owner_read_absent": foreign_view is None,
            },
        ),
        created,
    )


def demonstrate_idempotent_replay(
    runtime: PlaneRuntime, catalog: RepositoryCatalog, owner: str, created: DraftAgentRecord
) -> ScenarioOutcome:
    repository = catalog.draft_agents
    conflict: dict[str, object] = {}
    with runtime.transaction() as transaction:
        replayed = repository.create_draft(
            transaction,
            draft_id=created.draft_id,
            owner_id=owner,
            agent_name=created.agent_name,
            agent_slug=created.agent_slug,
            description=created.description,
            observed_at=created.created_at or 0,
            draft_uuid=created.draft_uuid,
            target_agent_id=created.target_agent_id,
        )
        try:
            repository.create_draft(
                transaction,
                draft_id=created.draft_id,
                owner_id=owner,
                agent_name=created.agent_name,
                agent_slug=created.agent_slug,
                description="A replay that changes an immutable identity",
                observed_at=created.created_at or 0,
                draft_uuid=created.draft_uuid,
                target_agent_id=created.target_agent_id,
            )
        except RepositoryConflictError as error:
            conflict.update(_error_shape(error))
    _require(replayed == created, "an identical replay did not return the original record")
    _require(bool(conflict), "an identity-changing replay was not refused")
    return ScenarioOutcome(
        scenario=IDEMPOTENT_REPLAY,
        detail={
            "replayed_revision": replayed.state_revision,
            "conflicting_replay": conflict,
        },
    )


def demonstrate_compare_and_set(
    runtime: PlaneRuntime, catalog: RepositoryCatalog, owner: str, created: DraftAgentRecord
) -> ScenarioOutcome:
    repository = catalog.draft_agents
    stale: dict[str, object] = {}
    foreign: dict[str, object] = {}
    with runtime.transaction() as transaction:
        updated = repository.compare_and_set_draft(
            transaction,
            owner_id=owner,
            draft_id=created.draft_id,
            expected_revision=created.state_revision,
            updates={"description": "Rotated once by the external-host example"},
            updated_at=int(time.time()),
        )
    with runtime.transaction() as transaction:
        try:
            repository.compare_and_set_draft(
                transaction,
                owner_id=owner,
                draft_id=created.draft_id,
                expected_revision=created.state_revision,
                updates={"description": "stale"},
                updated_at=int(time.time()),
            )
        except RepositoryConflictError as error:
            stale.update(_error_shape(error))
        try:
            repository.compare_and_set_draft(
                transaction,
                owner_id=OTHER_OWNER,
                draft_id=created.draft_id,
                expected_revision=updated.state_revision,
                updates={"description": "foreign"},
                updated_at=int(time.time()),
            )
        except RepositoryNotFoundError as error:
            foreign.update(_error_shape(error))
    _require(bool(stale), "a stale compare-and-set fence was not refused")
    _require(bool(foreign), "a foreign-owner compare-and-set fence was not refused")
    return ScenarioOutcome(
        scenario=COMPARE_AND_SET,
        detail={
            "applied_revision": updated.state_revision,
            "stale_fence": stale,
            "foreign_owner_fence": foreign,
        },
    )


def demonstrate_caller_rollback(
    runtime: PlaneRuntime, catalog: RepositoryCatalog, owner: str
) -> ScenarioOutcome:
    repository = catalog.draft_agents
    draft_id = f"ext-host-rollback-{uuid.uuid4().hex}"
    abort: dict[str, object] = {}
    try:
        with runtime.transaction() as transaction:
            repository.create_draft(
                transaction,
                draft_id=draft_id,
                owner_id=owner,
                agent_name="Rollback Agent",
                agent_slug=f"ext-host-rollback-{uuid.uuid4().hex[:12]}",
                description="This write must roll back with the host transaction",
                observed_at=int(time.time()),
            )
            raise HostAbortError("the host aborted its own transaction")
    except HostAbortError as error:
        abort.update({"error_type": type(error).__name__, "message": str(error)})
    with runtime.transaction() as transaction:
        draft_absent = repository.get_draft(transaction, owner_id=owner, draft_id=draft_id) is None
    _require(draft_absent, "the caller-aborted transaction was not rolled back")
    return ScenarioOutcome(
        scenario=CALLER_ROLLBACK,
        detail={"host_abort": abort, "draft_absent": draft_absent},
    )


def demonstrate_explicit_reconciliation(runtime: PlaneRuntime) -> ScenarioOutcome:
    report = runtime.reconcile(context={"example": BOOT_IDENTITY, "pass": "explicit"})
    return ScenarioOutcome(
        scenario=EXPLICIT_RECONCILIATION,
        detail={
            "hooks": [hook.hook.name for hook in report.hooks],
            "already_complete": [hook.already_complete for hook in report.hooks],
            "durably_complete": report.durably_complete,
        },
    )


def demonstrate_boot_refusal(refusal_database_url: str) -> ScenarioOutcome:
    refusal: dict[str, object] = {}
    runtime = create_postgres_runtime(
        refusal_database_url,
        identity=f"{REFUSAL_IDENTITY}-{uuid.uuid4().hex}",
        reconcilers=(HostReconciler(),),
        application_name="external-host-example-refusal",
    )
    try:
        try:
            runtime.initialize()
        except SchemaRevisionError as error:
            refusal.update(_error_shape(error))
            refusal["metadata"] = dict(error.metadata)
        _require(bool(refusal), "a foreign application schema was not refused at boot")
    finally:
        runtime.close()
    return ScenarioOutcome(scenario=BOOT_REFUSAL, detail=refusal)


def demonstrate_shutdown(runtime: PlaneRuntime) -> ScenarioOutcome:
    runtime.close()
    health = runtime.health()
    post_shutdown: dict[str, object] = {}
    try:
        with runtime.transaction():
            pass
    except InitializationError as error:
        post_shutdown.update(_error_shape(error))
    runtime.close()
    _require(health.pool.closed, "shutdown did not close the pool")
    _require(bool(post_shutdown), "admission stayed open after shutdown")
    return ScenarioOutcome(
        scenario=SHUTDOWN,
        detail={"pool_closed": health.pool.closed, "post_shutdown_refusal": post_shutdown},
    )


def run_example(
    database_url: str,
    blob_root: str,
    refusal_database_url: str | None = None,
) -> list[ScenarioOutcome]:
    outcomes: list[ScenarioOutcome] = []
    runtime, catalog, boot_outcome = boot(database_url)
    try:
        outcomes.append(boot_outcome)
        outcomes.append(demonstrate_explicit_root(blob_root))
        owner = f"ext-host-owner-{uuid.uuid4().hex[:12]}"
        transaction_outcome, created = demonstrate_owner_transactions(runtime, catalog, owner)
        outcomes.append(transaction_outcome)
        outcomes.append(demonstrate_idempotent_replay(runtime, catalog, owner, created))
        outcomes.append(demonstrate_compare_and_set(runtime, catalog, owner, created))
        outcomes.append(demonstrate_caller_rollback(runtime, catalog, owner))
        outcomes.append(demonstrate_explicit_reconciliation(runtime))
        if refusal_database_url is not None:
            outcomes.append(demonstrate_boot_refusal(refusal_database_url))
        outcomes.append(demonstrate_shutdown(runtime))
    finally:
        runtime.close()
    return outcomes


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Boot AstralPlane from an external host through the public facade."
    )
    parser.add_argument("--database-url", required=True, help="empty PostgreSQL database URL")
    parser.add_argument("--blob-root", required=True, help="absolute durable blob root path")
    parser.add_argument(
        "--refusal-database-url",
        default=None,
        help="non-Plane database URL used to demonstrate the boot refusal",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not Path(args.blob_root).is_absolute():
        print(
            f"--blob-root must be an absolute path outside source trees: {args.blob_root}",
            file=sys.stderr,
        )
        return 2
    try:
        outcomes = run_example(args.database_url, args.blob_root, args.refusal_database_url)
    except PlaneError as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "code": error.code,
                    "message": str(error),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    for outcome in outcomes:
        print(json.dumps(outcome.as_dict(), sort_keys=True))
    print(
        json.dumps(
            {"status": "passed", "scenarios": [outcome.scenario for outcome in outcomes]},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
