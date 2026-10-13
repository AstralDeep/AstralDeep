"""Tests for the event-loop blocking detector (tests/plugins/event_loop_guard.py): the
enforce-raise path, allowlist parsing and short-circuit, report-mode dedup,
caller-site fallback, and install() idempotence.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from tests import loop_guard_allowlist  # noqa: E402
from tests.plugins import event_loop_guard as guard  # noqa: E402


def test_enforce_mode_raises_with_actionable_message(monkeypatch):
    monkeypatch.setenv("LOOP_GUARD_ENFORCE", "1")
    monkeypatch.setattr(guard, "allowed_sites", lambda: set())
    with pytest.raises(guard.BlockingDBOnEventLoop) as exc:
        guard._flag_blocking_call("transaction")
    assert "transaction" in str(exc.value)
    assert "loop_guard_allowlist" in str(exc.value)


def test_allowlisted_site_short_circuits_even_in_enforce_mode(monkeypatch):
    monkeypatch.setenv("LOOP_GUARD_ENFORCE", "1")
    site = (f"{__name__}:"
            "test_allowlisted_site_short_circuits_even_in_enforce_mode")
    monkeypatch.setattr(guard, "allowed_sites", lambda: {site})
    guard._flag_blocking_call("transaction")


def test_allowlist_parsing_strips_justification_suffix(monkeypatch):
    monkeypatch.setattr(loop_guard_allowlist, "ALLOWED_SITES", [
        "pkg.mod:func -- transitional, see T042",
        "  other.mod:helper  ",
    ])
    assert loop_guard_allowlist.allowed_sites() == {
        "pkg.mod:func", "other.mod:helper"}


def test_report_mode_records_each_site_once(monkeypatch):
    monkeypatch.delenv("LOOP_GUARD_ENFORCE", raising=False)
    monkeypatch.setattr(guard, "allowed_sites", lambda: set())
    monkeypatch.setattr(guard, "OFFENDERS", [])
    monkeypatch.setattr(guard, "_reported_sites", set())

    guard._flag_blocking_call("transaction")
    guard._flag_blocking_call("transaction")

    assert len(guard.OFFENDERS) == 1
    offender = guard.OFFENDERS[0]
    assert offender["method"] == "transaction"
    assert offender["site"].startswith(f"{__name__}:")


def test_report_mode_formats_only_new_method_and_site_pairs(monkeypatch, caplog):
    monkeypatch.delenv("LOOP_GUARD_ENFORCE", raising=False)
    monkeypatch.setattr(guard, "allowed_sites", lambda: set())
    monkeypatch.setattr(guard, "OFFENDERS", [])
    monkeypatch.setattr(guard, "_reported_sites", set())
    site = ["caller:first"]
    stacks = []
    monkeypatch.setattr(guard, "_caller_site", lambda: site[0])

    def capture_stack(*, limit):
        assert limit == 30
        stacks.append(f"stack-{len(stacks)}")
        return [stacks[-1]]

    monkeypatch.setattr(guard.traceback, "format_stack", capture_stack)
    guard._flag_blocking_call("transaction")
    guard._flag_blocking_call("transaction")
    guard._flag_blocking_call("other_boundary")
    site[0] = "caller:second"
    guard._flag_blocking_call("transaction")
    guard._flag_blocking_call("transaction")

    assert len(stacks) == 3
    assert guard.OFFENDERS == [
        {"method": "transaction", "site": "caller:first", "stack": "stack-0"},
        {"method": "other_boundary", "site": "caller:first", "stack": "stack-1"},
        {"method": "transaction", "site": "caller:second", "stack": "stack-2"},
    ]
    assert len([record for record in caplog.records if record.name == guard.logger.name]) == 3


def test_reported_site_still_formats_and_raises_on_every_enforced_call(monkeypatch):
    monkeypatch.delenv("LOOP_GUARD_ENFORCE", raising=False)
    monkeypatch.setattr(guard, "allowed_sites", lambda: set())
    monkeypatch.setattr(guard, "OFFENDERS", [])
    monkeypatch.setattr(guard, "_reported_sites", set())
    monkeypatch.setattr(guard, "_caller_site", lambda: "caller:shared")
    stacks = []

    def capture_stack(*, limit):
        assert limit == 30
        stacks.append(f"stack-{len(stacks)}")
        return [stacks[-1]]

    monkeypatch.setattr(guard.traceback, "format_stack", capture_stack)
    guard._flag_blocking_call("transaction")
    monkeypatch.setenv("LOOP_GUARD_ENFORCE", "1")
    for index in (1, 2):
        with pytest.raises(guard.BlockingDBOnEventLoop, match=f"stack-{index}"):
            guard._flag_blocking_call("transaction")
    monkeypatch.delenv("LOOP_GUARD_ENFORCE", raising=False)
    guard._flag_blocking_call("transaction")

    assert len(stacks) == 3
    assert guard.OFFENDERS == [
        {"method": "transaction", "site": "caller:shared", "stack": "stack-0"},
    ]


@pytest.mark.parametrize("enforce", ["0", "1"])
def test_allowlisted_site_never_formats_a_stack(monkeypatch, enforce):
    monkeypatch.setenv("LOOP_GUARD_ENFORCE", enforce)
    monkeypatch.setattr(guard, "_caller_site", lambda: "caller:allowed")
    monkeypatch.setattr(guard, "allowed_sites", lambda: {"caller:allowed"})
    monkeypatch.setattr(guard, "OFFENDERS", [])
    monkeypatch.setattr(guard, "_reported_sites", set())

    def forbidden_stack(*args, **kwargs):
        raise AssertionError("allowlisted site must not capture a stack")

    monkeypatch.setattr(guard.traceback, "format_stack", forbidden_stack)
    guard._flag_blocking_call("transaction")
    assert guard.OFFENDERS == []
    assert guard._reported_sites == set()


def test_caller_site_falls_back_when_stack_is_all_db_frames(monkeypatch):
    frame = SimpleNamespace(
        f_globals={"__name__": "astralplane.database"},
        f_code=SimpleNamespace(co_name="transaction"),
        f_back=None,
    )
    monkeypatch.setattr(sys, "_getframe", lambda depth=0: frame)
    assert guard._caller_site() == "<unknown>:<unknown>"


def test_install_is_idempotent():
    from astralplane import PlaneRuntime

    guard.install()
    originals = dict(guard._originals)
    guard.install()
    assert guard._originals == originals
    for name in guard.GUARDED_METHODS:
        assert getattr(getattr(PlaneRuntime, name), "_loop_guard_wrapped", False)
