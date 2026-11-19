"""Summarizes older chat turns to fit the model's context window while keeping
tool-call/tool-result pairs intact; called by orchestrator.py's ReAct loop before
each LLM call.
"""

import logging
from typing import List, Dict

from orchestrator.context_token_accounting import (
    FALLBACK_CONTEXT_WINDOW,
    IMAGE_TOKEN_ALLOWANCE,
    TokenAccountant,
    build_model_profile,
    fallback_profile,
    get_context_window as _qualified_context_window,
)

logger = logging.getLogger("Orchestrator.Compaction")

CHARS_PER_TOKEN = 4

SUMMARY_PREFIX = "[Summary of prior conversation]"

_DEFAULT_CONTEXT_WINDOW = FALLBACK_CONTEXT_WINDOW
_IMAGE_PART_CHARS = IMAGE_TOKEN_ALLOWANCE * CHARS_PER_TOKEN


def _accountant(
    model_name: str | None,
    provider: str | None,
    *,
    context_window: int | None = None,
    encoding_name: str | None = None,
    reserved_output_tokens: int | None = None,
    assets_dir: str | None = None,
) -> TokenAccountant:
    profile = build_model_profile(
        provider,
        model_name,
        context_window=context_window,
        encoding=encoding_name,
        reserved_output_tokens=reserved_output_tokens,
    ) or fallback_profile(provider, model_name)
    return TokenAccountant(profile, assets_dir=assets_dir)


def estimate_tokens(
    messages: List[Dict],
    model_name: str | None = None,
    provider: str | None = None,
    *,
    assets_dir: str | None = None,
) -> int:
    return _accountant(model_name, provider, assets_dir=assets_dir).count_messages(messages)


def estimate_overhead_tokens(
    tools_desc: List[Dict] | None,
    model_name: str | None = None,
    provider: str | None = None,
    *,
    assets_dir: str | None = None,
) -> int:
    return _accountant(model_name, provider, assets_dir=assets_dir).count_tools(tools_desc)


def estimate_request_tokens(
    messages: List[Dict],
    tools_desc: List[Dict] | None = None,
    model_name: str | None = None,
    provider: str | None = None,
    *,
    context_window: int | None = None,
    encoding_name: str | None = None,
    reserved_output_tokens: int | None = None,
    assets_dir: str | None = None,
) -> int:
    accountant = _accountant(
        model_name,
        provider,
        context_window=context_window,
        encoding_name=encoding_name,
        reserved_output_tokens=reserved_output_tokens,
        assets_dir=assets_dir,
    )
    return accountant.account_request(
        messages, tools_desc, reserved_output_tokens=reserved_output_tokens
    ).total_tokens


def get_context_window(
    model_name: str | None,
    provider: str | None = None,
    *,
    context_window: int | None = None,
) -> int:
    return _qualified_context_window(model_name, provider, context_window=context_window)
