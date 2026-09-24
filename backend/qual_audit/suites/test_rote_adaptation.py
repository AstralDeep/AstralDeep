"""Tests that AstralProjection's ComponentAdapter (rote/adapter.py) transforms tables,
charts, grids, code blocks, and buttons correctly across the six device profiles,
from full passthrough on browser to text-only on voice.
"""

from rote.adapter import ComponentAdapter


TABLE_10COL = {
    "type": "table",
    "headers": [f"Col{i}" for i in range(10)],
    "rows": [[f"r{r}c{c}" for c in range(10)] for r in range(25)],
}

BAR_CHART = {
    "type": "bar_chart",
    "title": "Test Results",
    "labels": ["A", "B", "C"],
    "datasets": [{"label": "Series 1", "data": [10, 20, 30], "color": "#f00"}],
}

FOUR_COL_GRID = {
    "type": "grid",
    "columns": 4,
    "children": [
        {"type": "text", "content": f"Item {i}", "variant": "body"}
        for i in range(4)
    ],
}

CODE_BLOCK = {
    "type": "code",
    "code": "print('hello world')",
    "language": "python",
}

BUTTON_PRIMARY = {
    "type": "button",
    "label": "Submit",
    "action": "submit",
    "variant": "primary",
    "payload": {},
}

BUTTON_SECONDARY = {
    "type": "button",
    "label": "Cancel",
    "action": "cancel",
    "variant": "secondary",
    "payload": {},
}

COMPLEX_LAYOUT = {
    "type": "card",
    "title": "Dashboard",
    "content": [
        {"type": "text", "content": "This is a long description " * 20, "variant": "body"},
        TABLE_10COL,
        BAR_CHART,
    ],
}


class TestROTEAdaptation:
    def test_table_mobile_truncation(self, mobile_profile):
        result = ComponentAdapter.adapt([TABLE_10COL], mobile_profile)
        assert len(result) == 1
        table = result[0]
        assert table["type"] == "table"
        assert len(table["headers"]) <= 4
        assert len(table["rows"]) <= 20

    def test_chart_watch_degradation(self, watch_profile):
        result = ComponentAdapter.adapt([BAR_CHART], watch_profile)
        assert len(result) == 1
        comp = result[0]
        assert comp["type"] == "metric", f"Expected metric, got {comp['type']}"
        assert "title" in comp
        assert "value" in comp

    def test_voice_text_extraction(self, voice_profile):
        result = ComponentAdapter.adapt([COMPLEX_LAYOUT], voice_profile)
        assert len(result) >= 1
        for comp in result:
            assert comp["type"] == "text", f"Expected text, got {comp['type']}"
            assert len(comp.get("content", "")) <= 300

    def test_grid_collapse_mobile(self, mobile_profile):
        result = ComponentAdapter.adapt([FOUR_COL_GRID], mobile_profile)
        assert len(result) == 1
        comp = result[0]
        assert comp["type"] == "container"
        assert "children" in comp

    def test_code_block_mobile_removed(self, mobile_profile):
        result = ComponentAdapter.adapt([CODE_BLOCK], mobile_profile)
        assert len(result) == 0, "Code block should be removed on mobile"

    def test_browser_passthrough(self, browser_profile):
        components = [TABLE_10COL, BAR_CHART, FOUR_COL_GRID, CODE_BLOCK]
        result = ComponentAdapter.adapt(components, browser_profile)
        assert len(result) == len(components)
        assert len(result[0]["headers"]) == 10
        assert result[1]["type"] == "bar_chart"
        assert result[2]["columns"] == 4
        assert result[3]["type"] == "code"

    def test_button_tv_removed(self, tv_profile):
        result = ComponentAdapter.adapt([BUTTON_PRIMARY, BUTTON_SECONDARY], tv_profile)
        assert len(result) == 0, "Buttons should be removed on TV (read-only)"

    def test_table_tablet_column_limit(self, tablet_profile):
        result = ComponentAdapter.adapt([TABLE_10COL], tablet_profile)
        assert len(result) == 1
        table = result[0]
        assert table["type"] == "table"
        assert len(table["headers"]) <= 6
        for row in table["rows"]:
            assert len(row) <= 6
