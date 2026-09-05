"""Reconstructable, metered action execution through the ordinary dispatcher."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from astralplane.repositories.assignments import (
    AssignmentActionIntent,
    AssignmentActionOutcome,
    AssignmentOperationBinding,
    AssignmentResourceAmount,
)
from orchestrator.chain_authority import AuthoritySkip
from orchestrator.tool_permissions import turn_permission_memo
from personalization.phi_gate import get_phi_gate
from shared.llm_text import strip_reasoning_markup

from persistent_agents.cost_bounds import quoted_amount
from persistent_agents.dispatch_context import (
    DispatchDenied,
    PersistentDispatchContext,
    bind_dispatch,
    canonical,
)
from persistent_agents.runtime_values import digest, extract_result, thaw
from persistent_agents.privacy import content_text, privacy_text, redact_observation, reviewed_urls


_RESULT_FAILURE_CODES = frozenset({
    "assignment_model_failed", "assignment_model_output_truncated", "assignment_result_limit",
    "assignment_source_failed", "assignment_source_empty", "assignment_source_limit",
    "assignment_source_encoding_refused", "assignment_redaction_key_collision",
    "assignment_phi_refused", "assignment_phi_redaction_unavailable", "assignment_phi_redaction_invalid",
    "assignment_result_quarantined",
})
_RESULT_FAILURE_ALIASES = {
    "phi_redaction_refused": "assignment_phi_refused",
    "phi_redaction_unavailable": "assignment_phi_redaction_unavailable",
    "phi_redaction_invalid": "assignment_phi_redaction_invalid",
}


def _result_failure_code(value: Any) -> str:
    """Only fixed diagnostic identities may cross the rejected-content boundary."""
    if type(value) is str:
        value = _RESULT_FAILURE_ALIASES.get(value, value)
    return value if type(value) is str and value in _RESULT_FAILURE_CODES else "assignment_result_refused"


class ApprovalPending(RuntimeError):
    """The immutable action is persisted and requires attended owner review."""


async def safe_text(text: str, urls: tuple[str, ...] = ()) -> None:
    """Every payload is checked before assignment storage or downstream use."""
    from orchestrator.mas_defense import scan_message
    raw = content_text(text)
    if scan_message(text) or scan_message(raw):
        raise DispatchDenied("assignment_result_quarantined")
    if await asyncio.to_thread(get_phi_gate().contains_phi, privacy_text(raw, urls)):
        raise DispatchDenied("assignment_phi_refused")


class ActionExecutor:
    def __init__(self, runner, claim, operation_fence, websocket, *, interactive=False,
                 interactive_receipt_id=None, remote_marker=None):
        self.runner = runner
        self.orch = runner.orch
        self.service = runner.service
        self.store = self.service.store
        self.claim = claim
        self.operation_fence = operation_fence
        self.websocket = websocket
        self.interactive = interactive
        self.interactive_receipt_id = interactive_receipt_id
        self.remote_marker = remote_marker
        self.record = claim.assignment
        self.binding = AssignmentOperationBinding(
            str(operation_fence.operation_id), operation_fence.execution_generation,
            str(operation_fence.execution_lease_token),
        )

    def fork(self, websocket):
        """A child shares durable budgets, with its own live authority binding."""
        if self.interactive:
            raise DispatchDenied("assignment_foreground_fanout_denied")
        return ActionExecutor(self.runner, self.claim, self.operation_fence, websocket)

    async def refresh(self, request=None):
        self.record = await self.store.call("assert_current_claim", fence=self.claim.fence)
        await asyncio.to_thread(self.orch.work_admission.assert_current_execution,
                                self.operation_fence)
        definition = self.record.definition
        if not self.interactive:
            authority = await self.orch.derive_machine_authority(
                user_id=self.record.owner_id,
                agent_id=(request.get("agent_id") if request else None),
                turn_class="persistent_assignment",
                consented_scopes=list(definition.consented_scopes),
                grant_id=definition.offline_grant_id,
            )
            if isinstance(authority, AuthoritySkip):
                raise DispatchDenied("assignment_authorization_required")
            self.orch._bind_machine_turn(self.websocket, authority)
        claims = self.orch.ui_sessions.get(self.websocket, {})
        # A foreground chat's per-turn memo must not hide a permission revoke.
        with turn_permission_memo():
            return await self.service.validate_execution(
                self.record.owner_id, claims, self.record,
                SimpleNamespace(request=request) if request else None,
            )

    async def action(self, key: str, request: dict[str, Any], *, task_id=None, event_id=None):
        await safe_text(canonical(request), reviewed_urls(self.record.definition.source))
        for _ in range(256):
            existing = await self.store.call(
                "get_action_by_key", owner_id=self.record.owner_id,
                assignment_id=self.record.assignment_id, action_key=key,
            )
            if existing is None:
                break
            if existing.intent.request_digest != digest(request):
                raise DispatchDenied("assignment_action_binding_changed")
            unstarted_failure = (
                existing.state == "failed_not_started"
                and existing.intent.sensitivity == "ordinary"
                and not existing.intent.interactive_only
                and (request["kind"] == "model" or existing.intent.boundary == "read_only")
            )
            if ((existing.state == "invalidated" or unstarted_failure)
                    and not existing.ever_started and existing.result is None
                    and existing.control_epoch < self.record.control_epoch):
                # Controls can leave pre-permit failures intact. Preserve their
                # evidence while authorizing a fresh intent in the current epoch.
                # Begun actions, results and failed sensitive effects retain
                # their original identity and disposition.
                key = digest([key, "successor", existing.control_epoch])
                continue
            return await self.execute(existing)
        else:
            raise DispatchDenied("assignment_history_capacity_exhausted")
        checks = await self.refresh(request if request["kind"] == "tool" else None)
        limits = self.record.definition.limits
        timeout_ms = limits["step_timeout_ms"]
        model = request["kind"] == "model"
        if model:
            maximum = AssignmentResourceAmount(
                model_calls=1, tokens=len(canonical(request["messages"]).encode("utf-8"))
                + 512 + request["max_output_tokens"], elapsed_ms=timeout_ms,
            )
        else:
            bound = self.service.tool_bound(f"{request['agent_id']}:{request['tool_name']}")
            maximum = AssignmentResourceAmount(**bound)
            if bound["elapsed_ms"] > timeout_ms:
                raise DispatchDenied("assignment_tool_time_bound_exceeded")
        scope = None if model else self.orch.tool_permissions.get_tool_scope(
            request["agent_id"], request["tool_name"])
        sensitive = not model and scope not in {"tools:read", "tools:search"}
        maximum, quote_digest, quote_expires = quoted_amount(
            maximum, limits.get("currency"), self.record.definition.cost_quote_coverage,
            None if model else f"{request['agent_id']}:{request['tool_name']}",
        )
        intent = AssignmentActionIntent(
            action_key=key, request=request, request_digest=digest(request), maximum=maximum,
            permission_digest=checks["permission_digest"],
            precondition_digest=checks["precondition_digest"], task_id=task_id, event_id=event_id,
            sensitivity="sensitive" if sensitive else "ordinary", interactive_only=sensitive,
            boundary="unreplayable" if sensitive or model else "read_only",
            quote_digest=quote_digest, quote_expires_at=quote_expires,
            approval_expires_at=datetime.now(UTC) + timedelta(hours=1) if sensitive else None,
        )
        action = await self.store.call("put_action", fence=self.claim.fence, intent=intent)
        return await self.execute(action)

    async def execute(self, action):
        if action.state == "succeeded":
            retained = thaw(action.result)
            if retained.get("result_available") is False:
                raise DispatchDenied("assignment_result_requires_reconciliation")
            return retained["result"]
        if action.state in {"proposed", "approved"} and not self.interactive:
            raise ApprovalPending("assignment_approval_required")
        if action.state in {"uncertain", "started", "reconciliation"}:
            raise DispatchDenied("assignment_action_uncertain")
        request = thaw(action.intent.request)
        if action.state in {"declined", "invalidated", "expired"}:
            raise DispatchDenied("assignment_approval_invalid")
        attempt_id = str(uuid.uuid4())
        reserved = await self.store.call(
            "reserve_action", fence=self.claim.fence, action_id=action.action_id,
            attempt_id=attempt_id, expected_request_digest=action.intent.request_digest,
            maximum=action.intent.maximum, quote_digest=action.intent.quote_digest,
            quote_expires_at=action.intent.quote_expires_at,
        )
        if not reserved.created:
            raise DispatchDenied("assignment_attempt_already_reserved")
        started = time.monotonic()
        observed: dict[str, Any] | None = None
        observed_state = None
        checks: dict[str, Any] = {}
        permit_issued = False

        async def authorize():
            nonlocal checks
            checks = await self.refresh(request if request["kind"] == "tool" else None)
            if (checks["permission_digest"] != action.intent.permission_digest
                    or checks["precondition_digest"] != action.intent.precondition_digest):
                raise DispatchDenied("assignment_precondition_changed")

        async def start():
            nonlocal permit_issued
            def transaction(tx, repository):
                self.orch.work_admission.assert_current_execution(self.operation_fence, transaction=tx)
                return repository.start_action(
                    tx, fence=self.claim.fence, action_id=action.action_id, attempt_id=attempt_id,
                    expected_request_digest=action.intent.request_digest,
                    current_permission_digest=checks["permission_digest"],
                    current_precondition_digest=checks["precondition_digest"], binding=self.binding,
                    interactive_receipt_id=self.interactive_receipt_id
                    if self.interactive else None,
                )
            permit = await self.store.transaction(transaction)
            permit_issued = True
            return permit

        async def observe(permit, outcome, response):
            nonlocal observed, observed_state
            result: dict[str, Any] = {}
            actual = None
            if outcome == "uncertain" and action.intent.boundary == "read_only":
                # Retrying a reviewed read cannot duplicate a mutation. Keep
                # the full charge for this attempt even without a response.
                outcome = "failed"
            if outcome == "succeeded":
                try:
                    if request["kind"] == "model":
                        choices = getattr(response, "choices", None)
                        finish_reason = getattr(choices[0], "finish_reason", None) if choices else None
                        if type(finish_reason) is str and finish_reason == "length":
                            raise ValueError("assignment_model_output_truncated")
                        text = getattr(choices[0].message, "content", None) if choices else None
                        if not isinstance(text, str) or not text.strip():
                            raise ValueError("assignment_model_failed")
                        text = strip_reasoning_markup(text)
                        result = {"text": text}
                    else:
                        normalized = extract_result(response)
                        # Scan the complete bounded observation before redaction
                        # or truncation; an omitted tail cannot hide an injection.
                        from orchestrator.mas_defense import scan_message
                        original = canonical(normalized)
                        if scan_message(original) or scan_message(content_text(normalized)):
                            raise DispatchDenied("assignment_result_quarantined")
                        protected, redacted = await asyncio.to_thread(
                            redact_observation, normalized, get_phi_gate())
                        await safe_text(canonical(protected), reviewed_urls(self.record.definition.source))
                        text = protected["text"] or canonical(protected["data"])
                        result = {"text": text[:4096], "revision_digest": digest(protected),
                                  "truncated": len(text) > 4096, "redacted": redacted}
                    if len(canonical(result).encode("utf-8")) > 8192:
                        raise ValueError("assignment_result_limit")
                    await safe_text(result["text"], reviewed_urls(self.record.definition.source))
                    # Unknown provider usage is conservatively charged at the
                    # reserved maximum. No absent monetary usage becomes zero.
                    usage = getattr(response, "usage", None)
                    total = getattr(usage, "total_tokens", None)
                    if request["kind"] == "tool" or type(total) is int:
                        maximum = action.intent.maximum
                        actual = AssignmentResourceAmount(
                            model_calls=maximum.model_calls, tool_calls=maximum.tool_calls,
                            tokens=maximum.tokens if total is None else total,
                            elapsed_ms=min(maximum.elapsed_ms,
                                           max(1, int((time.monotonic() - started) * 1000))),
                            spend_micro_units=maximum.spend_micro_units,
                            currency=maximum.currency,
                        )
                except (ValueError, PermissionError) as exc:
                    code = _result_failure_code(exc.args[0] if len(exc.args) == 1 else None)
                    outcome, result = "failed", {"code": code}
            receipt = AssignmentActionOutcome(
                outcome=outcome, result_digest=digest(result), result=result, actual=actual,
            )
            retained = await self.store.call(
                "record_action_outcome", owner_id=self.record.owner_id,
                assignment_id=self.record.assignment_id, action_id=action.action_id,
                attempt_id=attempt_id, dispatch_token=permit.dispatch_token,
                expected_request_digest=action.intent.request_digest, outcome=receipt,
            )
            observed = thaw(retained.result)["result"]
            observed_state = outcome

        context = PersistentDispatchContext(
            owner_id=self.record.owner_id, kind=request["kind"],
            agent_id=request.get("agent_id"), tool_name=request.get("tool_name"),
            arguments=request.get("arguments", {}),
            timeout_seconds=action.intent.maximum.elapsed_ms / 1000,
            max_input_bytes=65536, max_output_tokens=request.get("max_output_tokens", 1024),
            authorize=authorize, start=start, observe=observe,
            remote_marker=self.remote_marker,
        )
        try:
            with bind_dispatch(context), turn_permission_memo():
                if request["kind"] == "model":
                    message, _ = await self.orch._call_llm(
                        self.websocket, request["messages"], feature="persistent_assignment",
                        response_format={"type": "json_object"}, allow_stream=False,
                    )
                    if message is None:
                        raise DispatchDenied("assignment_model_unconfigured")
                else:
                    name = request["tool_name"]
                    arguments = dict(request["arguments"])
                    if self.remote_marker is not None:
                        arguments["_remote_op_proposal_id"] = self.remote_marker
                    response = await self.orch.execute_single_tool(
                        self.websocket,
                        SimpleNamespace(function=SimpleNamespace(
                            name=name, arguments=canonical(arguments))),
                        {name: request["agent_id"]},
                        chat_id=self.record.definition.conversation_id,
                        user_id=self.record.owner_id,
                    )
                    if response is None or response.error:
                        raise DispatchDenied("assignment_tool_refused")
            if observed is None:
                raise DispatchDenied("assignment_action_not_executed")
            if observed_state != "succeeded":
                raise DispatchDenied(_result_failure_code(observed.get("code")))
            return observed
        except Exception:
            if observed_state == "uncertain":
                raise DispatchDenied("assignment_action_uncertain") from None
            raise
        finally:
            # Gate denials never received a physical permit, so their reserved
            # capacity can be released. Plane refuses release of begun work.
            if not permit_issued:
                await self.store.call(
                    "release_unstarted_action", owner_id=self.record.owner_id,
                    assignment_id=self.record.assignment_id, action_id=action.action_id,
                    attempt_id=attempt_id, expected_request_digest=action.intent.request_digest,
                    reason_code="assignment_dispatch_finished",
                )
