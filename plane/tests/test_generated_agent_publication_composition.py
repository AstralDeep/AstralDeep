"""Tests for the lock ordering between src/astralplane/immutable_bundle_store.py and
repositories/generated_agent_publications.py: journal and filesystem work stay
outside each other's commit.
"""

from __future__ import annotations

import hashlib
import json

from astralplane.immutable_bundle_store import (
    BundlePublicationKey,
    FinalizedBundle,
    ImmutableBundleStore,
    canonical_bundle_digest,
)
from astralplane.repositories.generated_agent_publications import (
    GENERATED_AGENT_BUNDLE_CONTRACT,
)

DRAFT_UUID = "10000000-0000-4000-8000-000000000001"
PUBLICATION_ID = "20000000-0000-4000-8000-000000000001"
REVISION_ID = "30000000-0000-4000-8000-000000000001"
AGENT_ID = "composition-agent"
LOCK_DIGEST = "b" * 64
FILES = {
    "agent_main.py": "main\n",
    "astralprims_ui.py": "ui\n",
    "protected_executor.py": "executor\n",
    "mcp_tools.py": "tools\n",
}


def _bundle() -> FinalizedBundle:
    digest = canonical_bundle_digest(FILES, GENERATED_AGENT_BUNDLE_CONTRACT)
    manifest = {
        "agent_id": AGENT_ID,
        "agent_name": "Composition Agent",
        "bundle_sha256": digest,
        "constitution_version": "0.1.0",
        "description": "Lock-order test fixture",
        "digest_algorithm": "sha256",
        "files": [
            {
                "name": name,
                "sha256": hashlib.sha256(FILES[name].encode("utf-8")).hexdigest(),
                "size_bytes": len(FILES[name].encode("utf-8")),
            }
            for name in GENERATED_AGENT_BUNDLE_CONTRACT.file_names
        ],
        "manifest_version": 2,
        "required_runtime_lock_sha256": LOCK_DIGEST,
        "revision_id": REVISION_ID,
        "runtime_contract_version": 3,
    }
    manifest_json = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    return FinalizedBundle(
        contract=GENERATED_AGENT_BUNDLE_CONTRACT,
        files=FILES,
        bundle_sha256=digest,
        manifest=manifest,
        manifest_json=manifest_json,
    )


def test_split_stage_promote_and_recover_keep_db_work_outside_filesystem_lock(
    tmp_path,
) -> None:
    store = ImmutableBundleStore(
        tmp_path / "bundles",
        contract=GENERATED_AGENT_BUNDLE_CONTRACT,
    )
    finalized = _bundle()
    key = BundlePublicationKey(
        scope_id=AGENT_ID,
        staging_id=DRAFT_UUID,
        source_revision=1,
        publication_id=PUBLICATION_ID,
        revision_id=REVISION_ID,
    )
    events: list[str] = []
    in_filesystem_callback = False

    def db_transaction(name: str) -> None:
        assert not in_filesystem_callback
        events.append(f"db:{name}")

    def local_fence(boundary: str) -> None:
        nonlocal in_filesystem_callback
        assert not in_filesystem_callback
        in_filesystem_callback = True
        try:
            events.append(f"fs:{boundary}")
        finally:
            in_filesystem_callback = False

    db_transaction("assert_before_stage")
    staged = store.stage(finalized, key=key, fence_check=local_fence)
    db_transaction("mark_staged")
    db_transaction("mark_validated_with_results")
    db_transaction("assert_before_promote")
    published = store.promote_staged(staged, fence_check=local_fence)
    db_transaction("commit_published")

    db_transaction("inventory_and_rebind")
    db_transaction("assert_before_recover")
    recovered = store.recover(
        key=key,
        expected_bundle_sha256=finalized.bundle_sha256,
        expected_manifest_sha256=finalized.manifest_sha256,
        expected_runtime_metadata=finalized.runtime_metadata,
        fence_check=local_fence,
    )
    db_transaction("replay_commit")

    assert published.bundle_sha256 == finalized.bundle_sha256
    assert recovered.published is not None
    assert recovered.published.bundle_sha256 == finalized.bundle_sha256
    assert events == [
        "db:assert_before_stage",
        "fs:before_stage",
        "db:mark_staged",
        "db:mark_validated_with_results",
        "db:assert_before_promote",
        "fs:before_replace",
        "fs:before_replace",
        "db:commit_published",
        "db:inventory_and_rebind",
        "db:assert_before_recover",
        "fs:before_recovery",
        "db:replay_commit",
    ]
