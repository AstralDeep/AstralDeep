"""Feature 027 — T011: top bar + static settings menu structure and gating.

Structural invariants (not byte-exact): menu groups/entries per
contracts/settings-surfaces.md, admin DOM-absence (SC-005), ARIA menu
markup (FR-017), sign-out plain link, and modal/notice escaping.
"""
import pytest

from webrender.chrome import chrome_error_block, notice_block, render_modal_shell, render_topbar


@pytest.fixture(autouse=True)
def _pulse_off_by_default(monkeypatch):
    """Keep the host flag off; Projection receives availability explicitly."""
    monkeypatch.delenv("FF_PULSE_DIGEST", raising=False)


def test_topbar_has_status_and_settings_trigger():
    html = render_topbar(roles=["user"])
    # Feature 089: the brand is the sidebar's own #astral-brand block (see
    # test_brand_lives_in_the_sidebar_exactly_once), so this renderer emits
    # only the control cluster.
    assert 'src="/static/img/AstralDeep.png"' not in html
    assert 'data-tour-target="topbar.brand"' not in html
    assert 'id="astral-status"' in html
    assert 'id="astral-settings-btn"' in html
    assert 'aria-haspopup="menu"' in html and 'aria-expanded="false"' in html
    assert 'id="astral-settings-menu"' in html and 'role="menu"' in html


def test_brand_lives_in_the_sidebar_exactly_once():
    """Feature 089: one logo, one tour target, and it returns to the landing.

    Two brand blocks would give the tour two targets to choose between and a
    reader two things that look like the same control.
    """
    from astralprojection.resources import template_path

    shell = template_path("shell.html").read_text(encoding="utf-8")
    assert shell.count('data-tour-target="topbar.brand"') == 1
    # The reference carries its content-hash token, like every other
    # static reference in the shell.
    assert shell.count('/static/img/AstralDeep.png?v=%%ASTRAL_V:') == 1
    assert 'id="astral-brand"' in shell
    brand = shell[shell.index('id="astral-brand"'):]
    assert 'aria-label="Return to the dashboard"' in brand[:400]
    assert shell.index('id="astral-sidebar"') < shell.index('id="astral-brand"')
    assert shell.index('id="astral-brand"') < shell.index('id="astral-main"')


def test_menu_contains_account_and_help_groups_for_everyone():
    html = render_topbar(roles=["user"])
    for label in ("Account", "Help"):
        assert label in html
    for entry in ("Agents &amp; permissions", "LLM settings", "Personalization",
                  "Audit log", "Theme", "Take the tour", "User guide", "Sign out"):
        assert entry in html, f"menu missing entry: {entry}"


def test_menu_entries_carry_chrome_open_actions():
    html = render_topbar(roles=["user"])
    assert 'data-ui-action="chrome_open"' in html
    for surface in ("agents", "llm", "personalization", "audit", "theme", "tour", "guide"):
        assert f'&quot;surface&quot;: &quot;{surface}&quot;' in html, f"missing surface payload: {surface}"


def test_workspace_timeline_promoted_to_topbar_icon():
    """Feature 045: the workspace timeline is a dedicated icon button next to
    Settings (one click back to an earlier canvas), not a Settings-menu entry."""
    html = render_topbar(roles=["user"])
    # The icon button is present, labelled, and tour-targetable.
    assert 'id="astral-timeline-btn"' in html
    assert 'aria-label="Workspace timeline"' in html
    assert 'data-tour-target="topbar.timeline"' in html
    # It fires the same chrome_open surface the menu entry used to.
    assert '&quot;surface&quot;: &quot;workspace_timeline&quot;' in html
    # It sits OUTSIDE (before) the Settings dropdown…
    assert html.index('id="astral-timeline-btn"') < html.index('id="astral-settings"')
    # …and is no longer an item inside the Settings menu.
    assert 'data-menu-key="timeline"' not in html


def test_sign_out_is_plain_link_outside_js():
    html = render_topbar(roles=["user"])
    assert 'href="/auth/logout"' in html and 'role="menuitem"' in html


def test_new_chat_button_in_topbar_for_every_role():
    """The New-chat button is core chrome on every client (Windows ＋ New,
    Android RootScaffold onNewChat) — the web top bar carries its twin,
    labelled, tour-targetable, and OUTSIDE (before) the Settings dropdown.
    Client-local behavior (reset + `new_chat` event) lives in client.js."""
    for roles in (["user"], ["admin", "user"], None):
        html = render_topbar(roles=roles)
        assert 'id="astral-newchat-btn"' in html
        assert 'aria-label="New chat"' in html
        assert 'data-tour-target="topbar.new-chat"' in html
        assert html.index('id="astral-newchat-btn"') < html.index('id="astral-settings"')
        # NOT a chrome_open surface — no settings-menu entry for it.
        assert 'data-menu-key="new-chat"' not in html


def test_canvas_page_actions_live_in_the_topbar_hidden_by_default():
    """Export page / Share page moved out of a sticky bar above the canvas and
    into the top bar (066). Two things must hold together: they ship HIDDEN, so
    an empty or unflagged canvas shows no chrome at all and client.js is the
    only thing that reveals them; and they keep the class names the delegated
    click handlers dispatch on, so the move changed placement, not behavior."""
    for roles in (["user"], ["admin", "user"], None):
        html = render_topbar(roles=roles, export_enabled=True, share_enabled=True)
        for btn_id in ('id="astral-export-page-btn"', 'id="astral-share-page-btn"'):
            assert btn_id in html
            assert html.index(btn_id) < html.index('id="astral-settings"')
        # The handler hooks: export by class, share by class + canvas scope.
        assert "astral-export-canvas" in html
        assert 'data-share-scope="canvas"' in html
        # Both are hidden at render time — nothing reveals itself server-side.
        assert html.count("hidden") >= 2
        assert 'aria-label="Export page"' in html
        assert 'aria-label="Share page"' in html


def test_conversation_restore_control_is_in_the_floating_panel():
    """Restoring the sidebar stays available beside the floating transcript."""
    from astralprojection.resources import template_path

    shell = template_path("shell.html").read_text(encoding="utf-8")
    assert 'id="astral-restore-chat-btn"' in shell
    assert 'aria-label="Move conversation to the right sidebar"' in shell
    assert shell.index('id="astral-form"') < shell.index('id="astral-restore-chat-btn"')
    for roles in (["user"], ["admin", "user"], None):
        html = render_topbar(roles=roles)
        assert 'id="astral-topbar-chat-btn"' not in html


# ── Feature 033 (C-U8) — Pulse digest top-bar icon (flag-gated) ──────────────

def test_pulse_icon_absent_when_host_disables_it():
    """Host policy OFF: the Pulse button is absent from the DOM entirely."""
    html = render_topbar(roles=["user"], pulse_enabled=False)
    assert 'id="astral-pulse-btn"' not in html
    assert '&quot;surface&quot;: &quot;pulse&quot;' not in html


def test_pulse_icon_present_when_host_enables_it():
    """Host policy ON: the Pulse icon button appears, labelled, firing chrome_open →
    surface 'pulse', and sits before the Settings dropdown."""
    html = render_topbar(roles=["user"], pulse_enabled=True)
    assert 'id="astral-pulse-btn"' in html
    assert 'aria-label="Pulse digest"' in html
    assert 'data-tour-target="topbar.pulse"' in html
    assert 'data-ui-action="chrome_open"' in html
    assert '&quot;surface&quot;: &quot;pulse&quot;' in html
    # It sits OUTSIDE (before) the Settings dropdown.
    assert html.index('id="astral-pulse-btn"') < html.index('id="astral-settings"')


def test_pulse_icon_on_for_any_role():
    """Pulse is per-user (not admin-gated) — present for a plain user too."""
    assert 'id="astral-pulse-btn"' in render_topbar(
        roles=["user"], pulse_enabled=True
    )
    assert 'id="astral-pulse-btn"' in render_topbar(
        roles=None, pulse_enabled=True
    )


def test_admin_group_present_for_admin():
    html = render_topbar(roles=["admin", "user"])
    assert "Admin tools" in html
    assert "Tool quality" in html and "Tutorial admin" in html
    assert "admin_tools" in html


def test_admin_group_dom_absent_for_non_admin():
    """SC-005: zero admin references in a non-admin's rendered output."""
    html = render_topbar(roles=["user"])
    for marker in ("Admin tools", "Tool quality", "Tutorial admin", "admin_tools"):
        assert marker not in html, f"admin marker leaked to non-admin DOM: {marker}"


def test_admin_group_dom_absent_for_empty_roles():
    html = render_topbar(roles=None)
    assert "Admin tools" not in html


def test_modal_shell_escapes_title():
    html = render_modal_shell("<script>alert(1)</script>", "<p>body</p>", "agents")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<p>body</p>" in html  # body is trusted, pre-rendered chrome output
    assert 'role="dialog"' in html and 'aria-modal="true"' in html
    assert "astral-modal-close" in html


def test_error_and_notice_blocks_escape_and_mark_roles():
    err = chrome_error_block("boom <img onerror=x>", retry_surface="agents")
    assert "<img" not in err and "&lt;img" in err
    assert 'data-ui-action="chrome_open"' in err  # retry affordance
    ok = notice_block("success", "saved <b>!</b>")
    assert "<b>" not in ok and "&lt;b&gt;" in ok
    assert 'role="status"' in ok
