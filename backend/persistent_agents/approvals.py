"""Durable bridge between exact assignment and attended remote approvals."""
from __future__ import annotations

import asyncio
import time

from orchestrator import remote_confirmation
from orchestrator.plane_repository_context import plane_source_from_orchestrator

from .models import AssignmentError
from .service import thaw
from .store import AssignmentStore


class AssignmentApprovalBridge:
    """First review never implies confirmation-gate approval or an external effect."""

    def __init__(self, runner):
        self.runner = runner
        self.service = runner.service
        self.orch = runner.orch
        self.store = self.service.store
        self.remote = plane_source_from_orchestrator(self.orch).plane_repositories.remote_operation_proposals

    async def _remote_row(self, owner_id, proposal_id):
        return await self.store.transaction(lambda tx, _: self.remote.get(
            tx, owner_id=owner_id, proposal_id=proposal_id))

    def _link(self, tx, owner_id, record, action, proposal_id):
        return self.store.repository.link_interactive_proposal(tx,
            owner_id=owner_id, assignment_id=record.assignment_id, action_id=action.action_id,
            expected_request_digest=action.intent.request_digest, proposal_id=proposal_id,
            expected_instruction_revision=record.instruction_revision,
            expected_control_epoch=record.control_epoch)

    async def _validate(self, owner_id, record, action, interaction):
        claims = self.service._interaction(owner_id, interaction)
        if (record.lifecycle != "active" or record.instruction_revision != action.instruction_revision
                or record.control_epoch != action.control_epoch):
            raise AssignmentError("assignment_approval_stale", 409)
        checks = await self.service.validate_execution(owner_id, claims, record, action)
        if (checks["permission_digest"] != action.intent.permission_digest
                or checks["precondition_digest"] != action.intent.precondition_digest):
            raise AssignmentError("assignment_precondition_changed", 409)
        return claims

    async def __call__(self, owner_id, claims, record, action, interaction):
        await self._validate(owner_id, record, action, interaction)
        request = thaw(action.intent.request)
        proposal_id = getattr(action, "interactive_proposal_id", None)
        if proposal_id:
            row = await self._remote_row(owner_id, proposal_id)
            if row is None:
                raise AssignmentError("assignment_confirmation_unavailable", 409)
            if row.status == "approved":
                return await self.runner.execute_approved(action, interaction, remote_marker=proposal_id)
            if row.status == "pending" and row.expires_at >= int(time.time()):
                await self._show(interaction, row)
                return action
            if row.status in ("declined", "expired") or row.expires_at < int(time.time()):
                return await self._observe(owner_id, row)
            raise AssignmentError("assignment_confirmation_reconciliation_required", 409)
        if (request.get("kind") != "tool" or not remote_confirmation.is_destructive_unattended(
                request["tool_name"], request["arguments"], request["agent_id"])):
            return await self.runner.execute_approved(action, interaction)
        # No evaluate()/conditional remote stat here. Conservative classification
        # asks for the existing review without an unreserved network request.
        if remote_confirmation.policy_for(request["agent_id"]) is None:
            return await self.runner.execute_approved(action, interaction)

        def create():
            return remote_confirmation._create_proposal(self.orch, owner_id,
                record.definition.conversation_id, request["agent_id"], request["tool_name"],
                request["arguments"], on_created=lambda tx, pid: self._link(tx, owner_id, record, action, pid))
        proposal_id, _card = await asyncio.to_thread(create)
        row = await self._remote_row(owner_id, proposal_id)
        if row is None:
            raise AssignmentError("assignment_confirmation_unavailable", 503)
        await self._show(interaction, row)
        return await self.store.call("get_action", owner_id=owner_id,
                                     assignment_id=record.assignment_id, action_id=action.action_id)

    async def _show(self, interaction, row):
        from astralprims import Button, Card, Text
        card = Card(title="Confirm this assignment action", content=[
            Text(content="Your assignment approval is recorded. This action also requires the existing "
                         "remote-operation review; it has not run yet.", variant="body"),
            Text(content=row.summary, variant="body"),
            Button(label="Approve exact action", action="remote_op_decision",
                   payload={"proposal_id": row.proposal_id, "decision": "approve"}),
            Button(label="Decline", action="remote_op_decision", variant="secondary",
                   payload={"proposal_id": row.proposal_id, "decision": "decline"}),
        ]).to_dict()
        try:
            await self.orch.send_ui_render(interaction, [card], target="chat")
        except Exception as exc:
            raise AssignmentError("assignment_confirmation_delivery_unavailable", 503) from exc

    async def _observe(self, owner_id, row):
        def observe(tx, repository):
            if row.status == "pending" and row.expires_at < int(time.time()):
                self.remote.expire_if_pending(tx, owner_id=owner_id,
                    proposal_id=row.proposal_id, observed_at=int(time.time()))
            return repository.observe_interactive_proposal(tx, owner_id=owner_id, proposal_id=row.proposal_id)
        return await self.store.transaction(observe)

    async def handle_remote_decision(self, owner_id, interaction, row, action, decision):
        claims = self.service._interaction(owner_id, interaction)
        record = await self.service.get(owner_id, claims, action.assignment_id)
        if action.state in ("succeeded", "failed", "uncertain", "started", "invalidated", "declined", "expired"):
            return action
        if decision not in ("approve", "decline"):
            raise AssignmentError("assignment_decision_invalid", 422)
        if row.status in ("declined", "expired") or row.expires_at < int(time.time()):
            return await self._observe(owner_id, row)
        if row.status == "consumed":
            raise AssignmentError("assignment_confirmation_reconciliation_required", 409)
        await self._validate(owner_id, record, action, interaction)
        if row.status == "pending":
            def decide(tx, repository):
                # Parent/action row locks precede the remote-proposal mutation;
                # this cannot approve an action already fenced by an owner control.
                self._link(tx, owner_id, record, action, row.proposal_id)
                changed = self.remote.decide_if_pending(tx, owner_id=owner_id,
                    proposal_id=row.proposal_id, decision="approved" if decision == "approve" else "declined",
                    decided_at=int(time.time()))
                if changed is None:
                    raise AssignmentError("assignment_confirmation_already_decided", 409)
                if decision == "decline":
                    repository.observe_interactive_proposal(tx, owner_id=owner_id, proposal_id=row.proposal_id)
                return changed
            row = await self.store.transaction(decide)
            await remote_confirmation._audit_async(owner_id, f"remote_op.{row.status}",
                "Persistent assignment remote-operation review", proposal_id=row.proposal_id,
                machine_id=row.machine_id, verb=row.tool_name, chat_id=row.conversation_id)
        if row.status != "approved" or decision != "approve":
            return await self.store.call("get_action", owner_id=owner_id,
                                         assignment_id=action.assignment_id, action_id=action.action_id)
        # The runner claims only this approved action, and re-enters the ordinary
        # gates with a trusted marker. Never run the remote handler's chat continuation.
        return await self.runner.execute_approved(action, interaction, remote_marker=row.proposal_id)


async def handle_linked_remote_decision(orch, interaction, owner_id, row, decision):
    """Return True for every linked row, including disabled/unavailable service.

    Lookup is independent of the feature flag: disabling assignments must never
    transform a retained confirmation into an ordinary chat execution capability.
    """
    try:
        source = plane_source_from_orchestrator(orch)
    except RuntimeError:
        return False  # A pre-composition caller has no assignment repository.
    repository = getattr(source.plane_repositories, "assignments", None)
    if repository is None:
        return False
    service = getattr(orch, "persistent_assignments", None)
    owned_store = service is None
    store = service.store if service is not None else AssignmentStore(orch)
    try:
        action = await store.call("get_action_for_interactive_proposal", owner_id=owner_id,
                                   proposal_id=row.proposal_id)
        if action is None:
            return False
        executor = getattr(service, "approval_executor", None)
        if not isinstance(executor, AssignmentApprovalBridge):
            raise AssignmentError("assignment_approval_executor_unavailable", 503)
        await executor.handle_remote_decision(owner_id, interaction, row, action, decision)
        return True
    except AssignmentError:
        from astralprims import Alert
        await orch.send_ui_render(interaction, [Alert(message="This assignment confirmation cannot run. "
            "Open the ongoing agent to review its current state.", variant="warning").to_dict()], target="chat")
        return True
    finally:
        if owned_store:
            store.close()
