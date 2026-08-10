"""The GUI must not advertise a tools listener it does not own.

`_on_status` tells the orchestrator to dial `http://{host}:{port}` on every
`connected`, gated only on `_win_agent_enabled`. The listener now refuses to
start without a usable `AGENT_API_KEY`, so a failed start MUST turn the feature
off — the previous `except Exception: pass` left the flag True and pointed the
orchestrator at a port this process does not listen on.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PySide6")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from astral_client.app import MainWindow  # noqa: E402


@pytest.fixture(autouse=True)
def _contained_side_effects():
    """Undo everything a real MainWindow + a real listener leave behind.

    Two distinct leaks, both of which really did break other tests:

    * **Listeners.** These tests bind REAL sockets (that is the point — a mocked
      start could not catch a bind failure). Unstopped, each leaves an aiohttp
      runner and an event loop alive on a daemon thread for the rest of the
      session, still answering requests.
    * **The workspace override.** ``MainWindow`` sets the module-global
      ``tools._WORKSPACE_OVERRIDE``, which WINS over ``ASTRAL_WORKSPACE_DIR`` —
      so it silently defeats any later test that monkeypatches that env var to
      point the file tools at a tmp dir (observed: test_win_agent's
      test_list_directory saw the real workspace and counted 0 entries).
    """
    import win_agent.agent as wa
    import win_agent.tools as wt

    started = []
    real = wa.start_agent_thread
    saved_workspace = wt._WORKSPACE_OVERRIDE

    def _tracking(*a, **k):
        thread = real(*a, **k)
        if thread is not None:
            started.append(thread)
        return thread

    wa.start_agent_thread = _tracking
    try:
        yield
    finally:
        wa.start_agent_thread = real
        wt._WORKSPACE_OVERRIDE = saved_workspace
        for thread in started:
            loop = getattr(thread, "_astral_loop", None)
            if loop is not None:
                loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)


def _window(monkeypatch, *, key: str | None):
    """A MainWindow with no socket, with/without a usable agent key."""
    if key is None:
        monkeypatch.delenv("AGENT_API_KEY", raising=False)
    else:
        monkeypatch.setenv("AGENT_API_KEY", key)
    monkeypatch.setenv("ASTRAL_WIN_AGENT", "1")
    monkeypatch.setenv("ASTRAL_AGENT_BIND", "127.0.0.1")
    # Port 0 lets the OS pick, so a real start never collides with a live agent.
    monkeypatch.setenv("WIN_AGENT_PORT", "0")
    return MainWindow("ws://127.0.0.1:8001/ws", "t", connect=False)


def test_no_key_disables_the_feature_entirely(qapp, monkeypatch):
    win = _window(monkeypatch, key=None)
    assert win._win_agent_thread is None
    assert win._win_agent_enabled is False, (
        "a refused listener must disable the feature, or _on_status will still "
        "send register_external_agent for a port nothing is serving"
    )


def test_weak_key_is_treated_as_no_key(qapp, monkeypatch):
    win = _window(monkeypatch, key="short")
    assert win._win_agent_thread is None
    assert win._win_agent_enabled is False


def test_usable_key_starts_the_listener(qapp, monkeypatch):
    win = _window(monkeypatch, key="startup-gate-key-0123456789ab")
    assert win._win_agent_thread is not None
    assert win._win_agent_enabled is True


def test_start_is_idempotent_across_deferred_first_run(qapp, monkeypatch):
    """The first-run dialog can supply the key AFTER the window exists, so the
    start is re-attempted — but must never double-bind."""
    win = _window(monkeypatch, key="startup-gate-key-0123456789ab")
    first = win._win_agent_thread
    assert first is not None
    win.maybe_start_tools_agent()
    win.maybe_start_tools_agent()
    assert win._win_agent_thread is first


def test_key_arriving_after_construction_still_starts_the_listener(qapp, monkeypatch):
    """The defect this guards: window built with no key -> listener refused ->
    key arrives from the first-run dialog -> listener would never start.

    Drives EXACTLY what _after_first_paint does — resolve config, then call
    maybe_start_tools_agent() — with no test-only flag poking. An earlier
    version of this test set _win_agent_enabled=True first, which the production
    caller never does, and so it passed against a build where the retry was dead
    code (the refusal cleared the very flag the method guards on)."""
    win = _window(monkeypatch, key=None)
    assert win._win_agent_thread is None
    monkeypatch.setenv("AGENT_API_KEY", "late-arriving-key-0123456789")
    win.maybe_start_tools_agent()          # the production call, verbatim
    assert win._win_agent_thread is not None
    assert win._win_agent_enabled is True


def test_a_failed_bind_is_reported_as_a_failure(qapp, monkeypatch):
    """The bind happens on a worker thread, so a failure there lands AFTER
    start_agent_thread returns. Without waiting for it, the caller would hold a
    live thread object and tell the orchestrator to dial a dead port."""
    import win_agent.agent as wa

    monkeypatch.setenv("AGENT_API_KEY", "bind-failure-key-0123456789ab")

    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def start(self):
            raise OSError("address already in use")

    monkeypatch.setattr(wa.web, "TCPSite", _Boom)
    assert wa.start_agent_thread(host="127.0.0.1", port=0) is None


def test_the_deferred_call_site_exists_and_is_ordered_after_config(qapp):
    """Pin the wiring: the key can arrive from the first-run dialog only AFTER
    the window is built, so _after_first_paint must resolve config and then
    retry the listener."""
    import inspect

    from astral_client import app as appmod

    src = inspect.getsource(appmod._launch)
    body = src.split("_after_first_paint", 1)[1]
    code = "\n".join(ln for ln in body.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "maybe_start_tools_agent()" in code
    assert code.index("_resolve_config(") < code.index("maybe_start_tools_agent()")


def test_refusal_is_surfaced_to_the_user(qapp, monkeypatch):
    """A listener that silently declines to exist is a support ticket.

    The notice is DEFERRED to the next event-loop turn: during __init__ the
    banner widget is not yet in a layout, and the startup sign-in status would
    overwrite it in the same turn anyway — so the test has to let that turn run.
    """
    shown = []
    monkeypatch.setattr(MainWindow, "_show_banner",
                        lambda self, text, kind="info", *a, **k: shown.append((text, kind)))
    _window(monkeypatch, key=None)
    assert shown == [], "the notice must not fire inside __init__"
    qapp.processEvents()
    assert shown, "no banner shown when the tools listener was refused"
    text, kind = shown[-1]
    assert "AGENT_API_KEY" in text
    assert kind == "warning"


def test_the_refusal_notice_fires_once_not_per_retry(qapp, monkeypatch):
    """maybe_start_tools_agent is called again after deferred config; a user who
    never sets a key must not collect a banner per attempt."""
    shown = []
    monkeypatch.setattr(MainWindow, "_show_banner",
                        lambda self, text, kind="info", *a, **k: shown.append((text, kind)))
    win = _window(monkeypatch, key=None)
    for _ in range(3):
        win.maybe_start_tools_agent()
        qapp.processEvents()
    assert len(shown) == 1, f"notice fired {len(shown)} times"


def test_a_later_success_cancels_the_pending_refusal_notice(qapp, monkeypatch):
    """Refused at construction, started once the key arrives — the user should
    not then be told the tools are off."""
    shown = []
    monkeypatch.setattr(MainWindow, "_show_banner",
                        lambda self, text, kind="info", *a, **k: shown.append((text, kind)))
    win = _window(monkeypatch, key=None)
    monkeypatch.setenv("AGENT_API_KEY", "late-arriving-key-0123456789")
    win.maybe_start_tools_agent()          # succeeds before the deferred notice runs
    qapp.processEvents()
    assert win._win_agent_thread is not None
    assert shown == [], "told the user tools were off after they came up"
