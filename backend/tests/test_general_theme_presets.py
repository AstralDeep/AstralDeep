"""Tests for agents/general/mcp_tools.py's theme tools: the Graphite-first preset table,
its use as the default shown in the customization card, and the registry text naming it.
"""

from agents.general import mcp_tools

GRAPHITE = {
    "bg": "#0B0B0D",
    "surface": "#151518",
    "primary": "#7C3AED",
    "secondary": "#6E6E78",
    "text": "#ECECEF",
    "muted": "#8C8C95",
    "accent": "#A78BFA",
}
MIDNIGHT = {
    "bg": "#0F1221",
    "surface": "#1A1E2E",
    "primary": "#6366F1",
    "secondary": "#8B5CF6",
    "text": "#F3F4F6",
    "muted": "#9CA3AF",
    "accent": "#06B6D4",
}
ORDER = ["graphite", "midnight", "daylight", "ocean", "sunset", "forest"]


def _pickers(component):
    found = []

    def visit(value):
        if isinstance(value, dict):
            if value.get("type") == "color_picker":
                found.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(component)
    return {picker["color_key"]: picker["value"] for picker in found}


def _grid(component):
    return next(item for item in component["content"] if item["type"] == "grid")


def test_graphite_leads_the_preset_table_and_midnight_keeps_its_values():
    assert list(mcp_tools.THEME_PRESETS) == ORDER
    assert mcp_tools.THEME_PRESETS["graphite"] == GRAPHITE
    assert mcp_tools.THEME_PRESETS["midnight"] == MIDNIGHT


def test_the_customization_card_defaults_to_graphite_with_one_button_per_preset():
    result = mcp_tools.change_theme()
    card = result["_ui_components"][0]

    assert _pickers(card) == GRAPHITE
    grid = _grid(card)
    assert grid["columns"] == len(ORDER)
    assert [child["payload"]["name"] for child in grid["children"]] == ORDER
    assert [child["label"] for child in grid["children"]][0] == "Graphite"
    assert result["_data"]["presets"] == ORDER


def test_the_customization_card_shows_the_requested_preset_and_ignores_unknown_names():
    assert _pickers(mcp_tools.change_theme("midnight")["_ui_components"][0]) == MIDNIGHT
    assert _pickers(mcp_tools.change_theme("chartreuse")["_ui_components"][0]) == GRAPHITE


def test_apply_theme_preset_applies_graphite_and_midnight_by_name():
    graphite = mcp_tools.apply_theme_preset(" Graphite ")
    assert graphite["_data"] == {
        "applied_preset": "graphite",
        "colors": GRAPHITE,
        "message": "Theme changed to Graphite",
    }
    assert graphite["_ui_components"][0]["type"] == "theme_apply"
    assert graphite["_ui_components"][0]["preset"] == "graphite"
    assert _pickers(graphite["_ui_components"][1]) == GRAPHITE

    midnight = mcp_tools.apply_theme_preset("midnight")
    assert midnight["_data"]["colors"] == MIDNIGHT
    assert _pickers(midnight["_ui_components"][1]) == MIDNIGHT


def test_apply_theme_preset_rejects_unknown_names_and_lists_graphite_first():
    result = mcp_tools.apply_theme_preset("chartreuse")

    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "Available: graphite, midnight" in alert["message"]


def test_the_tool_registry_names_graphite_in_its_enum_and_descriptions():
    apply_spec = mcp_tools.TOOL_REGISTRY["apply_theme_preset"]
    assert apply_spec["input_schema"]["properties"]["preset"]["enum"] == ORDER
    assert "graphite" in apply_spec["description"]
    change_spec = mcp_tools.TOOL_REGISTRY["change_theme"]
    assert "graphite" in change_spec["input_schema"]["properties"]["preset"]["description"]
