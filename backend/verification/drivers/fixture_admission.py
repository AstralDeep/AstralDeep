"""Real durable connection admission for explicitly registered verification turns.

This helper supplies the same operation/fence handoff as socket ingress. It does
not configure capacity, replace repositories, grant caller authority or bypass
the ordinary chat dispatcher. The caller owns its captured human request.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncIterator
from uuid import UUID

from orchestrator.work_admission import (
    AcceptedAdmission,
    AdmissionClass,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    PlaneWorkAdmissionRepository,
    WorkAdmissionCoordinator,
)


class FixtureAdmissionError(RuntimeError):
    """The verification turn could not retain ordinary durable admission."""


def _generation(value: object) -> UUID:
    try:
        parsed = UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise FixtureAdmissionError("registered turn generation is invalid") from None
    if parsed.version != 4 or str(parsed) != str(value):
        raise FixtureAdmissionError("registered turn generation is not canonical UUID4")
    return parsed


async def _settle_call(callback: Any, *args: Any, **kwargs: Any) -> tuple[Any, asyncio.CancelledError | None]:
    """Join blocking work and retain cancellation until its durable result is known."""
    task = asyncio.create_task(asyncio.to_thread(callback, *args, **kwargs))
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:
            break
    return task.result(), cancellation


async def _run_sync(callback: Any, *args: Any, **kwargs: Any) -> Any:
    result, cancellation = await _settle_call(callback, *args, **kwargs)
    if cancellation is not None:
        raise cancellation
    return result


def _submit_and_claim(coordinator: WorkAdmissionCoordinator, request: OperationRequest) -> Any:
    accepted = coordinator.submit(request)
    if not isinstance(accepted, AcceptedAdmission) or not accepted.accepted:
        raise FixtureAdmissionError("registered turn admission was refused")
    try:
        claim = coordinator.claim_operation(AdmissionClass.INTERACTIVE, accepted.operation_id)
    except BaseException:
        coordinator.terminalize_unselected(
            accepted.operation_id, terminal_code="fixture_claim_failed",
            safe_summary=None, retry_after_ms=0,
        )
        raise
    if claim is None:
        coordinator.terminalize_unselected(
            accepted.operation_id, terminal_code="fixture_capacity_unavailable",
            safe_summary=None, retry_after_ms=0,
        )
        raise FixtureAdmissionError("registered turn could not claim its exact admission")
    return claim


@asynccontextmanager
async def admitted_registered_turn(
    orch: Any,
    websocket: Any,
    *,
    frame: dict[str, Any],
    human_request: object,
) -> AsyncIterator[dict[str, Any]]:
    """Yield an exact CONNECTION operation context and settle it on every exit.

    Blocking Plane work runs in joined worker threads. Cancellation is retained
    until admission has a known durable result and exact-fence cleanup finishes.
    Capacity refusal and queued work are never fake-admitted.
    """
    coordinator = getattr(orch, "work_admission", None)
    if (type(coordinator) is not WorkAdmissionCoordinator
            or type(coordinator.repository) is not PlaneWorkAdmissionRepository):
        raise FixtureAdmissionError("registered turn requires the actual Plane admission coordinator")
    plane = getattr(getattr(orch, "runtime_composition", None), "plane", None)
    if (plane is None or coordinator.repository._runtime is not plane.runtime
            or coordinator.repository._plane_repository is not plane.repositories.work_admission):
        raise FixtureAdmissionError("registered turn admission is outside its Plane graph")
    context = getattr(orch, "_connection_contexts", {}).get(id(websocket))
    if (context is None or not getattr(context, "registered", False)
            or getattr(context, "closing", False)
            or websocket not in getattr(orch, "ui_sessions", {})):
        raise FixtureAdmissionError("registered turn connection is not current")
    if (frame.get("type") != "ui_event" or frame.get("action") != "chat_message"
            or human_request is None):
        raise FixtureAdmissionError("registered turn requires its captured chat request")
    generation = _generation(frame.get("connection_generation"))
    if generation != context.connection_generation:
        raise FixtureAdmissionError("registered turn connection generation changed")
    owner = OperationOwner(OwnerScope.CONNECTION, None, context.connection_scope_id)
    request_generation = _generation(frame.get("request_generation"))
    request = OperationRequest(
        operation_kind="chat_message", admission_class=AdmissionClass.INTERACTIVE,
        owner=owner, submission_id=_generation(frame.get("submission_id")),
        idempotency_namespace=None, idempotency_key=None, normalized_input_digest=None,
        chat_id=frame.get("chat_id"), parent_operation_id=None,
        connection_generation=generation, request_generation=request_generation,
    )
    claim, cancellation = await _settle_call(_submit_and_claim, coordinator, request)
    if cancellation is not None:
        await _run_sync(
            coordinator.terminalize, claim.fence, state=OperationState.CANCELLED,
            terminal_code="fixture_turn_cancelled", safe_summary=None, retry_after_ms=None,
        )
        raise cancellation
    operation_context = {
        "operation": claim.operation, "owner": owner, "execution_fence": claim.fence,
        "operation_kind": request.operation_kind, "connection_generation": generation,
        "request_generation": request_generation, "human_request": human_request,
        "work_read": None, "guidance_origin": None, "guidance_navigation": None,
    }
    stopped = asyncio.Event()
    renewal_failure: list[Exception] = []
    parent = asyncio.current_task()
    assert parent is not None
    initial_cancellations = parent.cancelling()

    async def renew() -> None:
        interval = max(0.01, coordinator.slot_lease.total_seconds() / 3)
        try:
            while True:
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=interval)
                    return
                except TimeoutError:
                    await _run_sync(coordinator.renew_execution_lease, claim.fence)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            renewal_failure.append(exc)
            parent.cancel()

    renewal = asyncio.create_task(renew(), name=f"fixture-admission-{claim.operation.operation_id}")
    state = OperationState.COMPLETED
    terminal_code = None
    try:
        yield operation_context
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError) and not renewal_failure:
            state, terminal_code = OperationState.CANCELLED, "fixture_turn_cancelled"
        else:
            state, terminal_code = OperationState.FAILED, "fixture_turn_failed"
        raise
    finally:
        stopped.set()
        renewal.cancel()
        # A retained renewal thread must finish before terminalization, including
        # when the caller is cancelled again while cleanup is already in flight.
        renewal_join = asyncio.create_task(_join_renewal(renewal))
        cleanup_cancellation = None
        while not renewal_join.done():
            try:
                await asyncio.shield(renewal_join)
            except asyncio.CancelledError as exc:
                cleanup_cancellation = cleanup_cancellation or exc
        renewal_join.result()
        if renewal_failure:
            state, terminal_code = OperationState.FAILED, "fixture_turn_failed"
        try:
            await _run_sync(
                coordinator.terminalize, claim.fence, state=state, terminal_code=terminal_code,
                safe_summary=None, retry_after_ms=None,
            )
        finally:
            if renewal_failure:
                parent.uncancel()
        if renewal_failure:
            # Convert only the cancellation this renewal task issued into its
            # explicit admission failure; unrelated caller cancellations remain.
            if parent.cancelling() > initial_cancellations:
                raise asyncio.CancelledError() from renewal_failure[0]
            raise FixtureAdmissionError("registered turn execution lease was lost") from renewal_failure[0]
        if cleanup_cancellation is not None:
            raise cleanup_cancellation


async def _join_renewal(renewal: asyncio.Task[None]) -> None:
    with suppress(asyncio.CancelledError):
        await renewal
