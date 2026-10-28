"""Asynchronous Work client for external OpenAI Agents SDK consumers: it speaks the
Astral MCP 2026-07-28 envelope over POST /mcp with bearer credentials, bounded
polling with truthful terminal states, and sanitized logging. agent.py binds these
operations into OpenAI Agents SDK tools; qualify.py drives them against a real
deployment; the tests exercise every path over deterministic transports.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

import httpx

from scripts.openai_agents_example.contract import (
    DISPOSITIONS,
    TERMINAL_DISPOSITIONS,
    request_envelope,
    request_headers,
)
from scripts.openai_agents_example.redaction import log_event

_LOGGER = logging.getLogger("astral_openai_agents_example")
_MCP_PATH = "/mcp"
MCP_RESPONSE_LIMIT = 4 * 1024 * 1024
_SAFE_TOOL_ERROR_CODES = frozenset({"assignment_sensitive_content_refused", "framework_scope_required",
    "work_not_found", "work_read_unavailable", "work_repository_unavailable"})


def canonical_base_url(value: str) -> str:
    try:
        if type(value) is not str or any(ord(char) <= 32 for char in value):
            raise ValueError
        url = urlsplit(value)
        if (url.scheme not in ("http", "https") or not url.hostname or "@" in url.netloc
                or "?" in value or "#" in value or url.path not in ("", "/")
                or url.port is not None and not 1 <= url.port <= 65535):
            raise ValueError
        return str(httpx.URL(value)).rstrip("/")
    except (ValueError, TypeError, httpx.InvalidURL):
        raise ValueError("invalid_base_url") from None


def validate_poll_bounds(max_polls: int, interval_seconds: float, timeout_seconds: float) -> None:
    if (type(max_polls) is not int or not 1 <= max_polls <= 1000
            or type(interval_seconds) not in (int, float) or not math.isfinite(interval_seconds)
            or interval_seconds < 0 or type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 1800):
        raise ValueError("invalid_poll_bounds")


class MCPProtocolError(Exception):
    def __init__(self, code: str, *, status: int | None = None, detail: str | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.detail = detail


class CredentialRefused(MCPProtocolError):
    def __init__(self, code: str, *, challenge: str | None = None, status: int | None = None):
        super().__init__(code, status=status)
        self.challenge = challenge


class ToolCallError(MCPProtocolError):
    pass


class OperationPendingTimeout(MCPProtocolError):
    pass


class AstralWorkClient:
    def __init__(
        self,
        *,
        base_url: str,
        bearer: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
        clock: Any = time.monotonic,
        logger: logging.Logger | None = None,
    ):
        self._base_url = canonical_base_url(base_url)
        self._bearer = bearer
        validate_poll_bounds(1, 0, timeout_seconds)
        self._request_timeout_seconds = float(timeout_seconds)
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport
        self.clock = clock
        self._logger = logger or _LOGGER
        self.requests: list[dict[str, Any]] = []
        self._client: httpx.AsyncClient | None = None
        self._request_id = 0

    async def __aenter__(self) -> AstralWorkClient:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport,
                follow_redirects=False, trust_env=False,
            )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _bounded_http(self, http_method: str, path: str, *, envelope=None, headers=None):
        await self.start()
        headers = {**(headers or {}), "Accept-Encoding": "identity"}
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                async with self._client.stream(http_method, path, json=envelope, headers=headers) as response:
                    status = response.status_code
                    challenge = response.headers.get("WWW-Authenticate")
                    body = bytearray()
                    if status == 200:
                        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                            raise MCPProtocolError("response_encoding_refused")
                        if response.is_stream_consumed:
                            if len(response.content) > MCP_RESPONSE_LIMIT:
                                raise MCPProtocolError("response_too_large")
                            body.extend(response.content)
                        else:
                            async for chunk in response.aiter_raw(chunk_size=65536):
                                if len(body) + len(chunk) > MCP_RESPONSE_LIMIT:
                                    raise MCPProtocolError("response_too_large")
                                body.extend(chunk)
        except httpx.TransportError as exc:
            log_event(self._logger, "transport_failed", method=http_method)
            raise MCPProtocolError("transport_unavailable", detail=type(exc).__name__) from None
        except TimeoutError:
            log_event(self._logger, "transport_failed", method=http_method)
            raise MCPProtocolError("transport_timeout") from None
        return status, challenge, body

    async def _post(self, method: str, params: dict[str, Any] | None, *, tool_name: str | None = None) -> dict[str, Any]:
        if self._client is None:
            await self.start()
        self._request_id += 1
        request_id = self._request_id
        envelope = request_envelope(method, params, request_id=request_id)
        headers = request_headers(bearer=self._bearer, method=method, tool_name=tool_name)
        headers["Accept-Encoding"] = "identity"
        self.requests.append({"method": method, "tool": tool_name, "at": self.clock()})
        started = self.clock()
        status, challenge, body = await self._bounded_http("POST", _MCP_PATH, envelope=envelope, headers=headers)
        elapsed_ms = int((self.clock() - started) * 1000)
        if status in (401, 403):
            log_event(self._logger, "credential_refused", method=method, tool=tool_name,
                      status=status)
            raise CredentialRefused(
                "credential_refused", challenge=challenge, status=status,
            )
        if status != 200:
            log_event(self._logger, "http_refused", method=method, tool=tool_name,
                      status=status)
            raise MCPProtocolError("http_refused", status=status)
        try:
            payload = json.loads(body)
        except ValueError:
            raise MCPProtocolError("invalid_response_body") from None
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            raise MCPProtocolError("invalid_response_envelope")
        if (type(payload.get("id")) is not int or payload["id"] != request_id
                or ("result" in payload) == ("error" in payload)):
            raise MCPProtocolError("invalid_response_envelope")
        if "error" in payload:
            error = payload["error"]
            if (type(error) is not dict or type(error.get("code")) is not int
                    or type(error.get("message")) is not str):
                raise MCPProtocolError("invalid_rpc_error")
            code = error["code"]
            log_event(self._logger, "rpc_refused", method=method, tool=tool_name, code=code)
            raise MCPProtocolError("rpc_error")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise MCPProtocolError("invalid_response_result")
        log_event(self._logger, "request_completed", method=method, tool=tool_name,
                  elapsed_ms=elapsed_ms)
        return result

    async def protected_resource_metadata(self) -> dict[str, Any]:
        self.requests.append({"method": "protected_resource_metadata", "tool": None, "at": self.clock()})
        status, _challenge, body = await self._bounded_http("GET", "/.well-known/oauth-protected-resource/mcp")
        if status != 200:
            raise MCPProtocolError("metadata_http_refused", status=status)
        try:
            document = json.loads(body)
        except ValueError:
            raise MCPProtocolError("invalid_metadata_body") from None
        if type(document) is not dict:
            raise MCPProtocolError("invalid_metadata_body")
        return document

    async def discover(self) -> dict[str, Any]:
        return await self._post("server/discover", None)

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._post("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise MCPProtocolError("invalid_tools_listing")
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._post("tools/call", {"name": name, "arguments": arguments},
                                  tool_name=name)
        if (result.get("resultType") != "complete" or type(result.get("isError")) is not bool
                or type(result.get("content")) is not list):
            raise MCPProtocolError("invalid_tool_result")
        if result.get("isError") is True:
            message = ""
            content = result.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                message = str(content[0].get("text", ""))
            code = message if message in _SAFE_TOOL_ERROR_CODES else "tool_call_refused"
            log_event(self._logger, "tool_refused", tool=name, detail_code=code)
            raise ToolCallError(message or "tool_call_refused")
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            raise MCPProtocolError("invalid_tool_result")
        return structured

    async def submit(
        self,
        *,
        idempotency_key: str,
        name: str,
        instructions: str,
        conversation_id: str | None = None,
        deadline_in_seconds: int | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "idempotency_key": idempotency_key, "name": name, "instructions": instructions,
        }
        if conversation_id is not None:
            arguments["conversation_id"] = conversation_id
        if deadline_in_seconds is not None:
            arguments["deadline_in_seconds"] = deadline_in_seconds
        return await self.call_tool("astral_submit_operation", arguments)

    async def get(self, operation_id: str) -> dict[str, Any]:
        return await self.call_tool("astral_get_operation", {"operation_id": operation_id})

    async def list(self, *, limit: int = 50, after_id: str | None = None) -> dict[str, Any]:
        arguments: dict[str, Any] = {"limit": limit}
        if after_id is not None:
            arguments["after_id"] = after_id
        return await self.call_tool("astral_list_operations", arguments)

    async def events(self, operation_id: str, after_revision: int | None = None) -> dict[str, Any]:
        arguments: dict[str, Any] = {"operation_id": operation_id}
        if after_revision is not None:
            arguments["after_revision"] = after_revision
        return await self.call_tool("astral_get_operation_events", arguments)

    async def artifact(self, operation_id: str) -> dict[str, Any]:
        return await self.call_tool("astral_get_artifact", {"operation_id": operation_id})

    async def cancel(self, operation_id: str, *, submission_id: str | None = None,
                     expected_revision: int | None = None) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "operation_id": operation_id,
            "submission_id": submission_id or str(uuid.uuid4()),
            "expected_revision": expected_revision,
        }
        return await self.call_tool("astral_cancel_operation", arguments)

    async def pause(self, operation_id: str, *, submission_id: str | None = None,
                    expected_revision: int | None = None) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "operation_id": operation_id,
            "submission_id": submission_id or str(uuid.uuid4()),
            "expected_revision": expected_revision,
        }
        return await self.call_tool("astral_pause_operation", arguments)

    async def wait_terminal(
        self,
        operation_id: str,
        *,
        after_revision: int | None = None,
        interval_seconds: float = 1.0,
        max_polls: int = 30,
        timeout_seconds: float = 120.0,
        sleep: Any = asyncio.sleep,
    ) -> dict[str, Any]:
        validate_poll_bounds(max_polls, interval_seconds, timeout_seconds)
        if after_revision is not None and (type(after_revision) is not int or after_revision < 0):
            raise ValueError("invalid_poll_revision")
        deadline = self.clock() + timeout_seconds
        revision = after_revision
        polls = 0
        try:
            async with asyncio.timeout(timeout_seconds):
                while polls < max_polls and self.clock() < deadline:
                    polls += 1
                    events = await self.events(operation_id, after_revision=revision)
                    if self.clock() >= deadline:
                        break
                    current = events.get("revision")
                    changed = events.get("changed")
                    resync = events.get("resync_required")
                    if (type(current) is not int or current < 1 or type(changed) is not bool
                            or type(resync) is not bool or changed != (current != revision)
                            or resync != (revision is not None and revision > current)):
                        raise MCPProtocolError("invalid_poll_event")
                    operation = events.get("operation")
                    if changed:
                        if (type(operation) is not dict or operation.get("id") != operation_id
                                or type(operation.get("revision")) is not int or operation["revision"] != current):
                            raise MCPProtocolError("invalid_poll_event")
                        disposition = operation.get("disposition")
                        if disposition not in DISPOSITIONS:
                            raise MCPProtocolError("disposition_out_of_contract")
                        if disposition in TERMINAL_DISPOSITIONS:
                            log_event(self._logger, "operation_terminal", operation_id=operation_id,
                                      disposition=disposition, revision=current, count=polls)
                            return operation
                    elif operation is not None:
                        raise MCPProtocolError("invalid_poll_event")
                    revision = None if resync else current
                    remaining = deadline - self.clock()
                    if polls >= max_polls or remaining <= 0:
                        break
                    await sleep(min(interval_seconds, remaining))
        except TimeoutError:
            pass
        log_event(self._logger, "operation_pending_timeout", operation_id=operation_id, count=polls)
        raise OperationPendingTimeout("operation_pending_timeout", detail=f"polls={polls} last_revision={revision}")


__all__ = [
    "AstralWorkClient",
    "CredentialRefused",
    "MCPProtocolError",
    "OperationPendingTimeout",
    "ToolCallError",
]
