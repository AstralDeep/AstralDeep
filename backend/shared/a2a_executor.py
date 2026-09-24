"""Adapts any agent's MCPServer to the a2a-sdk AgentExecutor interface: converts A2A
JSON-RPC requests to MCPRequests via shared/a2a_bridge.py, dispatches through the
tool registry, and publishes results as A2A events.
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
    ensure_task_created,
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
    def __init__(
        self,
        mcp_server,
        security_validator: Optional[A2ASecurityValidator] = None,
        private_key=None,
        protected_request_verifier=None,
    ):
        self.mcp_server = mcp_server
        self.security_validator = security_validator or A2ASecurityValidator()
        self._private_key = private_key
        self._protected_request_verifier = protected_request_verifier

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)

        try:
            await ensure_task_created(context, event_queue)
            await updater.start_work()

            message = context.message
            if not message:
                await updater.failed(
                    message=self._error_message("No message provided in request", context.task_id)
                )
                return

            mcp_request = a2a_message_to_mcp_request(message)

            if mcp_request:
                if await self._validated_bearer_claims(context) is None:
                    raise PermissionError("a2a_authentication_failed")
                mcp_request.validate_protocol_metadata(allow_legacy=True)
                protected_wire_arguments = dict(
                    (mcp_request.params.get("arguments", {}) or {})
                )
                mcp_request._protected_wire_arguments = protected_wire_arguments
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
                await self._execute_tool_call(mcp_request, updater, context)
            else:
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
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()

    async def _validated_bearer_claims(self, context: RequestContext):
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
        tool_name = mcp_request.params.get("name", "unknown")
        logger.info(f"A2A dispatching tool call: {tool_name}")

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
            result_msg = mcp_response_to_a2a_message(response, context.task_id)
            await updater.complete(message=result_msg)

    async def _list_tools(self, updater: TaskUpdater, context: RequestContext):
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
        return A2AMessage(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_AGENT,
            parts=[make_text_part(f"Error: {error_text}")],
            task_id=task_id,
        )
