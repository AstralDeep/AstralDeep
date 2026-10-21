"""Unit tests for the structured adapter's scratch, profile, result-validation, and
fail-closed worker-start/cleanup branches that production normally exercises in a child.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest

from agents.general.file_tools import structured_document as adapter
from agents.general.file_tools.structured_document import parse_structured_document


def _profile():
    return json.loads(
        (Path(__file__).resolve().parents[5] / "config" / "documents" /
         "structured-reader-profile.json").read_text()
    )


def _identity(payload, profile, worker_digest):
    return {
        "status": "complete",
        "complete": True,
        "truncated": False,
        "text": "text",
        "blocks": [],
        "tables": [],
        "pages": [],
        "warnings": [],
        "vision_required": False,
        "source": {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            "extension": "pdf",
            "kind": "pdf",
        },
        "parser": {
            "profile_id": profile["profile_id"],
            "profile_sha256": "profile",
            "adapter": profile["adapter"]["id"],
            "adapter_version": profile["adapter"]["version"],
            "worker_sha256": worker_digest,
        },
    }


def test_profile_and_worker_regular_file_boundaries(tmp_path, monkeypatch):
    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    with pytest.raises(OSError, match="size"):
        adapter._read_regular(empty, 10)
    oversized = tmp_path / "large.json"
    oversized.write_bytes(b"x" * 11)
    with pytest.raises(OSError, match="size"):
        adapter._read_regular(oversized, 10)
    with pytest.raises(OSError, match="regular"):
        adapter._read_regular(tmp_path / "missing.json", 10)

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{")
    monkeypatch.setenv("ASTRAL_STRUCTURED_DOCUMENT_PROFILE", str(invalid))
    with pytest.raises(ValueError, match="invalid"):
        adapter._profile()

    profile = _profile()
    profile["platform"]["os"] = "darwin"
    non_linux = tmp_path / "nonlinux.json"
    non_linux.write_text(json.dumps(profile))
    monkeypatch.setenv("ASTRAL_STRUCTURED_DOCUMENT_PROFILE", str(non_linux))
    with pytest.raises(ValueError, match="Linux"):
        adapter._profile()


def test_profile_path_default_and_environment_override(monkeypatch, tmp_path):
    monkeypatch.delenv("ASTRAL_STRUCTURED_DOCUMENT_PROFILE", raising=False)
    assert adapter._profile_path() == adapter._DEFAULT_PROFILE
    monkeypatch.setenv("ASTRAL_STRUCTURED_DOCUMENT_PROFILE", str(tmp_path / "p.json"))
    assert adapter._profile_path() == tmp_path / "p.json"


def test_read_bounded_rejects_symlink_and_oversized_output(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(OSError, match="symlink"):
        adapter._read_bounded(link, 100)
    with pytest.raises(OSError, match="boundary"):
        adapter._read_bounded(target, 1)
    empty = tmp_path / "empty-output.json"
    empty.write_bytes(b"")
    with pytest.raises(OSError, match="boundary"):
        adapter._read_bounded(empty, 100)
    assert adapter._read_bounded(target, 100) == b"{}"


def test_scratch_root_mode_and_privilege_drop(monkeypatch):
    calls = []
    monkeypatch.setattr(adapter.os, "geteuid", lambda: 0)
    monkeypatch.setattr(adapter.os, "setgroups", calls.append)
    monkeypatch.setattr(adapter.os, "setgid", calls.append)
    monkeypatch.setattr(adapter.os, "setuid", calls.append)
    adapter._drop_privileges()
    assert calls == [[], adapter._NON_ROOT_GID, adapter._NON_ROOT_UID]
    with adapter._scratch(b"worker", b"profile") as (runtime, work):
        assert runtime.stat().st_mode & 0o777 == 0o555
        assert work.stat().st_mode & 0o777 == 0o777


def test_preexec_builds_resource_and_privilege_callbacks(monkeypatch):
    calls = []
    monkeypatch.setattr(adapter, "make_preexec", lambda _limits: lambda: calls.append("resource"))
    monkeypatch.setattr(adapter, "_drop_privileges", lambda: calls.append("drop"))
    callback = adapter._preexec(_profile())
    callback()
    assert calls == ["resource", "drop"]
    profile = _profile()
    with monkeypatch.context() as patch:
        patch.setattr(adapter.os, "name", "nt")
        assert adapter._preexec(profile) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(status="unknown"),
        lambda value: value.update(status="failed", error={}),
        lambda value: value["source"].update(sha256="bad"),
        lambda value: value.update(text=None),
        lambda value: value.update(blocks={}),
        lambda value: value.update(complete="yes"),
        lambda value: value.update(status="complete", complete=False),
        lambda value: value.update(status="partial", complete=True),
    ],
)
def test_validate_result_rejects_invalid_status_identity_and_collections(mutate):
    payload = b"payload"
    profile = _profile()
    value = _identity(payload, profile, "worker")
    value["parser"]["profile_sha256"] = "profile"
    mutate(value)
    with pytest.raises(ValueError):
        adapter._validate_result(
            value,
            payload=payload,
            extension="pdf",
            profile=profile,
            profile_digest="profile",
            worker_digest="worker",
            max_chars=100,
        )


def test_validate_result_accepts_failed_and_complete_results():
    profile = _profile()
    failed = {"status": "failed", "error": {"code": "parse_failed"}}
    assert adapter._validate_result(
        failed, payload=b"x", extension="pdf", profile=profile,
        profile_digest="p", worker_digest="w", max_chars=100,
    ) is failed
    complete = _identity(b"x", profile, "w")
    complete["parser"]["profile_sha256"] = "p"
    assert adapter._validate_result(
        complete, payload=b"x", extension="pdf", profile=profile,
        profile_digest="p", worker_digest="w", max_chars=100,
    ) is complete


def test_parse_rejects_unsupported_extension_before_spawn():
    result = parse_structured_document(b"x", "rtf", None, 100)
    assert result["error"]["code"] == "unsupported"


class _FakeChild:
    pass


def test_parse_fails_closed_when_worker_cannot_start(monkeypatch):
    def fail_spawn(*_args, **_kwargs):
        raise RuntimeError("synthetic spawn failure")

    monkeypatch.setattr(adapter.ProcessSupervisor, "spawn", fail_spawn)
    result = parse_structured_document(b"%PDF-1.4", "pdf", None, 100)
    assert result["error"]["code"] == "structured_parser_unavailable"


def test_parse_reports_cleanup_uncertain_without_claiming_success(monkeypatch):
    monkeypatch.setattr(adapter.ProcessSupervisor, "spawn", lambda *_args, **_kwargs: _FakeChild())
    monkeypatch.setattr(
        adapter,
        "_wait_for_worker",
        lambda _child, _deadline, _cancel: (
            {"status": "timeout", "complete": False, "error": {"code": "timeout"}},
            False,
        ),
    )
    result = parse_structured_document(b"%PDF-1.4", "pdf", None, 100)
    assert result["status"] == "timeout"
    assert result["cleanup_ok"] is False
    assert result["error"]["code"] == "cleanup_uncertain"


def test_parse_rejects_malformed_worker_result_identity(monkeypatch):
    monkeypatch.setattr(adapter.ProcessSupervisor, "spawn", lambda *_args, **_kwargs: _FakeChild())
    monkeypatch.setattr(
        adapter, "_wait_for_worker", lambda _child, _deadline, _cancel: (None, True)
    )
    monkeypatch.setattr(adapter, "_read_bounded", lambda _path, _maximum: b"{}")
    result = parse_structured_document(b"%PDF-1.4", "pdf", None, 100)
    assert result["error"]["code"] == "structured_parser_failed"


def test_wait_for_worker_cancel_and_timeout_paths(monkeypatch):
    class Child:
        def __init__(self):
            self.reasons = []

        def terminate(self, *, reason):
            self.reasons.append(reason)
            return type("Snapshot", (), {
                "process_tree_terminated": True,
                "readers_joined": True,
                "pipes_closed": True,
                "cleanup_error": None,
            })()

    event = threading.Event()
    event.set()
    result, cleanup = adapter._wait_for_worker(Child(), 9999999999.0, event)
    assert result["status"] == "cancelled" and cleanup is True
    result, cleanup = adapter._wait_for_worker(Child(), 0.0, None)
    assert result["status"] == "timeout" and cleanup is True


def test_worker_env_scrubs_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    env = adapter._worker_env(tmp_path)
    assert "OPENAI_API_KEY" not in env
    assert env["TMPDIR"] == str(tmp_path)
    assert env["PATH"]

def test_parse_fails_closed_at_concurrency_limit(monkeypatch):
    slot = threading.BoundedSemaphore(1)
    slot.acquire()
    monkeypatch.setattr(adapter, "_worker_slot", lambda _limit: slot)
    try:
        result = parse_structured_document(b"%PDF-1.4", "pdf", None, 100)
    finally:
        slot.release()
    assert result["status"] == "failed"
    assert result["error"]["code"] == "structured_parser_busy"


def test_parse_rejects_invalid_max_chars_before_spawn():
    result = parse_structured_document(b"%PDF-1.4", "pdf", None, "not-a-number")
    assert result["error"]["code"] == "invalid_max_chars"
