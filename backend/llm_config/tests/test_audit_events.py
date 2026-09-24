"""Tests for llm_config/audit_events.py: the API-key guard rejects literal and
key-shaped-substring payloads across providers, and each helper emits the right
recorder shape for every action/scope/credential-source combination.
"""

from __future__ import annotations

from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_config.audit_events import (
    _assert_no_api_key,
    record_llm_call,
    record_llm_config_change,
    record_llm_unconfigured,
)
from llm_config.types import CredentialSource, ResolvedConfig


class TestAssertNoApiKey:
    def test_raises_on_literal_api_key_field(self):
        with pytest.raises(ValueError, match="api_key"):
            _assert_no_api_key({"api_key": "sk-anything"})

    def test_raises_on_nested_api_key_field(self):
        with pytest.raises(ValueError, match="api_key"):
            _assert_no_api_key({"outer": {"api_key": "sk-anything"}})

    def test_raises_on_openai_key_substring_in_free_text(self):
        with pytest.raises(ValueError, match="API key"):
            _assert_no_api_key(
                {"description": "user said: my key is sk-1234567890abcdef1234"}
            )

    def test_accepts_clean_payload(self):
        _assert_no_api_key({"action": "created", "base_url": "https://x.example/v1", "model": "m"})

    def test_short_strings_starting_with_sk_dash_are_not_flagged(self):
        _assert_no_api_key({"note": "sk-test"})

    def test_raises_on_google_aiza_key(self):
        with pytest.raises(ValueError, match="API key"):
            _assert_no_api_key(
                {"description": "key AIzaSyA1234567890abcdefghijk-xyz here"})

    def test_raises_on_anthropic_sk_ant_key(self):
        with pytest.raises(ValueError, match="API key"):
            _assert_no_api_key(
                {"description": "sk-ant-api03-abcdef1234567890abcdef"})


@pytest.fixture
def fake_recorder():
    rec = MagicMock()
    rec.record = AsyncMock()
    return rec


def _captured_events(rec) -> List:
    return [call.args[0] for call in rec.record.await_args_list]


class TestRecordLLMConfigChange:
    @pytest.mark.asyncio
    async def test_created_action(self, fake_recorder):
        await record_llm_config_change(
            fake_recorder,
            actor_user_id="u1",
            auth_principal="u1",
            action="created",
            base_url="https://x.example/v1",
            model="model-a",
            transport="ws",
        )
        ev = _captured_events(fake_recorder)[0]
        assert ev.event_class == "llm_config_change"
        assert ev.action_type == "llm_config.created"
        assert ev.outcome == "success"
        assert ev.inputs_meta == {
            "action": "created",
            "transport": "ws",
            "scope": "user",
            "base_url": "https://x.example/v1",
            "model": "model-a",
        }
        assert ev.outputs_meta == {}

    @pytest.mark.asyncio
    async def test_tested_failure_records_outcome_failure(self, fake_recorder):
        await record_llm_config_change(
            fake_recorder,
            actor_user_id="u1",
            auth_principal="u1",
            action="tested",
            base_url="x",
            model="m",
            transport="rest",
            result="failure",
            error_class="auth_failed",
        )
        ev = _captured_events(fake_recorder)[0]
        assert ev.outcome == "failure"
        assert ev.outputs_meta == {"result": "failure", "error_class": "auth_failed"}

    @pytest.mark.asyncio
    async def test_cleared_action_omits_base_url_when_none(self, fake_recorder):
        await record_llm_config_change(
            fake_recorder,
            actor_user_id="u1",
            auth_principal="u1",
            action="cleared",
            base_url=None,
            model=None,
            transport="ws",
        )
        ev = _captured_events(fake_recorder)[0]
        assert "base_url" not in ev.inputs_meta
        assert "model" not in ev.inputs_meta

    @pytest.mark.asyncio
    async def test_unknown_action_raises(self, fake_recorder):
        with pytest.raises(ValueError):
            await record_llm_config_change(
                fake_recorder,
                actor_user_id="u",
                auth_principal="u",
                action="rotated",
                base_url="x",
                model="m",
                transport="ws",
            )

    @pytest.mark.asyncio
    async def test_tested_requires_result(self, fake_recorder):
        with pytest.raises(ValueError):
            await record_llm_config_change(
                fake_recorder,
                actor_user_id="u",
                auth_principal="u",
                action="tested",
                base_url="x",
                model="m",
                transport="rest",
                result=None,
            )

    @pytest.mark.asyncio
    async def test_explicit_system_scope_is_recorded(self, fake_recorder):
        await record_llm_config_change(
            fake_recorder,
            actor_user_id="admin-1",
            auth_principal="admin-1",
            action="updated",
            base_url="https://api.openai.com/v1",
            model="gpt-4o",
            transport="ws",
            scope="system",
        )
        ev = _captured_events(fake_recorder)[0]
        assert ev.inputs_meta["scope"] == "system"

    @pytest.mark.asyncio
    async def test_unknown_scope_raises(self, fake_recorder):
        with pytest.raises(ValueError, match="scope"):
            await record_llm_config_change(
                fake_recorder,
                actor_user_id="u",
                auth_principal="u",
                action="created",
                base_url="x",
                model="m",
                transport="ws",
                scope="global",
            )
        assert _captured_events(fake_recorder) == []

    @pytest.mark.asyncio
    async def test_discarded_undecryptable_action_allowed(self, fake_recorder):
        await record_llm_config_change(
            fake_recorder,
            actor_user_id="u1",
            auth_principal="system",
            action="discarded_undecryptable",
            base_url=None,
            model=None,
            transport="ws",
            scope="user",
        )
        ev = _captured_events(fake_recorder)[0]
        assert ev.action_type == "llm_config.discarded_undecryptable"
        assert ev.inputs_meta["action"] == "discarded_undecryptable"
        assert ev.inputs_meta["scope"] == "user"
        assert "could not be decrypted" in ev.description
        assert "discarded" in ev.description
        assert "base_url" not in ev.inputs_meta


class TestRecordLLMUnconfigured:
    @pytest.mark.asyncio
    async def test_emits_failure_event(self, fake_recorder):
        await record_llm_unconfigured(
            fake_recorder,
            actor_user_id="u1",
            auth_principal="u1",
            feature="tool_dispatch",
        )
        ev = _captured_events(fake_recorder)[0]
        assert ev.event_class == "llm_unconfigured"
        assert ev.outcome == "failure"
        assert ev.inputs_meta == {
            "feature": "tool_dispatch",
            "reason": "no_user_config_no_env_default",
        }


class TestRecordLLMCall:
    @pytest.mark.asyncio
    async def test_user_credential_source_success(self, fake_recorder):
        await record_llm_call(
            fake_recorder,
            actor_user_id="u1",
            auth_principal="u1",
            feature="tool_dispatch",
            credential_source=CredentialSource.USER,
            resolved=ResolvedConfig(base_url="https://x.example/v1", model="m"),
            total_tokens=247,
            outcome="success",
        )
        ev = _captured_events(fake_recorder)[0]
        assert ev.event_class == "llm_call"
        assert ev.inputs_meta["credential_source"] == "user"
        assert ev.inputs_meta["base_url"] == "https://x.example/v1"
        assert ev.outputs_meta == {"total_tokens": 247}
        assert ev.outcome == "success"

    @pytest.mark.asyncio
    async def test_system_source_failure_records_error_class(self, fake_recorder):
        await record_llm_call(
            fake_recorder,
            actor_user_id="system",
            auth_principal="system",
            feature="tool_summary",
            credential_source=CredentialSource.SYSTEM,
            resolved=ResolvedConfig(base_url="https://x", model="m"),
            total_tokens=None,
            outcome="failure",
            upstream_error_class="rate_limit",
        )
        ev = _captured_events(fake_recorder)[0]
        assert ev.outcome == "failure"
        assert ev.inputs_meta["credential_source"] == "system"
        assert ev.outputs_meta == {"upstream_error_class": "rate_limit"}
        assert "total_tokens" not in ev.outputs_meta

    @pytest.mark.asyncio
    async def test_system_source_description_label(self, fake_recorder):
        await record_llm_call(
            fake_recorder,
            actor_user_id="system",
            auth_principal="system",
            feature="knowledge_synthesis",
            credential_source=CredentialSource.SYSTEM,
            resolved=ResolvedConfig(base_url="https://x", model="m"),
            total_tokens=42,
            outcome="success",
        )
        ev = _captured_events(fake_recorder)[0]
        assert "system credential" in ev.description
        assert "operator default" not in ev.description

    @pytest.mark.asyncio
    async def test_unknown_outcome_raises(self, fake_recorder):
        with pytest.raises(ValueError):
            await record_llm_call(
                fake_recorder,
                actor_user_id="u",
                auth_principal="u",
                feature="x",
                credential_source=CredentialSource.USER,
                resolved=ResolvedConfig(base_url="x", model="m"),
                total_tokens=None,
                outcome="partial",
            )
