"""
ROTE Capabilities — Device capability and profile models.

DeviceCapabilities: raw data reported by the frontend on connection.
DeviceProfile: derived rendering constraints used by the adapter.

Declarative per-target host-config: the per-device-type rendering constraints
are data, not code — a single ``_BASE_HOST_CONFIG`` dict that an operator can
tune (or extend with a new target) via the ``ROTE_HOST_CONFIG`` env var (a JSON
object of partial per-type overrides), with no code change. Two of the fields —
``max_actions`` and ``supports_interactivity`` — let a host bound what a
(potentially compromised) agent may render on a given surface; both default to
unlimited / interactive, so the mechanism is opt-in.
"""
import json
import logging
import os
from enum import Enum
from dataclasses import dataclass, asdict
from typing import Any, Dict, FrozenSet, Optional

logger = logging.getLogger("rote.capabilities")

_VOICE_PERMISSION_STATES = frozenset(
    {"not_determined", "authorized", "denied", "restricted"}
)
_VOICE_TRANSPORTS = frozenset({"livekit", "watch_pcm_websocket"})


class DeviceType(str, Enum):
    BROWSER = "browser"  # Full desktop browser
    WINDOWS = "windows"  # Native Windows desktop app (renders structured components natively)
    ANDROID = "android"  # Native Android app (phone/tablet/foldable; renders structured components natively)
    IOS = "ios"  # Native iOS/iPadOS app (renders structured components natively; 051)
    MACOS = "macos"  # Native macOS desktop app (renders structured components natively; 051)
    TABLET = "tablet"  # iPad / Android tablet (~768-1024px)
    MOBILE = "mobile"  # Phone (<=480px viewport)
    WATCH = "watch"  # Smartwatch (<=200px viewport, or explicit)
    TV = "tv"  # Smart TV (large screen, read-only)
    VOICE = "voice"  # Audio-only, no screen


# Declarative host-config. Keys mirror the DeviceProfile rendering fields;
# values are the per-device defaults. `max_actions` 0 = unlimited;
# `supports_interactivity` False means the surface is read-only (interactive
# buttons are stripped).
_BASE_HOST_CONFIG: Dict[str, dict] = {
    "browser": dict(max_grid_columns=6, supports_charts=True, supports_tables=True,
                    supports_code=True, supports_file_io=True, supports_tabs=True,
                    max_text_chars=0, max_table_rows=0, max_table_cols=0,
                    max_actions=0, supports_interactivity=True),
    # Native Windows desktop: full-capability surface like a browser, but it
    # renders structured components with native widgets (not HTML) — it reports
    # a `supported_types` set so ROTE substitutes the web-only primitives
    # (e.g. plotly_chart) it can't draw natively.
    "windows": dict(max_grid_columns=6, supports_charts=True, supports_tables=True,
                    supports_code=True, supports_file_io=True, supports_tabs=True,
                    max_text_chars=0, max_table_rows=0, max_table_cols=0,
                    max_actions=0, supports_interactivity=True),
    # Native Android app (phone/tablet/foldable): a full-capability native surface
    # like `windows`. It renders structured components with native Compose widgets
    # (not HTML) and reports a `supported_types` set so ROTE substitutes only the
    # primitives it can't draw natively. The client does its OWN responsive layout
    # (WindowSizeClass), so ROTE applies content substitution here — NOT the
    # web-oriented mobile/tablet density limits (which would, e.g., strip code on
    # a phone). Operators can still tune it via the ROTE_HOST_CONFIG env override.
    "android": dict(max_grid_columns=6, supports_charts=True, supports_tables=True,
                    supports_code=True, supports_file_io=True, supports_tabs=True,
                    max_text_chars=0, max_table_rows=0, max_table_cols=0,
                    max_actions=0, supports_interactivity=True),
    # Native Apple clients (051): full-capability native surfaces exactly like
    # `windows`/`android` — the client owns its own responsive layout (iPhone/
    # iPad size classes; AppKit windows), so ROTE applies `supported_types`
    # substitution, not web density limits. The watch target is NOT here on
    # purpose: watchOS registers the existing `watch` profile, which is the
    # degradation authority for the wearable.
    "ios":     dict(max_grid_columns=6, supports_charts=True, supports_tables=True,
                    supports_code=True, supports_file_io=True, supports_tabs=True,
                    max_text_chars=0, max_table_rows=0, max_table_cols=0,
                    max_actions=0, supports_interactivity=True),
    "macos":   dict(max_grid_columns=6, supports_charts=True, supports_tables=True,
                    supports_code=True, supports_file_io=True, supports_tabs=True,
                    max_text_chars=0, max_table_rows=0, max_table_cols=0,
                    max_actions=0, supports_interactivity=True),
    "tablet":  dict(max_grid_columns=3, supports_charts=True, supports_tables=True,
                    supports_code=True, supports_file_io=True, supports_tabs=True,
                    max_text_chars=0, max_table_rows=0, max_table_cols=6,
                    max_actions=0, supports_interactivity=True),
    "mobile":  dict(max_grid_columns=1, supports_charts=True, supports_tables=True,
                    supports_code=False, supports_file_io=True, supports_tabs=True,
                    max_text_chars=0, max_table_rows=20, max_table_cols=4,
                    max_actions=0, supports_interactivity=True),
    "watch":   dict(max_grid_columns=1, supports_charts=False, supports_tables=False,
                    supports_code=False, supports_file_io=False, supports_tabs=False,
                    max_text_chars=120, max_table_rows=3, max_table_cols=2,
                    max_actions=0, supports_interactivity=True),
    "tv":      dict(max_grid_columns=4, supports_charts=True, supports_tables=True,
                    supports_code=True, supports_file_io=False, supports_tabs=True,
                    max_text_chars=0, max_table_rows=0, max_table_cols=0,
                    max_actions=0, supports_interactivity=True),
    "voice":   dict(max_grid_columns=1, supports_charts=False, supports_tables=False,
                    supports_code=False, supports_file_io=False, supports_tabs=False,
                    max_text_chars=300, max_table_rows=0, max_table_cols=0,
                    max_actions=0, supports_interactivity=False),
}

# The DeviceProfile fields that the host-config supplies (everything except the
# identity pair device_type/capabilities). Used to validate env overrides.
_HOST_CONFIG_FIELDS = frozenset(_BASE_HOST_CONFIG["browser"].keys())


def load_host_config() -> Dict[str, dict]:
    """Return the effective per-device-type host-config: the base defaults with
    any ``ROTE_HOST_CONFIG`` env overrides merged in (partial, per-type). An
    unparseable or out-of-shape override is ignored (fail-safe to defaults) and
    only whitelisted fields are honored, so the env can never inject arbitrary
    keys into DeviceProfile."""
    merged = {k: dict(v) for k, v in _BASE_HOST_CONFIG.items()}
    raw = os.getenv("ROTE_HOST_CONFIG")
    if not raw:
        return merged
    try:
        overrides = json.loads(raw)
        if not isinstance(overrides, dict):
            raise ValueError("ROTE_HOST_CONFIG must be a JSON object")
    except (ValueError, TypeError) as exc:
        logger.warning("ROTE_HOST_CONFIG ignored (%s); using defaults", exc)
        return merged
    for dtype, fields in overrides.items():
        if dtype not in merged or not isinstance(fields, dict):
            continue
        for key, value in fields.items():
            if key in _HOST_CONFIG_FIELDS:
                merged[dtype][key] = value
    return merged


@dataclass
class DeviceCapabilities:
    """Raw capabilities as reported by the frontend in register_ui."""
    device_type: str = "browser"
    screen_width: int = 1920
    screen_height: int = 1080
    viewport_width: int = 1920
    viewport_height: int = 1080
    pixel_ratio: float = 1.0
    has_touch: bool = False
    has_geolocation: bool = False
    has_microphone: bool = False
    has_audio_output: bool = False
    microphone_permission: str = "not_determined"
    full_duplex: bool = False
    voice_transport: str = ""
    has_camera: bool = False
    has_file_system: bool = True
    connection_type: str = "unknown"  # wifi, 4g, 3g, 2g, slow-2g
    user_agent: str = ""
    # 066 additive envelope fields (older clients omit them; defaults are the
    # least-restrictive interpretation so behavior is unchanged when absent).
    reduced_motion: bool = False
    pointer_type: str = "fine"  # fine | coarse


@dataclass
class DeviceProfile:
    """Derived rendering profile used to drive component adaptation."""
    device_type: DeviceType
    capabilities: DeviceCapabilities
    # Rendering constraints
    max_grid_columns: int   # Maximum columns in a grid layout
    supports_charts: bool   # Bar/line/pie/plotly charts
    supports_tables: bool   # Table component
    supports_code: bool     # Code blocks
    supports_file_io: bool  # file_upload / file_download
    supports_tabs: bool     # Tabs component
    max_text_chars: int     # Max text length before truncation; 0 = unlimited
    max_table_rows: int     # Max rows to keep in tables; 0 = unlimited
    max_table_cols: int     # Max columns to keep in tables; 0 = unlimited
    # Host bounds — default to unbounded/interactive:
    max_actions: int = 0            # Max action-buttons per surface; 0 = unlimited
    supports_interactivity: bool = True  # False = read-only surface (buttons stripped)
    # Capability negotiation — the primitive types this target can render. None =
    # render everything (no substitution); a set engages the fallback ladder for
    # any type outside it.
    supported_types: Optional[FrozenSet[str]] = None

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "DeviceProfile":
        """Build a DeviceProfile from a raw dict (from the frontend).

        An optional ``supported_types`` list (the client's capability-negotiated
        renderable set) is carried onto the profile."""
        valid_keys = DeviceCapabilities.__dataclass_fields__.keys()
        normalized = {k: v for k, v in data.items() if k in valid_keys}
        voice = data.get("voice")
        voice = voice if isinstance(voice, dict) else {}
        for name in (
            "has_microphone",
            "has_audio_output",
            "microphone_permission",
            "full_duplex",
        ):
            if name not in normalized and name in voice:
                normalized[name] = voice[name]

        # Web and older Windows registration payloads used ``transport`` while
        # native protocol descriptors use the unambiguous ``voice_transport``.
        # Normalize the alias from declared capability data only; client type or
        # user-agent identity never participates in the decision.
        transport = normalized.get(
            "voice_transport",
            data.get(
                "transport",
                voice.get("voice_transport", voice.get("transport")),
            ),
        )
        normalized["voice_transport"] = (
            transport if transport in _VOICE_TRANSPORTS else ""
        )
        permission = normalized.get("microphone_permission")
        normalized["microphone_permission"] = (
            permission if permission in _VOICE_PERMISSION_STATES else "not_determined"
        )
        for name in ("has_microphone", "has_audio_output", "full_duplex"):
            value = normalized.get(name)
            normalized[name] = value if isinstance(value, bool) else False

        caps = DeviceCapabilities(**normalized)
        profile = DeviceProfile._derive(caps)
        st = data.get("supported_types")
        if isinstance(st, (list, tuple, set, frozenset)):
            cleaned = frozenset(str(t).strip().lower() for t in st if str(t).strip())
            if cleaned:
                profile.supported_types = cleaned
        return profile

    @staticmethod
    def default() -> "DeviceProfile":
        """Default full-browser profile (no adaptation)."""
        return DeviceProfile._derive(DeviceCapabilities())

    @staticmethod
    def _derive(caps: DeviceCapabilities) -> "DeviceProfile":
        """Derive the profile from capabilities, including size-based overrides."""
        raw = caps.device_type
        dt = DeviceType(raw) if raw in DeviceType._value2member_map_ else DeviceType.BROWSER

        # Override based on viewport size when the frontend reports "browser"
        vw = caps.viewport_width or caps.screen_width
        if dt == DeviceType.BROWSER:
            if vw <= 200:
                dt = DeviceType.WATCH
            elif vw <= 480:
                dt = DeviceType.MOBILE
            elif vw <= 1024:
                dt = DeviceType.TABLET

        # Constraints come from the declarative host-config (base defaults +
        # ROTE_HOST_CONFIG env overrides) instead of being hard-coded here.
        host_config = load_host_config()
        fields = host_config.get(dt.value, host_config[DeviceType.BROWSER.value])
        return DeviceProfile(device_type=dt, capabilities=caps, **fields)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["device_type"] = self.device_type.value
        # supported_types is a frozenset for fast membership checks, but the
        # profile is JSON-serialized into rote_config — emit it as a sorted list.
        if d.get("supported_types") is not None:
            d["supported_types"] = sorted(d["supported_types"])
        return d
