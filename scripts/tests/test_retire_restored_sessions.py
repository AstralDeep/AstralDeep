"""Tests for scripts/retire_restored_sessions.py: offline CLI boundaries against a fake
public Plane API, pending-receipt durability, and secret-free failure output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import retire_restored_sessions as command


@pytest.fixture
def setup(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    manifest = {
        "components": {"astral-plane": {"commit": "a" * 40}},
        "compatibility": {
            "data_plane": {
                "schema_revision": "088.002",
                "migration_sha256": "b" * 64,
            }
        },
    }
    path = tmp_path / "config/astral-composition.json"
    path.write_text(json.dumps(manifest))
    lock = tmp_path / "wheels.json"
    lock.write_text("{}\n")
    record = tmp_path / "maintenance-record"
    record.write_bytes(
        b"Independently retained operator record; not an automatic attestation."
    )
    url = tmp_path / "private-dsn"
    url.write_text(
        "postgresql://synthetic:PRIVATE_SENTINEL@recovery.invalid/restored\n"
    )
    url.chmod(0o600)
    args = argparse.Namespace(
        root=str(tmp_path),
        wheel_lock=str(lock),
        database="restored",
        schema="public",
        recovery_record=str(record),
        recovery_record_sha256=hashlib.sha256(record.read_bytes()).hexdigest(),
        database_url_file=str(url),
        output=str(tmp_path / "receipt.json"),
        execute=True,
    )
    events = []

    def load(root, **options):
        assert root == tmp_path
        assert options == {"require_sources": True, "require_gitlinks": True}
        events.append("composition")
        return SimpleNamespace(manifest_path=path)

    def verify(contract, actual_lock):
        assert contract.manifest_path == path and actual_lock == lock
        events.append("installed")

    @dataclass(frozen=True)
    class Result:
        retired_sessions: int

    calls = []

    def retire(**kwargs):
        calls.append(kwargs)
        events.append("database")
        pending = json.loads(Path(args.output).read_text())
        assert pending["status"] == "pending" and "retired_sessions" not in pending
        assert pending["operational_preconditions_verified_by_tool"] is False
        assert events[:2] == ["composition", "installed"]
        return Result(4)

    monkeypatch.setattr(command.components, "load_contract", load)
    monkeypatch.setattr(command.components, "verify_install", verify)
    api = SimpleNamespace(
        RestoredSessionRetirement=Result, retire_restored_sessions=retire
    )
    monkeypatch.setitem(sys.modules, "astralplane", api)
    return SimpleNamespace(
        args=args,
        calls=calls,
        events=events,
        api=api,
        manifest=manifest,
        path=path,
        lock=lock,
        record=record,
        url=url,
    )


def test_exact_inputs_private_pending_then_postcommit_receipt(setup):
    result = command.retire(setup.args)
    assert result["status"] == "commit_confirmed" and result["retired_sessions"] == 4
    assert (
        result["release_authority"] is False
        and result["admission_reopened_by_tool"] is False
    )
    assert setup.calls == [
        {
            "database_url": setup.url.read_text().strip(),
            "expected_database": "restored",
            "expected_schema": "public",
            "expected_schema_revision": "088.002",
            "expected_migration_digest": "b" * 64,
        }
    ]
    output = Path(setup.args.output)
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text()) == result
    assert "PRIVATE_SENTINEL" not in output.read_text()
    with pytest.raises(FileExistsError):
        command.retire(setup.args)
    assert len(setup.calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("execute", False),
        ("database", ""),
        ("schema", "\x00"),
        ("schema", "x" * 64),
        ("recovery_record_sha256", "BAD"),
        ("recovery_record_sha256", "c" * 64),
    ],
)
def test_bad_explicit_selection_refuses_before_database(setup, field, value):
    setattr(setup.args, field, value)
    with pytest.raises(command.RetirementUnavailable):
        command.retire(setup.args)
    assert setup.calls == [] and not Path(setup.args.output).exists()


@pytest.mark.parametrize(
    "change",
    [
        "public-dsn",
        "link-dsn",
        "link-record",
        "link-output",
        "empty-record",
        "oversize-record",
        "multiline-dsn",
        "directory-dsn",
    ],
)
def test_unsafe_local_inputs_never_mutate_database(setup, change, tmp_path):
    if change == "public-dsn":
        setup.url.chmod(0o644)
    elif change == "link-dsn":
        alias = tmp_path / "alias"
        alias.symlink_to(setup.url)
        setup.args.database_url_file = str(alias)
    elif change == "link-record":
        alias = tmp_path / "alias"
        alias.symlink_to(setup.record)
        setup.args.recovery_record = str(alias)
    elif change == "link-output":
        Path(setup.args.output).symlink_to(setup.record)
    elif change == "empty-record":
        setup.record.write_bytes(b"")
        setup.args.recovery_record_sha256 = hashlib.sha256(b"").hexdigest()
    elif change == "oversize-record":
        setup.record.write_bytes(b"a" * 1048577)
    elif change == "multiline-dsn":
        setup.url.write_text("first\nsecond")
    else:
        setup.args.database_url_file = str(tmp_path)
    with pytest.raises((command.RetirementUnavailable, OSError)):
        command.retire(setup.args)
    assert setup.calls == []


@pytest.mark.parametrize(
    "change", ["manifest", "lock", "different-manifest", "install-failure"]
)
def test_component_verification_cannot_drift_or_fail_open(setup, monkeypatch, change):
    def verify(*args):
        if change == "manifest":
            setup.path.write_text("{}")
        elif change == "lock":
            setup.lock.write_text('{"drift": true}')
        else:
            raise RuntimeError("untrusted verification failure PRIVATE_SENTINEL")

    if change == "different-manifest":
        monkeypatch.setattr(
            command.components,
            "load_contract",
            lambda *a, **k: SimpleNamespace(manifest_path=setup.lock),
        )
    else:
        monkeypatch.setattr(command.components, "verify_install", verify)
    with pytest.raises((command.RetirementUnavailable, RuntimeError)):
        command.retire(setup.args)
    assert setup.calls == [] and not Path(setup.args.output).exists()


@pytest.mark.parametrize(
    "failure",
    ["unknown-commit", "interrupt", "wrong-result", "bool-count", "negative-count"],
)
def test_unconfirmed_outcomes_retain_pending_receipt_without_retry(setup, failure):
    attempts = []

    def uncertain(**kwargs):
        attempts.append(True)
        if failure == "interrupt":
            raise KeyboardInterrupt
        if failure == "unknown-commit":
            raise RuntimeError("PRIVATE_SENTINEL unknown COMMIT response")
        if failure == "wrong-result":
            return object()
        return setup.api.RestoredSessionRetirement(
            True if failure == "bool-count" else -1
        )

    setup.api.retire_restored_sessions = uncertain
    with pytest.raises(
        (command.RetirementUnavailable, RuntimeError, KeyboardInterrupt)
    ):
        command.retire(setup.args)
    assert attempts == [True]
    assert json.loads(Path(setup.args.output).read_text())["status"] == "pending"


def _argv(args):
    result = []
    for name, value in vars(args).items():
        if name == "execute":
            if value:
                result.append("--execute")
        else:
            result.extend(("--" + name.replace("_", "-"), str(value)))
    return result


def test_main_sanitizes_failure_and_interrupt_without_secret_output(setup, capsys):
    def fail(**kwargs):
        raise RuntimeError("postgresql://PRIVATE_SENTINEL")

    setup.api.retire_restored_sessions = fail
    assert command.main(_argv(setup.args)) == 1
    assert "PRIVATE_SENTINEL" not in str(capsys.readouterr())
    setup.args.output += ".second"

    def interrupt(**kwargs):
        raise KeyboardInterrupt

    setup.api.retire_restored_sessions = interrupt
    assert command.main(_argv(setup.args)) == 130
    assert "PRIVATE_SENTINEL" not in str(capsys.readouterr())


def test_main_success_reports_only_committed_count(setup, capsys):
    assert command.main(_argv(setup.args)) == 0
    result = capsys.readouterr()
    assert "4 sessions" in result.out and "PRIVATE_SENTINEL" not in str(result)


@pytest.mark.parametrize(
    "data", [b'{"duplicate":1,"duplicate":2}', b'{"x":NaN}', b"[]"]
)
def test_closed_json_rejects_duplicate_or_unsupported_values(data):
    with pytest.raises(command.RetirementUnavailable):
        command._strict_json(data)


def test_pending_receipt_must_be_durable_before_database(setup, monkeypatch):
    def failed_fsync(_):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(os, "fsync", failed_fsync)
    with pytest.raises(OSError):
        command.retire(setup.args)
    assert setup.calls == []


def test_confirmed_database_with_failed_receipt_flush_is_not_cli_success(
    setup, monkeypatch, capsys
):
    original = os.fsync
    calls = []

    def flush(descriptor):
        calls.append(descriptor)
        if len(calls) == 3:
            raise OSError("synthetic postcommit receipt failure")
        return original(descriptor)

    monkeypatch.setattr(os, "fsync", flush)
    assert command.main(_argv(setup.args)) == 1
    assert len(setup.calls) == 1
    assert "commit confirmed" not in capsys.readouterr().out


def test_explicit_repeat_never_skips_on_prior_receipt(setup):
    assert command.retire(setup.args)["retired_sessions"] == 4
    setup.args.output += ".repeat"
    calls = []

    def repeated(**kwargs):
        calls.append(kwargs)
        return setup.api.RestoredSessionRetirement(0)

    setup.api.retire_restored_sessions = repeated
    receipt = command.retire(setup.args)
    assert receipt["retired_sessions"] == 0 and len(calls) == 1
    assert receipt["status"] == "commit_confirmed"
