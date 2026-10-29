"""Binds native Messages clients to encrypted configuration snapshots and approved async egress. The existing factory and orchestrator retain authorization, dispatch, and accounting ownership."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Awaitable, Callable

from astralplane.repositories.secrets import EncryptedLLMConfigRecord
from shared import external_http
from .native_wire import NativeContractError, build_request, decode_events, decode_response, encode
from .types import CredentialSource, LLMUnavailable

NATIVE_PROVIDER = "anthropic-native"
NATIVE_PROFILE = "anthropic-messages-text-tools/v1"
NATIVE_BASE_URL = "https://api.anthropic.com/v1"
NATIVE_API_VERSION = "2023-06-01"


def validate_native_selection(base_url, model, api_key):
    if (base_url != NATIVE_BASE_URL or type(model) is not str or not 1 <= len(model) <= 256
            or type(api_key) is not str or not 1 <= len(api_key) <= 8192
            or any(not 33 <= ord(char) <= 126 for char in api_key)):
        raise ValueError("native_configuration_invalid")


@dataclass(frozen=True, slots=True)
class NativeBinding:
    owner_id: str | None
    scope: str
    model: str
    base_url: str
    revision: str
    _key: str = field(repr=False)
    _validate: Callable[[], Awaitable[None]] = field(repr=False)

    async def validate(self):
        await self._validate()


def bind_record(store, record, *, scope, owner_id, api_key):
    if (type(record) is not EncryptedLLMConfigRecord or record.scope != scope or record.owner_id != owner_id or record.provider != NATIVE_PROVIDER
            or scope not in {"user", "system"} or (scope == "user" and (type(owner_id) is not str or not owner_id))
            or record.base_url != NATIVE_BASE_URL or type(api_key) is not str or not api_key):
        raise LLMUnavailable("native_configuration_binding_invalid")
    try:
        validate_native_selection(record.base_url, record.model, api_key)
    except ValueError:
        raise LLMUnavailable("native_configuration_binding_invalid") from None
    data = [record.scope, record.owner_id, record.provider, record.base_url, record.model,
            record.api_key_ciphertext, record.created_at.isoformat(), record.updated_at.isoformat(), record.updated_by]
    revision = hashlib.sha256(json.dumps(data, separators=(",", ":")).encode()).hexdigest()

    async def validate():
        def current():
            with store._repository.transaction() as transaction:
                sessions = store._repository.plane_runtime.repositories.history.sessions
                sessions.bound_request_execution_waits(transaction)
                repository = store._repository.repository
                return (repository.get_user(transaction, owner_id=owner_id) if scope == "user"
                        else repository.get_system(transaction))
        try:
            if await asyncio.to_thread(current) != record:
                raise ValueError
        except Exception:
            raise LLMUnavailable("native_configuration_changed") from None

    return NativeBinding(owner_id, scope, record.model, record.base_url, revision, api_key, validate)


class NativeMessagesClient:
    def __init__(self, binding: NativeBinding, source: CredentialSource, *, timeout=60.0):
        if (type(binding) is not NativeBinding or type(source) is not CredentialSource or source.value != binding.scope
                or binding.base_url != NATIVE_BASE_URL or type(binding._key) is not str
                or not 1 <= len(binding._key) <= 8192 or any(not 33 <= ord(char) <= 126 for char in binding._key)
                or type(timeout) not in (int, float) or not 0 < timeout <= 60):
            raise LLMUnavailable("native_configuration_binding_invalid")
        self.binding = binding
        self.timeout = float(timeout)
        self.chat = SimpleNamespace(completions=self)

    async def validate(self):
        if not native_profile_enabled():
            raise LLMUnavailable("native_profile_unavailable")
        await self.binding.validate()

    def create(self, **kwargs):
        if kwargs.get("stream"):
            raise NativeContractError("native_async_stream_required")
        raise NativeContractError("native_async_completion_required")

    def _request(self, kwargs):
        return build_request(bound_model=self.binding.model, **kwargs)

    async def _wire(self, payload):
        await self.validate()
        source = external_http.async_post(
            NATIVE_BASE_URL + "/messages", body=encode(payload), timeout_seconds=self.timeout,
            headers={"x-api-key": self.binding._key, "anthropic-version": NATIVE_API_VERSION,
                     "content-type": "application/json"},
        )
        try:
            async for part in source:
                yield part
        finally:
            await source.aclose()

    async def create_async(self, **kwargs):
        payload = self._request(kwargs)
        if payload["stream"]:
            raise NativeContractError("native_async_stream_required")
        async with asyncio.timeout(self.timeout):
            data = bytearray()
            async for part in self._wire(payload):
                data.extend(part)
            try:
                response = decode_response(bytes(data), payload)
            except NativeContractError as error:
                try:
                    await self.validate()
                except LLMUnavailable as changed:
                    changed.usage = getattr(error, "usage", None)
                    raise changed from None
                raise
            try:
                await self.validate()
            except LLMUnavailable as error:
                error.usage = response.usage
                raise
            return response

    async def stream_async(self, **kwargs):
        payload = self._request({**kwargs, "stream": True})
        async with asyncio.timeout(self.timeout):
            source = self._wire(payload)
            try:
                async for chunk in decode_events(source, payload):
                    try:
                        await self.validate()
                    except LLMUnavailable as error:
                        error.usage = chunk.usage
                        raise
                    yield chunk
            finally:
                await source.aclose()


def native_profile_enabled():
    from shared.feature_flags import flags
    return flags.is_enabled("anthropic_native")


def native_client(config, source, timeout):
    binding = getattr(config, "_native_binding", None)
    if (not native_profile_enabled() or type(binding) is not NativeBinding
            or config.provider != NATIVE_PROVIDER or config.base_url != binding.base_url
            or config.model != binding.model or config.api_key != binding._key):
        raise LLMUnavailable("native_profile_unavailable")
    return NativeMessagesClient(binding, source, timeout=timeout)
