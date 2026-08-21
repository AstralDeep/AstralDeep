"""Manual smoke (not collected by pytest): dog-grooming dashboard through the
full pipeline — upgraded interactive_artifacts -> multi-round design_round
(scripted LLM) -> materialize -> webrender. Dumps /tmp/dashboard_smoke.html.

Run inside the container:
    python tests/manual_dashboard_smoke.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import webrender
from astralprojection.resources import static_path, vendor_path
from agents.connectors.mcp_tools_creative import handle_artifacts
from orchestrator import ui_designer

result = handle_artifacts({
    "title": "Paws & Bubbles Grooming Dashboard",
    "subtitle": "Operations and growth at a glance",
    "sections": [
        {"widget_type": "metric", "title": "New Booking Requests", "value": "7",
         "data_source": "pending_requests", "variant": "warning"},
        {"widget_type": "metric", "title": "Today's Revenue", "value": "$840",
         "variant": "success"},
        {"widget_type": "chart", "title": "Monthly Revenue", "chart_kind": "line",
         "labels": ["Jan", "Feb", "Mar", "Apr", "May", "Jun"],
         "values": [3200, 4100, 3900, 4800, 5300, 6100]},
        {"widget_type": "chart", "title": "Most Popular Services", "chart_kind": "pie",
         "labels": ["Full Groom", "Bath & Brush", "Nail Trim", "De-shed"],
         "values": [42, 28, 18, 12]},
        {"widget_type": "table", "title": "Today's Schedule",
         "headers": ["Time", "Dog", "Breed", "Service"],
         "rows": [["9:00", "Bella", "Golden Retriever", "Full Groom"],
                  ["10:30", "Max", "Poodle", "Bath & Brush"],
                  ["13:00", "Luna", "Husky", "De-shed"]]},
        {"widget_type": "timeline", "title": "Upcoming",
         "events": [{"time": "9:00 AM", "title": "Bella — Full Groom", "variant": "success"},
                    {"time": "10:30 AM", "title": "Max — Bath & Brush"},
                    {"time": "1:00 PM", "title": "Luna — De-shed", "variant": "info"}]},
    ],
})
components = result["_ui_components"]
print(f"tool emitted {len(components)} components: {[c['type'] for c in components]}")

# Assign workspace-style identities the way upsert would.
for i, comp in enumerate(components):
    comp["component_id"] = f"wc_smoke{i:02d}"

ids = [c["component_id"] for c in components]

_DRAFT = json.dumps({"layout": [{"type": "ref", "component_id": cid} for cid in ids]})
_REFINED = json.dumps({"layout": [
    {"type": "ref", "component_id": ids[0]},                      # hero
    {"type": "grid", "columns": 3, "children": [
        {"type": "ref", "component_id": ids[1]},                  # bookings metric
        {"type": "ref", "component_id": ids[2]},                  # revenue metric
        {"type": "rating", "value": 4.8, "label": "Customer satisfaction",
         "subtitle": "128 reviews"},
    ]},
    {"type": "grid", "columns": 2, "children": [
        {"type": "ref", "component_id": ids[3]},                  # line chart
        {"type": "ref", "component_id": ids[4]},                  # pie chart
    ]},
    {"type": "grid", "columns": 2, "children": [
        {"type": "ref", "component_id": ids[5]},                  # schedule table
        {"type": "ref", "component_id": ids[6]},                  # timeline
    ]},
    {"type": "collapsible", "title": "Notes", "content": [
        {"type": "ref", "component_id": ids[7]},                  # sample-data caption
    ]},
]})

replies = [_DRAFT, _REFINED, "DONE"]
calls = []


async def llm(messages):
    calls.append(messages)
    return replies[min(len(calls) - 1, len(replies) - 1)]


layout = asyncio.run(ui_designer.design_round(
    user_request="build me a web dashboard for my dog grooming business",
    round_components=components,
    canvas_rows=[],
    chat_id="smoke-chat",
    layout_key="ly_smoke",
    allowed_types=set(webrender.allowed_primitive_types()),
    llm_call=llm,
))
assert layout is not None, "designer fell back"
print(f"designer passes: {len(calls)}; final layout nodes: {[n.get('type') for n in layout]}")

by_id = {c["component_id"]: c for c in components}
materialized = ui_designer.materialize(layout, by_id)
html_out = webrender.render_workspace(materialized, profile=None)
tailwind_url = Path(str(vendor_path("tailwind.js"))).resolve().as_uri()
css_url = Path(str(static_path("astral.css"))).resolve().as_uri()

page = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<script src="{tailwind_url}"></script>
<link rel="stylesheet" href="{css_url}">
</head><body><div id="astral-canvas" class="p-4 space-y-3">{html_out}</div></body></html>"""
Path("./backend/tmp/dashboard_smoke.html").write_text(page, encoding="utf-8")

checks = {
    "hero band": "astral-hero--gradient" in html_out,
    "metric tiles": html_out.count("astral-metric") >= 2,
    "3-col headline grid": "lg:grid-cols-3" in html_out,
    "charts hydrate": html_out.count("astral-chart") >= 2,
    "schedule table": "Golden Retriever" in html_out,
    "timeline rail": "astral-tl-item--success" in html_out,
    "rating garnish": "astral-star--filled" in html_out,
    "garnish ids stamped": 'id="dg_' in html_out,
    "morph anchors on nested refs": html_out.count("data-component-id") >= len(ids),
    "no unsupported placeholders": "astral-unsupported" not in html_out,
    "no render errors": "astral-render-error" not in html_out,
}
for name, ok in checks.items():
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
assert all(checks.values()), "smoke checks failed"
print("dashboard smoke OK -> /tmp/dashboard_smoke.html")
