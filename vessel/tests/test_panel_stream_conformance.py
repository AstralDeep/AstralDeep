# Checks that the panel's real turn streamer emits history accepted by Vessel's transcript parser.

from __future__ import annotations

import asyncio
import base64
import os
import sys
from importlib import import_module
from pathlib import Path
from types import ModuleType

import pytest

from astralvessel import transcript

PANEL_SOURCE = "VESSEL_PANEL_STREAMER_SOURCE"
PANEL_PACKAGE = "_astralvessel_panel_streamer"
IMAGE_URL = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
             "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
PNG = base64.b64decode(IMAGE_URL.split(",", 1)[1])


def test_panel_stream_replays_two_tool_images(tmp_path, monkeypatch):
    source = os.environ.get(PANEL_SOURCE)
    if not source:
        pytest.skip(f"set {PANEL_SOURCE} to the panel source checkout")
    package_path = Path(source) / "astralvessel"
    assert (package_path / "agent.py").is_file(), f"panel streamer is missing from {package_path}"
    package = ModuleType(PANEL_PACKAGE)
    package.__path__ = [str(package_path)]
    monkeypatch.setitem(sys.modules, PANEL_PACKAGE, package)
    agent = import_module(f"{PANEL_PACKAGE}.agent")
    manifest = import_module(f"{PANEL_PACKAGE}.manifest")
    model = import_module(f"{PANEL_PACKAGE}.model")
    files = import_module(f"{PANEL_PACKAGE}.files")

    (tmp_path / "vessel.toml").write_text(
        '[agent]\nname = "image-test"\ndescription = "Test image transcript order."\n'
        '[model]\nmodel = "fixture"\nimages = true\n'
        '[tools]\nbuiltin = ["list", "glob"]\n'
        '[output]\nfiles = ["png"]\n',
        encoding="utf-8",
    )
    (tmp_path / "prompt.md").write_text("Return the two charts.", encoding="utf-8")
    replies = iter([
        model.StepResult(tool_calls=[
            model.ToolCall("call-1", "list", "{}"),
            model.ToolCall("call-2", "glob", "{}"),
        ]),
        model.StepResult(text="Both charts are ready."),
    ])

    class Model:
        async def stream(self, messages, tools):
            yield "done", next(replies)

    class ToolRunner:
        def __init__(self):
            self.output = []

        async def run(self, name, args, workspace=None, owner=""):
            self.output.append(files.ToolFile(f"{name}.png", "image/png", PNG))
            return "Chart ready.", False

        def take_files(self, workspace):
            output, self.output = self.output, []
            return output

        async def end_turn(self, workspace):
            pass

    streamed = []

    async def emit(event):
        pass

    async def on_message(message):
        streamed.append(message)

    turn = agent.Turn(manifest.load_package(tmp_path), Model(), ToolRunner(), emit, on_message=on_message)
    result = asyncio.run(turn.run([], "Show both charts.", conversation_id="test", turn_id="test"))
    assert result["status"] == "ok"
    assert transcript.parse_history(transcript.envelope(streamed)) == streamed
    assert [message["role"] for message in streamed] == ["user", "assistant", "tool", "tool",
                                                        "user", "user", "assistant"]
    assert [message["tool_call_id"] for message in streamed[2:4]] == ["call-1", "call-2"]
    assert [message["content"][0]["text"] for message in streamed[4:6]] == [
        "[image returned by tool list: list.png]",
        "[image returned by tool glob: glob.png]",
    ]
    for message in streamed[4:6]:
        image = message["content"][1]
        assert image["type"] == "image_url"
        assert image["image_url"]["url"] == IMAGE_URL
