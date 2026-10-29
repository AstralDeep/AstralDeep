"""Exercise viewport hydration through real admission records and execution fences.
The database fixture provisions disposable PostgreSQL state independently of application storage.
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from orchestrator.harness.selection_service import HarnessSelectionService
from orchestrator.orchestrator import ConnectionContext, Orchestrator, _CONNECTION_OPERATION_CONTEXT
from orchestrator.work_admission import AdmissionClass, OperationState, OwnerScope
from tests.helpers.voice_plane_runtime import isolated_plane_runtime
from tests.test_conversation_snapshot_060 import _coordinator
from tests.test_viewport_hydration import CHAT, CONNECTION, REQUEST, frames, harness, message


@pytest.mark.parametrize("cancelled", [False, True])
async def test_real_admission_correlates_connection_owner_chat_and_fresh_request(cancelled, monkeypatch):
    import audit.hooks

    monkeypatch.setattr(audit.hooks, "record_ws_action", AsyncMock())
    host, socket, _binding, _authority = harness()
    del host._conversation_authority
    context = ConnectionContext(
        socket, host._connection_contexts[id(socket)].connection_scope_id,
        time.monotonic() + 60, connection_generation=UUID(CONNECTION), registered=True,
    )
    host._connection_contexts[id(socket)] = context
    raw = message().to_json()
    ingress = host._connection_frame(context, raw, json.loads(raw))
    assert ingress is not None and ingress.chat_id == CHAT
    assert ingress.authenticated_user_id == "owner"
    assert ingress.ingress_registration[1] is context
    with isolated_plane_runtime("viewport_hydration_admission") as database:
        host.work_admission = _coordinator(database)
        host.runtime_composition = SimpleNamespace(plane=SimpleNamespace(
            runtime=database, repositories=database.repositories,
        ))
        host.audit_repo = None
        host._llm_store = None
        host.harness_selection = HarnessSelectionService(orchestrator=host)
        _frame, owner, admitted, projection = host._submit_connection_batch(context, [ingress])[0]
        assert admitted.accepted and owner.owner_scope is OwnerScope.CONNECTION
        assert owner.connection_scope_id == context.connection_scope_id
        assert projection.chat_id == CHAT and str(projection.request_generation) == REQUEST
        assert str(projection.connection_generation) == CONNECTION
        claim = host.work_admission.claim_operation(AdmissionClass.INTERACTIVE, admitted.operation_id)
        assert claim is not None
        if cancelled:
            host.work_admission.terminalize(
                claim.fence, state=OperationState.CANCELLED, terminal_code="cancelled_by_user",
                safe_summary="Cancelled", retry_after_ms=None,
            )
        token = _CONNECTION_OPERATION_CONTEXT.set({
            "operation": claim.operation, "owner": owner, "execution_fence": claim.fence,
        })
        try:
            assert Orchestrator._conversation_authority(_CONNECTION_OPERATION_CONTEXT.get(), socket) is not None
            await host.handle_ui_message(socket, raw)
        finally:
            _CONNECTION_OPERATION_CONTEXT.reset(token)
        result = frames(host)[-1]
        assert result["request_generation"] == REQUEST
        assert result["chat_id"] == CHAT
        assert result["type"] == ("error" if cancelled else "conversation_snapshot")
        if cancelled:
            host.conversation_commits.build_snapshot.assert_not_called()
