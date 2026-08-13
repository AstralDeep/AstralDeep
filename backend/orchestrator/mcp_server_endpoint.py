"""Flag-gated MCP 2026-07-28 Streamable HTTP resource server."""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from orchestrator.mcp_authz import (
    MCPAuthError,
    authorize_mcp_request,
    canonical_public_base_url,
    challenge_header,
    protected_resource_metadata,
)
from orchestrator.mcp_projection import project_tools, resolve_projected_tool
from orchestrator.work_admission import AdmissionClass, OperationState
from shared.protocol import (
    MCP_HEADER_MISMATCH,
    MCP_INVALID_PARAMS,
    MCP_INVALID_REQUEST,
    MCP_MISSING_REQUIRED_CLIENT_CAPABILITY,
    MCP_PROTOCOL_VERSION,
    MCP_SUPPORTED_PROTOCOL_VERSIONS,
    MCP_UNSUPPORTED_PROTOCOL_VERSION,
)
from webrender.targets.mcp_renderer import install as install_mcp_renderer
from webrender.targets.mcp_renderer import render_mcp


logger = logging.getLogger("MCPServer")

MCP_BODY_LIMIT = 1024 * 1024
MCP_DISCOVERY_TTL_MS = 60_000
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603
_SAFE_PLAIN_NAME = re.compile(r"^[\x21-\x7e]+$")
_BASE64_SENTINEL = re.compile(r"^=\?base64\?([A-Za-z0-9+/]*={0,2})\?=$")
_METHOD_PHASE = {
    "server/discover": "discover",
    "tools/list": "tools_list",
    "tools/call": "tools_call",
    "subscriptions/listen": "subscriptions_listen",
}
_METHOD_SCOPE = {
    "server/discover": ("mcp:discover",),
    "tools/list": ("mcp:tools:read",),
    "tools/call": ("mcp:tools:invoke",),
    "subscriptions/listen": ("mcp:discover",),
}


def _error_body(
    request_id: Any,
    code: int,
    message: str,
    *,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _headers(*, cache_control: str = "no-store") -> dict[str, str]:
    return {
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        "Cache-Control": cache_control,
        "Vary": "Authorization, Origin",
    }


def _json_error(
    request_id: Any,
    code: int,
    message: str,
    status_code: int,
    *,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    response_headers = _headers()
    response_headers.update(headers or {})
    return JSONResponse(
        _error_body(request_id, code, message, data=data),
        status_code=status_code,
        headers=response_headers,
    )


def _decode_name_header(value: str) -> str:
    sentinel = _BASE64_SENTINEL.fullmatch(value)
    if sentinel:
        try:
            raw = base64.b64decode(sentinel.group(1), validate=True)
            decoded = raw.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ValueError("Mcp-Name Base64 sentinel is malformed") from exc
        if not decoded:
            raise ValueError("Mcp-Name must not be empty")
        return decoded
    if not _SAFE_PLAIN_NAME.fullmatch(value) or value != value.strip():
        raise ValueError("unsafe Mcp-Name must use the Base64 sentinel")
    if value.startswith("=?base64?") or value.endswith("?="):
        raise ValueError("sentinel-looking Mcp-Name must be encoded")
    return value


async def _read_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MCP_BODY_LIMIT:
                raise OverflowError
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MCP_BODY_LIMIT:
            raise OverflowError
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_request(body: bytes) -> tuple[dict[str, Any], Any, bool]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request body must be one UTF-8 JSON object") from exc
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        raise ValueError("request must be a JSON-RPC 2.0 object")
    if "result" in payload or "error" in payload or not isinstance(payload.get("method"), str):
        raise ValueError("client responses and malformed methods are not accepted")
    request_id = payload.get("id")
    if "id" in payload and (
        isinstance(request_id, bool)
        or not isinstance(request_id, (str, int, type(None)))
    ):
        raise ValueError("JSON-RPC id must be a string, integer, or null")
    params = payload.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    return payload, request_id, "id" not in payload


def _validate_headers_and_metadata(request: Request, payload: dict[str, Any]) -> None:
    method = payload["method"]
    params = payload.get("params", {})
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        raise _RequestError(MCP_INVALID_PARAMS, "params._meta is required")
    body_version = meta.get("io.modelcontextprotocol/protocolVersion")
    capabilities = meta.get("io.modelcontextprotocol/clientCapabilities")
    if body_version is None or capabilities is None:
        raise _RequestError(
            MCP_INVALID_PARAMS,
            "protocolVersion and clientCapabilities metadata are required",
        )
    if not isinstance(capabilities, dict):
        raise _RequestError(MCP_INVALID_PARAMS, "clientCapabilities must be an object")
    if body_version not in MCP_SUPPORTED_PROTOCOL_VERSIONS:
        raise _RequestError(
            MCP_UNSUPPORTED_PROTOCOL_VERSION,
            "Unsupported MCP protocol version",
            data={"supported": list(MCP_SUPPORTED_PROTOCOL_VERSIONS)},
        )
    header_version = request.headers.get("mcp-protocol-version")
    header_method = request.headers.get("mcp-method")
    if not header_version or not header_method:
        raise _RequestError(MCP_HEADER_MISMATCH, "required MCP mirror header is missing")
    if header_version != body_version or header_method != method:
        raise _RequestError(MCP_HEADER_MISMATCH, "MCP mirror header does not match body")

    required_caps = params.get("requiredCapabilities", [])
    if required_caps is not None:
        if not isinstance(required_caps, list) or not all(
            isinstance(value, str) for value in required_caps
        ):
            raise _RequestError(MCP_INVALID_PARAMS, "requiredCapabilities must be a string array")
        missing = [value for value in required_caps if value not in capabilities]
        if missing:
            raise _RequestError(
                MCP_MISSING_REQUIRED_CLIENT_CAPABILITY,
                "Required client capability was not declared",
                data={"missing": missing},
            )

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not name:
            raise _RequestError(MCP_INVALID_PARAMS, "tools/call requires a name")
        if not isinstance(arguments, dict):
            raise _RequestError(MCP_INVALID_PARAMS, "tools/call arguments must be an object")
        name_header = request.headers.get("mcp-name")
        if not name_header:
            raise _RequestError(MCP_HEADER_MISMATCH, "Mcp-Name is required for tools/call")
        try:
            decoded_name = _decode_name_header(name_header)
        except ValueError as exc:
            raise _RequestError(MCP_HEADER_MISMATCH, str(exc)) from exc
        if decoded_name != name:
            raise _RequestError(MCP_HEADER_MISMATCH, "Mcp-Name does not match params.name")


class _RequestError(Exception):
    def __init__(self, code: int, message: str, *, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.data = data


def _server_info() -> dict[str, str]:
    return {"name": "AstralDeep", "version": "1.0.0"}


def _discover_result() -> dict[str, Any]:
    return {
        "resultType": "complete",
        "supportedVersions": list(MCP_SUPPORTED_PROTOCOL_VERSIONS),
        "capabilities": {"tools": {}},
        "instructions": (
            "Astral exposes the requesting user's authorized tools. "
            "Destructive unattended calls are refused."
        ),
        "ttlMs": MCP_DISCOVERY_TTL_MS,
        "cacheScope": "private",
        "_meta": {"io.modelcontextprotocol/serverInfo": _server_info()},
    }


def _structured(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {} if value is None else {"value": value}


def _tool_result(response: Any) -> dict[str, Any]:
    if response.result_type == "task":
        return {
            "resultType": "complete",
            "content": [{
                "type": "text",
                "text": "MCP Tasks are not supported by this server",
            }],
            "structuredContent": {},
            "isError": True,
            "_meta": {"io.modelcontextprotocol/serverInfo": _server_info()},
        }
    if response.error:
        message = str(response.error.get("message") or "Tool execution failed")
        return {
            "resultType": "complete",
            "content": [{"type": "text", "text": message[:65_536]}],
            "structuredContent": {},
            "isError": True,
            "_meta": {"io.modelcontextprotocol/serverInfo": _server_info()},
        }
    content = render_mcp(response.ui_components or [])
    structured = _structured(response.result)
    if not content:
        content = [{
            "type": "text",
            "text": json.dumps(structured, ensure_ascii=False, default=str)[:65_536],
        }]
    return {
        "resultType": response.result_type,
        "content": content,
        "structuredContent": structured,
        "isError": False,
        "_meta": {
            "io.modelcontextprotocol/serverInfo": _server_info(),
            "astral/correlationId": response.correlation_id,
        },
    }


async def _wait_for_disconnect(
    request: Request,
    operation: Awaitable[Any],
) -> Any:
    task = asyncio.create_task(operation)
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=0.1)
            if done:
                break
            if await request.is_disconnected():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise asyncio.CancelledError
        return await task
    except BaseException:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise


async def _renew_lease(orchestrator: Any, fence: Any, stop: asyncio.Event) -> None:
    interval = max(1.0, orchestrator.work_admission.slot_lease.total_seconds() / 3)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            await orchestrator._call_work_admission(
                orchestrator.work_admission.renew_execution_lease,
                fence,
            )


@asynccontextmanager
async def _admitted_request(
    orchestrator: Any,
    *,
    user_id: str,
    method: str,
) -> AsyncIterator[Any]:
    claimed = await orchestrator._claim_personal_agent_operation(
        owner_user_id=user_id,
        operation_kind="mcp_request",
        idempotency_namespace="mcp_request",
        idempotency_key=uuid.uuid4().hex,
        normalized_identity={"method": method, "generation": uuid.uuid4().hex},
        admission_class=AdmissionClass.MCP,
        request_generation=uuid.uuid4(),
        wait_seconds=5.0,
    )
    if claimed is None:
        raise _AdmissionRefused
    _owner, claim = claimed
    stop = asyncio.Event()
    renewer = asyncio.create_task(_renew_lease(orchestrator, claim.fence, stop))
    state = OperationState.COMPLETED
    terminal_code: str | None = None
    summary = "MCP request completed"
    try:
        yield claim
    except asyncio.CancelledError:
        state = OperationState.CANCELLED
        terminal_code = "client_disconnected"
        summary = "MCP request cancelled after disconnect"
        raise
    except BaseException:
        state = OperationState.FAILED
        terminal_code = "mcp_request_failed"
        summary = "MCP request failed"
        raise
    finally:
        stop.set()
        renewer.cancel()
        await asyncio.gather(renewer, return_exceptions=True)
        await orchestrator._call_work_admission(
            orchestrator.work_admission.terminalize,
            claim.fence,
            state=state,
            terminal_code=terminal_code,
            safe_summary=summary,
            retry_after_ms=None,
        )


class _AdmissionRefused(Exception):
    pass


def _record_metric(
    orchestrator: Any,
    event: str,
    *,
    phase: str,
    result_code: str,
    duration_seconds: float | None = None,
) -> None:
    observer = getattr(orchestrator, "runtime_observability", None)
    if observer is None:
        return
    observer.record_operation(
        event,
        operation_kind="mcp_request",
        phase=phase,
        result_code=result_code,
    )
    if duration_seconds is not None:
        observer.observe_operation_duration(
            duration_seconds,
            operation_kind="mcp_request",
            phase=phase,
            result_code=result_code,
        )


def _with_origin(response: Response, origin: str | None, allowed_origin: str) -> Response:
    if origin:
        response.headers["Access-Control-Allow-Origin"] = allowed_origin
    if "Access-Control-Allow-Credentials" in response.headers:
        del response.headers["Access-Control-Allow-Credentials"]
    return response


class MCPNoCredentialsCORSMiddleware:
    """Outer MCP-only CORS policy that cannot alter application CORS."""

    def __init__(self, app: Any, *, allowed_origin: str):
        self.app = app
        self.allowed_origin = allowed_origin

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if scope.get("type") != "http" or path not in {
            "/mcp",
            "/.well-known/oauth-protected-resource/mcp",
        }:
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        origin = headers.get("origin")
        if origin and origin != self.allowed_origin:
            response = _json_error(None, MCP_INVALID_REQUEST, "Origin is not allowed", 403)
            await response(scope, receive, send)
            return
        if scope.get("method") == "OPTIONS":
            response = Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": self.allowed_origin,
                    "Access-Control-Allow-Methods": "POST",
                    "Access-Control-Allow-Headers": (
                        "Authorization, Content-Type, MCP-Protocol-Version, "
                        "Mcp-Method, Mcp-Name"
                    ),
                    "Access-Control-Max-Age": "600",
                    "Vary": "Origin",
                },
            )
            await response(scope, receive, send)
            return

        async def send_without_credentials(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                filtered = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"access-control-allow-credentials"
                ]
                if origin:
                    filtered = [
                        (key, value)
                        for key, value in filtered
                        if key.lower() != b"access-control-allow-origin"
                    ]
                    filtered.append(
                        (b"access-control-allow-origin", self.allowed_origin.encode("latin-1"))
                    )
                message["headers"] = filtered
            await send(message)

        await self.app(scope, receive, send_without_credentials)


def create_mcp_router(orchestrator: Any, *, public_base_url: str) -> APIRouter:
    router = APIRouter()
    base_url = canonical_public_base_url(public_base_url)

    @router.get(
        "/.well-known/oauth-protected-resource/mcp",
        include_in_schema=False,
    )
    async def mcp_protected_resource_metadata(request: Request) -> Response:
        origin = request.headers.get("origin")
        response = JSONResponse(
            protected_resource_metadata(base_url),
            headers={"Cache-Control": "public, max-age=60", "Vary": "Origin"},
        )
        return _with_origin(response, origin, base_url)

    @router.post("/mcp", include_in_schema=False)
    async def mcp_endpoint(request: Request) -> Response:
        started = time.monotonic()
        request_id: Any = None
        method = "unknown"
        phase = "unknown"
        result_code = "invalid_request"
        notification = False
        origin = request.headers.get("origin")
        try:
            media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                result_code = "unsupported_media_type"
                return _with_origin(
                    _json_error(request_id, MCP_INVALID_REQUEST, "Content-Type must be application/json", 415),
                    origin,
                    base_url,
                )
            try:
                body = await _read_body(request)
            except OverflowError:
                result_code = "body_too_large"
                return _with_origin(
                    _json_error(request_id, MCP_INVALID_REQUEST, "MCP request body exceeds 1 MiB", 413),
                    origin,
                    base_url,
                )
            except ValueError:
                result_code = "invalid_content_length"
                return _with_origin(
                    _json_error(request_id, MCP_INVALID_REQUEST, "Invalid Content-Length", 400),
                    origin,
                    base_url,
                )
            try:
                payload, request_id, notification = _parse_request(body)
            except ValueError as exc:
                result_code = "invalid_request"
                return _with_origin(
                    _json_error(request_id, MCP_INVALID_REQUEST, str(exc), 400),
                    origin,
                    base_url,
                )
            method = payload["method"]
            phase = _METHOD_PHASE.get(method, "unknown")
            _record_metric(
                orchestrator,
                "requested",
                phase=phase,
                result_code="received",
            )
            try:
                _validate_headers_and_metadata(request, payload)
            except _RequestError as exc:
                result_code = "protocol_refused"
                return _with_origin(
                    _json_error(request_id, exc.code, str(exc), 400, data=exc.data),
                    origin,
                    base_url,
                )

            required_scopes = _METHOD_SCOPE.get(method, ("mcp:discover",))
            try:
                claims = await authorize_mcp_request(
                    headers=request.headers,
                    query_params=request.query_params,
                    cookies=request.cookies,
                    required_scopes=required_scopes,
                )
            except MCPAuthError as exc:
                result_code = exc.error
                challenge = challenge_header(
                    base_url,
                    error=exc.error,
                    required_scopes=exc.required_scopes,
                )
                return _with_origin(
                    _json_error(
                        request_id,
                        MCP_INVALID_REQUEST,
                        "MCP authorization failed",
                        exc.status_code,
                        headers={"WWW-Authenticate": challenge},
                    ),
                    origin,
                    base_url,
                )
            user_id = claims.get("sub")
            if not isinstance(user_id, str) or not user_id:
                result_code = "invalid_token"
                return _with_origin(
                    _json_error(request_id, MCP_INVALID_REQUEST, "MCP authorization failed", 401),
                    origin,
                    base_url,
                )
            from orchestrator.external_identity_links import (
                claims_with_saved_identities,
            )
            identity_db = getattr(
                getattr(orchestrator, "history", None), "db", None
            )
            if identity_db is not None and hasattr(
                identity_db, "get_user_preferences"
            ):
                try:
                    claims = await asyncio.to_thread(
                        claims_with_saved_identities,
                        identity_db,
                        user_id,
                        claims,
                    )
                except Exception:
                    # Identity-bound tools remain hidden from the unaugmented
                    # claims. Discovery and unrelated agents stay available.
                    logger.warning(
                        "external identity lookup failed user=%s", user_id
                    )
            if method not in _METHOD_PHASE:
                result_code = "method_not_found"
                return _with_origin(
                    _json_error(request_id, _METHOD_NOT_FOUND, "Method not found", 404),
                    origin,
                    base_url,
                )

            try:
                async with _admitted_request(
                    orchestrator,
                    user_id=user_id,
                    method=method,
                ):
                    params = payload.get("params", {})
                    if method == "server/discover":
                        result = _discover_result()
                        cache_control = "private, max-age=60"
                    elif method == "tools/list":
                        tools = await asyncio.to_thread(
                            project_tools, orchestrator, user_id, claims
                        )
                        result = {
                            "resultType": "complete",
                            "tools": [tool.descriptor for tool in tools],
                            "ttlMs": MCP_DISCOVERY_TTL_MS,
                            "cacheScope": "private",
                            "_meta": {"io.modelcontextprotocol/serverInfo": _server_info()},
                        }
                        cache_control = "private, max-age=60"
                    elif method == "subscriptions/listen":
                        result = {
                            "resultType": "complete",
                            "notificationTypes": [],
                            "_meta": {"io.modelcontextprotocol/serverInfo": _server_info()},
                        }
                        cache_control = "no-store"
                    else:
                        projected = await asyncio.to_thread(
                            resolve_projected_tool,
                            orchestrator,
                            user_id,
                            params["name"],
                            claims,
                        )
                        if projected is None:
                            result = {
                                "resultType": "complete",
                                "content": [{
                                    "type": "text",
                                    "text": "Tool is unavailable or no longer authorized",
                                }],
                                "structuredContent": {},
                                "isError": True,
                                "_meta": {"io.modelcontextprotocol/serverInfo": _server_info()},
                            }
                        else:
                            try:
                                tool_response = await _wait_for_disconnect(
                                    request,
                                    orchestrator.execute_mcp_tool(
                                        claims=claims,
                                        user_id=user_id,
                                        agent_id=projected.agent_id,
                                        tool_name=projected.skill_id,
                                        arguments=params.get("arguments", {}),
                                    ),
                                )
                                result = _tool_result(tool_response)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                # Once invocation starts, a tool/runtime failure is
                                # data in a completed tool result, not a protocol error.
                                result = {
                                    "resultType": "complete",
                                    "content": [{"type": "text", "text": "Tool execution failed"}],
                                    "structuredContent": {},
                                    "isError": True,
                                    "_meta": {"io.modelcontextprotocol/serverInfo": _server_info()},
                                }
                        cache_control = "no-store"
            except _AdmissionRefused:
                result_code = "capacity_refused"
                return _with_origin(
                    _json_error(
                        request_id,
                        _INTERNAL_ERROR,
                        "MCP request capacity is exhausted",
                        503,
                        headers={"Retry-After": "1"},
                    ),
                    origin,
                    base_url,
                )
            if notification:
                result_code = "accepted"
                return _with_origin(
                    Response(status_code=202, headers=_headers()),
                    origin,
                    base_url,
                )
            result_code = "completed"
            return _with_origin(
                JSONResponse(
                    {"jsonrpc": "2.0", "id": request_id, "result": result},
                    headers=_headers(cache_control=cache_control),
                ),
                origin,
                base_url,
            )
        except asyncio.CancelledError:
            result_code = "cancelled"
            raise
        except Exception:
            result_code = "internal_error"
            logger.error(
                "mcp request failed method=%s phase=%s code=%s",
                method if method in _METHOD_PHASE else "unknown",
                phase,
                result_code,
            )
            return _with_origin(
                _json_error(request_id, _INTERNAL_ERROR, "Internal MCP server error", 500),
                origin,
                base_url,
            )
        finally:
            duration = max(0.0, time.monotonic() - started)
            event = "completed" if result_code in {"completed", "accepted"} else "refused"
            _record_metric(
                orchestrator,
                event,
                phase=phase,
                result_code=result_code,
                duration_seconds=duration,
            )
            logger.info(
                "mcp request method=%s phase=%s outcome=%s code=%s",
                method if method in _METHOD_PHASE else "unknown",
                phase,
                event,
                result_code,
            )

    return router


def install_mcp_server(
    app: FastAPI,
    orchestrator: Any,
    *,
    public_base_url: str | None = None,
) -> None:
    """Install the complete MCP surface. Call only when the startup flag is on."""

    base_url = canonical_public_base_url(public_base_url)
    # Validate authorization metadata before mounting a partially usable route.
    protected_resource_metadata(base_url)
    install_mcp_renderer()
    app.include_router(create_mcp_router(orchestrator, public_base_url=base_url))
    app.add_middleware(MCPNoCredentialsCORSMiddleware, allowed_origin=base_url)


__all__ = [
    "MCP_BODY_LIMIT",
    "MCPNoCredentialsCORSMiddleware",
    "create_mcp_router",
    "install_mcp_server",
]
