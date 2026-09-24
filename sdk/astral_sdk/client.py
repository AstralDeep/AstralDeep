"""Sync and async clients speaking Deep's framework Work-over-MCP JSON-RPC contract over
POST {base_url}/mcp, the framework credential's only ingress; depends on
astral_sdk.models and astral_sdk.errors, only httpx at runtime.
"""

from __future__ import annotations

import re
import time
import uuid
from types import TracebackType
from typing import Any, Optional

import httpx

from astral_sdk.errors import (
    AstralAuthError,
    AstralConflictError,
    AstralHTTPError,
    AstralTimeoutError,
    RetryExhaustedError,
)
from astral_sdk.models import Artifact, ControlResult, Event, Operation, OperationList, RetryPolicy
from astral_sdk.tools import MCP_PROTOCOL_VERSION

_CLIENT_INFO = {"name": "astral-sdk-python", "version": "0.1.0"}
_META = {
    "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": _CLIENT_INFO,
}
_CONFLICT_CODES = frozenset({
    "assignment_revision_conflict", "assignment_idempotency_conflict",
    "credential_allowance_exhausted", "framework_credential_authority_unavailable",
})
_AUTH_CODES = frozenset({
    "invalid_token", "insufficient_scope", "framework_scope_required",
    "framework_credential_authority_required",
})


def _envelope(method: str, params: dict[str, Any]) -> dict[str, Any]:
    body_params = dict(params)
    body_params["_meta"] = dict(_META)
    return {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": body_params}


def _headers(token: str, method: str, tool_name: Optional[str] = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Mcp-Protocol-Version": MCP_PROTOCOL_VERSION,
        "Mcp-Method": method,
        "Accept": "application/json",
    }
    if method == "tools/call":
        # Server rejects tools/call without this header
        headers["Mcp-Name"] = tool_name or ""
    return headers


def _classify_error(message: str) -> type[AstralHTTPError]:
    if message in _AUTH_CODES:
        return AstralAuthError
    if message in _CONFLICT_CODES:
        return AstralConflictError
    return AstralHTTPError


_WWW_AUTHENTICATE_ERROR = re.compile(r'error="([^"]*)"')


def _challenge_error_code(www_authenticate: Optional[str]) -> Optional[str]:
    if not www_authenticate:
        return None
    match = _WWW_AUTHENTICATE_ERROR.search(www_authenticate)
    return match.group(1) if match else None


def _raise_for_response(status_code: int, body: dict[str, Any],
                        www_authenticate: Optional[str] = None) -> dict[str, Any]:
    if "error" in body:
        error = body["error"] or {}
        message = str(error.get("message") or "MCP request failed")
        challenge_code = _challenge_error_code(www_authenticate)
        code = challenge_code or message
        cls = AstralAuthError if status_code in (401, 403) else _classify_error(code)
        raise cls(message, status_code=status_code, code=code, data=error.get("data"))
    result = body.get("result")
    if not isinstance(result, dict):
        raise AstralHTTPError("malformed MCP response: missing result", status_code=status_code)
    return result


def _unwrap_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("isError"):
        content = result.get("content") or []
        text = content[0].get("text") if content and isinstance(content[0], dict) else "tool call failed"
        raise _classify_error(text)(text, code=text)
    structured = result.get("structuredContent")
    return structured if isinstance(structured, dict) else {}


class _Attempt(Exception):
    def __init__(self, real_error: Exception, *, status_code: Optional[int], is_network_error: bool) -> None:
        super().__init__(str(real_error))
        self.real_error = real_error
        self.status_code = status_code
        self.is_network_error = is_network_error


class _RetryLoop:
    def __init__(self, policy: RetryPolicy) -> None:
        self.policy = policy

    def attempts(self):
        attempt = 1
        while True:
            yield attempt
            attempt += 1

    def delay(self, attempt: int) -> float:
        return self.policy.delay_for(attempt)

    def outcome(self, attempt: int, failure: _Attempt):
        retryable = (
            failure.is_network_error
            or (failure.status_code is not None and failure.status_code in self.policy.retryable_status_codes)
        )
        if not retryable:
            return "raise", failure.real_error
        if attempt >= self.policy.max_attempts:
            return "exhausted", failure.real_error
        return "retry", None


class AstralClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0,
                 retry_policy: Optional[RetryPolicy] = None,
                 transport: Optional[httpx.BaseTransport] = None) -> None:
        if not token:
            raise ValueError("a framework credential token is required")
        self._token = token
        self._retry = _RetryLoop(retry_policy or RetryPolicy())
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "AstralClient":
        return self

    def __exit__(self, exc_type: Optional[type[BaseException]], exc: Optional[BaseException],
                 tb: Optional[TracebackType]) -> None:
        self.close()

    def _attempt_once(self, method: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        try:
            response = self._http.post("/mcp", json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise _Attempt(AstralTimeoutError(f"MCP request timed out: {method}"),
                          status_code=None, is_network_error=True) from exc
        except httpx.HTTPError as exc:
            raise _Attempt(AstralHTTPError(f"transport error calling {method}: {exc}"),
                          status_code=None, is_network_error=True) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise _Attempt(AstralHTTPError("malformed JSON from MCP endpoint", status_code=response.status_code),
                          status_code=response.status_code, is_network_error=False) from exc
        try:
            return _raise_for_response(response.status_code, payload, response.headers.get("www-authenticate"))
        except AstralHTTPError as exc:
            raise _Attempt(exc, status_code=response.status_code, is_network_error=False) from exc

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        body = _envelope(method, params)
        headers = _headers(self._token, method, params.get("name") if method == "tools/call" else None)
        for attempt in self._retry.attempts():
            try:
                return self._attempt_once(method, body, headers)
            except _Attempt as failure:
                verdict, error = self._retry.outcome(attempt, failure)
                if verdict == "raise":
                    raise error from failure.__cause__
                if verdict == "exhausted":
                    raise RetryExhaustedError(f"exhausted retries calling {method}",
                                              attempts=attempt, last_error=error) from failure.__cause__
                time.sleep(self._retry.delay(attempt))
        raise AssertionError("unreachable")  # pragma: no cover

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        return _unwrap_tool_result(result)

    def discover(self) -> dict[str, Any]:
        return self._request("server/discover", {})

    def list_tools(self) -> list[dict[str, Any]]:
        return self._request("tools/list", {}).get("tools", [])

    def submit_operation(self, *, idempotency_key: str, name: str, instructions: str,
                         conversation_id: Optional[str] = None,
                         deadline_in_seconds: Optional[int] = None) -> Operation:
        arguments: dict[str, Any] = {
            "idempotency_key": idempotency_key, "name": name, "instructions": instructions,
        }
        if conversation_id is not None:
            arguments["conversation_id"] = conversation_id
        if deadline_in_seconds is not None:
            arguments["deadline_in_seconds"] = deadline_in_seconds
        return Operation.from_dict(self._call_tool("astral_submit_operation", arguments))

    def get_operation(self, operation_id: str) -> Operation:
        return Operation.from_dict(self._call_tool("astral_get_operation", {"operation_id": operation_id}))

    def list_operations(self, *, limit: int = 50, after_id: Optional[str] = None) -> OperationList:
        arguments: dict[str, Any] = {"limit": limit}
        if after_id is not None:
            arguments["after_id"] = after_id
        return OperationList.from_dict(self._call_tool("astral_list_operations", arguments))

    def poll_operation(self, operation_id: str, *, after_revision: Optional[int] = None) -> Event:
        arguments: dict[str, Any] = {"operation_id": operation_id}
        if after_revision is not None:
            arguments["after_revision"] = after_revision
        return Event.from_dict(self._call_tool("astral_get_operation_events", arguments))

    def cancel_operation(self, operation_id: str, *, submission_id: Optional[str] = None,
                         expected_revision: int) -> ControlResult:
        arguments = {"operation_id": operation_id, "submission_id": submission_id or str(uuid.uuid4()),
                    "expected_revision": expected_revision}
        return ControlResult.from_dict(self._call_tool("astral_cancel_operation", arguments))

    def pause_operation(self, operation_id: str, *, submission_id: Optional[str] = None,
                        expected_revision: int) -> ControlResult:
        arguments = {"operation_id": operation_id, "submission_id": submission_id or str(uuid.uuid4()),
                    "expected_revision": expected_revision}
        return ControlResult.from_dict(self._call_tool("astral_pause_operation", arguments))

    def get_artifact(self, operation_id: str) -> Artifact:
        return Artifact.from_dict(self._call_tool("astral_get_artifact", {"operation_id": operation_id}))

    def wait_for_terminal(self, operation_id: str, *, poll_interval_seconds: float = 1.0,
                          timeout_seconds: Optional[float] = None) -> Operation:
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        current = self.get_operation(operation_id)
        while not current.is_terminal:
            if deadline is not None and time.monotonic() >= deadline:
                return current
            time.sleep(poll_interval_seconds)
            event = self.poll_operation(operation_id, after_revision=current.revision)
            current = event.operation if event.operation is not None else current
        return current


class AsyncAstralClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0,
                 retry_policy: Optional[RetryPolicy] = None,
                 transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        if not token:
            raise ValueError("a framework credential token is required")
        self._token = token
        self._retry = _RetryLoop(retry_policy or RetryPolicy())
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncAstralClient":
        return self

    async def __aexit__(self, exc_type: Optional[type[BaseException]], exc: Optional[BaseException],
                        tb: Optional[TracebackType]) -> None:
        await self.aclose()

    async def _attempt_once(self, method: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self._http.post("/mcp", json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise _Attempt(AstralTimeoutError(f"MCP request timed out: {method}"),
                          status_code=None, is_network_error=True) from exc
        except httpx.HTTPError as exc:
            raise _Attempt(AstralHTTPError(f"transport error calling {method}: {exc}"),
                          status_code=None, is_network_error=True) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise _Attempt(AstralHTTPError("malformed JSON from MCP endpoint", status_code=response.status_code),
                          status_code=response.status_code, is_network_error=False) from exc
        try:
            return _raise_for_response(response.status_code, payload, response.headers.get("www-authenticate"))
        except AstralHTTPError as exc:
            raise _Attempt(exc, status_code=response.status_code, is_network_error=False) from exc

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        import asyncio

        body = _envelope(method, params)
        headers = _headers(self._token, method, params.get("name") if method == "tools/call" else None)
        for attempt in self._retry.attempts():
            try:
                return await self._attempt_once(method, body, headers)
            except _Attempt as failure:
                verdict, error = self._retry.outcome(attempt, failure)
                if verdict == "raise":
                    raise error from failure.__cause__
                if verdict == "exhausted":
                    raise RetryExhaustedError(f"exhausted retries calling {method}",
                                              attempts=attempt, last_error=error) from failure.__cause__
                await asyncio.sleep(self._retry.delay(attempt))
        raise AssertionError("unreachable")  # pragma: no cover

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        return _unwrap_tool_result(result)

    async def discover(self) -> dict[str, Any]:
        return await self._request("server/discover", {})

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request("tools/list", {})
        return result.get("tools", [])

    async def submit_operation(self, *, idempotency_key: str, name: str, instructions: str,
                               conversation_id: Optional[str] = None,
                               deadline_in_seconds: Optional[int] = None) -> Operation:
        arguments: dict[str, Any] = {
            "idempotency_key": idempotency_key, "name": name, "instructions": instructions,
        }
        if conversation_id is not None:
            arguments["conversation_id"] = conversation_id
        if deadline_in_seconds is not None:
            arguments["deadline_in_seconds"] = deadline_in_seconds
        return Operation.from_dict(await self._call_tool("astral_submit_operation", arguments))

    async def get_operation(self, operation_id: str) -> Operation:
        result = await self._call_tool("astral_get_operation", {"operation_id": operation_id})
        return Operation.from_dict(result)

    async def list_operations(self, *, limit: int = 50, after_id: Optional[str] = None) -> OperationList:
        arguments: dict[str, Any] = {"limit": limit}
        if after_id is not None:
            arguments["after_id"] = after_id
        return OperationList.from_dict(await self._call_tool("astral_list_operations", arguments))

    async def poll_operation(self, operation_id: str, *, after_revision: Optional[int] = None) -> Event:
        arguments: dict[str, Any] = {"operation_id": operation_id}
        if after_revision is not None:
            arguments["after_revision"] = after_revision
        return Event.from_dict(await self._call_tool("astral_get_operation_events", arguments))

    async def cancel_operation(self, operation_id: str, *, submission_id: Optional[str] = None,
                               expected_revision: int) -> ControlResult:
        arguments = {"operation_id": operation_id, "submission_id": submission_id or str(uuid.uuid4()),
                    "expected_revision": expected_revision}
        return ControlResult.from_dict(await self._call_tool("astral_cancel_operation", arguments))

    async def pause_operation(self, operation_id: str, *, submission_id: Optional[str] = None,
                              expected_revision: int) -> ControlResult:
        arguments = {"operation_id": operation_id, "submission_id": submission_id or str(uuid.uuid4()),
                    "expected_revision": expected_revision}
        return ControlResult.from_dict(await self._call_tool("astral_pause_operation", arguments))

    async def get_artifact(self, operation_id: str) -> Artifact:
        result = await self._call_tool("astral_get_artifact", {"operation_id": operation_id})
        return Artifact.from_dict(result)

    async def wait_for_terminal(self, operation_id: str, *, poll_interval_seconds: float = 1.0,
                                timeout_seconds: Optional[float] = None) -> Operation:
        import asyncio

        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        current = await self.get_operation(operation_id)
        while not current.is_terminal:
            if deadline is not None and time.monotonic() >= deadline:
                return current
            await asyncio.sleep(poll_interval_seconds)
            event = await self.poll_operation(operation_id, after_revision=current.revision)
            current = event.operation if event.operation is not None else current
        return current


__all__ = ["AstralClient", "AsyncAstralClient"]
