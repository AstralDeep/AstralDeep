"""
A2A Executor — Bridges the a2a-sdk AgentExecutor interface to MCP tool dispatch.

Wraps any agent's MCPServer so that incoming A2A JSON-RPC requests are
converted to MCPRequests, dispatched through the existing tool registry,
and results are published back as A2A events.
"""
import asyncio
import logging
import os
import uuid
from typing import Optional

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import (
    Message as A2AMessage,
    Role,
)

from shared.a2a_bridge import (
    a2a_message_to_mcp_request,
    mcp_response_to_a2a_message,
    extract_text_from_a2a_message,
    make_text_part,
    make_data_part,
)
from shared.a2a_security import A2ASecurityValidator
from shared.crypto import decrypt_from_orchestrator, is_e2e_encrypted

logger = logging.getLogger("MCPAgentExecutor")


class MCPAgentExecutor(AgentExecutor):
    """Bridges A2A AgentExecutor to existing MCP tool dispatch.

    For each incoming A2A message:
    1. Optionally validates the Bearer token (if security_validator provided)
    2. Extracts tool name + arguments from a data Part
    3. Dispatches via the agent's MCPServer.process_request()
    4. Converts the MCPResponse to A2A events and publishes them
    """

    def __init__(
        self,
        mcp_server,
        security_validator: Optional[A2ASecurityValidator] = None,
        private_key=None,
        protected_request_verifier=None,
    ):
        """
        Args:
            mcp_server: The agent's MCPServer instance (has .process_request() and .tools).
            security_validator: Optional A2ASecurityValidator for token validation.
            private_key: Optional EC private key for E2E credential decryption.
        """
        self.mcp_server = mcp_server
        self.security_validator = security_validator or A2ASecurityValidator()
        self._private_key = private_key
        self._protected_request_verifier = protected_request_verifier

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Execute an incoming A2A request by dispatching to MCP tools."""
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)

        try:
            await updater.start_work()

            message = context.message
            if not message:
                await updater.failed(
                    message=self._error_message("No message provided in request", context.task_id)
                )
                return

            # Try to extract a tool call from the message
            mcp_request = a2a_message_to_mcp_request(message)

            if mcp_request:
                if await self._validated_bearer_claims(context) is None:
                    raise PermissionError("a2a_authentication_failed")
                mcp_request.validate_protocol_metadata(allow_legacy=True)
                protected_wire_arguments = dict(
                    (mcp_request.params.get("arguments", {}) or {})
                )
                # Preserve the exact host-authorized wire mapping across
                # trusted executor-local credential decryption and signature
                # filtering. Generated MCP servers claim at their final
                # actuator seam and read this process-local snapshot there.
                mcp_request._protected_wire_arguments = protected_wire_arguments
                # Decrypt E2E credentials if present
                self._decrypt_credentials_if_needed(mcp_request)
                caller_capabilities = mcp_request.caller_capabilities
                protected_metadata_present = bool(
                    isinstance(caller_capabilities, dict)
                    and "astraldeep.lets/v1" in caller_capabilities
                )
                protected_executor_required = (
                    os.getenv("LETS_MODE", "off").strip().lower() == "enforce"
                    and os.getenv("ASTRAL_RUNTIME_COHORT", "").strip()
                    in {"server_dynamic", "byo_user"}
                )
                if self._protected_request_verifier is not None and (
                    protected_metadata_present or protected_executor_required
                ):
                    self._protected_request_verifier(
                        mcp_request,
                        final_wire_arguments=protected_wire_arguments,
                    )
                # Tool call: dispatch via MCP server
                await self._execute_tool_call(mcp_request, updater, context)
            else:
                # Natural language or tools/list: return capabilities
                text = extract_text_from_a2a_message(message)
                if text.strip().lower() in ("list tools", "list_tools", "help", "capabilities"):
                    await self._list_tools(updater, context)
                else:
                    await self._list_tools(updater, context)

        except Exception as e:
            logger.error(f"A2A execute error: {e}", exc_info=True)
            await updater.failed(
                message=self._error_message(str(e), context.task_id)
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Cancel an ongoing task."""
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()

    async def _validated_bearer_claims(self, context: RequestContext):
        """Validate the HTTP bearer exposed by the A2A SDK call context."""

        call_context = getattr(context, "call_context", None)
        state = getattr(call_context, "state", {}) or {}
        headers = state.get("headers", {}) if isinstance(state, dict) else {}
        authorization = ""
        if isinstance(headers, dict):
            authorization = next(
                (
                    str(value)
                    for key, value in headers.items()
                    if str(key).lower() == "authorization"
                ),
                "",
            )
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token.strip():
            return None
        claims = await self.security_validator.validate_token(token.strip())
        if not isinstance(claims, dict) or not isinstance(claims.get("sub"), str):
            return None
        return claims

    async def _execute_tool_call(self, mcp_request, updater: TaskUpdater, context: RequestContext):
        """Dispatch an MCP tool call and publish results."""

        tool_name = mcp_request.params.get("name", "unknown")
        logger.info(f"A2A dispatching tool call: {tool_name}")

        # Run synchronous tool call in thread pool
        response = await asyncio.to_thread(self.mcp_server.process_request, mcp_request)

        if response.error:
            error_msg = response.error.get("message", "Tool execution failed") if isinstance(response.error, dict) else str(response.error)
            parts = [make_text_part(f"Error: {error_msg}")]
            if response.ui_components:
                parts.append(make_data_part(
                    {"_ui_components": response.ui_components},
                    metadata={"type": "ui_components"},
                ))
            msg = A2AMessage(
                message_id=str(uuid.uuid4()),
                role=Role.ROLE_AGENT,
                parts=parts,
                task_id=context.task_id,
            )
            await updater.failed(message=msg)
        else:
            # Build result message
            result_msg = mcp_response_to_a2a_message(response, context.task_id)
            await updater.complete(message=result_msg)

    async def _list_tools(self, updater: TaskUpdater, context: RequestContext):
        """Return the list of available tools as an A2A message."""
        tool_list = self.mcp_server.get_tool_list()
        parts = [
            make_data_part(
                {"tools": tool_list, "method": "tools/list"},
                metadata={"type": "tool_list"},
            )
        ]
        msg = A2AMessage(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_AGENT,
            parts=parts,
            task_id=context.task_id,
        )
        await updater.complete(message=msg)

    def _decrypt_credentials_if_needed(self, mcp_request):
        """Decrypt E2E-encrypted credentials in-place before tool dispatch."""
        if not self._private_key:
            return
        args = mcp_request.params.get("arguments") if mcp_request.params else None
        if not args or not args.get("_credentials_encrypted"):
            return

        encrypted_creds = args.get("_credentials", {})
        plaintext_creds = {}
        for key, value in encrypted_creds.items():
            try:
                if is_e2e_encrypted(value):
                    plaintext_creds[key] = decrypt_from_orchestrator(value, self._private_key)
                else:
                    logger.warning(f"Credential '{key}' is not E2E-encrypted, skipping")
                    plaintext_creds[key] = value
            except Exception as e:
                logger.error(f"Failed to decrypt credential '{key}': {e}")

        args["_credentials"] = plaintext_creds
        args.pop("_credentials_encrypted", None)

    @staticmethod
    def _error_message(error_text: str, task_id: str) -> A2AMessage:
        """Create a simple error A2A Message."""
        return A2AMessage(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_AGENT,
            parts=[make_text_part(f"Error: {error_text}")],
            task_id=task_id,
        )
