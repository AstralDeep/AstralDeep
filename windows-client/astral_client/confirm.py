"""Cross-thread confirmation bridge for the Windows coding agent.

The coding tools run on the win_agent's daemon thread (its own asyncio loop),
but a native Qt dialog must be shown on the GUI main thread. This module is the
thread-safe bridge between them: a tool calls ``request_confirm`` (blocking,
from the agent thread); the GUI thread's ``QTimer`` poller picks up the request,
shows the right native dialog, and posts the reply on a thread-safe queue.

Two request kinds:

* ``"action"`` — a mutating/exec action needs an explicit Allow / Deny before
  it touches disk or runs a command. The dialog shows the tool name, the
  workspace-relative target, and a scrollable preview (file content / command).
* ``"directory"`` — ask the user to pick a workspace folder
  (``QFileDialog.getExistingDirectory``); returns the chosen path or ``None``.

Fail-closed: a timeout (``ASTRAL_CONFIRM_TIMEOUT``, default 300 s) or any bridge
error is treated as **declined** — no action is ever taken without an explicit
Allow. Mutating tools therefore never silently proceed when the GUI is absent
(headless test runs stub the bridge with an auto-reply, see the tests).

Pure-Python unit-testable: the poller is a plain function over a ``queue.Queue``,
so tests inject a fake "show dialog" callback and drive the poller without a
real Qt display. Qt is imported lazily inside the GUI-side callback so importing
this module never requires PySide6.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("astral.confirm")

_DEFAULT_TIMEOUT = 300  # seconds; overridable via ASTRAL_CONFIRM_TIMEOUT


def _timeout() -> float:
    try:
        return max(
            5.0, float(os.getenv("ASTRAL_CONFIRM_TIMEOUT", str(_DEFAULT_TIMEOUT)))
        )
    except ValueError:
        return float(_DEFAULT_TIMEOUT)


class _Bridge:
    """Singleton bridge. The GUI thread attaches once at startup; the agent
    thread calls ``request_confirm`` per action."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._reply: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._attached = False
        self._poller = None  # QTimer on the GUI thread, if Qt is attached
        # One confirmation in flight at a time. Two concurrent requesters
        # (the orchestrator CAN dispatch parallel tool calls at one agent)
        # would otherwise race on the shared reply queue — Queue.get() makes
        # no ordering promise across waiters, so requester A could consume
        # the Allow the user granted to requester B's dialog.
        self._serial = threading.Lock()

    def attach(self, show_fn: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        """Called once on the GUI thread. ``show_fn`` displays the dialog for a
        request dict and returns the reply dict
        ``{"accepted": bool, "choice": Optional[str]}``.

        Installs a ``QTimer`` that polls ``self._q`` on the GUI event loop.
        """
        with self._lock:
            self._show_fn = show_fn
            self._attached = True

        # Lazy Qt import — only the GUI thread has a QApplication.
        try:
            from PySide6.QtCore import QTimer
        except Exception:  # noqa: BLE001 — Qt optional in test/headless
            logger.info("confirm bridge attached without Qt (headless/test mode)")
            return

        timer = QTimer()
        timer.setInterval(100)  # ms
        timer.timeout.connect(self._drain_once)
        timer.start()
        self._poller = timer
        # Keep a reference so the timer isn't GC'd. The QApplication owns it
        # for the process lifetime; this is belt-and-braces.
        self._timer_ref = timer

    def _drain_once(self) -> None:
        """GUI-thread tick: drain one pending request (if any) and show it."""
        try:
            req = self._q.get_nowait()
        except queue.Empty:
            return
        self._show_and_reply(req)

    def _show_and_reply(self, req: Dict[str, Any]) -> None:
        """Show one request's dialog and post its correlated reply. The single
        GUI-side reply path — the tests' simulated pollers call this too, so
        the correlation contract can't drift between product and test."""
        try:
            reply = self._show_fn(req)
        except Exception as exc:  # noqa: BLE001 — never raise on the GUI thread
            logger.warning("confirm dialog failed: %s", exc)
            reply = {"accepted": False, "choice": None, "reason": "dialog_error"}
        if not isinstance(reply, dict):
            reply = {"accepted": bool(reply), "choice": None}
        # Stamp the reply with its request id so a waiter can never consume an
        # answer meant for a different (e.g. timed-out) request.
        reply.setdefault("_confirm_id", req.get("_confirm_id"))
        self._reply.put(reply)

    def request_confirm(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """Called from the agent thread. Blocks until the GUI replies or the
        timeout elapses. Returns ``{"accepted": bool, "choice": ...}``.

        If the bridge is not attached (no GUI — e.g. a standalone agent run or
        a test that didn't stub it), returns **declined** (fail-closed).

        Correlated + serialized: each request carries a unique id and the
        waiter discards any reply that doesn't match it — a dialog answered
        AFTER its requester timed out must never be delivered to the next
        request (a stale Allow approving a different action). ``_serial``
        additionally admits one confirmation at a time, so dialogs are shown
        to the user one by one in request order.
        """
        with self._lock:
            attached = self._attached
        if not attached:
            return {"accepted": False, "choice": None, "reason": "no_gui"}
        req = dict(req)
        req["_confirm_id"] = uuid.uuid4().hex
        with self._serial:
            self._q.put(req)
            deadline = time.monotonic() + _timeout()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "confirm request timed out after %ss: %s",
                        _timeout(), req.get("kind"),
                    )
                    return {"accepted": False, "choice": None, "reason": "timeout"}
                try:
                    reply = self._reply.get(timeout=remaining)
                except queue.Empty:
                    continue
                if reply.get("_confirm_id") == req["_confirm_id"]:
                    return reply
                # A stale reply from an earlier timed-out request: fail closed
                # by dropping it (its waiter already returned declined).
                logger.warning("dropping stale confirm reply (kind=%s)", req.get("kind"))


# Module-level singleton — one bridge per process.
BRIDGE = _Bridge()


# --------------------------------------------------------------------------- #
# Convenience wrappers for the two request kinds (called from tools.py)
# --------------------------------------------------------------------------- #


def confirm_action(
    *,
    tool: str,
    path: str = "",
    command: str = "",
    preview: str = "",
    summary: str = "",
) -> bool:
    """Ask the user to Allow/Deny a mutating action. Returns True iff allowed.

    ``preview`` is the scrollable text shown in the dialog (file content for
    write/edit, the command line for run_command/run_shell). Fail-closed on
    timeout / no-GUI / dialog error.
    """
    req: Dict[str, Any] = {
        "kind": "action",
        "tool": tool,
        "path": path,
        "command": command,
        "preview": preview,
        "summary": summary,
    }
    reply = BRIDGE.request_confirm(req)
    return bool(reply.get("accepted"))


def pick_directory(
    *, title: str = "Choose the folder Astral may read & write", default: str = ""
) -> Optional[str]:
    """Ask the user to pick a folder. Returns the absolute path or None."""
    req: Dict[str, Any] = {"kind": "directory", "title": title, "default": default}
    reply = BRIDGE.request_confirm(req)
    if not reply.get("accepted"):
        return None
    choice = reply.get("choice")
    return choice or None
