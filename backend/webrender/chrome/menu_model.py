"""Feature 042 — the server-owned chrome model: the single source of truth for
the top bar + settings menu that EVERY client renders.

Constitution II/XII: the application chrome is described ONCE, here. The web
renderer (``topbar.render_topbar``) turns this model into HTML; the
``chrome_menu`` WS frame and ``GET /api/chrome/menu`` serialize the SAME model
(``ChromeModel.to_dict``) for the native Windows/Android clients (and any future
client, e.g. iOS). There is no second menu definition anywhere — a client is a
thin consumer of this model, never a parallel reimplementation.

The model is role-filtered and feature-flag-resolved BEFORE serialization, so a
client renders exactly what it receives and never sees an item it must not (the
admin group is simply absent for non-admins). Server-side authorization
(``chrome_events`` + surface ``ADMIN_ONLY``) stays authoritative regardless of
what any client displays.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Bumped when the wire shape changes; clients ignore unknown fields and degrade
# gracefully rather than fail (data-model.md forward-compat rule).
MODEL_VERSION = 1


@dataclass(frozen=True)
class SurfaceRef:
    """A reference to a settings surface opened via the ``chrome_open`` action."""

    surface: str
    params: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {"surface": self.surface, "params": dict(self.params)}


@dataclass(frozen=True)
class TopBarControl:
    """One control in the top bar. ``kind`` is one of brand|status|action|menu.

    ``brand``/``status`` are non-interactive; ``action`` opens ``action``'s
    surface via ``chrome_open``; ``menu`` (the gear) toggles the client's local
    settings dropdown (no server round-trip).
    """

    key: str
    kind: str
    label: Optional[str] = None
    icon: Optional[str] = None  # semantic id (gear|history|sparkle); clients map to their own asset
    action: Optional[SurfaceRef] = None

    def to_dict(self) -> Dict:
        d: Dict = {"key": self.key, "kind": self.kind}
        if self.label is not None:
            d["label"] = self.label
        if self.icon is not None:
            d["icon"] = self.icon
        if self.action is not None:
            d["action"] = self.action.to_dict()
        return d


@dataclass(frozen=True)
class MenuItem:
    """One selectable Settings entry."""

    key: str
    label: str
    surface: str
    params: Dict = field(default_factory=dict)
    admin_only: bool = False

    def to_dict(self) -> Dict:
        return {
            "key": self.key,
            "label": self.label,
            "surface": self.surface,
            "params": dict(self.params),
            "admin_only": self.admin_only,
        }


@dataclass(frozen=True)
class MenuGroup:
    """A labeled, ordered group of items (rendered heading + items)."""

    key: str
    label: str
    items: Tuple[MenuItem, ...]
    admin_only: bool = False

    def to_dict(self) -> Dict:
        return {
            "key": self.key,
            "label": self.label,
            "admin_only": self.admin_only,
            "items": [i.to_dict() for i in self.items],
        }


@dataclass(frozen=True)
class SignOutItem:
    """The always-last, visually-distinct (red) sign-out entry.

    ``action="logout"`` — clients perform a real server logout then return to
    the sign-in entry point (web: ``GET /auth/logout``; native: the equivalent
    logout round-trip).
    """

    key: str = "signout"
    label: str = "Sign out"
    style: str = "danger"
    action: str = "logout"

    def to_dict(self) -> Dict:
        return {"key": self.key, "label": self.label, "style": self.style, "action": self.action}


@dataclass(frozen=True)
class ChromeModel:
    """The complete chrome description a client needs to render."""

    topbar: Tuple[TopBarControl, ...]
    menu: Tuple[MenuGroup, ...]
    signout: SignOutItem
    version: int = MODEL_VERSION

    def to_dict(self) -> Dict:
        return {
            "version": self.version,
            "topbar": [c.to_dict() for c in self.topbar],
            "menu": [g.to_dict() for g in self.menu],
            "signout": self.signout.to_dict(),
        }


# ---------------------------------------------------------------------------
# The ONE canonical inventory. Order here IS the order on every client. These
# are the exact labels/surfaces the web has shipped (topbar._menu_entries), now
# promoted to the single source of truth all clients consume.
# ---------------------------------------------------------------------------
_ACCOUNT_ITEMS: Tuple[MenuItem, ...] = (
    MenuItem("agents", "Agents & permissions", "agents"),
    MenuItem("llm", "LLM settings", "llm"),
    MenuItem("personalization", "Personalization", "personalization"),
    MenuItem("audit", "Audit log", "audit"),
    MenuItem("theme", "Theme", "theme"),
)
# Feature 058 — the ONLY affordance that opens BYO authoring. Flag-gated
# (FF_BYO_AGENTS, default OFF) exactly like Pulse: with the flag off the item is
# absent from every client's menu, and the surface + its handlers refuse anyway
# (defence in depth — a menu is a hint, never an authorization).
_BYO_AGENTS_ITEM = MenuItem("my-agents", "My agents", "agent_authoring")
# Feature 063 — the ONLY affordance that opens the Remote machines inventory.
# Flag-gated (FF_REMOTE_COMPUTE, default OFF) like "My agents": absent from every
# client's menu when off. Per-user (not admin), so admin_only stays False.
_REMOTE_MACHINES_ITEM = MenuItem("remote-machines", "Remote machines", "remote_machines")
_HELP_ITEMS: Tuple[MenuItem, ...] = (
    MenuItem("tour", "Take the tour", "tour"),
    MenuItem("guide", "User guide", "guide"),
)
_ADMIN_ITEMS: Tuple[MenuItem, ...] = (
    MenuItem("tool-quality", "Tool quality", "admin_tools", {"tab": "quality"}, admin_only=True),
    MenuItem("tutorial-admin", "Tutorial admin", "admin_tools", {"tab": "tutorial"}, admin_only=True),
    # Feature 054: the deployment-wide System LLM credential for background
    # work — a declared web-only admin carve-out (Constitution XII), like the
    # other admin tools: natives never receive the admin group.
    MenuItem("system-llm", "System LLM", "llm_system", admin_only=True),
)


def _resolve_pulse(pulse_enabled: Optional[bool]) -> bool:
    if pulse_enabled is not None:
        return bool(pulse_enabled)
    try:  # lazy import keeps this module import-cheap and easy to unit-test
        from dreaming.pulse import pulse_enabled as _pe
        return bool(_pe())
    except Exception:
        return False


def _resolve_byo(byo_enabled: Optional[bool]) -> bool:
    if byo_enabled is not None:
        return bool(byo_enabled)
    try:
        from shared.feature_flags import flags
        return bool(flags.is_enabled("byo_agents"))
    except Exception:
        return False


def _resolve_remote_compute(remote_enabled: Optional[bool]) -> bool:
    if remote_enabled is not None:
        return bool(remote_enabled)
    try:
        from shared.feature_flags import flags
        return bool(flags.is_enabled("remote_compute"))
    except Exception:
        return False


def build_menu_model(
    roles: Optional[List[str]] = None,
    *,
    pulse_enabled: Optional[bool] = None,
    byo_enabled: Optional[bool] = None,
    remote_enabled: Optional[bool] = None,
    include_admin: bool = True,
    include_tour: bool = True,
) -> ChromeModel:
    """Build the role-filtered, flag-resolved chrome model.

    Args:
        roles: the session's verified roles. ``"admin"`` unlocks the ADMIN TOOLS
            group. Anything falsy ⇒ no admin group.
        pulse_enabled: override for the Pulse control's presence (tests). When
            ``None``, resolved from ``FF_PULSE_DIGEST`` via ``dreaming.pulse``.
        byo_enabled: override for the "My agents" (BYO authoring) item's presence
            (tests). When ``None``, resolved from ``FF_BYO_AGENTS``.
        include_admin: whether the ADMIN TOOLS group is eligible at all. The web
            passes ``True`` (admins see it). Native clients (Windows/Android)
            pass ``False`` — admin settings are web-only, so the group is omitted
            even for admins (the ``chrome_menu`` frame / REST never send it).
            Server-side ``ADMIN_ONLY`` enforcement on ``chrome_open`` stays
            authoritative regardless.

    Returns:
        A :class:`ChromeModel` ready to render (web) or serialize (native).
    """
    roles = roles or []
    is_admin = "admin" in roles and include_admin
    show_pulse = _resolve_pulse(pulse_enabled)
    show_byo = _resolve_byo(byo_enabled)
    show_remote = _resolve_remote_compute(remote_enabled)

    topbar: List[TopBarControl] = [
        TopBarControl("brand", "brand"),
        TopBarControl("status", "status"),
    ]
    if show_pulse:
        topbar.append(
            TopBarControl("pulse", "action", label="Pulse digest", icon="sparkle",
                          action=SurfaceRef("pulse"))
        )
    topbar.append(
        TopBarControl("timeline", "action", label="Workspace timeline", icon="history",
                      action=SurfaceRef("workspace_timeline"))
    )
    topbar.append(TopBarControl("settings", "menu", label="Settings", icon="gear"))

    # Feature 043: "Take the tour" is a web-only capability (a web-DOM-anchored
    # walkthrough with no native analog). The native channels pass
    # include_tour=False so the item is omitted server-side — exactly like the
    # admin group below (Constitution XII v2.3.1 deliberate web-only carve-out).
    help_items = _HELP_ITEMS if include_tour else tuple(
        i for i in _HELP_ITEMS if i.surface != "tour"
    )
    account_items = (_ACCOUNT_ITEMS
                     + ((_BYO_AGENTS_ITEM,) if show_byo else ())
                     + ((_REMOTE_MACHINES_ITEM,) if show_remote else ()))
    groups: List[MenuGroup] = [
        MenuGroup("account", "Account", account_items),
        MenuGroup("help", "Help", help_items),
    ]
    if is_admin:
        groups.append(MenuGroup("admin", "Admin tools", _ADMIN_ITEMS, admin_only=True))

    return ChromeModel(topbar=tuple(topbar), menu=tuple(groups), signout=SignOutItem())


def menu_model_dict(
    roles: Optional[List[str]] = None,
    *,
    pulse_enabled: Optional[bool] = None,
    byo_enabled: Optional[bool] = None,
    remote_enabled: Optional[bool] = None,
    include_admin: bool = True,
    include_tour: bool = True,
) -> Dict:
    """Convenience: ``build_menu_model(...).to_dict()`` for the REST/WS channels.

    The native channels (``GET /api/chrome/menu`` + the ``chrome_menu`` WS frame)
    pass ``include_admin=False`` (ADMIN TOOLS is web-only) and
    ``include_tour=False`` (feature 043 — "Take the tour" is web-only).
    """
    return build_menu_model(
        roles,
        pulse_enabled=pulse_enabled,
        byo_enabled=byo_enabled,
        remote_enabled=remote_enabled,
        include_admin=include_admin,
        include_tour=include_tour,
    ).to_dict()
