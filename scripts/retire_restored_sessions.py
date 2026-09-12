"""Explicit offline recovery step; never starts services or reopens admission.

The operator must independently stop all writers, verify the joint database/blob
restore, and discard every application process/cache before reopening admission.
The recovery-record hash binds that external record; this tool does not certify
its contents. Run only with the exact qualified component wheels installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts import install_local_components as components


class RetirementUnavailable(Exception):
    """Data-free refusal; a pending receipt never means the database was unchanged."""


def _reject() -> None:
    raise RetirementUnavailable("restored session retirement unavailable")


def _regular_bytes(path: Path, *, maximum: int, private: bool = False) -> bytes:
    # Open the selected file itself without following a link. Parent directories
    # are operator-controlled; their spelling never selects a database implicitly.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        value = os.fstat(descriptor)
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_size > maximum
            or (private and (value.st_mode & 0o077 or value.st_uid != os.getuid()))
        ):
            _reject()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            _reject()
        return data
    finally:
        os.close(descriptor)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _reject()
        result[key] = value
    return result


def _strict_json(data: bytes) -> dict:
    value = json.loads(
        data, object_pairs_hook=_unique_object, parse_constant=lambda _: _reject()
    )
    if not isinstance(value, dict):
        _reject()
    return value


def _qualified_composition(root: Path, lock: Path) -> tuple[dict, str, str]:
    manifest_path = root / "config/astral-composition.json"
    manifest_bytes = _regular_bytes(manifest_path, maximum=262144)
    lock_bytes = _regular_bytes(lock, maximum=1048576)
    contract = components.load_contract(
        root, require_sources=True, require_gitlinks=True
    )
    if contract.manifest_path != manifest_path:
        _reject()
    components.verify_install(contract, lock)
    if (
        _regular_bytes(manifest_path, maximum=262144) != manifest_bytes
        or _regular_bytes(lock, maximum=1048576) != lock_bytes
    ):
        _reject()
    return _strict_json(manifest_bytes), _digest(manifest_bytes), _digest(lock_bytes)


def _retire(database_url: str, database: str, schema: str, contract: dict) -> int:
    # Import only after installed-package verification. No runtime initializer,
    # SQL, connection pool, migration or product hook belongs in this host tool.
    from astralplane import RestoredSessionRetirement, retire_restored_sessions

    result = retire_restored_sessions(
        database_url=database_url,
        expected_database=database,
        expected_schema=schema,
        expected_schema_revision=contract["schema_revision"],
        expected_migration_digest=contract["migration_sha256"],
    )
    if not isinstance(result, RestoredSessionRetirement):
        _reject()
    return result.retired_sessions


def _write_receipt(descriptor: int, record: dict[str, Any]) -> None:
    data = (
        json.dumps(record, sort_keys=True, allow_nan=False, indent=2) + "\n"
    ).encode()
    os.lseek(descriptor, 0, os.SEEK_SET)
    # The pending receipt remains until a postcommit result is available. A
    # torn final write is unconfirmed, never an instruction to reopen traffic.
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            _reject()
        offset += written
    os.ftruncate(descriptor, len(data))
    os.fsync(descriptor)


def retire(args: argparse.Namespace) -> dict:
    if os.name != "posix" or args.execute is not True:
        _reject()
    if any(
        not isinstance(value, str)
        or not value
        or len(value.encode()) > 63
        or "\x00" in value
        for value in (args.database, args.schema)
    ):
        _reject()
    if re.fullmatch(r"[0-9a-f]{64}", args.recovery_record_sha256) is None:
        _reject()
    root = Path(args.root).resolve(strict=True)
    record = _regular_bytes(Path(args.recovery_record), maximum=1048576)
    if not record or _digest(record) != args.recovery_record_sha256:
        _reject()
    manifest, manifest_digest, lock_digest = _qualified_composition(
        root, Path(args.wheel_lock)
    )
    contract = manifest["compatibility"]["data_plane"]
    plane_commit = manifest["components"]["astral-plane"]["commit"]
    database_url = (
        _regular_bytes(Path(args.database_url_file), maximum=65536, private=True)
        .decode()
        .strip()
    )
    if (
        not database_url
        or "\x00" in database_url
        or "\n" in database_url
        or "\r" in database_url
    ):
        _reject()
    output = Path(args.output).absolute()
    # Never overwrite a preceding attempt, including an uncertain one.
    descriptor = os.open(
        output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    receipt = {
        "format": "astraldeep.restored-session-retirement/v1",
        "attempt_id": str(uuid4()),
        "status": "pending",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "database": args.database,
        "schema": args.schema,
        "plane_commit": plane_commit,
        "schema_revision": contract["schema_revision"],
        "migration_sha256": contract["migration_sha256"],
        "composition_sha256": manifest_digest,
        "wheel_lock_sha256": lock_digest,
        "operator_recovery_record_sha256": args.recovery_record_sha256,
        "operational_preconditions_verified_by_tool": False,
        "admission_reopened_by_tool": False,
        "release_authority": False,
    }
    try:
        _write_receipt(descriptor, receipt)
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        count = _retire(database_url, args.database, args.schema, contract)
        if type(count) is not int or count < 0:
            _reject()
        receipt.update(
            status="commit_confirmed",
            retired_sessions=count,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        _write_receipt(descriptor, receipt)
        return receipt
    finally:
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--wheel-lock", required=True)
    parser.add_argument("--database-url-file", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--recovery-record", required=True)
    parser.add_argument("--recovery-record-sha256", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Retire all sessions only after independently verified closed-writer joint recovery.",
    )
    args = parser.parse_args(argv)
    try:
        receipt = retire(args)
    except KeyboardInterrupt:
        print(
            "Retirement interrupted; keep admission closed and treat any pending receipt as unconfirmed.",
            file=sys.stderr,
        )
        return 130
    except Exception:
        print(
            "Retirement unavailable; keep admission closed. A pending receipt does not prove rollback.",
            file=sys.stderr,
        )
        return 1
    print(
        f"Retirement commit confirmed: {receipt['retired_sessions']} sessions. Admission remains an external operator responsibility."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
