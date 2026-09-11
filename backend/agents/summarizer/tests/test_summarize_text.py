"""summarize_text + defensive LLM-JSON parsing tests (all LLM calls stubbed)."""
import json

from agents.summarizer import mcp_tools
from agents.summarizer.mcp_tools import (
    INPUT_CAP,
    _normalize_summary,
    _parse_llm_json,
    _strip_fences,
    summarize_text,
)

GOOD_JSON = json.dumps({
    "tldr": "The text is about snakes.",
    "key_points": ["Snakes are reptiles", "Some are venomous"],
    "quotes": ["A snake in the grass."],
})


# ---------------------------------------------------------------------------
# Defensive JSON parsing
# ---------------------------------------------------------------------------


def test_strip_fences_json_fence() -> None:
    assert _strip_fences("```json\n{\"a\": 1}\n```") == '{"a": 1}'


def test_strip_fences_bare_fence() -> None:
    assert _strip_fences("```\n{\"a\": 1}\n```") == '{"a": 1}'


def test_strip_fences_no_fence_passthrough() -> None:
    assert _strip_fences('{"a": 1}') == '{"a": 1}'


def test_parse_llm_json_strict() -> None:
    assert _parse_llm_json(GOOD_JSON)["tldr"] == "The text is about snakes."


def test_parse_llm_json_fenced() -> None:
    assert _parse_llm_json(f"```json\n{GOOD_JSON}\n```")["tldr"] == \
        "The text is about snakes."


def test_parse_llm_json_prose_wrapped() -> None:
    raw = f"Sure! Here is the JSON you asked for:\n{GOOD_JSON}\nHope that helps."
    assert _parse_llm_json(raw)["key_points"] == [
        "Snakes are reptiles", "Some are venomous",
    ]


def test_parse_llm_json_malformed_returns_none() -> None:
    assert _parse_llm_json("this is not json at all") is None


def test_parse_llm_json_non_dict_returns_none() -> None:
    assert _parse_llm_json("[1, 2, 3]") is None


def test_parse_llm_json_unparseable_brace_block_returns_none() -> None:
    assert _parse_llm_json("prefix {not: valid json} suffix") is None


def test_normalize_summary_coerces_shapes() -> None:
    summary = _normalize_summary({
        "tldr": 42,
        "key_points": "single point",
        "quotes": [None, "  q1  ", ""],
    })
    assert summary["tldr"] == "42"
    assert summary["key_points"] == ["single point"]
    assert summary["quotes"] == ["None", "q1"]


def test_normalize_summary_empty_uses_fallback() -> None:
    summary = _normalize_summary({}, raw_fallback="raw text")
    assert summary["tldr"] == "raw text"
    assert summary["key_points"] == []


# ---------------------------------------------------------------------------
# summarize_text
# ---------------------------------------------------------------------------


def test_summarize_text_renders_three_tabs(fake_openai) -> None:
    fake_openai(GOOD_JSON)
    result = summarize_text(text="Snakes are long reptiles.")
    tabs = result["_ui_components"][0]
    assert tabs["type"] == "tabs"
    assert [tab["label"] for tab in tabs["tabs"]] == [
        "TL;DR", "Key points", "Notable quotes",
    ]
    tldr = tabs["tabs"][0]["content"][0]
    assert tldr["type"] == "text"
    assert tldr["variant"] == "markdown"
    assert tldr["content"] == "The text is about snakes."
    key_points = tabs["tabs"][1]["content"][0]
    assert key_points["type"] == "list"
    assert key_points["items"] == ["Snakes are reptiles", "Some are venomous"]
    quotes = tabs["tabs"][2]["content"][0]
    assert quotes["items"] == ["A snake in the grass."]
    assert result["_data"]["truncated"] is False


def test_summarize_text_parses_fenced_llm_output(fake_openai) -> None:
    fake_openai(f"```json\n{GOOD_JSON}\n```")
    result = summarize_text(text="Snakes.")
    assert result["_data"]["summary"]["tldr"] == "The text is about snakes."


def test_summarize_text_malformed_output_falls_back_to_tldr(fake_openai) -> None:
    fake_openai("The text is mostly about cats, honestly.")
    result = summarize_text(text="Cats.")
    summary = result["_data"]["summary"]
    assert summary["tldr"] == "The text is mostly about cats, honestly."
    assert summary["key_points"] == []
    tabs = result["_ui_components"][0]
    assert tabs["tabs"][1]["content"][0]["items"] == ["(none identified)"]


def test_summarize_text_truncation_notice_and_capped_prompt(fake_openai) -> None:
    fake_cls = fake_openai(GOOD_JSON)
    long_text = "x" * (INPUT_CAP + 5_000)
    result = summarize_text(text=long_text)
    alert = result["_ui_components"][0]
    assert alert["type"] == "alert"
    assert alert["variant"] == "info"
    assert "24,000" in alert["message"]
    assert result["_ui_components"][1]["type"] == "tabs"
    assert result["_data"]["truncated"] is True
    assert result["_data"]["input_characters"] == INPUT_CAP + 5_000
    sent = fake_cls.calls_log[-1]["messages"][1]["content"]
    assert len(sent) <= INPUT_CAP + 200  # capped text + small prompt preamble


def test_summarize_text_focus_lands_in_prompt(fake_openai) -> None:
    fake_cls = fake_openai(GOOD_JSON)
    summarize_text(text="Snakes.", focus="venom risks")
    sent = fake_cls.calls_log[-1]["messages"][1]["content"]
    assert "venom risks" in sent


def test_summarize_text_makes_exactly_one_llm_call(fake_openai) -> None:
    fake_cls = fake_openai(GOOD_JSON)
    summarize_text(text="Snakes.")
    assert len(fake_cls.calls_log) == 1


def test_summarize_text_empty_input_is_error() -> None:
    result = summarize_text(text="   ")
    assert result["_ui_components"][0]["variant"] == "error"


def test_summarize_text_llm_unavailable(no_llm_credentials) -> None:
    result = summarize_text(text="Snakes.")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "LLM" in alert["message"]


def test_summarize_text_llm_exception_is_error(fake_openai) -> None:
    fake_openai(RuntimeError("model exploded"))
    result = summarize_text(text="Snakes.")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "summary could not be generated" in alert["message"]
    assert "model exploded" not in alert["message"]


def test_summarize_text_prefers_session_credentials(fake_openai) -> None:
    """Per-session credential resolution mirrors the general agent (006)."""
    fake_cls = fake_openai(GOOD_JSON)
    summarize_text(
        text="Snakes.",
        _session_llm_credentials={
            "OPENAI_API_KEY": "session-key",
            "OPENAI_BASE_URL": "https://llm.example.com/v1",
            "LLM_MODEL": "session-model",
        },
    )
    assert fake_cls.last_init == {
        "api_key": "session-key", "base_url": "https://llm.example.com/v1",
    }
    assert fake_cls.calls_log[-1]["model"] == "session-model"


def test_encrypted_agent_credentials_are_ignored(fake_openai, monkeypatch) -> None:
    """With _credentials_encrypted, the bundle must not be read for LLM creds.

    Feature 054 removed the env fallback that used to catch this case, so an
    encrypted bundle with no session credentials now lands on the honest
    'LLM not configured' error path — and no client is ever built from the
    still-encrypted bundle key (or from the now-inert env var).
    """
    fake_cls = fake_openai(GOOD_JSON)
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")  # must stay inert (054)
    result = summarize_text(
        text="Snakes.",
        _credentials={"OPENAI_API_KEY": "bundle-key"},
        _credentials_encrypted=True,
    )
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "LLM" in alert["message"]
    assert fake_cls.last_init == {}, "no client may be built from the encrypted bundle or env"


def test_no_session_key_leaked_in_output(fake_openai) -> None:
    fake_openai(GOOD_JSON)
    result = summarize_text(
        text="Snakes.",
        _session_llm_credentials={"OPENAI_API_KEY": "sentinel-llm-key"},
    )
    assert "sentinel-llm-key" not in json.dumps(result)


def test_resolve_llm_client_returns_none_without_key(no_llm_credentials) -> None:
    client, model = mcp_tools._resolve_llm_client({})
    assert client is None
    assert isinstance(model, str) and model
