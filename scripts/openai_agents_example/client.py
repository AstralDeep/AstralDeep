"""Asynchronous Work client for external OpenAI Agents SDK consumers: it speaks the
Astral MCP 2026-07-28 envelope over POST /mcp with bearer credentials, bounded
polling with truthful terminal states, and sanitized logging. agent.py binds these
operations into OpenAI Agents SDK tools; qualify.py drives them against a real
deployment; the tests exercise every path over deterministic transports.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

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
_SAFE_TOOL_ERROR_CODES = frozenset({"assignment_sensitive_content_refused", "framework_scope_required",
    "work_not_found", "work_read_unavailable", "work_repository_unavailable"})


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
        self._base_url = base_url.rstrip("/")
        self._bearer = bearer
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

    async def _post(self, method: str, params: dict[str, Any] | None, *, tool_name: str | None = None) -> dict[str, Any]:
        if self._client is None:
            await self.start()
        self._request_id += 1
        envelope = request_envelope(method, params, request_id=self._request_id)
        headers = request_headers(bearer=self._bearer, method=method, tool_name=tool_name)
        self.requests.append({"method": method, "tool": tool_name, "at": self.clock()})
        started = self.clock()
        try:
            response = await self._client.post(_MCP_PATH, json=envelope, headers=headers)
        except httpx.TransportError as exc:
            log_event(self._logger, "transport_failed", method=method, tool=tool_name)
            raise MCPProtocolError("transport_unavailable", detail=type(exc).__name__) from None
        elapsed_ms = int((self.clock() - started) * 1000)
        if response.status_code in (401, 403):
            log_event(self._logger, "credential_refused", method=method, tool=tool_name,
                      status=response.status_code)
            raise CredentialRefused(
                "credential_refused", challenge=response.headers.get("WWW-Authenticate"),
                status=response.status_code,
            )
        if response.status_code != 200:
            log_event(self._logger, "http_refused", method=method, tool=tool_name,
                      status=response.status_code)
            raise MCPProtocolError("http_refused", status=response.status_code)
        try:
            payload = response.json()
        except ValueError:
            raise MCPProtocolError("invalid_response_body") from None
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            raise MCPProtocolError("invalid_response_envelope")
        if "error" in payload:
            error = payload["error"]
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            log_event(self._logger, "rpc_refused", method=method, tool=tool_name, code=code)
            raise MCPProtocolError("rpc_error", detail=str(message) if message else None)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise MCPProtocolError("invalid_response_result")
        log_event(self._logger, "request_completed", method=method, tool=tool_name,
                  elapsed_ms=elapsed_ms)
        return result

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
        if max_polls < 1 or interval_seconds < 0 or timeout_seconds <= 0:
            raise ValueError("poll bounds must be non-negative with at least one poll")
        deadline = self.clock() + timeout_seconds
        revision = after_revision
        polls = 0
        while True:
            events = await self.events(operation_id, after_revision=revision)
            polls += 1
            if events.get("resync_required") is True:
                revision = None
            else:
                revision = events.get("revision")
                if not isinstance(revision, int):
                    raise MCPProtocolError("invalid_poll_revision")
            operation = events.get("operation")
            if operation is not None:
                disposition = operation.get("disposition")
                if disposition not in DISPOSITIONS:
                    raise MCPProtocolError("disposition_out_of_contract")
                if disposition in TERMINAL_DISPOSITIONS:
                    log_event(self._logger, "operation_terminal", operation_id=operation_id,
                              disposition=disposition, revision=revision, count=polls)
                    return operation
            if polls >= max_polls or self.clock() >= deadline:
                log_event(self._logger, "operation_pending_timeout", operation_id=operation_id,
                          count=polls)
                raise OperationPendingTimeout(
                    "operation_pending_timeout",
                    detail=f"polls={polls} last_revision={revision}",
                )
            await sleep(interval_seconds)


__all__ = [
    "AstralWorkClient",
    "CredentialRefused",
    "MCPProtocolError",
    "OperationPendingTimeout",
    "ToolCallError",
]
