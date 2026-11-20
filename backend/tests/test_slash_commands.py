"""Tests for the /slash-command catalog in orchestrator/slash_commands.py: discovery metadata and parsing."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import slash_commands  # noqa: E402


def test_command_list_exposes_discovery_metadata():
    cmds = slash_commands.command_list()
    names = {c["name"] for c in cmds}
    assert {"help", "agents", "summarize", "research", "weather",
            "download"} <= names
    assert all(c["usage"].startswith("/") for c in cmds)


def test_parse_splits_name_and_arguments_and_ignores_paths_and_prose():
    assert slash_commands.parse("/Summarize  https://example.com/a ") == ("summarize", "https://example.com/a")
    assert slash_commands.parse("/help") == ("help", "")
    assert slash_commands.parse("hello there") is None
    assert slash_commands.parse("/usr/local/bin") is None
    assert slash_commands.parse("") is None
