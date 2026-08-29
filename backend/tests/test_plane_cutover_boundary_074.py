"""Fail-closed inventory for the feature-074 AstralPlane cutover boundary."""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLANE_SOURCE = ROOT / "components" / "AstralPlane" / "src"
if str(PLANE_SOURCE) not in sys.path:
    sys.path.insert(0, str(PLANE_SOURCE))

from astralplane import (  # noqa: E402
    BLOB_LAYOUT_VERSION,
    CONTRACT_VERSION,
    GENERATED_AGENT_BUNDLE_CONTRACT,
    MIGRATION_DIGEST,
    PACKAGE_VERSION,
    READ_COMPATIBLE_FROM,
    SCHEMA_REVISION,
)
from astralplane.errors import PlaneError  # noqa: E402
from orchestrator.plane_composition import (  # noqa: E402
    AstralDeepPlaneContractReconciler,
    InitializedPlaneComposition,
    PlaneCompositionError,
    PlaneContractExpectation,
    compose_plane_catalog,
    compose_plane_runtime,
    inspect_plane_composition,
    load_plane_expectation,
)

GAPS_PATH = (
    ROOT
    / "specs"
    / "074-multirepo-lets-integration"
    / "execution"
    / "plane-cutover-gaps.json"
)
DB_METHODS = frozenset(
    {
        "_get_connection",
        "aexecute",
        "afetch_all",
        "afetch_one",
        "execute",
        "fetch_all",
        "fetch_one",
    }
)
DB_RECEIVERS = frozenset({"_database", "_db", "database", "db"})
FEATURE_074_QUALIFICATION_PLANE_COMMIT = (
    "799486468e13202fc87f73baf880bf8079b1e280"
)
FEATURE_074_SCHEMA_REVISION = "074.004"
FEATURE_074_MIGRATION_DIGEST = (
    "31495e9b916301e5d9d5011f256224e62e0a0822e25fdf3b9c339beb695eff50"
)


def _gaps() -> dict[str, object]:
    value = json.loads(GAPS_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _declared_plane_commit() -> str:
    composition = json.loads(
        (ROOT / "config" / "astral-composition.json").read_text(encoding="utf-8")
    )
    commit = composition["components"]["astral-plane"]["commit"]
    assert isinstance(commit, str)
    return commit


def _embedded_plane_gitlink() -> str:
    completed = subprocess.run(
        ["git", "ls-files", "--stage", "--", "components/AstralPlane"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    fields = completed.stdout.split()
    assert fields[0] == "160000"
    assert fields[2:] == ["0", "components/AstralPlane"]
    assert len(fields[1]) == 40
    return fields[1]


def _embedded_plane_parents(commit: str) -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "rev-list", "--parents", "-n", "1", commit],
        cwd=ROOT / "components" / "AstralPlane",
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    commits = completed.stdout.split()
    assert commits[0] == commit
    assert all(len(value) == 40 for value in commits)
    return tuple(commits[1:])


def _embedded_plane_is_ancestor(ancestor: str, descendant: str) -> bool:
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=ROOT / "components" / "AstralPlane",
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode in {0, 1}, completed.stderr
    return completed.returncode == 0


def _embedded_plane_changed_paths(source: str, evidence: str) -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "diff", "--name-only", f"{source}..{evidence}", "--"],
        cwd=ROOT / "components" / "AstralPlane",
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return tuple(line for line in completed.stdout.splitlines() if line)


def _embedded_plane_file(commit: str, path: str) -> bytes:
    completed = subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=ROOT / "components" / "AstralPlane",
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    return completed.stdout


def _production_python() -> tuple[Path, ...]:
    result: list[Path] = []
    for path in (ROOT / "backend").rglob("*.py"):
        relative = path.relative_to(ROOT)
        if (
            "tests" in relative.parts
            or "tmp" in relative.parts
            or path.name.startswith("test_")
        ):
            continue
        result.append(path)
    return tuple(sorted(result))


def _module_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _direct_psycopg_paths() -> list[str]:
    paths: list[str] = []
    for path in _production_python():
        if "scripts" in path.relative_to(ROOT).parts:
            continue
        imported = False
        for node in ast.walk(_module_tree(path)):
            if isinstance(node, ast.Import):
                imported = any(
                    alias.name.split(".", 1)[0] in {"psycopg", "psycopg2"}
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = node.module.split(".", 1)[0] in {"psycopg", "psycopg2"}
            if imported:
                paths.append(path.relative_to(ROOT).as_posix())
                break
    return sorted(paths)


def _legacy_database_callers() -> list[str]:
    paths: list[str] = []
    for path in _production_python():
        relative = path.relative_to(ROOT)
        if "scripts" in relative.parts or "qual_audit" in relative.parts:
            continue
        found = False
        for node in ast.walk(_module_tree(path)):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Attribute)
                or node.func.attr not in DB_METHODS
            ):
                continue
            receiver = node.func.value
            found = (
                node.func.attr == "_get_connection"
                or isinstance(receiver, ast.Name)
                and receiver.id in DB_RECEIVERS
                or isinstance(receiver, ast.Attribute)
                and receiver.attr in DB_RECEIVERS
            )
            if found:
                paths.append(relative.as_posix())
                break
    return sorted(paths)


def _matching_expectation() -> PlaneContractExpectation:
    return PlaneContractExpectation(
        contract_version=CONTRACT_VERSION,
        schema_revision=SCHEMA_REVISION,
        read_compatible_from=READ_COMPATIBLE_FROM,
        migration_sha256=MIGRATION_DIGEST,
        blob_layout_version=BLOB_LAYOUT_VERSION,
    )


def test_gap_inventory_is_exact_for_the_observed_deep_tree() -> None:
    gaps = _gaps()
    assert gaps["format"] == "astral.plane-cutover-gaps/v1"
    assert gaps["status"] == "ready-for-final-qualification"
    plane_blockers = gaps["blockingPlaneCapabilities"]
    assert gaps["blockingPlaneCapabilityCount"] == len(plane_blockers) == 0
    blockers = gaps["blockingProductDecisions"]
    assert isinstance(blockers, list)
    assert gaps["remainingBlockingProductDecisionCount"] == len(blockers) == 0
    resolved_decisions = gaps["resolvedProductDecisions"]
    assert resolved_decisions == [
        {
            "id": "account-deletion-reachability",
            "authority": "Owner direction recorded 2026-08-21",
            "event": (
                "Authenticated POST /api/account/retirement with deliberate "
                "confirmation"
            ),
            "ownerIdentity": "Immutable sub from the verified Keycloak access token",
            "statusContract": (
                "Durable 202 acceptance followed by owner-scoped GET status"
            ),
            "operatorRecovery": (
                "manual_review remains evidence-bound Plane administration only"
            ),
            "logoutDisposition": "Logout never schedules account retirement",
        }
    ]
    composition_pins = gaps["blockingCompositionPins"]
    assert gaps["blockingCompositionPinCount"] == len(composition_pins) == 0
    observed = gaps["observedPlanePublicSurface"]
    declared_commit = _declared_plane_commit()
    embedded_commit = _embedded_plane_gitlink()
    assert declared_commit == embedded_commit
    source_commit = observed["sourceCommit"]
    evidence_commit = observed["evidenceCommit"]
    qualification_commit = FEATURE_074_QUALIFICATION_PLANE_COMMIT
    assert _embedded_plane_is_ancestor(evidence_commit, qualification_commit)
    assert _embedded_plane_is_ancestor(qualification_commit, embedded_commit)
    # The retained migration receipt predates the final owner-CI qualification
    # commits. Bind the historic evidence-to-074-boundary delta exactly, then
    # separately require that immutable boundary to be an ancestor of today's
    # composed Plane. Feature-075 descendants cannot rewrite the 074 inventory.
    assert _embedded_plane_changed_paths(evidence_commit, qualification_commit) == (
        ".github/workflows/ci.yml",
        "README.md",
        "pyproject.toml",
        "tests/architecture/test_ci_workflow.py",
        "tests/test_blob_store.py",
        "tooling/python-ci/build-requirements.lock.txt",
        "uv.lock",
        "workflows-disabled/ci.yml",
    )
    assert _embedded_plane_parents(evidence_commit) == (source_commit,)
    assert _embedded_plane_changed_paths(source_commit, evidence_commit) == (
        "provenance/checks.json",
    )
    assert observed["evidencePath"] == "provenance/checks.json"
    evidence_bytes = _embedded_plane_file(evidence_commit, observed["evidencePath"])
    assert hashlib.sha256(evidence_bytes).hexdigest() == observed["evidenceSha256"]
    evidence = json.loads(evidence_bytes)
    assert evidence["schemaVersion"] == "astralplane.migration-checks/v1"
    assert evidence["candidate"] == {
        "branch": "codex/074-extract-data-plane",
        "head": source_commit,
        "workingTree": "clean",
    }
    assert evidence["status"] == observed["evidenceStatus"] == "passed"
    checks = evidence["checks"]
    assert len(checks) == observed["evidenceCheckCount"] == 8
    assert len({check["id"] for check in checks}) == len(checks)
    assert all(
        check["status"] == "passed"
        and check["exitCode"] == 0
        and check["timedOut"] is False
        for check in checks
    )
    assert (
        evidence["migrationRegistryDigest"]
        == observed["migrationRegistryDigest"]
        == FEATURE_074_MIGRATION_DIGEST
    )
    assert observed["evidenceCheckCount"] == 8
    assert observed["evidenceStatus"] == "passed"
    assert observed["schemaRevision"] == FEATURE_074_SCHEMA_REVISION

    remaining = gaps["planeOwnedImplementationsRemainingInDeep"]
    assert isinstance(remaining, list)
    assert remaining == sorted(set(remaining))
    assert all((ROOT / path).is_file() for path in remaining)

    assert gaps["observedDeepLegacyCallers"] == _legacy_database_callers()
    assert gaps["directPsycopgProductionPaths"] == _direct_psycopg_paths()
    assert any(
        capability["id"] == "streaming-attachment-blob-store"
        for capability in gaps["resolvedPlaneCapabilities"]
    )
    assert any(
        capability["id"] == "pending-first-attachment-materialization"
        for capability in gaps["resolvedPlaneCapabilities"]
    )
    assert any(
        capability["id"] == "streaming-durable-purge-composition"
        for capability in gaps["resolvedPlaneCapabilities"]
    )
    assert any(
        capability["id"] == "generated-agent-artifact-publication-recovery"
        for capability in gaps["resolvedPlaneCapabilities"]
    )


def test_gap_inventory_records_resolved_plane_exports() -> None:
    gaps = _gaps()
    resolved = gaps["resolvedPlaneCapabilities"]
    assert isinstance(resolved, list) and resolved
    required = {
        name for capability in resolved for name in capability["requiredPublicSurface"]
    }

    import astralplane

    catalog = astralplane.create_repository_catalog()
    unexpectedly_missing = []
    for name in sorted(required):
        if name.startswith("catalog."):
            value: object = catalog
            for part in name.split(".")[1:]:
                value = getattr(value, part, None)
                if value is None:
                    unexpectedly_missing.append(name)
                    break
        elif not hasattr(astralplane, name):
            unexpectedly_missing.append(name)
    assert unexpectedly_missing == [], (
        f"A resolved Plane capability is no longer exported: {unexpectedly_missing!r}"
    )


def test_plane_composition_boundary_contains_no_storage_implementation() -> None:
    source_path = ROOT / "backend" / "orchestrator" / "plane_composition.py"
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots.isdisjoint({"psycopg", "psycopg2", "shared"})
    assert "CREATE TABLE" not in source
    assert "ALTER TABLE" not in source
    assert "_get_connection" not in source


def test_plane_catalog_admission_is_exact_and_does_not_claim_runtime_readiness() -> (
    None
):
    expectation = _matching_expectation()
    report = inspect_plane_composition(expectation)
    assert report.compatible is True
    assert report.reasons == ()
    assert report.producer_version == PACKAGE_VERSION
    assert report.repositories

    composition = compose_plane_catalog(expectation)
    assert tuple(sorted(composition.repository_mapping)) == report.repositories
    with pytest.raises(TypeError):
        composition.repository_mapping["history"] = object()  # type: ignore[index]
    assert not hasattr(composition, "initialize")
    assert not hasattr(composition, "transaction")


def test_plane_runtime_delegates_pool_baseline_and_migrations_to_public_facade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expectation = _matching_expectation()
    calls: dict[str, object] = {}

    class Runtime:
        closed = False

        def initialize(self, *, expected_revision: str) -> None:
            calls["expected_revision"] = expected_revision
            calls.setdefault("startup_order", []).append("initialize")

        def health(self) -> object:
            calls.setdefault("startup_order", []).append("health")
            return SimpleNamespace(ready=True)

        def close(self) -> None:
            self.closed = True

    runtime = Runtime()

    def factory(database_url: str, **values: object) -> Runtime:
        calls["database_url"] = database_url
        calls["factory"] = values
        return runtime

    import orchestrator.plane_composition as boundary

    monkeypatch.setattr(boundary, "create_postgres_runtime", factory)
    bundle_store = object()

    def compose_generated_agent_bundles(root, *, contract):
        calls["generated_agent_bundle_root"] = root
        calls["generated_agent_bundle_contract"] = contract
        return bundle_store

    monkeypatch.setattr(
        boundary,
        "ImmutableBundleStore",
        compose_generated_agent_bundles,
    )
    policy_result = SimpleNamespace(
        policy_revision="current",
        marker_changed=True,
        agents_marked_for_revalidation=2,
    )

    def reconcile(runtime_arg, repositories_arg):
        calls.setdefault("startup_order", []).append("policy")
        calls["policy_runtime"] = runtime_arg
        calls["policy_repositories"] = repositories_arg
        return policy_result

    monkeypatch.setattr(boundary, "_reconcile_agent_validation_policy", reconcile)

    class Purges:
        aborted = False

        def reconcile_startup(self):
            calls.setdefault("startup_order", []).append("purge")
            return ()

        def abort(self):
            self.aborted = True

    purges = Purges()

    def compose_purges(**values):
        calls["purge_values"] = values
        return purges

    class Materializations:
        closed = False

        def close(self) -> None:
            self.closed = True

    materializations = Materializations()

    def compose_materializations(**values):
        calls["materialization_values"] = values
        return materializations

    monkeypatch.setattr(
        boundary,
        "create_attachment_materialization_coordinator",
        compose_materializations,
    )
    monkeypatch.setattr(boundary, "AttachmentPurgeCoordinator", compose_purges)
    composed = compose_plane_runtime(
        expectation,
        database_url="postgresql://redacted",
        blob_root=tmp_path / "blobs",
        personal_agent_artifact_root=tmp_path / "personal-agent-artifacts",
        identity="deep-a",
        minimum_connections=1,
        maximum_connections=4,
    )

    assert composed.runtime is runtime
    assert composed.generated_agent_bundles is bundle_store
    assert calls["generated_agent_bundle_root"] == (
        tmp_path / "personal-agent-artifacts"
    )
    assert calls["generated_agent_bundle_contract"] is (GENERATED_AGENT_BUNDLE_CONTRACT)
    assert composed.agent_policy_reconciliation is policy_result
    assert composed.attachment_purges is purges
    assert composed.repositories is composed.contract.repositories
    assert calls["database_url"] == "postgresql://redacted"
    assert calls["expected_revision"] == SCHEMA_REVISION
    assert calls["startup_order"] == ["initialize", "health", "policy", "purge"]
    assert calls["policy_runtime"] is runtime
    assert calls["policy_repositories"] is composed.repositories
    purge_values = calls["purge_values"]
    assert isinstance(purge_values, dict)
    assert purge_values["plane_runtime"] is runtime
    assert purge_values["purge_repository"] is composed.repositories.purge
    assert purge_values["blobs"] is composed.blobs
    materialization_values = calls["materialization_values"]
    assert isinstance(materialization_values, dict)
    assert materialization_values["database"] is runtime
    assert (
        materialization_values["materializations"]
        is composed.repositories.artifacts.materializations
    )
    assert materialization_values["blobs"] is composed.blobs
    factory_values = calls["factory"]
    assert isinstance(factory_values, dict)
    assert factory_values["repositories"] is composed.repositories
    assert factory_values["minimum_connections"] == 1
    assert factory_values["maximum_connections"] == 4
    reconcilers = factory_values["reconcilers"]
    assert isinstance(reconcilers, tuple) and len(reconcilers) == 1
    reconciler = reconcilers[0]
    assert isinstance(reconciler, AstralDeepPlaneContractReconciler)
    assert reconciler.name == "astraldeep-plane-contract"
    assert reconciler.version == f"contract-{reconciler.composition_digest[:24]}"
    assert factory_values["reconciliation_context"] == {
        "host": "astraldeep",
        "composition_digest": reconciler.composition_digest,
    }
    assert reconciler.reconcile(factory_values["reconciliation_context"]) == {
        "composition_digest": reconciler.composition_digest,
        "contract_version": CONTRACT_VERSION,
        "profile": "astraldeep-runtime/v1",
        "repository_count": len(composed.contract.report.repositories),
        "schema_revision": SCHEMA_REVISION,
    }
    composed.close()
    assert runtime.closed is True
    assert purges.aborted is True
    assert materializations.closed is True
    with pytest.raises(PlaneError) as closed_blob:
        composed.blobs.__enter__()
    assert closed_blob.value.code == "blob_store_closed"


def test_deep_runtime_factory_builds_real_plane_reconciliation_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exercise Plane's real constructor so an empty hook regression cannot hide."""

    from astralplane import create_plane_runtime

    captured: dict[str, object] = {}

    class DriverPool:
        closed = False

        def getconn(self):  # pragma: no cover - initialization is isolated below
            raise AssertionError(
                "constructor test must not borrow a database connection"
            )

        def putconn(self, connection, *, close: bool = False):  # pragma: no cover
            raise AssertionError(
                "constructor test must not return a database connection"
            )

        def closeall(self) -> None:
            self.closed = True

    class Coordinator:
        pass

    driver_pool = DriverPool()
    coordinator = Coordinator()

    class StartupHarness:
        def __init__(self, runtime) -> None:
            self.runtime = runtime

        def initialize(self, *, expected_revision: str) -> None:
            captured["expected_revision"] = expected_revision

        def health(self) -> object:
            return SimpleNamespace(ready=True)

        def transaction(self, *, isolation=None):
            return self.runtime.transaction(isolation=isolation)

        def close(self) -> None:
            self.runtime.close()

    def factory(_database_url: str, **values: object) -> StartupHarness:
        captured["reconciler"] = tuple(values["reconcilers"])[0]  # type: ignore[arg-type]
        runtime = create_plane_runtime(
            driver_pool,
            identity=str(values["identity"]),
            coordinator=coordinator,  # type: ignore[arg-type]
            reconcilers=values["reconcilers"],  # type: ignore[arg-type]
            reconciliation_context=values["reconciliation_context"],  # type: ignore[arg-type]
            repositories=values["repositories"],  # type: ignore[arg-type]
            expected_contract_version=str(values["expected_contract_version"]),
            observed_schema_revision=str(values["observed_schema_revision"]),
        )
        plan = runtime._reconciler.plan(  # noqa: SLF001 - constructor contract proof
            schema_revision=SCHEMA_REVISION
        )
        captured["hooks"] = plan.hooks
        captured["context"] = values["reconciliation_context"]
        return StartupHarness(runtime)

    import orchestrator.plane_composition as boundary

    monkeypatch.setattr(boundary, "create_postgres_runtime", factory)
    monkeypatch.setattr(
        boundary,
        "_reconcile_agent_validation_policy",
        lambda _runtime, _repositories: SimpleNamespace(
            policy_revision="current",
            marker_changed=False,
            agents_marked_for_revalidation=0,
        ),
    )
    purges = SimpleNamespace(reconcile_startup=lambda: (), abort=lambda: None)
    monkeypatch.setattr(
        boundary,
        "AttachmentPurgeCoordinator",
        lambda **_values: purges,
    )
    composed = compose_plane_runtime(
        _matching_expectation(),
        database_url="postgresql://redacted",
        blob_root=tmp_path / "blobs",
        personal_agent_artifact_root=tmp_path / "personal-agent-artifacts",
        identity="deep-constructor-test",
    )
    hooks = captured["hooks"]
    assert isinstance(hooks, tuple) and len(hooks) == 1
    assert hooks[0].name == "astraldeep-plane-contract"
    assert hooks[0].version.startswith("contract-")
    reconciler = captured["reconciler"]
    assert isinstance(reconciler, AstralDeepPlaneContractReconciler)
    assert captured["context"] == {
        "host": "astraldeep",
        "composition_digest": reconciler.composition_digest,
    }
    assert captured["expected_revision"] == SCHEMA_REVISION
    assert composed.attachment_purges is purges
    composed.close()
    assert driver_pool.closed is True


def test_agent_policy_reconciliation_uses_one_caller_owned_transaction() -> None:
    import orchestrator.plane_composition as boundary

    transaction = object()
    calls: dict[str, object] = {}

    class TransactionContext:
        def __enter__(self):
            calls["entered"] = True
            return transaction

        def __exit__(self, *_args):
            calls["exited"] = True

    class Runtime:
        def transaction(self):
            return TransactionContext()

    result = SimpleNamespace(
        policy_revision="current",
        marker_changed=True,
        agents_marked_for_revalidation=3,
    )

    class Agents:
        def reconcile_validation_policy_for_administration(
            self,
            received_transaction,
            *,
            policy_revision,
        ):
            calls["transaction"] = received_transaction
            calls["policy_revision"] = policy_revision
            return result

    observed = boundary._reconcile_agent_validation_policy(  # noqa: SLF001
        Runtime(),
        SimpleNamespace(agents=Agents()),
    )

    assert observed is result
    assert calls == {
        "entered": True,
        "transaction": transaction,
        "policy_revision": boundary.USER_AGENT_POLICY_REVISION,
        "exited": True,
    }


def test_agent_policy_reconciliation_report_is_bounded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import orchestrator.plane_composition as boundary

    caplog.set_level("INFO", logger=boundary.logger.name)
    boundary._report_agent_policy_reconciliation(  # noqa: SLF001
        SimpleNamespace(
            policy_revision="constitution=0.1.0;analyze=1",
            marker_changed=True,
            agents_marked_for_revalidation=3,
        )
    )

    message = caplog.messages[-1]
    assert "revision=constitution=0.1.0;analyze=1" in message
    assert "marker_changed=true" in message
    assert "agents_marked_for_revalidation=3" in message
    assert "owner" not in message
    assert "agent_id" not in message


def test_plane_runtime_closes_owned_pool_when_initialization_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Runtime:
        closed = False

        def initialize(self, *, expected_revision: str) -> None:
            assert expected_revision == SCHEMA_REVISION

        def health(self) -> object:
            return SimpleNamespace(ready=False)

        def close(self) -> None:
            self.closed = True

    runtime = Runtime()
    import orchestrator.plane_composition as boundary

    monkeypatch.setattr(
        boundary,
        "create_postgres_runtime",
        lambda *_args, **_kwargs: runtime,
    )
    with pytest.raises(PlaneCompositionError, match="did not become ready"):
        compose_plane_runtime(
            _matching_expectation(),
            database_url="postgresql://redacted",
            blob_root=tmp_path / "blobs",
            personal_agent_artifact_root=tmp_path / "personal-agent-artifacts",
            identity="deep-a",
        )
    assert runtime.closed is True


def test_plane_factory_failure_closes_blob_workers_before_runtime_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import orchestrator.plane_composition as boundary

    events: list[str] = []

    class Blobs:
        def close(self) -> None:
            events.append("blobs.close")

    monkeypatch.setattr(
        boundary,
        "create_streaming_blob_store",
        lambda **_values: Blobs(),
    )
    monkeypatch.setattr(
        boundary,
        "create_postgres_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("runtime construction failed")
        ),
    )

    with pytest.raises(RuntimeError, match="runtime construction failed"):
        compose_plane_runtime(
            _matching_expectation(),
            database_url="postgresql://redacted",
            blob_root=tmp_path / "blobs",
            personal_agent_artifact_root=tmp_path / "personal-agent-artifacts",
            identity="deep-a",
        )

    assert events == ["blobs.close"]


def test_bundle_store_constructor_failure_closes_blob_workers_before_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import orchestrator.plane_composition as boundary

    events: list[str] = []

    class Blobs:
        def close(self) -> None:
            events.append("blobs.close")

    monkeypatch.setattr(
        boundary,
        "create_streaming_blob_store",
        lambda **_values: Blobs(),
    )
    monkeypatch.setattr(
        boundary,
        "ImmutableBundleStore",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("bundle store construction failed")
        ),
    )
    monkeypatch.setattr(
        boundary,
        "create_postgres_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("runtime must not be constructed")
        ),
    )

    with pytest.raises(RuntimeError, match="bundle store construction failed"):
        compose_plane_runtime(
            _matching_expectation(),
            database_url="postgresql://redacted",
            blob_root=tmp_path / "blobs",
            personal_agent_artifact_root=tmp_path / "personal-agent-artifacts",
            identity="deep-a",
        )

    assert events == ["blobs.close"]


def test_busy_blob_close_keeps_plane_runtime_open_and_allows_retry() -> None:
    events: list[str] = []

    class Materializer:
        def abort(self) -> None:
            events.append("materializer.abort")

    class Materializations:
        def close(self) -> None:
            events.append("materializations.close")

    class Purges:
        def abort(self) -> None:
            events.append("purges.abort")

    class Blobs:
        attempts = 0

        def close(self) -> None:
            self.attempts += 1
            events.append("blobs.close")
            if self.attempts == 1:
                raise RuntimeError("blob_store_busy")

    class Runtime:
        closed = False

        def close(self) -> None:
            self.closed = True
            events.append("runtime.close")

    runtime = Runtime()
    composition = InitializedPlaneComposition(
        contract=SimpleNamespace(repositories=SimpleNamespace()),
        runtime=runtime,  # type: ignore[arg-type]
        blobs=Blobs(),  # type: ignore[arg-type]
        generated_agent_bundles=SimpleNamespace(),  # type: ignore[arg-type]
        agent_policy_reconciliation=SimpleNamespace(),  # type: ignore[arg-type]
        attachment_materializations=Materializations(),  # type: ignore[arg-type]
        attachment_purges=Purges(),  # type: ignore[arg-type]
        attachment_materializer=Materializer(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="blob_store_busy"):
        composition.close()

    assert runtime.closed is False
    assert events == [
        "materializer.abort",
        "materializations.close",
        "purges.abort",
        "blobs.close",
    ]

    composition.close()

    assert runtime.closed is True
    assert events == [
        "materializer.abort",
        "materializations.close",
        "purges.abort",
        "blobs.close",
        "materializer.abort",
        "materializations.close",
        "purges.abort",
        "blobs.close",
        "runtime.close",
    ]


def test_plane_runtime_closes_when_startup_purge_is_not_converged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from orchestrator.attachments.purge import AttachmentPurgeReadinessError

    class Runtime:
        closed = False

        def initialize(self, *, expected_revision: str) -> None:
            assert expected_revision == SCHEMA_REVISION

        def health(self) -> object:
            return SimpleNamespace(ready=True)

        def close(self) -> None:
            self.closed = True

    runtime = Runtime()
    import orchestrator.plane_composition as boundary

    monkeypatch.setattr(
        boundary,
        "create_postgres_runtime",
        lambda *_args, **_kwargs: runtime,
    )
    monkeypatch.setattr(
        boundary,
        "_reconcile_agent_validation_policy",
        lambda *_args: SimpleNamespace(
            policy_revision="current",
            marker_changed=False,
            agents_marked_for_revalidation=0,
        ),
    )
    purges = SimpleNamespace(
        aborted=False,
        reconcile_startup=lambda: (_ for _ in ()).throw(
            AttachmentPurgeReadinessError("purge_reconciliation_incomplete")
        ),
    )

    def _abort() -> None:
        purges.aborted = True

    purges.abort = _abort
    materializations = SimpleNamespace(closed=False)

    def _close_materializations() -> None:
        materializations.closed = True

    materializations.close = _close_materializations
    monkeypatch.setattr(
        boundary,
        "create_attachment_materialization_coordinator",
        lambda **_values: materializations,
    )
    monkeypatch.setattr(
        boundary,
        "AttachmentPurgeCoordinator",
        lambda **_values: purges,
    )

    with pytest.raises(
        AttachmentPurgeReadinessError,
        match="purge_reconciliation_incomplete",
    ):
        compose_plane_runtime(
            _matching_expectation(),
            database_url="postgresql://redacted",
            blob_root=tmp_path / "blobs",
            personal_agent_artifact_root=tmp_path / "personal-agent-artifacts",
            identity="deep-a",
        )

    assert runtime.closed is True
    assert purges.aborted is True
    assert materializations.closed is True


def test_plane_catalog_admission_rejects_every_exact_metadata_mismatch() -> None:
    expectation = _matching_expectation()
    mismatched = PlaneContractExpectation(
        contract_version=expectation.contract_version,
        schema_revision=expectation.schema_revision,
        read_compatible_from=expectation.read_compatible_from,
        migration_sha256="0" * 64,
        blob_layout_version=expectation.blob_layout_version,
    )
    report = inspect_plane_composition(mismatched)
    assert report.compatible is False
    assert report.reasons == ("migration_digest_mismatch",)
    with pytest.raises(PlaneCompositionError, match="migration_digest_mismatch"):
        compose_plane_catalog(mismatched)


def test_load_plane_expectation_reads_only_the_strict_data_plane_block(
    tmp_path: Path,
) -> None:
    expectation = _matching_expectation()
    path = tmp_path / "composition.json"
    path.write_text(
        json.dumps(
            {
                "unrelated": {"ignored": True},
                "compatibility": {
                    "data_plane": {
                        "contract_version": expectation.contract_version,
                        "schema_revision": expectation.schema_revision,
                        "read_compatible_from": expectation.read_compatible_from,
                        "migration_sha256": expectation.migration_sha256,
                        "blob_layout_version": expectation.blob_layout_version,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert load_plane_expectation(path) == expectation

    document = json.loads(path.read_text(encoding="utf-8"))
    document["compatibility"]["data_plane"]["extra"] = "not allowed"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(PlaneCompositionError, match="keys differ"):
        load_plane_expectation(path)
