"""Tests for llm_config/log_scrub.py: redact_llm_config catches keys in dicts, lists,
JSON strings, and free text across providers, and LLMKeyRedactionFilter scrubs both
string and tuple-shaped logging record args.
"""

from __future__ import annotations

import json
import logging


from llm_config.log_scrub import (
    LLMKeyRedactionFilter,
    install_redaction_filter,
    redact_llm_config,
)


class TestRedactLLMConfig:
    def test_redacts_api_key_field_in_dict(self):
        out = redact_llm_config({"api_key": "sk-real-key", "base_url": "https://x"})
        assert out == {"api_key": "<redacted>", "base_url": "https://x"}

    def test_redacts_nested_api_key_field(self):
        out = redact_llm_config({"outer": {"api_key": "sk-secret"}})
        assert out == {"outer": {"api_key": "<redacted>"}}

    def test_redacts_in_list_of_dicts(self):
        out = redact_llm_config([{"api_key": "k1"}, {"api_key": "k2"}])
        assert out == [{"api_key": "<redacted>"}, {"api_key": "<redacted>"}]

    def test_redacts_keys_in_free_text(self):
        out = redact_llm_config("auth header: Bearer sk-1234567890abcdef1234")
        assert "sk-1234567890abcdef1234" not in out
        assert "<redacted>" in out

    def test_redacts_keys_in_json_string(self):
        json_str = json.dumps({"api_key": "sk-abc1234567890abcdef12"})
        out = redact_llm_config(json_str)
        assert "sk-abc1234567890abcdef12" not in out
        parsed = json.loads(out)
        assert parsed["api_key"] == "<redacted>"

    def test_handles_groq_xai_openrouter_prefixes(self):
        text = (
            "tokens=gsk_groq567890abcdef1234567 "
            "xai-x567890abcdef1234567 "
            "or-router567890abcdef1234567"
        )
        out = redact_llm_config(text)
        assert "gsk_groq" not in out or out.count("gsk_") == 0
        assert "gsk_groq567890abcdef1234567" not in out
        assert "xai-x567890abcdef1234567" not in out
        assert "or-router567890abcdef1234567" not in out

    def test_redacts_google_aiza_tokens(self):
        out = redact_llm_config("google key: AIzaSyA1234567890abcdefghijk-xyz")
        assert "AIzaSyA1234567890abcdefghijk-xyz" not in out
        assert "<redacted>" in out

    def test_redacts_anthropic_sk_ant_keys(self):
        out = redact_llm_config("anthropic: sk-ant-api03-abcdef1234567890abcdef")
        assert "sk-ant-api03-abcdef1234567890abcdef" not in out
        assert "<redacted>" in out

    def test_passes_through_short_strings(self):
        assert redact_llm_config("sk-test") == "sk-test"

    def test_passes_through_non_string_non_dict(self):
        assert redact_llm_config(42) == 42
        assert redact_llm_config(None) is None
        assert redact_llm_config(3.14) == 3.14


class TestLLMKeyRedactionFilter:
    def test_filter_scrubs_record_msg(self):
        f = LLMKeyRedactionFilter()
        rec = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="key=sk-abcdef1234567890abcd", args=(), exc_info=None,
        )
        assert f.filter(rec) is True
        assert "sk-abcdef1234567890abcd" not in rec.msg

    def test_filter_scrubs_record_args_dict(self):
        f = LLMKeyRedactionFilter()
        rec = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="config=%(c)s", args=({"c": {"api_key": "sk-leak"}},),
            exc_info=None,
        )
        assert isinstance(rec.args, dict)
        f.filter(rec)
        assert rec.args["c"]["api_key"] == "<redacted>"

    def test_filter_scrubs_record_args_tuple(self):
        f = LLMKeyRedactionFilter()
        rec = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="cfg=%s extra=%s",
            args=({"api_key": "sk-leak"}, "other"),
            exc_info=None,
        )
        assert isinstance(rec.args, tuple)
        f.filter(rec)
        assert rec.args[0]["api_key"] == "<redacted>"
        assert rec.args[1] == "other"


class TestInstallRedactionFilter:
    def test_idempotent(self):
        logger_name = "llm_config.test_install_idempotent"
        log = logging.getLogger(logger_name)
        for f in list(log.filters):
            log.removeFilter(f)
        install_redaction_filter(logger_name)
        install_redaction_filter(logger_name)
        n = sum(1 for f in log.filters if isinstance(f, LLMKeyRedactionFilter))
        assert n == 1
