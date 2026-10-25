"""Verifies complete provider-aware request accounting, offline encoder assets, and
compaction budget boundaries. The synthetic request histories cover multilingual,
code, JSON, tool-call, result, and multimodal content.
"""

from __future__ import annotations

import json
import socket
import stat
import urllib.request
from types import SimpleNamespace

import pytest
import tiktoken

from orchestrator import context_token_accounting as accounting
from orchestrator.compaction import compact_messages, get_context_window
from orchestrator.context_token_accounting import (
    AUDIO_TOKEN_ALLOWANCE,
    FALLBACK_CONTEXT_WINDOW,
    FILE_TOKEN_ALLOWANCE,
    IMAGE_TOKEN_ALLOWANCE,
    EncoderAssetError,
    TokenAccountant,
    TokenAccountingError,
    account_request_tokens,
    build_model_profile,
    fallback_profile,
    load_encoder_asset,
    resolve_model_profile,
    verify_encoder_assets,
)


def _summary_llm(websocket, prompt):
    assert websocket is None
    return SimpleNamespace(content="condensed history"), None


def _history(pairs: int, chars: int = 40) -> list[dict]:
    body = "x" * chars
    messages: list[dict] = []
    for index in range(pairs):
        messages.append({"role": "user", "content": f"q{index} {body}"})
        messages.append({"role": "assistant", "content": f"a{index} {body}"})
    return messages


def _request(messages: list[dict], tools: list[dict] | None = None, reserved: int = 256):
    accountant = TokenAccountant(resolve_model_profile("openai", "gpt-4o"))
    return accountant, accountant.account_request(messages, tools, reserved_output_tokens=reserved)


SAMPLE_MESSAGES = [
    {"role": "system", "content": "You are a careful assistant."},
    {"role": "user", "content": "你好，世界\nПривет мир\nconsole.log(\"hello\");"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "lookup", "arguments": '{"query":"東京","page":2}'},
        }],
    },
    {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": "lookup",
        "content": '{"items":["alpha","βeta"],"ok":true}',
    },
]
SAMPLE_TOOLS = [{
    "type": "function",
    "function": {
        "name": "lookup",
        "description": "Find a record",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "page": {"type": "integer"}},
            "required": ["query"],
        },
    },
}]


@pytest.mark.parametrize("provider,model,expected", [
    ("openai", "gpt-4o", [2, 3, 3, 12, 13, 3, 5]),
    ("openai", "gpt-4", [2, 6, 5, 12, 14, 5, 5]),
])
def test_reviewed_tokenizer_samples_are_exact(provider, model, expected):
    accountant = TokenAccountant(resolve_model_profile(provider, model))
    samples = [
        "hello world",
        "你好，世界",
        "Привет мир",
        'def f(x):\n    return {"a": 1}',
        '{"city": "Rome", "n": 12345}',
        "🙂🚀",
        'console.log("hello");',
    ]
    assert [accountant.count_text(text) for text in samples] == expected
    assert accountant.qualified_encoding is True


def test_complete_request_reconciles_with_mocked_provider_usage():
    accountant, estimate = _request(SAMPLE_MESSAGES, SAMPLE_TOOLS)
    mocked_prompt_tokens = estimate.message_tokens + estimate.tool_tokens
    mocked_usage = {
        "prompt_tokens": mocked_prompt_tokens,
        "completion_tokens": estimate.reserved_output_tokens,
        "total_tokens": mocked_prompt_tokens + estimate.reserved_output_tokens,
    }
    assert (estimate.message_tokens, estimate.tool_tokens) == (114, 55)
    assert estimate.total_tokens == mocked_usage["total_tokens"] == 425
    assert estimate.encoding == "o200k_base"
    assert estimate.multimodal_tokens == 0


def test_tool_arguments_results_and_pairing_fields_are_accounted_outside_content():
    accountant = TokenAccountant(resolve_model_profile("openai", "gpt-4o"))
    base = [
        {"role": "assistant", "content": "calling", "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "call_1", "name": "lookup", "content": "ok"},
    ]
    expanded_arguments = json.loads(json.dumps(base))
    expanded_arguments[0]["tool_calls"][0]["function"]["arguments"] = json.dumps(
        {"query": "東京", "payload": "x" * 400}, ensure_ascii=False
    )
    expanded_result = json.loads(json.dumps(base))
    expanded_result[1]["content"] = "result " + "y" * 400
    assert accountant.count_messages(expanded_arguments) > accountant.count_messages(base) + 50
    assert accountant.count_messages(expanded_result) > accountant.count_messages(base) + 50
    assert [message.get("tool_call_id") for message in expanded_arguments] == [None, "call_1"]


def test_multimodal_parts_use_conservative_allowances_without_counting_payload_bytes():
    accountant = TokenAccountant(resolve_model_profile("openai", "gpt-4o"))
    payload = "A" * 400_000
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{payload}"}},
            {"type": "input_audio", "input_audio": {"data": payload, "format": "wav"}},
            {"type": "file", "file": {"filename": "synthetic.txt", "file_data": payload}},
        ],
    }]
    estimate = accountant.account_request(messages, reserved_output_tokens=0)
    assert estimate.multimodal_tokens == (
        IMAGE_TOKEN_ALLOWANCE + AUDIO_TOKEN_ALLOWANCE + FILE_TOKEN_ALLOWANCE
        + 3 * accounting.CONTENT_PART_FRAMING_TOKENS
    )
    assert estimate.total_tokens < IMAGE_TOKEN_ALLOWANCE + AUDIO_TOKEN_ALLOWANCE + FILE_TOKEN_ALLOWANCE + 100


def test_unknown_provider_or_model_never_borrows_openai_encoding_or_window():
    assert resolve_model_profile("openrouter", "gpt-4o") is None
    assert resolve_model_profile("openai", None) is None
    assert resolve_model_profile("openai", "  ") is None
    assert get_context_window("gpt-4o", provider="openrouter") == FALLBACK_CONTEXT_WINDOW


@pytest.mark.parametrize("kind", ["file", "input_file", "image_url", "input_audio", "audio"])
def test_unknown_route_multimodal_payload_retains_utf8_byte_bound(kind):
    accountant = TokenAccountant(fallback_profile("custom", "synthetic-model"))
    small = [{"role": "user", "content": [{"type": kind, "data": "ok"}]}]
    large = [{"role": "user", "content": [{"type": kind, "data": "界" * 10_000}]}]
    small_estimate = accountant.account_request(small)
    large_estimate = accountant.account_request(large)
    assert large_estimate.multimodal_tokens >= 30_000
    assert large_estimate.total_tokens > large_estimate.context_window
    assert large_estimate.total_tokens > small_estimate.total_tokens
    assert get_context_window("cl100k_base") == FALLBACK_CONTEXT_WINDOW
    assert get_context_window("my-gpt-4o-compatible-model") == FALLBACK_CONTEXT_WINDOW
    assert get_context_window("gpt-4o", provider="openai") == 128_000
    assert get_context_window("gpt-4o-2024-08-06", provider="openai") == 128_000

    profile = fallback_profile("custom", "synthetic-model")
    accountant = TokenAccountant(profile)
    assert accountant.qualified_encoding is False
    assert accountant.fallback_reason == "profile_unqualified"
    assert accountant.count_text("hello world") == len(b"hello world")
    assert accountant.count_text("你好") == len("你好".encode())


def test_explicit_configured_encoding_and_ceiling_are_honored():
    profile = build_model_profile(
        "custom", "synthetic-model",
        context_window=4_096, encoding="cl100k_base", reserved_output_tokens=512,
    )
    accountant = TokenAccountant(profile)
    estimate = accountant.account_request(SAMPLE_MESSAGES, SAMPLE_TOOLS)
    assert accountant.qualified_encoding is True
    assert estimate.context_window == 4_096
    assert estimate.reserved_output_tokens == 512
    assert estimate.total_tokens == estimate.message_tokens + estimate.tool_tokens + 512
    assert accountant.count_text("hello world") == 2
    configured = account_request_tokens(
        SAMPLE_MESSAGES, SAMPLE_TOOLS, provider="custom", model="synthetic-model",
        context_window=4_096, encoding="cl100k_base", reserved_output_tokens=512,
    )
    assert configured.total_tokens == estimate.total_tokens
    smallest = build_model_profile(
        "custom", "synthetic-model", context_window=1_024, encoding="cl100k_base"
    )
    assert smallest.reserved_output_tokens == 128


@pytest.mark.parametrize("kwargs", [
    {"context_window": 0},
    {"context_window": True},
    {"context_window": 4_096, "reserved_output_tokens": 4_096},
    {"context_window": 4_096, "reserved_output_tokens": -1},
    {"context_window": 4_096, "encoding": ""},
    {"context_window": 2_000_001},
])
def test_invalid_explicit_accounting_bounds_fail_closed(kwargs):
    with pytest.raises(TokenAccountingError):
        build_model_profile("custom", "synthetic-model", **kwargs)
    with pytest.raises(TokenAccountingError):
        accountant = TokenAccountant(fallback_profile("custom", "synthetic-model"))
        accountant.account_request([], reserved_output_tokens=True)


def test_missing_unknown_and_corrupt_encoders_use_documented_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *args, **kwargs: pytest.fail("network used"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("network used"))
    monkeypatch.setattr(tiktoken, "get_encoding", lambda *args, **kwargs: pytest.fail("registry fetch used"))

    missing = TokenAccountant(resolve_model_profile("openai", "gpt-4o"), assets_dir=tmp_path)
    assert missing.qualified_encoding is False
    assert missing.fallback_reason == "asset_unavailable"
    assert missing.count_text("hello world") == len(b"hello world")

    unknown = TokenAccountant(
        build_model_profile("custom", "synthetic-model", context_window=4_096, encoding="not-a-known-encoding")
    )
    assert unknown.qualified_encoding is False
    assert unknown.fallback_reason == "encoding_unknown"

    (tmp_path / "cl100k_base.tiktoken").write_bytes(b"corrupt asset")
    corrupt = TokenAccountant(resolve_model_profile("openai", "gpt-4"), assets_dir=tmp_path)
    assert corrupt.qualified_encoding is False
    assert corrupt.fallback_reason == "asset_digest_mismatch"
    with pytest.raises(EncoderAssetError, match="asset_digest_mismatch"):
        load_encoder_asset("cl100k_base", tmp_path)
    with pytest.raises(EncoderAssetError, match="asset_digest_mismatch"):
        verify_encoder_assets(tmp_path)


def test_bundled_assets_match_reviewed_digests_without_first_use_download(monkeypatch):
    accounting._load_encoder_cached.cache_clear()
    monkeypatch.setattr(socket.socket, "connect", lambda *args, **kwargs: pytest.fail("network used"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("network used"))
    monkeypatch.setattr(tiktoken, "get_encoding", lambda *args, **kwargs: pytest.fail("registry fetch used"))
    assert verify_encoder_assets() == {
        "cl100k_base": "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
        "o200k_base": "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
    }
    accountant = TokenAccountant(resolve_model_profile("openai", "gpt-4o"))
    assert accountant.count_text("hello world") == 2


def test_malformed_content_and_objects_are_counted_conservatively():
    accountant = TokenAccountant(resolve_model_profile("openai", "gpt-4o"))
    cyclic: dict = {}
    cyclic["self"] = cyclic
    messages = [
        {"role": "user", "content": cyclic},
        {"role": "assistant", "content": b"\xff\xfe", "tool_calls": [{"function": {"arguments": "not-json"}}]},
        {"role": "tool", "content": None},
        "not-a-message",
        None,
    ]
    assert accountant.count_messages(messages) > 0
    assert accountant.count_tools({"not": "a list"}) > 0


@pytest.mark.asyncio
async def test_compaction_boundary_accounts_tools_and_reserved_output():
    messages = [
        {"role": "system", "content": "sys"},
        *_history(pairs=6, chars=80),
        {"role": "user", "content": "current question"},
    ]
    reserved = 100
    accountant = TokenAccountant(resolve_model_profile("openai", "gpt-4o"))
    message_tokens = accountant.count_messages(messages)
    tool_tokens = accountant.count_tools(SAMPLE_TOOLS)
    required_window = message_tokens + tool_tokens + reserved

    untouched, fired = await compact_messages(
        messages, "gpt-4o", _summary_llm,
        provider="openai", tools_desc=SAMPLE_TOOLS,
        reserved_output_tokens=reserved, context_window=required_window,
        encoding_name="o200k_base", budget_ratio=1.0,
    )
    assert fired is False and untouched == messages

    compacted, fired = await compact_messages(
        messages, "gpt-4o", _summary_llm,
        provider="openai", tools_desc=SAMPLE_TOOLS,
        reserved_output_tokens=reserved, context_window=required_window - 1,
        encoding_name="o200k_base", budget_ratio=1.0,
    )
    assert fired is True
    assert accountant.count_messages(compacted) < message_tokens
    assert compacted[-1] == {"role": "user", "content": "current question"}


def test_multilingual_code_and_json_are_not_undercounted_by_character_heuristics():
    accountant = TokenAccountant(resolve_model_profile("openai", "gpt-4o"))
    text = "你好，世界\nПривет мир\n" + json.dumps({"東京": ["α", "β"]}, ensure_ascii=False) + "\nconsole.log('x');"
    assert accountant.count_text(text) > len(text) // 4


def test_request_estimate_properties_and_none_sequence():
    accountant, estimate = _request([], reserved=0)
    assert estimate.qualified_encoding is True
    assert accountant.count_messages(None) == 0
    assert accountant.count_messages([]) == 0
    with pytest.raises(TokenAccountingError, match="messages_not_sequence"):
        accountant.count_messages("not-a-list")


def test_nonstring_content_and_nonmapping_objects_are_serialized():
    accountant = TokenAccountant(resolve_model_profile("openai", "gpt-4o"))
    assert accountant.count_text({"nested": [1, 2, 3]}) > 0

    class Dumpable:
        def model_dump(self, mode):
            assert mode == "json"
            return {"role": "user", "content": "dumped"}

    class BrokenDump:
        def model_dump(self, mode):
            raise RuntimeError("synthetic dump failure")

    class ListDump:
        def model_dump(self, mode):
            return ["not", "a", "mapping"]

    class BadText:
        def __str__(self):
            raise TypeError("synthetic serialization failure")

        def __repr__(self):
            raise RuntimeError("synthetic repr failure")

    messages = [Dumpable(), BrokenDump(), ListDump(), {"role": "user", "content": BadText()}]
    assert accountant.count_messages(messages) > 0
    assert accounting._json_text(BadText()).startswith("<unrepresentable ")


def test_qualified_encoder_runtime_failure_uses_byte_fallback(monkeypatch):
    accountant = TokenAccountant(resolve_model_profile("openai", "gpt-4o"))
    assert accountant.qualified_encoding is True

    class BrokenEncoder:
        def encode_ordinary(self, text):
            raise ValueError("synthetic encoder failure")

    monkeypatch.setattr(accountant, "_encoder", BrokenEncoder())
    assert accountant.count_text("你好") == len("你好".encode())


def test_multimodal_text_fields_are_counted(monkeypatch):
    accountant = TokenAccountant(resolve_model_profile("openai", "gpt-4o"))
    messages = [{"role": "user", "content": [
        {"type": "image_url", "text": "caption text", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]
    estimate = accountant.account_request(messages, reserved_output_tokens=0)
    assert estimate.multimodal_tokens == IMAGE_TOKEN_ALLOWANCE + accounting.CONTENT_PART_FRAMING_TOKENS
    assert estimate.message_tokens > accountant.count_text("caption text")


def test_nonregular_oversized_and_malformed_assets_fail_closed(tmp_path, monkeypatch):
    asset = tmp_path / "cl100k_base.tiktoken"
    asset.mkdir()
    with pytest.raises(EncoderAssetError, match="asset_unavailable"):
        load_encoder_asset("cl100k_base", tmp_path)

    asset.rmdir()
    asset.write_bytes(b"x" * 32)
    monkeypatch.setattr(accounting, "_MAX_ASSET_BYTES", 16)
    with pytest.raises(EncoderAssetError, match="asset_unavailable"):
        load_encoder_asset("cl100k_base", tmp_path)
    monkeypatch.setattr(
        accounting.os, "fstat",
        lambda descriptor: SimpleNamespace(st_mode=stat.S_IFREG, st_size=1),
    )
    with pytest.raises(EncoderAssetError, match="asset_too_large"):
        load_encoder_asset("cl100k_base", tmp_path)
    monkeypatch.setattr(accounting, "_MAX_ASSET_BYTES", 16 * 1024 * 1024)
    monkeypatch.undo()

    with pytest.raises(EncoderAssetError, match="asset_malformed"):
        accounting._parse_ranks(b"\n")
    with pytest.raises(EncoderAssetError, match="asset_malformed"):
        accounting._parse_ranks(b"not-base64 1")
    with pytest.raises(EncoderAssetError, match="asset_malformed"):
        accounting._parse_ranks(b"YQ== 1\nYQ== 2")
    with pytest.raises(EncoderAssetError, match="asset_malformed"):
        accounting._parse_ranks(b"YQ== -1")


def test_encoder_import_initialization_and_parse_failures_are_classified(tmp_path, monkeypatch):
    import sys

    original = sys.modules.get("tiktoken", tiktoken)
    accounting._load_encoder_cached.cache_clear()
    monkeypatch.setitem(sys.modules, "tiktoken", None)
    with pytest.raises(EncoderAssetError, match="encoder_unavailable"):
        accounting.load_encoder_asset("cl100k_base")
    monkeypatch.setitem(sys.modules, "tiktoken", original)
    accounting._load_encoder_cached.cache_clear()

    monkeypatch.setattr(original, "Encoding", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(EncoderAssetError, match="encoder_initialization_failed"):
        accounting.load_encoder_asset("cl100k_base")
    accounting._load_encoder_cached.cache_clear()

    monkeypatch.setattr(accounting, "_parse_ranks", lambda data: (_ for _ in ()).throw(
        EncoderAssetError("asset_malformed")
    ))
    with pytest.raises(EncoderAssetError, match="asset_malformed"):
        accounting.load_encoder_asset("cl100k_base")
    accounting._load_encoder_cached.cache_clear()


@pytest.mark.asyncio
async def test_compaction_empty_invalid_bounds_and_request_estimator():
    assert await compact_messages([], "gpt-4o", _summary_llm) == ([], False)
    messages = [{"role": "system", "content": "sys"}, *_history(pairs=6), {"role": "user", "content": "q"}]
    assert await compact_messages(messages, "gpt-4o", _summary_llm, context_window=0) == (messages, False)

    accountant = TokenAccountant(resolve_model_profile("openai", "gpt-4o"))
    expected = accountant.account_request(SAMPLE_MESSAGES, SAMPLE_TOOLS, reserved_output_tokens=256).total_tokens
    assert accounting.account_request_tokens(
        SAMPLE_MESSAGES, SAMPLE_TOOLS, provider="openai", model="gpt-4o", reserved_output_tokens=256
    ).total_tokens == expected
    from orchestrator.compaction import estimate_request_tokens

    assert estimate_request_tokens(
        SAMPLE_MESSAGES, SAMPLE_TOOLS, "gpt-4o", "openai", reserved_output_tokens=256
    ) == expected
