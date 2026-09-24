"""Deterministic scripted LLM (backend/verification/personas.py): replaces only the
model's token output — dispatches the real reader for a persona's file, then a real
chart tool over its data, so every tool call still executes for real.
"""

from __future__ import annotations

import csv
import json
import types
from typing import Any, Callable, Dict, List, Optional

from verification.personas import Persona

_READER_FOR = {
    "spreadsheet": "read_spreadsheet",
    "document": "read_document",
    "text": "read_text",
    "image": "read_image",
}


def _usage() -> Any:
    return types.SimpleNamespace(total_tokens=0, prompt_tokens=0, completion_tokens=0)


def _tool_call(name: str, args: Dict[str, Any]) -> Any:
    return types.SimpleNamespace(
        id=f"call_{name}",
        type="function",
        function=types.SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def _msg(content: Optional[str], tool_calls: Optional[List[Any]]) -> Any:
    return types.SimpleNamespace(
        content=content, tool_calls=tool_calls, reasoning_content=None
    )


def _csv_as_records(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def _coerce_numeric(records: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    out = []
    for r in records:
        r2 = dict(r)
        try:
            r2[key] = float(str(r.get(key, "")).replace(",", ""))
        except (TypeError, ValueError):
            pass
        out.append(r2)
    return out


def scripted_llm_for(
    persona: Persona, attachment_id: str, fixture_path: str
) -> Callable[..., Any]:
    fixture = persona.fixture
    reader = _READER_FOR.get(fixture.category)
    chart = fixture.chart
    state: Dict[str, int] = {"tool_rounds": 0}

    async def _call_llm(websocket, messages, tools_desc=None, temperature=None,
                        feature: str = "tool_dispatch"):
        # Other callers (e.g. UI designer) must converge immediately too.
        if feature != "tool_dispatch":
            return _msg("DONE", None), _usage()

        state["tool_rounds"] += 1
        rnd = state["tool_rounds"]

        if rnd == 1 and reader is not None:
            return _msg(None, [_tool_call(reader, {"attachment_id": attachment_id})]), _usage()

        if rnd == 2 and chart is not None:
            try:
                records = _csv_as_records(fixture_path)
                if chart.get("y_key"):
                    records = _coerce_numeric(records, chart["y_key"])
            except Exception:
                records = []
            if records:
                args = {
                    "data": records,
                    "x_key": chart["x_key"],
                    "title": f"{persona.display_name}: breakdown",
                }
                if chart.get("y_key"):
                    args["y_key"] = chart["y_key"]
                return _msg(None, [_tool_call("generate_dynamic_chart", args)]), _usage()

        return _msg(
            f"Done — analyzed {fixture.filename} and produced the components above.",
            None,
        ), _usage()

    return _call_llm
