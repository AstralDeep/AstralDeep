"""Theme tokens mirroring the AstralDeep web palette (backend/webrender/static/
astral.css `:root`) so the native client reads as the same product:
indigo→purple accent, #0F1221 bg with corner glows, layered translucent
surfaces, soft white borders, Inter type.

Feature 044 (US5) — the palette is now **mutable** and the stylesheet
**rebuildable** so a chosen theme applies live. ``apply_theme(theme)`` mutates
the active :data:`PALETTE` from a named preset / a ``colors`` map / a single
``color_key``+``color_value`` (the shapes the backend Theme surface emits) and
``build_stylesheet()`` re-renders the QSS from the current palette. The five
named presets mirror ``backend/webrender/chrome/surfaces/theme.py`` PRESETS.
"""
from __future__ import annotations

import re

# Named presets (the seven server theme channels each). Values are the EXACT
# copies from backend/webrender/chrome/surfaces/theme.py PRESETS so a preset
# picked on any client renders identically.
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

#: The active theme channels (mutated in place by :func:`apply_theme`).
PALETTE: dict = dict(PRESETS["midnight"])

FONT = "'Inter', 'Segoe UI', system-ui, sans-serif"
MONO = "'JetBrains Mono', 'Cascadia Code', Consolas, monospace"

# Fixed semantic status colors (info/success/warning/error do not theme; the
# palette-tied accent/default entries are recomputed in _derive()).
_SEMANTIC = {
    "info": ("#3B82F6", "rgba(59,130,246,0.14)"),
    "success": ("#22C55E", "rgba(34,197,94,0.14)"),
    "warning": ("#EAB308", "rgba(234,179,8,0.14)"),
    "error": ("#EF4444", "rgba(239,68,68,0.14)"),
}

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")

# Style tokens derived from PALETTE by _derive() (declared here so they are
# always defined module-level names — the values below are placeholders).
BG = SURFACE = SURFACE_2 = BORDER = TEXT = MUTED = ""
PRIMARY = SECONDARY = ACCENT = PRIMARY_SOFT = GRAD = ""
VARIANT_COLORS: dict = {}
_ROOT_BG = ""
ROOT_BG_STYLE = ""


def _hex_to_rgb(value) -> tuple:
    s = str(value or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except (ValueError, IndexError):
        return (0, 0, 0)


def _rgba(value, alpha) -> str:
    r, g, b = _hex_to_rgb(value)
    return f"rgba({r},{g},{b},{alpha})"


def _mix(a: str, b: str, t: float) -> str:
    ra, ga, ba = _hex_to_rgb(a)
    rb, gb, bb = _hex_to_rgb(b)
    return "#{:02X}{:02X}{:02X}".format(
        round(ra + (rb - ra) * t), round(ga + (gb - ga) * t), round(ba + (bb - ba) * t)
    )


def _normalize_hex(value) -> str:
    """Return ``#RRGGBB`` for a valid 6-digit hex string, else ``""``."""
    s = str(value or "").strip()
    if not _HEX_RE.match(s):
        return ""
    return s if s.startswith("#") else "#" + s


def _derive() -> None:
    """(Re)compute the module-level style tokens from the active PALETTE."""
    global BG, SURFACE, SURFACE_2, BORDER, TEXT, MUTED
    global PRIMARY, SECONDARY, ACCENT, PRIMARY_SOFT, GRAD, VARIANT_COLORS
    global _ROOT_BG, ROOT_BG_STYLE
    bg = PALETTE["bg"]
    surface = PALETTE["surface"]
    primary = PALETTE["primary"]
    secondary = PALETTE["secondary"]
    text = PALETTE["text"]
    muted = PALETTE["muted"]
    accent = PALETTE["accent"]
    BG = bg
    # The palette carries one solid raised surface; the layered surface-1 tone
    # sits halfway between the bg and that raised surface (works dark AND light).
    SURFACE = _mix(bg, surface, 0.5)
    SURFACE_2 = surface
    # A soft border derived from the text color so it reads on light themes too.
    BORDER = _rgba(text, 0.10)
    TEXT = text
    MUTED = muted
    PRIMARY = primary
    SECONDARY = secondary
    ACCENT = accent
    PRIMARY_SOFT = _rgba(primary, 0.15)
    # 135° primary→secondary accent gradient (hero / primary button).
    GRAD = (f"qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            f"stop:0 {primary}, stop:1 {secondary})")
    VARIANT_COLORS = {
        **_SEMANTIC,
        "accent": (accent, _rgba(accent, 0.14)),
        "default": (primary, _rgba(primary, 0.14)),
    }
    # Root background carries the same corner radial glows as the web body.
    _ROOT_BG = (f"qradialgradient(cx:0.85, cy:-0.05, radius:0.9, "
                f"stop:0 {_rgba(secondary, 0.10)}, stop:0.6 transparent), "
                f"qradialgradient(cx:-0.05, cy:1.05, radius:0.9, "
                f"stop:0 {_rgba(primary, 0.08)}, stop:0.55 transparent), {bg}")
    ROOT_BG_STYLE = f"QWidget#root {{ background: {_ROOT_BG}; }}"


def build_stylesheet() -> str:
    """Render the application QSS from the current (mutable) palette. Called at
    import for :data:`APP_STYLESHEET` and again by the app on every live theme
    change (feature 044 US5).

    The treatments mirror ``backend/webrender/static/astral.css`` (the web
    reference, feature 066 style parity): radius-sm 6 / radius-md 10 with
    rounded-lg (8px) buttons+fields, translucent text-channel surfaces instead
    of solid grays, the primary→secondary accent gradient on primary actions,
    and the same secondary / ghost / tint button families the web renderer
    emits."""
    return f"""
* {{ font-family: {FONT}; }}
QWidget {{ background: transparent; color: {TEXT}; font-size: 14px; }}
QLabel {{ background: transparent; border: none; }}
QToolTip {{ background: {SURFACE_2}; color: {TEXT}; border: 1px solid {BORDER};
           padding: 4px 8px; }}
QLabel#voiceRequestTerminalNotice {{
    background: {_SEMANTIC["error"][1]};
    border: 2px solid {_SEMANTIC["error"][0]};
    border-radius: 8px;
    padding: 8px 10px;
    color: {TEXT};
    font-weight: 700;
}}
QLabel#voiceRequestTerminalNotice[noticeKind="speech_error"] {{
    background: {_SEMANTIC["warning"][1]};
    border-color: {_SEMANTIC["warning"][0]};
}}
QMainWindow, QWidget#root {{ background: {BG}; }}
QScrollArea {{ background: transparent; border: none; }}
QLineEdit, QPlainTextEdit, QTextEdit {{ background: {_rgba(TEXT, 0.05)}; border: 1px solid {_rgba(TEXT, 0.10)};
           border-radius: 8px; padding: 8px 12px; color: {TEXT};
           selection-background-color: {PRIMARY}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{
           border: 1px solid {_rgba(PRIMARY, 0.6)};
           background: {_rgba(TEXT, 0.08)}; }}
QComboBox {{ background: {_rgba(TEXT, 0.05)}; border: 1px solid {_rgba(TEXT, 0.10)};
           border-radius: 8px; padding: 6px 10px; color: {TEXT}; }}
QComboBox:hover {{ border-color: {_rgba(PRIMARY, 0.6)}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {SURFACE_2}; color: {TEXT};
           border: 1px solid {BORDER}; border-radius: 6px; padding: 2px;
           selection-background-color: {_rgba(PRIMARY, 0.25)};
           selection-color: {TEXT}; outline: none; }}
QCheckBox {{ color: {TEXT}; spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px;
           border: 1px solid {_rgba(TEXT, 0.20)}; background: {_rgba(TEXT, 0.10)}; }}
QCheckBox::indicator:checked {{ background: {PRIMARY}; border-color: {PRIMARY}; }}
/* Neutral outline button — the web pager/utility treatment (border-white/10,
   hover bg-white/10). Named variants below override it. */
QPushButton {{ background: {_rgba(TEXT, 0.04)}; border: 1px solid {_rgba(TEXT, 0.12)};
           border-radius: 8px; padding: 8px 16px; color: {TEXT}; }}
QPushButton:hover {{ border-color: {_rgba(PRIMARY, 0.65)}; background: {_rgba(TEXT, 0.08)}; }}
QPushButton:disabled {{ color: {_rgba(MUTED, 0.6)}; border-color: {_rgba(TEXT, 0.07)}; }}
QPushButton#primary {{ background: {GRAD}; border: none; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: {SECONDARY}; }}
/* Web .astral-btn-secondary — outline with the primary-tinted border. */
QPushButton#secondary {{ background: transparent; border: 1px solid {_rgba(PRIMARY, 0.40)};
           color: {TEXT}; }}
QPushButton#secondary:hover {{ background: {_rgba(PRIMARY, 0.10)}; }}
/* Web .astral-btn-ghost — text-only, muted until hover. */
QPushButton#ghost {{ background: transparent; border: none; color: {MUTED}; }}
QPushButton#ghost:hover {{ background: {_rgba(TEXT, 0.05)}; color: {TEXT}; }}
/* Web file-upload / file-download soft tint buttons. */
QPushButton#tintPrimary {{ background: {_rgba(PRIMARY, 0.20)};
           border: 1px solid {_rgba(PRIMARY, 0.30)}; color: {PRIMARY}; font-weight: 500; }}
QPushButton#tintPrimary:hover {{ background: {_rgba(PRIMARY, 0.30)}; }}
QPushButton#tintSecondary {{ background: {_rgba(SECONDARY, 0.20)};
           border: 1px solid {_rgba(SECONDARY, 0.30)}; color: {SECONDARY}; font-weight: 500; }}
QPushButton#tintSecondary:hover {{ background: {_rgba(SECONDARY, 0.30)}; }}
/* Param-picker checklist chips (web .astral-pp-field checklist buttons). */
QPushButton#chip {{ background: {_rgba(TEXT, 0.05)}; border: 1px solid {_rgba(TEXT, 0.10)};
           border-radius: 6px; padding: 4px 8px; font-size: 12px; color: {MUTED}; }}
QPushButton#chip:hover {{ background: {_rgba(TEXT, 0.10)}; }}
QPushButton#chip:checked {{ background: {_rgba(PRIMARY, 0.30)}; border-color: {PRIMARY};
           color: white; }}
/* 066 cross-client style parity: the square, quiet icon button the web client
   uses for its top-bar and voice controls (.astral-voice-control — 38px, 8px
   radius, faint border, muted glyph that takes the accent on hover). Windows
   drew these as full-width text buttons, which is what made its top bar and
   composer read as a different application beside web/Android. */
QPushButton#iconGhost, QPushButton#voiceComposerControl[iconOnly="true"] {{
           min-width: 38px; max-width: 38px; min-height: 34px;
           padding: 0; font-size: 15px;
           background: {_rgba(TEXT, 0.05)}; border: 1px solid {_rgba(TEXT, 0.12)};
           border-radius: 8px; color: {MUTED}; }}
/* Settings and the paperclip open menus; Qt's dropdown arrow would eat the
   glyph inside a fixed 38px square (and neither web nor Android draws one). */
QPushButton#iconGhost::menu-indicator {{ image: none; width: 0; height: 0; }}
QPushButton#iconGhost:hover, QPushButton#voiceComposerControl[iconOnly="true"]:hover {{
           color: {TEXT}; border-color: {_rgba(PRIMARY, 0.65)};
           background: {_rgba(PRIMARY, 0.12)}; }}
QPushButton#iconGhost:disabled, QPushButton#voiceComposerControl[iconOnly="true"]:disabled {{
           color: {_rgba(MUTED, 0.5)}; border-color: {_rgba(TEXT, 0.08)}; }}
/* A pressed toggle (mic on, speech muted) reads as filled, matching the web
   control's aria-pressed styling — the state is not carried by color alone. */
QPushButton#voiceComposerControl[iconOnly="true"][pressed="true"] {{
           background: {_rgba(PRIMARY, 0.22)}; border-color: {PRIMARY}; color: {TEXT}; }}
QPushButton#danger {{ background: {_SEMANTIC["error"][0]}; border: none; color: white; font-weight: 600; }}
QPushButton#danger:hover {{ background: #DC2626; }}
QTableWidget {{ background: transparent; border: none;
           alternate-background-color: {_rgba(TEXT, 0.02)};
           selection-background-color: {_rgba(PRIMARY, 0.20)}; }}
QTableWidget::item {{ padding: 8px 12px; color: {TEXT};
           border-bottom: 1px solid {_rgba(TEXT, 0.05)}; }}
/* Web thead band: bg-astral-primary/10, uppercase muted text-xs headers. */
QHeaderView::section {{ background: {_rgba(PRIMARY, 0.10)}; color: {MUTED}; border: none;
           border-bottom: 1px solid {_rgba(TEXT, 0.05)}; padding: 9px 12px;
           font-weight: 600; font-size: 11px; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 8px 16px; border: none; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {PRIMARY}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 10px; }}
QMenu {{ background: {SURFACE_2}; color: {TEXT}; border: 1px solid {_rgba(TEXT, 0.12)};
           border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 24px; border-radius: 6px; }}
QMenu::item:selected {{ background: {_rgba(PRIMARY, 0.18)}; color: {TEXT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
QSplitter::handle {{ background: {BORDER}; width: 1px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {_rgba(PRIMARY, 0.3)}; border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {_rgba(PRIMARY, 0.55)}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {_rgba(PRIMARY, 0.3)}; border-radius: 5px; min-width: 28px; }}
QScrollBar::handle:horizontal:hover {{ background: {_rgba(PRIMARY, 0.55)}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""


def apply_theme(theme) -> bool:
    """Mutate :data:`PALETTE` from a theme spec, returning ``True`` when it
    changed (so the caller can re-apply the stylesheet).

    Accepts the shapes the backend Theme surface / ``theme_apply`` component
    emit: a preset name (a bare string OR ``{"preset": name}``), a ``{"colors":
    {channel: hex}}`` map, or a single ``{"color_key", "color_value"}`` pair.
    Unknown presets / invalid hex are ignored (no-op → ``False``)."""
    if isinstance(theme, str):
        theme = {"preset": theme}
    if not isinstance(theme, dict):
        return False
    before = dict(PALETTE)
    # Preset name first (the fallback for old servers), then an explicit
    # ``colors`` map wins per channel — the server resolves the preset to its
    # channel map and sends it alongside the name, and the resolved colors are
    # authoritative (the local preset table is only a fallback).
    preset = theme.get("preset")
    if isinstance(preset, str) and preset in PRESETS:
        PALETTE.update(PRESETS[preset])
    colors = theme.get("colors")
    if isinstance(colors, dict):
        for key in list(PALETTE.keys()):
            hexv = _normalize_hex(colors.get(key))
            if hexv:
                PALETTE[key] = hexv
    key = theme.get("color_key")
    hexv = _normalize_hex(theme.get("color_value"))
    if isinstance(key, str) and key in PALETTE and hexv:
        PALETTE[key] = hexv
    if PALETTE == before:
        return False
    _derive()
    global APP_STYLESHEET
    APP_STYLESHEET = build_stylesheet()
    return True


# Compute the derived tokens + the initial stylesheet at import.
_derive()
APP_STYLESHEET = build_stylesheet()
