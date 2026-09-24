#!/usr/bin/env python3
"""Drives real turns through a running stack over the web client's own websocket,
measuring client-visible first-progress time plus the orchestrator perf-log window
between the routing seam opening and the first model call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional, Sequence

ROOT = Path(__file__).resolve().parents[2]

PROGRESS_FRAMES = {
    # Omitting this frame measures render, not first progress
    "operation_status",
    "status", "ui_render", "ui_update", "ui_append", "ui_upsert",
    "ui_stream_data", "turn_phase", "processing_async", "chat_message",
}

PERF_LINE = re.compile(
    r"perf (?P<name>[\w.]+) duration_ms=(?P<ms>\d+)(?P<ctx>(?: \w+=\S+)*)"
)


def _percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _stats(name: str, samples: Sequence[float]) -> dict:
    return {
        "measure": name,
        "n": len(samples),
        "p50_ms": round(_percentile(samples, 0.50), 2),
        "p95_ms": round(_percentile(samples, 0.95), 2),
        "max_ms": round(max(samples), 2) if samples else 0.0,
        "mean_ms": round(statistics.fmean(samples), 2) if samples else 0.0,
    }


async def _drive(uri: str, token: str, prompts: Sequence[str], turns: int,
                 per_turn_timeout: float) -> dict:
    import websockets

    to_first_frame: list[float] = []
    completed = 0
    hung = 0

    session_id = str(uuid.uuid4())
    connection_generation = None

    async with websockets.connect(uri, max_size=50 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "type": "register_ui",
            "token": token,
            "capabilities": ["text", "images"],
            "session_id": session_id,
        }))
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            connection_generation = connection_generation or frame.get("connection_generation")
            if frame.get("type") == "system_config":
                break
            if frame.get("type") in {"auth_required", "error"}:
                raise SystemExit(
                    "register_ui refused: "
                    f"{frame.get('message') or frame.get('type')}. Pass --token "
                    "a real access token: the mock literal is refused by the 088 "
                    "guidance authority by design. See verification.md 7c for the "
                    "two ways to get one without a realm configuration change."
                )

        for index in range(turns):
            prompt = prompts[index % len(prompts)]
            sent = time.perf_counter()
            submission_id = str(uuid.uuid4())
            request_generation = str(uuid.uuid4())
            envelope = {
                "type": "ui_event",
                "action": "chat_message",
                "session_id": session_id,
                "submission_id": submission_id,
                "request_generation": request_generation,
                "payload": {
                    "message": prompt,
                    "chat_id": session_id,
                    "submission_id": submission_id,
                    "request_generation": request_generation,
                    "snapshot_purpose": "commit",
                },
            }
            if connection_generation:
                envelope["connection_generation"] = connection_generation
                envelope["payload"]["connection_generation"] = connection_generation
            await ws.send(json.dumps(envelope))
            first: Optional[float] = None
            deadline = time.monotonic() + per_turn_timeout
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=max(0.1, deadline - time.monotonic())
                    )
                except asyncio.TimeoutError:
                    break
                frame = json.loads(raw)
                if first is None and frame.get("type") in PROGRESS_FRAMES:
                    first = (time.perf_counter() - sent) * 1000.0
                    to_first_frame.append(first)
                if (frame.get("type") == "operation_status"
                        and frame.get("terminal")):
                    completed += 1
                    break
                if frame.get("type") in {"turn_complete", "chat_complete", "error"}:
                    completed += 1
                    break
            else:
                hung += 1
            if first is None:
                hung += 1

    return {"to_first_frame": to_first_frame, "completed": completed, "hung": hung}


def _perf_windows(container: str, since: str) -> dict:
    try:
        raw = subprocess.run(
            ["docker", "logs", "--since", since, container],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "MSYS_NO_PATHCONV": "1"},
        )
    except Exception as exc:  # pragma: no cover
        return {"error": str(exc), "windows": []}

    opened: dict[str, float] = {}
    windows: list[float] = []
    routing_ms: dict[str, float] = {}
    added: list[float] = []
    sent_at: dict[str, float] = {}
    to_tool: list[float] = []
    stamp = re.compile(r"^(?P<t>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]\d+)")
    for line in (raw.stdout + raw.stderr).splitlines():
        match = PERF_LINE.search(line)
        if not match:
            continue
        ctx = dict(
            pair.split("=", 1) for pair in match.group("ctx").split() if "=" in pair
        )
        chat = ctx.get("chat")
        if not chat:
            continue
        ts = stamp.match(line)
        if not ts:
            continue
        moment = time.mktime(time.strptime(ts.group("t")[:19], "%Y-%m-%d %H:%M:%S"))
        moment += float("0." + re.split(r"[.,]", ts.group("t"))[-1])
        if match.group("name") == "turn.typesafe_start":
            opened[chat] = moment
            sent_at[chat] = moment
        elif match.group("name") == "turn.typesafe":
            routing_ms[chat] = float(match.group("ms"))
        elif match.group("name") == "turn.first_llm_call_start" and chat in opened:
            preparation = (moment - opened.pop(chat)) * 1000.0
            windows.append(preparation)
            routing = routing_ms.pop(chat, None)
            if routing is not None:
                added.append(max(0.0, routing - preparation))
        elif match.group("name") == "turn.first_tool_dispatch" and chat in sent_at:
            to_tool.append((moment - sent_at.pop(chat)) * 1000.0)
    return {"windows": windows, "added_wait": added, "routing_to_tool": to_tool}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", default=f"ws://127.0.0.1:{os.getenv('ORCHESTRATOR_PORT', '8001')}/ws")
    parser.add_argument("--token", default=os.getenv("TEST_UI_TOKEN", "dev-token"))
    parser.add_argument("--turns", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--container", default="astraldeep")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument(
        "--perf-only", action="store_true",
        help="Read the server-side overlap window from the container log and "
             "stop. Drives no turns -- use it after the completion walkthrough, "
             "which drives them from inside the container where docker logs is "
             "not reachable.")
    parser.add_argument(
        "--since", default=None,
        help="Log start for --perf-only: a docker --since value, such as the "
             "UTC stamp the walkthrough prints (2026-09-17T14:03:11) or 900s.")
    args = parser.parse_args(argv)

    if args.perf_only:
        if not args.since:
            parser.error("--perf-only needs --since")
        perf = _perf_windows(args.container, args.since)
        if "error" in perf:
            print(f"could not read the container log: {perf['error']}")
            return 1
        stats = _stats(
            "turn.typesafe_start to turn.first_llm_call_start",
            perf.get("windows", []))
        print("Server-side preparation window (the routing call overlaps this)")
        print(f"{'measure':<52}{'n':>5}{'p50':>9}{'p95':>9}{'max':>9}")
        print(f"{stats['measure']:<52}{stats['n']:>5}{stats['p50_ms']:>9.1f}"
              f"{stats['p95_ms']:>9.1f}{stats['max_ms']:>9.1f}")
        added = _stats("SC-002 added wait max(0, routing - preparation)",
                       perf.get("added_wait", []))
        to_tool = _stats("routing open to first tool dispatch",
                         perf.get("routing_to_tool", []))
        for row in (added, to_tool):
            print(f"{row['measure']:<52}{row['n']:>5}{row['p50_ms']:>9.1f}"
                  f"{row['p95_ms']:>9.1f}{row['max_ms']:>9.1f}")
        if added["n"]:
            print("")
            print(f"SC-002 bound: p95 added wait <= 150 ms -> "
                  f"{added['p95_ms']:.1f} ms "
                  f"{'PASS' if added['p95_ms'] <= 150 else 'FAIL'}")
        if not stats["n"]:
            print("")
            print("No paired markers in that window. Either no turn ran, or the "
                  "window is wrong -- pass the stamp the walkthrough printed.")
            return 1
        if args.json:
            args.json.write_text(json.dumps(
                {"preparation_window": stats, "added_wait": added,
                 "routing_to_first_tool_dispatch": to_tool}, indent=2),
                encoding="utf-8")
        return 0

    fixtures = ROOT / "backend/tests/fixtures/typesafe_routing/prompts.json"
    prompts = [c["prompt"] for c in json.loads(fixtures.read_text(encoding="utf-8"))["cases"]]

    started = time.time()
    result = asyncio.run(_drive(args.uri, args.token, prompts, args.turns, args.timeout))
    elapsed = max(1, int(time.time() - started) + 5)
    perf = _perf_windows(args.container, f"{elapsed}s")

    report = {
        "turns": args.turns,
        "completed": result["completed"],
        "hung": result["hung"],
        "send_to_first_frame": _stats("Send to first progress frame", result["to_first_frame"]),
        "preparation_window": _stats(
            "turn.typesafe_start to turn.first_llm_call_start", perf.get("windows", [])
        ),
        "added_wait": _stats(
            "SC-002 added wait max(0, routing - preparation)", perf.get("added_wait", [])
        ),
        "routing_to_first_tool_dispatch": _stats(
            "routing open to first tool dispatch", perf.get("routing_to_tool", [])
        ),
    }
    if "error" in perf:
        report["preparation_window_error"] = perf["error"]

    lines = [
        "Turn timeline on the running stack",
        f"turns: {report['turns']}   completed: {report['completed']}   "
        f"no first frame: {report['hung']}",
        "",
        f"{'measure':<52}{'n':>5}{'p50':>9}{'p95':>9}{'max':>9}",
    ]
    for key in ("send_to_first_frame", "preparation_window", "added_wait",
                "routing_to_first_tool_dispatch"):
        s = report[key]
        lines.append(
            f"{s['measure']:<52}{s['n']:>5}{s['p50_ms']:>9.1f}"
            f"{s['p95_ms']:>9.1f}{s['max_ms']:>9.1f}"
        )
    if report["added_wait"]["n"]:
        lines.append("")
        lines.append(
            f"SC-002 bound: p95 added wait <= 150 ms -> "
            f"{report['added_wait']['p95_ms']:.1f} ms "
            f"{'PASS' if report['added_wait']['p95_ms'] <= 150 else 'FAIL'}"
        )
    lines.append("")
    sys.stdout.write("\n".join(lines) + "\n")

    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
