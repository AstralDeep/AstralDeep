"""Bridges artifact tool calls to orchestrator/artifact_html.py after BaseA2AAgent verifies dispatch.
Public tool arguments never supply the owner or conversation; both come from the dispatch binding.
"""

import logging

from orchestrator.artifact_html import (
    AGENT_ID,
    DEFAULT_KIND,
    KINDS,
    MAX_TITLE_CHARS,
    PUBLISH_TOOL,
    UPDATE_TOOL,
    publish,
    update,
)
from shared.agent_runtime import AgentRuntime
from shared.protocol import MCPRequest, MCPResponse

logger = logging.getLogger("ArtifactsAgentMCPServer")

_HTML = {
    "type": "string",
    "minLength": 1,
    "description": (
        "The complete page: an HTML document or a body fragment with inline CSS and JavaScript. "
        "Under 640 KiB once JSON-encoded."
    ),
}

TOOL_REGISTRY = {
    PUBLISH_TOOL: {
        "description": (
            "Publish a self-contained HTML page (an app, visualization or interactive tool) as a "
            "versioned artifact on the canvas. The Mac app shows it in a sandboxed viewer; other "
            "clients offer it as a download. Scripts may load only from cdnjs.cloudflare.com, "
            "cdn.jsdelivr.net, unpkg.com or esm.sh, stylesheets only from Google Fonts, and "
            "everything else must be inline. Returns artifact_id, version and title, or the list "
            "of problems to fix."
        ),
        "scope": "tools:write",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": MAX_TITLE_CHARS,
                          "description": "A 2-4 word name for the page."},
                "html": _HTML,
                "kind": {"type": "string", "enum": list(KINDS), "default": DEFAULT_KIND},
            },
            "required": ["title", "html"],
        },
    },
    UPDATE_TOOL: {
        "description": (
            "Replace the page of an artifact published earlier in this conversation and increase "
            "its version; the newest five versions stay in its history. Same rules and size limit "
            "as publish_artifact. Returns artifact_id, version and title, or the list of problems "
            "to fix."
        ),
        "scope": "tools:write",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "artifact_id": {"type": "string", "pattern": "^art_[0-9a-f]{16}$", "maxLength": 20,
                                "description": "The artifact_id returned by publish_artifact."},
                "html": _HTML,
            },
            "required": ["artifact_id", "html"],
        },
    },
}


class MCPServer:
    def __init__(self, orchestrator):
        self._orchestrator = orchestrator
        self.tools = TOOL_REGISTRY

    def get_tool_list(self) -> list:
        return [{"name": name, **info} for name, info in self.tools.items()]

    def process_request(self, request: MCPRequest) -> MCPResponse:
        if request.method == "tools/list":
            return MCPResponse(request_id=request.request_id, result={"tools": self.get_tool_list()})
        params = request.params or {}
        name, arguments = params.get("name"), params.get("arguments")
        runtime = arguments.get("_runtime") if isinstance(arguments, dict) else None
        if (request.method != "tools/call" or type(name) is not str or name not in self.tools
                or type(runtime) is not AgentRuntime or runtime.request_id != request.request_id
                or runtime.agent_id != AGENT_ID):
            return self._refused(request.request_id)
        binding = getattr(self._orchestrator, "_dispatch_context", {}).get(request.request_id)
        owner = binding.get("requester_owner") if isinstance(binding, dict) else None
        chat_id = binding.get("requester_chat") if isinstance(binding, dict) else None
        if (not isinstance(binding, dict) or binding.get("agent_id") != AGENT_ID
                or binding.get("requester_verified") is not True
                or type(owner) is not str or not owner or type(chat_id) is not str or not chat_id):
            return self._refused(request.request_id)
        public = {key: value for key, value in arguments.items()
                  if not key.startswith("_") and key not in {"user_id", "session_id"}}
        try:
            if name == PUBLISH_TOOL:
                response = publish(public)
            else:
                from orchestrator.plane_repository_context import plane_source_from_orchestrator

                response = update(self._orchestrator, owner=owner, chat_id=chat_id, args=public,
                                  plane_source=plane_source_from_orchestrator(self._orchestrator))
        except Exception:
            logger.exception("artifact tool %s failed", name)
            return MCPResponse(request_id=request.request_id, error={
                "code": "artifact_unavailable", "retryable": True,
                "message": "The artifact could not be saved. Try again.",
            })
        response.request_id = request.request_id
        return response

    @staticmethod
    def _refused(request_id):
        return MCPResponse(request_id=request_id, error={
            "code": "artifact_not_authorized", "retryable": False,
            "message": "Artifacts are unavailable or not authorized in this conversation.",
        })
