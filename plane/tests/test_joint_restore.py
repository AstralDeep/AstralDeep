"""Unit tests for astralplane.joint_restore: exact blob manifest verification, opaque
restore evidence, database-fence binding, and retirement failure propagation.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astralplane import (
    BLOB_LAYOUT_VERSION,
    JOINT_RESTORE_CONTRACT_VERSION,
    MIGRATION_DIGEST,
    SCHEMA_REVISION,
    EndpointFence,
    JointRestoreError,
    RestoreBlobEntry,
    SessionRetirementError,
)
from astralplane import joint_restore as module


class _BadPath:
    def __fspath__(self):
        raise TypeError("not a path")


def _write(root, key: str, content: bytes) -> RestoreBlobEntry:
    path = root.joinpath(*key.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return RestoreBlobEntry(
        root="attachments",
        storage_key=key,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _stage_marker(root, parent: str = "owner/nested") -> RestoreBlobEntry:
    key = f"{parent}/.astralplane-stage-{'a' * 64}.lock"
    path = root.joinpath(*key.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return RestoreBlobEntry(
        root="attachments",
        storage_key=key,
        size_bytes=0,
        sha256=hashlib.sha256(b"").hexdigest(),
    )


def _request(root, entries):
    return {
        "database_url": "PRIVATE-DSN",
        "blob_roots": {"attachments": root},
        "blob_inventory": tuple(entries),
        "expected_database": "plane",
        "expected_schema": "public",
        "expected_schema_revision": SCHEMA_REVISION,
        "expected_migration_digest": MIGRATION_DIGEST,
    }


def _fence(root, inventory_digest: str, **changes: object) -> EndpointFence:
    store = module.ExplicitRootStreamingBlobStore(root, create_root=False)
    try:
        blob_identity = module._digest([("attachments", store.root_identity_digest)])
    finally:
        store.close()
    values: dict[str, object] = {
        "topology": "astralplane.external-primary-paired-blobs/v1",
        "database_identity": "a" * 64,
        "schema_identity": "b" * 64,
        "blob_identity": blob_identity,
        "blob_inventory_digest": inventory_digest,
        "blob_roots": ("attachments",),
        "schema_revision": SCHEMA_REVISION,
        "migration_digest": MIGRATION_DIGEST,
        "blob_layout_version": BLOB_LAYOUT_VERSION,
    }
    values.update(changes)
    return EndpointFence(**values)  # type: ignore[arg-type]


def _writable(root, inventory_digest: str) -> SimpleNamespace:
    return SimpleNamespace(
        writable=True,
        fence=_fence(root, inventory_digest),
        reasons=(),
    )


def _install_qualified(monkeypatch, root) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def qualified(**arguments):
        calls.append(arguments)
        return _writable(root, arguments["blob_inventory_digest"])

    monkeypatch.setattr(module, "inspect_endpoint_readiness", qualified)
    return calls


@pytest.mark.parametrize(
    "changes",
    [
        {"root": ""},
        {"root": "bad name"},
        {"storage_key": "../escape"},
        {"storage_key": ".astralplane-owner-locks/a.lock"},
        {"size_bytes": -1},
        {"size_bytes": True},
        {"sha256": "A" * 64},
    ],
)
def test_restore_blob_entry_rejects_unsafe_or_incomplete_metadata(changes) -> None:
    values = {
        "root": "attachments",
        "storage_key": "owner/attachment/file.bin",
        "size_bytes": 1,
        "sha256": "a" * 64,
    }
    values.update(changes)
    with pytest.raises(ValueError):
        RestoreBlobEntry(**values)


def test_restore_blob_entry_accepts_only_empty_nested_stage_marker() -> None:
    for owner in ("owner", "__verif__owner"):
        entry = RestoreBlobEntry(
            root="attachments",
            storage_key=f"{owner}/.astralplane-stage-{'a' * 64}.lock",
            size_bytes=0,
            sha256=hashlib.sha256(b"").hexdigest(),
        )
        assert entry.size_bytes == 0
    with pytest.raises(ValueError):
        RestoreBlobEntry(
            root="attachments",
            storage_key=f".astralplane-stage-{'a' * 64}.lock",
            size_bytes=0,
            sha256=hashlib.sha256(b"").hexdigest(),
        )
    with pytest.raises(ValueError):
        RestoreBlobEntry(
            root="attachments",
            storage_key=None,
            size_bytes=0,
            sha256=hashlib.sha256(b"").hexdigest(),
        )
    with pytest.raises(ValueError):
        RestoreBlobEntry(
            root="attachments",
            storage_key=f"owner/.astralplane-stage-{'a' * 64}.lock",
            size_bytes=1,
            sha256=hashlib.sha256(b"x").hexdigest(),
        )
    for storage_key in (
        f"owner/.astralplane-stage-{'A' * 64}.lock",
        f"owner with spaces/.astralplane-stage-{'a' * 64}.lock",
        f".astralplane-other/.astralplane-stage-{'a' * 64}.lock",
        f"owner//.astralplane-stage-{'a' * 64}.lock",
        f"owner/.astralplane-stage-{'a' * 63}.lock",
        f"owner/.astralplane-stage-{'a' * 64}.lock/child",
    ):
        with pytest.raises(ValueError):
            RestoreBlobEntry(
                root="attachments",
                storage_key=storage_key,
                size_bytes=0,
                sha256=hashlib.sha256(b"").hexdigest(),
            )
    with pytest.raises(ValueError):
        RestoreBlobEntry(
            root="attachments",
            storage_key=f"owner/.astralplane-stage-{'a' * 64}.lock",
            size_bytes=0,
            sha256="b" * 64,
        )
    overdeep = "/".join(["o"] * 32 + [f".astralplane-stage-{'a' * 64}.lock"])
    with pytest.raises(ValueError):
        RestoreBlobEntry(
            root="attachments",
            storage_key=overdeep,
            size_bytes=0,
            sha256=hashlib.sha256(b"").hexdigest(),
        )
    overlong = "/".join(
        ["owner", *(["o" * 255] * 17), f".astralplane-stage-{'a' * 64}.lock"]
    )
    with pytest.raises(ValueError):
        RestoreBlobEntry(
            root="attachments",
            storage_key=overlong,
            size_bytes=0,
            sha256=hashlib.sha256(b"").hexdigest(),
        )


def test_restore_inventory_accepts_exact_persistent_stage_marker(tmp_path) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    blob = _write(root, "owner/nested/file.bin", b"content")
    marker = _stage_marker(root)
    assert module._blob_evidence({"attachments": root}, (blob, marker))[1:] == (2, 7)
    with pytest.raises(ValueError):
        module.validate_blob_storage_key(marker.storage_key)


def test_public_qualification_accepts_product_created_stage_marker(monkeypatch, tmp_path) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    content = b"published by blob staging"
    blob_key = "owner/recovery/file.bin"
    storage = module.ExplicitRootStreamingBlobStore(root, create_root=False)
    from astralplane.blob_store import _create_blob_publish_authority

    authority = _create_blob_publish_authority(
        owner_id="owner",
        storage_key="recovery/file.bin",
        max_bytes=len(content),
        lease_id="restore-marker-test",
    )
    reservation = storage.reserve_materialization_staging(owner_id="owner")
    staged = storage._begin_staged_materialization(
        authority=authority,
        reservation=reservation,
    ).write_chunks((content,))
    storage._publish_staged_materialization(staged, authority=authority)
    storage.close()
    blob = RestoreBlobEntry(
        root="attachments",
        storage_key=blob_key,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    staging_digest = hashlib.sha256(b"owner\0recovery/file.bin").hexdigest()
    marker_key = (
        "owner/recovery/.astralplane-stage-"
        f"{staging_digest}.lock"
    )
    marker = RestoreBlobEntry(
        root="attachments",
        storage_key=marker_key,
        size_bytes=0,
        sha256=hashlib.sha256(b"").hexdigest(),
    )
    _install_qualified(monkeypatch, root)
    evidence = module.qualify_joint_restore(**_request(root, (blob, marker)))
    assert evidence.blob_count == 2
    assert evidence.blob_size_bytes == len(content)


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "extra",
        "changed",
        "nonzero",
        "symlink",
        "hardlink",
        "temporary",
        "malformed",
    ],
)
def test_restore_inventory_rejects_damaged_or_malformed_stage_markers(
    tmp_path, damage: str
) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    blob = _write(root, "owner/file.bin", b"content")
    marker = _stage_marker(root)
    marker_path = root.joinpath(*marker.storage_key.split("/"))
    inventory = (blob, marker)
    if damage == "missing":
        marker_path.unlink()
    elif damage == "extra":
        inventory = (blob,)
    elif damage == "changed":
        marker_path.write_bytes(b"changed")
    elif damage == "nonzero":
        with pytest.raises(ValueError):
            replace(marker, size_bytes=7, sha256=hashlib.sha256(b"changed").hexdigest())
        marker_path.write_bytes(b"changed")
    elif damage == "symlink":
        marker_path.unlink()
        target = tmp_path / "outside.lock"
        target.write_bytes(b"")
        try:
            os.symlink(target, marker_path)
        except OSError:
            pytest.skip("file symlinks are unavailable on this host")
    elif damage == "hardlink":
        target = tmp_path / "outside.lock"
        try:
            os.link(marker_path, target)
        except OSError:
            pytest.skip("hard links are unavailable on this host")
    elif damage == "temporary":
        (marker_path.parent / (marker_path.name.removesuffix(".lock") + ".tmp")).write_bytes(b"")
    else:
        malformed = marker_path.parent / ".astralplane-stage-invalid.lock"
        malformed.write_bytes(b"")
    with pytest.raises(JointRestoreError):
        module._blob_evidence({"attachments": root}, inventory)


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"database_url": "   "}, "database_url_invalid"),
        ({"expected_database": ""}, "expected_database_invalid"),
        ({"expected_database": "\ud800"}, "expected_database_invalid"),
        ({"expected_schema": "pg_catalog"}, "expected_schema_invalid"),
        ({"expected_schema": "information_schema"}, "expected_schema_invalid"),
        ({"expected_schema_revision": "088.008"}, "schema_revision_not_current"),
        ({"expected_migration_digest": "0" * 64}, "migration_digest_not_current"),
        ({"expected_blob_layout_version": "astralplane.blob-layout/v0"}, "blob_layout_not_current"),
        ({"expected_blob_inventory_digest": "not-a-digest"}, "blob_inventory_digest_invalid"),
    ],
)
def test_static_input_denial_precedes_blob_and_database_io(
    monkeypatch, tmp_path, changes: dict[str, object], reason: str
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("static denial performed blob or database I/O")

    monkeypatch.setattr(module, "_blob_evidence", forbidden)
    monkeypatch.setattr(module, "inspect_endpoint_readiness", forbidden)
    root = tmp_path / "attachments"
    root.mkdir()
    entry = _write(root, "owner/file.bin", b"synthetic")
    selected = _request(root, [entry])
    selected.update(changes)
    with pytest.raises(JointRestoreError) as error:
        module.qualify_joint_restore(**selected)  # type: ignore[arg-type]
    assert error.value.code == "joint_restore_input_incompatible"
    assert error.value.metadata == (("reason", reason),)


@pytest.mark.parametrize(
    "roots,reason",
    [
        ({}, "blob_roots_invalid"),
        ({"bad name": "unused"}, "blob_roots_invalid"),
        ({"attachments": object()}, "blob_roots_invalid"),
        ({"attachments": ""}, "blob_roots_invalid"),
        ({"attachments": b"bytes"}, "blob_roots_invalid"),
        ({"attachments": _BadPath()}, "blob_roots_invalid"),
    ],
)
def test_invalid_root_sets_are_refused_without_database_io(
    monkeypatch, tmp_path, roots, reason: str
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("invalid roots opened a database driver")

    monkeypatch.setattr(module, "inspect_endpoint_readiness", forbidden)
    selected = _request(tmp_path, ())
    selected["blob_roots"] = roots
    with pytest.raises(JointRestoreError) as error:
        module.qualify_joint_restore(**selected)
    assert error.value.code == "joint_restore_input_incompatible"
    assert error.value.metadata == (("reason", reason),)


@pytest.mark.parametrize(
    "case,reason",
    [
        ("not-iterable", "blob_inventory_invalid"),
        ("mapping", "blob_inventory_invalid"),
        ("wrong-type", "blob_inventory_invalid"),
        ("unknown-root", "blob_inventory_root_unknown"),
        ("duplicate", "blob_inventory_duplicate"),
        ("case-duplicate", "blob_inventory_duplicate"),
    ],
)
def test_invalid_inventory_forms_are_refused(
    monkeypatch, tmp_path, case: str, reason: str
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("invalid inventory opened a database driver or scanned blobs")

    monkeypatch.setattr(module, "_blob_evidence", forbidden)
    monkeypatch.setattr(module, "inspect_endpoint_readiness", forbidden)
    root = tmp_path / "attachments"
    root.mkdir()
    entry = _write(root, "owner/file.bin", b"synthetic")
    selected = _request(root, [entry])
    if case == "not-iterable":
        selected["blob_inventory"] = object()
    elif case == "mapping":
        selected["blob_inventory"] = {"entry": entry}
    elif case == "wrong-type":
        selected["blob_inventory"] = ("not-an-entry",)
    elif case == "unknown-root":
        selected["blob_inventory"] = (replace(entry, root="other"),)
    elif case == "duplicate":
        selected["blob_inventory"] = (entry, entry)
    else:
        selected["blob_inventory"] = (
            entry,
            replace(entry, storage_key="Owner/File.bin"),
        )
    with pytest.raises(JointRestoreError) as error:
        module.qualify_joint_restore(**selected)  # type: ignore[arg-type]
    assert error.value.code == "joint_restore_input_incompatible"
    assert error.value.metadata == (("reason", reason),)


def test_expected_inventory_digest_is_checked_before_scanning(monkeypatch, tmp_path) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("mismatched inventory digest scanned blobs")

    monkeypatch.setattr(module, "_blob_evidence", forbidden)
    root = tmp_path / "attachments"
    root.mkdir()
    entry = _write(root, "owner/file.bin", b"synthetic")
    selected = _request(root, [entry])
    selected["expected_blob_inventory_digest"] = "0" * 64
    with pytest.raises(JointRestoreError) as error:
        module.qualify_joint_restore(**selected)
    assert error.value.code == "joint_restore_inventory_mismatch"
    assert error.value.metadata == (("reason", "blob_inventory_digest_mismatch"),)


@pytest.mark.parametrize(
    "damage,reason",
    [
        ("missing", "blob_membership_mismatch"),
        ("extra", "blob_membership_mismatch"),
        ("truncated", "blob_size_mismatch"),
        ("digest", "blob_digest_mismatch"),
    ],
)
def test_incomplete_or_changed_blob_inventory_is_refused(
    monkeypatch, tmp_path, damage: str, reason: str
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("damaged blobs opened a database driver")

    monkeypatch.setattr(module, "inspect_endpoint_readiness", forbidden)
    root = tmp_path / "attachments"
    root.mkdir()
    first = _write(root, "owner/first.bin", b"first complete bytes")
    second = _write(root, "owner/second.bin", b"second complete bytes")
    selected = _request(root, [first, second])
    if damage == "missing":
        root.joinpath("owner", "first.bin").unlink()
    elif damage == "extra":
        root.joinpath("owner", "extra.bin").write_bytes(b"extra")
    elif damage == "truncated":
        root.joinpath("owner", "first.bin").write_bytes(b"first")
    else:
        root.joinpath("owner", "first.bin").write_bytes(b"first complete bytex")
    with pytest.raises(JointRestoreError) as error:
        module.qualify_joint_restore(**selected)
    assert error.value.code == "joint_restore_inventory_mismatch"
    assert error.value.metadata == (("reason", reason),)


def test_blob_link_and_internal_control_objects_are_checked(tmp_path) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    entry = _write(root, "owner/file.bin", b"synthetic")
    control = root / ".astralplane-owner-locks"
    control.mkdir()
    (control / ("a" * 64 + ".lock")).write_bytes(b"\0")
    assert module._blob_evidence({"attachments": root}, (entry,))[1:] == (1, 9)
    (control / "unexpected.txt").write_text("unsafe", encoding="utf-8")
    with pytest.raises(JointRestoreError) as error:
        module.qualify_joint_restore(**_request(root, [entry]))
    assert error.value.metadata == (("reason", "blob_control_unsafe"),)


def test_nested_link_is_refused_without_following_it(tmp_path) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    entry = _write(root, "owner/file.bin", b"synthetic")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    try:
        os.symlink(outside, root / "owner" / "linked.bin")
    except OSError:
        pytest.skip("file symlinks are unavailable on this host")
    with pytest.raises(JointRestoreError) as error:
        module.qualify_joint_restore(**_request(root, [entry]))
    assert error.value.metadata == (("reason", "blob_link_unsafe"),)


def test_non_regular_blob_object_is_refused(tmp_path) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    entry = _write(root, "owner/file.bin", b"synthetic")
    try:
        os.mkfifo(root / "owner" / "pipe.bin")
    except (AttributeError, OSError):
        pytest.skip("named pipes are unavailable on this host")
    with pytest.raises(JointRestoreError) as error:
        module.qualify_joint_restore(**_request(root, [entry]))
    assert error.value.metadata == (("reason", "blob_object_unsafe"),)


def test_blob_open_failure_is_data_free(monkeypatch, tmp_path) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    entry = _write(root, "owner/file.bin", b"synthetic")

    def unavailable(*args, **kwargs):
        raise OSError("PRIVATE path")

    monkeypatch.setattr(module.os, "open", unavailable)
    with pytest.raises(JointRestoreError) as error:
        module.qualify_joint_restore(**_request(root, [entry]))
    assert error.value.metadata == (("reason", "blob_unavailable"),)
    assert "PRIVATE" not in repr(error.value)


def test_non_writable_database_is_refused_before_retirement(monkeypatch, tmp_path) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    entry = _write(root, "owner/file.bin", b"synthetic")
    monkeypatch.setattr(
        module,
        "inspect_endpoint_readiness",
        lambda **arguments: SimpleNamespace(
            writable=False,
            fence=None,
            reasons=("schema_metadata_mismatch",),
        ),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("unqualified restore retired sessions")

    monkeypatch.setattr(module, "retire_restored_sessions", forbidden)
    with pytest.raises(JointRestoreError) as error:
        module.recover_joint_restore(**_request(root, [entry]))
    assert error.value.code == "joint_restore_database_unqualified"
    assert error.value.metadata == (("reason", "schema_metadata_mismatch"),)


def test_database_diagnostics_never_escape(monkeypatch, tmp_path) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    entry = _write(root, "owner/file.bin", b"synthetic")

    def unavailable(**arguments):
        raise RuntimeError("PRIVATE-DSN PASSWORD")

    monkeypatch.setattr(module, "inspect_endpoint_readiness", unavailable)
    with pytest.raises(JointRestoreError) as error:
        module.qualify_joint_restore(**_request(root, [entry]))
    assert error.value.code == "joint_restore_unavailable"
    assert str(error.value) == "joint restore is not qualified"
    assert error.value.__suppress_context__
    assert "PRIVATE" not in repr(error.value)


def test_changed_root_identity_or_fence_inventory_is_refused(monkeypatch, tmp_path) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    entry = _write(root, "owner/file.bin", b"synthetic")
    monkeypatch.setattr(
        module,
        "inspect_endpoint_readiness",
        lambda **arguments: SimpleNamespace(
            writable=True,
            fence=_fence(
                root,
                arguments["blob_inventory_digest"],
                blob_identity="f" * 64,
            ),
            reasons=(),
        ),
    )
    with pytest.raises(JointRestoreError) as error:
        module.qualify_joint_restore(**_request(root, [entry]))
    assert error.value.metadata == (("reason", "blob_fence_mismatch"),)


def test_qualified_evidence_is_deterministic_and_path_free(monkeypatch, tmp_path) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    entry = _write(root, "owner/file.bin", b"synthetic bytes")
    calls = _install_qualified(monkeypatch, root)
    selected = _request(root, [entry])
    first = module.qualify_joint_restore(**selected)
    second = module.qualify_joint_restore(**selected)
    assert first == second
    assert first.contract_version == JOINT_RESTORE_CONTRACT_VERSION
    assert first.schema_revision == SCHEMA_REVISION
    assert first.migration_digest == MIGRATION_DIGEST
    assert first.blob_count == 1 and first.blob_size_bytes == len(b"synthetic bytes")
    assert first.blob_roots == ("attachments",)
    assert first.to_dict()["state"] == "qualified"
    assert first.digest == first.to_dict()["digest"]
    assert str(root) not in repr(first.to_dict())
    assert "PRIVATE" not in repr(first.to_dict())
    assert calls and calls[0]["blob_inventory_digest"] == first.blob_inventory_digest


def test_recovery_returns_evidence_and_repeat_is_explicit(monkeypatch, tmp_path) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    entry = _write(root, "owner/file.bin", b"synthetic bytes")
    _install_qualified(monkeypatch, root)
    retired: list[dict[str, object]] = []

    def retire(**arguments):
        retired.append(arguments)
        return SimpleNamespace(retired_sessions=2 if len(retired) == 1 else 0)

    monkeypatch.setattr(module, "retire_restored_sessions", retire)
    selected = _request(root, [entry])
    first = module.recover_joint_restore(**selected)
    repeat = module.recover_joint_restore(**selected)
    assert first.retired_sessions == 2 and repeat.retired_sessions == 0
    assert first.evidence == repeat.evidence
    assert first.to_dict()["state"] == "recovered"
    assert first.to_dict()["retired_sessions"] == 2
    assert len(retired) == 2
    assert str(root) not in repr(first.to_dict())


def test_retirement_failure_propagates_without_completion_evidence(
    monkeypatch, tmp_path
) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    entry = _write(root, "owner/file.bin", b"synthetic bytes")
    _install_qualified(monkeypatch, root)

    def failed(**arguments):
        raise SessionRetirementError("restored session retirement unavailable")

    monkeypatch.setattr(module, "retire_restored_sessions", failed)
    with pytest.raises(SessionRetirementError):
        module.recover_joint_restore(**_request(root, [entry]))

@pytest.mark.parametrize(
    "expected_roots,code,reason",
    [
        (
            ("attachments", "artifacts"),
            "joint_restore_inventory_mismatch",
            "blob_root_set_mismatch",
        ),
        ("attachments", "joint_restore_input_incompatible", "expected_blob_roots_invalid"),
        (("bad name",), "joint_restore_input_incompatible", "expected_blob_roots_invalid"),
        (
            ("attachments", "attachments"),
            "joint_restore_input_incompatible",
            "expected_blob_roots_invalid",
        ),
    ],
)
def test_expected_root_set_must_match_the_configured_roots(
    monkeypatch, tmp_path, expected_roots, code: str, reason: str
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected root set opened a database driver or scanned blobs")

    monkeypatch.setattr(module, "_blob_evidence", forbidden)
    monkeypatch.setattr(module, "inspect_endpoint_readiness", forbidden)
    root = tmp_path / "attachments"
    root.mkdir()
    entry = _write(root, "owner/file.bin", b"synthetic")
    selected = _request(root, [entry])
    selected["expected_blob_roots"] = expected_roots
    with pytest.raises(JointRestoreError) as error:
        module.qualify_joint_restore(**selected)
    assert error.value.code == code
    assert error.value.metadata == (("reason", reason),)



def test_inventory_iteration_failure_is_data_free(monkeypatch, tmp_path) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    entry = _write(root, "owner/file.bin", b"synthetic")

    def broken():
        if False:
            yield entry
        raise ValueError("PRIVATE inventory")

    selected = _request(root, ())
    selected["blob_inventory"] = broken()
    with pytest.raises(JointRestoreError) as error:
        module.qualify_joint_restore(**selected)
    assert error.value.metadata == (("reason", "blob_inventory_invalid"),)
    assert "PRIVATE" not in repr(error.value)


def test_duplicate_filesystem_root_identity_is_refused(tmp_path) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    entry = _write(root, "owner/file.bin", b"synthetic")
    second = replace(entry, root="artifacts")
    with pytest.raises(JointRestoreError) as error:
        module.qualify_joint_restore(
            **{
                **_request(root, [entry, second]),
                "blob_roots": {"artifacts": root, "attachments": root},
            }
        )
    assert error.value.metadata == (("reason", "blob_root_duplicate"),)


def test_blob_store_cleanup_failure_is_refused(monkeypatch, tmp_path) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    entry = _write(root, "owner/file.bin", b"synthetic")

    class BrokenStore:
        def __init__(self, *args, **kwargs):
            pass

        @property
        def root_identity_digest(self):
            return "a" * 64

        def close(self):
            raise RuntimeError("PRIVATE cleanup")

    monkeypatch.setattr(module, "ExplicitRootStreamingBlobStore", BrokenStore)
    with pytest.raises(JointRestoreError) as error:
        module.qualify_joint_restore(**_request(root, [entry]))
    assert error.value.metadata == (("reason", "blob_root_cleanup_failed"),)
    assert "PRIVATE" not in repr(error.value)
