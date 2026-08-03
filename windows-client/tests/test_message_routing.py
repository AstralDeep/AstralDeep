"""Feature 044 (US1) — desktop inbound routing, connection UX, sign-out ladder.

Constructs a MainWindow with the transport + integrity check stubbed so no real
socket/thread runs, then drives _on_message / _on_status directly and asserts
the visible banner + turn state (FR-002/FR-003/FR-006/SC-006).
"""
import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ASTRAL_WIN_AGENT"] = "0"  # don't spawn the client-hosted tools agent

from astral_client import app as appmod  # noqa: E402
from astral_client.app import MainWindow, normalize_error  # noqa: E402


class _FakeClient:
    """Stands in for OrchestratorClient — records sends, never touches a socket."""
    def __init__(self, *a, **k):
        self.sent = []
        self._sig = None

    class _Sig:
        def connect(self, *_a):
            pass

    message = _Sig()
    status = _Sig()

    def start(self):
        pass

    def stop(self):
        pass

    def send_event(self, action, payload, session_id=None):
        self.sent.append((action, payload))

    def send_chat(self, *a, **k):
        pass


@pytest.fixture
def win(qapp, monkeypatch):
    monkeypatch.setattr(appmod, "OrchestratorClient", _FakeClient)
    monkeypatch.setattr(MainWindow, "_start_integrity_check", lambda self: None)
    # These are message-routing tests, not workspace tests: stub the workspace
    # init so constructing the window doesn't mutate process env
    # (ASTRAL_WORKSPACE_DIR) and leak into the win_agent tool tests.
    monkeypatch.setattr(MainWindow, "_init_workspace", lambda self: None)
    w = MainWindow("ws://127.0.0.1:9/ws", "dev-token")
    yield w
    w.close()


# --- normalize_error: the three historical shapes (FR-002) ------------------

def test_normalize_error_shapes():
    assert normalize_error({"message": "boom"}) == "boom"
    assert normalize_error({"payload": {"message": "deep boom"}}) == "deep boom"
    assert normalize_error({"code": "llm_config_invalid", "message": "bad"}) == "bad (llm_config_invalid)"
    assert normalize_error({"code": "internal", "message": "x"}) == "x"  # internal code hidden
    assert "wrong" in normalize_error({}).lower()


# --- error frames are visible and resolve the turn (SC-006) -----------------

def test_error_frame_shows_banner_and_resolves_turn(win):
    win._turn_active = True
    win._on_message({"type": "error", "code": "internal", "message": "server fell over"})
    assert (not win._banner.isHidden())
    assert "server fell over" in win._banner.text()
    assert win._turn_active is False


def test_notification_frame_shows_banner(win):
    win._on_message({"type": "notification", "title": "Job done", "body": "report ready", "level": "info"})
    assert (not win._banner.isHidden())
    assert "Job done" in win._banner.text() and "report ready" in win._banner.text()


# --- unknown vs classified-ignored logging (FR-002) -------------------------

def test_unknown_frame_is_logged_not_crashing(win, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="astral.client"):
        win._on_message({"type": "totally_new_server_frame"})
    assert any("unhandled frame type=totally_new_server_frame" in r.message for r in caplog.records)


def test_classified_ignore_is_info_not_warning(win, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="astral.client"):
        win._on_message({"type": "heartbeat"})  # classified "ignored"
    assert any("ignored frame type=heartbeat" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


# --- progress signals reach a terminal state (FR-006) -----------------------

def test_progress_signals_and_terminal(win):
    win._on_message({"type": "user_message_acked"})
    assert win._turn_active is True
    win._on_message({"type": "chat_step", "step": {"name": "search", "status": "completed"}})
    win._on_message({"type": "tool_progress", "label": "fetching page 2"})
    win._on_message({"type": "task_started", "task_id": "t1"})
    assert (not win._banner.isHidden())
    win._on_message({"type": "task_completed", "task_id": "t1"})
    assert win._turn_active is False


# --- connection UX (FR-003) -------------------------------------------------

def test_reconnecting_status_shows_banner(win):
    win._connected_once = True
    win._on_status("reconnecting:3")
    assert (not win._banner.isHidden())
    assert "attempt 3" in win._banner.text()


def test_connected_hides_banner(win):
    win._on_status("reconnecting:1")
    assert (not win._banner.isHidden())
    win._on_status("connected")
    assert not (not win._banner.isHidden())


def test_send_dropped_is_visible(win):
    win._on_status("send_dropped:chat_message")
    assert (not win._banner.isHidden())
    assert "chat_message" in win._banner.text()


# --- dead-auth sign-in affordance (FR-004) ----------------------------------

def test_expired_dev_session_does_not_dead_end(win):
    # dev-token / no login params → explicit guidance, not a frozen caption
    win._auth_session = None
    win._login_params = {}
    win._on_status("auth_required:expired")
    assert (not win._banner.isHidden())
    assert "expired" in win._banner.text().lower()


# --- workspace timeline read-only banner (FR-007 seed) ----------------------

def test_timeline_mode_banner(win):
    win._on_message({"type": "workspace_timeline_mode", "active": True})
    assert win._timeline_mode is True
    assert (not win._banner.isHidden())
    win._on_message({"type": "workspace_timeline_mode", "active": False})
    assert win._timeline_mode is False


# --- T041: timeline read-only disables the composer -------------------------

def test_timeline_mode_disables_composer(win):
    assert win._input.isEnabled() and win._send_btn.isEnabled()
    win._on_message({"type": "workspace_timeline_mode", "active": True})
    assert win._input.isEnabled() is False
    assert win._send_btn.isEnabled() is False
    win._on_message({"type": "workspace_timeline_mode", "active": False})
    assert win._input.isEnabled() is True
    assert win._send_btn.isEnabled() is True


# --- T032: ui_render target=history is routed, never silently dropped --------

def test_history_target_render_populates_dialog(win, caplog):
    import logging

    from astral_client.app import HistoryDialog

    win._history_dialog = HistoryDialog(win, lambda cid: None)
    with caplog.at_level(logging.INFO, logger="astral.client"):
        win._on_message({"type": "ui_render", "target": "history", "components": [
            {"type": "chat_history", "title": "Recent chats", "items": [
                {"chat_id": "c1", "title": "First"},
                {"chat_id": "c2", "title": "Second"}]}]})
    # Observable effect: the native Recent-chats dialog is populated (2 + stretch).
    assert win._history_dialog._listlay.count() == 3
    # And it is logged with intent (the old code silently `pass`ed).
    assert any("history surface rendered" in r.message for r in caplog.records)


def test_history_target_render_without_dialog_is_logged(win, caplog):
    import logging

    win._history_dialog = None
    with caplog.at_level(logging.INFO, logger="astral.client"):
        win._on_message({"type": "ui_render", "target": "history", "components": [
            {"type": "chat_history", "items": [{"chat_id": "c1", "title": "X"}]}]})
    assert any("history surface rendered" in r.message for r in caplog.records)


# --- T038: top bar renders server-model action controls ---------------------

def test_topbar_renders_and_routes_action_buttons(qapp):
    from astral_client.app import TopBar

    opened = []
    tb = TopBar("user", lambda: None, lambda: None,
                lambda s, ln: opened.append((s, ln)), lambda: None)
    tb.set_menu_model({
        "topbar": [
            {"key": "brand", "kind": "brand"},
            {"key": "timeline", "kind": "action", "label": "Workspace timeline",
             "icon": "history", "action": {"surface": "workspace_timeline", "params": {}}},
            {"key": "pulse", "kind": "action", "label": "Pulse",
             "icon": "pulse", "action": {"surface": "pulse", "params": {}}},
            {"key": "settings", "kind": "menu", "label": "Settings", "icon": "gear"},
        ],
        "menu": [],
        "signout": {"label": "Sign out", "action": "logout"},
    })
    assert len(tb._action_buttons) == 2
    # 066: action buttons are icon-only (web/Android presentation), so the
    # label lives in the accessible name and tooltip, not the button text.
    assert any(b.accessibleName() == "Workspace timeline" for b in tb._action_buttons)
    tb._action_buttons[0].click()
    assert opened and opened[0][0] == "workspace_timeline"


def test_topbar_actions_rebuilt_and_cleared(qapp):
    from astral_client.app import TopBar

    tb = TopBar("u", lambda: None, lambda: None, lambda s, ln: None, lambda: None)
    tb.set_menu_model({"topbar": [
        {"kind": "action", "label": "T", "action": {"surface": "workspace_timeline"}}]})
    assert len(tb._action_buttons) == 1
    tb.set_menu_model({"topbar": [], "menu": []})  # no actions -> cleared
    assert tb._action_buttons == []


def test_settings_menu_shows_group_headers_and_literal_ampersand(qapp):
    """The Settings dropdown must match the web/Android menus: visible ACCOUNT /
    HELP group headers (a styled QWidgetAction — addSection() text is dropped by
    Fusion) and a literal '&' in item labels (Qt mnemonic escaping)."""
    from PySide6.QtWidgets import QLabel, QWidgetAction

    from astral_client.app import TopBar

    tb = TopBar("u", lambda: None, lambda: None, lambda s, ln: None, lambda: None)
    tb.set_menu_model({
        "topbar": [],
        "menu": [
            {"key": "account", "label": "Account", "items": [
                {"key": "agents", "label": "Agents & permissions", "surface": "agents"},
                {"key": "theme", "label": "Theme", "surface": "theme"}]},
            {"key": "help", "label": "Help", "items": [
                {"key": "guide", "label": "User guide", "surface": "guide"}]},
        ],
        "signout": {"label": "Sign out", "action": "logout"},
    })
    header_texts = [
        wa.defaultWidget().text()
        for wa in tb._menu.actions()
        if isinstance(wa, QWidgetAction) and isinstance(wa.defaultWidget(), QLabel)
    ]
    assert "ACCOUNT" in header_texts and "HELP" in header_texts
    assert "Sign out" in header_texts  # the red sign-out QWidgetAction
    item_texts = [a.text() for a in tb._menu.actions() if a.text()]
    # Qt escape "&&" renders a literal "&" — the label must not lose it.
    assert "Agents && permissions" in item_texts
    assert {"Theme", "User guide"} <= set(item_texts)


# --- T040: settings surface load timeout + retry + in-flight ----------------

def test_surface_dialog_timeout_shows_retry_and_arrival_cancels(qapp):
    from PySide6.QtWidgets import QPushButton

    from astral_client.app import SurfaceDialog

    retried = []
    dlg = SurfaceDialog(None, emit=lambda a, p: None,
                        on_retry=lambda s, p: retried.append((s, p)))
    dlg.begin_load("theme", {}, title="Theme")
    assert dlg._timer.isActive()                       # bound armed on load
    dlg._on_timeout()                                  # simulate the timeout
    assert dlg._timer.isActive() is False
    retry = [b for b in dlg.findChildren(QPushButton) if b.text() == "Retry"]
    assert retry, "no Retry affordance after the load timeout"
    retry[0].click()                                   # re-request + re-arm
    assert dlg._timer.isActive()
    assert retried and retried[0][0] == "theme"
    dlg.set_surface("Theme", [{"type": "text", "content": "hi"}])  # arrival
    assert dlg._timer.isActive() is False              # bound cancelled
    dlg.close()


def test_surface_dialog_chrome_submit_shows_in_flight(qapp):
    from astral_client.app import SurfaceDialog

    sent = []
    dlg = SurfaceDialog(None, emit=lambda a, p: sent.append((a, p)))
    dlg.set_surface("LLM", [])
    # a chrome_* form submit from inside the surface shows the in-flight state
    dlg._emit_from_surface("chrome_llm_save", {"fields": {}})
    assert sent == [("chrome_llm_save", {"fields": {}})]
    assert not dlg._status.isHidden()             # in-flight status shown
    # arrival of the re-render clears it
    dlg.set_surface("LLM", [])
    assert dlg._status.isHidden()
    dlg.close()


def test_surface_dialog_switch_removes_stale_widgets_immediately(qapp):
    """Switching settings surfaces must not stack pages: `_clear_body` has to
    detach the previous surface's widgets from the paint tree SYNCHRONOUSLY
    (setParent(None)), not just deleteLater() them — a deferred delete doesn't
    run during nested/synthetic event processing, which painted one surface's
    components over the next (seen as Personalization bleeding into Theme)."""
    from PySide6.QtWidgets import QLabel

    from astral_client.app import SurfaceDialog

    dlg = SurfaceDialog(None, emit=lambda a, p: None)
    dlg.set_surface("Personalization", [{"type": "text", "content": "SOUL-TAB"}])
    assert any("SOUL-TAB" in (w.text() or "") for w in dlg._inner.findChildren(QLabel))
    dlg.set_surface("Theme", [{"type": "text", "content": "PRESETS"}])
    texts = [(w.text() or "") for w in dlg._inner.findChildren(QLabel)]
    assert any("PRESETS" in t for t in texts)
    assert not any("SOUL-TAB" in t for t in texts), (
        "previous surface's widgets still attached after set_surface switch"
    )
    # the body layout holds exactly the new component (+ the trailing stretch)
    assert dlg._lay.count() == 2
    dlg.close()


# --- W1: an empty chrome_surface frame is the server's CLOSE instruction -----

def test_chrome_surface_close_frame_closes_open_dialog(win):
    """chrome_surface {surface_key:"", title:"", components:[]} is a CLOSE
    (shared/protocol.py: empty components clears/closes the modal) — it must
    close the open SurfaceDialog, not repaint it as a blank Settings page."""
    win._on_message({"type": "chrome_surface", "surface_key": "theme",
                     "title": "Theme",
                     "components": [{"type": "text", "content": "PRESETS"}]})
    assert win._surface_dialog is not None
    assert not win._surface_dialog.isHidden()
    win._on_message({"type": "chrome_surface", "surface_key": "", "title": "",
                     "components": []})
    assert win._surface_dialog.isHidden()


def test_chrome_surface_close_frame_does_not_create_dialog(win):
    """A close frame with no dialog open must not lazily create one (the old
    handler popped a blank modal over the canvas)."""
    assert win._surface_dialog is None
    win._on_message({"type": "chrome_surface", "surface_key": "", "title": "",
                     "components": []})
    assert win._surface_dialog is None


# --- 054 (T019): mandatory first-run gate on chrome_surface ------------------

_MANDATORY_FRAME = {
    "type": "chrome_surface", "surface_key": "llm",
    "title": "Set up your AI provider",
    "components": [{"type": "text", "content": "provider form"}],
    "mode": "mandatory",
}

_CLOSE_FRAME = {"type": "chrome_surface", "surface_key": "", "title": "",
                "components": [], "mode": "replace"}


def test_chrome_surface_mode_absent_is_replace(win):
    """Regression: a frame without `mode` (or mode:"replace") behaves exactly
    as today — non-modal dialog, ✕ intact, no sign-out affordance."""
    win._on_message({"type": "chrome_surface", "surface_key": "theme",
                     "title": "Theme",
                     "components": [{"type": "text", "content": "PRESETS"}]})
    dlg = win._surface_dialog
    assert dlg is not None and not dlg.isHidden()
    assert dlg._mandatory is False
    assert dlg.isModal() is False
    assert dlg._signout_btn.isHidden()
    assert dlg.close() is True                     # dismissable as before
    assert dlg.isHidden()


def test_chrome_surface_mandatory_pins_modal_and_suppresses_dismissal(win):
    from PySide6.QtCore import Qt

    win._on_message(dict(_MANDATORY_FRAME))
    dlg = win._surface_dialog
    assert dlg is not None and not dlg.isHidden()
    assert dlg._mandatory is True
    # Application-modal — the main window is not interactable behind it.
    assert dlg.windowModality() == Qt.WindowModality.ApplicationModal
    # Titlebar ✕ removed (CustomizeWindowHint set, close hint absent).
    assert dlg.windowFlags() & Qt.WindowType.CustomizeWindowHint
    assert not (dlg.windowFlags() & Qt.WindowType.WindowCloseButtonHint)
    # Sign-out escape hatch (FR-013) present only while mandatory.
    assert not dlg._signout_btn.isHidden()
    # Esc (reject) and close are both refused while pinned.
    dlg.reject()
    assert not dlg.isHidden()
    assert dlg.close() is False
    assert not dlg.isHidden()


def test_mandatory_signout_button_invokes_sign_out_routine(qapp):
    from astral_client.app import SurfaceDialog

    signed_out = []
    dlg = SurfaceDialog(None, emit=lambda a, p: None,
                        on_sign_out=lambda: signed_out.append(True))
    assert dlg._signout_btn.isHidden()             # hidden while not mandatory
    dlg.set_mandatory(True)
    assert not dlg._signout_btn.isHidden()
    dlg._signout_btn.click()
    assert signed_out == [True]
    dlg.set_mandatory(False)
    assert dlg._signout_btn.isHidden()
    dlg.close()


def test_blank_close_clears_mandatory_pin_then_plain_surfaces_unchanged(win):
    win._on_message(dict(_MANDATORY_FRAME))
    dlg = win._surface_dialog
    assert dlg._mandatory is True
    # The server's existing blank close instruction releases the pin + closes.
    win._on_message(dict(_CLOSE_FRAME))
    assert dlg._mandatory is False
    assert dlg.isHidden()
    # A subsequent plain surface reuses the dialog with today's behavior.
    win._on_message({"type": "chrome_surface", "surface_key": "theme",
                     "title": "Theme",
                     "components": [{"type": "text", "content": "PRESETS"}]})
    assert not dlg.isHidden()
    assert dlg.isModal() is False
    assert dlg._signout_btn.isHidden()
    assert dlg.close() is True


# --- W4: workspace_timeline opens the SDUI timeline surface ------------------

def test_workspace_timeline_routes_to_sdui_surface(win):
    """The timeline menu/topbar entry must open the SDUI workspace-timeline
    surface (chrome_open round-trip), NOT the legacy Recent-chats dialog —
    otherwise the snapshot list/view/back-to-live surface is unreachable."""
    win.client.sent.clear()
    win._open_surface("workspace_timeline", "Workspace timeline")
    assert ("chrome_open",
            {"surface": "workspace_timeline", "params": {}}) in win.client.sent
    assert win._surface_dialog is not None       # SDUI dialog, in-flight state
    assert win._history_dialog is None           # legacy dialog NOT opened
    assert ("get_history", {}) not in win.client.sent


# --- M3: a client-local action must NOT arm the surface load timeout ---------

def test_surface_dialog_client_local_action_does_not_arm_timer(qapp):
    """`attach_existing` (a client_local_actions entry) is handled in-app and
    never yields a server chrome_surface re-render, so it must not arm the 10s
    load timeout — which would wrongly fire and wipe the attachments surface."""
    from astral_client.app import SurfaceDialog

    sent = []
    dlg = SurfaceDialog(None, emit=lambda a, p: sent.append((a, p)))
    dlg.set_surface("Your files", [])
    assert dlg._timer.isActive() is False
    dlg._emit_from_surface("attach_existing", {"attachment_id": "att-1"})
    assert sent == [("attach_existing", {"attachment_id": "att-1"})]  # raw emit still fires
    assert dlg._timer.isActive() is False   # NOT armed for a client-local action
    assert dlg._status.isHidden()           # no in-flight state shown
    dlg.close()


def test_client_local_actions_includes_attach_existing():
    from astral_client.app import _CLIENT_LOCAL_ACTIONS

    assert "attach_existing" in _CLIENT_LOCAL_ACTIONS


# --- M4: chat_status:done is a per-turn reset, not a full reconnect re-sync ---

def test_chat_status_done_does_not_clear_banner_or_resync(win):
    # An error banner is showing (and a turn was active).
    win._turn_active = True
    win._on_message({"type": "error", "code": "internal", "message": "server fell over"})
    assert not win._banner.isHidden()
    win.client.sent.clear()  # ignore anything captured during setup
    # A turn completing must NOT wipe the banner nor re-fire the reconnect re-sync.
    win._on_message({"type": "chat_status", "status": "done"})
    assert not win._banner.isHidden()
    assert "server fell over" in win._banner.text()
    actions = [a for a, _ in win.client.sent]
    assert "discover_agents" not in actions
    assert "get_history" not in actions
    assert win._turn_active is False


def test_stream_unsubscribed_does_not_resync(win):
    win.client.sent.clear()
    win._on_message({"type": "stream_unsubscribed", "stream_id": "s1"})
    actions = [a for a, _ in win.client.sent]
    assert "discover_agents" not in actions and "get_history" not in actions


def test_real_connected_still_resyncs(win):
    # The genuine (re)connect transition still does the full re-sync.
    win.client.sent.clear()
    win._on_status("connected")
    actions = [a for a, _ in win.client.sent]
    assert "discover_agents" in actions and "get_history" in actions


# --- W5: theme apply from the canvas restyles via the app path (deferred) ----

def test_theme_apply_component_triggers_app_restyle(win, qapp, monkeypatch):
    """A theme_apply component rendered on the canvas routes through the
    MainWindow's single theme path (RenderContext.apply_theme) and triggers the
    full restyle — DEFERRED out of the render pass (headless-Qt segfault
    protection), delivered on the next event-loop turn."""
    import astral_client.theme as T

    restyled = []
    monkeypatch.setattr(win, "_restyle_all", lambda: restyled.append(True))
    snap = dict(T.PALETTE)
    try:
        win.canvas.set_components([{"type": "theme_apply", "preset": "forest"}])
        assert T.PALETTE["primary"] == T.PRESETS["forest"]["primary"]
        assert restyled == []          # deferred, never inside the render pass
        for _ in range(100):
            qapp.processEvents()
            if restyled:
                break
        assert restyled == [True]      # the ONE restyle path ran
    finally:
        T.PALETTE.clear()
        T.PALETTE.update(snap)
        T._derive()
        T.APP_STYLESHEET = T.build_stylesheet()


# --- M1: silent token refresh runs OFF the GUI thread -----------------------

def test_silent_refresh_done_reconnects_on_token(win, monkeypatch):
    reconnected = []
    monkeypatch.setattr(win, "_reconnect", lambda tok: reconnected.append(tok))
    win._silent_refresh_active = True
    win._on_silent_refresh_done("NEWTOKEN")
    assert reconnected == ["NEWTOKEN"]
    assert win._silent_refresh_active is False


def test_silent_refresh_done_prompts_on_failure(win, monkeypatch):
    prompted = []
    monkeypatch.setattr(win, "_prompt_reauth", lambda: prompted.append(True))
    win._silent_refresh_active = True
    win._on_silent_refresh_done(None)
    assert prompted == [True]
    assert win._silent_refresh_active is False


def test_auth_required_runs_refresh_off_gui_thread(win, qapp, monkeypatch):
    """The silent refresh (a blocking urlopen up to 15s) must run on a worker
    thread, not the GUI thread where _on_status is a slot (M1)."""
    import threading

    reconnected = []
    monkeypatch.setattr(win, "_reconnect", lambda tok: reconnected.append(tok))
    seen = {}
    done = threading.Event()
    main_thread = threading.current_thread()

    class _Sess:
        access_token = "old"
        refresh_token = "r"
        client_id = "astral-desktop"
        token_url = ""

        def refresh(self):
            seen["thread"] = threading.current_thread()
            done.set()
            return "NEWTOKEN"

    win._auth_session = _Sess()
    win._reauth_tries = 0
    win._on_status("auth_required:expired")
    assert win._silent_refresh_active is True     # in flight — GUI thread not blocked
    assert done.wait(3.0), "the refresh worker never ran"
    assert seen["thread"] is not main_thread      # ran OFF the GUI thread
    # deliver the queued result on the GUI thread → reconnect with the new token
    for _ in range(100):
        qapp.processEvents()
        if reconnected:
            break
    assert reconnected == ["NEWTOKEN"]
    assert win._silent_refresh_active is False


def test_auth_required_bound_exhausted_prompts(win, monkeypatch):
    prompted = []
    monkeypatch.setattr(win, "_prompt_reauth", lambda: prompted.append(True))

    class _Sess:
        access_token = "old"
        refresh_token = "r"
        client_id = "c"
        token_url = ""

        def refresh(self):  # pragma: no cover — must not be called past the bound
            raise AssertionError("refresh attempted past the retry bound")

    win._auth_session = _Sess()
    win._reauth_tries = 2  # bound already exhausted
    win._on_status("auth_required:expired")
    assert prompted == [True]
    assert win._silent_refresh_active is False
