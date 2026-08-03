"""
BaseA2AAgent — Base class for all AstralDeep agents with WebSocket + A2A dual transport.

Provides:
- WebSocket endpoint (/agent) for orchestrator communication (default internal transport)
- A2A JSON-RPC endpoint for external A2A-compliant clients
- Legacy agent card (/.well-known/agent-card.json) for backward compat
- Official A2A agent card (/a2a/.well-known/agent-card.json via a2a-sdk routes)
- Health check endpoint (/health)
- Orchestrator-mediated agent-to-agent hops via ``AgentRuntime.call_agent_tool``
  (feature 056). The direct peer-call path was RETIRED — it forwarded the
  caller's delegation token unattenuated and bypassed the orchestrator's gate
  stack; agents can no longer open peer transports (see the "Agent-to-Agent
  Communication" section below).
"""
import asyncio
import inspect
import json
import os
import sys
import logging
import uuid
import socket
from typing import Set, Dict, Optional, Any, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from shared.protocol import (
    Message, RegisterAgent, MCPRequest, MCPResponse,
    AgentCard, AgentSkill,
    MCPProtocolError, MCP_INVALID_REQUEST,
    ToolStreamData, ToolStreamEnd, ToolStreamCancel,
)
from shared.feature_flags import flags
from shared.stream_sdk import (
    StreamComponents, StreamCtx, StreamPayloadError,
    is_streaming_tool, get_stream_metadata,
    assign_stream_id_to_components, validate_chunk_size,
)
from shared.a2a_bridge import custom_card_to_a2a
from shared.a2a_executor import MCPAgentExecutor
from shared.a2a_security import A2ASecurityValidator
from shared.crypto import (
    generate_ec_keypair, build_jwk, save_private_key, load_private_key,
    decrypt_from_orchestrator, is_e2e_encrypted,
)


logger = logging.getLogger("BaseA2AAgent")

BASE_PORT = int(os.getenv("AGENT_PORT", "8003"))
MAX_PORT_OFFSET = 20


class EndpointFilter(logging.Filter):
    """Filter out noisy agent-card polling from access logs."""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "/.well-known/agent-card.json" not in msg and "/.well-known/agent.json" not in msg


def find_available_port(start_port: int = BASE_PORT, max_offset: int = MAX_PORT_OFFSET) -> int:
    """Find an available port starting from start_port."""
    for offset in range(max_offset):
        port = start_port + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                if s.connect_ex(('localhost', port)) != 0:
                    return port
        except Exception:
            continue
    return start_port


class BaseA2AAgent:
    """Base class for all AstralDeep agents with WebSocket + A2A dual transport.

    Subclasses must provide:
        - agent_id: Unique identifier (e.g. "general-1")
        - service_name: Human-readable name (e.g. "General Agent")
        - description: Agent description
        - mcp_server: An MCPServer instance with .tools and .process_request()

    Optional overrides:
        - skill_tags: Default tags to add to all skills
        - card_metadata: Extra metadata for the agent card (e.g. required_credentials)
    """

    agent_id: str = ""
    service_name: str = ""
    description: str = ""
    skill_tags: List[str] = []
    card_metadata: Dict[str, Any] = {}

    def __init__(self, mcp_server, port: int = None, port_env_var: str = None, default_port_offset: int = 0):
        """
        Args:
            mcp_server: The agent's MCPServer instance.
            port: Explicit port (from command line).
            port_env_var: Environment variable name for port (e.g. "WEATHER_AGENT_PORT").
            default_port_offset: Fallback offset from BASE_PORT if no port is found.
        """
        self.host = os.getenv("HOST", "0.0.0.0")
        self.mcp_server = mcp_server
        self.orchestrator_connections: Set[WebSocket] = set()

        # Port resolution: explicit > env var > dynamic discovery
        if port is not None:
            self.port = port
        elif port_env_var and os.getenv(port_env_var):
            self.port = int(os.getenv(port_env_var))
        else:
            self.port = find_available_port(BASE_PORT)

        # ECIES key pair for end-to-end credential encryption (must init before card build)
        self._init_crypto()

        # Build agent cards (includes public key JWK in metadata)
        self.card = self._build_agent_card()

        # Security validator for A2A requests
        self._security_validator = A2ASecurityValidator()

        # 001-tool-stream-ui: in-flight streaming tasks keyed by stream_id so
        # an inbound ToolStreamCancel can find and cancel the right generator.
        # Each entry is (asyncio.Task, Optional[StreamCtx]) — ctx is non-None
        # only for callback-style tools that take a StreamCtx parameter.
        self._active_streams: Dict[str, "tuple[asyncio.Task, Optional[StreamCtx]]"] = {}

        # Strong refs to the outer wrapper tasks for streaming dispatches.
        # asyncio only keeps weak refs to create_task results, so we pin each
        # task here until it completes — otherwise the GC can collect the
        # wrapper before it registers its runner_task in _active_streams.
        self._stream_wrapper_tasks: set = set()

        self._logger = logging.getLogger(self.__class__.__name__)

    def _init_crypto(self):
        """Initialize EC P-256 key pair for end-to-end credential decryption.

        Key resolution order:
        1. ``AGENT_KEY_PATH`` env var (operator override).
        2. Per-agent path under ``backend/data/agent_keys/<agent_id>.pem``.
           ``backend/data/`` is bind-mounted from the host in the standard
           docker-compose setup, so keys survive container recreation —
           which is critical because all per-user credentials are ECIES-
           encrypted to this key, and a regenerated key invalidates every
           saved credential at once.
        3. Legacy ``<agent_module>/data/agent_key.pem`` — only honored
           when it already exists, for back-compat with existing installs.
           Net-new keys never go there because the agent module dir is
           inside the container's writable layer (not persisted).
        """
        backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        central_path = os.path.join(
            backend_dir, "data", "agent_keys", f"{self.agent_id or 'unknown'}.pem"
        )
        legacy_path = None
        agent_module = sys.modules.get(self.__class__.__module__)
        if agent_module and hasattr(agent_module, "__file__") and agent_module.__file__:
            agent_dir = os.path.dirname(os.path.abspath(agent_module.__file__))
            legacy_path = os.path.join(agent_dir, "data", "agent_key.pem")

        env_path = os.getenv("AGENT_KEY_PATH")
        if env_path:
            key_path = env_path
        elif os.path.exists(central_path):
            key_path = central_path
        elif legacy_path and os.path.exists(legacy_path):
            key_path = legacy_path
            logger.warning(
                f"Loading legacy agent key from {legacy_path}; "
                f"copy it to {central_path} so it survives container recreation."
            )
        else:
            key_path = central_path

        if os.path.exists(key_path):
            self._private_key = load_private_key(key_path)
            logger.info(f"Loaded agent ECIES key from {key_path}")
        else:
            self._private_key, _ = generate_ec_keypair()
            save_private_key(self._private_key, key_path)
            logger.info(f"Generated new agent ECIES key at {key_path}")

        self._public_key = self._private_key.public_key()
        self._public_key_jwk = build_jwk(self._public_key)

        # Feature 029: predecessor keys for consolidated agents. Credentials
        # saved while a PREDECESSOR identity was live are ECIES-encrypted to
        # that identity's key; loading those keys keeps them decryptable
        # after a catalog merge (FR-008) instead of forcing a re-save.
        self._fallback_private_keys = []
        for pred_id in getattr(self, "predecessor_agent_ids", ()) or ():
            pred_path = os.path.join(backend_dir, "data", "agent_keys", f"{pred_id}.pem")
            if os.path.exists(pred_path):
                try:
                    self._fallback_private_keys.append(load_private_key(pred_path))
                    logger.info(f"Loaded predecessor ECIES key for '{pred_id}'")
                except Exception:
                    logger.warning(f"Could not load predecessor key {pred_path}", exc_info=True)

    def _build_agent_card(self) -> AgentCard:
        """Build custom AgentCard from registered MCP tools."""
        skills = []
        for name, info in self.mcp_server.tools.items():
            desc = info.get("description", "No description provided")
            tags = list(self.skill_tags) if self.skill_tags else []
            skill_metadata = {}
            # Legacy single-key form: top-level "streamable" with a poll
            # config dict. Defaults streaming_kind to "poll" so the
            # validate_streaming_metadata check at RegisterAgent time (which
            # requires an explicit kind) accepts it.
            if "streamable" in info:
                skill_metadata["streamable"] = info["streamable"]
                if isinstance(info["streamable"], dict):
                    skill_metadata.setdefault("streaming_kind", "poll")
            # 001-tool-stream-ui: tools may declare a full metadata dict
            # under the "metadata" key (containing streamable, streaming_kind,
            # max_fps, min_fps, max_chunk_bytes). Merge it into the skill
            # metadata. The orchestrator's validate_streaming_metadata
            # enforces the shape at register_agent time.
            tool_metadata = info.get("metadata")
            if isinstance(tool_metadata, dict):
                skill_metadata.update(tool_metadata)
            # Feature 063 (FR-025): surface the registry's destructive
            # classification on the card so clients can mark destructive verbs
            # BEFORE any grant. Passed through unchanged ("never" | "always" |
            # "if_exists" | {"by_action": [...]}) — the confirmation gate stays
            # the sole enforcer; this is display metadata only.
            if "destructive" in info:
                skill_metadata["destructive"] = info["destructive"]
            skills.append(AgentSkill(
                name=name,
                description=desc,
                id=name,
                input_schema=info.get("input_schema"),
                output_schema=info.get("output_schema"),
                tags=tags,
                scope=info.get("scope", "tools:read"),
                metadata=skill_metadata,
            ))

        metadata = dict(self.card_metadata) if self.card_metadata else {}
        metadata["public_key_jwk"] = self._public_key_jwk

        return AgentCard(
            name=self.service_name,
            description=self.description,
            agent_id=self.agent_id,
            version="1.0.0",
            skills=skills,
            metadata=metadata,
        )

    def _build_a2a_card(self):
        """Build official A2A AgentCard from custom card."""
        base_url = f"http://{self.host}:{self.port}"
        return custom_card_to_a2a(self.card, base_url)

    async def handle_websocket(self, websocket: WebSocket):
        """Handle WebSocket connection from orchestrator or peer agent."""
        await websocket.accept()
        self._logger.info("Connection established via WebSocket")
        self.orchestrator_connections.add(websocket)

        try:
            # Send registration with agent card. The shared AGENT_API_KEY (if
            # configured) authenticates this agent to the orchestrator — the
            # orchestrator refuses keyless registrations outside dev mode
            # (028 FR-016 fail-closed).
            register_msg = RegisterAgent(agent_card=self.card,
                                         api_key=os.getenv("AGENT_API_KEY") or None)
            await websocket.send_text(register_msg.to_json())
            self._logger.info(f"Sent RegisterAgent with {len(self.card.skills)} skills")

            async for message in websocket.iter_text():
                try:
                    parsed = Message.from_json(message)
                    if isinstance(parsed, MCPRequest):
                        await self.handle_mcp_request(websocket, parsed)
                    elif isinstance(parsed, ToolStreamCancel):
                        # 001-tool-stream-ui: orchestrator wants us to stop a
                        # streaming generator. Cancel the task; the generator's
                        # `finally` block runs to free upstream subscriptions.
                        await self._handle_stream_cancel(parsed)
                    elif parsed.type == "agent_hop_response":
                        # 056 US1: outcome of a mediated hop this agent's
                        # runtime requested via call_agent_tool — resolve the
                        # awaiting future registered on this socket.
                        self._resolve_hop_response(websocket, parsed)
                except Exception as e:
                    self._logger.error(f"Error processing message: {e}")
                    # A malformed MCP request must fail its caller promptly.
                    # Merely logging here used to leave the orchestrator's
                    # pending future alive until the 30-second timeout.
                    try:
                        raw = json.loads(message)
                    except (TypeError, json.JSONDecodeError):
                        raw = None
                    if (
                        isinstance(raw, dict)
                        and raw.get("type") == "mcp_request"
                        and isinstance(raw.get("request_id"), str)
                    ):
                        protocol_error = (
                            e.to_error()
                            if isinstance(e, MCPProtocolError)
                            else {
                                "code": MCP_INVALID_REQUEST,
                                "message": "Malformed MCP request",
                                "retryable": False,
                            }
                        )
                        await websocket.send_text(
                            MCPResponse(
                                request_id=raw["request_id"],
                                error=protocol_error,
                                responder_info={
                                    "name": self.agent_id,
                                    "version": self.card.version,
                                },
                            ).to_json()
                        )

        except WebSocketDisconnect:
            self._logger.info("Connection disconnected")
        finally:
            self.orchestrator_connections.discard(websocket)

    async def handle_mcp_request(self, ws: WebSocket, msg: MCPRequest):
        """Handle MCP request by dispatching to MCP server.

        If the orchestrator sent E2E-encrypted credentials, decrypt them
        transparently before tool dispatch so individual tools see plaintext.

        001-tool-stream-ui: If ``params._stream`` is True AND the named tool
        is a streaming tool (decorated with ``@streaming_tool``) AND the
        ``tool_streaming`` feature flag is enabled, dispatch to the streaming
        path instead of the single-response path. The streaming path emits
        ``ToolStreamData`` chunks until the generator returns or is
        cancelled, then sends ``ToolStreamEnd``.

        015-external-ai-agents: For ``tools/call`` we inject an
        :class:`AgentRuntime` instance into ``arguments["_runtime"]`` so that
        tools needing background work (long-running upstream jobs) can call
        ``runtime.start_long_running_job(poll_fn)`` from synchronous code.
        Tools that don't accept ``_runtime`` continue to ignore it (the MCP
        server filters kwargs by signature).
        """
        self._logger.info(f"Processing MCP Request: {msg.method}")
        try:
            msg.validate_protocol_metadata(allow_legacy=True)
        except MCPProtocolError as exc:
            await ws.send_text(
                MCPResponse(
                    request_id=msg.request_id,
                    error=exc.to_error(),
                    responder_info={
                        "name": self.agent_id,
                        "version": self.card.version,
                    },
                ).to_json()
            )
            return
        self._decrypt_credentials_if_needed(msg)
        if msg.method == "tools/call" and msg.params is not None:
            from shared.agent_runtime import AgentRuntime
            args = msg.params.setdefault("arguments", {})
            args["_runtime"] = AgentRuntime(
                ws=ws,
                msg=msg,
                agent_id=self.agent_id,
                loop=asyncio.get_running_loop(),
            )

        # --- Streaming dispatch (001-tool-stream-ui) ---
        if (
            flags.is_enabled("tool_streaming")
            and msg.method == "tools/call"
            and msg.params.get("_stream") is True
        ):
            tool_name = msg.params.get("name", "")
            tool_info = self.mcp_server.tools.get(tool_name) if hasattr(self.mcp_server, "tools") else None
            tool_fn = tool_info.get("function") if tool_info else None
            if tool_fn is not None and is_streaming_tool(tool_fn):
                # Run the stream as a detached task so the agent's WebSocket
                # message loop stays free to accept OTHER tool calls and
                # ToolStreamCancel messages while this generator keeps
                # emitting. Awaiting inline would deadlock the agent for the
                # duration of the stream (which is often unbounded, e.g.
                # `live_system_metrics`'s `while True`). _handle_streaming_request
                # registers itself in self._active_streams for cancellation.
                # We keep a strong reference in _stream_wrapper_tasks because
                # asyncio only weak-refs create_task results and the GC would
                # otherwise drop this task before its inner runner registers.
                task = asyncio.create_task(self._handle_streaming_request(ws, msg, tool_fn))
                self._stream_wrapper_tasks.add(task)
                task.add_done_callback(self._stream_wrapper_tasks.discard)
                return
            # Fallthrough: tool exists but isn't a streaming tool — run as
            # one-shot. The orchestrator should not have set _stream=True
            # for a non-streaming tool, but defense-in-depth helps debugging.

        # --- Existing single-response path (unchanged) ---
        response = await asyncio.to_thread(self.mcp_server.process_request, msg)
        response.responder_info = {
            "name": self.agent_id,
            "version": self.card.version,
        }
        try:
            response.validate_result_shape()
        except Exception as exc:
            self._logger.error("Agent produced an invalid MCP response: %s", exc)
            response = MCPResponse(
                request_id=msg.request_id,
                error={
                    "code": -32603,
                    "message": "Agent produced an invalid MCP response",
                    "retryable": False,
                },
                responder_info={
                    "name": self.agent_id,
                    "version": self.card.version,
                },
            )
        await ws.send_text(response.to_json())
        self._logger.info(f"Sent response for {msg.request_id}")

    # =========================================================================
    # Streaming dispatch (001-tool-stream-ui)
    # =========================================================================

    async def _handle_streaming_request(
        self,
        ws: WebSocket,
        msg: MCPRequest,
        tool_fn: Any,
    ) -> None:
        """Drive a streaming tool to completion (or cancellation), emitting
        ``ToolStreamData`` chunks per yield/emit and a final ``ToolStreamEnd``
        on natural completion.

        Two paths depending on the tool form:

        - **Async generator** (``inspect.isasyncgenfunction(tool_fn)``): the
          wrapper iterates ``async for chunk in tool_fn(args, credentials):``
          and emits each chunk.
        - **StreamCtx form** (``async def`` taking a ``ctx: StreamCtx``): the
          wrapper constructs a ``StreamCtx``, schedules the tool function as
          a task, and drains the ctx queue concurrently.

        Errors raised by the tool become a final ``ToolStreamData`` chunk
        with ``error.code="tool_error"``, ``error.phase="failed"``,
        ``terminal: true``. The orchestrator's ``_classify_error`` then
        decides whether to auto-retry the stream (FR-021a).
        """
        request_id = msg.request_id
        stream_id = msg.params.get("_stream_id") or f"stream-{uuid.uuid4().hex[:12]}"
        tool_name = msg.params.get("name", "")
        agent_id = self.agent_id
        arguments = dict(msg.params.get("arguments", {}))
        credentials = arguments.pop("_credentials", {}) if "_credentials" in arguments else {}
        meta = get_stream_metadata(tool_fn) or {}
        max_chunk_bytes = (
            meta.get("metadata", {}).get("max_chunk_bytes", 65536)
        )
        uses_ctx = bool(meta.get("uses_ctx"))

        seq = 0

        async def _emit(chunk: StreamComponents) -> None:
            """Validate, assign id, send one ToolStreamData."""
            nonlocal seq
            try:
                validate_chunk_size(chunk, max_chunk_bytes)
            except StreamPayloadError as e:
                await _emit_error("chunk_too_large", str(e), terminal=True)
                raise
            seq += 1
            components_with_id = assign_stream_id_to_components(
                chunk.components, stream_id
            )
            data_msg = ToolStreamData(
                request_id=request_id,
                stream_id=stream_id,
                agent_id=agent_id,
                tool_name=tool_name,
                seq=seq,
                components=components_with_id,
                raw=chunk.raw,
                terminal=bool(chunk.terminal),
                error=chunk.error,
            )
            await ws.send_text(data_msg.to_json())

        async def _emit_error(code: str, message: str, terminal: bool = True) -> None:
            """Send a single error chunk and (optionally) an end marker."""
            nonlocal seq
            seq += 1
            err_msg = ToolStreamData(
                request_id=request_id,
                stream_id=stream_id,
                agent_id=agent_id,
                tool_name=tool_name,
                seq=seq,
                components=[],
                raw=None,
                terminal=terminal,
                error={
                    "code": code,
                    "message": message,
                    "phase": "failed",
                    "retryable": False,
                },
            )
            await ws.send_text(err_msg.to_json())

        ctx: Optional[StreamCtx] = None

        async def _runner() -> None:
            """The actual stream-driving coroutine."""
            try:
                if inspect.isasyncgenfunction(tool_fn):
                    # Async generator form
                    agen = tool_fn(arguments, credentials)
                    try:
                        async for payload in agen:
                            if not isinstance(payload, StreamComponents):
                                raise StreamPayloadError(
                                    f"streaming tool {tool_name!r} yielded "
                                    f"a {type(payload).__name__}, expected "
                                    f"StreamComponents"
                                )
                            await _emit(payload)
                            if payload.terminal:
                                return
                    finally:
                        # Closing the generator runs the tool's `finally`
                        # block (e.g. closing upstream subscriptions).
                        try:
                            await agen.aclose()
                        except Exception:  # pragma: no cover
                            pass
                elif uses_ctx:
                    # StreamCtx form: ctx is constructed in the outer scope
                    # so the cancel handler can call ctx._cancel()
                    nonlocal ctx
                    ctx = StreamCtx(stream_id=stream_id)
                    self._active_streams[stream_id] = (
                        asyncio.current_task(), ctx
                    )
                    # Run the tool function as a child task
                    tool_task = asyncio.create_task(
                        tool_fn(arguments, credentials, ctx)
                    )
                    try:
                        # Drain the queue until the tool completes or
                        # cancellation arrives.
                        while not tool_task.done():
                            drain = asyncio.create_task(ctx._drain())
                            done, _ = await asyncio.wait(
                                {drain, tool_task},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if drain in done:
                                payload = drain.result()
                                if payload is None:
                                    break  # cancelled
                                await _emit(payload)
                            else:
                                drain.cancel()
                                try:
                                    await drain
                                except (asyncio.CancelledError, Exception):
                                    pass
                    finally:
                        if not tool_task.done():
                            tool_task.cancel()
                            try:
                                await tool_task
                            except (asyncio.CancelledError, Exception):
                                pass
                else:
                    # Marker says streaming but signature is neither — bug.
                    raise StreamPayloadError(
                        f"streaming tool {tool_name!r} is neither an async "
                        f"generator nor a StreamCtx-style coroutine"
                    )

                # Natural completion → ToolStreamEnd
                end_msg = ToolStreamEnd(
                    request_id=request_id,
                    stream_id=stream_id,
                )
                await ws.send_text(end_msg.to_json())
                self._logger.info(
                    f"Stream {stream_id} ({tool_name}) completed naturally "
                    f"after {seq} chunks"
                )

            except asyncio.CancelledError:
                # ToolStreamCancel arrived; let the agen.aclose finally run
                # via re-raise. Send a terminal cancellation chunk so the
                # orchestrator knows we're done.
                self._logger.info(
                    f"Stream {stream_id} ({tool_name}) cancelled at seq={seq}"
                )
                try:
                    await _emit_error("cancelled", "stream cancelled", terminal=True)
                except Exception:
                    pass
                raise
            except StreamPayloadError as e:
                self._logger.warning(
                    f"Stream {stream_id} ({tool_name}) payload error: {e}"
                )
                # _emit_error already sent if it was a chunk_too_large; for
                # other payload errors send tool_error.
                if "chunk_too_large" not in str(e):
                    try:
                        await _emit_error("tool_error", str(e), terminal=True)
                    except Exception:
                        pass
            except Exception as e:
                self._logger.error(
                    f"Stream {stream_id} ({tool_name}) raised "
                    f"{type(e).__name__}: {e}"
                )
                try:
                    await _emit_error("tool_error", str(e), terminal=True)
                except Exception:
                    pass

        # Register the task BEFORE starting it so a fast cancellation
        # can find it. The StreamCtx case re-registers with ctx populated.
        runner_task = asyncio.create_task(_runner())
        self._active_streams[stream_id] = (runner_task, None)
        try:
            await runner_task
        finally:
            self._active_streams.pop(stream_id, None)

    async def _handle_stream_cancel(self, msg: ToolStreamCancel) -> None:
        """Handle an inbound ToolStreamCancel from the orchestrator.

        Looks up the in-flight task by ``stream_id``, signals the StreamCtx
        if present (for graceful queue drain wakeup), then cancels the task.
        The task's `finally` block runs to free upstream subscriptions.
        Returns immediately; cleanup is asynchronous but bounded by the 1 s
        budget in contracts/protocol-messages.md §B3.
        """
        entry = self._active_streams.get(msg.stream_id)
        if entry is None:
            self._logger.debug(
                f"ToolStreamCancel for unknown stream_id {msg.stream_id}"
            )
            return
        task, ctx = entry
        if ctx is not None:
            ctx._cancel()
        if not task.done():
            task.cancel()
        self._logger.info(f"ToolStreamCancel processed for {msg.stream_id}")

    def _decrypt_credentials_if_needed(self, msg: MCPRequest):
        """Decrypt E2E-encrypted credentials in-place before tool dispatch.

        If a saved credential cannot be decrypted (typically because the
        agent's private key was regenerated since the credential was
        saved), set ``_credentials_stale=True`` so tool code can surface
        a friendly "please re-save" message instead of the generic "not
        configured" one — the credentials *are* in the DB, they're just
        unreadable by this agent process.
        """
        args = msg.params.get("arguments") if msg.params else None
        if not args or not args.get("_credentials_encrypted"):
            return

        encrypted_creds = args.get("_credentials", {})
        plaintext_creds = {}
        had_decrypt_failure = False
        for key, value in encrypted_creds.items():
            try:
                if is_e2e_encrypted(value):
                    plaintext_creds[key] = self._decrypt_with_fallbacks(value)
                else:
                    # Legacy Fernet value — agent cannot decrypt, pass through as-is
                    self._logger.warning(f"Credential '{key}' is not E2E-encrypted, skipping")
                    plaintext_creds[key] = value
            except Exception as e:
                self._logger.error(f"Failed to decrypt credential '{key}': {e}")
                had_decrypt_failure = True

        args["_credentials"] = plaintext_creds
        if had_decrypt_failure:
            args["_credentials_stale"] = True
        args.pop("_credentials_encrypted", None)

    def _decrypt_with_fallbacks(self, value: str) -> str:
        """Decrypt an E2E blob with this agent's key, then any predecessor keys.

        Consolidated agents (feature 029) declare ``predecessor_agent_ids``;
        credentials encrypted to a predecessor's key remain readable without
        a re-save. Raises when no loaded key decrypts the blob.
        """
        try:
            return decrypt_from_orchestrator(value, self._private_key)
        except Exception:
            for fallback in getattr(self, "_fallback_private_keys", []) or []:
                try:
                    return decrypt_from_orchestrator(value, fallback)
                except Exception:
                    continue
            raise

    # =========================================================================
    # Agent-to-Agent Communication (mediated only — feature 056)
    # =========================================================================
    # The direct peer-call path (connect_to_peer / _peer_listen_loop /
    # call_peer_tool / _call_peer_via_ws / _call_peer_via_a2a and the peer
    # connection registry) was RETIRED by feature 056 (FR-010, D12): it
    # forwarded the caller's delegation token UNATTENUATED to the peer — a
    # confused-deputy seam — and bypassed the orchestrator's gate stack
    # entirely. The sanctioned replacement is the ORCHESTRATOR-MEDIATED hop:
    # a tool calls ``kwargs["_runtime"].call_agent_tool(...)``, which routes
    # an agent_hop_request control frame to the orchestrator; the hop runs
    # under a freshly minted, strictly-narrower child delegation through the
    # full single-path gate stack. Agents cannot open peer transports.

    def _resolve_hop_response(self, websocket, parsed) -> None:
        """Resolve a mediated hop's awaiting future (056 US1).

        The orchestrator delivers an ``agent_hop_response`` frame over this
        agent's own control socket; the future was registered on that socket
        by ``AgentRuntime.call_agent_tool``. Unknown/settled hop ids are
        logged and dropped (the awaiter has its own timeout)."""
        futures = getattr(websocket, "_hop_futures", None)
        fut = futures.pop(parsed.request_id, None) if isinstance(futures, dict) else None
        if fut is None or fut.done():
            self._logger.warning("hop response for unknown/settled hop %s", parsed.request_id)
            return
        r = parsed.response or {}
        hop_error = r.get("error")
        fut.set_result(MCPResponse(
            request_id=parsed.request_id,
            result=None if hop_error is not None else r.get("result"),
            error=hop_error,
            ui_components=None if hop_error is not None else r.get("ui_components"),
            result_type=r.get("result_type", "complete"),
            responder_info=r.get("responder_info"),
        ))

    # =========================================================================
    # Server Setup & Run
    # =========================================================================

    def _setup_a2a_routes(self, app: FastAPI):
        """Mount A2A JSON-RPC and agent-card routes on the FastAPI app."""
        try:
            from a2a.server.request_handlers import DefaultRequestHandler
            from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
            from a2a.server.routes import (
                create_jsonrpc_routes, create_agent_card_routes,
            )

            a2a_card = self._build_a2a_card()
            executor = MCPAgentExecutor(self.mcp_server, self._security_validator, private_key=self._private_key)
            handler = DefaultRequestHandler(
                agent_executor=executor,
                task_store=InMemoryTaskStore(),
                agent_card=a2a_card,
            )

            # /.well-known/agent-card.json is served by the agent's own
            # FastAPI route below; expose the v1.0 SDK routes under /a2a.
            for route in create_jsonrpc_routes(handler, rpc_url="/a2a", enable_v0_3_compat=True):
                app.router.routes.append(route)
            for route in create_agent_card_routes(a2a_card, card_url="/a2a/.well-known/agent-card.json"):
                app.router.routes.append(route)

            self._logger.info("A2A JSON-RPC endpoint mounted at /a2a (v0.3 compat enabled)")

        except Exception as e:
            self._logger.warning(f"A2A setup failed (SDK may not be installed): {e}")

    async def run(self):
        """Run the FastAPI server with both WebSocket and A2A endpoints."""
        app = FastAPI(title=f"Agent: {self.service_name}")

        # Suppress noisy access logs
        logging.getLogger("uvicorn.access").addFilter(EndpointFilter())

        # Legacy A2A Agent Card endpoint (for existing orchestrator)
        @app.get("/.well-known/agent-card.json")
        async def get_agent_card():
            return self.card.to_dict()

        # Health check
        @app.get("/health")
        async def health_check():
            return {
                "status": "ok",
                "agent_id": self.agent_id,
                "tools": len(self.mcp_server.tools),
                "a2a_compliant": True,
            }

        # WebSocket for orchestrator/peer communication
        app.add_api_websocket_route("/agent", self.handle_websocket)

        # Mount A2A JSON-RPC endpoint
        self._setup_a2a_routes(app)

        self._logger.info(f"Starting {self.service_name} on http://{self.host}:{self.port}")
        self._logger.info(f"Legacy Card: http://localhost:{self.port}/.well-known/agent-card.json")
        self._logger.info(f"A2A Card:    http://localhost:{self.port}/a2a/.well-known/agent-card.json")
        self._logger.info(f"A2A RPC:     http://localhost:{self.port}/a2a/")
        self._logger.info(f"WebSocket:   ws://localhost:{self.port}/agent")
        self._logger.info(f"Registered tools: {list(self.mcp_server.tools.keys())}")

        config = uvicorn.Config(
            app, host=self.host, port=self.port,
            log_level="info", ws_max_size=50 * 1024 * 1024,
        )
        server = uvicorn.Server(config)
        await server.serve()
