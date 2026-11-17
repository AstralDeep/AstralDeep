"""Fault-injected tests for src/astralplane/immutable_bundle_store.py: two-phase
stage/promote/recover, journal-gap crash recovery, and cross-platform durable file
replacement.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import shutil
import stat
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

import astralplane.immutable_bundle_store as publication
from astralplane.immutable_bundle_store import (
    ArtifactCollisionError,
    ArtifactIntegrityError,
    ArtifactPublicationError,
    ArtifactPublicationRevokedError,
    ArtifactReconciliationError,
    BundlePublicationKey,
    BundleRecoveryDisposition,
    FinalizedBundle,
    ImmutableBundleContract,
    ImmutableBundleStore,
    StagedBundleReceipt,
)

_AGENT_ID = "ua-atomic-agent-owner"
_CONSTITUTION_VERSION = "0.1.0"
_RUNTIME_LOCK_SHA256 = "9" * 64
_BUNDLE_FILENAMES = (
    "agent_main.py",
    "astralprims_ui.py",
    "protected_executor.py",
    "mcp_tools.py",
)
_CONTRACT = ImmutableBundleContract(
    file_names=_BUNDLE_FILENAMES,
    scope_identity_field="agent_id",
    required_text_metadata_fields=(
        "agent_name",
        "description",
        "constitution_version",
    ),
    nonempty_text_metadata_fields=("constitution_version",),
)


def _ids() -> tuple[str, str, str]:
    return str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())


def _bundle(revision_id: str, *, marker: str = "one"):
    files = {
        "agent_main.py": '"""Generated entry point."""\n',
        "astralprims_ui.py": '"""Generated structured UI."""\n',
        "protected_executor.py": '"""Generated protected executor."""\n',
        "mcp_tools.py": (
            '"""Generated tools."""\n'
            f'MARKER = "{marker}"\n'
            "TOOL_REGISTRY = {}\n"
        ),
    }
    bundle_sha256 = publication.canonical_bundle_digest(files, _CONTRACT)
    manifest = {
        "manifest_version": 2,
        "runtime_contract_version": 3,
        "revision_id": revision_id,
        "agent_id": _AGENT_ID,
        "agent_name": "Atomic Agent",
        "description": "Safely tests immutable bundle publication.",
        "constitution_version": _CONSTITUTION_VERSION,
        "required_runtime_lock_sha256": _RUNTIME_LOCK_SHA256,
        "digest_algorithm": "sha256",
        "bundle_sha256": bundle_sha256,
        "files": [
            {
                "name": filename,
                "sha256": hashlib.sha256(files[filename].encode("utf-8")).hexdigest(),
                "size_bytes": len(files[filename].encode("utf-8")),
            }
            for filename in _BUNDLE_FILENAMES
        ],
    }
    manifest_json = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"
    return FinalizedBundle(
        contract=_CONTRACT,
        files=files,
        bundle_sha256=bundle_sha256,
        manifest=manifest,
        manifest_json=manifest_json,
    )


def _store(root):
    return ImmutableBundleStore(root, contract=_CONTRACT)


def _key(draft_uuid, publication_id, revision_id):
    return BundlePublicationKey(
        scope_id=_AGENT_ID,
        staging_id=draft_uuid,
        source_revision=7,
        publication_id=publication_id,
        revision_id=revision_id,
    )


def _publish(store, finalized, draft_uuid, publication_id, revision_id, **kwargs):
    return store.publish(
        finalized,
        key=_key(draft_uuid, publication_id, revision_id),
        **kwargs,
    )


def _stage(store, finalized, draft_uuid, publication_id, revision_id, **kwargs):
    return store.stage(
        finalized,
        key=_key(draft_uuid, publication_id, revision_id),
        **kwargs,
    )


def _recover(store, finalized, draft_uuid, publication_id, revision_id, **kwargs):
    return store.recover(
        key=_key(draft_uuid, publication_id, revision_id),
        expected_bundle_sha256=finalized.bundle_sha256,
        expected_manifest_sha256=finalized.manifest_sha256,
        expected_runtime_metadata=finalized.runtime_metadata,
        **kwargs,
    )


def _rebuild_bundle(
    finalized,
    *,
    contract=None,
    files=None,
    bundle_sha256=None,
    manifest=None,
    manifest_json=None,
):
    selected_contract = contract or finalized.contract
    selected_files = dict(finalized.files) if files is None else files
    selected_manifest = finalized.manifest_dict() if manifest is None else manifest
    if manifest_json is None:
        manifest_json = json.dumps(
            selected_manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n"
    return FinalizedBundle(
        contract=selected_contract,
        files=selected_files,
        bundle_sha256=(
            finalized.bundle_sha256 if bundle_sha256 is None else bundle_sha256
        ),
        manifest=selected_manifest,
        manifest_json=manifest_json,
    )


def test_publication_key_derives_exact_canonical_journal_paths():
    staging_id, publication_id, revision_id = _ids()
    key = BundlePublicationKey(
        scope_id=_AGENT_ID,
        staging_id=staging_id,
        source_revision=7,
        publication_id=publication_id,
        revision_id=revision_id,
    )

    assert publication.paths_for(key) == publication.BundlePublicationPaths(
        staging_relative_path=f"staging/{staging_id}/7/{publication_id}",
        revision_relative_path=f"revisions/{_AGENT_ID}/{revision_id}",
        quarantine_relative_path=f"quarantine/{publication_id}",
    )
    assert not publication.paths_for(key).staging_relative_path.startswith(".")
    with pytest.raises(TypeError, match="publication key"):
        publication.paths_for(object())


def test_public_two_phase_and_recovery_symbols_are_direct_module_contract():
    expected = {
        "BundleRecoveryDisposition",
        "BundleRecoveryResult",
        "StagedBundleReceipt",
        "runtime_metadata_for_manifest",
    }

    assert expected <= set(publication.__all__)
    assert publication.StagedBundleReceipt is StagedBundleReceipt
    assert publication.__name__ == "astralplane.immutable_bundle_store"


def test_recovery_metadata_is_derived_from_validated_manifest_without_files():
    _, _, revision_id = _ids()
    finalized = _bundle(revision_id)

    from_json = publication.runtime_metadata_for_manifest(
        _CONTRACT,
        finalized.manifest_json,
    )
    from_mapping = publication.runtime_metadata_for_manifest(
        _CONTRACT,
        finalized.manifest,
    )

    assert dict(from_json) == dict(finalized.runtime_metadata)
    assert dict(from_mapping) == dict(finalized.runtime_metadata)
    with pytest.raises(TypeError):
        from_json["description"] = "mutable"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda manifest: manifest.__setitem__("manifest_version", 2.0), "version"),
        (
            lambda manifest: manifest["files"][0].__setitem__(
                "size_bytes",
                True,
            ),
            "file metadata",
        ),
        (lambda manifest: manifest.__setitem__("unexpected", "value"), "fields"),
        (
            lambda manifest: manifest.__setitem__(
                "required_runtime_lock_sha256",
                "not-a-digest",
            ),
            "lock digest",
        ),
    ],
)
def test_recovery_metadata_rejects_weaker_manifest_shapes(mutation, message):
    _, _, revision_id = _ids()
    finalized = _bundle(revision_id)
    manifest = finalized.manifest_dict()
    mutation(manifest)

    with pytest.raises(ValueError, match=message):
        publication.runtime_metadata_for_manifest(_CONTRACT, manifest)


def test_recovery_metadata_requires_canonical_journal_manifest_json():
    _, _, revision_id = _ids()
    finalized = _bundle(revision_id)
    noncanonical = json.dumps(finalized.manifest_dict(), indent=2)

    with pytest.raises(ValueError, match="canonical"):
        publication.runtime_metadata_for_manifest(_CONTRACT, noncanonical)


def test_recovery_metadata_rejects_invalid_input_domains():
    _, _, revision_id = _ids()
    finalized = _bundle(revision_id)
    manifest = finalized.manifest_dict()

    with pytest.raises(TypeError, match="contract"):
        publication.runtime_metadata_for_manifest(object(), manifest)
    with pytest.raises(ValueError, match="size limit"):
        publication.runtime_metadata_for_manifest(
            _CONTRACT,
            "{" + (" " * _CONTRACT.max_manifest_bytes),
        )
    with pytest.raises(ValueError, match="invalid JSON"):
        publication.runtime_metadata_for_manifest(_CONTRACT, "{broken}\n")
    with pytest.raises(ValueError, match="must be an object"):
        publication.runtime_metadata_for_manifest(_CONTRACT, "[]\n")
    with pytest.raises(TypeError, match="mapping or canonical"):
        publication.runtime_metadata_for_manifest(_CONTRACT, 7)

    not_json = finalized.manifest_dict()
    not_json["description"] = float("nan")
    with pytest.raises(ValueError, match="bounded JSON"):
        publication.runtime_metadata_for_manifest(_CONTRACT, not_json)

    oversized = finalized.manifest_dict()
    oversized["description"] = "x" * _CONTRACT.max_manifest_bytes
    with pytest.raises(ValueError, match="size limit"):
        publication.runtime_metadata_for_manifest(_CONTRACT, oversized)

    bad_digest = finalized.manifest_dict()
    bad_digest["bundle_sha256"] = "not-a-digest"
    with pytest.raises(ValueError, match="bundle digest"):
        publication.runtime_metadata_for_manifest(_CONTRACT, bad_digest)


def test_public_constructors_reject_untyped_contract_and_manifest_inputs(tmp_path):
    _, _, revision_id = _ids()
    finalized = _bundle(revision_id)

    with pytest.raises(TypeError, match="contract"):
        FinalizedBundle(
            contract=object(),
            files={},
            bundle_sha256="0" * 64,
            manifest={},
            manifest_json="{}\n",
        )
    with pytest.raises(TypeError, match="manifest_json"):
        replace(finalized, manifest_json=object())
    with pytest.raises(TypeError, match="contract"):
        ImmutableBundleStore(tmp_path / "invalid", contract=object())


def test_stage_and_promote_split_revalidates_exact_live_receipt(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)

    staged = _stage(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )

    assert isinstance(staged, StagedBundleReceipt)
    assert staged.paths == publication.paths_for(staged.publication_key)
    staging_path = store.root.joinpath(
        *Path(staged.paths.staging_relative_path).parts
    )
    assert staging_path.is_dir()
    assert not (store.root / staged.paths.revision_relative_path).exists()

    published = store.promote_staged(staged)

    assert published.receipt is not None
    assert published.receipt.publication_key == staged.publication_key
    assert published.storage_identity == staged.storage_identity
    assert not staging_path.exists()


def test_two_phase_journal_gap_occurs_after_root_lock_release(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    original_lock = store._publication_lock
    lock_held = False

    @contextmanager
    def tracking_lock():
        nonlocal lock_held
        assert not lock_held
        with original_lock():
            lock_held = True
            try:
                yield
            finally:
                lock_held = False

    monkeypatch.setattr(store, "_publication_lock", tracking_lock)

    staged = _stage(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )
    assert not lock_held
    journal_evidence = (
        staged.publication_key,
        staged.bundle_sha256,
        staged.manifest_sha256,
        dict(staged.runtime_metadata),
    )
    assert not lock_held

    published = store.promote_staged(staged)

    assert not lock_held
    assert published.receipt is not None
    assert published.receipt.publication_key == journal_evidence[0]


def test_stage_is_idempotent_for_exact_durable_bytes(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)

    first = _stage(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )
    replay = _stage(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )

    assert replay.storage_identity == first.storage_identity
    assert replay.bundle_sha256 == first.bundle_sha256


def test_publish_never_replaces_a_different_live_stage_receipt(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    first_bundle = _bundle(revision_id, marker="stage-owner")
    competing_bundle = _bundle(revision_id, marker="competing-publish")
    staged = _stage(
        store,
        first_bundle,
        draft_uuid,
        publication_id,
        revision_id,
    )
    staging_path = store.root.joinpath(
        *Path(staged.paths.staging_relative_path).parts
    )

    with pytest.raises(ArtifactCollisionError, match="another valid bundle"):
        _publish(
            store,
            competing_bundle,
            draft_uuid,
            publication_id,
            revision_id,
        )

    assert publication._path_entry_state(staging_path).identity == (
        staged.storage_identity
    )
    promoted = store.promote_staged(staged)
    assert 'MARKER = "stage-owner"' in promoted.files["mcp_tools.py"]


def test_stage_rejects_an_already_published_revision_and_preserves_it(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    published_bundle = _bundle(revision_id, marker="published")
    competing_bundle = _bundle(revision_id, marker="new-stage")
    published = _publish(
        store,
        published_bundle,
        draft_uuid,
        publication_id,
        revision_id,
    )

    with pytest.raises(ArtifactCollisionError, match="recover it instead"):
        _stage(
            store,
            competing_bundle,
            draft_uuid,
            publication_id,
            revision_id,
        )

    loaded = store.load(
        published.bundle_relative_path,
        expected_digest=published.bundle_sha256,
        expected_manifest_digest=published.manifest_sha256,
    )
    assert 'MARKER = "published"' in loaded.files["mcp_tools.py"]


def test_stage_and_promote_reject_forged_public_inputs(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    key = _key(draft_uuid, publication_id, revision_id)

    with pytest.raises(TypeError, match="finalized"):
        store.stage(object(), key=key)
    with pytest.raises(TypeError, match="publication key"):
        store.stage(finalized, key=object())
    incompatible_contract = replace(
        _CONTRACT,
        max_manifest_bytes=_CONTRACT.max_manifest_bytes + 1,
    )
    incompatible_store = ImmutableBundleStore(
        tmp_path / "incompatible",
        contract=incompatible_contract,
    )
    with pytest.raises(ArtifactIntegrityError, match="contract"):
        incompatible_store.stage(finalized, key=key)
    with pytest.raises(ArtifactIntegrityError, match="scope"):
        store.stage(finalized, key=replace(key, scope_id="other-owner"))
    with pytest.raises(ArtifactIntegrityError, match="revision"):
        store.stage(finalized, key=replace(key, revision_id=str(uuid.uuid4())))

    staged = store.stage(finalized, key=key)
    with pytest.raises(TypeError, match="receipt"):
        store.promote_staged(object())
    forged_receipts = (
        replace(staged, publication_key=object()),
        replace(
            staged,
            paths=replace(staged.paths, revision_relative_path="revisions/wrong/path"),
        ),
        replace(staged, bundle_sha256="invalid"),
        replace(staged, manifest_sha256="invalid"),
    )
    for forged in forged_receipts:
        with pytest.raises(ArtifactIntegrityError):
            store.promote_staged(forged)


def test_quarantine_staged_is_exact_and_idempotently_redurable(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    staged = _stage(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )
    staging_path = store.root.joinpath(
        *Path(staged.paths.staging_relative_path).parts
    )
    quarantine_path = store.root.joinpath(
        *Path(staged.paths.quarantine_relative_path).parts
    )

    store.quarantine_staged(staged)
    store.quarantine_staged(staged)

    assert not staging_path.exists()
    assert publication._path_entry_state(quarantine_path).identity == (
        staged.storage_identity
    )
    recovered = _recover(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )
    assert recovered.disposition is BundleRecoveryDisposition.PARTIAL
    assert recovered.quarantined


def test_quarantine_staged_retry_repairs_post_move_fsync_failure(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    staged = _stage(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )
    staging_path = store.root.joinpath(
        *Path(staged.paths.staging_relative_path).parts
    )
    quarantine_path = store.root.joinpath(
        *Path(staged.paths.quarantine_relative_path).parts
    )
    original_fsync = store._fsync_directory
    failed = False

    def fail_quarantine_parent_once(path, *, descriptor=None):
        nonlocal failed
        if path == quarantine_path.parent and not failed:
            failed = True
            raise OSError("simulated quarantine parent fsync failure")
        return original_fsync(path, descriptor=descriptor)

    monkeypatch.setattr(store, "_fsync_directory", fail_quarantine_parent_once)
    with pytest.raises(OSError, match="quarantine parent fsync"):
        store.quarantine_staged(staged)
    assert not staging_path.exists()
    assert publication._path_entry_state(quarantine_path).identity == (
        staged.storage_identity
    )

    monkeypatch.setattr(store, "_fsync_directory", original_fsync)
    store.quarantine_staged(staged)
    assert publication._path_entry_state(quarantine_path).identity == (
        staged.storage_identity
    )


def test_quarantine_staged_refuses_promoted_replaced_and_occupied_entries(
    tmp_path,
):
    promoted_store = _store(tmp_path / "promoted")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    promoted_receipt = _stage(
        promoted_store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )
    promoted_store.promote_staged(promoted_receipt)
    with pytest.raises(ArtifactReconciliationError, match="already promoted"):
        promoted_store.quarantine_staged(promoted_receipt)

    replaced_store = _store(tmp_path / "replaced")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    replaced_receipt = _stage(
        replaced_store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )
    replaced_path = replaced_store.root.joinpath(
        *Path(replaced_receipt.paths.staging_relative_path).parts
    )
    shutil.rmtree(replaced_path)
    replaced_path.mkdir()
    (replaced_path / "foreign.txt").write_text("foreign", encoding="utf-8")
    with pytest.raises(ArtifactReconciliationError):
        replaced_store.quarantine_staged(replaced_receipt)
    assert (replaced_path / "foreign.txt").read_text(encoding="utf-8") == (
        "foreign"
    )

    occupied_store = _store(tmp_path / "occupied")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    occupied_receipt = _stage(
        occupied_store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )
    quarantine_path = occupied_store.root.joinpath(
        *Path(occupied_receipt.paths.quarantine_relative_path).parts
    )
    quarantine_path.mkdir(parents=True)
    marker = quarantine_path / "foreign.txt"
    marker.write_text("foreign", encoding="utf-8")
    with pytest.raises(ArtifactReconciliationError, match="occupied"):
        occupied_store.quarantine_staged(occupied_receipt)
    assert marker.read_text(encoding="utf-8") == "foreign"


def test_promote_rejects_replaced_staging_receipt_without_touching_entry(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    staged = _stage(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )
    staging_path = store.root.joinpath(
        *Path(staged.paths.staging_relative_path).parts
    )
    shutil.rmtree(staging_path)
    staging_path.mkdir()
    marker = staging_path / "foreign.txt"
    marker.write_text("foreign", encoding="utf-8")

    with pytest.raises(ArtifactIntegrityError):
        store.promote_staged(staged)

    assert marker.read_text(encoding="utf-8") == "foreign"
    assert not (store.root / staged.paths.revision_relative_path).exists()


def test_recovery_reports_absent_without_filesystem_mutation(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)

    result = _recover(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )

    assert result.disposition is BundleRecoveryDisposition.ABSENT
    assert result.published is None
    assert not result.quarantined


def test_recovery_promotes_crash_durable_staging_without_bundle_files(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)

    def crash_after_staging(boundary):
        if boundary == "after_staging_fsync":
            raise RuntimeError("simulated process crash")

    with pytest.raises(RuntimeError, match="simulated process crash"):
        _publish(
            store,
            finalized,
            draft_uuid,
            publication_id,
            revision_id,
            fault_hook=crash_after_staging,
        )

    result = _recover(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )
    replay = _recover(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )

    assert result.disposition is BundleRecoveryDisposition.STAGING_PROMOTED
    assert result.published is not None
    assert result.published.bundle_sha256 == finalized.bundle_sha256
    assert result.published.receipt is not None
    assert replay.disposition is BundleRecoveryDisposition.FINAL_VALID
    assert replay.published is not None
    assert replay.published.storage_identity == result.published.storage_identity


def test_recovery_redurables_native_commit_after_process_crash(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)

    def crash_after_replace(boundary):
        if boundary == "after_replace":
            raise RuntimeError("simulated native commit crash")

    with pytest.raises(RuntimeError, match="simulated native commit crash"):
        _publish(
            store,
            finalized,
            draft_uuid,
            publication_id,
            revision_id,
            fault_hook=crash_after_replace,
        )

    result = _recover(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )

    assert result.disposition is BundleRecoveryDisposition.FINAL_VALID
    assert result.published is not None
    assert result.published.bundle_sha256 == finalized.bundle_sha256


def test_recovery_quarantines_partial_staging_by_exact_identity_idempotently(
    tmp_path,
):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    key = _key(draft_uuid, publication_id, revision_id)
    paths = publication.paths_for(key)
    staging_path = store.root.joinpath(*Path(paths.staging_relative_path).parts)
    quarantine_path = store.root.joinpath(
        *Path(paths.quarantine_relative_path).parts
    )
    staging_path.mkdir(parents=True)
    (staging_path / "agent_main.py").write_text("partial", encoding="utf-8")
    before = publication._path_entry_state(staging_path)

    result = _recover(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )
    replay = _recover(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )

    assert result.disposition is BundleRecoveryDisposition.PARTIAL
    assert result.quarantined
    assert not staging_path.exists()
    assert publication._path_entry_state(quarantine_path).identity == before.identity
    assert replay.disposition is BundleRecoveryDisposition.PARTIAL
    assert replay.quarantined


def test_recovery_classifies_other_valid_bundle_as_collision_without_moving_it(
    tmp_path,
):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    staged_bundle = _bundle(revision_id, marker="other-valid-stage")
    journal_bundle = _bundle(revision_id, marker="journal-expected")
    staged = _stage(
        store,
        staged_bundle,
        draft_uuid,
        publication_id,
        revision_id,
    )
    staging_path = store.root.joinpath(
        *Path(staged.paths.staging_relative_path).parts
    )

    result = _recover(
        store,
        journal_bundle,
        draft_uuid,
        publication_id,
        revision_id,
    )

    assert result.disposition is BundleRecoveryDisposition.COLLISION
    assert not result.quarantined
    assert publication._path_entry_state(staging_path).identity == (
        staged.storage_identity
    )
    promoted = store.promote_staged(staged)
    assert 'MARKER = "other-valid-stage"' in promoted.files["mcp_tools.py"]


def test_recovery_classifies_other_valid_quarantine_as_collision(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    staged_bundle = _bundle(revision_id, marker="other-valid-quarantine")
    journal_bundle = _bundle(revision_id, marker="journal-expected")
    staged = _stage(
        store,
        staged_bundle,
        draft_uuid,
        publication_id,
        revision_id,
    )
    staging_path = store.root.joinpath(
        *Path(staged.paths.staging_relative_path).parts
    )
    quarantine_path = store.root.joinpath(
        *Path(staged.paths.quarantine_relative_path).parts
    )
    store._relocate_exact_entry(
        staging_path,
        quarantine_path,
        expected_identity=staged.storage_identity,
    )

    result = _recover(
        store,
        journal_bundle,
        draft_uuid,
        publication_id,
        revision_id,
    )

    assert result.disposition is BundleRecoveryDisposition.COLLISION
    assert result.quarantined
    assert publication._path_entry_state(quarantine_path).identity == (
        staged.storage_identity
    )


def test_recovery_rechecks_fence_and_cancellation_before_quarantine_move(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    paths = publication.paths_for(_key(draft_uuid, publication_id, revision_id))
    staging_path = store.root.joinpath(*Path(paths.staging_relative_path).parts)
    quarantine_path = store.root.joinpath(
        *Path(paths.quarantine_relative_path).parts
    )
    staging_path.mkdir(parents=True)
    (staging_path / "partial.txt").write_text("partial", encoding="utf-8")
    cancelled = threading.Event()
    boundaries = []

    def cancel_at_quarantine(boundary):
        boundaries.append(boundary)
        if boundary == "before_recovery_quarantine":
            cancelled.set()

    with pytest.raises(ArtifactPublicationRevokedError):
        _recover(
            store,
            finalized,
            draft_uuid,
            publication_id,
            revision_id,
            fence_check=cancel_at_quarantine,
            cancellation_event=cancelled,
        )

    assert boundaries == ["before_recovery", "before_recovery_quarantine"]
    assert staging_path.is_dir()
    assert not quarantine_path.exists()


def test_recovery_quarantines_foreign_staging_entry_without_traversal(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    paths = publication.paths_for(_key(draft_uuid, publication_id, revision_id))
    staging_path = store.root.joinpath(*Path(paths.staging_relative_path).parts)
    staging_path.parent.mkdir(parents=True)
    staging_path.write_text("foreign entry", encoding="utf-8")
    before = publication._path_entry_state(staging_path)

    result = _recover(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )

    quarantine_path = store.root.joinpath(
        *Path(paths.quarantine_relative_path).parts
    )
    assert result.disposition is BundleRecoveryDisposition.FOREIGN
    assert result.quarantined
    assert not staging_path.exists()
    assert publication._path_entry_state(quarantine_path).identity == before.identity


def test_recovery_preserves_foreign_immutable_destination_as_collision(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    paths = publication.paths_for(_key(draft_uuid, publication_id, revision_id))
    revision_path = store.root.joinpath(*Path(paths.revision_relative_path).parts)
    revision_path.mkdir(parents=True)
    marker = revision_path / "foreign.txt"
    marker.write_text("do not overwrite", encoding="utf-8")
    before = publication._path_entry_state(revision_path)

    result = _recover(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )

    assert result.disposition is BundleRecoveryDisposition.COLLISION
    assert publication._path_entry_state(revision_path).identity == before.identity
    assert marker.read_text(encoding="utf-8") == "do not overwrite"


def test_recovery_fence_and_cancellation_leave_valid_staging_unmoved(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    staged = _stage(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )
    staging_path = store.root.joinpath(
        *Path(staged.paths.staging_relative_path).parts
    )
    revision_path = store.root.joinpath(
        *Path(staged.paths.revision_relative_path).parts
    )

    with pytest.raises(RuntimeError, match="stale journal fence"):
        _recover(
            store,
            finalized,
            draft_uuid,
            publication_id,
            revision_id,
            fence_check=lambda _boundary: (_ for _ in ()).throw(
                RuntimeError("stale journal fence")
            ),
        )
    assert staging_path.is_dir()
    assert not revision_path.exists()

    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ArtifactPublicationRevokedError):
        _recover(
            store,
            finalized,
            draft_uuid,
            publication_id,
            revision_id,
            cancellation_event=cancelled,
        )
    assert staging_path.is_dir()
    assert not revision_path.exists()


def test_recovery_native_collision_never_overwrites_competing_destination(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    staged = _stage(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )
    staging_path = store.root.joinpath(
        *Path(staged.paths.staging_relative_path).parts
    )
    revision_path = store.root.joinpath(
        *Path(staged.paths.revision_relative_path).parts
    )
    original_replace = store._durable_replace

    def install_competitor_then_replace(source, destination, **kwargs):
        destination.mkdir()
        (destination / "competitor.txt").write_text(
            "preserve me",
            encoding="utf-8",
        )
        return original_replace(source, destination, **kwargs)

    monkeypatch.setattr(
        store,
        "_durable_replace",
        install_competitor_then_replace,
    )

    result = _recover(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )

    assert result.disposition is BundleRecoveryDisposition.COLLISION
    assert (revision_path / "competitor.txt").read_text(encoding="utf-8") == (
        "preserve me"
    )
    assert staging_path.is_dir()


def test_recovery_preserves_both_entries_when_quarantine_is_occupied(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    staged = _stage(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )
    staging_path = store.root.joinpath(
        *Path(staged.paths.staging_relative_path).parts
    )
    quarantine_path = store.root.joinpath(
        *Path(staged.paths.quarantine_relative_path).parts
    )
    quarantine_path.mkdir(parents=True)
    marker = quarantine_path / "competitor.txt"
    marker.write_text("preserve me", encoding="utf-8")
    staging_identity = publication._path_entry_state(staging_path).identity
    quarantine_identity = publication._path_entry_state(quarantine_path).identity

    result = _recover(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )

    assert result.disposition is BundleRecoveryDisposition.COLLISION
    assert publication._path_entry_state(staging_path).identity == staging_identity
    assert publication._path_entry_state(quarantine_path).identity == (
        quarantine_identity
    )
    assert marker.read_text(encoding="utf-8") == "preserve me"


def test_recovery_requires_exact_bounded_manifest_metadata(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    _publish(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )
    mismatched_metadata = dict(finalized.runtime_metadata)
    mismatched_metadata["description"] = "different journal evidence"

    mismatch = store.recover(
        key=_key(draft_uuid, publication_id, revision_id),
        expected_bundle_sha256=finalized.bundle_sha256,
        expected_manifest_sha256=finalized.manifest_sha256,
        expected_runtime_metadata=mismatched_metadata,
    )

    assert mismatch.disposition is BundleRecoveryDisposition.COLLISION
    with pytest.raises(ValueError, match="fields"):
        store.recover(
            key=_key(draft_uuid, publication_id, revision_id),
            expected_bundle_sha256=finalized.bundle_sha256,
            expected_manifest_sha256=finalized.manifest_sha256,
            expected_runtime_metadata={
                **dict(finalized.runtime_metadata),
                "unexpected": "not journal contract",
            },
        )


def test_recovery_rejects_forged_journal_evidence_before_filesystem_access(
    tmp_path,
):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    key = _key(draft_uuid, publication_id, revision_id)
    base = dict(finalized.runtime_metadata)

    with pytest.raises(TypeError, match="publication key"):
        store.recover(
            key=object(),
            expected_bundle_sha256=finalized.bundle_sha256,
            expected_manifest_sha256=finalized.manifest_sha256,
            expected_runtime_metadata=base,
        )
    for bundle_digest, manifest_digest in (
        ("invalid", finalized.manifest_sha256),
        (finalized.bundle_sha256, "invalid"),
    ):
        with pytest.raises(ValueError, match="lowercase SHA-256"):
            store.recover(
                key=key,
                expected_bundle_sha256=bundle_digest,
                expected_manifest_sha256=manifest_digest,
                expected_runtime_metadata=base,
            )
    with pytest.raises(TypeError, match="must be a mapping"):
        store.recover(
            key=key,
            expected_bundle_sha256=finalized.bundle_sha256,
            expected_manifest_sha256=finalized.manifest_sha256,
            expected_runtime_metadata=[],
        )

    invalid_metadata = []
    for field_name, field_value in (
        ("runtime_contract_version", True),
        ("required_runtime_lock_sha256", "invalid"),
        ("agent_name", 3),
        ("constitution_version", ""),
    ):
        candidate = dict(base)
        candidate[field_name] = field_value
        invalid_metadata.append(candidate)
    not_json = dict(base)
    not_json["runtime_contract_version"] = float("nan")
    invalid_metadata.append(not_json)
    oversized = dict(base)
    oversized["description"] = "x" * _CONTRACT.max_manifest_bytes
    invalid_metadata.append(oversized)
    for candidate in invalid_metadata:
        with pytest.raises(ValueError):
            store.recover(
                key=key,
                expected_bundle_sha256=finalized.bundle_sha256,
                expected_manifest_sha256=finalized.manifest_sha256,
                expected_runtime_metadata=candidate,
            )


@pytest.mark.parametrize(
    "values",
    [
        {"scope_id": "../escape"},
        {"staging_id": "not-a-uuid"},
        {"publication_id": "not-a-uuid"},
        {"revision_id": "not-a-uuid"},
        {"source_revision": -1},
        {"source_revision": True},
    ],
)
def test_publication_key_rejects_invalid_identifiers(values):
    staging_id, publication_id, revision_id = _ids()
    valid = {
        "scope_id": _AGENT_ID,
        "staging_id": staging_id,
        "source_revision": 7,
        "publication_id": publication_id,
        "revision_id": revision_id,
    }
    valid.update(values)

    with pytest.raises(ValueError):
        BundlePublicationKey(**valid)


@pytest.mark.parametrize(
    "contract_kwargs",
    [
        {"file_names": ()},
        {"file_names": ["mutable.py"]},
        {"file_names": ("same.py", "same.py")},
        {"file_names": ("../escape.py",)},
        {"file_names": ("manifest.json",)},
        {"file_names": ("agent.py",), "scope_identity_field": "bad/path"},
        {
            "file_names": ("agent.py",),
            "scope_identity_field": "manifest_version",
        },
        {
            "file_names": ("agent.py",),
            "revision_identity_field": "bundle_sha256",
        },
        {
            "file_names": ("agent.py",),
            "required_text_metadata_fields": ("files",),
        },
        {
            "file_names": ("agent.py",),
            "scope_identity_field": "revision_id",
        },
        {"file_names": ("agent.py",), "max_file_bytes": 0},
        {"file_names": ("agent.py",), "max_manifest_bytes": True},
        {"file_names": ("agent.py",), "required_text_metadata_fields": ["mutable"]},
        {
            "file_names": ("agent.py",),
            "nonempty_text_metadata_fields": ["mutable"],
        },
        {
            "file_names": ("agent.py",),
            "nonempty_text_metadata_fields": ("not-required",),
        },
    ],
)
def test_bundle_contract_rejects_unsafe_or_ambiguous_shapes(contract_kwargs):
    with pytest.raises(ValueError):
        ImmutableBundleContract(**contract_kwargs)


def test_finalized_bundle_is_deeply_immutable_and_mapping_constructible():
    revision_id = str(uuid.uuid4())
    finalized = _bundle(revision_id)
    rebuilt = FinalizedBundle.from_mapping(
        {
            "contract": _CONTRACT,
            "files": dict(finalized.files),
            "bundle_sha256": finalized.bundle_sha256,
            "manifest": finalized.manifest_dict(),
            "manifest_json": finalized.manifest_json,
        }
    )

    assert rebuilt == finalized
    assert rebuilt.scope_id == _AGENT_ID
    assert rebuilt.revision_id == revision_id
    assert rebuilt.manifest_sha256 == hashlib.sha256(
        rebuilt.manifest_json.encode("utf-8")
    ).hexdigest()
    assert rebuilt.runtime_metadata == {
        "runtime_contract_version": 3,
        "required_runtime_lock_sha256": _RUNTIME_LOCK_SHA256,
        "agent_name": "Atomic Agent",
        "description": "Safely tests immutable bundle publication.",
        "constitution_version": _CONSTITUTION_VERSION,
    }
    with pytest.raises(TypeError):
        rebuilt.files["agent_main.py"] = "changed"
    with pytest.raises(TypeError):
        rebuilt.manifest["agent_id"] = "changed"
    with pytest.raises(TypeError, match="mapping"):
        FinalizedBundle.from_mapping(object())
    with pytest.raises(ValueError, match="fields"):
        FinalizedBundle.from_mapping({})
    with pytest.raises(TypeError, match="contract"):
        publication.canonical_bundle_digest({}, object())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("manifest_version", 1),
        ("manifest_version", 2.0),
        ("agent_id", "../escape"),
        ("revision_id", "not-a-uuid"),
        ("bundle_sha256", "0" * 64),
        ("digest_algorithm", "sha512"),
        ("runtime_contract_version", True),
        ("required_runtime_lock_sha256", "invalid"),
        ("agent_name", 42),
        ("constitution_version", ""),
    ],
)
def test_finalized_bundle_rejects_invalid_manifest_identity_and_runtime(field, value):
    finalized = _bundle(str(uuid.uuid4()))
    manifest = finalized.manifest_dict()
    manifest[field] = value

    with pytest.raises(ValueError):
        _rebuild_bundle(finalized, manifest=manifest)


def test_finalized_bundle_rejects_noncanonical_or_mismatched_manifest():
    finalized = _bundle(str(uuid.uuid4()))
    manifest = finalized.manifest_dict()
    without_metadata = dict(manifest)
    without_metadata.pop("description")
    reversed_inventory = dict(manifest)
    reversed_inventory["files"] = list(reversed(manifest["files"]))
    malformed_record = finalized.manifest_dict()
    malformed_record["files"][0]["unexpected"] = True
    wrong_file_digest = finalized.manifest_dict()
    wrong_file_digest["files"][0]["sha256"] = "0" * 64
    non_text_key = finalized.manifest_dict()
    non_text_key[1] = "invalid"

    for candidate in (
        without_metadata,
        reversed_inventory,
        malformed_record,
        wrong_file_digest,
    ):
        with pytest.raises(ValueError):
            _rebuild_bundle(finalized, manifest=candidate)

    string_key_manifest = finalized.manifest_dict()
    string_key_manifest["1"] = "invalid"
    string_key_json = json.dumps(
        string_key_manifest,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    with pytest.raises(ValueError, match="keys must be text"):
        _rebuild_bundle(
            finalized,
            manifest=non_text_key,
            manifest_json=string_key_json,
        )

    with pytest.raises(ValueError, match="canonical"):
        _rebuild_bundle(
            finalized,
            manifest_json=json.dumps(manifest, indent=2),
        )
    with pytest.raises(ValueError, match="invalid JSON"):
        _rebuild_bundle(finalized, manifest_json="{")
    with pytest.raises(ValueError, match="must be an object"):
        _rebuild_bundle(finalized, manifest=[], manifest_json="[]\n")
    changed = finalized.manifest_dict()
    changed["description"] = "different"
    changed_json = json.dumps(
        changed,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    with pytest.raises(ValueError, match="does not match"):
        _rebuild_bundle(finalized, manifest_json=changed_json)


def test_finalized_bundle_rejects_invalid_files_digests_and_size_bounds():
    finalized = _bundle(str(uuid.uuid4()))
    with pytest.raises(TypeError, match="files must be a mapping"):
        _rebuild_bundle(finalized, files=[])
    missing_file = dict(finalized.files)
    missing_file.pop("mcp_tools.py")
    with pytest.raises(ValueError, match="inventory"):
        _rebuild_bundle(finalized, files=missing_file)
    non_text = dict(finalized.files)
    non_text["mcp_tools.py"] = b"bytes"
    with pytest.raises(TypeError, match="UTF-8"):
        _rebuild_bundle(finalized, files=non_text)
    with pytest.raises(ValueError, match="bundle_sha256"):
        _rebuild_bundle(finalized, bundle_sha256="invalid")

    one_byte_files = dict(finalized.files)
    one_byte_files["agent_main.py"] = "x"
    one_byte_digest = publication.canonical_bundle_digest(one_byte_files, _CONTRACT)
    bool_size_manifest = finalized.manifest_dict()
    bool_size_manifest["bundle_sha256"] = one_byte_digest
    bool_size_manifest["files"][0] = {
        "name": "agent_main.py",
        "sha256": hashlib.sha256(b"x").hexdigest(),
        "size_bytes": True,
    }
    with pytest.raises(ValueError, match="metadata"):
        _rebuild_bundle(
            finalized,
            files=one_byte_files,
            bundle_sha256=one_byte_digest,
            manifest=bool_size_manifest,
        )

    tiny_file_contract = replace(_CONTRACT, max_file_bytes=1)
    with pytest.raises(ValueError, match="size limit"):
        _rebuild_bundle(finalized, contract=tiny_file_contract)
    tiny_manifest_contract = replace(_CONTRACT, max_manifest_bytes=1)
    with pytest.raises(ValueError, match="manifest exceeds"):
        _rebuild_bundle(finalized, contract=tiny_manifest_contract)


def test_publish_fsyncs_and_atomically_exposes_exact_revision(tmp_path, monkeypatch):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    fsynced: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        fsynced.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    published = _publish(
        store, finalized, draft_uuid, publication_id, revision_id
    )

    assert published.bundle_relative_path == (
        f"revisions/{_AGENT_ID}/{revision_id}"
    )
    assert published.bundle_sha256 == finalized.bundle_sha256
    assert published.files == finalized.files
    assert published.manifest_dict() == finalized.manifest_dict()
    assert published.manifest_sha256 == hashlib.sha256(
        finalized.manifest_json.encode("utf-8")
    ).hexdigest()
    revision_path = store.root / published.bundle_relative_path
    assert {entry.name for entry in revision_path.iterdir()} == {
        *_BUNDLE_FILENAMES,
        "manifest.json",
    }
    assert not (store.root / "staging" / draft_uuid).joinpath(
        "7", publication_id
    ).exists()
    # Windows durability comes from MoveFileExW(WRITE_THROUGH), not os.fsync
    if os.name == "nt":
        assert len(fsynced) >= len(_BUNDLE_FILENAMES) + 1
    else:
        assert len(fsynced) >= 9


@pytest.mark.skipif(
    os.name == "nt" or not Path("/proc/self/fd").is_dir(),
    reason="requires POSIX descriptor path inspection",
)
def test_posix_publish_fsyncs_exact_directory_graph_in_order(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    recorded: list[Path] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            recorded.append(target.relative_to(store.root))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    published = _publish(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )

    staging_draft = Path("staging") / draft_uuid
    staging_revision = staging_draft / "7"
    staging_publication = staging_revision / publication_id
    revision_agent = Path("revisions") / _AGENT_ID
    revision = revision_agent / revision_id
    assert recorded == [
        staging_draft,
        Path("staging"),
        staging_revision,
        staging_draft,
        staging_publication,
        staging_revision,
        staging_publication,
        staging_revision,
        revision_agent,
        Path("revisions"),
        revision,
        revision_agent,
        staging_revision,
    ]

    recorded.clear()
    assert published.receipt is not None
    store.quarantine_receipt(published.receipt)
    assert recorded == [
        Path("quarantine") / publication_id,
        Path("quarantine"),
        revision_agent,
    ]


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX flock")
def test_posix_publication_lock_is_the_pinned_root_inode(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path / "artifacts")
    locked_identities = []
    real_flock = publication._fcntl.flock

    def recording_flock(descriptor, operation):
        if operation == publication._fcntl.LOCK_EX:
            status = os.fstat(descriptor)
            locked_identities.append((status.st_dev, status.st_ino))
        return real_flock(descriptor, operation)

    monkeypatch.setattr(publication._fcntl, "flock", recording_flock)
    with store._publication_lock():
        lock_path = store.root / ".publication.lock"
        lock_path.write_text("not-the-lock-domain", encoding="utf-8")

    assert locked_identities == [publication._directory_identity(store.root)]


def test_intermediate_reparse_never_redirects_staging_outside_root(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = store.root / "staging" / draft_uuid
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside), str(redirected))
    else:
        redirected.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(
            ArtifactPublicationError,
            match=r"reparse|trustworthy",
        ):
            _publish(
                store,
                finalized,
                draft_uuid,
                publication_id,
                revision_id,
            )
        assert list(outside.iterdir()) == []
    finally:
        if os.name == "nt":
            os.rmdir(redirected)
        else:
            redirected.unlink()


class _FakeWin32Error(OSError):
    def __init__(self, error_code: int) -> None:
        super().__init__(error_code, f"Win32 error {error_code}")
        self.winerror = error_code


def _install_win32_move_barrier(monkeypatch, outcomes):
    pending = iter(outcomes)
    calls = []
    sleeps = []

    def move(source, destination):
        calls.append((source, destination))
        outcome = next(pending)
        if outcome == 0:
            os.replace(source, destination)
        return outcome

    monkeypatch.setattr(publication, "_move_file_ex_write_through", move)
    monkeypatch.setattr(publication, "_win32_error", _FakeWin32Error)
    monkeypatch.setattr(
        publication,
        "_sleep_before_win32_move_retry",
        sleeps.append,
    )
    return calls, sleeps


@pytest.mark.skipif(os.name != "nt", reason="requires ctypes.WinError")
def test_win32_error_and_retry_sleep_preserve_native_seams(monkeypatch):
    sleeps = []
    monkeypatch.setattr(publication.time, "sleep", sleeps.append)

    error = publication._win32_error(87)
    publication._sleep_before_win32_move_retry(0.125)

    assert error.winerror == 87
    assert sleeps == [0.125]


def test_directory_identity_rejects_non_directory_win32_entry(
    tmp_path,
    monkeypatch,
):
    class _FakeWindowsOS:
        name = "nt"

    monkeypatch.setattr(publication, "os", _FakeWindowsOS)
    monkeypatch.setattr(
        publication,
        "_win32_directory_information",
        lambda _path: (0, 11, 22),
    )

    with pytest.raises(ArtifactPublicationError, match="not a directory"):
        publication._directory_identity(tmp_path)


def test_posix_directory_identity_rejects_missing_link_and_file(
    tmp_path,
    monkeypatch,
):
    class _FakePosixOS:
        name = "posix"

    monkeypatch.setattr(publication, "os", _FakePosixOS)

    with pytest.raises(ArtifactPublicationError, match="disappeared"):
        publication._directory_identity(tmp_path / "missing")

    class _LinkPath:
        @staticmethod
        def lstat():
            return type(
                "LinkStatus",
                (),
                {
                    "st_mode": publication.stat.S_IFLNK,
                    "st_dev": 1,
                    "st_ino": 2,
                    "st_file_attributes": 0,
                },
            )()

    with pytest.raises(ArtifactPublicationError, match="symbolic link"):
        publication._directory_identity(_LinkPath())

    regular_file = tmp_path / "file.txt"
    regular_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ArtifactPublicationError, match="not a directory"):
        publication._directory_identity(regular_file)


def test_missing_path_is_not_classified_as_reparse(tmp_path):
    assert not publication._path_is_reparse_or_symlink(tmp_path / "missing")


def test_store_uses_the_explicit_application_root(tmp_path):
    configured = tmp_path / "configured-artifacts"
    store = _store(configured)

    assert store.root == configured


def test_store_pin_rejects_path_outside_configured_root(tmp_path):
    store = _store(tmp_path / "artifacts")

    with (
        pytest.raises(ArtifactPublicationError, match="escaped"),
        store._pin_store_directory(tmp_path / "outside"),
    ):
        pass


def test_store_pin_rejects_changed_root_and_namespace_identities(tmp_path):
    store = _store(tmp_path / "artifacts-root")
    store._root_identity = (-1, -1)
    with (
        pytest.raises(ArtifactIntegrityError, match="root identity changed"),
        store._pin_store_directory(store.root),
    ):
        pass

    store = _store(tmp_path / "artifacts-namespace")
    store._staging_root_identity = (-1, -1)
    with (
        pytest.raises(
            ArtifactIntegrityError,
            match="namespace root identity changed",
        ),
        store._pin_store_directory(store._staging_root),
    ):
        pass


def test_identity_from_status_rejects_links_and_files(tmp_path):
    regular_file = tmp_path / "regular.txt"
    regular_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ArtifactPublicationError, match="not a directory"):
        publication._identity_from_status(regular_file.lstat())

    link = tmp_path / "link"
    try:
        link.symlink_to(regular_file)
    except OSError:
        pytest.skip("host cannot create a test symlink")
    with pytest.raises(ArtifactPublicationError, match="symbolic link"):
        publication._identity_from_status(link.lstat())


def test_replace_rejects_destination_parent_identity_change(tmp_path):
    source = tmp_path / "staging"
    expected_parent = tmp_path / "expected-parent"
    replacement_parent = tmp_path / "replacement-parent"
    source.mkdir()
    expected_parent.mkdir()
    replacement_parent.mkdir()

    with pytest.raises(ArtifactIntegrityError, match="parent identity changed"):
        publication._validate_replace_paths(
            source,
            replacement_parent / "revision",
            expected_source_identity=publication._directory_identity(source),
            expected_destination_parent_identity=(
                publication._directory_identity(expected_parent)
            ),
        )


def test_ensure_directory_rejects_untrusted_created_path(tmp_path, monkeypatch):
    store = _store(tmp_path / "artifacts")
    target = store.root / "untrusted"

    @contextmanager
    def reject_pin(_path, **_kwargs):
        raise ArtifactPublicationError("untrusted")
        yield

    monkeypatch.setattr(store, "_pin_store_directory", reject_pin)

    with pytest.raises(ArtifactPublicationError, match="not trustworthy"):
        store._ensure_directory(target)


def test_posix_directory_fsync_flushes_and_closes_descriptor(monkeypatch):
    calls = []

    class _FakePosixOS:
        name = "posix"
        O_RDONLY = 0
        O_DIRECTORY = 0

        @staticmethod
        def open(path, flags):
            calls.append(("open", path, flags))
            return 17

        @staticmethod
        def fsync(descriptor):
            calls.append(("fsync", descriptor))

        @staticmethod
        def close(descriptor):
            calls.append(("close", descriptor))

    monkeypatch.setattr(publication, "os", _FakePosixOS)
    path = object()

    ImmutableBundleStore._fsync_directory(path)

    assert calls == [
        ("open", path, 0),
        ("fsync", 17),
        ("close", 17),
    ]


def test_durable_replace_uses_native_replace_off_windows(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "staging"
    destination = tmp_path / "revision"
    source.mkdir()
    replacements = []
    real_replace = os.replace

    def move_no_replace(current_source, current_destination, **_kwargs):
        replacements.append((current_source, current_destination))
        real_replace(current_source, current_destination)
        return 0

    monkeypatch.setattr(publication.os, "name", "posix")
    monkeypatch.setattr(publication, "_move_posix_no_replace", move_no_replace)

    fence_checks = []
    ImmutableBundleStore._durable_replace(
        source,
        destination,
        retry_check=lambda: fence_checks.append("checked"),
    )

    assert replacements == [(source, destination)]
    assert fence_checks == ["checked"]
    assert destination.is_dir()


def test_posix_durable_replace_retries_eintr_after_revalidation(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "staging"
    destination = tmp_path / "revision"
    source.mkdir()
    calls = 0
    real_replace = os.replace

    def interrupted_then_committed(current_source, current_destination, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return errno.EINTR
        real_replace(current_source, current_destination)
        return 0

    monkeypatch.setattr(publication.os, "name", "posix")
    monkeypatch.setattr(
        publication,
        "_move_posix_no_replace",
        interrupted_then_committed,
    )
    checks = []

    outcome = ImmutableBundleStore._durable_replace(
        source,
        destination,
        retry_check=lambda: checks.append("checked"),
    )

    assert outcome == "committed"
    assert calls == 2
    assert checks == ["checked", "checked"]


@pytest.mark.parametrize(
    ("error_code", "expected_exception", "message"),
    [
        (
            errno.ENOSYS,
            ArtifactPublicationError,
            "no-replace directory rename is unavailable",
        ),
        (
            errno.EXDEV,
            ArtifactPublicationError,
            "different filesystems",
        ),
        (errno.EIO, OSError, None),
    ],
)
def test_posix_durable_replace_fails_closed_for_native_errors(
    tmp_path,
    monkeypatch,
    error_code,
    expected_exception,
    message,
):
    source = tmp_path / "staging"
    destination = tmp_path / "revision"
    source.mkdir()
    monkeypatch.setattr(publication.os, "name", "posix")
    monkeypatch.setattr(
        publication,
        "_move_posix_no_replace",
        lambda *_args, **_kwargs: error_code,
    )

    with pytest.raises(expected_exception, match=message):
        ImmutableBundleStore._durable_replace(source, destination)

    assert source.is_dir()
    assert not destination.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires native MoveFileExW")
def test_win32_native_move_never_replaces_competing_destination(tmp_path):
    source = tmp_path / "staging"
    destination = tmp_path / "revision"
    source.mkdir()
    destination.mkdir()
    marker = destination / "competitor.txt"
    marker.write_text("preserve", encoding="utf-8")

    error_code = publication._move_file_ex_write_through(
        source,
        destination,
    )

    assert error_code in publication._WIN32_COLLISION_MOVE_ERRORS
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert source.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX renameat2")
def test_posix_native_move_never_replaces_competing_destination(tmp_path):
    source = tmp_path / "staging"
    destination = tmp_path / "revision"
    source.mkdir()
    destination.mkdir()
    marker = destination / "competitor.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(ArtifactCollisionError):
        ImmutableBundleStore._durable_replace(
            source,
            destination,
        )

    assert marker.read_text(encoding="utf-8") == "preserve"
    assert source.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 reparse semantics")
def test_win32_directory_identity_rejects_python311_reparse_attributes(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        publication,
        "_win32_directory_information",
        lambda _path: (
            publication._FILE_ATTRIBUTE_DIRECTORY
            | publication._FILE_ATTRIBUTE_REPARSE_POINT,
            11,
            22,
        ),
    )

    with pytest.raises(ArtifactPublicationError, match="reparse point"):
        publication._directory_identity(tmp_path)


def test_windows_durable_replace_retries_transient_barriers_then_succeeds(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "staging"
    destination = tmp_path / "revision"
    source.mkdir()
    calls, sleeps = _install_win32_move_barrier(
        monkeypatch,
        [
            publication._WIN32_ERROR_SHARING_VIOLATION,
            publication._WIN32_ERROR_ACCESS_DENIED,
            0,
        ],
    )
    retry_checks = []

    ImmutableBundleStore._durable_replace_windows(
        source,
        destination,
        retry_check=lambda: retry_checks.append("checked"),
    )

    assert calls == [(source, destination)] * 3
    assert sleeps == list(publication._WIN32_MOVE_RETRY_DELAYS_SECONDS[:2])
    assert retry_checks == ["checked", "checked", "checked"]
    assert destination.is_dir()
    assert not source.exists()


def test_windows_durable_replace_fails_honestly_after_retry_bound(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "staging"
    destination = tmp_path / "revision"
    source.mkdir()
    retry_count = len(publication._WIN32_MOVE_RETRY_DELAYS_SECONDS)
    calls, sleeps = _install_win32_move_barrier(
        monkeypatch,
        [publication._WIN32_ERROR_ACCESS_DENIED] * (retry_count + 1),
    )
    retry_checks = []

    with pytest.raises(_FakeWin32Error) as captured:
        ImmutableBundleStore._durable_replace_windows(
            source,
            destination,
            retry_check=lambda: retry_checks.append("checked"),
        )

    assert captured.value.winerror == publication._WIN32_ERROR_ACCESS_DENIED
    assert calls == [(source, destination)] * (retry_count + 1)
    assert sleeps == list(publication._WIN32_MOVE_RETRY_DELAYS_SECONDS)
    assert retry_checks == ["checked"] * (retry_count + 1)


def test_windows_durable_replace_does_not_retry_other_win32_errors(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "staging"
    destination = tmp_path / "revision"
    source.mkdir()
    calls, sleeps = _install_win32_move_barrier(monkeypatch, [87])
    retry_checks = []

    with pytest.raises(_FakeWin32Error) as captured:
        ImmutableBundleStore._durable_replace_windows(
            source,
            destination,
            retry_check=lambda: retry_checks.append("checked"),
        )

    assert captured.value.winerror == 87
    assert calls == [(source, destination)]
    assert sleeps == []
    assert retry_checks == ["checked"]


def test_windows_durable_replace_preserves_racing_destination(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "staging"
    destination = tmp_path / "revision"
    source.mkdir()
    calls = []

    def competing_move(current_source, current_destination):
        calls.append((current_source, current_destination))
        current_destination.mkdir()
        (current_destination / "competitor.txt").write_text(
            "preserve",
            encoding="utf-8",
        )
        return publication._WIN32_ERROR_ALREADY_EXISTS

    monkeypatch.setattr(
        publication,
        "_move_file_ex_write_through",
        competing_move,
    )

    with pytest.raises(ArtifactCollisionError):
        ImmutableBundleStore._durable_replace_windows(
            source,
            destination,
        )

    assert calls == [(source, destination)]
    assert (destination / "competitor.txt").read_text(encoding="utf-8") == (
        "preserve"
    )
    assert source.is_dir()


def test_native_collision_accepts_only_exact_equivalent_bytes(tmp_path):
    source = tmp_path / "staging"
    destination = tmp_path / "revision"
    source.mkdir()
    destination.mkdir()
    expected_identity = publication._directory_identity(source)

    outcome = ImmutableBundleStore._reconcile_native_move(
        source,
        destination,
        native_error_code=errno.EEXIST,
        expected_source_identity=expected_identity,
        collision_errors={errno.EEXIST},
        equivalent_destination_check=lambda: True,
        mismatch_handler=None,
    )

    assert outcome == "idempotent"


def test_native_success_without_move_is_an_integrity_error(tmp_path):
    source = tmp_path / "staging"
    destination = tmp_path / "revision"
    source.mkdir()

    with pytest.raises(ArtifactIntegrityError, match="without committing"):
        ImmutableBundleStore._reconcile_native_move(
            source,
            destination,
            native_error_code=0,
            expected_source_identity=publication._directory_identity(source),
            collision_errors={errno.EEXIST},
            equivalent_destination_check=None,
            mismatch_handler=None,
        )


@pytest.mark.parametrize("cleanup_outcome", ["raises", "refuses"])
def test_native_mismatch_preserves_integrity_error_as_primary(
    tmp_path,
    cleanup_outcome,
):
    source = tmp_path / "missing-staging"
    destination = tmp_path / "wrong-revision"
    destination.mkdir()
    cleanup_error = OSError("exact cleanup failed")

    def cleanup(_identity):
        if cleanup_outcome == "raises":
            raise cleanup_error
        return False

    with pytest.raises(ArtifactIntegrityError, match="untrusted") as captured:
        ImmutableBundleStore._reconcile_native_move(
            source,
            destination,
            native_error_code=0,
            expected_source_identity=(123, 456),
            collision_errors={errno.EEXIST},
            equivalent_destination_check=None,
            mismatch_handler=cleanup,
        )

    if cleanup_outcome == "raises":
        assert captured.value.__cause__ is cleanup_error
    else:
        assert isinstance(
            captured.value.__cause__,
            ArtifactPublicationError,
        )


def test_native_mismatch_without_destination_fails_integrity(tmp_path):
    with pytest.raises(ArtifactIntegrityError, match="untrusted"):
        ImmutableBundleStore._reconcile_native_move(
            tmp_path / "missing-staging",
            tmp_path / "missing-revision",
            native_error_code=0,
            expected_source_identity=(123, 456),
            collision_errors={errno.EEXIST},
            equivalent_destination_check=None,
            mismatch_handler=lambda _identity: True,
        )


def test_windows_durable_replace_rejects_source_identity_swap_before_move(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "staging"
    original = tmp_path / "original"
    replacement = tmp_path / "replacement"
    destination = tmp_path / "revision"
    source.mkdir()
    replacement.mkdir()
    expected_identity = publication._directory_identity(source)
    calls, sleeps = _install_win32_move_barrier(monkeypatch, [0])

    def swap_source():
        source.rename(original)
        replacement.rename(source)

    with pytest.raises(ArtifactIntegrityError, match="identity changed"):
        ImmutableBundleStore._durable_replace_windows(
            source,
            destination,
            retry_check=swap_source,
            expected_source_identity=expected_identity,
        )

    assert calls == []
    assert sleeps == []
    assert not destination.exists()


def test_windows_durable_replace_rejects_post_move_identity_change(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "staging"
    destination = tmp_path / "revision"
    source.mkdir()
    source_identity = publication._directory_identity(source)
    parent_identity = publication._directory_identity(destination.parent)
    real_entry_state = publication._path_entry_state
    calls, sleeps = _install_win32_move_barrier(monkeypatch, [0])

    def changed_destination_identity(path, **kwargs):
        state = real_entry_state(path, **kwargs)
        if path == destination and state.identity is not None:
            return publication._PathEntryState(
                exists=True,
                identity=(state.identity[0], state.identity[1] + 1),
                is_directory=True,
            )
        return state

    monkeypatch.setattr(
        publication,
        "_path_entry_state",
        changed_destination_identity,
    )

    with pytest.raises(ArtifactIntegrityError, match="untrusted"):
        ImmutableBundleStore._durable_replace_windows(
            source,
            destination,
            expected_source_identity=source_identity,
            expected_destination_parent_identity=parent_identity,
        )

    assert calls == [(source, destination)]
    assert sleeps == []


def test_windows_retry_backoff_wakes_on_revocation(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "staging"
    destination = tmp_path / "revision"
    source.mkdir()
    cancelled = threading.Event()
    calls = []

    def held_move(current_source, current_destination):
        calls.append((current_source, current_destination))
        cancelled.set()
        return publication._WIN32_ERROR_SHARING_VIOLATION

    monkeypatch.setattr(
        publication,
        "_move_file_ex_write_through",
        held_move,
    )

    with pytest.raises(publication.ArtifactPublicationRevokedError):
        ImmutableBundleStore._durable_replace_windows(
            source,
            destination,
            cancellation_event=cancelled,
        )

    assert calls == [(source, destination)]


def test_windows_durable_replace_revalidates_destination_before_retry(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "staging"
    destination = tmp_path / "revision"
    source.mkdir()
    calls, sleeps = _install_win32_move_barrier(
        monkeypatch,
        [
            publication._WIN32_ERROR_SHARING_VIOLATION,
            publication._WIN32_ERROR_ALREADY_EXISTS,
        ],
    )

    def destination_appears(delay):
        sleeps.append(delay)
        destination.mkdir()

    monkeypatch.setattr(
        publication,
        "_sleep_before_win32_move_retry",
        destination_appears,
    )

    with pytest.raises(ArtifactCollisionError):
        ImmutableBundleStore._durable_replace_windows(
            source,
            destination,
        )

    assert calls == [(source, destination)] * 2
    assert sleeps == [publication._WIN32_MOVE_RETRY_DELAYS_SECONDS[0]]


def test_windows_durable_replace_revalidates_source_before_retry(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "staging"
    destination = tmp_path / "revision"
    source.mkdir()
    calls, sleeps = _install_win32_move_barrier(
        monkeypatch,
        [publication._WIN32_ERROR_SHARING_VIOLATION],
    )

    def source_disappears(delay):
        sleeps.append(delay)
        source.rmdir()

    monkeypatch.setattr(
        publication,
        "_sleep_before_win32_move_retry",
        source_disappears,
    )

    with pytest.raises(ArtifactPublicationError):
        ImmutableBundleStore._durable_replace_windows(
            source,
            destination,
        )

    assert calls == [(source, destination)]
    assert sleeps == [publication._WIN32_MOVE_RETRY_DELAYS_SECONDS[0]]


def test_windows_durable_replace_propagates_retry_fence_cancellation(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "staging"
    destination = tmp_path / "revision"
    source.mkdir()
    calls, sleeps = _install_win32_move_barrier(
        monkeypatch,
        [publication._WIN32_ERROR_SHARING_VIOLATION, 0],
    )

    fence_checks = []

    def cancelled_fence():
        fence_checks.append("checked")
        if len(fence_checks) == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        ImmutableBundleStore._durable_replace_windows(
            source,
            destination,
            retry_check=cancelled_fence,
        )

    assert calls == [(source, destination)]
    assert sleeps == [publication._WIN32_MOVE_RETRY_DELAYS_SECONDS[0]]
    assert fence_checks == ["checked", "checked"]


@pytest.mark.parametrize(
    "boundary",
    [
        "before_stage",
        "after_staging_directory",
        "after_file:agent_main.py",
        "after_file:astralprims_ui.py",
        "after_file:mcp_tools.py",
        "after_file:manifest.json",
        "after_staging_fsync",
        "after_validate",
        "before_replace",
        "after_replace",
        "after_revision_fsync",
    ],
)
def test_crash_at_every_publication_boundary_replays_same_identity(
    tmp_path, boundary
):
    store = _store(tmp_path / boundary.replace(":", "-"))
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)

    def crash(current: str) -> None:
        if current == boundary:
            raise RuntimeError("simulated process crash")

    with pytest.raises(RuntimeError, match="simulated process crash"):
        _publish(
            store,
            finalized,
            draft_uuid,
            publication_id,
            revision_id,
            fault_hook=crash,
        )

    recovered = _publish(
        store, finalized, draft_uuid, publication_id, revision_id
    )
    assert recovered.bundle_sha256 == finalized.bundle_sha256
    assert recovered.bundle_relative_path.endswith(revision_id)
    assert recovered.files["mcp_tools.py"].find('MARKER = "one"') >= 0


def test_replay_after_native_commit_reflushes_destination_then_source_parent(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)

    def crash_after_replace(boundary):
        if boundary == "after_replace":
            raise RuntimeError("simulated commit crash")

    with pytest.raises(RuntimeError, match="simulated commit crash"):
        _publish(
            store,
            finalized,
            draft_uuid,
            publication_id,
            revision_id,
            fault_hook=crash_after_replace,
        )

    recorded = []
    real_fsync_directory = store._fsync_directory

    def recording_fsync(path, **kwargs):
        recorded.append(path)
        return real_fsync_directory(path, **kwargs)

    monkeypatch.setattr(store, "_fsync_directory", recording_fsync)
    recovered = _publish(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )
    revision_path = store.root / recovered.bundle_relative_path
    assert recorded == [
        revision_path,
        revision_path.parent,
        store.root / "staging" / draft_uuid / "7",
    ]


def test_fence_is_rechecked_before_replace_and_stale_claim_never_publishes(
    tmp_path,
):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    boundaries: list[str] = []

    def fence(boundary: str) -> None:
        boundaries.append(boundary)
        if boundary == "before_replace":
            raise RuntimeError("generation claim is stale")

    with pytest.raises(RuntimeError, match="generation claim is stale"):
        _publish(
            store,
            finalized,
            draft_uuid,
            publication_id,
            revision_id,
            fence_check=fence,
        )

    assert boundaries == ["before_stage", "before_replace"]
    assert not (
        store.root / f"revisions/{_AGENT_ID}/{revision_id}"
    ).exists()


def test_exact_destination_appearing_after_stage_is_reflushed_before_cleanup(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    staging_path = (
        store.root / "staging" / draft_uuid / "7" / publication_id
    )
    revision_path = store.root / f"revisions/{_AGENT_ID}/{revision_id}"
    recorded = []
    real_fsync_directory = store._fsync_directory

    def recording_fsync(path, **kwargs):
        recorded.append(path)
        return real_fsync_directory(path, **kwargs)

    def exact_competitor_after_stage(boundary):
        if boundary == "before_replace":
            revision_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staging_path, revision_path)
            recorded.clear()

    monkeypatch.setattr(store, "_fsync_directory", recording_fsync)
    published = _publish(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
        fault_hook=exact_competitor_after_stage,
    )

    assert published.bundle_relative_path.endswith(revision_id)
    assert recorded == [
        revision_path,
        revision_path.parent,
        staging_path.parent,
    ]
    assert not staging_path.exists()


def test_fence_is_rechecked_inside_lock_after_wait(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    waiting = threading.Event()
    release = threading.Event()
    stale = threading.Event()
    boundaries = []
    generic_lock_count = 0

    @contextmanager
    def gated_publication_lock(lock_name=".publication.lock"):
        nonlocal generic_lock_count
        if lock_name == ".publication.lock":
            generic_lock_count += 1
            if generic_lock_count == 1:
                waiting.set()
                if not release.wait(5):
                    raise RuntimeError("test lock barrier timed out")
        yield

    def fence(boundary):
        boundaries.append(boundary)
        if stale.is_set():
            raise RuntimeError("generation claim is stale")

    monkeypatch.setattr(store, "_publication_lock", gated_publication_lock)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            _publish,
            store,
            finalized,
            draft_uuid,
            publication_id,
            revision_id,
            fence_check=fence,
        )
        assert waiting.wait(5)
        stale.set()
        release.set()
        with pytest.raises(RuntimeError, match="generation claim is stale"):
            future.result(timeout=5)

    assert boundaries == ["before_stage"]
    assert not (
        store.root / f"revisions/{_AGENT_ID}/{revision_id}"
    ).exists()


def test_stale_staging_cleanup_refuses_untrusted_path(tmp_path, monkeypatch):
    store = _store(tmp_path / "artifacts")
    staging_path = store.root / "staging" / str(uuid.uuid4())
    store._ensure_directory(staging_path)

    @contextmanager
    def reject_pin(_path, **_kwargs):
        raise ArtifactPublicationError("untrusted")
        yield

    monkeypatch.setattr(store, "_pin_store_directory", reject_pin)

    with pytest.raises(ArtifactPublicationError, match="not a trustworthy"):
        store._remove_stale_staging(staging_path)
    assert staging_path.is_dir()


def test_stale_staging_cleanup_removes_only_the_pinned_tree(tmp_path):
    store = _store(tmp_path / "artifacts")
    staging_path = store.root / "staging" / str(uuid.uuid4())
    store._ensure_directory(staging_path)
    (staging_path / "stale.txt").write_text("stale", encoding="utf-8")

    store._remove_stale_staging(staging_path)

    assert not staging_path.exists()


def test_quarantine_refuses_missing_revision_and_occupied_staging(tmp_path):
    store = _store(tmp_path / "artifacts")
    missing_revision = store.root / "revisions" / "missing-revision"
    quarantine_path = store.root / "quarantine" / str(uuid.uuid4())
    store._ensure_directory(quarantine_path)

    assert not store._quarantine_failed_revision(
        missing_revision,
        quarantine_path,
        expected_identity=(1, 2),
    )

    revision_path = store.root / "revisions" / str(uuid.uuid4())
    store._ensure_directory(revision_path)
    revision_identity = publication._directory_identity(revision_path)
    assert not store._quarantine_failed_revision(
        revision_path,
        quarantine_path,
        expected_identity=revision_identity,
    )
    assert revision_path.is_dir()
    assert quarantine_path.is_dir()


def test_receipt_quarantine_rejects_inconsistent_or_occupied_paths(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    published = _publish(
        store,
        _bundle(revision_id),
        draft_uuid,
        publication_id,
        revision_id,
    )
    assert published.receipt is not None
    receipt = published.receipt

    with pytest.raises(TypeError, match="receipt is required"):
        store.quarantine_receipt(object())
    with pytest.raises(ArtifactPublicationError, match="paths are inconsistent"):
        store.quarantine_receipt(
            replace(
                receipt,
                paths=replace(
                    receipt.paths,
                    revision_relative_path="revisions/wrong/path",
                ),
            )
        )
    with pytest.raises(ArtifactPublicationError, match="paths are inconsistent"):
        store.quarantine_receipt(
            replace(
                receipt,
                paths=replace(
                    receipt.paths,
                    quarantine_relative_path="quarantine/wrong",
                ),
            )
        )

    occupied = store.root / receipt.paths.quarantine_relative_path
    store._ensure_directory(occupied)
    with pytest.raises(ArtifactPublicationError, match="is occupied"):
        store.quarantine_receipt(receipt)


def test_receipt_quarantine_never_moves_replaced_live_destination(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    published = _publish(
        store,
        _bundle(revision_id),
        draft_uuid,
        publication_id,
        revision_id,
    )
    assert published.receipt is not None
    revision_path = store.root / published.bundle_relative_path
    displaced = tmp_path / "displaced-published-revision"
    revision_path.rename(displaced)
    revision_path.mkdir()
    marker = revision_path / "competitor.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(ArtifactPublicationError, match="no longer matches"):
        store.quarantine_receipt(published.receipt)

    assert marker.read_text(encoding="utf-8") == "preserve"
    assert displaced.is_dir()


def test_win32_quarantine_move_failure_preserves_revision(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path / "artifacts")
    revision_path = store.root / "revisions" / str(uuid.uuid4())
    quarantine_path = store.root / "quarantine" / str(uuid.uuid4())
    store._ensure_directory(revision_path)
    expected_identity = publication._directory_identity(revision_path)

    def fail_move(*_args, **_kwargs):
        raise OSError("held")

    monkeypatch.setattr(store, "_durable_replace", fail_move)

    assert not store._quarantine_failed_revision(
        revision_path,
        quarantine_path,
        expected_identity=expected_identity,
    )
    assert revision_path.is_dir()
    assert not quarantine_path.exists()


def test_win32_source_swap_is_quarantined_instead_of_left_live(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "staging"
    destination = tmp_path / "revision"
    replacement = tmp_path / "replacement"
    displaced = tmp_path / "displaced"
    quarantine = tmp_path / "quarantine"
    source.mkdir()
    replacement.mkdir()
    expected_identity = publication._directory_identity(source)

    def swap_during_move(current_source, current_destination):
        current_source.rename(displaced)
        replacement.rename(current_source)
        os.replace(current_source, current_destination)
        return 0

    def quarantine_mismatch(observed_identity):
        assert publication._directory_identity(destination) == observed_identity
        os.replace(destination, quarantine)
        return True

    monkeypatch.setattr(
        publication,
        "_move_file_ex_write_through",
        swap_during_move,
    )

    with pytest.raises(ArtifactIntegrityError, match="untrusted"):
        ImmutableBundleStore._durable_replace_windows(
            source,
            destination,
            expected_source_identity=expected_identity,
            mismatch_handler=quarantine_mismatch,
        )

    assert not destination.exists()
    assert quarantine.is_dir()
    assert displaced.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="requires a native junction")
def test_win32_source_swap_to_junction_is_quarantined_without_traversal(
    tmp_path,
    monkeypatch,
):
    import _winapi

    store = _store(tmp_path / "artifacts")
    source = store.root / "staging" / str(uuid.uuid4())
    destination_parent = store.root / "revisions" / _AGENT_ID
    destination = destination_parent / str(uuid.uuid4())
    quarantine = store.root / "quarantine" / str(uuid.uuid4())
    displaced = tmp_path / "displaced-source"
    outside = tmp_path / "outside"
    replacement = tmp_path / "replacement-junction"
    store._ensure_directory(source)
    store._ensure_directory(destination_parent)
    outside.mkdir()
    (outside / "preserve.txt").write_text("preserve", encoding="utf-8")
    _winapi.CreateJunction(str(outside), str(replacement))
    expected_identity = publication._directory_identity(source)
    real_move = publication._move_file_ex_write_through

    def swap_during_first_move(current_source, current_destination):
        if current_source == source:
            current_source.rename(displaced)
            os.replace(replacement, current_source)
        return real_move(current_source, current_destination)

    def quarantine_mismatch(observed_identity):
        store._relocate_exact_entry(
            destination,
            quarantine,
            expected_identity=observed_identity,
        )
        return True

    monkeypatch.setattr(
        publication,
        "_move_file_ex_write_through",
        swap_during_first_move,
    )

    with pytest.raises(ArtifactIntegrityError, match="untrusted"):
        ImmutableBundleStore._durable_replace_windows(
            source,
            destination,
            expected_source_identity=expected_identity,
            mismatch_handler=quarantine_mismatch,
        )

    assert not destination.exists()
    quarantined = publication._path_entry_state(quarantine)
    assert quarantined.exists and quarantined.is_reparse
    assert (outside / "preserve.txt").read_text(encoding="utf-8") == (
        "preserve"
    )
    os.rmdir(quarantine)


def test_same_revision_is_idempotent_but_different_bytes_are_rejected(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    original = _bundle(revision_id, marker="original")
    first = _publish(store, original, draft_uuid, publication_id, revision_id)
    replay = _publish(store, original, draft_uuid, publication_id, revision_id)
    assert replay.bundle_sha256 == first.bundle_sha256

    replacement = _bundle(revision_id, marker="replacement")
    with pytest.raises(ArtifactIntegrityError, match="digest mismatch"):
        _publish(
            store,
            replacement,
            draft_uuid,
            publication_id,
            revision_id,
        )
    loaded = store.load(
        first.bundle_relative_path,
        expected_digest=first.bundle_sha256,
        expected_manifest_digest=first.manifest_sha256,
    )
    assert 'MARKER = "original"' in loaded.files["mcp_tools.py"]


def test_native_equivalent_collision_reflushes_destination_before_source_parent(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    recorded = []
    real_fsync_directory = store._fsync_directory

    def recording_fsync(path, **kwargs):
        recorded.append(path)
        return real_fsync_directory(path, **kwargs)

    def exact_competitor_wins(source, destination, **kwargs):
        shutil.copytree(source, destination)
        equivalent_check = kwargs["equivalent_destination_check"]
        assert equivalent_check is not None and equivalent_check()
        recorded.clear()
        return "idempotent"

    monkeypatch.setattr(store, "_fsync_directory", recording_fsync)
    monkeypatch.setattr(store, "_durable_replace", exact_competitor_wins)
    published = _publish(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )
    revision_path = store.root / published.bundle_relative_path
    assert recorded == [
        revision_path,
        revision_path.parent,
        store.root / "staging" / draft_uuid / "7",
    ]
    assert not (
        store.root / "staging" / draft_uuid / "7" / publication_id
    ).exists()


def test_post_move_tampering_is_quarantined_and_replay_recovers(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    revision_path = store.root / f"revisions/{_AGENT_ID}/{revision_id}"

    def poison_after_move(boundary):
        if boundary == "after_replace":
            (revision_path / "mcp_tools.py").write_text(
                'MARKER = "poisoned"\n',
                encoding="utf-8",
            )

    with pytest.raises(ArtifactIntegrityError, match="digest mismatch"):
        _publish(
            store,
            finalized,
            draft_uuid,
            publication_id,
            revision_id,
            fault_hook=poison_after_move,
        )

    assert not revision_path.exists()
    recovered = _publish(
        store,
        finalized,
        draft_uuid,
        str(uuid.uuid4()),
        revision_id,
    )
    assert recovered.bundle_sha256 == finalized.bundle_sha256
    assert 'MARKER = "one"' in recovered.files["mcp_tools.py"]


def test_post_commit_fsync_failure_quarantines_exact_object_and_replays(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    revision_path = store.root / f"revisions/{_AGENT_ID}/{revision_id}"
    real_fsync_directory = store._fsync_directory
    failed = False

    def fail_first_moved_revision(path, **kwargs):
        nonlocal failed
        if path == revision_path and not failed:
            failed = True
            raise OSError("simulated revision fsync failure")
        return real_fsync_directory(path, **kwargs)

    monkeypatch.setattr(
        store,
        "_fsync_directory",
        fail_first_moved_revision,
    )
    with pytest.raises(OSError, match="simulated revision fsync failure"):
        _publish(
            store,
            finalized,
            draft_uuid,
            publication_id,
            revision_id,
        )

    assert not revision_path.exists()
    quarantine_path = store.root / "quarantine" / publication_id
    assert quarantine_path.is_dir()

    recovered = _publish(
        store,
        finalized,
        draft_uuid,
        str(uuid.uuid4()),
        revision_id,
    )
    assert recovered.bundle_sha256 == finalized.bundle_sha256
    assert revision_path.is_dir()


def test_quarantine_retry_reflushes_already_moved_exact_object(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    published = _publish(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )
    assert published.receipt is not None
    quarantine_path = store.root / "quarantine" / publication_id
    revision_parent = store.root / "revisions" / _AGENT_ID
    real_fsync_directory = store._fsync_directory
    paths = []
    failed = False

    def fail_once(path, **kwargs):
        nonlocal failed
        paths.append(path)
        if path == quarantine_path and not failed:
            failed = True
            raise OSError("simulated quarantine fsync failure")
        return real_fsync_directory(path, **kwargs)

    monkeypatch.setattr(store, "_fsync_directory", fail_once)
    with pytest.raises(OSError, match="simulated quarantine fsync failure"):
        store.quarantine_receipt(published.receipt)

    assert not (store.root / published.bundle_relative_path).exists()
    assert quarantine_path.is_dir()
    paths.clear()
    store.quarantine_receipt(published.receipt)
    assert paths == [
        quarantine_path,
        store.root / "quarantine",
        revision_parent,
    ]


def test_staging_identity_swap_during_validation_never_publishes(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    staging_path = (
        store.root
        / "staging"
        / draft_uuid
        / "7"
        / publication_id
    )
    displaced = tmp_path / "displaced-staging"
    real_load_path = store._load_path
    swapped = False

    def load_then_swap(path, **kwargs):
        nonlocal swapped
        loaded = real_load_path(path, **kwargs)
        if path == staging_path and not swapped:
            swapped = True
            path.rename(displaced)
            path.mkdir()
        return loaded

    monkeypatch.setattr(store, "_load_path", load_then_swap)

    with pytest.raises(ArtifactIntegrityError, match="identity changed"):
        _publish(
            store,
            finalized,
            draft_uuid,
            publication_id,
            revision_id,
        )

    assert displaced.is_dir()
    assert staging_path.is_dir()
    assert not (
        store.root / f"revisions/{_AGENT_ID}/{revision_id}"
    ).exists()


def test_failed_verification_never_removes_replaced_destination(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    revision_path = store.root / f"revisions/{_AGENT_ID}/{revision_id}"
    displaced = tmp_path / "displaced-valid-revision"

    def replace_after_move(boundary):
        if boundary == "after_replace":
            revision_path.rename(displaced)
            revision_path.mkdir()
            (revision_path / "competitor.txt").write_text(
                "preserve",
                encoding="utf-8",
            )

    with pytest.raises(ArtifactIntegrityError) as captured:
        _publish(
            store,
            finalized,
            draft_uuid,
            publication_id,
            revision_id,
            fault_hook=replace_after_move,
        )

    assert (revision_path / "competitor.txt").read_text(
        encoding="utf-8"
    ) == "preserve"
    assert displaced.is_dir()
    assert isinstance(captured.value.__cause__, ArtifactIntegrityError)


def test_load_translates_untrusted_revision_directory_to_integrity_error(
    tmp_path,
    monkeypatch,
):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    published = _publish(
        store,
        finalized,
        draft_uuid,
        publication_id,
        revision_id,
    )
    revision_path = store.root / published.bundle_relative_path
    real_identity = publication._directory_identity

    def reject_revision(path):
        if path == revision_path:
            raise ArtifactPublicationError("untrusted")
        return real_identity(path)

    monkeypatch.setattr(publication, "_directory_identity", reject_revision)

    with pytest.raises(ArtifactIntegrityError, match="unavailable"):
        store.load(
            published.bundle_relative_path,
            expected_digest=published.bundle_sha256,
            expected_manifest_digest=published.manifest_sha256,
        )


def test_concurrent_same_revision_publication_has_one_stable_result(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)

    def publish_once(_index: int):
        return _publish(
            store, finalized, draft_uuid, publication_id, revision_id
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(publish_once, range(32)))

    assert {result.bundle_sha256 for result in results} == {
        finalized.bundle_sha256
    }
    revision_path = store.root / results[0].bundle_relative_path
    assert len(list(revision_path.parent.iterdir())) == 1


def test_load_rejects_traversal_extra_entries_symlinks_and_tampering(tmp_path):
    store = _store(tmp_path / "artifacts")
    draft_uuid, publication_id, revision_id = _ids()
    finalized = _bundle(revision_id)
    published = _publish(
        store, finalized, draft_uuid, publication_id, revision_id
    )

    with pytest.raises(ValueError, match="outside the revision root"):
        store.load("../escape", expected_digest=published.bundle_sha256)

    revision_path = store.root / published.bundle_relative_path
    extra = revision_path / "extra.py"
    extra.write_text("pass\n", encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError, match="contents are not exact"):
        store.load(
            published.bundle_relative_path,
            expected_digest=published.bundle_sha256,
        )
    extra.unlink()

    tools = revision_path / "mcp_tools.py"
    tools.unlink()
    tools.symlink_to(revision_path / "agent_main.py")
    with pytest.raises(ArtifactIntegrityError, match="unsafe entry"):
        store.load(
            published.bundle_relative_path,
            expected_digest=published.bundle_sha256,
        )
    tools.unlink()
    tools.write_text('MARKER = "tampered"\n', encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError, match="bundle digest mismatch"):
        store.load(
            published.bundle_relative_path,
            expected_digest=published.bundle_sha256,
        )
