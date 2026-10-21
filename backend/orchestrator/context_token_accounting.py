"""Accounts for complete model requests with explicit provider-bound context metadata,
hash-checked local encodings, and a conservative fallback for unknown routes. Unknown
routes count UTF-8 bytes against an 8,192-token ceiling; legacy compaction uses this seam.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import stat
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger("Orchestrator.ContextTokenAccounting")

FALLBACK_CONTEXT_WINDOW = 8_192
FALLBACK_RESERVED_OUTPUT_TOKENS = 1_024
MESSAGE_FRAMING_TOKENS = 4
CONTENT_PART_FRAMING_TOKENS = 4
TOOL_FRAMING_TOKENS = 8
IMAGE_TOKEN_ALLOWANCE = 1_500
AUDIO_TOKEN_ALLOWANCE = 2_000
FILE_TOKEN_ALLOWANCE = 4_000
_MAX_ASSET_BYTES = 16 * 1024 * 1024
_MAX_CONTEXT_WINDOW = 2_000_000

DEFAULT_ASSET_DIR = Path(__file__).with_name("token_assets")

_CL100K_PATTERN = r"""'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"""
_O200K_PATTERN = "|".join([
    r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
    r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
    r"""\p{N}{1,3}""",
    r""" ?[^\s\p{L}\p{N}]+[\r\n/]*""",
    r"""\s*[\r\n]+""",
    r"""\s+(?!\S)""",
    r"""\s+""",
])

_ENCODER_ASSETS: dict[str, tuple[str, str, str, dict[str, int]]] = {
    "cl100k_base": (
        "cl100k_base.tiktoken",
        "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
        _CL100K_PATTERN,
        {
            "<|endoftext|>": 100257,
            "<|fim_prefix|>": 100258,
            "<|fim_middle|>": 100259,
            "<|fim_suffix|>": 100260,
            "<|endofprompt|>": 100276,
        },
    ),
    "o200k_base": (
        "o200k_base.tiktoken",
        "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
        _O200K_PATTERN,
        {"<|endoftext|>": 199999, "<|endofprompt|>": 200018},
    ),
}

_MULTIMODAL_ALLOWANCES = {
    "image_url": IMAGE_TOKEN_ALLOWANCE,
    "input_image": IMAGE_TOKEN_ALLOWANCE,
    "image": IMAGE_TOKEN_ALLOWANCE,
    "input_audio": AUDIO_TOKEN_ALLOWANCE,
    "audio": AUDIO_TOKEN_ALLOWANCE,
    "file": FILE_TOKEN_ALLOWANCE,
    "input_file": FILE_TOKEN_ALLOWANCE,
}


class EncoderAssetError(ValueError):
    pass


class TokenAccountingError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ModelProfile:
    provider: str | None
    model: str
    context_window: int
    reserved_output_tokens: int
    encoding: str | None


@dataclass(frozen=True, slots=True)
class RequestTokenEstimate:
    message_tokens: int
    multimodal_tokens: int
    tool_tokens: int
    reserved_output_tokens: int
    total_tokens: int
    context_window: int
    encoding: str | None
    profile: ModelProfile

    @property
    def qualified_encoding(self) -> bool:
        return self.encoding is not None


_QUALIFIED_PROFILES: tuple[ModelProfile, ...] = (
    ModelProfile("openai", "gpt-4.1-nano", 1_047_576, 32_768, "o200k_base"),
    ModelProfile("openai", "gpt-4.1-mini", 1_047_576, 32_768, "o200k_base"),
    ModelProfile("openai", "gpt-4.1", 1_047_576, 32_768, "o200k_base"),
    ModelProfile("openai", "gpt-4o-mini", 128_000, 16_384, "o200k_base"),
    ModelProfile("openai", "gpt-4o", 128_000, 16_384, "o200k_base"),
    ModelProfile("openai", "gpt-4-turbo", 128_000, 4_096, "cl100k_base"),
    ModelProfile("openai", "gpt-4-32k", 32_768, 4_096, "cl100k_base"),
    ModelProfile("openai", "gpt-4", 8_192, 4_096, "cl100k_base"),
    ModelProfile("openai", "gpt-3.5-turbo", 16_385, 4_096, "cl100k_base"),
)


def _nonnegative_integer(value: Any, *, maximum: int | None = None) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
        and (maximum is None or value <= maximum)
    )


def _model_matches(profile_model: str, candidate: str) -> bool:
    return candidate == profile_model or candidate.startswith(profile_model + "-")


def resolve_model_profile(provider: str | None, model: str | None) -> ModelProfile | None:
    if not isinstance(model, str) or not model.strip():
        return None
    candidate = model.strip().lower()
    provider_key = provider.strip().lower() if isinstance(provider, str) and provider.strip() else None
    matches = [
        profile
        for profile in _QUALIFIED_PROFILES
        if (provider_key is None or profile.provider == provider_key)
        and _model_matches(profile.model, candidate)
    ]
    if not matches:
        return None
    return max(matches, key=lambda profile: len(profile.model))


def build_model_profile(
    provider: str | None,
    model: str | None,
    *,
    context_window: int | None = None,
    encoding: str | None = None,
    reserved_output_tokens: int | None = None,
) -> ModelProfile | None:
    base = resolve_model_profile(provider, model)
    configured = (
        context_window is not None
        or encoding is not None
        or reserved_output_tokens is not None
    )
    if not configured:
        return base
    window = context_window if context_window is not None else (
        base.context_window if base is not None else FALLBACK_CONTEXT_WINDOW
    )
    output = reserved_output_tokens if reserved_output_tokens is not None else (
        base.reserved_output_tokens if base is not None else min(
            FALLBACK_RESERVED_OUTPUT_TOKENS, max(1, window // 8)
        )
    )
    selected_encoding = encoding if encoding is not None else (base.encoding if base is not None else None)
    if not _nonnegative_integer(window, maximum=_MAX_CONTEXT_WINDOW) or window == 0:
        raise TokenAccountingError("context_window_invalid")
    if not _nonnegative_integer(output) or output >= window:
        raise TokenAccountingError("reserved_output_invalid")
    if selected_encoding is not None and (
        not isinstance(selected_encoding, str) or not selected_encoding.strip()
    ):
        raise TokenAccountingError("encoding_invalid")
    return ModelProfile(
        provider if isinstance(provider, str) and provider.strip() else (base.provider if base else None),
        model.strip() if isinstance(model, str) and model.strip() else (base.model if base else ""),
        window,
        output,
        selected_encoding.strip() if isinstance(selected_encoding, str) else None,
    )


def fallback_profile(provider: str | None = None, model: str | None = None) -> ModelProfile:
    return ModelProfile(
        provider if isinstance(provider, str) and provider.strip() else None,
        model.strip() if isinstance(model, str) and model.strip() else "",
        FALLBACK_CONTEXT_WINDOW,
        FALLBACK_RESERVED_OUTPUT_TOKENS,
        None,
    )


def get_context_window(
    model: str | None,
    provider: str | None = None,
    *,
    context_window: int | None = None,
) -> int:
    profile = build_model_profile(
        provider, model, context_window=context_window
    ) or fallback_profile(provider, model)
    return profile.context_window


def _read_asset(name: str, assets_dir: str | os.PathLike[str] | None) -> tuple[bytes, str]:
    if name not in _ENCODER_ASSETS:
        raise EncoderAssetError("encoding_unknown")
    filename, expected, _pattern, _special_tokens = _ENCODER_ASSETS[name]
    directory = Path(assets_dir) if assets_dir is not None else DEFAULT_ASSET_DIR
    path = directory / filename
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_ASSET_BYTES:
                raise ValueError
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                data = source.read(_MAX_ASSET_BYTES + 1)
        finally:
            os.close(descriptor)
    except (OSError, TypeError, ValueError):
        raise EncoderAssetError("asset_unavailable") from None
    if len(data) > _MAX_ASSET_BYTES:
        raise EncoderAssetError("asset_too_large")
    if hashlib.sha256(data).hexdigest() != expected:
        raise EncoderAssetError("asset_digest_mismatch")
    return data, expected


def _parse_ranks(data: bytes) -> dict[bytes, int]:
    ranks: dict[bytes, int] = {}
    try:
        for line in data.splitlines():
            if not line:
                continue
            token, rank = line.split()
            decoded = base64.b64decode(token, validate=True)
            value = int(rank)
            if not decoded or value < 0 or decoded in ranks:
                raise ValueError
            ranks[decoded] = value
    except (TypeError, ValueError, binascii.Error):
        raise EncoderAssetError("asset_malformed") from None
    if not ranks:
        raise EncoderAssetError("asset_malformed")
    return ranks


@lru_cache(maxsize=8)
def _load_encoder_cached(name: str, assets_dir: str) -> Any:
    data, _digest = _read_asset(name, assets_dir)
    _filename, _expected, pattern, special_tokens = _ENCODER_ASSETS[name]
    try:
        from tiktoken import Encoding
    except ImportError:
        raise EncoderAssetError("encoder_unavailable") from None
    try:
        return Encoding(
            name=name,
            pat_str=pattern,
            mergeable_ranks=_parse_ranks(data),
            special_tokens=special_tokens,
        )
    except EncoderAssetError:
        raise
    except Exception:
        raise EncoderAssetError("encoder_initialization_failed") from None


def load_encoder_asset(name: str, assets_dir: str | os.PathLike[str] | None = None) -> Any:
    directory = str(Path(assets_dir) if assets_dir is not None else DEFAULT_ASSET_DIR)
    return _load_encoder_cached(name, directory)


def verify_encoder_assets(assets_dir: str | os.PathLike[str] | None = None) -> dict[str, str]:
    verified = {}
    for name in _ENCODER_ASSETS:
        _data, digest = _read_asset(name, assets_dir)
        load_encoder_asset(name, assets_dir)
        verified[name] = digest
    return verified


def _json_text(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False, default=str,
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        try:
            return repr(value)
        except Exception:
            return f"<unrepresentable {type(value).__name__}>"


def _mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            value = dump(mode="json")
        except Exception:
            return None
        if isinstance(value, dict):
            return value
    return None


class TokenAccountant:
    def __init__(
        self,
        profile: ModelProfile | None = None,
        *,
        assets_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.profile = profile or fallback_profile()
        self._encoder = None
        self._fallback_reason = "profile_unqualified"
        if self.profile.encoding is not None:
            try:
                self._encoder = load_encoder_asset(self.profile.encoding, assets_dir)
                self._fallback_reason = ""
            except EncoderAssetError as exc:
                self._fallback_reason = str(exc)
                logger.warning(
                    "Token encoder %s unavailable (%s); using conservative fallback",
                    self.profile.encoding,
                    self._fallback_reason,
                )

    @property
    def qualified_encoding(self) -> bool:
        return self._encoder is not None

    @property
    def fallback_reason(self) -> str:
        return self._fallback_reason

    def _fallback_text_tokens(self, text: str) -> int:
        return len(text.encode("utf-8", errors="surrogatepass"))

    def count_text(self, text: str) -> int:
        if not isinstance(text, str):
            text = _json_text(text)
        if self._encoder is not None:
            try:
                return len(self._encoder.encode_ordinary(text))
            except Exception:
                logger.debug("Qualified token encoding failed; using fallback", exc_info=True)
        return self._fallback_text_tokens(text)

    def _content_tokens(self, content: Any) -> tuple[int, int]:
        if content is None:
            return 0, 0
        if isinstance(content, str):
            return self.count_text(content), 0
        if isinstance(content, (list, tuple)):
            text_tokens = 0
            multimodal_tokens = 0
            for part in content:
                value = _mapping(part)
                kind = value.get("type") if value is not None else None
                allowance = _MULTIMODAL_ALLOWANCES.get(kind) if isinstance(kind, str) else None
                if allowance is not None:
                    multimodal_tokens += allowance + CONTENT_PART_FRAMING_TOKENS
                    part_text = value.get("text")
                    if isinstance(part_text, str):
                        text_tokens += self.count_text(part_text)
                else:
                    text_tokens += self.count_text(_json_text(part)) + CONTENT_PART_FRAMING_TOKENS
            return text_tokens, multimodal_tokens
        return self.count_text(_json_text(content)), 0

    def _message_tokens(self, message: Any) -> tuple[int, int]:
        value = _mapping(message)
        if value is None:
            return self.count_text(_json_text(message)) + MESSAGE_FRAMING_TOKENS, 0
        fields = {key: item for key, item in value.items() if key != "content"}
        text_tokens = self.count_text(_json_text(fields)) + MESSAGE_FRAMING_TOKENS
        content_tokens, multimodal_tokens = self._content_tokens(value.get("content"))
        return text_tokens + content_tokens, multimodal_tokens

    def _messages_breakdown(self, messages: Any) -> tuple[int, int]:
        if messages is None:
            return 0, 0
        if not isinstance(messages, (list, tuple)):
            raise TokenAccountingError("messages_not_sequence")
        text_tokens = 0
        multimodal_tokens = 0
        for message in messages:
            message_text, message_multimodal = self._message_tokens(message)
            text_tokens += message_text
            multimodal_tokens += message_multimodal
        return text_tokens, multimodal_tokens

    def count_messages(self, messages: Any) -> int:
        text_tokens, multimodal_tokens = self._messages_breakdown(messages)
        return text_tokens + multimodal_tokens

    def count_tools(self, tools: Any) -> int:
        if tools is None or tools == []:
            return 0
        count = self.count_text(_json_text(tools)) + TOOL_FRAMING_TOKENS
        if isinstance(tools, (list, tuple)):
            count += max(0, len(tools) - 1) * TOOL_FRAMING_TOKENS
        return count

    def account_request(
        self,
        messages: Any,
        tools: Any = None,
        *,
        reserved_output_tokens: int | None = None,
    ) -> RequestTokenEstimate:
        output = (
            self.profile.reserved_output_tokens
            if reserved_output_tokens is None
            else reserved_output_tokens
        )
        if not _nonnegative_integer(output):
            raise TokenAccountingError("reserved_output_invalid")
        message_tokens, multimodal_tokens = self._messages_breakdown(messages)
        tool_tokens = self.count_tools(tools)
        total = message_tokens + multimodal_tokens + tool_tokens + output
        return RequestTokenEstimate(
            message_tokens=message_tokens,
            multimodal_tokens=multimodal_tokens,
            tool_tokens=tool_tokens,
            reserved_output_tokens=output,
            total_tokens=total,
            context_window=self.profile.context_window,
            encoding=self.profile.encoding if self.qualified_encoding else None,
            profile=self.profile,
        )


def account_request_tokens(
    messages: Any,
    tools: Any = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    context_window: int | None = None,
    encoding: str | None = None,
    reserved_output_tokens: int | None = None,
    assets_dir: str | os.PathLike[str] | None = None,
) -> RequestTokenEstimate:
    profile = build_model_profile(
        provider,
        model,
        context_window=context_window,
        encoding=encoding,
        reserved_output_tokens=reserved_output_tokens,
    ) or fallback_profile(provider, model)
    return TokenAccountant(profile, assets_dir=assets_dir).account_request(
        messages, tools, reserved_output_tokens=reserved_output_tokens
    )


__all__ = [
    "AUDIO_TOKEN_ALLOWANCE",
    "CONTENT_PART_FRAMING_TOKENS",
    "DEFAULT_ASSET_DIR",
    "EncoderAssetError",
    "FALLBACK_CONTEXT_WINDOW",
    "FALLBACK_RESERVED_OUTPUT_TOKENS",
    "FILE_TOKEN_ALLOWANCE",
    "IMAGE_TOKEN_ALLOWANCE",
    "MESSAGE_FRAMING_TOKENS",
    "ModelProfile",
    "RequestTokenEstimate",
    "TOOL_FRAMING_TOKENS",
    "TokenAccountingError",
    "TokenAccountant",
    "account_request_tokens",
    "build_model_profile",
    "fallback_profile",
    "get_context_window",
    "load_encoder_asset",
    "resolve_model_profile",
    "verify_encoder_assets",
]
