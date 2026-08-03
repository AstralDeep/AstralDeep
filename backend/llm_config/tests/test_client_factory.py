"""Feature 054 — build_llm_client factory unit tests.

New signature: ``build_llm_client(config, source)`` — the caller
(``Orchestrator._resolve_llm_client_for``) picks the record and source;
the factory only materializes the client. The feature-006 two-tier
user→operator-default rule is gone:

* config present  ⇒ client bound to that record's base_url/api_key.
* config None     ⇒ LLMUnavailable (first-run gate / honest system skip).
* OPERATOR_DEFAULT source ⇒ ValueError (retired; no new call may carry it).
* keyless config  ⇒ api_key placeholder "not-needed".
"""
from __future__ import annotations

import pytest

from llm_config.client_factory import (
    DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS,
    build_llm_client,
)
from llm_config.types import CredentialSource, LLMUnavailable, ResolvedConfig
from llm_config.user_store import PersistedLLMConfig


def _cfg(api_key="sk-user-key-1234567890abcdef",
         base_url="https://user.example/v1",
         model="user-model") -> PersistedLLMConfig:
    return PersistedLLMConfig(
        provider="custom", base_url=base_url, model=model, api_key=api_key)


class TestConfigPresent:
    def test_user_source_builds_client_from_record(self):
        client, source, resolved = build_llm_client(_cfg(), CredentialSource.USER)
        assert source is CredentialSource.USER
        assert client.api_key == "sk-user-key-1234567890abcdef"
        assert str(client.base_url).rstrip("/") == "https://user.example/v1"
        assert isinstance(resolved, ResolvedConfig)
        assert resolved.base_url == "https://user.example/v1"
        assert resolved.model == "user-model"

    def test_system_source_builds_client_from_record(self):
        cfg = _cfg(api_key="sk-system-key-1234567890abcd",
                   base_url="https://system.example/v1", model="sys-model")
        client, source, resolved = build_llm_client(cfg, CredentialSource.SYSTEM)
        assert source is CredentialSource.SYSTEM
        assert client.api_key == "sk-system-key-1234567890abcd"
        assert str(client.base_url).rstrip("/") == "https://system.example/v1"
        assert resolved.model == "sys-model"

    def test_resolved_config_never_carries_the_key(self):
        _, _, resolved = build_llm_client(_cfg(), CredentialSource.USER)
        assert not hasattr(resolved, "api_key")
        assert "sk-user-key" not in repr(resolved)


class TestConfigAbsent:
    def test_none_with_user_source_raises_llmunavailable(self):
        with pytest.raises(LLMUnavailable, match="provider setup"):
            build_llm_client(None, CredentialSource.USER)

    def test_none_with_system_source_raises_llmunavailable(self):
        with pytest.raises(LLMUnavailable, match="system credential"):
            build_llm_client(None, CredentialSource.SYSTEM)

    def test_user_and_system_messages_differ(self):
        with pytest.raises(LLMUnavailable) as user_exc:
            build_llm_client(None, CredentialSource.USER)
        with pytest.raises(LLMUnavailable) as system_exc:
            build_llm_client(None, CredentialSource.SYSTEM)
        assert str(user_exc.value) != str(system_exc.value)


class TestOperatorDefaultRetired:
    def test_operator_default_raises_valueerror_even_with_config(self):
        with pytest.raises(ValueError, match="retired"):
            build_llm_client(_cfg(), CredentialSource.OPERATOR_DEFAULT)

    def test_operator_default_raises_valueerror_with_none_config(self):
        # The source check precedes the config check: no path may carry
        # the retired source, not even the unavailable one.
        with pytest.raises(ValueError, match="OPERATOR_DEFAULT"):
            build_llm_client(None, CredentialSource.OPERATOR_DEFAULT)


class TestKeylessConfig:
    def test_empty_key_becomes_not_needed_placeholder(self):
        cfg = _cfg(api_key="", base_url="http://localhost:11434/v1",
                   model="llama3")
        client, source, resolved = build_llm_client(cfg, CredentialSource.USER)
        assert client.api_key == "not-needed"
        assert str(client.base_url).rstrip("/") == "http://localhost:11434/v1"
        assert resolved.model == "llama3"


class TestFactoryIsPureAndUncached:
    """A clear (which re-gates the user) must be observed on the very
    next call — the factory never caches clients across calls."""

    def test_two_calls_yield_different_clients(self):
        c1, _, _ = build_llm_client(_cfg(), CredentialSource.USER)
        c2, _, _ = build_llm_client(
            _cfg(api_key="sk-other-key-1234567890abcd",
                 base_url="https://other.example/v1", model="other-model"),
            CredentialSource.USER)
        assert c1 is not c2

    def test_same_config_still_yields_fresh_client(self):
        cfg = _cfg()
        c1, _, _ = build_llm_client(cfg, CredentialSource.USER)
        c2, _, _ = build_llm_client(cfg, CredentialSource.USER)
        assert c1 is not c2


class TestTimeoutPassthrough:
    def test_default_timeout_is_bounded_and_sdk_retries_are_disabled(self):
        client, _, _ = build_llm_client(_cfg(), CredentialSource.USER)
        assert client.timeout == DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS == 60.0
        assert client.max_retries == 0

    def test_timeout_kwarg_is_threaded_through(self):
        client, _, _ = build_llm_client(_cfg(), CredentialSource.USER,
                                        timeout=5.0)
        assert client.timeout == 5.0
        assert client.max_retries == 0
