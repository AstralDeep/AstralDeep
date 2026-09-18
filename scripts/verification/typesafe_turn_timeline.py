#!/usr/bin/env python3
"""Feature 089 (T004): the turn timeline on a running candidate stack.

`typesafe_turn_latency.py` measures the routing seam in isolation, which is an
**upper bound** on what a turn waits: it assumes nothing overlaps the call. The
turn does not work that way. `start_routing` is opened early and the decision
is collected several hundred lines later, after the history load, the tool
assembly, the permission checks and the prompt build. Whatever those take is
time the routing call was already using.

So this drives real turns through a running stack over the same WebSocket the
web client uses, and reads two things:

* **client-side**, the time from Send to the first frame that puts something on
  screen — the "first progress state" of SC-001;
* **server-side**, from the orchestrator's own `perf` log, the window between
  `turn.typesafe_start` and `turn.first_llm_call_start` — the preparation the
  routing call overlaps.

The added wait SC-002 bounds is then `max(0, routing - preparation)`, and both
halves are measured rather than assumed.

No key is ever read here: whether the signed-in user has one is the stack's
business, and this script only times what happens.

Usage:
    python scripts/typesafe_turn_timeline.py --turns 30
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

#: Frames that mean "something is on screen now".
PROGRESS_FRAMES = {
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

    async with websockets.connect(uri, max_size=50 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "type": "register_ui",
            "token": token,
            "capabilities": ["text", "images"],
            "session_id": f"turn-timeline-{uuid.uuid4().hex[:8]}",
        }))
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if frame.get("type") == "system_config":
                break
            if frame.get("type") in {"auth_required", "error"}:
                raise SystemExit(
                    "register_ui refused: "
                    f"{frame.get('message') or frame.get('type')}. The stack is "
                    "running real auth; this driver needs the development posture."
                )

        for index in range(turns):
            prompt = prompts[index % len(prompts)]
            sent = time.perf_counter()
            await ws.send(json.dumps({
                "type": "ui_event",
                "action": "chat_message",
                "payload": {"message": prompt},
            }))
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
                if frame.get("type") in {"turn_complete", "chat_complete", "error"}:
                    completed += 1
                    break
            else:
                hung += 1
            if first is None:
                hung += 1

    return {"to_first_frame": to_first_frame, "completed": completed, "hung": hung}


def _perf_windows(container: str, since: str) -> dict:
    """The preparation window, from the orchestrator's own perf log.

    `turn.typesafe_start` is logged when the seam opens and
    `turn.first_llm_call_start` when the first model call is about to be made,
    both with the chat id, so the gap between the two log lines is the work the
    routing call overlapped.
    """
    try:
        raw = subprocess.run(
            ["docker", "logs", "--since", since, container],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "MSYS_NO_PATHCONV": "1"},
        )
    except Exception as exc:  # pragma: no cover - diagnostics only
        return {"error": str(exc), "windows": []}

    opened: dict[str, float] = {}
    windows: list[float] = []
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
        elif match.group("name") == "turn.first_llm_call_start" and chat in opened:
            windows.append((moment - opened.pop(chat)) * 1000.0)
    return {"windows": windows}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", default=f"ws://127.0.0.1:{os.getenv('ORCHESTRATOR_PORT', '8001')}/ws")
    parser.add_argument("--token", default=os.getenv("TEST_UI_TOKEN", "dev-token"))
    parser.add_argument("--turns", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--container", default="astraldeep")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

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
    for key in ("send_to_first_frame", "preparation_window"):
        s = report[key]
        lines.append(
            f"{s['measure']:<52}{s['n']:>5}{s['p50_ms']:>9.1f}"
            f"{s['p95_ms']:>9.1f}{s['max_ms']:>9.1f}"
        )
    lines.append("")
    sys.stdout.write("\n".join(lines) + "\n")

    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
