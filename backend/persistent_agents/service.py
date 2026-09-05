"""Owner policy for durable assignments; all execution uses normal dispatch."""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import inspect
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from uuid import UUID

from astralplane.repositories.assignment_models import (
    AssignmentActionDecision,
    AssignmentControl,
    AssignmentControlResult,
    AssignmentDefinition,
)
from orchestrator.tool_permissions import VALID_SCOPES
from orchestrator.tool_visibility import eligible_tool_pairs
from shared.external_http import validate_egress_url

from .models import (
    ApprovalDecisionRequest,
    AssignmentError,
    ControlRequest,
    CreateAssignmentRequest,
    ReviseAssignmentRequest,
    SourceSelection,
    digest,
    validate_id,
)
from .store import AssignmentStore
from .privacy import content_text, privacy_text, reviewed_urls


def thaw(value):
    """Convert detached frozen records without unsafe JSON/string fallbacks."""
    if is_dataclass(value):
        return {entry.name: thaw(getattr(value, entry.name)) for entry in fields(value)}
    if isinstance(value, Mapping):
        return {key: thaw(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw(child) for child in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def public_record(record):
    data = thaw(record)
    data.pop("owner_id", None)
    grant = data["definition"].pop("offline_grant_id", None)
    data["definition"].pop("cost_quote_coverage", None)
    data["authority"] = {"grant_bound": bool(grant)}
    data["cost_status"] = "capped" if data["definition"]["limits"].get("currency") else "unpriced"
    data["last_check_at"] = data.get("checkpoint", {}).get("last_checked_at")
    data["latest_result"] = data.get("checkpoint", {}).get("last_finding")
    # Private cursors/source event receipts are never part of owner view state.
    data.pop("checkpoint", None)
    return data


def public_action(action):
    data = thaw(action)
    data.pop("owner_id", None)
    data["intent"].pop("downstream_key", None)
    # Attempts contain observation capabilities; public consumers need states,
    # never dispatch tokens or operation lease credentials.
    data["attempts"] = [{key: attempt[key] for key in
                         ("attempt_id", "state", "outcome", "result_digest") if key in attempt}
                        for attempt in data.get("attempts", [])]
    return data


async def _thread(function, *args, **kwargs):
    # Do not inherit a foreground turn's permission memo into fresh authority reads.
    return await asyncio.to_thread(contextvars.Context().run, lambda: function(*args, **kwargs))


async def _invoke(callback, *args):
    result = callback(*args)
    return await result if inspect.isawaitable(result) else result


class AssignmentService:
    def __init__(self, orch, store=None, *, approval_executor=None, enabled=None,
                 phi_gate=None, execution_validator=None, quote_provider=None):
        self.orch = orch
        self.store = store if store is not None else AssignmentStore(orch)
        self.approval_executor = approval_executor
        self.execution_validator = execution_validator
        self.quote_provider = quote_provider
        self.enabled = enabled
        if phi_gate is None:
            from personalization.phi_gate import get_phi_gate
            phi_gate = get_phi_gate()
        self.phi_gate = phi_gate

    def _owner(self, owner_id, claims, *, human=True):
        if self.enabled is None:
            from shared.feature_flags import flags
            enabled = flags.is_enabled("persistent_agents")
        else:
            enabled = self.enabled
        if not enabled:
            raise AssignmentError("assignment_feature_disabled", 503)
        if (not isinstance(owner_id, str) or not 1 <= len(owner_id) <= 512
                or not isinstance(claims, dict) or claims.get("sub") != owner_id):
            raise AssignmentError("assignment_owner_required", 403)
        if human and (claims.get("act") or any(claims.get(key) for key in
                      ("machine_class", "machine_turn_class", "_machine_turn", "delegated"))):
            raise AssignmentError("assignment_human_required", 403)
        if human:
            from .dispatch_context import current_dispatch
            if current_dispatch() is not None:
                raise AssignmentError("assignment_human_required", 403)

    @staticmethod
    def _resource_id(value):
        try:
            return validate_id(value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise AssignmentError("assignment_not_found", 404) from exc

    async def _get(self, owner_id, assignment_id):
        self._resource_id(assignment_id)
        record = await self.store.call("get_assignment", owner_id=owner_id, assignment_id=assignment_id)
        if record is None:
            raise AssignmentError("assignment_not_found", 404)
        return record

    async def get(self, owner_id, claims, assignment_id):
        self._owner(owner_id, claims)
        return await self._get(owner_id, assignment_id)

    async def list(self, owner_id, claims, *, limit=50, after_id=None):
        self._owner(owner_id, claims)
        self._page(limit)
        if after_id is not None:
            self._resource_id(after_id)
        return await self.store.call("list_assignments", owner_id=owner_id, limit=limit, after_id=after_id)

    @staticmethod
    def _page(limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AssignmentError("assignment_page_invalid", 422)

    async def activity(self, owner_id, claims, assignment_id, *, after_sequence=0, limit=100):
        await self.get(owner_id, claims, assignment_id)
        self._page(limit)
        if type(after_sequence) is not int or not 0 <= after_sequence <= 9_223_372_036_854_775_807:
            raise AssignmentError("assignment_cursor_invalid", 422)
        return await self.store.call("list_activity", owner_id=owner_id, assignment_id=assignment_id,
                                     after_sequence=after_sequence, limit=limit)

    async def tasks(self, owner_id, claims, assignment_id):
        return (await self.get(owner_id, claims, assignment_id)).tasks

    async def proposals(self, owner_id, claims, assignment_id):
        await self.get(owner_id, claims, assignment_id)
        return await self.store.call("list_actions", owner_id=owner_id, assignment_id=assignment_id,
                                     states=("proposed", "approved", "uncertain"), limit=100)

    async def actions(self, owner_id, claims, assignment_id, *, limit=100, after_id=None):
        """Owner-visible durable outcomes, including completed actions."""
        await self.get(owner_id, claims, assignment_id)
        self._page(limit)
        if after_id is not None:
            self._resource_id(after_id)
        return await self.store.call("list_actions", owner_id=owner_id, assignment_id=assignment_id,
                                     limit=limit, after_id=after_id)

    async def events(self, owner_id, claims, assignment_id, *, limit=100, after_id=None):
        """Durable source receipt identities, without private source content."""
        await self.get(owner_id, claims, assignment_id)
        self._page(limit)
        if after_id is not None:
            self._resource_id(after_id)
        return await self.store.call("list_events", owner_id=owner_id, assignment_id=assignment_id,
                                     limit=limit, after_id=after_id)

    def _live_tools(self, owner_id, claims, tools, source):
        try:
            permissions = self.orch.tool_permissions
            disabled = permissions.list_disabled_agents(owner_id)
            pairs = eligible_tool_pairs(self.orch, owner_id, disabled_agents=disabled,
                                        identity_claims=claims)
            eligible = {f"{agent_id}:{skill.id}" for agent_id, skill in pairs}
            if not set(tools) <= eligible:
                raise AssignmentError("assignment_tool_unavailable", 403)
            scopes = {identity: permissions.get_tool_scope(*identity.split(":", 1))
                      for identity in tools}
            if any(scope not in VALID_SCOPES for scope in scopes.values()):
                raise AssignmentError("assignment_scope_unavailable", 403)
            if scopes[source.identity] not in ("tools:read", "tools:search"):
                raise AssignmentError("assignment_source_not_read_only", 403)
            for identity in tools:
                self.tool_bound(identity)
            return scopes
        except AssignmentError:
            raise
        except Exception as exc:
            raise AssignmentError("assignment_authority_unavailable", 503) from exc

    def tool_bound(self, identity):
        if identity == "web-research-1:fetch_page":
            return {"model_calls": 0, "tool_calls": 1, "tokens": 0, "elapsed_ms": 30_000}
        bound = getattr(self.orch, "persistent_tool_bounds", {}).get(identity)
        metrics = ("model_calls", "tool_calls", "tokens", "elapsed_ms")
        if (not isinstance(bound, Mapping) or any(type(bound.get(key)) is not int
                or not 0 <= bound[key] <= 100_000_000 for key in metrics)
                or bound["tool_calls"] < 1 or bound["elapsed_ms"] < 1):
            raise AssignmentError("assignment_tool_bound_unavailable", 422)
        return {key: bound[key] for key in metrics}

    async def _source(self, source):
        if source.profile == "public_page":
            try:
                for url in [source.arguments["url"], *source.linked_document_urls]:
                    await _thread(validate_egress_url, url)
            except Exception as exc:
                raise AssignmentError("assignment_source_unavailable", 422) from exc

    async def _definition(self, owner_id, claims, body):
        if body.consent is not True:
            raise AssignmentError("assignment_consent_required", 422)
        try:
            protected_text = privacy_text(content_text({
                "name": body.name, "instructions": body.instructions,
                "arguments": body.source.arguments,
                "linked_document_urls": body.source.linked_document_urls,
                "completion_condition": body.completion_condition,
            }), reviewed_urls(body.source.model_dump()))
            contains_phi = await _thread(self.phi_gate.contains_phi, protected_text)
        except ValueError as exc:
            raise AssignmentError("assignment_sensitive_content_refused", 422) from exc
        except Exception as exc:
            raise AssignmentError("assignment_phi_gate_unavailable", 503) from exc
        if contains_phi:
            raise AssignmentError("assignment_sensitive_content_refused", 422)
        source = body.source
        await self._source(source)
        identities = tuple(tool.identity for tool in body.allowed_tools)
        scopes = await _thread(self._live_tools, owner_id, claims, identities, source)
        if body.conversation_id is not None:
            try:
                owned_chat = await _thread(self.orch.history.get_chat, body.conversation_id, user_id=owner_id)
            except Exception as exc:
                raise AssignmentError("assignment_destination_unavailable", 503) from exc
            if owned_chat is None:
                raise AssignmentError("assignment_destination_not_found", 404)
        limits = body.limits.to_plane()
        if body.limits.max_depth:
            from shared.feature_flags import flags
            if not flags.is_enabled("recursive_delegation"):
                raise AssignmentError("assignment_delegation_disabled", 422)
        coverage = None
        if limits.get("currency"):
            if self.quote_provider is None:
                raise AssignmentError("assignment_cost_quote_unavailable", 422)
            coverage = await _invoke(self.quote_provider, owner_id, claims, body)
            if not isinstance(coverage, Mapping) or not coverage:
                raise AssignmentError("assignment_cost_quote_unavailable", 422)
        try:
            refresh = await _thread(self.orch.web_sessions.latest_refresh_token_for, owner_id)
            if not refresh:
                raise AssignmentError("assignment_authorization_required", 422)
            grant = await _thread(self.orch.offline_grants.capture, owner_id, refresh)
            if not grant or not await _thread(self.orch.offline_grants.is_valid, grant, user_id=owner_id):
                raise AssignmentError("assignment_authorization_required", 422)
        except AssignmentError:
            raise
        except Exception as exc:
            raise AssignmentError("assignment_authorization_required", 422) from exc
        return AssignmentDefinition(name=body.name, instructions=body.instructions,
            source=source.model_dump(), allowed_tools=identities,
            consented_scopes=tuple(sorted(set(scopes.values()))), offline_grant_id=grant,
            limits=limits, completion_condition=body.completion_condition,
            conversation_id=body.conversation_id, cost_quote_coverage=coverage)

    async def create(self, owner_id, claims, body: CreateAssignmentRequest):
        self._owner(owner_id, claims)
        # Stable owner-derived UUID4 gives lost-ack retries a lookup before grant
        # capture. Plane independently compares the complete submission digest.
        assignment_id = str(UUID(bytes=hashlib.sha256(
            f"{owner_id}\0{body.submission_id}".encode()).digest()[:16], version=4))
        submission_digest = digest(body.model_dump())
        receipt = await self._receipt(owner_id, assignment_id, body.submission_id, submission_digest, "create")
        if receipt is not None:
            return receipt
        definition = await self._definition(owner_id, claims, body)
        result = await self._mutation("create_assignment", "create", owner_id=owner_id,
            assignment_id=assignment_id, submission_id=body.submission_id,
            submission_digest=submission_digest, definition=definition,
            max_owned_assignments=25, max_retained_assignments=256)
        await self._audit(claims, "create", result)
        return result

    async def revise(self, owner_id, claims, assignment_id, body: ReviseAssignmentRequest):
        record = await self.get(owner_id, claims, assignment_id)
        submission_digest = digest({"command": "revise", "assignment_id": assignment_id, **body.model_dump()})
        receipt = await self._receipt(owner_id, assignment_id, body.submission_id, submission_digest, "revise")
        if receipt is not None:
            return AssignmentControlResult(receipt, False)
        # Let Plane replay an accepted revision before stale-CAS rejection.
        stale = (record.instruction_revision != body.expected_instruction_revision
                 or record.control_epoch != body.expected_control_epoch)
        replacement = record.definition if stale else await self._definition(owner_id, claims, body)
        result = await self._mutation("apply_control", "revise", owner_id=owner_id, assignment_id=assignment_id,
            expected_instruction_revision=body.expected_instruction_revision,
            expected_control_epoch=body.expected_control_epoch, submission_id=body.submission_id,
            submission_digest=submission_digest, control=AssignmentControl.REVISE, replacement=replacement)
        await self._audit(claims, "revise", result.assignment)
        return result

    async def control(self, owner_id, claims, assignment_id, command, body: ControlRequest):
        record = await self.get(owner_id, claims, assignment_id)
        if command not in ("pause", "resume", "stop", "revoke", "run-now"):
            raise AssignmentError("assignment_control_invalid", 422)
        submission_digest = digest({"command": command, "assignment_id": assignment_id, **body.model_dump()})
        receipt = await self._receipt(owner_id, assignment_id, body.submission_id, submission_digest, command)
        if receipt is not None:
            return receipt if command == "run-now" else AssignmentControlResult(receipt, False)
        if command in ("resume", "run-now"):
            await self.validate_execution(owner_id, claims, record)
        kwargs = {"owner_id": owner_id, "assignment_id": assignment_id,
            "expected_instruction_revision": body.expected_instruction_revision,
            "expected_control_epoch": body.expected_control_epoch, "submission_id": body.submission_id,
            "submission_digest": submission_digest}
        if command == "run-now":
            result = await self.store.call("request_check", **kwargs)
        else:
            result = await self.store.call("apply_control", control=AssignmentControl(command), **kwargs)
        await self._audit(claims, command.replace("-", "_"), getattr(result, "assignment", result))
        return result

    async def _receipt(self, owner_id, assignment_id, submission_id, submission_digest, command):
        return await self.store.call("get_submission_receipt", owner_id=owner_id, assignment_id=assignment_id,
            submission_id=submission_id, submission_digest=submission_digest, command=command)

    async def _mutation(self, method, command, **kwargs):
        try:
            return await self.store.call(method, **kwargs)
        except AssignmentError as exc:
            if exc.status_code != 409:
                raise
            # Concurrent consented retries can capture distinct server grants.
            # Only Plane's retained exact client-submission receipt proves replay.
            receipt = await self._receipt(kwargs["owner_id"], kwargs["assignment_id"],
                kwargs["submission_id"], kwargs["submission_digest"], command)
            if receipt is None:
                raise
            return receipt if method == "create_assignment" else AssignmentControlResult(receipt, False)

    async def validate_execution(self, owner_id, claims, record, action=None):
        self._owner(owner_id, claims, human=False)
        if record.owner_id != owner_id:
            raise AssignmentError("assignment_not_found", 404)
        source = SourceSelection.model_validate(thaw(record.definition.source))
        await self._source(source)
        scopes = await _thread(self._live_tools, owner_id, claims, record.definition.allowed_tools, source)
        if not set(scopes.values()) <= set(record.definition.consented_scopes):
            raise AssignmentError("assignment_scope_changed", 403)
        grant = record.definition.offline_grant_id
        try:
            valid_grant = bool(grant) and await _thread(self.orch.offline_grants.is_valid, grant, user_id=owner_id)
        except Exception as exc:
            raise AssignmentError("assignment_authorization_required", 403) from exc
        if not valid_grant:
            raise AssignmentError("assignment_authorization_required", 403)
        permission = digest({"tools": scopes, "instruction_revision": record.instruction_revision,
                             "control_epoch": record.control_epoch, "offline_grant_id": grant})
        precondition = digest({"source": thaw(record.definition.source)})
        if action is not None:
            request = thaw(action.intent.request if hasattr(action, "intent") else action.request)
            if request.get("kind") == "tool":
                identity = f"{request.get('agent_id')}:{request.get('tool_name')}"
                if identity not in record.definition.allowed_tools:
                    raise AssignmentError("assignment_tool_outside_consent", 403)
                if identity == "web-research-1:fetch_page":
                    action_source = SourceSelection(agent_id="web-research-1", tool_name="fetch_page",
                                                    arguments=request.get("arguments", {}))
                    allowed_urls = [source.arguments.get("url"), *source.linked_document_urls]
                    if action_source.arguments["url"] not in allowed_urls:
                        raise AssignmentError("assignment_source_outside_consent", 403)
                    await self._source(action_source)
                    precondition = digest({key: request[key] for key in ("agent_id", "tool_name", "arguments")})
                elif self.execution_validator is not None:
                    checked = await _invoke(self.execution_validator, owner_id, claims, record, action)
                    precondition = checked.get("precondition_digest") if isinstance(checked, Mapping) else None
                    if not isinstance(precondition, str) or len(precondition) != 64:
                        raise AssignmentError("assignment_precondition_unavailable", 403)
                elif identity == source.identity and request.get("arguments") == thaw(source.arguments):
                    precondition = digest({key: request[key] for key in ("agent_id", "tool_name", "arguments")})
                else:
                    raise AssignmentError("assignment_precondition_unavailable", 403)
            elif request.get("kind") != "model":
                raise AssignmentError("assignment_action_invalid", 422)
        return {"permission_digest": permission, "precondition_digest": precondition}

    def _interaction(self, owner_id, interaction):
        if interaction is None or isinstance(interaction, (dict, str)):
            raise AssignmentError("assignment_live_interaction_required", 409)
        try:
            claims = self.orch.ui_sessions.get(interaction)
        except (TypeError, AttributeError) as exc:
            raise AssignmentError("assignment_live_interaction_required", 409) from exc
        self._owner(owner_id, claims)
        if getattr(interaction, "closed", False) or getattr(interaction, "task", None) is not None:
            raise AssignmentError("assignment_live_interaction_required", 409)
        return claims

    async def decide(self, owner_id, claims, assignment_id, action_id,
                     body: ApprovalDecisionRequest, *, interaction=None):
        record = await self.get(owner_id, claims, assignment_id)
        self._resource_id(action_id)
        action = await self.store.call("get_action", owner_id=owner_id,
                                       assignment_id=assignment_id, action_id=action_id)
        if action is None:
            raise AssignmentError("assignment_action_not_found", 404)
        if body.request_digest != action.intent.request_digest:
            raise AssignmentError("assignment_proposal_changed", 409)
        retained = action.state in ("succeeded", "completed", "uncertain", "declined", "cancelled", "invalidated")
        if body.decision == "approve" and not retained:
            claims = self._interaction(owner_id, interaction)
            if self.approval_executor is None:
                raise AssignmentError("assignment_approval_executor_unavailable", 503)
            checked = await self.validate_execution(owner_id, claims, record, action)
        else:
            checked = {"permission_digest": action.intent.permission_digest,
                       "precondition_digest": action.intent.precondition_digest}
        decision = AssignmentActionDecision(proposal_digest=body.request_digest,
            decision=body.decision, submission_id=body.submission_id,
            submission_digest=digest({"assignment_id": assignment_id, "action_id": action_id,
                                      **body.model_dump()}), **checked)
        decided = await self.store.call("decide_action", owner_id=owner_id,
            assignment_id=assignment_id, action_id=action_id,
            expected_instruction_revision=body.expected_instruction_revision,
            expected_control_epoch=body.expected_control_epoch, decision=decision)
        await self._audit(claims, body.decision, record)
        # A retained completed/uncertain receipt is never a reason to dispatch again.
        if body.decision == "approve" and decided.state == "approved":
            return await self.approval_executor(owner_id, claims, record, decided, interaction)
        return decided

    async def delete(self, owner_id, claims, assignment_id, *, expected_control_epoch):
        await self.get(owner_id, claims, assignment_id)
        if type(expected_control_epoch) is not int or expected_control_epoch < 1:
            raise AssignmentError("assignment_control_invalid", 422)
        return await self.store.call("delete_for_owner", owner_id=owner_id,
            assignment_id=assignment_id, expected_control_epoch=expected_control_epoch)

    async def _audit(self, claims, command, record):
        from audit.hooks import record_generic
        await record_generic(claims=claims, event_class="settings", action_type=f"assignment_{command}",
            description="Persistent assignment owner command", outputs_meta={
                "assignment_id": record.assignment_id, "instruction_revision": record.instruction_revision,
                "control_epoch": record.control_epoch})
