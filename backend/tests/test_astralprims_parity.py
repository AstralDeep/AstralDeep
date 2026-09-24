"""Tests that astralprims/__init__.py's primitive catalog covers every type and field
the product uses, including the style→css rename, cross-checked dynamically against
the legacy shared.primitives module while it still exists.
"""

import importlib

import pytest

import astralprims
from astralprims import Primitive, create_ui_response


EXPECTED = {
    "container": ("Container", {"children", "direction"}),
    "text": ("Text", {"content", "variant"}),
    "button": ("Button", {"label", "action", "payload", "variant"}),
    "input": ("Input", {"placeholder", "name", "value"}),
    "param_picker": ("ParamPicker", {"title", "description", "fields", "submit_label", "submit_message_template"}),
    "card": ("Card", {"title", "content", "variant"}),
    "table": ("Table", {"headers", "rows", "variant", "total_rows", "page_size",
                         "page_offset", "page_sizes", "source_tool", "source_agent", "source_params"}),
    "list": ("List_", {"items", "ordered", "variant"}),
    "alert": ("Alert", {"message", "variant", "title"}),
    "progress": ("ProgressBar", {"value", "label", "variant", "show_percentage"}),
    "metric": ("MetricCard", {"title", "value", "subtitle", "icon", "variant", "progress"}),
    "code": ("CodeBlock", {"code", "language", "show_line_numbers"}),
    "image": ("Image", {"url", "alt", "width", "height"}),
    "grid": ("Grids", {"columns", "children", "gap"}),
    "tabs": ("Tabs", {"tabs", "variant"}),
    "collapsible": ("Collapsible", {"title", "content", "default_open"}),
    "divider": ("Divider", {"variant"}),
    "bar_chart": ("BarChart", {"title", "labels", "datasets"}),
    "line_chart": ("LineChart", {"title", "labels", "datasets"}),
    "pie_chart": ("PieChart", {"title", "labels", "data", "colors"}),
    "plotly_chart": ("PlotlyChart", {"title", "data", "layout", "config"}),
    "color_picker": ("ColorPicker", {"label", "color_key", "value"}),
    "theme_apply": ("ThemeApply", {"preset", "colors", "color_key", "color_value", "message"}),
    "file_upload": ("FileUpload", {"label", "accept", "action"}),
    "file_download": ("FileDownload", {"label", "url", "filename"}),
    "audio": ("Audio", {"src", "contentType", "autoplay", "loop", "label", "showControls", "description"}),
}


EXPECTED_0_2 = {
    "badge": ("Badge", {"label", "variant", "icon"}),
    "hero": ("Hero", {"title", "subtitle", "eyebrow", "icon", "variant", "badges"}),
    "keyvalue": ("KeyValue", {"title", "items", "columns"}),
    "timeline": ("Timeline", {"title", "items", "variant"}),
    "rating": ("Rating", {"value", "max_value", "label", "subtitle", "show_value"}),
}

_HAS_0_2 = all(hasattr(astralprims, cls) for cls, _ in EXPECTED_0_2.values())
needs_astralprims_0_2 = pytest.mark.skipif(
    not _HAS_0_2, reason="astralprims < 0.2.0 installed — dashboard primitives ship with 0.2.0"
)


@needs_astralprims_0_2
@pytest.mark.parametrize("type_name", sorted(EXPECTED_0_2))
def test_astralprims_exposes_dashboard_type_and_fields(type_name):
    cls_name, fields = EXPECTED_0_2[type_name]
    cls = getattr(astralprims, cls_name)
    inst = cls()
    assert inst.to_dict()["type"] == type_name
    missing = fields - set(cls.model_fields.keys())
    assert not missing, f"{cls_name} missing fields: {missing}"


@needs_astralprims_0_2
def test_from_dict_roundtrips_dashboard_types():
    for type_name in EXPECTED_0_2:
        rebuilt = Primitive.from_dict({"type": type_name})
        assert rebuilt.to_dict()["type"] == type_name


@pytest.mark.parametrize("type_name", sorted(EXPECTED))
def test_astralprims_exposes_type_and_fields(type_name):
    cls_name, fields = EXPECTED[type_name]
    cls = getattr(astralprims, cls_name)
    inst = cls()
    assert inst.to_dict()["type"] == type_name
    model_fields = set(cls.model_fields.keys())
    missing = fields - model_fields
    assert not missing, f"{cls_name} missing fields: {missing}"


def test_from_dict_roundtrips_every_type():
    for type_name in EXPECTED:
        d = {"type": type_name}
        rebuilt = Primitive.from_dict(d)
        assert rebuilt.to_dict()["type"] == type_name


def test_base_fields_present():
    fields = set(astralprims.Text.model_fields.keys())
    assert {"css", "id", "tooltip", "attributes"}.issubset(fields)


def test_create_ui_response_envelope():
    env = create_ui_response([astralprims.Text(content="hi"), astralprims.Button(label="ok", action="go")])
    assert set(env.keys()) == {"_ui_components", "_data"}
    assert env["_data"] is None
    assert [c["type"] for c in env["_ui_components"]] == ["text", "button"]


def test_table_pagination_roundtrip():
    t = astralprims.Table(
        headers=["a", "b"], rows=[[1, 2]], total_rows=100, page_size=20,
        page_offset=0, page_sizes=[10, 20], source_tool="tool", source_agent="agent",
        source_params={"x": 1},
    )
    d = t.to_dict()
    for k in ("total_rows", "page_size", "page_offset", "page_sizes", "source_tool", "source_agent", "source_params"):
        assert k in d, f"Table lost pagination field {k}"


def test_dynamic_crosscheck_against_legacy_module_if_present():
    try:
        legacy = importlib.import_module("shared.primitives")
    except Exception:
        pytest.skip("legacy shared.primitives removed (post-cutover) — durable catalog test covers parity")

    base_fields = {"type", "id", "style", "tooltip", "css", "class_name", "attributes"}
    import dataclasses
    legacy_types = {}
    for name in dir(legacy):
        obj = getattr(legacy, name)
        if dataclasses.is_dataclass(obj):
            try:
                inst = obj()
                t = getattr(inst, "type", None)
            except Exception:
                continue
            if isinstance(t, str) and t and t != "primitive":
                legacy_fields = {fld.name for fld in dataclasses.fields(obj)} - base_fields
                legacy_types[t] = legacy_fields

    for t, legacy_fields in legacy_types.items():
        assert t in EXPECTED, f"legacy type {t!r} not covered by astralprims catalog"
        cls = getattr(astralprims, EXPECTED[t][0])
        ap_fields = set(cls.model_fields.keys())
        missing = legacy_fields - ap_fields
        assert not missing, f"type {t!r}: astralprims missing legacy fields {missing}"
