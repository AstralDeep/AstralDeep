"""Tests for orchestrator/context_engineering.py: compose_system_prompt's byte-identical
off-path and stable-prefix on-path ordering, and edit_context's tombstoning of stale
tool outputs while preserving assistant/tool pairing.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import context_engineering as ce  # noqa: E402

FC = ce.FILE_CONTEXT_MARK
CC = ce.CANVAS_CONTEXT_MARK

TEMPLATE = f"HEADER\n\n{FC}\n\nRULES go here.\n{CC}\nFOOTER\n"


def test_off_path_is_byte_identical_to_in_place_substitution():
    fc = "\nFILES: a -> /b\n"
    cc = "\nCANVAS: comp1\n"
    got = ce.compose_system_prompt(
        TEMPLATE, file_context=fc, canvas_context=cc, cache_stable=False
    )
    legacy = TEMPLATE.replace(FC, fc).replace(CC, cc)
    assert got == legacy
    assert FC not in got and CC not in got
    assert got.index("FILES: a -> /b") < got.index("RULES go here")


def test_off_path_empty_volatile_matches_legacy_blanks():
    got = ce.compose_system_prompt(
        TEMPLATE, file_context="", canvas_context="", cache_stable=False
    )
    assert got == TEMPLATE.replace(FC, "").replace(CC, "")


def test_on_path_moves_volatile_to_the_end():
    fc = "\nFILES: a -> /b\n"
    cc = "\nCANVAS: comp1\n"
    got = ce.compose_system_prompt(
        TEMPLATE, file_context=fc, canvas_context=cc, cache_stable=True
    )
    assert got.index("RULES go here") < got.index("FILES: a -> /b")
    assert got.index("FOOTER") < got.index("FILES: a -> /b")
    assert got.index("FILES: a -> /b") < got.index("CANVAS: comp1")
    assert FC not in got and CC not in got


def test_on_path_prefix_is_stable_across_different_volatile():
    a = ce.compose_system_prompt(
        TEMPLATE, file_context="\nF1\n", canvas_context="\nC1\n", cache_stable=True
    )
    b = ce.compose_system_prompt(
        TEMPLATE, file_context="\nF2 totally different\n",
        canvas_context="\nC2 other\n", cache_stable=True
    )
    core = TEMPLATE.replace(FC, "").replace(CC, "").rstrip()
    assert a.startswith(core)
    assert b.startswith(core)


def test_on_path_no_volatile_is_just_core():
    got = ce.compose_system_prompt(
        TEMPLATE, file_context="", canvas_context="", cache_stable=True
    )
    assert got == TEMPLATE.replace(FC, "").replace(CC, "")


def _assistant(call_id):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id}]}


def _tool(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "name": "fetch",
            "content": content}


def _convo(n_rounds, *, size=1000):
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    for r in range(n_rounds):
        msgs.append(_assistant(f"c{r}"))
        msgs.append(_tool(f"c{r}", f"RESULT-{r} " + "x" * size))
    return msgs


def test_tombstones_old_rounds_keeps_recent():
    msgs = _convo(6)
    out, n = ce.edit_context(msgs, keep_last_tool_rounds=3, min_tombstone_chars=400)
    assert n == 3
    tool_contents = [m["content"] for m in out if m.get("role") == "tool"]
    assert tool_contents[:3] == [ce.TOMBSTONE] * 3
    assert all(c.startswith("RESULT-") for c in tool_contents[3:])


def test_preserves_pairing_fields():
    msgs = _convo(5)
    out, _ = ce.edit_context(msgs, keep_last_tool_rounds=2)
    for m in out:
        if m.get("role") == "tool":
            assert "tool_call_id" in m and m["tool_call_id"].startswith("c")
            assert m["name"] == "fetch"


def test_never_touches_non_tool_messages():
    msgs = _convo(6)
    out, _ = ce.edit_context(msgs, keep_last_tool_rounds=1)
    assert out[0] == {"role": "system", "content": "sys"}
    assert out[1] == {"role": "user", "content": "hi"}
    for m in out:
        if m.get("role") == "assistant":
            assert m.get("content") != ce.TOMBSTONE


def test_small_outputs_not_tombstoned():
    msgs = _convo(6, size=10)
    out, n = ce.edit_context(msgs, keep_last_tool_rounds=2, min_tombstone_chars=400)
    assert n == 0
    assert all(m["content"].startswith("RESULT-")
               for m in out if m.get("role") == "tool")


def test_idempotent():
    msgs = _convo(6)
    out1, n1 = ce.edit_context(msgs, keep_last_tool_rounds=2)
    out2, n2 = ce.edit_context(out1, keep_last_tool_rounds=2)
    assert n1 == 4 and n2 == 0
    assert out2 == out1


def test_within_keep_window_is_noop():
    msgs = _convo(3)
    out, n = ce.edit_context(msgs, keep_last_tool_rounds=3)
    assert n == 0 and out == msgs


def test_no_tool_messages_is_noop():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    out, n = ce.edit_context(msgs)
    assert n == 0 and out is msgs


def test_does_not_mutate_input():
    msgs = _convo(6)
    snapshot = [dict(m) for m in msgs]
    ce.edit_context(msgs, keep_last_tool_rounds=2)
    assert msgs == snapshot
    assert all(m["content"].startswith(("RESULT-",))
               for m in msgs if m.get("role") == "tool")


def test_malformed_input_passes_through():
    assert ce.edit_context(None) == (None, 0)
    assert ce.edit_context([]) == ([], 0)
    weird = [{"role": "user", "content": "u"}, "not-a-dict",
             _assistant("c0"), _tool("c0", "y" * 1000)]
    out, n = ce.edit_context(weird, keep_last_tool_rounds=0)
    assert n == 1
