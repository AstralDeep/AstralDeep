"""Qualifies candidate migrations against a populated database built by exact origin/main
source in an isolated interpreter. Historical scripts and tests never run; baseline
registry, catalog, and synthetic rows are observed before candidate admission and repeat.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

import psycopg2
from psycopg2.extensions import make_dsn

from astralplane.database.migrations import (
    CURRENT_DATA_PLANE_REVISION,
    MIGRATION_REGISTRY,
    MigrationRunner,
)
from astralplane.database.pool import ConnectionPool
from astralplane.database.postgres import create_postgres_driver_pool
from astralplane.database.transaction import PlaneDatabase

ROOT = Path(__file__).resolve().parents[1]
DATABASE_ENV = "ASTRALPLANE_TEST_POSTGRES_DSN"
FIXTURE_QUERY = """
SELECT id, user_id, description, state_revision FROM draft_agents
ORDER BY id
"""
EXPECTED_ROWS = [
    {
        "id": f"10000000-0000-4000-8000-00000000000{index}",
        "user_id": f"upgrade-owner-{index}",
        "description": f"synthetic-{index}",
        "state_revision": 0,
    }
    for index in (1, 2)
]
BOOTSTRAP = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
from astralplane.database.baseline import BaselineMigrationRunner
from astralplane.database.migrations import (
    CURRENT_DATA_PLANE_REVISION, CURRENT_SCHEMA_STRUCTURE_QUERY, MIGRATION_REGISTRY,
    MigrationRunner, _schema_structure_digest,
)
from astralplane.database.pool import ConnectionPool
from astralplane.database.postgres import create_postgres_driver_pool
from astralplane.database.transaction import PlaneDatabase
from astralplane.api import create_repository_catalog
pool = ConnectionPool(create_postgres_driver_pool(os.environ["PLANE_UPGRADE_DATABASE_DSN"],
                                                minimum_connections=1))
try:
    database = PlaneDatabase(pool)
    runner = MigrationRunner(database, revision=CURRENT_DATA_PLANE_REVISION,
                             registry=MIGRATION_REGISTRY)
    BaselineMigrationRunner(database, runner).run(
        expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)
    catalog = create_repository_catalog()
    with database.transaction() as tx:
        for index in (1, 2):
            owner = f"upgrade-owner-{index}"
            catalog.identity.upsert_identity(tx, owner_id=owner, observed_at=1,
                email=f"synthetic-{index}@example.test", username=f"synthetic-{index}",
                display_name=f"Synthetic {index}", roles=("member",))
            catalog.draft_agents.create_draft(tx,
                draft_id=f"10000000-0000-4000-8000-00000000000{index}",
                draft_uuid=f"10000000-0000-4000-8000-00000000000{index}",
                target_agent_id=f"synthetic-agent-{index}", owner_id=owner,
                agent_name=f"Synthetic {index}", agent_slug=f"synthetic-{index}",
                description=f"synthetic-{index}", observed_at=1)
            catalog.share_grants.create_grant(tx, owner_id=owner,
                token_sha256=str(index)*64, chat_id=f"synthetic-chat-{index}", scope="chat",
                component_id=None, snapshot_html="<p>Synthetic</p>",
                snapshot_json={"type":"text","value":f"synthetic-{index}"}, expires_at=None)
        metadata = {row["key"]: row["value"]
                    for row in tx.fetch_all("SELECT key,value FROM schema_meta")}
        catalog_digest = _schema_structure_digest(tx.fetch_all(CURRENT_SCHEMA_STRUCTURE_QUERY))
    print(json.dumps({"revision": CURRENT_DATA_PLANE_REVISION.schema_revision,
        "digest": MIGRATION_REGISTRY.digest, "catalog_digest": catalog_digest,
        "metadata": metadata, "migrations": [{"name": item.name,
            "sources": list(item.source_revisions), "target": item.target_revision,
            "checksum": item.checksum} for item in MIGRATION_REGISTRY.migrations]}, sort_keys=True))
finally:
    pool.close()
"""


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


def archive_source(root: Path, baseline_sha: str, target: Path) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", baseline_sha) is None:
        raise ValueError("baseline must be an exact commit SHA")
    if git(root, "rev-parse", "refs/remotes/origin/main").decode().strip() != baseline_sha:
        raise ValueError("baseline differs from the fetched origin/main")
    payload = git(root, "archive", baseline_sha, "src")
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        for member in archive.getmembers():
            path = Path(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.parts[0] != "src"
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError("baseline archive contains an unsafe source entry")
            destination = target / path
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("baseline archive source bytes are missing")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(stream.read())
    if not (target / "src" / "astralplane" / "__init__.py").is_file():
        raise ValueError("baseline archive has no Plane package")
    return git(root, "rev-parse", f"{baseline_sha}:src").decode().strip()


def validate_lineage(
    baseline: dict, revision=CURRENT_DATA_PLANE_REVISION, registry=MIGRATION_REGISTRY
) -> None:
    actual_revision, actual_digest = baseline["revision"], baseline["digest"]
    metadata = baseline["metadata"]
    if (
        metadata.get("revision") != actual_revision
        or metadata.get("astralplane_migration_digest") != actual_digest
    ):
        raise ValueError("baseline markers differ from its loaded registry")
    accepted = (
        revision.migration_digest
        if revision.schema_revision == actual_revision
        else revision.predecessor_digest_for(actual_revision)
    )
    if accepted != actual_digest:
        raise ValueError("candidate does not admit the actual main migration digest")
    candidate = {
        item.name: {
            "name": item.name,
            "sources": list(item.source_revisions),
            "target": item.target_revision,
            "checksum": item.checksum,
        }
        for item in registry.migrations
    }
    if any(candidate.get(item["name"]) != item for item in baseline["migrations"]):
        raise ValueError("candidate changed a historical main migration edge")


def fixture_snapshot(database: PlaneDatabase) -> dict:
    with database.transaction() as tx:
        rows = [dict(row) for row in tx.fetch_all(FIXTURE_QUERY)]
        if rows != EXPECTED_ROWS:
            raise ValueError("upgrade changed populated draft data")
        owners = [row["id"] for row in tx.fetch_all("SELECT id FROM users ORDER BY id")]
        shares = [
            dict(row)
            for row in tx.fetch_all(
                "SELECT user_id,token_sha256,snapshot_json,open_count "
                "FROM share_grant ORDER BY user_id"
            )
        ]
        if owners != ["upgrade-owner-1", "upgrade-owner-2"] or shares != [
            {
                "user_id": f"upgrade-owner-{index}",
                "token_sha256": str(index) * 64,
                "snapshot_json": {"type": "text", "value": f"synthetic-{index}"},
                "open_count": 0,
            }
            for index in (1, 2)
        ]:
            raise ValueError("upgrade changed populated owner/share data")
        return {"drafts": rows, "owners": owners, "shares": shares}


def baseline_input(root: Path = ROOT) -> tuple[Path, str]:
    supplied_root = os.environ.get("ASTRALPLANE_UPGRADE_BASELINE_ROOT")
    supplied_sha = os.environ.get("ASTRALPLANE_UPGRADE_BASELINE_SHA")
    if bool(supplied_root) != bool(supplied_sha):
        raise ValueError("external baseline source root and SHA must be supplied together")
    if supplied_root and supplied_sha:
        return Path(supplied_root).resolve(), supplied_sha
    return root, git(root, "rev-parse", "refs/remotes/origin/main").decode().strip()


def qualify(
    *,
    root: Path,
    baseline_sha: str,
    administrator_dsn: str,
    baseline_source_root: Path | None = None,
) -> dict:
    if not administrator_dsn:
        raise ValueError("real PostgreSQL DSN is required")
    name = "plane_upgrade_" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="plane-main-upgrade-") as directory:
        snapshot = Path(directory)
        source_tree = archive_source(baseline_source_root or root, baseline_sha, snapshot)
        administrator = psycopg2.connect(administrator_dsn)
        administrator.autocommit = True
        try:
            with administrator.cursor() as cursor:
                cursor.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
            configured = make_dsn(administrator_dsn, dbname=name)
            try:
                completed = subprocess.run(
                    [sys.executable, "-I", "-c", BOOTSTRAP, str(snapshot / "src")],
                    cwd=directory,
                    env={**os.environ, "PLANE_UPGRADE_DATABASE_DSN": configured},
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if completed.returncode:
                    raise ValueError("exact-main bootstrap failed")
                baseline = json.loads(completed.stdout)
                validate_lineage(baseline)
                pool = ConnectionPool(
                    create_postgres_driver_pool(configured, minimum_connections=1)
                )
                try:
                    database = PlaneDatabase(pool)
                    before = fixture_snapshot(database)
                    runner = MigrationRunner(
                        database,
                        revision=CURRENT_DATA_PLANE_REVISION,
                        registry=MIGRATION_REGISTRY,
                    )
                    first = runner.run(
                        expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision
                    )
                    after = fixture_snapshot(database)
                    repeated = runner.run(
                        expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision,
                    )
                    if (
                        before != after
                        or fixture_snapshot(database) != before
                        or not repeated.already_current
                        or repeated.applied_steps
                    ):
                        raise ValueError("upgrade repeat was not a data-preserving no-op")
                finally:
                    pool.close()
                return {
                    "contract": "astralplane.main-upgrade/v1",
                    "status": "passed",
                    "candidate_sha": git(root, "rev-parse", "HEAD").decode().strip(),
                    "candidate_source_tree": git(root, "rev-parse", "HEAD:src").decode().strip(),
                    "baseline_sha": baseline_sha,
                    "baseline_source_tree": source_tree,
                    "baseline_revision": baseline["revision"],
                    "baseline_migration_digest": baseline["digest"],
                    "baseline_catalog_digest": baseline["catalog_digest"],
                    "candidate_revision": first.target_revision,
                    "candidate_migration_digest": first.migration_digest,
                    "applied_steps": list(first.applied_steps),
                    "repeat_applied_steps": [],
                    "populated_owner_count": len(before["owners"]),
                    "populated_draft_count": len(before["drafts"]),
                    "populated_share_count": len(before["shares"]),
                    "release_authorized": False,
                }
            finally:
                with administrator.cursor() as cursor:
                    cursor.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        finally:
            administrator.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qualify a populated upgrade from exact main.")
    parser.add_argument("--baseline-sha", required=True)
    parser.add_argument("--baseline-source-root", type=Path, default=ROOT)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = qualify(
            root=ROOT,
            baseline_sha=args.baseline_sha,
            administrator_dsn=os.environ.get(DATABASE_ENV, ""),
            baseline_source_root=args.baseline_source_root,
        )
    except Exception:
        report = {
            "contract": "astralplane.main-upgrade/v1",
            "status": "failed",
            "baseline_sha": args.baseline_sha,
            "release_authorized": False,
        }
        args.report.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
        print("main upgrade qualification failed; admission remains closed", file=sys.stderr)
        return 2
    args.report.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
