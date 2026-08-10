"""Cross-client top-bar parity (Constitution XII).

The top bar is ONE shared, server-owned definition: "No client may add, omit,
rename, reorder, or otherwise diverge from those shared definitions"
(`.specify/memory/constitution.md`), and
`specs/044-native-client-parity/contracts/chrome-parity.md` pins "Ordering and
presence follow the model verbatim".

Web, Android and Apple all lay the bar out as

    brand · New chat · Recent chats · <server-model actions> · Settings

The Windows client shipped the server-model action cluster (Pulse, Workspace
timeline) BEFORE the client-local New/Recent buttons — visibly out of order next
to the web app on the same desktop, which is the constitution's own example. This
file is the drift guard so it cannot silently regress.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


def _bar(qapp):
    from astral_client.app import TopBar

    return TopBar("u", lambda: None, lambda: None, lambda s, ln: None, lambda: None)


def _order(tb):
    """The widgets of the top-bar row, in visual (layout) order."""
    lay = tb.layout()
    out = []
    for i in range(lay.count()):
        item = lay.itemAt(i)
        w = item.widget()
        out.append(w if w is not None else "stretch")
    return out


def test_topbar_widget_order_matches_the_shared_model(qapp):
    tb = _bar(qapp)
    order = _order(tb)

    # brand cluster (status mark + wordmark, the web's logo + "AstralDeep")
    # < (stretch) < new < recent < server-model actions < settings
    assert order[0] is tb._mark
    assert order[1] is tb.brand_label
    assert order[2] == "stretch"
    assert order[3:] == [tb.new_btn, tb.recent_btn, tb._actions_holder, tb.settings_btn]


def test_server_model_actions_sit_between_recent_and_settings(qapp):
    """The specific defect: the actions holder must NOT precede New/Recent."""
    tb = _bar(qapp)
    order = _order(tb)

    assert order.index(tb._actions_holder) > order.index(tb.recent_btn)
    assert order.index(tb._actions_holder) > order.index(tb.new_btn)
    assert order.index(tb._actions_holder) < order.index(tb.settings_btn)


def test_recent_chats_does_not_use_the_clock_glyph(qapp):
    """The clock belongs to the server 'Workspace timeline' control, which now
    sits immediately beside Recent chats — two clocks side by side is the drift
    android's RootScaffold explicitly warns about."""
    tb = _bar(qapp)
    assert "🕓" not in tb.recent_btn.text()
    # 066: the button is icon-only (web/Android presentation), so its identity
    # is carried by the accessible name + tooltip rather than visible text.
    assert tb.recent_btn.accessibleName() == "Recent chats"
    assert tb.recent_btn.toolTip() == "Recent chats"


def test_topbar_controls_are_icon_only_with_names(qapp):
    """066 style parity: web and Android render every top-bar control except
    "＋ New" as an icon with the name in the tooltip. Windows shipped full text
    labels, which read as a different application beside them. Icon-only is
    only acceptable while every control still NAMES itself for a screen reader
    and for hover — this pins both halves together."""
    tb = _bar(qapp)
    tb._rebuild_topbar_actions(
        [{"surface": "workspace_timeline", "label": "Workspace timeline",
          "icon": "clock"}]
    )

    for btn, name in (
        (tb.recent_btn, "Recent chats"),
        (tb.settings_btn, "Settings"),
        (tb._action_buttons[0], "Workspace timeline"),
    ):
        assert len(btn.text()) <= 2, f"{name} still renders a text label"
        assert btn.accessibleName() == name
        assert btn.toolTip() == name

    # "＋ New" keeps its word on every client — it is the primary action.
    assert "New" in tb.new_btn.text()


def test_every_server_action_icon_name_has_a_glyph(qapp):
    """The icon names the top-bar model can emit, from the single source
    `backend/webrender/chrome/menu_model.py` (web resolves the same three in
    `chrome/topbar.py::_ICON_SVG`). `sparkle` was absent from the Windows map,
    so Pulse digest was the one control still rendering as text next to
    icon-only neighbours. A new server icon must land here too."""
    from astral_client.app import TopBar

    for name in ("sparkle", "history", "gear"):
        assert TopBar._ACTION_ICONS.get(name), f"no Windows glyph for icon {name!r}"
