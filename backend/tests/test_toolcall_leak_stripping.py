"""Tests for _strip_toolcall_leakage in orchestrator/orchestrator.py: leaked pseudo
tool-call syntax is stripped from the chat narrative, doc-card promotion, and tool
summaries, with an honest fallback if stripping empties the response.
"""

from __future__ import annotations

import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.orchestrator import (  # noqa: E402
    Orchestrator,
    _LEAK_FALLBACK_TEXT,
    _sanitize_text_response,
    _strip_toolcall_leakage,
)

RECORDED_FIXTURE = (
    "update_component<arg_key>component_id</arg_key>"
    "<arg_value>doc_3f9a1c2b7d44</arg_value>"
    "<arg_key>content</arg_key>"
    "<arg_value># Project Update\n\nRevised draft of the aims section.</arg_value>"
    "NEW_PAGE@true"
)


def test_recorded_fixture_strips_to_empty():
    assert _strip_toolcall_leakage(RECORDED_FIXTURE) == ""


def test_fixture_embedded_in_prose_preserves_prose():
    out = _strip_toolcall_leakage(
        "Here is the revision. " + RECORDED_FIXTURE + " Let me know."
    )
    assert "Here is the revision." in out
    assert "Let me know." in out
    for token in ("update_component", "<arg_key>", "<arg_value>", "@true"):
        assert token not in out


def test_truncated_train_strips_to_end_of_text():
    out = _strip_toolcall_leakage(
        "Working on it. update_component<arg_key>content</arg_key>"
        "<arg_value># Draft\nhalf-written and never closed"
    )
    assert out == "Working on it."


def test_dangling_arg_tags_removed():
    assert _strip_toolcall_leakage("before </arg_key> after") == "before  after"


def test_attribute_trains_removed_but_emails_survive():
    out = _strip_toolcall_leakage("Saving now NEW_PAGE@true APPEND@false done")
    assert "@true" not in out and "@false" not in out
    assert "Saving now" in out and "done" in out
    intact = "mail me at sam@example.com"
    assert _strip_toolcall_leakage(intact) == intact


def test_existing_wrapper_patterns_still_covered():
    dsml = (
        'I will check. <｜DSML｜tool_calls> <｜DSML｜invoke name="read_spreadsheet">'
        "</｜DSML｜invoke> </｜DSML｜tool_calls>"
    )
    assert _strip_toolcall_leakage(dsml) == "I will check."
    assert _strip_toolcall_leakage("<|tool_call|>{}<tool_call|> hi") == "hi"
    assert _strip_toolcall_leakage('[TOOL_CALLS][{"name":"x"}][/TOOL_CALLS] ok') == "ok"


def test_plain_markdown_untouched():
    text = "Nothing to strip here. **bold** and `code` survive.\n\n# Heading"
    assert _strip_toolcall_leakage(text) == text


def test_sanitize_text_response_fallback_preserved():
    assert "No agents are currently enabled" in _sanitize_text_response(RECORDED_FIXTURE)
    assert _sanitize_text_response("plain answer") == "plain answer"


def _narrative(text, chat_id=None):
    host = SimpleNamespace(_derive_chat_title=Orchestrator._derive_chat_title)
    return Orchestrator._chat_narrative(host, text, chat_id=chat_id)


def test_chat_narrative_clean_content_unchanged():
    out = _narrative("It is 72°F and sunny in Lexington.")
    assert out == [{"type": "text",
                    "content": "It is 72°F and sunny in Lexington.",
                    "variant": "markdown"}]


def test_chat_narrative_strips_embedded_leak():
    out = _narrative("The table is updated. " + RECORDED_FIXTURE)
    assert out[0]["content"] == "The table is updated."


def test_chat_narrative_honest_fallback_when_stripped_empty(caplog):
    with caplog.at_level(logging.WARNING, logger="Orchestrator"):
        out = _narrative(RECORDED_FIXTURE, chat_id="chat-9")
    assert out == [{"type": "text", "content": _LEAK_FALLBACK_TEXT,
                    "variant": "markdown"}]
    assert any("toolcall_leak.stripped_empty" in r.getMessage()
               and "surface=chat_narrative" in r.getMessage()
               and "chat-9" in r.getMessage() for r in caplog.records)


def test_doc_card_strips_embedded_leak_keeps_document():
    doc = "# Specific Aims\n\nAim 1 text.\n\n" + RECORDED_FIXTURE + "\n\nAim 2 text."
    card = Orchestrator._narrative_doc_card("chat-1", doc)
    body = card["content"][0]["content"]
    assert "Aim 1 text." in body and "Aim 2 text." in body
    assert "<arg_key>" not in body and "@true" not in body
    clean = Orchestrator._narrative_doc_card(
        "chat-1", "# Specific Aims\n\nAim 1 text.\n\nAim 2 text.")
    assert card["id"] == clean["id"]
    assert card["title"] == "Specific Aims"


def test_doc_card_honest_fallback_when_stripped_empty(caplog):
    with caplog.at_level(logging.WARNING, logger="Orchestrator"):
        card = Orchestrator._narrative_doc_card("chat-2", RECORDED_FIXTURE)
    assert card["content"][0]["content"] == _LEAK_FALLBACK_TEXT
    assert card["title"] == "Document"
    assert any("toolcall_leak.stripped_empty" in r.getMessage()
               and "surface=doc_card" in r.getMessage() for r in caplog.records)


def _summary_orch(llm_text):
    orch = Orchestrator.__new__(Orchestrator)
    orch._llm_audit_principals = lambda ws: ("u1", "u1")
    orch._LLMUnavailable = type("Unavailable", (Exception,), {})
    orch._CredentialSource = SimpleNamespace(USER="user")
    orch.audit_recorder = MagicMock()
    orch._accumulate_usage = lambda chat_id, usage: None
    orch._record_llm_call = AsyncMock()
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=llm_text))],
    )
    orch._resolve_llm_client_for = AsyncMock(
        return_value=(client, "system", SimpleNamespace(model="m")))
    return orch


async def test_tool_summary_strips_embedded_leak():
    orch = _summary_orch("Results look good. " + RECORDED_FIXTURE)
    cards = await orch._generate_tool_summary(None, [], chat_id="chat-3")
    body = cards[0]["content"][0]["content"]
    assert body == "Results look good."


async def test_tool_summary_honest_fallback_when_stripped_empty(caplog):
    orch = _summary_orch(RECORDED_FIXTURE)
    with caplog.at_level(logging.WARNING, logger="Orchestrator"):
        cards = await orch._generate_tool_summary(None, [], chat_id="chat-4")
    assert cards[0]["content"][0]["content"] == _LEAK_FALLBACK_TEXT
    assert any("toolcall_leak.stripped_empty" in r.getMessage()
               and "surface=tool_summary" in r.getMessage() for r in caplog.records)


def test_email_with_boolean_domain_label_survives():
    from orchestrator.orchestrator import _strip_toolcall_leakage
    text = "Email me at john@true.example.com today."
    assert _strip_toolcall_leakage(text) == text
    assert "NEW_PAGE@true" not in _strip_toolcall_leakage("done NEW_PAGE@true now")
    assert "@false" not in _strip_toolcall_leakage("SET_MODE@false end")
