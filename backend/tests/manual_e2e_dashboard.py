"""Manual E2E (not collected by pytest): drive the LIVE orchestrator over
ws://localhost:8001/ws with the real dev LLM and verify a chat query produces
a rich, designed dashboard on the canvas.

Requires USE_MOCK_AUTH=true (token 'dev-token'). Run inside the container:
    python tests/manual_e2e_dashboard.py "generate a rich dashboard for ..."
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import websockets

QUERY = sys.argv[1] if len(sys.argv) > 1 else \
    "generate a rich dashboard for a fake dog walking business"
DEADLINE_S = 420


async def main():
    summary = {"upserts": [], "canvas_renders": [], "chat_renders": 0, "steps": []}
    last_canvas_html = ""
    async with websockets.connect("ws://localhost:8001/ws", max_size=32 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "type": "register_ui", "token": "dev-token",
            "capabilities": ["render", "stream"], "session_id": f"e2e-{int(time.time())}",
            "device": {"device_type": "browser", "screen_width": 1920, "screen_height": 1080,
                       "supports_charts": True, "supports_tables": True, "supports_images": True},
        }))
        await asyncio.sleep(1.5)  # drain registration burst lazily below
        await ws.send(json.dumps({
            "type": "ui_event", "action": "chat_message",
            "payload": {"message": QUERY, "chat_id": None},
        }))
        chat_id = None
        deadline = time.time() + DEADLINE_S
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(1, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            t = msg.get("type")
            if t == "chat_created" and msg.get("payload"):
                chat_id = msg["payload"].get("chat_id")
            elif t == "ui_upsert":
                ops = msg.get("ops") or []
                summary["upserts"].append({
                    "ops": len(ops),
                    "component_ids": [o.get("component_id") for o in ops],
                    "distinct_ids": len({o.get("component_id") for o in ops}),
                    "types": [(o.get("component") or {}).get("type") for o in ops],
                })
            elif t == "ui_render":
                if msg.get("target") == "chat":
                    summary["chat_renders"] += 1
                else:
                    html = msg.get("html") or ""
                    last_canvas_html = html
                    summary["canvas_renders"].append({
                        "bytes": len(html),
                        "anchors": html.count("data-component-id"),
                    })
            elif t == "chat_step" and msg.get("step"):
                step = msg["step"]
                summary["steps"].append(f"{step.get('name') or step.get('kind')}:{step.get('status')}")
            elif t == "chat_status" and msg.get("status") == "done":
                break

    print(f"chat_id: {chat_id}")
    print(f"upserts: {summary['upserts']}")
    print(f"canvas renders: {summary['canvas_renders']}")
    print(f"chat bubbles: {summary['chat_renders']}; steps: {summary['steps'][-6:]}")

    markers = {
        "hero": "astral-hero" in last_canvas_html,
        "metric tiles": "astral-metric" in last_canvas_html,
        "charts": "astral-chart" in last_canvas_html,
        "table or timeline": ("astral-table-wrap" in last_canvas_html
                              or "astral-timeline" in last_canvas_html),
        "designed garnish (dg_)": 'id="dg_' in last_canvas_html,
        "no unsupported": "astral-unsupported" not in last_canvas_html,
        "no render errors": "astral-render-error" not in last_canvas_html,
    }
    rich = sum(1 for k in ("hero", "metric tiles", "charts", "table or timeline") if markers[k])
    for name, ok in markers.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    if chat_id:
        component_ids = [
            component_id
            for upsert in summary["upserts"]
            for component_id in upsert["component_ids"]
            if component_id
        ]
        # "Rich": ≥2 distinct server-issued component identities and at least 3 of the 4
        # visual marker groups on the final canvas (router variance on the
        # weak dev LLM decides HOW MANY tool calls happen; the designer's
        # garnish fills the gaps).
        ok = (len(set(component_ids)) >= 2
              and rich >= 3 and markers["no unsupported"] and markers["no render errors"])
        print("E2E VERDICT:", "RICH DASHBOARD OK" if ok else "NOT RICH ENOUGH — investigate")
        return 0 if ok else 1
    print("E2E VERDICT: NO CHAT CREATED")
    return 1


sys.exit(asyncio.run(main()))
