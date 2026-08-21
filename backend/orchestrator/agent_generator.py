"""
Agent Code Generator for AstralDeep.

Two generation targets, sharing one LLM-written ``mcp_tools.py``:

**backend** (feature 027 — server-hosted draft agents), 4 files:
- {slug}_agent.py  — from template (not LLM)
- mcp_server.py    — from template (not LLM)
- protected_executor.py — deterministic LETS v1.0.10 final-boundary verifier
- mcp_tools.py     — LLM-generated tool implementations

**byo** (feature 060/074 — user-hosted desktop agents), 4 executable files:
- agent_main.py    — from template (not LLM): self-contained JSON-lines-over-stdio runner
- astralprims_ui.py — deterministic tool-result/UI normalization boundary
- protected_executor.py — deterministic LETS v1.0.10 final-boundary verifier
- mcp_tools.py     — LLM-generated tool implementations

The four files are finalized together into one deterministic v3 runtime
manifest.  ``manifest.json`` is metadata about those bytes, not a fourth input to
their digest.  Runtime contract v2 remains an explicit dispatch-mediated-only
legacy disposition; it is never silently treated as a protected executor.

The BYO bundle must be SELF-CONTAINED: it runs on the owner's desktop, which
ships none of the backend package (no fastapi/uvicorn/a2a-sdk) and sits behind
NAT, so ``BaseA2AAgent``'s inbound uvicorn server is both too heavy and the wrong
topology. See specs/058-byo-agents-runtime/contracts/host-bundle.md.
"""
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import re
import time
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional
import uuid

from astralplane import (
    GENERATED_AGENT_BUNDLE_CONTRACT,
    FinalizedBundle,
    canonical_bundle_digest,
)
from openai import OpenAI
from httpx import Timeout

from orchestrator.agent_spec import generate_llm_prompt_section

logger = logging.getLogger("AgentGenerator")


#: Feature-060 personal-agent runtime contract and the exact reviewed lock file
#: shipped by the Windows host. Tests hash the tracked artifact and fail if the
#: generator, neutral fixture, or packaged host metadata drifts.
BYO_RUNTIME_CONTRACT_VERSION = 3
BYO_LEGACY_RUNTIME_DISPOSITIONS = MappingProxyType(
    {2: "dispatch_mediated_only"}
)
BYO_RUNTIME_LOCK_ARTIFACT = "components/AstralProjection/windows-client/requirements-release.lock.txt"
BYO_RUNTIME_LOCK_SHA256 = (
    "948def355ae1e4c37478d2c87c287d41d3a81055a96ff813c341d1b466096943"
)

#: Exact executable files covered by the v3 immutable canonical-JSON digest.
#: Mapping insertion order never changes the serialized hash input.
BYO_BUNDLE_FILENAMES = GENERATED_AGENT_BUNDLE_CONTRACT.file_names


def _protected_executor_source() -> str:
    """Return the reviewed generated-runtime adapter as deterministic text.

    The emitted module contains only Astral's typed envelope/host checks and
    delegates cryptography, receipt parsing, replay state, clock policy, and
    rollback anchoring to the pinned public LETS v1.0.10 APIs.
    """

    source_path = Path(__file__).with_name("generated_lets_executor.py")
    try:
        source = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("generated protected-executor template is unavailable") from exc
    if not source.endswith("\n"):
        source += "\n"
    compile(source, "protected_executor.py", "exec")
    return source


# Compatibility import name for the legacy publisher during the Plane cutover.
# Plane's public class is the only implementation and validation authority.
FinalizedBYOBundle = FinalizedBundle


# ─── Templates ──────────────────────────────────────────────────────────

AGENT_PY_TEMPLATE = '''#!/usr/bin/env python3
"""
{service_name} — A2A-compliant agent.

{docstring_description}
"""
import asyncio
import os
import sys
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.base_agent import BaseA2AAgent
from agents.{slug}.mcp_server import MCPServer

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


class {class_name}(BaseA2AAgent):
    """{docstring_description}"""

    agent_id = "{agent_id}"
    service_name = "{service_name}"
    description = """{escaped_description}"""
    skill_tags = {skill_tags}

    def __init__(self, port: int = None):
        super().__init__(MCPServer(), port=port, port_env_var="{port_env_var}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='{service_name}')
    parser.add_argument('--port', type=int, default=None, help='Port to run the agent on')
    args = parser.parse_args()

    agent = {class_name}(port=args.port)
    asyncio.run(agent.run())
'''

MCP_SERVER_TEMPLATE = '''#!/usr/bin/env python3
"""
MCP Server for {service_name} — dispatches tool calls to tool functions.
"""
import os
import sys
import json
import inspect
import logging
from typing import Dict, Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from shared.protocol import MCPRequest, MCPResponse
from agents.{slug}.mcp_tools import TOOL_REGISTRY
from agents.{slug}.protected_executor import (
    ProtectedExecutorError,
    extract_permit,
    load_protected_executor,
)

logger = logging.getLogger('{class_name}MCPServer')

RETRYABLE_EXCEPTIONS = (
    ConnectionError, TimeoutError, json.JSONDecodeError, OSError,
)

try:
    import requests
    RETRYABLE_EXCEPTIONS = RETRYABLE_EXCEPTIONS + (
        requests.exceptions.RequestException,
    )
except ImportError:
    pass

NON_RETRYABLE_EXCEPTIONS = (TypeError, KeyError, ValueError, AttributeError)


class MCPServer:
    """MCP server that routes tool/call requests to registered functions."""

    def __init__(self):
        self.tools = TOOL_REGISTRY
        self.protected_executor = load_protected_executor(agent_id={agent_id!r})
        # BaseA2AAgent uses this reviewed marker to avoid claiming the same
        # receipt at its earlier transport seam. This server claims exactly
        # once here, immediately before the generated tool function runs.
        self.protected_executor_at_actuator = True

    def get_tool_list(self) -> list:
        """Return list of available tools with their schemas."""
        return [
            {{
                "name": name,
                "description": info["description"],
                "input_schema": info.get("input_schema", {{"type": "object", "properties": {{}}}})
            }}
            for name, info in self.tools.items()
        ]

    @staticmethod
    def _classify_error(exc: Exception) -> bool:
        """Return True if the error is retryable (transient), False otherwise."""
        if isinstance(exc, RETRYABLE_EXCEPTIONS):
            return True
        if isinstance(exc, NON_RETRYABLE_EXCEPTIONS):
            return False
        return True

    def process_request(self, request: MCPRequest) -> MCPResponse:
        """Process an MCP request and return a response."""
        if request.method == "tools/list":
            return MCPResponse(
                request_id=request.request_id,
                result={{"tools": self.get_tool_list()}}
            )

        if request.method == "tools/call":
            tool_name = request.params.get("name", "")
            arguments = request.params.get("arguments", {{}})

            if tool_name not in self.tools:
                return MCPResponse(
                    request_id=request.request_id,
                    error={{"code": -32601, "message": f"Unknown tool: {{tool_name}}",
                           "retryable": False}}
                )

            try:
                tool_fn = self.tools[tool_name]["function"]
                # Filter out orchestrator-injected kwargs the tool doesn't expect
                sig = inspect.signature(tool_fn)
                params = sig.parameters
                has_var_keyword = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
                )
                if not has_var_keyword:
                    arguments = {{
                        k: v for k, v in arguments.items() if k in params
                    }}
                permit = extract_permit(request.caller_capabilities)
                if self.protected_executor.requires_permit:
                    if permit is None:
                        raise ProtectedExecutorError("missing_protected_permit")
                    self.protected_executor.verify_and_claim(
                        metadata=permit,
                        final_arguments=getattr(
                            request,
                            "_protected_wire_arguments",
                            arguments,
                        ),
                        tool_id=tool_name,
                        tool_scope=self.tools[tool_name].get("scope", ""),
                    )
                result = tool_fn(**arguments)

                if isinstance(result, dict) and "_ui_components" in result:
                    ui_comps = result["_ui_components"]
                    has_error = any(
                        isinstance(c, dict) and c.get("variant") == "error"
                        for c in ui_comps
                    )
                    if has_error:
                        error_msg = "Tool returned an error"
                        for c in ui_comps:
                            if isinstance(c, dict) and c.get("variant") == "error":
                                error_msg = c.get("message", error_msg)
                                break
                        logger.warning(f"Tool '{{tool_name}}' returned error alert: {{error_msg}}")
                        return MCPResponse(
                            request_id=request.request_id,
                            error={{"code": -32603, "message": error_msg,
                                   "retryable": True}}
                        )

                    data = result.get("_data")
                    return MCPResponse(
                        request_id=request.request_id,
                        result=data,
                        ui_components=ui_comps
                    )

                return MCPResponse(
                    request_id=request.request_id,
                    result=result
                )

            except ProtectedExecutorError as e:
                logger.warning("Protected tool '%s' refused: %s", tool_name, e.code)
                return MCPResponse(
                    request_id=request.request_id,
                    error={{"code": -32073, "message": e.code,
                           "retryable": e.retryable}}
                )
            except Exception as e:
                retryable = MCPServer._classify_error(e)
                logger.error(f"Tool '{{tool_name}}' raised {{type(e).__name__}}: {{e}} "
                             f"(retryable={{retryable}})")
                return MCPResponse(
                    request_id=request.request_id,
                    error={{"code": -32603, "message": str(e),
                           "retryable": retryable}}
                )

        return MCPResponse(
            request_id=request.request_id,
            error={{"code": -32601, "message": f"Unknown method: {{request.method}}",
                   "retryable": False}}
        )
'''

# ─── BYO templates (feature 058) ────────────────────────────────────────
#
# Split header/body because the body is brace-dense (dict literals) and
# ``str.format`` would need every one of them doubled. Only the header — the few
# identity constants — is formatted; the runner body is a plain constant.

BYO_AGENT_MAIN_HEADER = '''#!/usr/bin/env python3
"""{service_name} — user-hosted (BYO) agent.

{docstring_description}

Runs as a supervised CHILD PROCESS on the OWNER'S machine and speaks JSON lines
over stdio to its parent (the desktop client), which relays each frame over its
own authenticated tunnel. Self-contained by construction: no backend package, no
inbound server, no third-party import beyond astralprims (which mcp_tools uses).
"""
import inspect
import json
import os
import sys
import uuid

from astralprims_ui import normalize_tool_result
from mcp_tools import TOOL_REGISTRY
from protected_executor import (
    ProtectedExecutorConfigurationError,
    ProtectedExecutorError,
    extract_permit,
    load_protected_executor,
)

AGENT_ID = {agent_id!r}
AGENT_NAME = {agent_name!r}
AGENT_DESCRIPTION = {agent_desc!r}
SKILL_TAGS = {skill_tags!r}
'''

BYO_AGENT_MAIN_BODY = '''

def build_card(protected_executor=None):
    """The AgentCard the orchestrator's registration path expects."""
    return {
        "name": AGENT_NAME,
        "description": AGENT_DESCRIPTION,
        "agent_id": AGENT_ID,
        "version": "1.0.0",
        "skills": [{
            "id": name,
            "name": name,
            "description": info.get("description", ""),
            "input_schema": info.get("input_schema", {"type": "object", "properties": {}}),
            "output_schema": None,
            "tags": SKILL_TAGS,
            "scope": info.get("scope", "tools:read"),
            "metadata": {},
        } for name, info in TOOL_REGISTRY.items()],
        "metadata": {
            "host": "byo_client",
            "transport": "stdio",
            "protected_executor": (
                protected_executor.card_metadata()
                if protected_executor is not None
                else {"contract": "astraldeep.lets/v1", "mode": "off", "ready": True}
            ),
        },
    }


def _emit(frame):
    """One JSON object per line on stdout; the parent relays it verbatim.

    No AGENT_API_KEY: authority on this path is the owner's authenticated UI
    session, never anything the frame presents.
    """
    sys.stdout.write(json.dumps(frame, separators=(",", ":")) + "\\n")
    sys.stdout.flush()


def _canonical_uuid4(value, name):
    if not isinstance(value, str):
        raise ValueError("%s must be UUID4" % name)
    parsed = uuid.UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("%s must be canonical UUID4" % name)
    return value


def _runtime_context():
    """Load the host-owned v3 launch and authority fences."""
    fence_raw = os.environ.get("ASTRAL_RUNTIME_FENCE_JSON")
    authority_raw = os.environ.get("ASTRAL_RUNTIME_AUTHORITY_JSON")
    contract_raw = os.environ.get("ASTRAL_RUNTIME_CONTRACT_VERSION")
    digest = os.environ.get("ASTRAL_RUNTIME_BUNDLE_SHA256")
    mode = os.environ.get("LETS_MODE")
    if [fence_raw, authority_raw, contract_raw, digest, mode] == [
        None, None, None, None, None
    ]:
        # ``generate_byo_files`` remains an explicit feature-058 compatibility
        # helper. A production v3 host always supplies its core launch values and
        # never treats this legacy frame as protected execution.
        return None
    if any(value is None for value in (fence_raw, contract_raw, digest, mode)):
        raise ValueError("runtime launch metadata is incomplete")
    if mode not in {"off", "shadow", "enforce"}:
        raise ValueError("runtime protection mode is invalid")
    fence = json.loads(fence_raw)
    expected_fence = {
        "agent_id", "host_id", "host_session_id", "delivery_id",
        "revision_id", "runtime_instance_id", "process_id",
        "lifecycle_generation",
    }
    if not isinstance(fence, dict) or set(fence) != expected_fence:
        raise ValueError("runtime fence is invalid")
    if fence["agent_id"] != AGENT_ID:
        raise ValueError("runtime agent identity is invalid")
    for name in (
        "host_id", "host_session_id", "delivery_id", "revision_id",
        "runtime_instance_id", "process_id",
    ):
        _canonical_uuid4(fence[name], name)
    generation = fence["lifecycle_generation"]
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
        or generation >= 1 << 64
    ):
        raise ValueError("lifecycle generation is invalid")
    authority = None
    if mode == "off":
        if authority_raw is not None:
            raise ValueError("off runtime must not carry an authority binding")
    elif authority_raw is not None:
        authority = json.loads(authority_raw)
        expected_authority = {
            "owner_id", "binding_id", "lease_id", "lineage_id",
            "population", "executor_audience",
            "agent_id", "runtime_instance_id", "lifecycle_generation",
        }
        if not isinstance(authority, dict) or set(authority) != expected_authority:
            raise ValueError("runtime authority is invalid")
        for name in (
            "owner_id", "binding_id", "lease_id", "lineage_id",
            "executor_audience",
        ):
            value = authority[name]
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError("runtime authority is invalid")
        if authority["population"] != "byo_user":
            raise ValueError("runtime authority population is invalid")
        if (
            authority["agent_id"] != fence["agent_id"]
            or authority["runtime_instance_id"] != fence["runtime_instance_id"]
            or authority["lifecycle_generation"] != generation
        ):
            raise ValueError("runtime authority does not match launch fence")
    elif mode == "enforce":
        if authority_raw is None:
            raise ValueError("protected runtime authority is missing")
    if contract_raw != "3":
        raise ValueError("runtime contract version is unsupported")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(ch not in "0123456789abcdef" for ch in digest)
    ):
        raise ValueError("runtime bundle digest is invalid")
    return {
        "fence": fence,
        "authority": authority,
        "runtime_contract_version": 3,
        "bundle_sha256": digest,
    }


def _valid_fenced_request(req, runtime):
    if req.get("fence") != runtime["fence"]:
        return False
    try:
        _canonical_uuid4(req.get("request_id"), "request_id")
        _canonical_uuid4(req.get("request_generation"), "request_generation")
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _fence_response(req, response, runtime):
    response = dict(response)
    response.setdefault("result_type", "complete")
    response.setdefault(
        "responder_info",
        {"name": AGENT_ID, "version": "1.0.0"},
    )
    if runtime is None:
        return response
    response["fence"] = runtime["fence"]
    response["request_id"] = req["request_id"]
    response["request_generation"] = req["request_generation"]
    return response


def dispatch(req, protected_executor=None):
    """One mcp_request dict -> one mcp_response dict."""
    rid = req.get("request_id", "")
    method = req.get("method", "")

    if method == "tools/list":
        return {"type": "mcp_response", "request_id": rid, "result": {"tools": [
            {"name": n, "description": i.get("description", ""),
             "input_schema": i.get("input_schema", {"type": "object", "properties": {}})}
            for n, i in TOOL_REGISTRY.items()]}}

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name", "")
        # The permit is bound to the exact post-orchestrator wire arguments.
        # Signature filtering is a local invocation concern and must not alter
        # the receipt-bound evidence checked immediately before actuation.
        wire_arguments = dict(params.get("arguments", {}) or {})
        args = dict(wire_arguments)
        info = TOOL_REGISTRY.get(name)
        if not info:
            return {"type": "mcp_response", "request_id": rid,
                    "error": {"code": -32601, "message": "Unknown tool: %s" % name,
                              "retryable": False}}
        try:
            fn = info["function"]
            sig = inspect.signature(fn)
            if not any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
                args = {k: v for k, v in args.items() if k in sig.parameters}
            if protected_executor is None:
                protected_executor = load_protected_executor(agent_id=AGENT_ID)
            permit = extract_permit(req.get("caller_capabilities"))
            if protected_executor.requires_permit:
                if permit is None:
                    raise ProtectedExecutorError("missing_protected_permit")
                protected_executor.verify_and_claim(
                    metadata=permit,
                    final_arguments=wire_arguments,
                    tool_id=name,
                    tool_scope=info.get("scope", ""),
                )
            result = fn(**args)
            data, comps, tool_error = normalize_tool_result(result)
            # The tool-error convention the backend MCPServer implements: a tool
            # that handled its own failure returns create_ui_response([
            # Alert(variant="error")]). That is an ERROR response, not a success
            # one — without this the orchestrator would treat a failed BYO tool
            # call as having succeeded.
            if tool_error is not None:
                return {"type": "mcp_response", "request_id": rid,
                        "error": {"code": -32603,
                                  "message": tool_error,
                                  "retryable": True}}
            return {"type": "mcp_response", "request_id": rid,
                    "result": data, "ui_components": comps}
        except ProtectedExecutorError as exc:
            return {"type": "mcp_response", "request_id": rid,
                    "error": {"code": -32073, "message": exc.code,
                              "retryable": exc.retryable}}
        except Exception as exc:
            return {"type": "mcp_response", "request_id": rid,
                    "error": {"code": -32603, "message": str(exc), "retryable": True}}

    return {"type": "mcp_response", "request_id": rid,
            "error": {"code": -32601, "message": "Unknown method: %s" % method,
                      "retryable": False}}


def main():
    # stdout is the channel — force UTF-8 so a non-ASCII tool result cannot
    # wedge the pipe on a Windows console default codepage.
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    try:
        runtime = _runtime_context()
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        sys.stderr.write("invalid personal-agent runtime launch metadata\\n")
        return 78

    try:
        protected_executor = load_protected_executor(agent_id=AGENT_ID)
    except ProtectedExecutorConfigurationError:
        sys.stderr.write("protected executor is not ready\\n")
        return 78

    if runtime is None:
        _emit({"type": "register_agent", "agent_card": build_card(protected_executor)})
    else:
        _emit({
            "type": "agent_runtime_register",
            "fence": runtime["fence"],
            "runtime_contract_version": runtime["runtime_contract_version"],
            "bundle_sha256": runtime["bundle_sha256"],
            "agent_card": build_card(protected_executor),
        })

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except (ValueError, TypeError):
            # A stray print() in tool code must not corrupt the channel.
            sys.stderr.write("discarded non-JSON line on stdin\\n")
            continue
        if not isinstance(req, dict):
            continue
        if runtime is not None and not _valid_fenced_request(req, runtime):
            continue
        if req.get("type") == "mcp_request" or "method" in req:
            _emit(_fence_response(req, dispatch(req, protected_executor), runtime))

    return 0   # EOF: the child dies with its parent


if __name__ == "__main__":
    raise SystemExit(main())
'''


BYO_ASTRALPRIMS_UI = '''"""Deterministic BYO tool-result normalization.

The LLM-written tool module may construct astralprims objects, but its public
return contract is plain JSON-compatible data.  Keeping the normalization in a
separate deterministic file makes the complete runtime bundle explicit and
keeps generated tool code focused on the approved capabilities.
"""


def normalize_tool_result(result):
    """Return ``(data, components, safe_error_message)`` for one tool result."""
    components = result.get("_ui_components") if isinstance(result, dict) else None
    data = result.get("_data") if isinstance(result, dict) else result
    if isinstance(components, list):
        for component in components:
            if isinstance(component, dict) and component.get("variant") == "error":
                message = component.get("message")
                if not isinstance(message, str) or not message:
                    message = "Tool returned an error"
                return data, components, message[:1024]
    return data, components, None
'''

#: A BYO bundle that reaches for the backend package would ImportError on the
#: desktop host (and, if it somehow resolved, would import server-side code onto
#: a user's machine). Checked as a GATE on the LLM-written file, not merely asked
#: for in the prompt.
BYO_FORBIDDEN_PATTERNS = (
    "from shared", "import shared", "from agents.", "import agents.",
    "sys.path.insert",
)


def byo_import_violations(code: str) -> List[str]:
    """The forbidden backend-coupling patterns present in a BYO bundle file."""
    return [p for p in BYO_FORBIDDEN_PATTERNS if p in (code or "")]


# ─── Security rules (shared by generate + refine) ───────────────────────

_SECURITY_RULES_COMMON = """- Do NOT use `eval()`, `exec()`, `compile()`, or `__import__()`
- Do NOT use `subprocess`, `os.system`, `os.popen`, or any shell execution
- Do NOT touch `os.environ` or `os.getenv` AT ALL — not even to read a config
  value. Any reference to them is refused and your code is never run. Take
  configuration from the tool's own input schema instead.
- Do NOT use `globals()`, `locals()`, `setattr()`, or `delattr()`
- Do NOT use `pickle`, `marshal`, or `yaml.load` (unsafe deserialization)
- Do NOT write/read files outside of returning data
- Do NOT use `ctypes`, `cffi`, or native code execution

Code that breaks any of these rules is REFUSED by a static analyzer BEFORE it
is ever imported or run, so a violation is a dead end, not a warning."""

#: Server-hosted (027) image: requests/httpx ARE installed.
_SECURITY_RULES_BACKEND = f"""{_SECURITY_RULES_COMMON}
- Do NOT open network sockets directly (use `requests`/`httpx` for HTTP only)
- Import ONLY the Python standard library and packages already installed in this
  image. Do NOT assume any `pip install` is available — there is no network or
  package-install step. If a format truly needs an unavailable library, do a
  best-effort structural extraction with the standard library (e.g. treat
  zip/OOXML/epub as `zipfile` + XML, archives via `tarfile`/`zipfile`, columnar
  or binary data via a documented partial read) and clearly state the limitation
  in the returned output rather than failing."""

#: BYO (058) desktop host: ONLY the standard library + astralprims exist there.
#: An `import requests` bundle dies at import on the user's machine, never sends
#: `register_agent`, and surfaces only as the host's silence timeout — so the
#: allowlist is enforced as a GATE at generation time (agent_validator).
_SECURITY_RULES_BYO = f"""{_SECURITY_RULES_COMMON}
- Do NOT open network sockets directly. For HTTP use `urllib.request` from the
  standard library — `requests` and `httpx` are NOT available on the user's machine.
- Import ONLY the Python standard library and `astralprims`. NOTHING else is
  installed where this agent runs. Do NOT assume any `pip install` is available,
  do NOT import from `shared` or `agents.`, and NEVER touch `sys.path`.
  A file that imports anything else is REJECTED and never delivered.
- Import by literal name at the top of the file. `importlib.import_module(...)`
  and `__import__(...)` are checked against the same allowlist, and a module
  name computed at runtime is REJECTED because it cannot be checked at all."""


def security_rules_block(self_contained: bool = False) -> str:
    """The SECURITY RULES prompt block for the target the code will run on."""
    return _SECURITY_RULES_BYO if self_contained else _SECURITY_RULES_BACKEND


# ─── Code Generator ─────────────────────────────────────────────────────

class AgentCodeGenerator:
    """Generates agent code files using LLM for tool implementations and templates for boilerplate."""

    def __init__(self, llm_client: Optional[OpenAI] = None, llm_model: str = None,
                 config_resolver=None):
        """Args:
            llm_client / llm_model: An explicit pre-built client (tests,
                injection seams). When absent, ``config_resolver`` is used.
            config_resolver: Zero-arg SYNC callable returning the current
                system LLM configuration (feature 054 — codegen is a
                system-context flow billed to the admin-managed credential;
                the retired ``OPENAI_*`` env fallback is gone). Resolved
                per generation call so an admin save takes effect without
                a restart.
        """
        self.llm_client = llm_client
        self.llm_model = llm_model
        self._config_resolver = config_resolver

    async def _aresolve_client(self, config_resolver=None):
        """Resolve (client, model) for one generation call, or (None, None).

        ``config_resolver`` overrides the default (system) resolver for this call.
        BYO authoring passes the OWNER's resolver: the user is actively authoring
        their own private agent, so its code is generated with THEIR configured
        LLM — not the admin-managed system credential that background codegen uses
        (feature 054). A direct ``llm_client`` still wins (tests inject it)."""
        if self.llm_client is not None:
            return self.llm_client, self.llm_model
        resolver = config_resolver or self._config_resolver
        if resolver is None:
            return None, None
        try:
            cfg = await asyncio.to_thread(resolver)
        except Exception:
            logger.exception("agent codegen: LLM config resolution failed")
            return None, None
        if cfg is None:
            return None, None
        from llm_config.client_factory import openai_auth_kwargs

        return OpenAI(
            base_url=cfg.base_url,
            timeout=Timeout(180.0, connect=10.0),
            **openai_auth_kwargs(getattr(cfg, "api_key", "") or ""),
        ), cfg.model

    def _slugify(self, name: str) -> str:
        """Convert agent name to a safe directory/module slug."""
        slug = re.sub(r'[^a-z0-9]+', '_', name.lower().strip())
        slug = slug.strip('_')
        return slug or 'custom_agent'

    def _class_name(self, slug: str) -> str:
        """Convert slug to PascalCase class name."""
        return ''.join(word.capitalize() for word in slug.split('_')) + 'Agent'

    @staticmethod
    def _sanitize_description(description: str) -> str:
        """Sanitize description for safe injection into Python source code.

        Returns a single-line string safe for use in triple-quoted strings.
        Collapses whitespace, escapes backslashes and triple quotes, and
        ensures the string doesn't end with a quote (which would collide
        with the closing triple-quote delimiter).
        """
        # Collapse all whitespace (newlines, tabs, multiple spaces) to single spaces
        safe = ' '.join(description.split())
        # Escape backslashes first, then triple quotes
        safe = safe.replace('\\', '\\\\').replace('"""', '\\"\\"\\"')
        # If it ends with a quote, add a trailing space to prevent """" ambiguity
        if safe.endswith('"'):
            safe += ' '
        return safe

    @staticmethod
    def default_agent_id(slug: str) -> str:
        """The runtime agent id a slug implies on the server-hosted (027) path."""
        return f"{slug.replace('_', '-')}-1"

    def generate_template_files(self, agent_name: str, description: str,
                                 slug: str, skill_tags: List[str] = None,
                                 agent_id: Optional[str] = None) -> Dict[str, str]:
        """Generate the boilerplate agent_py and mcp_server files from templates.

        ``agent_id`` defaults to the slug-derived id, so the 027 path is
        byte-identical. It is explicit because a BYO agent's identity is
        owner-namespaced (``ua-<name>-<ownerhash>``) and must be the id the card
        presents — the registry looks the card's id up and refuses fail-closed on
        a mismatch (``user_agents.authorize_registration``)."""
        class_name = self._class_name(slug)
        agent_id = agent_id or self.default_agent_id(slug)
        port_env_var = f"{slug.upper()}_AGENT_PORT"
        tags_repr = repr(skill_tags or [])
        safe_desc = self._sanitize_description(description)

        agent_py = AGENT_PY_TEMPLATE.format(
            service_name=agent_name.replace('"', '\\"'),
            docstring_description=safe_desc,
            escaped_description=safe_desc,
            slug=slug,
            class_name=class_name,
            agent_id=agent_id,
            skill_tags=tags_repr,
            port_env_var=port_env_var,
        )

        mcp_server_py = MCP_SERVER_TEMPLATE.format(
            service_name=agent_name,
            slug=slug,
            class_name=class_name,
            agent_id=agent_id,
        )

        return {
            f"{slug}_agent.py": agent_py,
            "mcp_server.py": mcp_server_py,
            "protected_executor.py": _protected_executor_source(),
        }

    def generate_byo_files(self, agent_name: str, description: str,
                           agent_id: str, skill_tags: List[str] = None,
                           constitution_version: Optional[str] = None) -> Dict[str, str]:
        """Build the legacy feature-058 scaffold and provisional manifest.

        New code MUST use :meth:`generate_byo_scaffold` and then
        :meth:`finalize_byo_bundle` after ``mcp_tools.py`` exists.  This helper is
        retained only so an older caller does not silently receive a different
        return shape before the v2 delivery seam is wired.

        The runner bakes the OWNER-NAMESPACED ``agent_id`` it is handed; a
        slug-derived id here would be refused at registration and the refusal is
        silent on the wire (host-bundle.md §6)."""
        safe_desc = self._sanitize_description(description)
        agent_main = BYO_AGENT_MAIN_HEADER.format(
            service_name=agent_name.replace('"', '\\"'),
            docstring_description=safe_desc,
            agent_id=agent_id,
            agent_name=agent_name,
            agent_desc=safe_desc,
            skill_tags=list(skill_tags or []),
        ) + BYO_AGENT_MAIN_BODY

        manifest = json.dumps({
            "agent_id": agent_id,
            "agent_name": agent_name,
            "description": safe_desc,
            "constitution_version": constitution_version,
            "generated_at": int(time.time() * 1000),
        }, indent=2) + "\n"

        return {"agent_main.py": agent_main, "manifest.json": manifest}

    def generate_byo_scaffold(
        self,
        *,
        agent_name: str,
        description: str,
        agent_id: str,
        skill_tags: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        """Generate the deterministic executable half of a v3 BYO bundle.

        ``mcp_tools.py`` is deliberately absent: the lifecycle manager adds the
        final, statically validated LLM output and only then finalizes the
        revision manifest and digest.
        """

        safe_desc = self._sanitize_description(description)
        agent_main = BYO_AGENT_MAIN_HEADER.format(
            service_name=agent_name.replace('"', '\\"'),
            docstring_description=safe_desc,
            agent_id=agent_id,
            agent_name=agent_name,
            agent_desc=safe_desc,
            skill_tags=list(skill_tags or []),
        ) + BYO_AGENT_MAIN_BODY
        return {
            "agent_main.py": agent_main,
            "astralprims_ui.py": BYO_ASTRALPRIMS_UI,
            "protected_executor.py": _protected_executor_source(),
        }

    @staticmethod
    def _bundle_digest(files: Mapping[str, str]) -> str:
        """Hash the exact four-file map using the host's canonical JSON rule."""

        return canonical_bundle_digest(files, GENERATED_AGENT_BUNDLE_CONTRACT)

    def finalize_byo_bundle(
        self,
        *,
        files: Mapping[str, str],
        agent_id: str,
        revision_id: str,
        agent_name: str,
        description: str,
        constitution_version: str,
        required_runtime_lock_sha256: str,
    ) -> FinalizedBundle:
        """Finalize one immutable v3 revision after all four files exist.

        The digest contains no timestamp, filesystem path, mapping order, or
        serialization-dependent value.  ``manifest.json`` names the already
        finalized bytes and therefore cannot create a circular hash.
        """

        if not isinstance(files, Mapping):
            raise TypeError("files must be a mapping")
        if set(files) != set(BYO_BUNDLE_FILENAMES):
            raise ValueError("v3 BYO bundle must contain exactly four approved files")
        ordered_files: dict[str, str] = {}
        for filename in BYO_BUNDLE_FILENAMES:
            content = files[filename]
            if not isinstance(content, str):
                raise TypeError(f"{filename} must be UTF-8 text")
            # Encoding now makes malformed surrogate input fail before hashing or
            # delivery, so every digest always identifies actual UTF-8 bytes.
            content.encode("utf-8")
            ordered_files[filename] = content

        try:
            revision_id = str(uuid.UUID(str(revision_id)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("revision_id must be a UUID") from exc
        if required_runtime_lock_sha256 != BYO_RUNTIME_LOCK_SHA256:
            raise ValueError("required runtime lock must match packaged runtime lock")

        bundle_sha256 = self._bundle_digest(ordered_files)
        file_manifest = []
        for filename in BYO_BUNDLE_FILENAMES:
            content_bytes = ordered_files[filename].encode("utf-8")
            file_manifest.append(
                {
                    "name": filename,
                    "sha256": hashlib.sha256(content_bytes).hexdigest(),
                    "size_bytes": len(content_bytes),
                }
            )
        manifest = {
            "manifest_version": 2,
            "runtime_contract_version": BYO_RUNTIME_CONTRACT_VERSION,
            "revision_id": revision_id,
            "agent_id": agent_id,
            "agent_name": str(agent_name),
            "description": self._sanitize_description(str(description)),
            "constitution_version": constitution_version,
            "required_runtime_lock_sha256": required_runtime_lock_sha256,
            "digest_algorithm": "sha256",
            "bundle_sha256": bundle_sha256,
            "files": file_manifest,
        }
        manifest_json = json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n"
        return FinalizedBundle(
            contract=GENERATED_AGENT_BUNDLE_CONTRACT,
            files=ordered_files,
            bundle_sha256=bundle_sha256,
            manifest=manifest,
            manifest_json=manifest_json,
        )

    async def generate_tools_file(self, agent_name: str, description: str,
                                   tools_spec: List[Dict[str, Any]],
                                   packages: List[str] = None,
                                   knowledge_context: str = "",
                                   self_contained: bool = False,
                                   config_resolver=None) -> str:
        """Use LLM to generate mcp_tools.py with tool implementations.

        ``self_contained`` (BYO): the file runs on the owner's desktop, which has
        no backend package — say so in the prompt. The hard guarantee is the
        ``byo_import_violations`` gate on the result, not this instruction.
        ``config_resolver`` (BYO): use the owner's LLM, not the system one."""
        _client, _model = await self._aresolve_client(config_resolver)
        if not _client:
            raise RuntimeError("LLM not configured — cannot generate agent tools")

        tools_description = ""
        if tools_spec:
            for i, tool in enumerate(tools_spec, 1):
                tools_description += f"\n{i}. **{tool.get('name', f'tool_{i}')}**\n"
                tools_description += f"   - Description: {tool.get('description', 'No description')}\n"
                if tool.get('input_schema'):
                    tools_description += f"   - Input schema: {json.dumps(tool['input_schema'])}\n"
                if tool.get('scope'):
                    tools_description += f"   - Scope: {tool['scope']}\n"

        packages_note = ""
        if packages:
            packages_note = f"\nAllowed packages to import: {', '.join(packages)}"
        if self_contained:
            packages_note += (
                "\n\nThis agent runs on the USER'S OWN DESKTOP, not on the server. "
                "The file MUST be self-contained: import ONLY the Python standard "
                "library and `astralprims`. NEVER import from `shared`, from "
                "`agents.`, and NEVER touch `sys.path`."
            )

        ui_spec = generate_llm_prompt_section(self_contained=self_contained)

        knowledge_section = ""
        if knowledge_context:
            knowledge_section = f"""
## Proven Patterns & Techniques
The following patterns have been learned from production usage and should inform your implementation:

{knowledge_context}
"""

        prompt = f"""You are a Python code generator for an agent tool system. Generate a complete `mcp_tools.py` file.

## Agent Info
- Name: {agent_name}
- Description: {description}

## Tools to Implement
{tools_description if tools_description else "Create appropriate tools based on the agent description."}
{packages_note}

{ui_spec}
{knowledge_section}

## CREDENTIAL DECLARATION

If this agent needs external API keys, OAuth tokens, or other secrets for **third-party services**
(e.g. a weather API, email service, database), declare them with REQUIRED_CREDENTIALS:

```python
REQUIRED_CREDENTIALS = [
    {{
        "key": "SERVICE_API_KEY",        # UPPER_SNAKE_CASE key name
        "label": "Service API Key",      # Human-readable label
        "description": "Get this from ...",  # Help text for the user
        "required": True,                # True if agent cannot work without it
        "type": "api_key"                # One of: api_key, oauth_client_id, oauth_client_secret, token, password, username
    }},
]
```

If the agent does NOT need any external credentials (e.g. it only generates data locally
or uses public APIs), set `REQUIRED_CREDENTIALS = []`.

**NEVER declare credentials for the LLM itself** (no OpenAI key, no model config, no AI/LLM API keys).
The LLM is provided by the system and shared across all agents — agents do not need their own LLM credentials.
Only declare credentials for external third-party services the agent's tools call directly.

IMPORTANT: Credentials are injected at runtime via the `_credentials` dict parameter.
Inside tool functions, accept `**kwargs` and access them like:
`api_key = kwargs.get("_credentials", {{}}).get("SERVICE_API_KEY", "")`
Do NOT hardcode secrets. Do NOT use os.environ for secrets.

## SECURITY RULES — You MUST follow these:
{security_rules_block(self_contained)}

Output ONLY the Python code. No markdown fences, no explanations."""

        messages = [
            {"role": "system", "content": "You are a precise Python code generator. Output ONLY valid Python code, no markdown fences or explanations."},
            {"role": "user", "content": prompt}
        ]

        response = await asyncio.to_thread(
            _client.chat.completions.create,
            model=_model,
            messages=messages,
            temperature=0.2,
        )

        code = response.choices[0].message.content.strip()
        # Strip markdown fences if present
        if code.startswith("```"):
            lines = code.split("\n")
            # Remove first and last fence lines
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            code = "\n".join(lines)

        return code

    async def refine_tools_file(self, current_code: str, user_message: str,
                                 agent_name: str, description: str,
                                 self_contained: bool = False,
                                 config_resolver=None) -> str:
        """Refine existing mcp_tools.py based on user feedback.

        ``self_contained`` (BYO): the refinement must stay runnable on the owner's
        desktop — no backend package, no sys.path shim, stdlib + astralprims only.
        The auto-fix loop refines BYO code too, so a refine prompt that mandated
        the backend imports block would hand the self-containment gate a file it
        must reject.
        ``config_resolver`` (BYO): use the owner's LLM, not the system one."""
        _client, _model = await self._aresolve_client(config_resolver)
        if not _client:
            raise RuntimeError("LLM not configured — cannot refine agent tools")

        ui_spec = generate_llm_prompt_section(self_contained=self_contained)

        prompt = f"""You are refining the tool implementations for an agent.

## Agent Info
- Name: {agent_name}
- Description: {description}

## Current mcp_tools.py code:
```python
{current_code}
```

## User's requested changes:
{user_message}

{ui_spec}

IMPORTANT: Ensure all UI components use the astralprims classes (Card, MetricCard, Alert, etc.)
and call `.to_dict()` to serialize them. Do NOT use raw dicts for UI components.

## CREDENTIAL DECLARATION

The file must include a `REQUIRED_CREDENTIALS` list at the module level. If the agent needs
external API keys, OAuth tokens, or other secrets for **third-party services**, declare each one:

```python
REQUIRED_CREDENTIALS = [
    {{"key": "SERVICE_API_KEY", "label": "Service API Key", "description": "Get this from ...", "required": True, "type": "api_key"}},
]
```

If no credentials are needed, set `REQUIRED_CREDENTIALS = []`.
If the refinement adds or removes API integrations, update REQUIRED_CREDENTIALS accordingly.
Access credentials at runtime via: `kwargs.get("_credentials", {{}}).get("KEY", "")`

**NEVER declare credentials for the LLM/AI model** (no OpenAI key, no model config).
The LLM is system-provided and shared across all agents. Only declare credentials for external services.

## SECURITY RULES — You MUST follow these:
{security_rules_block(self_contained)}

Apply the requested changes and output the COMPLETE updated mcp_tools.py file.
Output ONLY the Python code. No markdown fences, no explanations."""

        messages = [
            {"role": "system", "content": "You are a precise Python code generator. Output ONLY valid Python code, no markdown fences or explanations."},
            {"role": "user", "content": prompt}
        ]

        response = await asyncio.to_thread(
            _client.chat.completions.create,
            model=_model,
            messages=messages,
            temperature=0.2,
        )

        code = response.choices[0].message.content.strip()
        if code.startswith("```"):
            lines = code.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            code = "\n".join(lines)

        return code
