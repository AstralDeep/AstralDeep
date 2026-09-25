"""Checks lossless completed-canvas consolidation and preserved reasoning transcripts.
Fixtures cover duplicate tool previews, distinct datasets, controls, and server seams.
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest

from orchestrator.canvas_consolidation import consolidate_canvas
from orchestrator.history import _content_parts, _rail_parts, augment_conversation_snapshot_for_target
from orchestrator.orchestrator import Orchestrator


def table(title="Monthly spend", rows=None, **values):
    return {"type": "table", "title": title, "headers": ["Month", "Amount"],
            "rows": rows if rows is not None else [["Jan", "100"], ["Feb", "200"]], **values}


def walk(nodes):
    for node in nodes:
        if not isinstance(node, dict):
            continue
        yield node
        for key in ("content", "children"):
            if isinstance(node.get(key), list):
                yield from walk(node[key])
        for tab in node.get("tabs", []):
            if isinstance(tab, dict):
                yield from walk(tab.get("content", []))


def test_final_dashboard_keeps_one_table_downloads_warning_and_source():
    original = table(component_id="wc_table", _source_agent="office", _source_tool="excel_generate",
                     _source_params={"title": "Monthly spend"}, _source_correlation_id="call-1")
    download = {"type": "file_download", "url": "/api/download/a/result.xlsx", "filename": "result.xlsx"}
    warning = {"type": "alert", "message": "Sample data only", "variant": "warning"}
    button = {"type": "button", "action": "refresh", "payload": {"id": "a"}}
    dashboard = {"type": "card", "title": "Grant dashboard", "content": [table(), warning, button]}
    components = [original, download, dashboard]
    before = copy.deepcopy(components)
    result = consolidate_canvas(components)
    assert components == before
    assert result == [original, download, {**dashboard, "content": [warning, button]}]
    assert consolidate_canvas(result) == result


def test_known_projected_preview_is_removed_but_workbook_control_stays():
    preview = {"type": "table", "id": "modify-data-preview", "headers": ["Month"], "rows": [["Jan"]]}
    download = {"type": "file_download", "url": "/api/download/a/workbook.xlsx"}
    status = {"type": "alert", "message": "Workbook created", "variant": "success"}
    card = {"type": "card", "title": "Data Modified Successfully", "_source_agent": "general",
            "_source_tool": "general__modify_data", "content": [status, download, preview]}
    result = consolidate_canvas([card, table()])
    assert result[0]["content"] == [status, download]
    assert result[1] == table()


def test_preview_status_title_does_not_prevent_final_workbook_consolidation():
    detail = table(title="", component_id="detail", _source_agent="office", _source_tool="excel_generate")
    summary = {"type": "table", "component_id": "summary", "headers": ["Grant", "Budget", "% of YTD Budget Spent"],
               "rows": [["A", "100", "90%"], ["B", "200", "80%"]], "_source_agent": "office", "_source_tool": "excel_generate"}
    preview_cards = []
    for full, filename in ((detail, "detail.xlsx"), (summary, "summary.xlsx")):
        preview_cards.append({"type": "card", "id": "modify-data-card", "title": "Data Modified Successfully",
                              "_source_agent": "general", "_source_tool": "modify_data", "content": [
                                  {"type": "file_download", "url": f"/api/download/sample/{filename}", "filename": filename},
                                  {"type": "table", "id": "modify-data-preview", "headers": full["headers"][:1],
                                   "rows": [row[:1] for row in full["rows"][:1]]}]})
    final_detail = table(title="Sheet 1 — Monthly Spend Detail")
    final_summary = {"type": "table", "title": "Sheet 2 — Summary by Grant", "headers": ["Grant", "Budget", "% of YTD Spent"], "rows": summary["rows"]}
    components = [detail, summary, *preview_cards, {"type": "card", "title": "Grant workbook", "content": [final_detail, final_summary]}]
    result = consolidate_canvas(components)
    tables = [node for node in walk(result) if node.get("type") == "table"]
    downloads = [node for node in walk(result) if node.get("type") == "file_download"]
    assert len(tables) == 2
    assert [node["component_id"] for node in tables] == ["detail", "summary"]
    assert [node["title"] for node in tables] == [final_detail["title"], final_summary["title"]]
    assert [node["filename"] for node in downloads] == ["detail.xlsx", "summary.xlsx"]
    assert consolidate_canvas(result) == result


def test_spreadsheet_tool_and_dashboard_tool_exact_copies_keep_spreadsheet_identity():
    detail = table(title="", component_id="original-detail", _source_agent="connectors-1", _source_tool="excel_generate",
                   _source_params={"title": "Monthly spend", "description": "Monthly source", "columns": ["Month", "Amount"], "rows": [["Jan", "100"], ["Feb", "200"]]})
    summary = {"type": "table", "component_id": "original-summary", "headers": ["Grant", "Budget", "% of YTD Budget Spent"],
               "rows": [["A", "100", "90%"], ["B", "200", "80%"]], "_source_agent": "connectors-1", "_source_tool": "excel_generate",
               "_source_params": {"title": "Summary", "description": "Summary source", "columns": ["Grant", "Budget", "% of YTD Budget Spent"], "rows": [["A", "100", "90%"], ["B", "200", "80%"]]}}
    final_detail = table(title="Sheet 1 — Monthly Spend Detail", component_id="dashboard-detail", _source_agent="connectors-1",
                         _source_tool="interactive_artifacts", _source_params={"title": "Workbook", "subtitle": "Comparison", "sections": [{"kind": "table"}]})
    final_summary = {**summary, "title": "Sheet 2 — Summary by Grant", "component_id": "dashboard-summary", "headers": ["Grant", "Budget", "% of YTD Spent"],
                     "_source_tool": "interactive_artifacts", "_source_params": final_detail["_source_params"]}
    download = {"type": "file_download", "filename": "grant.xlsx", "url": "/api/download/sample/grant.xlsx"}
    result = consolidate_canvas([detail, summary, download, final_detail, final_summary])
    tables = [node for node in walk(result) if node.get("type") == "table"]
    assert len(tables) == 2
    assert [node["component_id"] for node in tables] == ["original-detail", "original-summary"]
    assert [node["_source_tool"] for node in tables] == ["excel_generate", "excel_generate"]
    assert tables[0]["_source_params"] == detail["_source_params"]
    assert tables[1]["_source_params"] == summary["_source_params"]
    assert download in result
    assert consolidate_canvas(result) == result


@pytest.mark.parametrize("change", [{"rows": [["Jan", "999"], ["Feb", "200"]]}, {"rows": [["Jan", "100"]]}, {"_source_agent": "other-agent"}, {"_source_tool": "other_tool"}])
def test_dashboard_copy_rule_does_not_merge_different_data_or_unrelated_tools(change):
    original = table(_source_agent="connectors-1", _source_tool="excel_generate")
    dashboard = table(_source_agent="connectors-1", _source_tool="interactive_artifacts")
    dashboard.update(change)
    assert consolidate_canvas([original, dashboard]) == [original, dashboard]


@pytest.mark.parametrize("change", [
    {"title": "Different grant"}, {"rows": [["Jan", "999"], ["Feb", "200"]]},
    {"rows": [["Jan", "100"], ["Jan", "100"]]},
    {"caption": "Values exclude indirect costs"}, {"headers": ["Month", "Balance"]},
    {"action": "refresh"}, {"editable": False}, {"selectable": True},
])
def test_distinct_meaning_counts_or_interaction_is_preserved(change):
    original = [table(), table(**change)]
    assert consolidate_canvas(original) == original


def test_distinct_parent_context_is_preserved():
    components = [{"type": "card", "title": title, "content": [table(title="")]} for title in ("Grant A", "Grant B")]
    assert consolidate_canvas(components) == components


def test_distinct_tool_datasets_and_sources_are_preserved():
    first = table(_source_agent="office", _source_tool="query", _source_params={"region": "east"})
    second = table(_source_agent="office", _source_tool="query", _source_params={"region": "west"})
    third = table(_source_agent="other", _source_tool="query")
    assert consolidate_canvas([first, second, third]) == [first, second, third]


def test_nonpreview_row_subsets_and_repeated_rows_remain_distinct():
    full = table(rows=[["Jan", "100"], ["Jan", "100"], ["Feb", "200"]])
    partial = table(rows=[["Jan", "100"], ["Jan", "100"]])
    result = consolidate_canvas([full, partial])
    assert result == [full, partial]


def test_missing_columns_are_not_removed_except_known_previews():
    partial = {"type": "table", "title": "Monthly spend", "headers": ["Month"], "rows": [["Jan"], ["Feb"]]}
    assert consolidate_canvas([partial, table()]) == [partial, table()]


def test_column_reordering_is_compared_by_header():
    reordered = {"type": "table", "title": "Monthly spend", "headers": ["Amount", "Month"],
                 "rows": [["100", "Jan"], ["200", "Feb"]]}
    assert consolidate_canvas([table(), reordered]) == [reordered]


def test_abbreviated_summary_header_keeps_more_specific_wording():
    full = {"type": "table", "headers": ["Grant ID", "Budget", "% of YTD Budget Spent"], "rows": [["A", "100", "94.7%"]]}
    final = {"type": "table", "title": "Sheet 2 — Summary by Grant", "headers": ["Grant ID", "Budget", "% of YTD Spent"], "rows": [["A", "100", "94.7%"]]}
    assert consolidate_canvas([full, final]) == [{**final, "headers": full["headers"]}]


@pytest.mark.parametrize("headers", [
    ["Grant ID", "Budget", "% of YTD Remaining"],
    ["Grant ID", "Budget", "% Spent"],
    ["Grant ID", "YTD Budget", "% of YTD Spent"],
])
def test_different_or_ambiguous_summary_headers_are_not_merged(headers):
    full = {"type": "table", "headers": ["Grant ID", "Budget", "% of YTD Budget Spent"], "rows": [["A", "100", "94.7%"]]}
    other = {**full, "headers": headers}
    assert consolidate_canvas([full, other]) == [full, other]


@pytest.mark.parametrize("headers", [
    ["Serum sodium mmol", "Serum sodium mmol per liter"],
    ["Mean blood pressure", "Mean blood pressure after treatment"],
])
def test_measurement_qualifiers_are_never_interpreted_as_header_aliases(headers):
    left = table()
    right = table()
    left["headers"][1], right["headers"][1] = headers
    assert consolidate_canvas([left, right]) == [left, right]


def test_equal_numbers_and_strings_display_once():
    numeric = table(rows=[["Jan", 100], ["Feb", 200]])
    assert consolidate_canvas([numeric, table()]) == [table()]


@pytest.mark.parametrize("invalid", [
    {"headers": []}, {"headers": ["Amount", "Amount"]}, {"headers": [""]},
    {"headers": [{"label": "Month"}]}, {"rows": []}, {"rows": "bad"},
    {"rows": [["Jan"]]}, {"rows": [["Jan", {"value": 100}]]},
    {"rows": [["Jan", float("nan")]]},
])
def test_unsupported_tables_are_left_for_normal_validation(invalid):
    original = table(**invalid)
    result = consolidate_canvas([original, table()])
    assert len(result) == 2


def test_recursive_layouts_prune_only_empty_wrappers_and_keep_tabs():
    components = [{"type": "grid", "children": [{"type": "card", "content": [table()]}]},
                  {"type": "tabs", "tabs": [{"label": "Spend", "content": [table()]}]}]
    assert consolidate_canvas(components) == [components[1]]


def test_wrapper_action_is_preserved_when_duplicate_table_is_removed():
    wrapper = {"type": "card", "action": "refresh", "payload": {"target": "original"}, "content": [table()]}
    assert consolidate_canvas([wrapper, table()]) == [{**wrapper, "content": []}, table()]


def test_untitled_winner_retains_title():
    result = consolidate_canvas([table(), table(title="")])
    assert result == [table()]


def test_hidden_source_title_does_not_block_richer_final_table():
    original = table(title="", _source_params={"title": "Monthly spend"})
    result = consolidate_canvas([original, table(title="Sheet 1 — Monthly spend")])
    assert result[0]["title"] == "Sheet 1 — Monthly spend"


def test_source_action_identity_stays_at_original_location_with_final_title():
    original = table(title="", component_id="stored-tool-table", _source_agent="office", _source_tool="excel_generate", _source_params={"title": "Monthly"})
    final = table(title="Sheet 1 — Monthly Spend", component_id="generated-table")
    dashboard = {"type": "card", "component_id": "dashboard", "content": [final]}
    result = consolidate_canvas([original, dashboard])
    assert result == [{**original, "title": final["title"]}]
    assert [c["component_id"] for c in walk(result) if c.get("type") == "table"] == ["stored-tool-table"]
    assert consolidate_canvas(result) == result


def test_source_without_stored_identity_is_not_lost_or_transplanted():
    original = table(_source_agent="office", _source_tool="excel_generate")
    assert consolidate_canvas([original, table()]) == [original]


def test_source_identity_survives_three_duplicate_presentations():
    original = table(component_id="stored", _source_agent="office", _source_tool="excel_generate")
    result = consolidate_canvas([original, table(component_id="second"), table(component_id="third")])
    assert result == [original]


def test_independent_actions_and_distinct_downloads_remain_while_exact_downloads_merge():
    controls = [{"type": "file_download", "url": "/first.xlsx"},
                {"type": "file_download", "url": "/second.xlsx"},
                {"type": "file_download", "url": "/first.xlsx"},
                {"type": "button", "action": "run", "payload": {"target": "a"}},
                {"type": "button", "action": "run", "payload": {"target": "b"}}]
    assert consolidate_canvas(controls) == [controls[0], controls[1], controls[3], controls[4]]


@pytest.mark.parametrize("size", ["tables", "nodes", "cells", "depth"])
def test_bounds_return_original_presentation(size):
    if size == "tables":
        original = [table()] * 129
    elif size == "nodes":
        original = [{"type": "text", "content": "x"}] * 4001
    elif size == "cells":
        original = [table(rows=[["Jan", "100"]] * 50001), table()]
    else:
        original = [table()]
        for _ in range(42):
            original = [{"type": "card", "content": original}]
    assert consolidate_canvas(original) == original


@pytest.mark.parametrize("nested", [False, True])
def test_reasoning_remains_collapsed_component_in_completed_transcript(nested):
    reasoning = {"type": "collapsible", "title": "Reasoning", "default_open": False,
                 "content": [{"type": "text", "content": "Private model details", "variant": "markdown"}]}
    content = [{"type": "card", "content": [{"type": "text", "content": "Answer"}, reasoning]}] if nested else [reasoning]
    parts = _rail_parts(_content_parts(content))
    rendered = [component for part in parts if part["type"] == "components" for component in part["components"]]
    assert len(rendered) == 1
    assert rendered[0]["type"] == "collapsible"
    assert rendered[0]["default_open"] is False
    assert all("Private model details" not in part.get("text", "") for part in parts)
    snapshot = {"transcript": [{"role": "assistant", "parts": parts}], "canvas": {"components": []}}
    web = augment_conversation_snapshot_for_target(snapshot, None, target="web")
    html = "".join(c["_presentation"]["html"] for p in web["transcript"][0]["parts"] if p["type"] == "components" for c in p["components"])
    assert "Reasoning" in html
    assert "Private model details" in html


def test_reasoning_on_canvas_is_not_repeated_in_rail_and_ordinary_prose_stays():
    content = [{"type": "container", "content": [
        {"type": "card", "content": [{"type": "text", "content": "Answer"}]},
        {"type": "collapsible", "component_id": "on-canvas", "title": "Detail",
         "content": [{"type": "text", "content": "Detail"}]},
    ]}]
    assert _rail_parts(_content_parts(content), canvas_component_ids=frozenset({"on-canvas"})) == [
        {"type": "text", "text": "Answer"}]


def test_snapshot_adapter_preserves_raw_identity_and_consolidates_presentation():
    from rote.rote import ROTE

    fake = SimpleNamespace(rote=ROTE())
    socket = object()
    fake.rote.register_device(socket, {})
    originals = [table(component_id="source"), table(component_id="result")]
    before = copy.deepcopy(originals)
    snapshot = {"transcript": [], "canvas": {"components": originals}}
    result = Orchestrator._adapt_conversation_snapshot(fake, socket, snapshot)
    assert fake.rote.get_cached_components(socket) == before
    assert len(result["canvas"]["components"]) == 1
    assert result["canvas"]["components"][0]["component_id"] == "result"
    assert originals == before


@pytest.mark.parametrize("nested", [False, True])
def test_retained_source_and_final_title_pass_real_rote_snapshot_rendering(nested):
    from rote.rote import ROTE
    from shared.protocol import ConversationSnapshot

    source = table(title="", component_id="stored-tool-table", _source_agent="office", _source_tool="excel_generate", _source_params={"title": "Monthly"})
    final = table(title="Sheet 1 — Monthly Spend", component_id="generated-table")
    target = {"type": "card", "component_id": "dashboard", "content": [final]} if nested else final
    snapshot = {"type": "conversation_snapshot", "schema_version": 1,
                "snapshot_id": str(uuid.uuid4()), "chat_id": str(uuid.uuid4()),
                "connection_generation": str(uuid.uuid4()), "request_generation": str(uuid.uuid4()),
                "snapshot_purpose": "commit", "render_revision": 1, "committed_at": "2026-09-23T12:00:00Z",
                "transcript": [], "canvas": {"target": "canvas", "components": [source, target]}}
    fake = SimpleNamespace(rote=ROTE())
    socket = object()
    fake.rote.register_device(socket, {})
    adapted = Orchestrator._adapt_conversation_snapshot(fake, socket, snapshot)
    ConversationSnapshot(**adapted).validate()
    assert all(node.get("component_id") and node.get("_presentation", {}).get("html") for node in adapted["canvas"]["components"])
    tables = [node for node in walk(adapted["canvas"]["components"]) if node.get("type") == "table"]
    assert len(tables) == 1
    assert tables[0]["component_id"] == "stored-tool-table"
    assert tables[0]["_source_params"] == source["_source_params"]
    html = "".join(node["_presentation"]["html"] for node in adapted["canvas"]["components"])
    assert "Sheet 1" in html
    assert "stored-tool-table" in html


@pytest.mark.parametrize("with_layout", [False, True])
def test_live_canvas_uses_same_consolidation(with_layout):
    components = [table(component_id="first"), table(component_id="second")]
    layouts = [{"layout": [{"type": "ref", "component_id": "first"}, {"type": "ref", "component_id": "second"}], "position": 1}] if with_layout else []
    workspace = SimpleNamespace(live_layouts=lambda *args: layouts, live_components=lambda *args: copy.deepcopy(components),
                                live_rows=lambda *args: [{"component_id": c["component_id"], "component_data": copy.deepcopy(c), "position": i} for i, c in enumerate(components)])
    result = Orchestrator._canvas_components(SimpleNamespace(workspace=workspace), "chat", "owner")
    assert len([node for node in walk(result) if node.get("type") == "table"]) == 1
