"""Shared deterministic MCP server fakes for the example's offline suites: every handler
enforces the MCP 2026-07-28 metadata/header contract, models scope- and owner-aware tool
projection, and serves scripted lifecycle responses. No network and no third-party SDK
are involved; both scripts/tests/test_openai_agents_example.py and the isolated real-SDK
suite in this directory build on it.
"""

from __future__ import annotations

import json

import httpx

BEARER = "afk_fake.owner.credential-value"
OTHER_BEARER = "afk_fake.other.owner-value"
READONLY_BEARER = "afk_fake.readonly.value"
REVOKED_BEARER = "afk_fake.revoked.value"
EXPIRED_BEARER = "afk_fake.expired.value"
ADMISSION_BEARER = "afk_fake.admission.value"
OPERATION_ID = "0b98d4e5-6a11-4a53-9df1-3f5d0c93a001"
ARTIFACT_OPERATION_ID = "0b98d4e5-6a11-4a53-9df1-3f5d0c93a002"
METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
SENSITIVE_CODE = "assignment_sensitive_content_refused"
UNAUTHORIZED_CODE = "Tool is unavailable or not authorized"

FULL_TOOLS = [
    {"name": "astral_submit_operation"}, {"name": "astral_get_operation"},
    {"name": "astral_list_operations"}, {"name": "astral_get_operation_events"},
    {"name": "astral_get_artifact"}, {"name": "astral_cancel_operation"},
    {"name": "astral_pause_operation"},
    {"name": "astral_subscribe_operation_completion"},
]
READONLY_TOOLS = [tool for tool in FULL_TOOLS if tool["name"] != "astral_submit_operation"]


def operation(disposition="queued", revision=1):
    return {
        "id": OPERATION_ID, "revision": revision, "instruction_revision": 1,
        "control_epoch": 0, "title": "qualification", "kind": "chat",
        "disposition": disposition, "lifecycle": "running", "phase": None,
        "created_at": "2026-10-27T00:00:00+00:00", "updated_at": "2026-10-27T00:00:00+00:00",
        "next_wake_at": None, "deadline_at": "2026-10-27T01:00:00+00:00",
        "schema_supported": True, "safe_error_code": None, "usage": {},
    }


class FakeAstral:
    def __init__(self, *, behaviors=None, revoked_bearers=frozenset({REVOKED_BEARER, EXPIRED_BEARER}),
                 metadata=None, metadata_status=200, discovery=None, tools_by_bearer=None,
                 readonly_submit_error=UNAUTHORIZED_CODE, other_owner_get_succeeds=False,
                 revoked_challenge=True, revoked_status=401):
        self.behaviors = behaviors or {}
        self.revoked_bearers = set(revoked_bearers)
        self.metadata = metadata
        self.metadata_status = metadata_status
        self.discovery = discovery
        self.tools_by_bearer = tools_by_bearer or {}
        self.readonly_submit_error = readonly_submit_error
        self.other_owner_get_succeeds = other_owner_get_succeeds
        self.revoked_challenge = revoked_challenge
        self.revoked_status = revoked_status
        self.requests = []
        self.bearers = []
        self.submissions = {}
        self.cancelled = set()
        self.control_receipts = set()
        self.transport = httpx.MockTransport(self._handle)

    def _rpc_result(self, request_id, result):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": request_id, "result": result})

    def _rpc_error(self, request_id, message):
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32000, "message": message},
        })

    def _tool_result(self, request_id, structured, *, error=None):
        content = [{"type": "text", "text": error}] if error else [{"type": "text", "text": "ok"}]
        return self._rpc_result(request_id, {
            "resultType": "complete", "content": content,
            "structuredContent": {} if error else structured,
            "isError": bool(error),
            "_meta": {"astral/correlationId": "synthetic"},
        })

    def _validate_contract(self, request, body):
        assert request.url.path == "/mcp"
        assert request.headers["content-type"].split(";")[0] == "application/json"
        assert request.headers["MCP-Protocol-Version"] == "2026-07-28"
        assert request.headers["Mcp-Method"] == body["method"]
        assert request.headers["authorization"].startswith("Bearer ")
        meta = body["params"]["_meta"]
        assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
        assert isinstance(meta["io.modelcontextprotocol/clientCapabilities"], dict)
        if body["method"] == "tools/call":
            from scripts.openai_agents_example.contract import encode_mirror_name

            expected = encode_mirror_name(body["params"]["name"])
            assert request.headers["Mcp-Name"] == expected

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == METADATA_PATH:
            body = self.metadata or {
                "resource": "http://testserver/mcp",
                "authorization_servers": ["http://testserver-iam"],
                "scopes_supported": ["mcp:discover", "mcp:tools:read", "mcp:tools:invoke"],
                "bearer_methods_supported": ["header"],
            }
            return httpx.Response(self.metadata_status, json=body)
        bearer = request.headers["authorization"].partition(" ")[2]
        self.bearers.append(bearer)
        body = json.loads(request.content.decode("utf-8"))
        if bearer in self.revoked_bearers:
            headers = {"WWW-Authenticate": 'Bearer resource_metadata="http://testserver/mcp"'} \
                if self.revoked_challenge else {}
            return httpx.Response(self.revoked_status, headers=headers, json={})
        self._validate_contract(request, body)
        request_id = body["id"]
        method = body["method"]
        params = body.get("params", {})
        if method == "server/discover":
            return self._rpc_result(request_id, self.discovery or {
                "resultType": "complete", "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {}}, "ttlMs": 60000, "cacheScope": "private",
            })
        if method == "tools/list":
            tools = self.tools_by_bearer.get(
                bearer, READONLY_TOOLS if bearer == READONLY_BEARER else FULL_TOOLS,
            )
            return self._rpc_result(request_id, {"resultType": "complete", "tools": tools})
        if method == "tools/call":
            return self._tool_call(request_id, params, bearer)
        return self._rpc_error(request_id, "method not found")

    def _scripted(self, name):
        scripted = self.behaviors.get(name)
        if scripted is None:
            return None
        if isinstance(scripted, list):
            return scripted.pop(0) if scripted else None
        return scripted

    def _tool_call(self, request_id, params, bearer):
        name = params["name"]
        arguments = params.get("arguments", {})
        scripted = self._scripted(name)
        if isinstance(scripted, Exception):
            raise scripted
        if isinstance(scripted, tuple) and scripted and scripted[0] == "tool_error":
            return self._tool_result(request_id, {}, error=scripted[1])
        if scripted is not None:
            return self._tool_result(request_id, scripted)
        if name == "astral_submit_operation":
            if bearer == ADMISSION_BEARER:
                return self._tool_result(request_id, {}, error="credential_allowance_exhausted")
            if bearer == READONLY_BEARER:
                return self._tool_result(request_id, {}, error=self.readonly_submit_error)
            if bearer == OTHER_BEARER:
                return self._tool_result(request_id, {}, error=UNAUTHORIZED_CODE)
            if "@example.com" in str(arguments.get("instructions", "")):
                return self._tool_result(request_id, {}, error=SENSITIVE_CODE)
            key = arguments.get("idempotency_key")
            if key in self.submissions:
                return self._tool_result(request_id, {**self.submissions[key], "created": False})
            record = {**operation("queued", 1), "created": True}
            self.submissions[key] = record
            return self._tool_result(request_id, record)
        if name == "astral_get_operation":
            if bearer == OTHER_BEARER and not self.other_owner_get_succeeds:
                return self._tool_result(request_id, {}, error="work_not_found")
            if arguments.get("operation_id") == ARTIFACT_OPERATION_ID:
                return self._tool_result(request_id, {**operation("completed", 3), "id": ARTIFACT_OPERATION_ID})
            return self._tool_result(request_id, operation("queued", 1))
        if name == "astral_list_operations":
            return self._tool_result(request_id, {
                "operations": [], "next_cursor": None, "page_full": False, "resync_required": False,
            })
        if name == "astral_get_operation_events":
            if bearer == OTHER_BEARER:
                return self._tool_result(request_id, {}, error="work_not_found")
            if OPERATION_ID in self.cancelled:
                return self._tool_result(request_id, {
                    "revision": 3, "changed": True, "resync_required": False,
                    "operation": operation("cancelled", 3),
                })
            revision = arguments.get("after_revision") or 0
            next_revision = revision + 1
            disposition = "completed" if revision >= 2 else "active"
            return self._tool_result(request_id, {
                "revision": next_revision, "changed": True, "resync_required": False,
                "operation": operation(disposition, next_revision),
            })
        if name == "astral_cancel_operation":
            self.cancelled.add(arguments.get("operation_id"))
            identity = (arguments.get("operation_id"), arguments.get("submission_id"))
            applied = identity not in self.control_receipts
            self.control_receipts.add(identity)
            return self._tool_result(request_id, {
                "operation": operation("cancelled", 3), "applied": applied,
            })
        if name == "astral_pause_operation":
            return self._tool_result(request_id, {
                "operation": operation("paused", 3), "applied": True,
            })
        if name == "astral_get_artifact":
            if arguments.get("operation_id") == ARTIFACT_OPERATION_ID:
                return self._tool_result(request_id, {
                    "id": ARTIFACT_OPERATION_ID, "revision": 3,
                    "result": {"version": 1, "available": True, "reason": None,
                               "content": {"version": 1, "text": "synthetic retained artifact"}},
                })
            return self._tool_result(request_id, {
                "id": OPERATION_ID, "revision": 3,
                "result": {"version": 1, "available": False, "reason": "unsupported", "content": None},
            })
        return self._tool_result(request_id, {}, error=UNAUTHORIZED_CODE)
