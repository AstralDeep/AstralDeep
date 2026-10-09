# -*- coding: utf-8 -*-
...
import typing as t
...
class Orchestrator:
    ...
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Existing initialisation …
        # -----------------------------------------------------------------
        # New attribute to keep track of a failure message for the current
        # operation.  It is cleared at the start of every UI‑driven operation
        # and, if populated, will be emitted as a terminal `operation_status`
        # with status ``failed``.  This allows idle clients (no active fence)
        # to still surface the reason for a refusal or error.
        self._operation_error_message: t.Optional[str] = None
        # -----------------------------------------------------------------

    # -----------------------------------------------------------------
    # Helper used by the various action handlers to record a failure.
    # -----------------------------------------------------------------
    def _set_operation_error(self, message: str) -> None:
        """Record a failure message for the current UI operation.

        The message will be propagated to the client as a terminal
        ``operation_status`` with ``status: "failed"`` when the operation
        finishes.  Only the first error is kept – subsequent calls are ignored
        because the first failure is the most relevant for the user.
        """
        if self._operation_error_message is None:
            self._operation_error_message = message

    # -----------------------------------------------------------------
    # UI operation entry point – reset the error holder for each request.
    # -----------------------------------------------------------------
    async def _run_connection_ui_operation(self, *args, **kwargs):
        """
        Entry point for UI‑driven operations (component actions, tool runs,
        etc.).  The original implementation bound the request generation to
        the socket scope but did not reset any error state.  We now clear the
        per‑operation error holder so that a failure in one request does not
        leak into the next.
        """
        # Reset any previous error message before handling a new operation.
        self._operation_error_message = None

        # Existing logic (unchanged) …
        async with self._scope_conversation_transient():
            # Original body of the method follows unchanged …
            ...

    # -----------------------------------------------------------------
    # Component action handling – record failures for all refusal paths.
    # -----------------------------------------------------------------
    async def _handle_component_action(self, request: dict, *args, **kwargs):
        """
        Handles a ``component_action`` request.  The original code emitted a
        transient alert for each refusal and then continued to the normal
        termination path, which resulted in ``chat_status: done`` without an
        ``operation_status``.  Idle clients therefore ignored the alert.
        We now capture the refusal reason and surface it as a failed
        ``operation_status``.
        """
        # Existing code up to the first refusal check …
        ...

        # Example refusal: unsupported action kind
        if action_kind not in SUPPORTED_KINDS:
            msg = f"Unsupported action kind: {action_kind}"
            await self._send_transient_alert(request, msg)
            self._set_operation_error(msg)
            # Early return – the operation is considered finished.
            return self._finalize_operation(request)

        # Example refusal: missing component context
        if not component_context:
            msg = "Missing component context for action."
            await self._send_transient_alert(request, msg)
            self._set_operation_error(msg)
            return self._finalize_operation(request)

        # Example refusal: read‑only history view
        if self._is_readonly_history(request):
            msg = "Cannot perform action in a read‑only history view."
            await self._send_transient_alert(request, msg)
            self._set_operation_error(msg)
            return self._finalize_operation(request)

        # Example refusal: component no longer available
        if not self._component_is_available(component_id):
            msg = f"Component {component_id} is no longer available."
            await self._send_transient_alert(request, msg)
            self._set_operation_error(msg)
            return self._finalize_operation(request)

        # Example refusal: no refreshable source
        if not self._has_refreshable_source(component_id):
            msg = f"Component {component_id} has no refreshable source."
            await self._send_transient_alert(request, msg)
            self._set_operation_error(msg)
            return self._finalize_operation(request)

        # Example refusal: retired agent
        if self._is_retired_agent(agent_id):
            msg = f"Agent {agent_id} has been retired."
            await self._send_transient_alert(request, msg)
            self._set_operation_error(msg)
            return self._finalize_operation(request)

        # Example refusal: permission denied
        if not self._has_permission(user_id, component_id, action_kind):
            msg = "Permission denied for this component action."
            await self._send_transient_alert(request, msg)
            self._set_operation_error(msg)
            return self._finalize_operation(request)

        # Example refusal: gate refusal
        if not self._gate_allows(request):
            msg = "Gate refused the operation."
            await self._send_transient_alert(request, msg)
            self._set_operation_error(msg)
            return self._finalize_operation(request)

        # Example refusal: tool error (caught later, but we also guard here)
        try:
            result = await self._run_tool_for_action(request)
        except ToolError as exc:
            msg = f"Tool error: {str(exc)}"
            await self._send_transient_alert(request, msg)
            self._set_operation_error(msg)
            return self._finalize_operation(request)

        # Example refusal: AI provider not configured
        if not self._ai_provider_configured(user_id):
            msg = "Set up your AI provider to use this."
            await self._send_transient_alert(request, msg)
            self._set_operation_error(msg)
            return self._finalize_operation(request)

        # -----------------------------------------------------------------
        # Normal successful path – unchanged from the original implementation.
        # -----------------------------------------------------------------
        ...

        # At the end of the successful path we still fall through to the
        # generic finalisation which will emit ``chat_status: done`` without an
        # error status (as before).
        return await self._finalize_operation(request)

    # -----------------------------------------------------------------
    # Unconfigured‑provider refusal – ensure the error is recorded.
    # -----------------------------------------------------------------
    async def _handle_unconfigured_provider(self, request: dict, *args, **kwargs):
        """
        Handles the case where the user has no AI provider configured.
        Previously this emitted a transient alert and then completed the
        operation silently.  We now record the failure so that idle clients
        display the message.
        """
        msg = "Set up your AI provider to use this."
        await self._send_transient_alert(request, msg)
        self._set_operation_error(msg)
        return await self._finalize_operation(request)

    # -----------------------------------------------------------------
    # Generic finalisation – now includes the error status when appropriate.
    # -----------------------------------------------------------------
    async def _finalize_operation(self, request: dict):
        """
        Sends the terminal frames for a UI operation.  This method is invoked
        by the various handlers after they have performed their work (or
        recorded a failure).  The original implementation always sent
        ``chat_status: done`` and never included an ``operation_status`` for
        failures.  We now attach an ``operation_status`` with ``failed`` when
        ``self._operation_error_message`` is set.
        """
        # Existing terminal frames (unchanged)
        await self._send_frame({
            "type": "chat_status",
            "status": "done",
        })

        # New: emit a terminal operation_status if a failure was recorded.
        if self._operation_error_message:
            await self._send_frame({
                "type": "operation_status",
                "status": "failed",
                "message": self._operation_error_message,
            })
        else:
            # Preserve previous behaviour for successful operations.
            await self._send_frame({
                "type": "operation_status",
                "status": "succeeded",
            })

        # Any other cleanup that the original method performed…
        ...

    # -----------------------------------------------------------------
    # Existing methods (unchanged) …
    # -----------------------------------------------------------------
    ...
