"""Native renderer: AstralDeep SDUI component dicts -> PySide6 widgets.

This is a real ROTE/webrender *target* — it consumes the same structured
`components` that the orchestrator puts on every `ui_render`/`ui_upsert` (the
non-web wire layer, FR-018) and draws native Qt widgets instead of HTML. The web
renderer (backend/webrender/renderer.py) is the reference for field shapes.

Public API:
    render(component: dict, ctx: RenderContext) -> QWidget

`ctx.emit(action, payload)` is called for interactive components (buttons,
history rows, param-picker / table-pagination submits) so the app can post a
`ui_event` back to the orchestrator.
"""

from __future__ import annotations

import base64
import json
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QPlainTextEdit,
    QProgressBar,
    QHeaderView,
)

from . import theme as T


@dataclass
class RenderContext:
    """Carried through a render pass. `emit` posts a ui_event back to the server;
    `download` (optional) fetches an authed backend file URL and saves it natively;
    `chat_id` (optional, kept current by the app) scopes component actions such
    as table pagination to the active conversation; `apply_theme` (optional,
    wired by the MainWindow to its `_apply_theme_pref` path) is the app's single
    theme-apply implementation — the `theme_apply` component and the color
    picker route their specs through it (feature 044 US5)."""

    emit: Callable[[str, dict], None]
    download: Optional[Callable[[str, str], None]] = None
    chat_id: Optional[str] = None
    apply_theme: Optional[Callable[[Any], None]] = None


# --------------------------------------------------------------------------- #
# small widget helpers
# --------------------------------------------------------------------------- #


def _label(
    text: str,
    *,
    color: str = T.TEXT,
    size: int = 14,
    bold: bool = False,
    markdown: bool = False,
    wrap: bool = True,
) -> QLabel:
    lab = QLabel()
    lab.setFrameShape(QFrame.Shape.NoFrame)
    lab.setTextFormat(
        Qt.TextFormat.MarkdownText if markdown else Qt.TextFormat.PlainText
    )
    lab.setText(str(text))
    lab.setWordWrap(wrap)
    lab.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextSelectableByMouse
        | Qt.TextInteractionFlag.LinksAccessibleByMouse
    )
    lab.setOpenExternalLinks(True)
    weight = "600" if bold else "400"
    lab.setStyleSheet(
        f"color:{color}; font-size:{size}px; font-weight:{weight}; background:transparent;"
    )
    return lab


def _vbox(spacing: int = 8, margins: tuple = (0, 0, 0, 0)) -> QVBoxLayout:
    lay = QVBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    return lay


_box_counter = [0]


def _scoped(widget: QWidget, css: str) -> QWidget:
    """Apply ``css`` to ``widget`` ONLY, via an object-name selector.

    A bare-property stylesheet (e.g. ``border: 1px solid``) set directly on a
    container cascades that border onto every child widget in Qt — which is why
    a card's border was bleeding onto its labels. Scoping to ``#name`` confines
    it to the container itself.
    """
    _box_counter[0] += 1
    name = f"abox{_box_counter[0]}"
    widget.setObjectName(name)
    widget.setStyleSheet(f"#{name} {{ {css} }}")
    return widget


def _card_frame(
    radius: int = 12, bg: str = T.SURFACE, border: str = T.BORDER
) -> QFrame:
    f = QFrame()
    _scoped(f, f"background:{bg}; border:1px solid {border}; border-radius:{radius}px;")
    f.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    return f


def _children(comp: dict) -> List[dict]:
    kids = comp.get("content")
    if kids is None:
        kids = comp.get("children")
    if isinstance(kids, dict):
        return [kids]
    if isinstance(kids, list):
        return [c for c in kids if isinstance(c, dict)]
    if isinstance(kids, str):
        return [{"type": "text", "content": kids}]
    return []


def _render_into(layout, items: List[dict], ctx: RenderContext) -> None:
    for child in items:
        layout.addWidget(render(child, ctx))


# --------------------------------------------------------------------------- #
# primitive renderers
# --------------------------------------------------------------------------- #

_TEXT_SIZES = {
    "h1": (26, True),
    "h2": (21, True),
    "h3": (17, True),
    "body": (14, False),
    "caption": (12, False),
}


def _r_text(c, ctx):
    variant = c.get("variant", "body")
    content = c.get("content", c.get("text", ""))
    if variant == "markdown":
        return _label(content, markdown=True)
    size, bold = _TEXT_SIZES.get(variant, (14, False))
    color = T.MUTED if variant == "caption" else T.TEXT
    return _label(content, color=color, size=size, bold=bold)


def _r_card(c, ctx):
    frame = _card_frame()
    lay = _vbox(10, (16, 14, 16, 14))
    frame.setLayout(lay)
    title = c.get("title")
    if title:
        row = QHBoxLayout()
        row.setSpacing(8)
        bar = QLabel()
        bar.setFixedSize(4, 18)
        bar.setStyleSheet(f"background:{T.PRIMARY}; border-radius:2px;")
        row.addWidget(bar)
        row.addWidget(_label(title, size=16, bold=True))
        row.addStretch(1)
        lay.addLayout(row)
    _render_into(lay, _children(c), ctx)
    return frame


def _css_of(c: dict) -> dict:
    """The component's astralprims ``css`` styling dict ({} when absent/bad)."""
    v = c.get("css")
    return v if isinstance(v, dict) else {}


def _css_px(css: dict, key: str, default: int) -> int:
    """A ``"22px"``/``"22"`` css length as int, tolerant of garbage."""
    try:
        raw = str(css.get(key, "")).strip().lower().replace("px", "")
        n = int(float(raw)) if raw else default
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def _css_flex(css: dict) -> int:
    """The css ``flex`` grow factor as a Qt stretch (0 = unset)."""
    try:
        return max(0, int(float(str(css.get("flex") or 0))))
    except (TypeError, ValueError):
        return 0


def _css_swatch(c: dict) -> Optional[QWidget]:
    """A childless css-styled container is a colored box — e.g. the Theme
    surface's preset swatch cells. The web applies the astralprims ``css``
    field as inline styles; natively we honor the same minimal subset
    (background / height / flex) so those strips are never blank. Mirrors the
    Android twin (Attrs.kt containerMode / Basic.kt SwatchBox)."""
    css = _css_of(c)
    bg = str(css.get("background") or "").strip()
    if _children(c) or not bg:
        return None
    f = QFrame()
    _scoped(f, f"background:{bg}; border-radius:3px;")
    f.setFixedHeight(_css_px(css, "height", 22))
    f.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return f


def _r_container(c, ctx):
    swatch = _css_swatch(c)
    if swatch is not None:
        return swatch
    w = QWidget()
    kids = _children(c)
    if (c.get("direction") or "") == "row":
        # The web flex row (tab bars, per-row action buttons, swatch strips).
        lay = QHBoxLayout()
        lay.setSpacing(8)
        lay.setContentsMargins(0, 0, 0, 0)
        w.setLayout(lay)
        flexed = False
        for child in kids:
            stretch = _css_flex(_css_of(child))
            flexed = flexed or stretch > 0
            lay.addWidget(render(child, ctx), stretch)
        if not flexed:
            lay.addStretch(1)  # plain rows left-align instead of spreading
        return w
    lay = _vbox(10)
    w.setLayout(lay)
    _render_into(lay, kids, ctx)
    return w


def _r_grid(c, ctx):
    w = QWidget()
    grid = QGridLayout()
    grid.setSpacing(10)
    grid.setContentsMargins(0, 0, 0, 0)
    w.setLayout(grid)
    cols = max(1, int(c.get("columns", 2) or 2))
    for i, child in enumerate(_children(c)):
        grid.addWidget(render(child, ctx), i // cols, i % cols)
    return w


def _r_hero(c, ctx):
    frame = QFrame()
    if c.get("variant") == "gradient":
        # Subtle primary→secondary wash derived from the LIVE palette (parity
        # with the web's .astral-hero--gradient — never hardcoded midnight hex).
        bg = (
            "qlineargradient(x1:0,y1:0,x2:1,y2:1,"
            f"stop:0 {T._rgba(T.PRIMARY, 0.16)}, stop:1 {T._rgba(T.SECONDARY, 0.07)})"
        )
        border = T._rgba(T.PRIMARY, 0.25)
    else:
        # Default hero = plain raised surface with a soft border (web parity).
        bg = T.SURFACE
        border = T.BORDER
    _scoped(frame, f"background:{bg}; border:1px solid {border}; border-radius:14px;")
    frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    lay = _vbox(4, (20, 18, 20, 18))
    frame.setLayout(lay)
    if c.get("eyebrow"):
        lab = _label(str(c["eyebrow"]).upper(), color=T.PRIMARY, size=11, bold=True)
        lay.addWidget(lab)
    lay.addWidget(_label(c.get("title", ""), size=24, bold=True))
    if c.get("subtitle"):
        lay.addWidget(_label(c["subtitle"], color=T.MUTED, size=13))
    badges = c.get("badges") or []
    if badges:
        row = QHBoxLayout()
        row.setSpacing(6)
        for b in badges:
            row.addWidget(_r_badge({"label": b, "variant": "default"}, ctx))
        row.addStretch(1)
        lay.addLayout(row)
    return frame


def _r_badge(c, ctx):
    color, bg = T.VARIANT_COLORS.get(
        c.get("variant", "default"), T.VARIANT_COLORS["default"]
    )
    lab = QLabel(str(c.get("label", "")))
    lab.setStyleSheet(
        f"color:{color}; background:{bg}; border:1px solid {color};"
        f"border-radius:10px; padding:2px 10px; font-size:12px; font-weight:600;"
    )
    lab.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)
    return lab


def _r_metric(c, ctx):
    frame = QFrame()
    _scoped(
        frame,
        "background:qlineargradient(x1:0,y1:0,x2:1,y2:1,"
        "stop:0 rgba(99,102,241,0.18), stop:1 rgba(99,102,241,0.03));"
        f"border:1px solid {T.BORDER}; border-radius:12px;",
    )
    frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    lay = _vbox(2, (16, 14, 16, 14))
    frame.setLayout(lay)
    lay.addWidget(
        _label(str(c.get("title", "")).upper(), color=T.MUTED, size=11, bold=True)
    )
    row = QHBoxLayout()
    row.setSpacing(8)
    row.addWidget(_label(c.get("value", ""), size=24, bold=True))
    if c.get("delta"):
        row.addWidget(
            _label(
                str(c["delta"]),
                color=T.VARIANT_COLORS["success"][0],
                size=13,
                bold=True,
            )
        )
    row.addStretch(1)
    lay.addLayout(row)
    if c.get("subtitle"):
        lay.addWidget(_label(c["subtitle"], color=T.MUTED, size=12))
    return frame


def _r_keyvalue(c, ctx):
    frame = _card_frame()
    lay = _vbox(8, (16, 14, 16, 14))
    frame.setLayout(lay)
    if c.get("title"):
        lay.addWidget(_label(c["title"], size=14, bold=True))
    grid = QGridLayout()
    grid.setSpacing(8)
    for i, item in enumerate(c.get("items", []) or []):
        if not isinstance(item, dict):
            continue
        k = _label(
            str(item.get("label", "")).upper(), color=T.MUTED, size=11, bold=True
        )
        v = _label(item.get("value", ""), size=14, bold=True)
        grid.addWidget(k, i, 0)
        grid.addWidget(v, i, 1)
    grid.setColumnStretch(1, 1)
    lay.addLayout(grid)
    return frame


def _r_timeline(c, ctx):
    frame = _card_frame()
    lay = _vbox(8, (16, 14, 16, 14))
    frame.setLayout(lay)
    if c.get("title"):
        lay.addWidget(_label(c["title"], size=14, bold=True))
    for item in c.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        row = QHBoxLayout()
        row.setSpacing(10)
        dot = QLabel("●")
        dot.setStyleSheet(f"color:{T.PRIMARY}; font-size:10px;")
        row.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
        col = _vbox(0)
        if item.get("time"):
            col.addWidget(_label(item["time"], color=T.MUTED, size=11))
        col.addWidget(
            _label(item.get("title", item.get("label", "")), size=13, bold=True)
        )
        if item.get("description"):
            col.addWidget(_label(item["description"], color=T.MUTED, size=12))
        row.addLayout(col)
        row.addStretch(1)
        lay.addLayout(row)
    return frame


def _r_rating(c, ctx):
    try:
        val = float(c.get("value", 0))
        mx = int(c.get("max_value", c.get("max", 5)))
    except (TypeError, ValueError):
        val, mx = 0.0, 5
    mx = max(1, min(mx, 10))
    stars = "".join("★" if i < round(val) else "☆" for i in range(mx))
    frame = _card_frame()
    lay = _vbox(4, (16, 12, 16, 12))
    frame.setLayout(lay)
    if c.get("label") or c.get("title"):
        lay.addWidget(_label(c.get("label") or c.get("title"), size=13, bold=True))
    row = QHBoxLayout()
    row.setSpacing(8)
    sl = QLabel(stars)
    sl.setStyleSheet(f"color:{T.VARIANT_COLORS['warning'][0]}; font-size:18px;")
    row.addWidget(sl)
    row.addWidget(_label(f"{val:g}/{mx}", color=T.MUTED, size=13))
    row.addStretch(1)
    lay.addLayout(row)
    return frame


def _r_alert(c, ctx):
    color, _ = T.VARIANT_COLORS.get(c.get("variant", "info"), T.VARIANT_COLORS["info"])
    frame = QFrame()
    _scoped(
        frame,
        f"background:{T.SURFACE}; border:1px solid {T.BORDER};"
        f"border-left:3px solid {color}; border-radius:8px;",
    )
    lay = _vbox(4, (14, 12, 14, 12))
    frame.setLayout(lay)
    if c.get("title"):
        lay.addWidget(_label(c["title"], color=color, size=14, bold=True))
    lay.addWidget(_label(c.get("message", ""), color=T.TEXT, size=13))
    return frame


def _btn_label(label) -> str:
    """Literal button text: Qt treats a lone ``&`` as a mnemonic marker and
    swallows it ("Attachments & files" → "Attachments files"), so server-provided
    labels escape it. Android/web render the ampersand verbatim — parity."""
    return str(label).replace("&", "&&")


def _r_button(c, ctx):
    btn = QPushButton(_btn_label(c.get("label", "Button")))
    variant = c.get("variant", "primary")
    if variant == "primary":
        btn.setObjectName("primary")
    elif variant == "danger":
        btn.setObjectName("danger")  # solid error-token treatment (theme QSS)
    action = c.get("action")
    payload = c.get("payload") or {}
    if action:
        btn.clicked.connect(lambda: ctx.emit(action, dict(payload)))
    btn.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return btn


def _to_number(text: str):
    """Mirror the web client's ``Number(value)`` coercion for number fields."""
    try:
        f = float(text)
    except (TypeError, ValueError):
        return text
    return int(f) if f.is_integer() else f


def _fill_template(template: str, state: dict) -> str:
    """Build a submit message from a template + collected field values.

    Mirrors client.js ``submitParamPicker``: ``{__values_json__}`` expands to the
    pretty-printed state, then ``{field}`` tokens are substituted (strings inline,
    other values as JSON); unknown tokens are left untouched.
    """
    msg = (template or "").replace("{__values_json__}", json.dumps(state, indent=2))

    def _repl(m):
        k = m.group(1)
        if k not in state:
            return m.group(0)
        v = state[k]
        return v if isinstance(v, str) else json.dumps(v)

    return re.sub(r"\{(\w+)\}", _repl, msg)


def _r_param_picker(c, ctx):
    """A field form + Submit; on submit emit a ``chat_message`` built from the
    field values and ``submit_message_template`` (parity with the web target)."""
    frame = _card_frame()
    lay = _vbox(10, (16, 14, 16, 14))
    frame.setLayout(lay)
    if c.get("title"):
        lay.addWidget(_label(c["title"], size=16, bold=True))
    if c.get("description"):
        lay.addWidget(_label(c["description"], color=T.MUTED, size=13))
    getters: Dict[str, Callable[[], Any]] = {}
    # 063.1 declarative visibility: fields marked visible_when {field, equals,
    # default} show only while the named controller select matches; hidden
    # fields still submit (the server reads only the matching inputs).
    combos: Dict[str, QComboBox] = {}
    conditional: List[Any] = []  # (visible_when, [widgets])
    for field in c.get("fields", []) or []:
        if not isinstance(field, dict):
            continue
        name = field.get("name", "")
        label = field.get("label") or name
        kind = field.get("kind", "text")
        default = field.get("default")
        first_item = lay.count()
        if kind == "boolean":
            box = QCheckBox(str(label))
            box.setChecked(bool(default))
            getters[name] = lambda b=box: b.isChecked()
            lay.addWidget(box)
        elif kind == "select":
            lay.addWidget(_label(label, color=T.MUTED, size=12))
            combo = QComboBox()
            opts = [str(o) for o in (field.get("options") or [])]
            combo.addItems(opts)
            if default is not None and str(default) in opts:
                combo.setCurrentText(str(default))
            getters[name] = lambda cb=combo: cb.currentText()
            combos[name] = combo
            lay.addWidget(combo)
        elif kind == "checklist":
            lay.addWidget(_label(label, color=T.MUTED, size=12))
            sel = set(default) if isinstance(default, list) else set()
            chips: List = []
            row = QHBoxLayout()
            row.setSpacing(6)
            for opt in field.get("options") or []:
                btn = QPushButton(_btn_label(opt))
                btn.setCheckable(True)
                btn.setChecked(opt in sel)
                row.addWidget(btn)
                chips.append((opt, btn))
            row.addStretch(1)
            lay.addLayout(row)
            getters[name] = lambda ch=chips: [o for o, b in ch if b.isChecked()]
        elif kind == "number":
            lay.addWidget(_label(label, color=T.MUTED, size=12))
            edit = QLineEdit()
            if default is not None:
                edit.setText(str(default))
            getters[name] = lambda e=edit: None if e.text() == "" else _to_number(e.text())
            lay.addWidget(edit)
        elif kind == "password":
            # Feature 043: write-only key field — never pre-filled (blank = keep).
            lay.addWidget(_label(label, color=T.MUTED, size=12))
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            getters[name] = lambda e=edit: e.text()
            lay.addWidget(edit)
        elif kind == "textarea":
            lay.addWidget(_label(label, color=T.MUTED, size=12))
            area = QPlainTextEdit()
            if default is not None:
                area.setPlainText(str(default))
            area.setMinimumHeight(72)
            getters[name] = lambda a=area: a.toPlainText()
            lay.addWidget(area)
        else:  # text (default)
            lay.addWidget(_label(label, color=T.MUTED, size=12))
            edit = QLineEdit()
            if default is not None:
                edit.setText(str(default))
            getters[name] = lambda e=edit: e.text()
            lay.addWidget(edit)
        if field.get("help"):
            lay.addWidget(_label(field["help"], color=T.MUTED, size=11))
        vw = field.get("visible_when")
        if isinstance(vw, dict):
            widgets = [lay.itemAt(i).widget() for i in range(first_item, lay.count())]
            conditional.append((vw, [w for w in widgets if w is not None]))
    if conditional:
        def _apply_visibility() -> None:
            for vw, widgets in conditional:
                combo = combos.get(vw.get("field", ""))
                current = (combo.currentText() if combo is not None
                           else str(vw.get("default") or ""))
                show = current == vw.get("equals")
                for widget in widgets:
                    widget.setVisible(show)
        for combo in combos.values():
            combo.currentTextChanged.connect(lambda _t: _apply_visibility())
        _apply_visibility()
    # Feature 043: settings forms (LLM, Personalization) submit their collected
    # fields to a chrome_* action (action-submit) rather than a chat message. A
    # form may carry several action buttons (Load / Test / Save) that all submit
    # the SAME fields. Falls back to the legacy chat_message submit otherwise.
    actions = c.get("actions") if isinstance(c.get("actions"), list) else []
    if not actions and c.get("submit_action"):
        actions = [{"label": c.get("submit_label", "Save"), "action": c["submit_action"],
                    "variant": "primary", "payload": c.get("submit_payload") or {}}]
    row = QHBoxLayout()
    row.addStretch(1)
    if actions:
        def _make(action, extra):
            def _s():
                state = {k: g() for k, g in getters.items()}
                ctx.emit(action, {"fields": state, **(extra or {})})
            return _s
        for a in actions:
            if not isinstance(a, dict) or not a.get("action"):
                continue
            btn = QPushButton(_btn_label(a.get("label") or "Submit"))
            if (a.get("variant") or "") == "primary":
                btn.setObjectName("primary")
            btn.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(_make(a["action"], a.get("payload") or {}))
            row.addWidget(btn)
    else:
        template = c.get("submit_message_template", "")
        submit = QPushButton(_btn_label(c.get("submit_label", "Submit")))
        submit.setObjectName("primary")
        submit.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

        def _submit():
            state = {k: g() for k, g in getters.items()}
            ctx.emit("chat_message", {"message": _fill_template(template, state)})

        submit.clicked.connect(_submit)
        row.addWidget(submit)
    lay.addLayout(row)
    return frame


def _r_input(c, ctx):
    """A labeled single-line text field + Submit (emits a ``chat_message``)."""
    w = QWidget()
    lay = _vbox(6)
    w.setLayout(lay)
    name = c.get("name") or "value"
    label = c.get("label") or c.get("name")
    if label:
        lay.addWidget(_label(label, color=T.MUTED, size=12))
    edit = QLineEdit()
    edit.setText(str(c.get("value", "")))
    if c.get("placeholder"):
        edit.setPlaceholderText(str(c["placeholder"]))
    template = c.get("submit_message_template", "")
    submit = QPushButton(_btn_label(c.get("submit_label", "Submit")))
    submit.setObjectName("primary")
    submit.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def _submit():
        val = edit.text()
        msg = _fill_template(template, {name: val}) if template else val
        ctx.emit("chat_message", {"message": msg})

    submit.clicked.connect(_submit)
    edit.returnPressed.connect(_submit)
    row = QHBoxLayout()
    row.setSpacing(8)
    row.addWidget(edit, 1)
    row.addWidget(submit)
    lay.addLayout(row)
    return w


def _accept_to_filter(accept: str) -> str:
    """Turn an HTML ``accept`` string into a Qt file dialog filter."""
    accept = (accept or "").strip()
    if not accept or accept == "*/*":
        return "All files (*)"
    pats = []
    for tok in accept.split(","):
        tok = tok.strip()
        if tok.startswith("."):
            pats.append("*" + tok)
        elif tok and "/" not in tok:
            pats.append("*." + tok)
    if not pats:
        return "All files (*)"
    return f"Accepted ({' '.join(pats)});;All files (*)"


def _r_file_upload(c, ctx):
    """A native file-picker button. When the component carries an ``action`` the
    chosen path is emitted with its payload (mirrors ``_r_button`` wiring)."""
    w = QWidget()
    lay = _vbox(6)
    w.setLayout(lay)
    label = c.get("label", "Upload File")
    accept = c.get("accept", "")
    action = c.get("action")
    payload = c.get("payload") or {}
    btn = QPushButton(_btn_label(label))
    btn.setObjectName("primary")
    btn.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    chosen = _label("", color=T.MUTED, size=12)

    def _pick():
        path, _sel = QFileDialog.getOpenFileName(
            btn, str(label), "", _accept_to_filter(accept)
        )
        if not path:
            return
        chosen.setText(path)
        if action:
            data = dict(payload)
            data["path"] = path
            ctx.emit(action, data)

    btn.clicked.connect(_pick)
    row = QHBoxLayout()
    row.setSpacing(8)
    row.addWidget(btn)
    row.addWidget(chosen, 1)
    lay.addLayout(row)
    return w


def _r_file_download(c, ctx):
    """A download button/link showing the filename, opening the URL externally,
    plus a SHA-256 integrity block when present. Serves both ``file_download``
    and the richer ``download_card``."""
    frame = _card_frame()
    lay = _vbox(8, (16, 12, 16, 12))
    frame.setLayout(lay)
    if c.get("title"):
        lay.addWidget(_label(c["title"], size=14, bold=True))
    if c.get("description"):
        lay.addWidget(_label(c["description"], color=T.MUTED, size=12))
    version = c.get("version")
    platform = c.get("platform")
    meta = " · ".join(
        x for x in [f"v{version}" if version else "", platform or ""] if x
    )
    if meta:
        lay.addWidget(_label(meta, color=T.MUTED, size=11))
    # file_download uses `url`; download_card uses `download_url`.
    url = c.get("url") or c.get("download_url") or ""
    filename = c.get("filename") or c.get("title")
    label = c.get("label") or (f"Download {filename}" if filename else "Download File")
    valid = bool(url) and url != "#" and str(url).startswith(("http", "/"))
    btn = QPushButton(_btn_label(label))
    btn.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    if valid:
        btn.setObjectName("primary")
        u = str(url)
        # A root-relative /api/download/... URL is an authed backend file: fetch
        # it with the session token and save via a native dialog. An absolute
        # http(s) URL (e.g. a download_card GitHub asset) opens externally.
        if u.startswith("/") and getattr(ctx, "download", None) is not None:
            fn = str(filename or "download")
            btn.clicked.connect(lambda checked=False, uu=u, ff=fn: ctx.download(uu, ff))
        else:
            btn.clicked.connect(lambda checked=False, uu=u: QDesktopServices.openUrl(QUrl(uu)))
    else:
        btn.setEnabled(False)
        btn.setText(str(label) + " (unavailable)")
    lay.addWidget(btn)
    sha = str(c.get("sha256") or c.get("sha") or "").lower()
    if sha:
        lay.addWidget(_label("SHA-256", color=T.MUTED, size=11, bold=True))
        sha_lab = _label(sha, color=T.MUTED, size=11)
        sha_lab.setStyleSheet(
            f"color:{T.MUTED}; font-family:Consolas,monospace; font-size:11px;"
            "background:transparent;"
        )
        lay.addWidget(sha_lab)
    return frame


def _r_code(c, ctx):
    edit = QPlainTextEdit()
    edit.setReadOnly(True)
    edit.setPlainText(c.get("code", ""))
    edit.setStyleSheet(
        f"background:{T.SURFACE_2}; border:1px solid {T.BORDER}; border-radius:8px;"
        f"font-family:Consolas,monospace; font-size:13px; color:{T.TEXT};"
    )
    lines = c.get("code", "").count("\n") + 1
    edit.setFixedHeight(min(360, 22 * lines + 20))
    return edit


def _r_divider(c, ctx):
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet(f"background:{T.BORDER}; max-height:1px;")
    return line


def _r_progress(c, ctx):
    w = QWidget()
    lay = _vbox(4)
    w.setLayout(lay)
    if c.get("label"):
        lay.addWidget(_label(c["label"], color=T.MUTED, size=12))
    bar = QProgressBar()
    bar.setRange(0, 100)
    try:
        bar.setValue(int(float(c.get("value", 0)) * 100))
    except (TypeError, ValueError):
        bar.setValue(0)
    bar.setTextVisible(bool(c.get("show_percentage", True)))
    bar.setStyleSheet(
        f"QProgressBar{{background:{T.SURFACE_2}; border:1px solid {T.BORDER};"
        f"border-radius:6px; height:14px; text-align:center; color:{T.TEXT};}}"
        f"QProgressBar::chunk{{background:{T.PRIMARY}; border-radius:6px;}}"
    )
    lay.addWidget(bar)
    return w


def _r_list(c, ctx):
    frame = _card_frame()
    lay = _vbox(6, (16, 12, 16, 12))
    frame.setLayout(lay)
    if c.get("title"):
        lay.addWidget(_label(c["title"], size=14, bold=True))
    ordered = bool(c.get("ordered"))
    for i, item in enumerate(c.get("items", []) or []):
        if isinstance(item, dict):
            txt = item.get("title", item.get("content", json.dumps(item)))
        else:
            txt = str(item)
        bullet = f"{i + 1}." if ordered else "•"
        row = QHBoxLayout()
        row.setSpacing(8)
        b = _label(bullet, color=T.MUTED)
        b.setFixedWidth(20)
        row.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(_label(txt), 1)
        lay.addLayout(row)
    return frame


def _r_table(c, ctx):
    headers = c.get("headers", []) or []
    rows = c.get("rows", []) or []
    frame = _card_frame()
    lay = _vbox(8, (12, 12, 12, 12))
    frame.setLayout(lay)
    title = c.get("title") or c.get("label")
    if title:
        lay.addWidget(_label(title, size=14, bold=True))
    tbl = QTableWidget(len(rows), len(headers))
    tbl.setHorizontalHeaderLabels([str(h) for h in headers])
    tbl.verticalHeader().setVisible(False)
    tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    tbl.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
    for r, row in enumerate(rows):
        for col, cell in enumerate(row if isinstance(row, list) else []):
            tbl.setItem(r, col, QTableWidgetItem(str(cell)))
    tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    tbl.setFixedHeight(min(420, 32 * (len(rows) + 1) + 8))
    lay.addWidget(tbl)
    # Feature 044 (T026): a server-side pagination pager when the table
    # advertises a total + page size (parity with the web table_paginate
    # round-trip — the server replies with a ui_upsert keyed to component_id).
    total = c.get("total_rows")
    page_size = c.get("page_size")
    if isinstance(total, int) and isinstance(page_size, int) and page_size > 0:
        lay.addLayout(_table_pager(c, ctx, len(rows)))
    return frame


def _table_pager(c, ctx, n_rows: int):
    """A ``‹ Prev  rows X–Y of Z  Next ›`` row under a paginated table. Prev/Next
    emit ``table_paginate`` for the table's component id; the server upserts the
    same component in place."""
    total = int(c.get("total_rows") or 0)
    page_size = int(c.get("page_size") or 0)
    try:
        page_offset = max(0, int(c.get("page_offset") or 0))
    except (TypeError, ValueError):
        page_offset = 0
    cid = c.get("component_id") or c.get("id")
    shown = n_rows if n_rows else page_size
    start = page_offset + 1 if total else 0
    end = min(page_offset + shown, total) if total else page_offset + shown

    def _go(new_offset: int) -> None:
        payload = {
            "component_id": cid,
            "params": {"page_offset": max(0, new_offset), "page_size": page_size},
        }
        chat_id = getattr(ctx, "chat_id", None)
        if chat_id:
            payload["chat_id"] = chat_id
        ctx.emit("table_paginate", payload)

    row = QHBoxLayout()
    row.setSpacing(8)
    prev = QPushButton("‹ Prev")
    prev.setEnabled(page_offset > 0)
    prev.clicked.connect(lambda: _go(page_offset - page_size))
    nxt = QPushButton("Next ›")
    nxt.setEnabled(page_offset + page_size < total)
    nxt.clicked.connect(lambda: _go(page_offset + page_size))
    for b in (prev, nxt):
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    row.addWidget(prev)
    row.addWidget(_label(f"rows {start}–{end} of {total}", color=T.MUTED, size=12), 1)
    row.addWidget(nxt)
    return row


def _r_tabs(c, ctx):
    tabs = QTabWidget()
    for tab in c.get("tabs", []) or []:
        if not isinstance(tab, dict):
            continue
        page = QWidget()
        pl = _vbox(10, (12, 12, 12, 12))
        page.setLayout(pl)
        _render_into(pl, _children(tab), ctx)
        pl.addStretch(1)
        tabs.addTab(page, str(tab.get("label", "Tab")))
    return tabs


def _r_collapsible(c, ctx):
    frame = _card_frame()
    lay = _vbox(6, (12, 10, 12, 10))
    frame.setLayout(lay)
    body = QWidget()
    bl = _vbox(8)
    body.setLayout(bl)
    _render_into(bl, _children(c), ctx)
    body.setVisible(bool(c.get("default_open")))
    btn = QPushButton(
        ("▾ " if body.isVisible() else "▸ ") + str(c.get("title", "Details"))
    )
    btn.setStyleSheet(
        "text-align:left; border:none; background:transparent; font-weight:600;"
    )

    def toggle():
        body.setVisible(not body.isVisible())
        btn.setText(
            ("▾ " if body.isVisible() else "▸ ") + str(c.get("title", "Details"))
        )

    btn.clicked.connect(toggle)
    lay.addWidget(btn)
    lay.addWidget(body)
    return frame


def _r_chart(c, ctx):
    """bar/line/pie via QtCharts when available, else a labeled fallback."""
    try:
        from .charts import build_chart

        w = build_chart(c)
        if w is not None:
            return w
    except Exception:
        pass
    frame = _card_frame(bg=T.SURFACE_2)
    lay = _vbox(4, (16, 14, 16, 14))
    frame.setLayout(lay)
    lay.addWidget(_label(c.get("title", c.get("type", "chart")), size=14, bold=True))
    lay.addWidget(_label("(chart)", color=T.MUTED, size=12))
    return frame


def _image_pixmap(url: str):
    """Best-effort ``QPixmap`` from a ``data:`` URI ONLY (synchronous, fast, no
    network). Returns ``None`` on any failure — and for http(s) URLs, which are
    fetched OFF the GUI thread by :class:`_AsyncImageLabel` (a synchronous
    urlopen here would freeze the render for up to 4 s per image). Never raises."""
    from PySide6.QtGui import QPixmap

    url = str(url or "")
    if not url.startswith("data:"):
        return None
    try:
        header, _, payload = url.partition(",")
        raw = (base64.b64decode(payload) if "base64" in header.lower()
               else payload.encode("utf-8"))
        pix = QPixmap()
        pix.loadFromData(raw)
        return pix if not pix.isNull() else None
    except Exception:  # noqa: BLE001 — image load is best-effort, degrade to alt
        return None


def _fetch_image_bytes(url: str):
    """Fetch remote image bytes over http(s) (8 MB / 4 s bounds). Returns
    ``None`` on any failure so the caller degrades to alt text — never raises.
    Called ONLY from a worker thread (never the GUI thread)."""
    url = str(url or "")
    if not url.startswith(("http://", "https://")):
        return None
    try:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "AstralWindowsClient"})
        with urllib.request.urlopen(req, timeout=4) as r:  # noqa: S310 — scheme-guarded
            return r.read(8 * 1024 * 1024)
    except Exception:  # noqa: BLE001 — image load is best-effort, degrade to alt
        return None


class _AsyncImageLabel(QLabel):
    """A QLabel that shows its alt text immediately, then fetches a remote image
    OFF the GUI thread and swaps in the QPixmap when the bytes arrive.

    A synchronous ``urlopen`` during ``render()`` used to freeze the GUI thread
    up to 4 s per remote image. The fetch runs on a daemon thread and the bytes
    are marshaled back via the ``_loaded`` signal, so the widget is only ever
    touched on the GUI thread (the worker never touches Qt state). A failed or
    empty fetch keeps the alt-text placeholder."""

    _loaded = Signal(object)  # raw image bytes, or None on failure

    def __init__(self, url: str, alt: str, maxw: int, parent=None):
        super().__init__(parent)
        self._maxw = maxw
        self.setText(alt or "🖼 image")
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setStyleSheet(f"color:{T.MUTED}; font-size:12px; background:transparent;")
        self._loaded.connect(self._apply_bytes)
        threading.Thread(
            target=self._fetch, args=(url,), name="astral-image", daemon=True
        ).start()

    def _fetch(self, url: str) -> None:
        raw = _fetch_image_bytes(url)
        try:
            self._loaded.emit(raw)
        except RuntimeError:  # the C++ QLabel may be gone during teardown
            pass

    def _apply_bytes(self, raw: object) -> None:
        """GUI-thread slot: turn fetched bytes into the displayed pixmap."""
        if not raw:
            return  # keep the alt-text placeholder
        from PySide6.QtGui import QPixmap

        pix = QPixmap()
        pix.loadFromData(raw)
        if pix.isNull():
            return
        if pix.width() > self._maxw:
            pix = pix.scaledToWidth(self._maxw, Qt.TransformationMode.SmoothTransformation)
        self.setText("")
        self.setPixmap(pix)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft)


def _image_max_width(c) -> int:
    try:
        maxw = int(c.get("width") or 0) or 480
    except (TypeError, ValueError):
        maxw = 480
    return max(48, min(maxw, 900))


def _r_image(c, ctx):
    """Native image: decode a ``data:`` URI synchronously, or fetch an http(s)
    URL OFF the GUI thread (immediate alt-text placeholder, QPixmap when ready);
    show the alt text when unavailable. Parity with the Android ``image``
    renderer — a native improvement over the old placeholder. The widget is
    always returned synchronously (no blocking network on the render path)."""
    w = QWidget()
    lay = _vbox(4)
    w.setLayout(lay)
    alt = str(c.get("alt") or c.get("caption") or c.get("title") or "")
    url = str(c.get("url") or c.get("src") or "")
    maxw = _image_max_width(c)
    if url.startswith(("http://", "https://")):
        # Remote: placeholder now, real bytes fetched off-thread (never blocks).
        lay.addWidget(_AsyncImageLabel(url, alt, maxw))
        return w
    pix = _image_pixmap(url)  # data: URIs decode synchronously (fast, no network)
    if pix is not None:
        if pix.width() > maxw:
            pix = pix.scaledToWidth(maxw, Qt.TransformationMode.SmoothTransformation)
        img = QLabel()
        img.setPixmap(pix)
        img.setAlignment(Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(img)
        if alt:
            lay.addWidget(_label(alt, color=T.MUTED, size=11))
    else:
        lay.addWidget(_label(alt or "🖼 image", color=T.MUTED, size=12))
    return w


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _plotly_to_chart_dict(c) -> Optional[dict]:
    """Extract numeric x/y traces from a Plotly figure spec into a chart dict the
    QtCharts path (``charts.build_chart``) can draw, or ``None`` when there is
    nothing numeric to plot."""
    data = c.get("data")
    layout = c.get("layout") if isinstance(c.get("layout"), dict) else {}
    fig = c.get("figure") if isinstance(c.get("figure"), dict) else None
    if fig:
        if isinstance(fig.get("data"), list):
            data = fig["data"]
        if isinstance(fig.get("layout"), dict):
            layout = fig["layout"]
    if not isinstance(data, list):
        return None
    datasets: List[dict] = []
    labels: List[str] = []
    kind = "line_chart"
    for tr in data:
        if not isinstance(tr, dict):
            continue
        ys = [float(v) for v in (tr.get("y") or []) if _is_number(v)]
        if not ys:
            continue
        datasets.append({"label": str(tr.get("name") or f"series {len(datasets) + 1}"),
                         "data": ys})
        if not labels and isinstance(tr.get("x"), list):
            labels = [str(x) for x in tr["x"]][: len(ys)]
        if str(tr.get("type") or "").lower() == "bar":
            kind = "bar_chart"
    if not datasets:
        return None
    title = c.get("title") or ""
    if not title and layout:
        t = layout.get("title")
        title = (t.get("text") if isinstance(t, dict) else t) or ""
    return {"type": kind, "title": str(title), "labels": labels, "datasets": datasets}


def _r_plotly_chart(c, ctx):
    """Plotly is web/JS-only; draw a best-effort native approximation from the
    figure's traces via the QtCharts bar/line path, else a labeled note. Never
    raises. Advertising this type keeps ROTE from degrading server-side charts."""
    spec = _plotly_to_chart_dict(c)
    if spec is not None:
        try:
            from .charts import build_chart

            w = build_chart(spec)
            if w is not None:
                return w
        except Exception:  # noqa: BLE001 — fall through to the labeled note
            pass
    frame = _card_frame(bg=T.SURFACE_2)
    lay = _vbox(4, (16, 14, 16, 14))
    frame.setLayout(lay)
    lay.addWidget(_label(c.get("title", "Chart"), size=14, bold=True))
    lay.addWidget(_label("interactive chart — view on web", color=T.MUTED, size=12))
    return frame


def _r_chat_history(c, ctx):
    frame = _card_frame()
    lay = _vbox(4, (10, 10, 10, 10))
    frame.setLayout(lay)
    if c.get("title"):
        lay.addWidget(_label(c["title"], size=14, bold=True))
    for item in c.get("items", c.get("chats", [])) or []:
        if not isinstance(item, dict):
            continue
        cid = item.get("chat_id") or item.get("id")
        btn = QPushButton(_btn_label(item.get("title", "Chat")))
        btn.setStyleSheet("text-align:left; padding:8px;")
        if cid:
            btn.clicked.connect(
                lambda _=False, x=cid: ctx.emit("load_chat", {"chat_id": x})
            )
        lay.addWidget(btn)
    return frame


def _r_skeleton(c, ctx):
    w = QWidget()
    lay = _vbox(8)
    w.setLayout(lay)
    try:
        count = int(c.get("count", 3) or 3)
    except (TypeError, ValueError):
        count = 3
    # "card" placeholders are chunky blocks (the canvas loading state, matching
    # the web/Android SkeletonCanvas); the default stays thin text-like lines.
    height, radius = (72, 12) if c.get("variant") == "card" else (14, 7)
    for _ in range(max(1, min(6, count))):
        bar = QLabel()
        bar.setFixedHeight(height)
        bar.setStyleSheet(f"background:{T.SURFACE_2}; border-radius:{radius}px;")
        lay.addWidget(bar)
    return w


# Feature 055 (US4, wire-contract §6): the server-stamped ``provenance`` field
# → compact trust badge (glyph, label, tooltip, semantic color). Absent/unknown
# values render nothing — pre-055 servers (and FF off) stay byte-identical.
# Colors mirror the web footer: grounded=success green, estimated=warning
# yellow, generated=muted. ``None`` color resolves to T.MUTED at render time
# (the palette is live-themable, so colors can't be captured at import).
_PROVENANCE_BADGES = {
    "grounded": ("✓", "tool data", "success",
                 "Sourced from a tool result"),
    "estimated": ("≈", "estimated", "warning",
                  "Estimated / low-confidence value"),
    "generated": ("✦", "AI-generated", None,
                  "Written by the assistant — not sourced from a tool"),
}

#: Decorative / structural types that assert no facts — never badged
#: (parity with the web footer's skip set).
_PROVENANCE_SKIP_TYPES = frozenset({"divider", "skeleton"})


def provenance_badge(c: dict) -> Optional[QLabel]:
    """The compact provenance badge for a component dict, or ``None`` when the
    field is absent/unknown or the type is decorative (render nothing)."""
    if not isinstance(c, dict):
        return None
    if str(c.get("type", "")).strip().lower() in _PROVENANCE_SKIP_TYPES:
        return None
    meta = _PROVENANCE_BADGES.get(c.get("provenance"))
    if meta is None:
        return None
    icon, text, variant, tip = meta
    color = T.VARIANT_COLORS[variant][0] if variant else T.MUTED
    lab = QLabel(f"{icon} {text}")
    lab.setObjectName("provenance_badge")
    lab.setProperty("provenance", str(c["provenance"]))
    lab.setToolTip(tip)
    lab.setStyleSheet(f"color:{color}; font-size:10px; background:transparent;")
    lab.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)
    return lab


def _with_provenance_badge(widget: QWidget, c: dict) -> QWidget:
    """Wrap a rendered top-level component with its right-aligned provenance
    badge row (the native component chrome). No badge → the widget is returned
    unwrapped, byte-identical to pre-055 rendering."""
    badge = provenance_badge(c)
    if badge is None:
        return widget
    wrap = QWidget()
    lay = _vbox(2)
    wrap.setLayout(lay)
    lay.addWidget(widget)
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 4, 0)
    row.addStretch(1)
    row.addWidget(badge)
    lay.addLayout(row)
    return wrap


def _r_fallback(c, ctx):
    frame = QFrame()
    frame.setStyleSheet(
        f"background:{T.SURFACE_2}; border:1px dashed {T.BORDER}; border-radius:8px;"
    )
    lay = _vbox(2, (12, 8, 12, 8))
    frame.setLayout(lay)
    lay.addWidget(_label(f"[{c.get('type', '?')}]", color=T.MUTED, size=12, bold=True))
    for k in ("title", "label", "message", "content", "value"):
        if isinstance(c.get(k), str):
            lay.addWidget(_label(c[k], color=T.TEXT, size=13))
            break
    return frame


REGISTRY: Dict[str, Callable[[dict, RenderContext], QWidget]] = {
    "text": _r_text,
    "card": _r_card,
    "container": _r_container,
    "grid": _r_grid,
    "hero": _r_hero,
    "badge": _r_badge,
    "metric": _r_metric,
    "keyvalue": _r_keyvalue,
    "timeline": _r_timeline,
    "rating": _r_rating,
    "alert": _r_alert,
    "button": _r_button,
    "param_picker": _r_param_picker,
    "input": _r_input,
    "file_upload": _r_file_upload,
    "file_download": _r_file_download,
    "download_card": _r_file_download,
    "code": _r_code,
    "divider": _r_divider,
    "progress": _r_progress,
    "list": _r_list,
    "table": _r_table,
    "tabs": _r_tabs,
    "collapsible": _r_collapsible,
    "bar_chart": _r_chart,
    "line_chart": _r_chart,
    "pie_chart": _r_chart,
    "plotly_chart": _r_plotly_chart,
    "image": _r_image,
    "chat_history": _r_chat_history,
    "skeleton": _r_skeleton,
}


def _apply_theme_via_ctx(spec, ctx) -> None:
    """Feature 044 (US5) — route a theme spec to the app's SINGLE theme-apply
    implementation: ``RenderContext.apply_theme``, wired by the MainWindow to
    its ``_apply_theme_pref`` path (which mutates the palette synchronously and
    DEFERS the global restyle to the next event-loop turn — a global re-polish
    from *inside* a render pass is re-entrant and segfaults headless Qt).
    Without a wired callback (bare unit renders) only the palette mutates;
    theming must never break a render pass (fail-open)."""
    try:
        cb = getattr(ctx, "apply_theme", None)
        if callable(cb):
            cb(spec)
        else:
            T.apply_theme(spec)
    except Exception:  # noqa: BLE001 — theming must never break a render pass
        pass


def _choose_color(initial: str, parent, key: str) -> Optional[str]:
    """Open the native colour chooser, returning the picked ``#rrggbb`` (or
    ``None`` if cancelled). Factored out so the color_picker's emit path is
    unit-testable without driving a modal dialog."""
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QColorDialog

    chosen = QColorDialog.getColor(QColor(initial), parent, f"Choose {key or 'color'}")
    return chosen.name() if chosen.isValid() else None


def _r_color_picker(c, ctx):
    """Feature 043/044 — a theme channel swatch + hex readout (Theme surface).

    Clicking the swatch opens a native colour chooser; on pick it emits
    ``save_theme`` (server persist, FR-016) AND applies the change to the live
    palette immediately (US5 fine-tune)."""
    w = QWidget()
    lay = QHBoxLayout()
    lay.setContentsMargins(0, 2, 0, 2)
    lay.setSpacing(8)
    w.setLayout(lay)
    key = str(c.get("color_key") or "")
    val = str(c.get("value") or "#000000")
    swatch = QPushButton()
    swatch.setFixedSize(22, 22)
    swatch.setCursor(Qt.CursorShape.PointingHandCursor)
    _swatch_css = "background:%s; border:1px solid rgba(255,255,255,0.2); border-radius:4px;"
    swatch.setStyleSheet(_swatch_css % val)
    readout = _label(val, color=T.MUTED, size=12)

    def _pick():
        hexv = _choose_color(val, w, key)
        if not hexv:
            return
        swatch.setStyleSheet(_swatch_css % hexv)
        readout.setText(hexv)
        if key:
            ctx.emit("save_theme", {"theme": {"color_key": key, "color_value": hexv}})
            _apply_theme_via_ctx({"color_key": key, "color_value": hexv}, ctx)

    swatch.clicked.connect(_pick)
    lay.addWidget(swatch)
    lay.addWidget(_label(str(c.get("label") or key or ""), color=T.TEXT, size=13))
    lay.addStretch(1)
    lay.addWidget(readout)
    return w


def _r_theme_apply(c, ctx):
    """Feature 043/044 — the ``theme_apply`` side-effect carries the chosen
    preset/colors for the client to apply. Route it to the app's theme path
    (US5) and return a zero-height spacer (no visible UI)."""
    _apply_theme_via_ctx(c, ctx)
    w = QWidget()
    w.setFixedHeight(0)
    return w


REGISTRY.update({"color_picker": _r_color_picker, "theme_apply": _r_theme_apply})


def render(component: Any, ctx: RenderContext, *, top_level: bool = False) -> QWidget:
    """Render one structured SDUI component dict into a native widget.

    ``top_level=True`` adds the component chrome — today the provenance badge
    (055 US4) — and is passed only by the canvas for its top-level components;
    nested children are stamped with ``provenance`` too (``_tag_source``
    recurses) but must not each grow a badge."""
    if not isinstance(component, dict):
        return _label(str(component))
    builder = REGISTRY.get(component.get("type", ""), _r_fallback)
    try:
        widget = builder(component, ctx)
    except Exception as exc:  # never let one bad component crash the canvas
        widget = _label(
            f"[render error: {component.get('type')}: {exc}]", color=T.MUTED, size=12
        )
    if top_level:
        widget = _with_provenance_badge(widget, component)
    cid = component.get("component_id") or component.get("id")
    if cid:
        widget.setProperty("component_id", str(cid))
    return widget


def supported_types() -> List[str]:
    """The primitive types this native target renders directly (the rest fall
    back to a labeled placeholder) — used for ROTE capability negotiation."""
    return sorted(REGISTRY.keys())
