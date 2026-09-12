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
from persistent_agents.research_result import (
    legacy_page_response, read_page_observation, retain_page_observation,
)


_RESULT_FAILURE_CODES = frozenset({
    "assignment_model_failed", "assignment_model_output_truncated", "assignment_result_limit",
    "assignment_source_failed", "assignment_source_empty", "assignment_source_limit",
    "assignment_source_encoding_refused", "assignment_redaction_key_collision",
    "assignment_phi_refused", "assignment_phi_redaction_unavailable", "assignment_phi_redaction_invalid",
    "assignment_result_quarantined",
    "assignment_source_observation_invalid",
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


class _OperationAuthorityWindow:
    """Own one optional authority lock, with an idempotent early permit release."""

    def __init__(self, lock):
        self.lock = lock
        self.held = False

    async def acquire(self):
        """Acquire once; cancellation cannot release another window's lock."""
        if self.lock is not None:
            await self.lock.acquire()
            self.held = True
        return self

    def release(self):
        """Release only this window, including early release at permit issuance."""
        if self.held:
            self.held = False
            self.lock.release()

    async def __aenter__(self):
        return await self.acquire()

    async def __aexit__(self, *exc):
        self.release()


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
                 interactive_receipt_id=None, remote_marker=None, approved_action_id=None,
                 operation_sessions=None, operation_authority_lock=None):
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
        self.approved_action_id = approved_action_id
        # Deliberately unregistered: existing runners supply no session resolver.
        self.operation_sessions = operation_sessions
        if operation_authority_lock is not None and not isinstance(operation_authority_lock, asyncio.Lock):
            raise TypeError("operation authority lock must be an asyncio lock")
        self.operation_authority_lock = operation_authority_lock
        self.record = claim.assignment
        self.one_shot = self.record.execution_profile == "one_shot"
        self.binding = AssignmentOperationBinding(
            str(operation_fence.operation_id), operation_fence.execution_generation,
            str(operation_fence.execution_lease_token),
        )

    def fork(self, websocket):
        """A child shares durable budgets, with its own live authority binding."""
        if self.interactive or self.one_shot:
            raise DispatchDenied("assignment_foreground_fanout_denied")
        return ActionExecutor(self.runner, self.claim, self.operation_fence, websocket)

    def _operation_reader(self, request, action=None):
        """Only the qualified durable read profile can enter this adapter."""
        if (self.operation_sessions is None or self.interactive
                or self.remote_marker is not None or self.approved_action_id is not None
                or self.record.operation.get("version") != 2
                or self.record.operation.get("source_retention") != "operation"
                or not isinstance(request, dict) or request.get("kind") != "tool"
                or set(request) != {"kind", "agent_id", "tool_name", "arguments"}
                or not isinstance(request.get("arguments"), dict)
                or any(not isinstance(key, str) or key.startswith("_") or key in {"session_id", "user_id"}
                       for key in request["arguments"])
                or self.orch.tool_permissions.get_tool_scope(
                    request.get("agent_id"), request.get("tool_name")) not in {"tools:read", "tools:search"}
                or (action is not None and (action.intent.boundary != "read_only"
                    or action.intent.sensitivity != "ordinary" or action.intent.interactive_only
                    or action.intent.transient_input is not None))):
            raise DispatchDenied("assignment_operation_profile_unavailable")

    async def refresh(self, request=None, *, authority=None):
        if self.one_shot:
            self._operation_reader(request)
            if authority is None:
                from orchestrator.session_authority import refresh_operation_execution_authority
                authority = await refresh_operation_execution_authority(
                    owner_id=self.claim.fence.owner_id, assignment_id=self.claim.fence.assignment_id,
                    sessions=self.operation_sessions, plane_runtime=self.store.plane_runtime)
            current = await self.store.call_for_operation("assert_current_assignment_execution",
                fence=self.claim.fence, binding=self.binding, authority=authority.observation)
            with turn_permission_memo():
                checks = await self.service.validate_execution(
                    current.owner_id, authority.claims, current,
                    SimpleNamespace(request=request), authority=authority)
            # Permission/source checks can await. Revalidate the same snapshot,
            # never rotate the JWT after the ordinary delegation gate has run.
            self.record = await self.store.call_for_operation("assert_current_assignment_execution",
                fence=self.claim.fence, binding=self.binding, authority=authority.observation)
            return {**checks, "authority": authority}
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
        """Keep preparation and permit authorization coherent with optional renewal."""
        async with _OperationAuthorityWindow(self.operation_authority_lock if self.one_shot else None) as window:
            return await self._action(key, request, task_id=task_id, event_id=event_id,
                                      authority_window=window if window.lock is not None else None)

    async def _action(self, key, request, *, task_id=None, event_id=None, authority_window=None):
        if self.one_shot:
            self._operation_reader(request)
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
            return (await self._execute(existing, authority_window=authority_window)
                    if authority_window is not None else await self.execute(existing))
        else:
            raise DispatchDenied("assignment_history_capacity_exhausted")
        checks = await self.refresh(request if request["kind"] == "tool" else None)
        limits = self.record.definition.limits
        timeout_ms = (min(120_000, limits["elapsed_ms"]
            - self.record.usage.get("spent", {}).get("elapsed_ms", 0)
            - self.record.usage.get("outstanding", {}).get("elapsed_ms", 0))
            if self.one_shot else limits["step_timeout_ms"])
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
        if self.one_shot:
            action = await self.store.call_for_operation("put_action_for_execution",
                fence=self.claim.fence, binding=self.binding, intent=intent,
                authority=checks["authority"].observation)
        else:
            action = await self.store.call("put_action", fence=self.claim.fence, intent=intent)
        return (await self._execute(action, authority_window=authority_window)
                if authority_window is not None else await self.execute(action))

    async def execute(self, action):
        """Execute a stored action with an optional pre-permit authority window."""
        async with _OperationAuthorityWindow(self.operation_authority_lock if self.one_shot else None) as window:
            return await self._execute(action, authority_window=window)

    async def _execute(self, action, *, authority_window=None):
        operation_checks = None
        if self.one_shot:
            request = thaw(action.intent.request)
            self._operation_reader(request, action)
            operation_checks = await self.refresh(request)
            current, actual = await self.store.read_current_action(
                fence=self.claim.fence, binding=self.binding, action_id=action.action_id,
                authority=operation_checks["authority"].observation)
            if (actual.owner_id != self.claim.fence.owner_id
                    or actual.assignment_id != self.claim.fence.assignment_id
                    or actual.instruction_revision != self.claim.fence.instruction_revision
                    or actual.control_epoch != self.claim.fence.control_epoch
                    or actual.intent != action.intent):
                raise DispatchDenied("assignment_action_binding_changed")
            self.record, action = current, actual
            if (operation_checks["permission_digest"] != action.intent.permission_digest
                    or operation_checks["precondition_digest"] != action.intent.precondition_digest):
                raise DispatchDenied("assignment_precondition_changed")
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
        if self.one_shot:
            bound = self.service.tool_bound(f"{request['agent_id']}:{request['tool_name']}")
            if (not 0 < action.intent.maximum.elapsed_ms <= 120_000
                    or any(getattr(action.intent.maximum, name) != value for name, value in bound.items())):
                raise DispatchDenied("assignment_tool_time_bound_exceeded")
        attempt_id = str(uuid.uuid4())
        reserve = self.store.call_for_operation if self.one_shot else self.store.call
        reserved = await reserve(
            "reserve_action_for_execution" if self.one_shot else "reserve_action",
            fence=self.claim.fence, action_id=action.action_id,
            attempt_id=attempt_id, expected_request_digest=action.intent.request_digest,
            maximum=action.intent.maximum, quote_digest=action.intent.quote_digest,
            quote_expires_at=action.intent.quote_expires_at,
            **({"binding": self.binding, "authority": operation_checks["authority"].observation}
               if self.one_shot else {}),
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
            if self.one_shot and (self.orch.ui_sessions.get(invocation) is not private_session
                    or private_session != expected_session):
                raise DispatchDenied("assignment_authorization_required")
            checks = await self.refresh(request if request["kind"] == "tool" else None,
                **({"authority": operation_checks["authority"]} if self.one_shot else {}))
            if self.one_shot and (self.orch.ui_sessions.get(invocation) is not private_session
                    or private_session != expected_session):
                raise DispatchDenied("assignment_authorization_required")
            if (checks["permission_digest"] != action.intent.permission_digest
                    or checks["precondition_digest"] != action.intent.precondition_digest):
                raise DispatchDenied("assignment_precondition_changed")

        async def start():
            nonlocal permit_issued
            if self.one_shot:
                permit = await self.store.call_for_operation("start_action_for_execution",
                    fence=self.claim.fence, binding=self.binding, authority=checks["authority"].observation,
                    action_id=action.action_id, attempt_id=attempt_id,
                    expected_request_digest=action.intent.request_digest,
                    current_permission_digest=checks["permission_digest"],
                    current_precondition_digest=checks["precondition_digest"])
                permit_issued = True
                if authority_window is not None:
                    # Issuance commits the effect permit. Renewal may now run
                    # while physical I/O continues; settlement has its own window.
                    authority_window.release()
                return permit
            def transaction(tx, repository, _current):
                return repository.start_action(
                    tx, fence=self.claim.fence, action_id=action.action_id, attempt_id=attempt_id,
                    expected_request_digest=action.intent.request_digest,
                    current_permission_digest=checks["permission_digest"],
                    current_precondition_digest=checks["precondition_digest"], binding=self.binding,
                    interactive_receipt_id=self.interactive_receipt_id
                    if self.interactive else None,
                )
            permit = await self.store.current_execution_transaction(
                fence=self.claim.fence, binding=self.binding, action_id=action.action_id,
                callback=transaction,
            )
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
                        fixed_page = (request.get("agent_id") == "web-research-1"
                                      and request.get("tool_name") == "fetch_page")
                        normalized = extract_result(legacy_page_response(response) if fixed_page else response)
                        page = (read_page_observation(response, requested_url=request["arguments"]["url"])
                                if self.one_shot and fixed_page else None)
                        # Scan the existing bounded reader observation (page text <=20,000)
                        # before retained excerpts; upstream omitted tails are unavailable.
                        from orchestrator.mas_defense import scan_message
                        complete = {"legacy": normalized, "page": page} if page is not None else normalized
                        original = canonical(complete)
                        if scan_message(original) or scan_message(content_text(complete)):
                            raise DispatchDenied("assignment_result_quarantined")
                        if page is not None:
                            # Metadata is never redacted into a different source
                            # identity. Only prose is transformed; unsafe actual
                            # URL/time/profile facts must fail their normal gate.
                            protected, redacted = await asyncio.to_thread(redact_observation,
                                {"text": page["text"], "title": page["title"]}, get_phi_gate())
                            protected = {**page, **protected}
                            # Validated server timestamps/enums are structured
                            # facts, not source prose (ISO dates match DOB rules).
                            # Every source-controlled text/URL still meets PHI.
                            await safe_text(canonical({key: protected[key] for key in
                                ("requested_url", "final_url", "title", "text")}),
                                reviewed_urls(self.record.definition.source))
                            result = retain_page_observation(protected,
                                source_action_id=action.action_id, redacted=redacted)
                        else:
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
            result_context = {}
            cancelled = False
            settlement_window = _OperationAuthorityWindow(
                self.operation_authority_lock if self.one_shot else None)
            if getattr(self.record, "execution_profile", "persistent") == "one_shot":
                # An authentic old permit must settle even when a current claim,
                # admission generation or fresh remote authority is no longer
                # available. Only a fresh matching observation may retain content.
                try:
                    await settlement_window.acquire()
                    current_checks = await self.refresh(request if request["kind"] == "tool" else None)
                    if (current_checks["permission_digest"] == action.intent.permission_digest
                            and current_checks["precondition_digest"] == action.intent.precondition_digest):
                        result_context = {"result_fence": self.claim.fence,
                                          "result_binding": self.binding,
                                          "result_authority": current_checks["authority"].observation}
                except Exception:  # noqa: BLE001 - settlement survives unavailable authority
                    pass
                except asyncio.CancelledError:
                    cancelled = True
            settle = self.store.call_for_operation if self.one_shot else self.store.call
            try:
                retained = await settle(
                    "record_action_outcome", owner_id=self.record.owner_id,
                    assignment_id=self.record.assignment_id, action_id=action.action_id,
                    attempt_id=attempt_id, dispatch_token=permit.dispatch_token,
                    expected_request_digest=action.intent.request_digest, outcome=receipt,
                    **result_context,
                )
            finally:
                settlement_window.release()
            retained_result = thaw(retained.result)
            observed = retained_result["result"]
            observed_state = outcome
            if cancelled:
                raise asyncio.CancelledError
            if retained_result.get("result_available") is False:
                raise DispatchDenied("assignment_result_unavailable")

        context = PersistentDispatchContext(
            owner_id=self.record.owner_id, kind=request["kind"],
            agent_id=request.get("agent_id"), tool_name=request.get("tool_name"),
            arguments=request.get("arguments", {}),
            timeout_seconds=action.intent.maximum.elapsed_ms / 1000,
            max_input_bytes=65536, max_output_tokens=request.get("max_output_tokens", 1024),
            authorize=authorize, start=start, observe=observe,
            remote_marker=self.remote_marker,
            strict_final_arguments=self.one_shot,
            conversation_id=self.record.definition.conversation_id if self.one_shot else None,
        )
        invocation = object() if self.one_shot else None
        private_session = None
        expected_session = None
        try:
            if self.one_shot:
                # One coherent verified generation precedes every ordinary gate.
                # Final callbacks only recheck this same snapshot locally.
                authority = operation_checks["authority"]
                expected_session = authority.claims
                expected_session.update(_raw_token=authority.subject_token, _invocation_channel="background")
                private_session = authority.claims
                private_session.update(_raw_token=authority.subject_token, _invocation_channel="background")
                self.orch.ui_sessions[invocation] = private_session
            with bind_dispatch(context), turn_permission_memo():
                if request["kind"] == "model":
                    message, _ = await self.orch._call_llm(
                        self.websocket, request["messages"], feature="persistent_assignment",
                        response_format={"type": "json_object"}, allow_stream=False,
                        reasoning_effort=request.get("reasoning_effort"),
                    )
                    if message is None:
                        raise DispatchDenied("assignment_model_unconfigured")
                elif self.one_shot:
                    await context.validate_tool(self.record.owner_id, request["agent_id"],
                                                request["tool_name"], request["arguments"])
                    response = await self.orch.execute_authorized_tool(
                        claims=authority.claims, user_id=self.record.owner_id,
                        agent_id=request["agent_id"], tool_name=request["tool_name"],
                        arguments=dict(request["arguments"]), channel="background",
                        chat_id=self.record.definition.conversation_id, websocket=invocation,
                        timeout=action.intent.maximum.elapsed_ms / 1000)
                    if response is None or response.error:
                        raise DispatchDenied("assignment_tool_refused")
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
            if invocation is not None and self.orch.ui_sessions.get(invocation) is private_session:
                self.orch.ui_sessions.pop(invocation, None)
            # Gate denials never received a physical permit, so their reserved
            # capacity can be released. Plane refuses release of begun work.
            if not permit_issued:
                await self.store.call(
                    "release_unstarted_action", owner_id=self.record.owner_id,
                    assignment_id=self.record.assignment_id, action_id=action.action_id,
                    attempt_id=attempt_id, expected_request_digest=action.intent.request_digest,
                    reason_code="assignment_dispatch_finished",
                )
