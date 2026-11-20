"""Tests for the token estimators in backend/orchestrator/compaction.py."""

from __future__ import annotations

import json

from orchestrator.compaction import CHARS_PER_TOKEN, estimate_overhead_tokens


def test_overhead_estimator_counts_complete_tool_definitions_conservatively():
    tools = [{"type": "function", "function": {"name": "roll", "parameters": {}}}]
    compact = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert estimate_overhead_tokens(tools) >= len(compact.encode("utf-8"))
    assert estimate_overhead_tokens(tools) > len(json.dumps(tools)) // CHARS_PER_TOKEN
    assert estimate_overhead_tokens(tools, "gpt-4o", "openai") > 0
    assert estimate_overhead_tokens([]) == 0
    assert estimate_overhead_tokens(None) == 0
