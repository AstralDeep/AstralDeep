"""Renders the Theme surface: preset color cards plus per-key color pickers, persisted
through Plane's owner-scoped theme preference and mirrored into client.js's PRESETS
map. Saving embeds a theme_apply block so the client restyles instantly.
"""

import asyncio
import json
import logging
import re
from collections.abc import Mapping

from webrender.chrome import esc, notice_block, render_one
from webrender.chrome.surfaces import _sdui

logger = logging.getLogger("Orchestrator.Chrome")

TITLE = "Theme"

PRESETS = {
    "midnight": {"bg": "#0F1221", "surface": "#1A1E2E", "primary": "#6366F1",
                 "secondary": "#8B5CF6", "text": "#F3F4F6", "muted": "#9CA3AF",
                 "accent": "#06B6D4"},
    "daylight": {"bg": "#F8FAFC", "surface": "#FFFFFF", "primary": "#4F46E5",
                 "secondary": "#7C3AED", "text": "#1E293B", "muted": "#64748B",
                 "accent": "#0891B2"},
    "ocean": {"bg": "#0C1222", "surface": "#132038", "primary": "#0EA5E9",
              "secondary": "#06B6D4", "text": "#E2E8F0", "muted": "#94A3B8",
              "accent": "#2DD4BF"},
    "sunset": {"bg": "#1C1017", "surface": "#2D1B24", "primary": "#F97316",
               "secondary": "#EF4444", "text": "#FEF2F2", "muted": "#A8A29E",
               "accent": "#FBBF24"},
    "forest": {"bg": "#0F1A14", "surface": "#1A2E22", "primary": "#22C55E",
               "secondary": "#10B981", "text": "#ECFDF5", "muted": "#86EFAC",
               "accent": "#A3E635"},
}

_DEFAULT_PRESET = "midnight"

_COLOR_KEYS = [
    ("bg", "Background"),
    ("surface", "Surface"),
    ("primary", "Primary"),
    ("secondary", "Secondary"),
    ("text", "Text"),
    ("muted", "Muted"),
    ("accent", "Accent"),
]

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


def _theme_context(orch):
    from orchestrator.plane_repository_context import (
        PlaneRepositoryContext,
        plane_source_from_orchestrator,
    )

    injected = getattr(orch, "theme_preference_context", None)
    if injected is not None:
        return injected
    source = plane_source_from_orchestrator(orch)
    return PlaneRepositoryContext(
        repository=source.plane_repositories.preferences.theme,
        plane_runtime=source.plane_runtime,
    )


def _normalize_hex(value) -> str:
    s = str(value or "").strip()
    if not _HEX_RE.match(s):
        return ""
    return s if s.startswith("#") else "#" + s


def _stored_theme(orch, user_id: str) -> dict:
    try:
        context = _theme_context(orch)
        record = context.call(context.repository.get, owner_id=user_id)
    except Exception:
        logger.exception("chrome theme: failed to load preferences for %s", user_id)
        return {}
    if record is None:
        return {}
    theme = record.theme
    return dict(theme) if isinstance(theme, Mapping) else {}


def _save_theme(orch, user_id: str, theme: dict) -> None:
    context = _theme_context(orch)
    context.call(context.repository.put, owner_id=user_id, theme=theme)


def _effective_colors(theme: dict) -> dict:
    colors = dict(PRESETS[_DEFAULT_PRESET])
    preset = theme.get("preset")
    if isinstance(preset, str) and preset in PRESETS:
        colors.update(PRESETS[preset])
        return colors
    stored = theme.get("colors")
    if isinstance(stored, dict):
        for key, _label in _COLOR_KEYS:
            hexval = _normalize_hex(stored.get(key))
            if hexval:
                colors[key] = hexval
        return colors
    key = theme.get("color_key")
    hexval = _normalize_hex(theme.get("color_value"))
    if isinstance(key, str) and key in colors and hexval:
        colors[key] = hexval
    return colors


def _summary_text(theme: dict) -> str:
    preset = theme.get("preset")
    if isinstance(preset, str) and preset in PRESETS:
        return f"Current theme: {preset.capitalize()} preset (saved)."
    if isinstance(theme.get("colors"), dict) or theme.get("color_key"):
        return "Current theme: custom colors (defaults shown where unset)."
    return f"Current theme: default ({_DEFAULT_PRESET.capitalize()})."


def _preset_card(name: str, active: bool) -> str:
    payload = esc(json.dumps({"preset": name}))
    swatches = "".join(
        f'<span class="flex-1 h-6" style="background:{esc(PRESETS[name][key])}"></span>'
        for key, _label in _COLOR_KEYS
    )
    border = "border-astral-primary ring-1 ring-astral-primary/40" if active else (
        "border-white/10 hover:border-white/25")
    badge = ""
    if active:
        badge = ('<span class="ml-2 text-[10px] font-semibold uppercase tracking-wider '
                 'text-astral-primary">Active</span>')
    return (
        f'<button type="button" class="astral-theme-preset text-left rounded-lg border {border} '
        f'bg-white/5 p-3 focus:outline-none focus:ring-1 focus:ring-astral-primary/40" '
        f'aria-pressed="{"true" if active else "false"}" '
        f"data-ui-action=\"chrome_theme_preset\" data-ui-payload='{payload}'>"
        f'<span class="flex rounded-md overflow-hidden border border-white/10">{swatches}</span>'
        f'<span class="mt-2 flex items-center text-sm font-medium text-astral-text">'
        f'{esc(name.capitalize())}{badge}</span></button>'
    )


async def render(orch, user_id, roles, params) -> str:
    theme = await asyncio.to_thread(_stored_theme, orch, user_id)
    active_preset = theme.get("preset") if theme.get("preset") in PRESETS else None
    colors = _effective_colors(theme)

    cards = "".join(_preset_card(name, name == active_preset) for name in PRESETS)
    pickers = "".join(
        render_one({"type": "color_picker", "color_key": key,
                    "value": colors[key], "label": label})
        for key, label in _COLOR_KEYS
    )
    return (
        f'<p class="text-xs text-astral-muted">{esc(_summary_text(theme))}</p>'
        f'<div class="space-y-2">'
        f'<h3 class="text-sm font-semibold text-astral-text">Presets</h3>'
        f'<p class="text-xs text-astral-muted">Pick a preset to apply and save it.</p>'
        f'<div class="grid grid-cols-1 sm:grid-cols-2 gap-3">{cards}</div></div>'
        f'<div class="space-y-2 border-t border-white/5 pt-4">'
        f'<h3 class="text-sm font-semibold text-astral-text">Fine-tune colors</h3>'
        f'<p class="text-xs text-astral-muted">Color changes apply and save instantly.</p>'
        f'<div class="bg-white/5 border border-white/10 rounded-lg p-3">{pickers}</div></div>'
    )


async def components(orch, user_id, roles, params):
    theme = await asyncio.to_thread(_stored_theme, orch, user_id)
    active = theme.get("preset") if theme.get("preset") in PRESETS else None
    colors = _effective_colors(theme)

    out = []
    if theme:
        spec = {"type": "theme_apply", "message": "Theme applied",
                "colors": dict(colors)}
        if active:
            spec["preset"] = active
        out.append(spec)
    out += [
        _sdui.text(_summary_text(theme), "caption"),
        _sdui.text("Presets", "h3"),
        _sdui.text("Pick a preset to apply and save it.", "caption"),
    ]
    for name in PRESETS:
        is_active = name == active
        swatches = _sdui.container(
            [{"type": "container", "children": [],
              "css": {"background": PRESETS[name][key], "height": "22px", "flex": "1"}}
             for key, _label in _COLOR_KEYS],
            direction="row",
        )
        out.append(_sdui.card(
            name.capitalize() + (" — Active" if is_active else ""),
            [swatches, _sdui.button(
                "Applied" if is_active else f"Apply {name.capitalize()}",
                "chrome_theme_preset", {"preset": name},
                variant="secondary" if is_active else "primary")],
        ))
    out.append(_sdui.text("Fine-tune colors", "h3"))
    out.append(_sdui.text("Color changes apply and save instantly.", "caption"))
    for key, label in _COLOR_KEYS:
        out.append({"type": "color_picker", "color_key": key,
                    "value": colors[key], "label": label})
    return out


async def _handle_theme_preset(orch, websocket, user_id, roles, payload):
    preset = str((payload or {}).get("preset") or "").strip().lower()
    if preset not in PRESETS:
        return ("theme", {}, notice_block("error", f"Unknown theme preset: {preset or '(none)'}"))
    try:
        await asyncio.to_thread(_save_theme, orch, user_id, {"preset": preset})
    except Exception:
        logger.exception("chrome theme: failed to save preset %s for %s", preset, user_id)
        return ("theme", {}, notice_block("error", "Failed to save theme. Please retry."))
    notice = (
        notice_block("success", f"{preset.capitalize()} theme saved.")
        + render_one({"type": "theme_apply", "preset": preset, "message": "Theme applied"})
    )
    return ("theme", {}, notice)


HANDLERS = {"chrome_theme_preset": _handle_theme_preset}
