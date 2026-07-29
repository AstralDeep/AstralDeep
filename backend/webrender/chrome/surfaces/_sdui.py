"""Feature 043 — helpers to compose a settings surface as ``astralprims``
component dicts for native SDUI delivery (the ``components()`` surface path).

Each helper returns a plain ``.to_dict()``-shaped dict built from the real
``astralprims`` classes, so the orchestrator renders + ROTE-adapts them and the
native clients (Windows ``renderer.py`` / Android ``render/Renderer.kt``) render
them with the SAME component renderer they use for the chat canvas. Actions bind
to the SAME ``chrome_*`` keys the web HTML uses, so surface ``HANDLERS`` are
unchanged (research D2, contracts/chrome-surface.md).

Interactive settings forms use the ``ParamPicker`` **action-submit** mode: the
extra ``submit_action`` / ``submit_payload`` attributes ride through
``Primitive.attributes`` (merged at the top level by ``to_dict()``), so on submit
the client posts ``ui_event{action: submit_action, payload:{fields, ...}}``
instead of a chat message. The installed ``astralprims`` wheel need not define
these yet — the renderers honor the emitted keys (feature-029 precedent).
"""
from typing import Any, Dict, List, Optional

from astralprims import (
    Alert,
    Badge,
    Button,
    Card,
    Container,
    KeyValue,
    List_,
    ParamPicker,
    Tabs,
    Text,
)


def text(content: str, variant: str = "body") -> Dict[str, Any]:
    """A run of text (variant: h1|h2|h3|body|caption)."""
    return Text(content=content, variant=variant).to_dict()


def card(title: str, content: List[Dict[str, Any]], variant: str = "default") -> Dict[str, Any]:
    """A titled card wrapping child component dicts."""
    return Card(title=title, content=list(content), variant=variant).to_dict()


def container(children: List[Dict[str, Any]], direction: Optional[str] = None) -> Dict[str, Any]:
    """A layout container holding child component dicts."""
    return Container(children=list(children), direction=direction).to_dict()


def button(label: str, action: str, payload: Optional[Dict[str, Any]] = None,
           variant: str = "secondary") -> Dict[str, Any]:
    """A button that dispatches ``action`` with ``payload`` over ``ui_event``.

    Used for per-row actions/toggles (``chrome_skill_toggle``,
    ``chrome_job_pause``, …), theme presets (``chrome_theme_preset``), and
    TOC/nav (``chrome_open``).
    """
    return Button(label=label, action=action, payload=payload or {}, variant=variant).to_dict()


def badge(label: str, variant: str = "default") -> Dict[str, Any]:
    """A small inline status chip (default|success|warning|error|info|accent)."""
    return Badge(label=label, variant=variant).to_dict()


def alert(message: str, kind: str = "info", title: Optional[str] = None) -> Dict[str, Any]:
    """A callout/banner (kind: info|success|warning|error)."""
    variant = kind if kind in ("info", "success", "warning", "error") else "info"
    return Alert(message=message, variant=variant, title=title).to_dict()


def key_value(items: List[Dict[str, Any]], title: Optional[str] = None,
              columns: int = 2) -> Dict[str, Any]:
    """A compact label/value fact sheet (each item ``{label, value, hint?}``)."""
    return KeyValue(items=list(items), title=title, columns=columns).to_dict()


def bullet_list(items: List[Any], ordered: bool = False) -> Dict[str, Any]:
    """An ordered/unordered list of strings or ``{...}`` item dicts."""
    return List_(items=list(items), ordered=ordered).to_dict()


def tabs(tab_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A tabbed container. Each ``tab_items`` entry is
    ``{"label": str, "value": str, "content": [component dicts]}``."""
    return Tabs(tabs=list(tab_items)).to_dict()


def field(name: str, label: str, kind: str = "text", default: Any = None,
          options: Optional[List[Any]] = None, help: Optional[str] = None,
          step: Optional[float] = None,
          visible_when: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One ``ParamPicker`` field.

    ``kind`` is text|password|textarea|number|boolean|select|checklist. The
    ``password``/``textarea`` kinds are the feature-043 additions (research D2).

    ``visible_when`` (063, additive) is ``{"field": <controller name>,
    "equals": <value>, "default": <controller's default>}`` — capable clients
    hide the field unless the controller's current value matches; clients that
    predate the attribute ignore it and render every field, so the submit
    handler must keep tolerating values from hidden fields.
    """
    f: Dict[str, Any] = {"name": name, "label": label, "kind": kind}
    if default is not None:
        f["default"] = default
    if options is not None:
        f["options"] = options
    if help is not None:
        f["help"] = help
    if step is not None:
        f["step"] = step
    if visible_when is not None:
        f["visible_when"] = visible_when
    return f


def form(fields: List[Dict[str, Any]], submit_action: Optional[str] = None,
         submit_label: str = "Save", submit_payload: Optional[Dict[str, Any]] = None,
         actions: Optional[List[Dict[str, Any]]] = None, title: str = "",
         description: str = "") -> Dict[str, Any]:
    """A ``ParamPicker`` in **action-submit** mode (research D2).

    Single-action form: pass ``submit_action`` — submit posts
    ``ui_event{action: submit_action, payload:{fields:{...}, ...submit_payload}}``.
    Multi-action form (e.g. LLM's Load / Test / Save): pass ``actions`` as
    ``[{"label", "action", "variant"?, "payload"?}]`` — the client renders each
    as a button that submits the SAME collected ``fields`` with that action.
    Either way ``payload.fields`` is the shape the existing ``chrome_*`` handlers
    already parse.
    """
    attrs: Dict[str, Any] = {}
    if actions:
        attrs["actions"] = [dict(a) for a in actions]
    if submit_action:
        attrs["submit_action"] = submit_action
        attrs["submit_payload"] = submit_payload or {}
    return ParamPicker(
        title=title,
        description=description,
        fields=list(fields),
        submit_label=submit_label,
        attributes=attrs,
    ).to_dict()


def placeholder(label: str) -> Dict[str, Any]:
    """The single labeled placeholder for a surface not yet converted to SDUI
    (FR-014 graceful degradation — never a blank/crashed screen)."""
    return Alert(
        message=f"“{label}” isn't available in this app yet.",
        variant="info",
    ).to_dict()
