"""Base class every AstralDeep agent subclasses for dual WebSocket + A2A transport:
builds the agent card, decrypts E2E credentials, drives streaming tools, and mediates
agent-to-agent hops via AgentRuntime.call_agent_tool.
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
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "/.well-known/agent-card.json" not in msg and "/.well-known/agent.json" not in msg


def find_available_port(start_port: int = BASE_PORT, max_offset: int = MAX_PORT_OFFSET) -> int:
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
    agent_id: str = ""
    service_name: str = ""
    description: str = ""
    skill_tags: List[str] = []
    card_metadata: Dict[str, Any] = {}
    examples: List[Dict[str, str]] = []

    def __init__(self, mcp_server, port: int = None, port_env_var: str = None, default_port_offset: int = 0):
        self.host = os.getenv("HOST", "0.0.0.0")
        self.mcp_server = mcp_server
        self.orchestrator_connections: Set[WebSocket] = set()

        if port is not None:
            self.port = port
        elif port_env_var and os.getenv(port_env_var):
            self.port = int(os.getenv(port_env_var))
        else:
            self.port = find_available_port(BASE_PORT)

        self._init_crypto()

        self.card = self._build_agent_card()

        self._security_validator = A2ASecurityValidator()

        self._active_streams: Dict[str, "tuple[asyncio.Task, Optional[StreamCtx]]"] = {}

        # Pinned here — asyncio only weak-refs create_task results
        self._stream_wrapper_tasks: set = set()

        self._lets_executor_runtime = None
        self._lets_executor_initialization_error: str | None = None

        self._logger = logging.getLogger(self.__class__.__name__)

    def _init_crypto(self):
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
        skills = []
        for name, info in self.mcp_server.tools.items():
            desc = info.get("description", "No description provided")
            tags = list(self.skill_tags) if self.skill_tags else []
            skill_metadata = {}
            if "streamable" in info:
                skill_metadata["streamable"] = info["streamable"]
                if isinstance(info["streamable"], dict):
                    skill_metadata.setdefault("streaming_kind", "poll")
            tool_metadata = info.get("metadata")
            if isinstance(tool_metadata, dict):
                skill_metadata.update(tool_metadata)
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
        declared = getattr(self, "examples", None) or []
        if declared:
            metadata["examples"] = [
                {"title": str(e.get("title", "")), "prompt": str(e.get("prompt", ""))}
                for e in declared
                if isinstance(e, dict) and e.get("prompt")
            ]

        return AgentCard(
            name=self.service_name,
            description=self.description,
            agent_id=self.agent_id,
            version="1.0.0",
            skills=skills,
            metadata=metadata,
        )

    def _build_a2a_card(self):
        base_url = f"http://{self.host}:{self.port}"
        return custom_card_to_a2a(self.card, base_url)

    async def handle_websocket(self, websocket: WebSocket):
        await websocket.accept()
        self._logger.info("Connection established via WebSocket")
        self.orchestrator_connections.add(websocket)

        try:
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
                        await self._handle_stream_cancel(parsed)
                    elif parsed.type == "agent_hop_response":
                        self._resolve_hop_response(websocket, parsed)
                except Exception as e:
                    self._logger.error(f"Error processing message: {e}")
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
        protected_tool_arguments = dict(
            (msg.params.get("arguments", {}) if msg.params else {}) or {}
        )
        protected_wire_arguments = protected_tool_arguments
        if msg.params and msg.params.get("_stream") is True:
            protected_wire_arguments = {
                "arguments": protected_tool_arguments,
                "_stream": True,
                "_stream_id": msg.params.get("_stream_id"),
            }
        msg._protected_wire_arguments = protected_wire_arguments
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

        server_claims_at_actuator = (
            getattr(self.mcp_server, "protected_executor_at_actuator", False)
            is True
        )
        caller_capabilities = msg.caller_capabilities
        protected_metadata_present = bool(
            isinstance(caller_capabilities, dict)
            and "astraldeep.lets/v1" in caller_capabilities
        )
        protected_executor_required = (
            os.getenv("LETS_MODE", "off").strip().lower() == "enforce"
            and os.getenv("ASTRAL_RUNTIME_COHORT", "").strip()
            in {"server_dynamic", "byo_user"}
        )
        streaming_actuator = False
        if (
            server_claims_at_actuator
            and flags.is_enabled("tool_streaming")
            and msg.method == "tools/call"
            and msg.params.get("_stream") is True
        ):
            candidate_name = msg.params.get("name", "")
            candidate_info = (
                self.mcp_server.tools.get(candidate_name)
                if hasattr(self.mcp_server, "tools")
                else None
            )
            candidate_fn = candidate_info.get("function") if candidate_info else None
            streaming_actuator = bool(
                candidate_fn is not None and is_streaming_tool(candidate_fn)
            )
        try:
            if (
                protected_metadata_present or protected_executor_required
            ) and (not server_claims_at_actuator or streaming_actuator):
                self._verify_and_claim_protected_request(
                    msg,
                    final_wire_arguments=protected_wire_arguments,
                )
        except Exception as exc:
            from orchestrator.lets_gateway import LetsGatewayError

            code = exc.code if isinstance(exc, LetsGatewayError) else "protected_executor_failed"
            retryable = bool(
                isinstance(exc, LetsGatewayError) and exc.retryable
            )
            self._logger.warning(
                "Protected executor refused request %s: %s",
                msg.request_id,
                code,
            )
            await ws.send_text(
                MCPResponse(
                    request_id=msg.request_id,
                    error={
                        "code": code,
                        "message": "Protected tool authorization was refused",
                        "retryable": retryable,
                    },
                    responder_info={
                        "name": self.agent_id,
                        "version": self.card.version,
                    },
                ).to_json()
            )
            return

        if (
            flags.is_enabled("tool_streaming")
            and msg.method == "tools/call"
            and msg.params.get("_stream") is True
        ):
            tool_name = msg.params.get("name", "")
            tool_info = self.mcp_server.tools.get(tool_name) if hasattr(self.mcp_server, "tools") else None
            tool_fn = tool_info.get("function") if tool_info else None
            if tool_fn is not None and is_streaming_tool(tool_fn):
                # Awaiting here would deadlock unbounded streams
                task = asyncio.create_task(self._handle_streaming_request(ws, msg, tool_fn))
                self._stream_wrapper_tasks.add(task)
                task.add_done_callback(self._stream_wrapper_tasks.discard)
                return

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

    async def _handle_streaming_request(
        self,
        ws: WebSocket,
        msg: MCPRequest,
        tool_fn: Any,
    ) -> None:
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
            try:
                if inspect.isasyncgenfunction(tool_fn):
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
                        try:
                            await agen.aclose()
                        except Exception:  # pragma: no cover
                            pass
                elif uses_ctx:
                    nonlocal ctx
                    ctx = StreamCtx(stream_id=stream_id)
                    self._active_streams[stream_id] = (
                        asyncio.current_task(), ctx
                    )
                    tool_task = asyncio.create_task(
                        tool_fn(arguments, credentials, ctx)
                    )
                    try:
                        while not tool_task.done():
                            drain = asyncio.create_task(ctx._drain())
                            done, _ = await asyncio.wait(
                                {drain, tool_task},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if drain in done:
                                payload = drain.result()
                                if payload is None:
                                    break
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
                    raise StreamPayloadError(
                        f"streaming tool {tool_name!r} is neither an async "
                        f"generator nor a StreamCtx-style coroutine"
                    )

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

        runner_task = asyncio.create_task(_runner())
        self._active_streams[stream_id] = (runner_task, None)
        try:
            await runner_task
        finally:
            self._active_streams.pop(stream_id, None)

    async def _handle_stream_cancel(self, msg: ToolStreamCancel) -> None:
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
                    self._logger.warning(f"Credential '{key}' is not E2E-encrypted, skipping")
                    plaintext_creds[key] = value
            except Exception as e:
                self._logger.error(f"Failed to decrypt credential '{key}': {e}")
                had_decrypt_failure = True

        args["_credentials"] = plaintext_creds
        if had_decrypt_failure:
            args["_credentials_stale"] = True
        args.pop("_credentials_encrypted", None)

    def _verify_and_claim_protected_request(
        self,
        msg: MCPRequest,
        *,
        final_wire_arguments: Dict[str, Any],
    ) -> None:
        from orchestrator.lets_gateway import (
            LetsGatewayError,
            create_executor_gateway,
            extract_lets_metadata,
        )

        metadata = extract_lets_metadata(msg.caller_capabilities)
        runtime_cohort = os.getenv("ASTRAL_RUNTIME_COHORT", "").strip()
        mode = os.getenv("LETS_MODE", "off").strip().lower() or "off"
        required = (
            mode == "enforce"
            and runtime_cohort in {"server_dynamic", "byo_user"}
        )
        if metadata is None:
            if required:
                raise LetsGatewayError("missing_protected_permit")
            return
        if mode != "enforce":
            raise LetsGatewayError("unexpected_protected_permit")

        if self._lets_executor_runtime is None:
            if self._lets_executor_initialization_error is not None:
                raise LetsGatewayError(self._lets_executor_initialization_error)
            try:
                from orchestrator.lets_config import load_lets_config

                loaded = load_lets_config()
                if loaded.config is None or loaded.config.mode != "enforce":
                    raise LetsGatewayError("executor_not_configured")
                self._lets_executor_runtime = create_executor_gateway(loaded.config)
            except LetsGatewayError as exc:
                self._lets_executor_initialization_error = exc.code
                raise
            except Exception:
                self._lets_executor_initialization_error = "executor_initialization_failed"
                raise LetsGatewayError("executor_initialization_failed") from None

        owner_id = os.getenv("ASTRAL_AUTHORITY_OWNER_ID", "").strip()
        binding_id = os.getenv("ASTRAL_AUTHORITY_BINDING_ID", "").strip()
        lease_id = os.getenv("ASTRAL_AUTHORITY_LEASE_ID", "").strip()
        lineage_id = os.getenv("ASTRAL_AUTHORITY_LINEAGE_ID", "").strip()
        runtime_id = os.getenv("ASTRAL_RUNTIME_ID", "").strip()
        generation_raw = os.getenv("ASTRAL_RUNTIME_GENERATION", "").strip()
        audience = os.getenv("LETS_EXECUTOR_INSTANCE_ID", "").strip()
        try:
            generation = int(generation_raw)
        except ValueError:
            generation = 0
        if (
            not owner_id
            or not binding_id
            or not lease_id
            or not lineage_id
            or not runtime_id
            or generation < 1
            or not audience
        ):
            raise LetsGatewayError("executor_host_context_unavailable")

        tool_id = str((msg.params or {}).get("name", ""))
        self._lets_executor_runtime.gateway.verify_and_claim(
            metadata=metadata,
            final_arguments=final_wire_arguments,
            owner_id=owner_id,
            binding_id=binding_id,
            lease_id=lease_id,
            lineage_id=lineage_id,
            agent_id=self.agent_id,
            runtime_id=runtime_id,
            runtime_generation=generation,
            tool_id=tool_id,
            executor_audience=audience,
        )

    def _decrypt_with_fallbacks(self, value: str) -> str:
        try:
            return decrypt_from_orchestrator(value, self._private_key)
        except Exception:
            for fallback in getattr(self, "_fallback_private_keys", []) or []:
                try:
                    return decrypt_from_orchestrator(value, fallback)
                except Exception:
                    continue
            raise

    def _resolve_hop_response(self, websocket, parsed) -> None:
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

    def _setup_a2a_routes(self, app: FastAPI):
        try:
            from a2a.server.request_handlers import DefaultRequestHandler
            from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
            from a2a.server.routes import (
                create_jsonrpc_routes, create_agent_card_routes,
            )

            a2a_card = self._build_a2a_card()
            executor = MCPAgentExecutor(
                self.mcp_server,
                self._security_validator,
                private_key=self._private_key,
                protected_request_verifier=(
                    None
                    if getattr(
                        self.mcp_server,
                        "protected_executor_at_actuator",
                        False,
                    )
                    is True
                    else self._verify_and_claim_protected_request
                ),
            )
            handler = DefaultRequestHandler(
                agent_executor=executor,
                task_store=InMemoryTaskStore(),
                agent_card=a2a_card,
            )

            for route in create_jsonrpc_routes(handler, rpc_url="/a2a", enable_v0_3_compat=True):
                app.router.routes.append(route)
            for route in create_agent_card_routes(a2a_card, card_url="/a2a/.well-known/agent-card.json"):
                app.router.routes.append(route)

            self._logger.info("A2A JSON-RPC endpoint mounted at /a2a (v0.3 compat enabled)")

        except Exception as e:
            self._logger.warning(f"A2A setup failed (SDK may not be installed): {e}")

    async def run(self):
        app = FastAPI(title=f"Agent: {self.service_name}")

        logging.getLogger("uvicorn.access").addFilter(EndpointFilter())

        @app.get("/.well-known/agent-card.json")
        async def get_agent_card():
            return self.card.to_dict()

        @app.get("/health")
        async def health_check():
            return {
                "status": "ok",
                "agent_id": self.agent_id,
                "tools": len(self.mcp_server.tools),
                "a2a_compliant": True,
            }

        app.add_api_websocket_route("/agent", self.handle_websocket)

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
