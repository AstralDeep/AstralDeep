"""Feature 030 — accessibility of server-rendered primitives (webrender).

Designed canvases must be perceivable by assistive tech: the renderer now
generates roles/names for charts, metric tiles, tables and timelines, and
passes through author-supplied ``aria-*``/``role`` entries from the
``attributes`` escape hatch — while STILL refusing every non-whitelisted
attribute (escape-by-default, FR-017). Components are plain dicts so the
suite is independent of the installed astralprims version.
"""
import pathlib

import webrender
from webrender import render_one


# ---------------------------------------------------------------------------
# (1) attributes whitelist — aria-*/role pass-through, everything else refused
# ---------------------------------------------------------------------------

def test_aria_attributes_pass_through():
    html = render_one({"type": "badge", "label": "Open",
                       "attributes": {"aria-label": "Status: open", "aria-describedby": "x1"}})
    assert 'aria-label="Status: open"' in html
    assert 'aria-describedby="x1"' in html


def test_aria_key_case_normalized_and_malformed_aria_keys_refused():
    html = render_one({"type": "badge", "label": "x", "attributes": {
        "ARIA-LABEL": "named", "aria-label-two": "refused", "ariafake": "refused"}})
    assert 'aria-label="named"' in html
    assert "refused" not in html


def test_role_allowlisted_values_pass_through():
    for role in ("img", "list", "listitem", "status", "note", "group", "region"):
        html = render_one({"type": "card", "title": "T", "content": [],
                           "attributes": {"role": role}})
        assert f' role="{role}"' in html, role


def test_role_outside_allowlist_is_dropped():
    for bad in ("button", "menuitem", "alertdialog", '"><script>x</script>', "", None):
        html = render_one({"type": "card", "title": "T", "content": [],
                           "attributes": {"role": bad}})
        assert " role=" not in html, repr(bad)
        assert "<script" not in html.lower()


def test_role_value_case_and_whitespace_normalized():
    html = render_one({"type": "card", "title": "T", "content": [],
                       "attributes": {"role": "  IMG "}})
    assert ' role="img"' in html


def test_non_whitelisted_attributes_still_refused():
    html = render_one({"type": "badge", "label": "ok", "attributes": {
        "onclick": "alert(1)", "style": "position:fixed", "src": "https://evil.example",
        "href": "javascript:alert(1)", "onmouseover": "x", "class": "evil",
    }})
    for needle in ("onclick", "style=", "src=", "href=", "onmouseover", "evil"):
        assert needle not in html, needle


def test_data_ui_dispatch_family_is_refused():
    """client.js delegates on ``[data-ui-action]`` document-wide with no class
    and no container scoping, so an author-supplied one would be a one-click
    trigger for an arbitrary chrome action fired as the viewing user."""
    html = render_one({"type": "card", "title": "Quarterly report", "content": [],
                       "attributes": {"data-ui-action": "chrome_perms_save",
                                      "data-ui-payload": '{"agent_id":"x"}',
                                      "data-ui-collect": "true",
                                      "data-ui-form": "1"}})
    for needle in ("data-ui-action", "data-ui-payload", "data-ui-collect",
                   "data-ui-form", "chrome_perms_save"):
        assert needle not in html, needle


def test_data_ui_refusal_covers_both_wire_shapes_and_key_case():
    # astralprims' to_dict() merges `attributes` at the TOP level, so the
    # refusal must hold for the flattened shape too, and keys are lowercased
    # before the whitelist runs.
    flat = render_one({"type": "badge", "label": "x",
                       "DATA-UI-ACTION": "chrome_agent_enabled"})
    nested = render_one({"type": "badge", "label": "x",
                         "attributes": {"Data-Ui-Action": "chrome_agent_enabled"}})
    for html in (flat, nested):
        assert "chrome_agent_enabled" not in html
        assert "data-ui-action" not in html.lower()


def test_morph_anchor_data_attribute_still_passes_through():
    # The designer's nested-ref anchor is the one data-* key the product sets
    # through this path; narrowing must not break it.
    html = render_one({"type": "table", "headers": ["A"], "rows": [["1"]],
                       "attributes": {"data-component-id": "wc_anchor",
                                      "data-ui-action": "chrome_open"}})
    assert 'data-component-id="wc_anchor"' in html
    assert "chrome_open" not in html


def test_hostile_aria_value_is_attribute_escaped():
    html = render_one({"type": "badge", "label": "x",
                       "attributes": {"aria-label": '"><script>alert(1)</script>'}})
    assert "<script" not in html
    assert 'aria-label="&quot;&gt;&lt;script&gt;' in html


# ---------------------------------------------------------------------------
# (7) buttons — orchestrator-supplied aria-labels flow through the whitelist
# ---------------------------------------------------------------------------

def test_button_passes_through_supplied_aria_label():
    html = render_one({"type": "button", "label": "Go", "action": "chat_message",
                       "payload": {}, "attributes": {"aria-label": "Send example: weather"}})
    assert html.startswith("<button") and 'aria-label="Send example: weather"' in html
    # dispatch contract intact for client.js
    assert 'data-action="chat_message"' in html and 'class="astral-action' in html


def test_button_attributes_cannot_retarget_dispatch():
    html = render_one({"type": "button", "label": "Go", "action": "real",
                       "payload": {}, "attributes": {"data-action": "fake"}})
    # the renderer's own data-action is emitted FIRST; HTML keeps the first
    # occurrence of a duplicated attribute, so dispatch cannot be retargeted
    assert html.index('data-action="real"') < html.index('data-action="fake"')


# ---------------------------------------------------------------------------
# (2) charts — role="img", generated aria-label, sr-only data fallback
# ---------------------------------------------------------------------------

def _bar(title=None, data=(3, 21, 7, 16)):
    c = {"type": "bar_chart", "labels": [str(i) for i in range(len(data))],
         "datasets": [{"label": "d", "data": list(data)}]}
    if title:
        c["title"] = title
    return c


def test_chart_role_img_and_title_aria_label():
    html = render_one(_bar(title="Patient Accrual"))
    assert 'role="img"' in html
    assert 'aria-label="Bar chart: Patient Accrual"' in html


def test_chart_without_title_falls_back_to_kind_plus_summary():
    html = render_one(_bar())
    assert 'aria-label="Bar chart: 4 data points, range 3 to 21"' in html


def test_chart_sr_only_fallback_outside_img_node():
    html = render_one(_bar(title="Accrual"))
    # the sr-only summary must sit OUTSIDE the role="img" element (children
    # of role="img" are presentational to AT) — i.e. after the chart div
    assert 'style="min-height:320px"></div><span class="astral-sr-only">4 data points, range 3 to 21</span>' in html


def test_line_pie_plotly_labels_and_summaries():
    line = render_one({"type": "line_chart", "labels": ["a"], "datasets": [{"data": [1.5]}]})
    assert 'aria-label="Line chart: 1 data point, range 1.5 to 1.5"' in line
    pie = render_one({"type": "pie_chart", "title": "Mix", "labels": ["a", "b"], "data": [60, 40]})
    assert 'aria-label="Pie chart: Mix"' in pie
    assert '<span class="astral-sr-only">2 data points, range 40 to 60</span>' in pie
    plotly = render_one({"type": "plotly_chart", "data": [{"y": [1]}, {"y": [2]}]})
    assert 'aria-label="Chart: 2 data series"' in plotly
    assert '<span class="astral-sr-only">2 data series</span>' in plotly


def test_chart_summary_non_numeric_data_omits_range():
    html = render_one({"type": "pie_chart", "labels": ["a", "b"], "data": ["x", "y"]})
    assert "2 data points<" in html and "range" not in html


def test_chart_summary_skips_bools_and_overflowing_ints():
    html = render_one({"type": "pie_chart", "labels": ["a", "b", "c"], "data": [True, 10**400, 5]})
    assert "3 data points, range 5 to 5" in html


def test_chart_title_escaped_inside_aria_label():
    html = render_one(_bar(title='"><img onerror=x>'))
    assert "<img" not in html
    assert 'aria-label="Bar chart: &quot;&gt;&lt;img onerror=x&gt;"' in html


# ---------------------------------------------------------------------------
# (3) metric tiles — generated "<label>: <value>" name
# ---------------------------------------------------------------------------

def test_metric_tile_aria_label():
    html = render_one({"type": "metric", "title": "Enrolled", "value": "128"})
    assert 'aria-label="Enrolled: 128"' in html


def test_metric_without_title_uses_value_only():
    html = render_one({"type": "metric", "value": 42})
    assert 'aria-label="42"' in html


def test_metric_author_supplied_aria_label_wins_without_duplicate():
    html = render_one({"type": "metric", "title": "Enrolled", "value": "128",
                       "attributes": {"aria-label": "Custom name"}})
    assert html.count("aria-label=") == 1
    assert 'aria-label="Custom name"' in html


def test_metric_aria_label_escaped():
    html = render_one({"type": "metric", "title": 'A"B', "value": "<1>"})
    assert 'aria-label="A&quot;B: &lt;1&gt;"' in html


# ---------------------------------------------------------------------------
# (4) tables — th scope, title as accessible name
# ---------------------------------------------------------------------------

def test_table_headers_carry_scope_col():
    html = render_one({"type": "table", "headers": ["A", "B"], "rows": [["1", "2"]]})
    assert html.count('<th scope="col"') == 2


def test_table_title_becomes_table_aria_label():
    html = render_one({"type": "table", "title": "Enrollment by site",
                       "headers": ["A"], "rows": [["1"]]})
    assert '<table class="w-full text-sm" aria-label="Enrollment by site">' in html


def test_table_without_explicit_title_gets_no_aria_label():
    html = render_one({"type": "table", "headers": ["A"], "rows": [["1"]]})
    assert "aria-label" not in html


def test_markdown_table_headers_carry_scope_col():
    from webrender.sanitize import block_md
    out = block_md("| A | B |\n|---|---|\n| 1 | 2 |")
    assert out.count('<th scope="col"') == 2


# ---------------------------------------------------------------------------
# (5) timeline — explicit list semantics
# ---------------------------------------------------------------------------

def test_timeline_explicit_list_role():
    html = render_one({"type": "timeline", "items": [{"title": "a"}, {"title": "b"}]})
    # real <ol>/<li> markup, plus role="list" because astral.css strips the
    # implicit list role with list-style:none (WebKit/VoiceOver)
    assert '<ol class="astral-tl-list space-y-3" role="list">' in html
    assert html.count("<li ") == 2


# ---------------------------------------------------------------------------
# (6) headings sane + decorative icons hidden
# ---------------------------------------------------------------------------

def test_hero_h2_and_card_h3_not_regressed():
    hero = render_one({"type": "hero", "title": "T"})
    assert "<h2 " in hero and "<h1" not in hero
    card = render_one({"type": "card", "title": "T", "content": []})
    assert "<h3 " in card


def test_decorative_icons_are_aria_hidden():
    hero = render_one({"type": "hero", "title": "T", "icon": "\U0001f43e"})
    assert '<span class="astral-hero-icon text-3xl mr-3" aria-hidden="true">' in hero
    badge = render_one({"type": "badge", "label": "x", "icon": "✔"})
    assert '<span class="astral-badge-icon" aria-hidden="true">' in badge


# ---------------------------------------------------------------------------
# (2) stylesheet — the sr-only utility actually exists
# ---------------------------------------------------------------------------

def test_sr_only_utility_defined_in_stylesheet():
    css_path = pathlib.Path(webrender.__file__).parent / "static" / "astral.css"
    css = css_path.read_text(encoding="utf-8")
    assert ".astral-sr-only" in css
    assert "clip: rect(0, 0, 0, 0)" in css
