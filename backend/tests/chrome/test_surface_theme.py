"""Tests for orchestrator/projection_surfaces/theme.py: preset and custom-color
rendering, persistence via a fake Plane theme-preference repository, and the
chrome_theme_preset save handler.
"""

import asyncio

import pytest

from astralplane.repositories.preferences import ThemePreferenceRecord

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
        return ThemePreferenceRecord(owner_id=owner_id, theme=self.theme, updated_at=1)

    def put(self, _transaction, *, owner_id, theme):
        if self.fail_on_put:
            raise RuntimeError("plane down")
        self.put_calls.append((owner_id, theme))
        self.theme = dict(theme)
        return ThemePreferenceRecord(owner_id=owner_id, theme=self.theme, updated_at=1)


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
    assert set(theme_surface.HANDLERS) == {"chrome_theme_preset", "save_theme"}


@pytest.mark.parametrize("submitted", [
    {"color_key": "accent", "color_value": "abcdef"},
    {"colors": {"accent": "abcdef"}},
    {"preset": "ocean"},
])
def test_custom_theme_handler_returns_the_accepted_complete_palette(submitted):
    handler = theme_surface.HANDLERS.get("save_theme")
    assert callable(handler)
    orch = FakeOrch(prefs={"theme": {"colors": {"primary": "#123456"}}})
    surface, params, notice = asyncio.run(handler(orch, None, "user-1", ["user"], {"theme": submitted}))
    assert surface == "theme" and "astral-theme-apply" in notice
    stored = orch.theme_preference_context.repository.theme
    assert set(stored["colors"]) == {key for key, _ in theme_surface._COLOR_KEYS}
    assert stored["colors"]["accent"] == ("#2DD4BF" if "preset" in submitted else "#ABCDEF")
    if "preset" not in submitted:
        assert stored["colors"]["primary"] == "#123456"
    orch.theme_preference_context.repository.fail_on_get = True
    components = asyncio.run(theme_surface.components(orch, "user-1", ["user"], params))
    assert components[0]["type"] == "theme_apply" and components[0]["colors"] == stored["colors"]
    html = asyncio.run(theme_surface.render(orch, "user-1", ["user"], params))
    assert 'value="#123456"' in html or "#132038" in html


def test_client_params_cannot_forge_an_accepted_palette_snapshot():
    orch = FakeOrch(prefs={"theme": {"preset": "ocean"}})
    forged = {"_accepted_theme": {"colors": {"accent": "#BADBAD"}}}
    components = asyncio.run(theme_surface.components(orch, "user-1", ["user"], forged))
    assert components[0]["colors"]["accent"] == "#2DD4BF"


def test_server_palette_snapshot_cannot_be_used_for_another_owner():
    orch = FakeOrch(prefs={"theme": {"preset": "ocean"}})
    foreign = {"_accepted_theme": ThemePreferenceRecord(owner_id="foreign", theme={"colors": {"accent": "#BADBAD"}}, updated_at=1)}
    components = asyncio.run(theme_surface.components(orch, "user-1", ["user"], foreign))
    assert components[0]["colors"]["accent"] == "#2DD4BF"


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
