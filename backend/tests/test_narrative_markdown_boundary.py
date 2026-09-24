"""Tests for orchestrator/stream_manager.py's markdown_safe_prefix_len: streamed
narrative frames never end inside an open emphasis, code, fence, or link span,
verified by property tests and through the live _call_llm streaming path.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.stream_manager import markdown_safe_prefix_len  # noqa: E402
from tests.test_llm_streaming import (  # noqa: E402
    _bare_orch, _content_chunk, _FakeCompletions, _stream_frames,
)


def test_dangling_bold_held():
    assert markdown_safe_prefix_len("You rolled **") == len("You rolled ")


def test_closed_bold_ships_through_following_whitespace():
    text = "You rolled **6** and won"
    assert markdown_safe_prefix_len(text) == len("You rolled **6** and ")


def test_dangling_italic_held():
    assert markdown_safe_prefix_len("a *word") == len("a ")


def test_dangling_inline_code_held():
    assert markdown_safe_prefix_len("run `pip install") == len("run ")


def test_dangling_link_held():
    assert markdown_safe_prefix_len("see [the docs](https://x") == len("see ")
    text = "see [the docs](https://x) for more"
    assert markdown_safe_prefix_len(text) == len("see [the docs](https://x) for ")


def test_open_fence_holds_until_closed():
    text = "intro\n```python\ncode = 1\n"
    assert markdown_safe_prefix_len(text) == len("intro\n")
    closed = text + "```\nafter "
    assert markdown_safe_prefix_len(closed) == len(closed)


def test_partial_fence_marker_line_held():
    assert markdown_safe_prefix_len("intro\n```py") == len("intro\n")


def test_completed_line_resets_inline_state():
    text = "oops **\nnext words "
    assert markdown_safe_prefix_len(text) == len(text)


def test_bullet_marker_is_not_emphasis():
    assert markdown_safe_prefix_len("* item one") == len("* item ")


def test_bare_list_marker_held():
    assert markdown_safe_prefix_len("* ") == 0
    assert markdown_safe_prefix_len("- ") == 0
    assert markdown_safe_prefix_len("1. ") == 0


def test_backslash_is_not_an_escape():
    text = r"a \*lit and more"
    assert markdown_safe_prefix_len(text) == len("a ")
    assert markdown_safe_prefix_len(r"a \*lit\* done ") == len(r"a \*lit\* done ")


def test_no_boundary_yet():
    assert markdown_safe_prefix_len("") == 0
    assert markdown_safe_prefix_len("Hello") == 0


_SPAN = "span"
_PLAIN = "plain"

_DOC_PARTS = [
    (_PLAIN, "The dice results are in. "),
    (_SPAN, "**a bold verdict**"),
    (_PLAIN, " arrived with "),
    (_SPAN, "*subtle emphasis*"),
    (_PLAIN, " and inline "),
    (_SPAN, "`code_token()`"),
    (_PLAIN, " plus a "),
    (_SPAN, "[link label](https://example.com/path)"),
    (_PLAIN, ".\n\nA list follows:\n"),
    (_PLAIN, "* first item with "),
    (_SPAN, "**bold in item**"),
    (_PLAIN, "\n* second item\n- third item\n\n"),
    (_SPAN, "```python\ndef f():\n    return 42  # ** ` [ not markup\n```"),
    (_PLAIN, "\nAfter the fence, "),
    (_SPAN, "*receipts*"),
    (_PLAIN, " and totals "),
    (_SPAN, "**42 dollars**"),
    (_PLAIN, " even. Escaped \\*literal\\* stars too.\n"),
]

_DOC = "".join(part for _, part in _DOC_PARTS)


def _span_intervals():
    intervals, pos = [], 0
    for kind, part in _DOC_PARTS:
        if kind == _SPAN:
            intervals.append((pos, pos + len(part)))
        pos += len(part)
    return intervals


def test_property_random_split_points_never_ship_dangling_tokens():
    rng = random.Random(20260713)
    intervals = _span_intervals()
    for _trial in range(200):
        cut_count = rng.randint(1, 40)
        cuts = sorted(rng.sample(range(1, len(_DOC)), cut_count))
        pieces = [_DOC[a:b] for a, b in zip([0] + cuts, cuts + [len(_DOC)])]
        text = ""
        prev_safe = 0
        for piece in pieces:
            text += piece
            safe = markdown_safe_prefix_len(text)
            assert 0 <= safe <= len(text)
            assert safe >= prev_safe
            if safe > prev_safe:
                frame = text[:safe]
                assert _DOC.startswith(frame)
                assert frame[-1].isspace()
                for a, b in intervals:
                    assert not (a < safe < b), (
                        f"frame ends inside span {_DOC[a:b]!r} at offset {safe}"
                    )
            prev_safe = safe
        assert text == _DOC


async def test_stream_path_never_ships_dangling_bold():
    full = "You rolled **6** and won."
    comp = _FakeCompletions(chunks=[
        _content_chunk("You rolled "), _content_chunk("**"),
        _content_chunk("6"), _content_chunk("**"),
        _content_chunk(" and won.")])
    orch = _bare_orch(comp)
    msg, _usage = await orch._call_llm(
        object(), [{"role": "user", "content": "roll"}],
        allow_stream=True, stream_chat_id="chat-1")
    assert msg.content == full
    frames = _stream_frames(orch)
    assert frames and frames[-1]["terminal"] is True
    bold_span = (full.index("**"), full.index(" and won."))
    for frame in frames[:-1]:
        content = frame["components"][0]["content"]
        assert full.startswith(content)
        assert content == full or content[-1].isspace()
        assert not bold_span[0] < len(content) < bold_span[1], (
            f"frame ends inside the bold span: {content!r}")
    assert frames[0]["components"][0]["content"] == "You rolled "
    assert frames[-2]["components"][0]["content"] == full


async def test_stream_path_terminal_flush_delivers_held_tail():
    comp = _FakeCompletions(chunks=[
        _content_chunk("Total: "), _content_chunk("**42")])
    orch = _bare_orch(comp)
    msg, _usage = await orch._call_llm(
        object(), [{"role": "user", "content": "sum"}],
        allow_stream=True, stream_chat_id="chat-1")
    assert msg.content == "Total: **42"
    frames = _stream_frames(orch)
    contents = [f["components"][0]["content"] for f in frames if not f["terminal"]]
    assert contents[0] == "Total: "
    assert contents[-1] == "Total: **42"
    assert frames[-1]["terminal"] is True


async def test_stream_path_no_redundant_flush_when_fully_shipped():
    comp = _FakeCompletions(chunks=[_content_chunk("done. ")])
    orch = _bare_orch(comp)
    msg, _usage = await orch._call_llm(
        object(), [{"role": "user", "content": "hi"}],
        allow_stream=True, stream_chat_id="chat-1")
    assert msg.content == "done. "
    frames = _stream_frames(orch)
    assert [f["components"][0]["content"] for f in frames if not f["terminal"]] == ["done. "]
    assert frames[-1]["terminal"] is True
