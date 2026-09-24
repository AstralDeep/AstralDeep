"""Tests for orchestrator/projection_surfaces/theme.py: preset and custom-color
rendering, persistence via a fake Plane theme-preference repository, and the
chrome_theme_preset save handler.
"""

import asyncio
from types import SimpleNamespace

from orchestrator.projection_surfaces import theme as theme_surface


class FakeThemeRepository:
    def __init__(self, prefs=None, fail_on_put=False, fail_on_get=False):
        self.theme = (prefs or {}).get("theme")
        self.fail_on_put = fail_on_put
        self.fail_on_get = fail_on_get
        self.put_calls = []

    def get(self, _transaction, *, owner_id):
        if self.fail_on_get:
            raise RuntimeError("plane down")
        if self.theme is None:
            return None
        return SimpleNamespace(owner_id=owner_id, theme=self.theme, updated_at=1)

    def put(self, _transaction, *, owner_id, theme):
        if self.fail_on_put:
            raise RuntimeError("plane down")
        self.put_calls.append((owner_id, theme))
        self.theme = dict(theme)
        return SimpleNamespace(owner_id=owner_id, theme=self.theme, updated_at=1)


class FakeThemeContext:
    def __init__(self, repository):
        self.repository = repository

    def call(self, operation, **kwargs):
        return operation(object(), **kwargs)


class FakeOrch:
    def __init__(self, prefs=None, fail_on_put=False, fail_on_get=False):
        repository = FakeThemeRepository(prefs, fail_on_put, fail_on_get)
        self.theme_preference_context = FakeThemeContext(repository)


def render(orch, params=None):
    return asyncio.run(theme_surface.render(orch, "user-1", ["user"], params or {}))


def handle(orch, payload):
    return asyncio.run(theme_surface.HANDLERS["chrome_theme_preset"](
        orch, None, "user-1", ["user"], payload))


def test_module_contract():
    assert theme_surface.TITLE == "Theme"
    assert not getattr(theme_surface, "ADMIN_ONLY", False)
    assert set(theme_surface.HANDLERS) == {"chrome_theme_preset"}


def test_render_has_all_preset_cards_with_action_and_swatches():
    html = render(FakeOrch())
    assert html.count('data-ui-action="chrome_theme_preset"') == 5
    for name in ("midnight", "daylight", "ocean", "sunset", "forest"):
        assert f"&quot;preset&quot;: &quot;{name}&quot;" in html, f"missing payload for {name}"
        assert name.capitalize() in html
    for hexval in ("#0F1221", "#F8FAFC", "#0EA5E9", "#F97316", "#22C55E"):
        assert f"background:{hexval}" in html


def test_render_embeds_seven_color_pickers_with_midnight_defaults():
    html = render(FakeOrch())
    assert html.count("astral-color-picker") == 7
    for key in ("bg", "surface", "primary", "secondary", "text", "muted", "accent"):
        assert f'data-color-key="{key}"' in html
    assert 'value="#0F1221"' in html
    assert 'value="#06B6D4"' in html
    assert "Current theme: default (Midnight)." in html


def test_render_reflects_persisted_preset():
    html = render(FakeOrch(prefs={"theme": {"preset": "ocean"}}))
    assert "Current theme: Ocean preset (saved)." in html
    assert 'aria-pressed="true"' in html and html.count('aria-pressed="true"') == 1
    assert ">Active</span>" in html
    assert 'value="#0C1222"' in html
    assert 'value="#2DD4BF"' in html


def test_render_overlays_single_custom_color_on_defaults():
    html = render(FakeOrch(prefs={"theme": {"color_key": "primary", "color_value": "#ABCDEF"}}))
    assert 'value="#ABCDEF"' in html
    assert 'value="#0F1221"' in html
    assert "custom colors" in html
    assert 'aria-pressed="true"' not in html


def test_render_overlays_colors_map_and_ignores_invalid_hex():
    prefs = {"theme": {"colors": {"bg": "112233", "accent": "<script>alert(1)</script>"}}}
    html = render(FakeOrch(prefs=prefs))
    assert 'value="#112233"' in html
    assert "<script>" not in html
    assert 'value="#06B6D4"' in html


def test_render_tolerates_db_failure_and_bad_theme_shape():
    html = render(FakeOrch(fail_on_get=True))
    assert "Current theme: default (Midnight)." in html

    html2 = render(FakeOrch(prefs={"theme": "not-a-dict"}))
    assert "Current theme: default (Midnight)." in html2


def test_preset_save_persists_like_save_theme_and_applies_instantly():
    orch = FakeOrch()
    surface, params, notice = handle(orch, {"preset": "ocean"})
    assert surface == "theme" and params == {}
    assert orch.theme_preference_context.repository.put_calls == [
        ("user-1", {"preset": "ocean"})
    ]
    assert "astral-chrome-notice" in notice and "Ocean theme saved." in notice
    assert "astral-theme-apply" in notice
    assert "&quot;preset&quot;: &quot;ocean&quot;" in notice
    assert "Theme applied" in notice


def test_unknown_preset_is_error_notice_without_save():
    orch = FakeOrch()
    surface, params, notice = handle(orch, {"preset": "<neon>"})
    assert surface == "theme"
    assert orch.theme_preference_context.repository.put_calls == []
    assert "bg-red-500/10" in notice
    assert "&lt;neon&gt;" in notice and "<neon>" not in notice
    assert "astral-theme-apply" not in notice


def test_missing_preset_is_error_notice():
    orch = FakeOrch()
    surface, _params, notice = handle(orch, {})
    assert surface == "theme"
    assert "Unknown theme preset" in notice
    assert orch.theme_preference_context.repository.put_calls == []


def test_db_failure_returns_error_notice_not_exception():
    orch = FakeOrch(fail_on_put=True)
    surface, _params, notice = handle(orch, {"preset": "forest"})
    assert surface == "theme"
    assert "Failed to save theme" in notice and "bg-red-500/10" in notice
    assert "astral-theme-apply" not in notice
