"""Tests for the cross-thread confirmation bridge (feature 039 UX).

Pure-Python — does NOT require PySide6. The bridge's poller is a plain function
over a ``queue.Queue``, so tests inject a fake ``show_fn`` (the GUI-side
callback) and drive ``_drain_once`` directly from a thread, simulating the
QTimer tick. This proves the threading contract (request → reply, timeout →
fail-closed) without a Qt display.
"""

from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import astral_client.confirm as confirm  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_bridge_singleton():
    """Save/restore the module BRIDGE singleton so tests that reassign it
    (to inject a fresh bridge) don't leak into other test files."""
    saved = confirm.BRIDGE
    yield
    confirm.BRIDGE = saved


def _drain_in_thread(b):
    """Simulate the GUI-thread QTimer tick draining one pending request.

    Blocks on the request queue (as a real poller would, without the 100 ms
    QTimer cadence), then routes through the bridge's REAL ``_show_and_reply``
    (which stamps the reply-correlation id), so the test exercises the product
    reply path rather than a hand-mirrored copy of it.
    """

    def gui_tick():
        try:
            req = b._q.get(timeout=5)
        except Exception:  # noqa: BLE001 — queue.Empty after timeout
            return
        b._show_and_reply(req)

    t = threading.Thread(target=gui_tick)
    t.start()
    return t


def _make_bridge(monkeypatch, show_fn, *, attach=True):
    monkeypatch.setenv("ASTRAL_CONFIRM_TIMEOUT", "3")
    b = confirm._Bridge()
    if attach:
        b.attach(show_fn)
    return b


def test_request_confirm_allow(monkeypatch):
    b = _make_bridge(monkeypatch, lambda req: {"accepted": True, "choice": None})
    t = _drain_in_thread(b)
    reply = b.request_confirm(
        {"kind": "action", "tool": "write_file", "preview": "x=1"}
    )
    t.join(timeout=2)
    assert reply["accepted"] is True


def test_request_confirm_deny(monkeypatch):
    b = _make_bridge(monkeypatch, lambda req: {"accepted": False, "choice": None})
    t = _drain_in_thread(b)
    reply = b.request_confirm({"kind": "action", "tool": "write_file"})
    t.join(timeout=2)
    assert reply["accepted"] is False


def test_timeout_fail_closed(monkeypatch):
    """No GUI tick draining the request ⇒ timeout ⇒ declined (fail-closed)."""
    b = _make_bridge(monkeypatch, lambda req: {"accepted": True, "choice": None})
    # Attach but never drain — request_confirm blocks until the timeout.
    reply = b.request_confirm({"kind": "action", "tool": "write_file"})
    assert reply["accepted"] is False
    assert reply.get("reason") == "timeout"


def test_not_attached_fail_closed(monkeypatch):
    """If the bridge is never attached (headless, no GUI), requests decline."""
    b = _make_bridge(monkeypatch, lambda req: {"accepted": True}, attach=False)
    reply = b.request_confirm({"kind": "action", "tool": "write_file"})
    assert reply["accepted"] is False
    assert reply.get("reason") == "no_gui"


def test_dialog_error_fail_closed(monkeypatch):
    """A show_fn that raises ⇒ fail-closed decline, never an exception."""

    def boom(req):
        raise RuntimeError("qt exploded")

    b = _make_bridge(monkeypatch, boom)
    t = _drain_in_thread(b)
    reply = b.request_confirm({"kind": "action", "tool": "write_file"})
    t.join(timeout=2)
    assert reply["accepted"] is False
    assert reply.get("reason") == "dialog_error"


def test_stale_reply_never_approves_the_next_request(monkeypatch):
    """A dialog answered AFTER its requester timed out must not leak its Allow
    to the next request (pre-fix, the uncorrelated shared reply queue delivered
    exactly that: a stale approved=True consumed by a different action)."""
    monkeypatch.setenv("ASTRAL_CONFIRM_TIMEOUT", "1")
    b = confirm._Bridge()
    b.attach(lambda req: {"accepted": True, "choice": None})

    # First request: nobody drains it → its waiter times out (declined) while
    # the request itself stays queued.
    first = b.request_confirm({"kind": "action", "tool": "write_file"})
    assert first["accepted"] is False and first.get("reason") == "timeout"

    # The GUI now (late) answers the FIRST dialog with Allow…
    stale_req = b._q.get(timeout=2)
    b._show_and_reply(stale_req)

    # …and a SECOND, different action asks for confirmation. Its dialog is
    # denied; the stale Allow above must not be consumed as its answer.
    def deny_second(req):
        return {"accepted": False, "choice": None}

    b._show_fn = deny_second
    t = _drain_in_thread(b)
    second = b.request_confirm({"kind": "action", "tool": "delete_file"})
    t.join(timeout=3)
    assert second["accepted"] is False


def test_directory_pick_returns_choice(monkeypatch):
    b = _make_bridge(
        monkeypatch, lambda req: {"accepted": True, "choice": "C:/Users/me/Workspace"}
    )
    confirm.BRIDGE = b
    t = _drain_in_thread(b)
    path = confirm.pick_directory()
    t.join(timeout=2)
    assert path == "C:/Users/me/Workspace"


def test_directory_pick_cancelled_returns_none(monkeypatch):
    b = _make_bridge(monkeypatch, lambda req: {"accepted": False, "choice": None})
    confirm.BRIDGE = b
    t = _drain_in_thread(b)
    path = confirm.pick_directory()
    t.join(timeout=2)
    assert path is None
