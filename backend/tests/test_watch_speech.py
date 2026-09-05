"""051 — spoken rendition for watch sockets + the watch degradation sweep.

Covers the helper (`orchestrator/watch_speech.py`), the additive `speech`
wire field (absent-not-null; contracts/spoken-rendition.md), and the US5 seed
sweep: every component type in the committed manifest survives watch-profile
adaptation without error, and the speakable core produces a non-empty spoken
rendition. DB-free.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.watch_speech import build_speech, speech_for_profile  # noqa: E402
from rote.adapter import ComponentAdapter  # noqa: E402
from rote.capabilities import DeviceProfile  # noqa: E402
from shared.protocol import UIRender, UIUpsert  # noqa: E402

MANIFEST = json.loads(
    (
        BACKEND_DIR.parent
        / "components"
        / "AstralProjection"
        / "contracts"
        / "ui_protocol.json"
    ).read_text()
)

WATCH = DeviceProfile.from_dict({"device_type": "watch"})
IOS = DeviceProfile.from_dict({"device_type": "ios"})
BROWSER = DeviceProfile.from_dict({"device_type": "browser"})


def _sample(component_type: str) -> dict:
    """A minimal plausible instance of every manifest component type,
    following astralprims field conventions."""
    base = {"type": component_type, "title": f"{component_type} sample"}
    extra = {
        "text": {"content": "Weather for Lexington: 72 and clear."},
        "alert": {"message": "Job finished.", "variant": "info"},
        "card": {"content": [{"type": "text", "content": "inside a card"}]},
        "container": {"content": [{"type": "text", "content": "inside"}]},
        "grid": {"content": [{"type": "text", "content": "cell"}], "columns": 2},
        "collapsible": {"content": [{"type": "text", "content": "hidden"}]},
        "tabs": {"tabs": [{"label": "One", "content": [{"type": "text", "content": "t1"}]}]},
        "table": {"headers": ["a", "b", "c"], "rows": [[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11, 12]]},
        "list": {"items": ["first", "second", "third"]},
        "keyvalue": {"pairs": [{"key": "temp", "value": "72"}]},
        "metric": {"value": "72", "label": "degrees"},
        "badge": {"label": "OK", "variant": "success"},
        "hero": {"heading": "Big headline", "subheading": "small print"},
        "timeline": {"items": [{"label": "step 1"}, {"label": "step 2"}]},
        "rating": {"value": 4, "max": 5},
        "progress": {"value": 40, "max": 100},
        "code": {"content": "print('hi')", "language": "python"},
        "bar_chart": {"data": [{"label": "a", "value": 1}]},
        "line_chart": {"data": [{"label": "a", "value": 1}]},
        "pie_chart": {"data": [{"label": "a", "value": 1}]},
        "plotly_chart": {"figure": {"data": []}},
        "image": {"url": "https://example.com/x.png", "alt": "an image"},
        "audio": {"url": "https://example.com/x.mp3"},
        "button": {"label": "Do it", "action": "noop"},
        "input": {"label": "Name", "name": "name"},
        "file_upload": {"label": "Upload"},
        "file_download": {"filename": "report.pdf", "url": "https://example.com/r.pdf"},
        "download_card": {"filename": "report.pdf", "url": "https://example.com/r.pdf"},
        "color_picker": {"label": "Color"},
        "param_picker": {"params": []},
        "chat_history": {"messages": []},
        "generative": {"content": []},
        "skeleton": {},
        "divider": {},
        "theme_apply": {"theme": "dark"},
    }
    base.update(extra.get(component_type, {}))
    return base


# ---------------------------------------------------------------------------
# build_speech / speech_for_profile
# ---------------------------------------------------------------------------

def test_build_speech_ssml_and_text():
    speech = build_speech([
        {"type": "text", "content": "Weather & sky"},
        {"type": "metric", "title": "Temp", "value": "72"},
    ])
    assert speech is not None
    assert speech["ssml"].startswith("<speak>")
    assert "&amp;" in speech["ssml"]            # SSML stays escaped
    assert "Temp" in speech["text"] and "72" in speech["text"]
    assert "<" not in speech["text"]            # tags stripped
    assert "&amp;" not in speech["text"]        # entities unescaped in fallback
    assert "Weather & sky" in speech["text"]


def test_build_speech_nothing_speakable():
    assert build_speech(None) is None
    assert build_speech([]) is None
    assert build_speech(["not-a-dict"]) is None


def test_build_speech_fails_open_when_voice_target_raises(monkeypatch):
    """A voice-rendition exception must yield None (visual delivery unaffected),
    never propagate."""
    import webrender.voice as voice_mod

    def _boom(_comps):
        raise RuntimeError("voice target exploded")

    monkeypatch.setattr(voice_mod, "render_voice", _boom)
    assert build_speech([{"type": "text", "content": "hello"}]) is None


def test_build_speech_none_when_rendition_is_blank(monkeypatch):
    """Tag-only / whitespace SSML collapses to empty text ⇒ no speech."""
    import webrender.voice as voice_mod
    monkeypatch.setattr(voice_mod, "render_voice", lambda _c: "<speak>   </speak>")
    assert build_speech([{"type": "text", "content": "x"}]) is None


def test_speech_for_profile_fails_open_on_bad_profile():
    """A profile whose device_type access raises degrades to None, not an error."""
    class _Exploding:
        @property
        def device_type(self):
            raise RuntimeError("no device type")

    assert speech_for_profile(_Exploding(), [{"type": "text", "content": "hi"}]) is None


def test_speech_only_for_watch_profile():
    comps = [{"type": "text", "content": "hello"}]
    assert speech_for_profile(WATCH, comps) is not None
    assert speech_for_profile(IOS, comps) is None
    assert speech_for_profile(BROWSER, comps) is None
    assert speech_for_profile(None, comps) is None


# ---------------------------------------------------------------------------
# Wire shape: absent, not null (contracts/spoken-rendition.md)
# ---------------------------------------------------------------------------

def test_speech_field_absent_when_none():
    frame = json.loads(UIRender(components=[{"type": "text"}]).to_json())
    assert "speech" not in frame
    up = json.loads(UIUpsert(chat_id="c1", ops=[]).to_json())
    assert "speech" not in up


def test_speech_field_present_for_watch_payloads():
    speech = {"ssml": "<speak><s>hi</s></speak>", "text": "hi"}
    frame = json.loads(UIRender(components=[], speech=speech).to_json())
    assert frame["speech"] == speech
    up = json.loads(UIUpsert(chat_id="c1", ops=[], speech=speech).to_json())
    assert up["speech"] == speech


# ---------------------------------------------------------------------------
# US5 seed sweep (T047): all 35 manifest types through the watch profile.
# ---------------------------------------------------------------------------

def test_manifest_lists_expected_vocabulary():
    assert len(MANIFEST["component_types"]) == 35
    names = {item["name"] for item in MANIFEST["push_types"]}
    assert len(MANIFEST["push_types"]) == len(names) == 72
    assert {"voice_local_turn_bound", "voice_local_announcement",
            "voice_local_session_ready"} <= names


def test_every_component_type_survives_watch_adaptation():
    for ctype in MANIFEST["component_types"]:
        adapted = ComponentAdapter.adapt([_sample(ctype)], WATCH)
        assert isinstance(adapted, list), ctype
        for comp in adapted:
            assert isinstance(comp, dict) and comp.get("type"), ctype
        # the watch profile's own bounds hold: no over-wide tables sneak out
        for comp in adapted:
            if comp.get("type") == "table":
                assert len(comp.get("rows", [])) <= WATCH.max_table_rows, ctype
                assert all(len(r) <= WATCH.max_table_cols for r in comp.get("rows", [])), ctype


def test_speakable_core_produces_speech_after_adaptation():
    speakable = ("text", "alert", "metric", "table", "list", "keyvalue",
                 "card", "badge", "hero", "timeline")
    for ctype in speakable:
        adapted = ComponentAdapter.adapt([_sample(ctype)], WATCH)
        speech = build_speech(adapted)
        assert speech and speech["text"], f"no spoken rendition for {ctype}"


def test_whole_manifest_speech_is_fail_open():
    # No component type may ever make the speech helper raise — worst case is
    # a silent (visual-only) delivery.
    for ctype in MANIFEST["component_types"]:
        adapted = ComponentAdapter.adapt([_sample(ctype)], WATCH)
        build_speech(adapted)  # must not raise
