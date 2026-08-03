"""Feature-066 pins for operation-chat adoption in ``handle_ui_message``.

The first message of a new chat is admitted BEFORE its conversation exists,
so ``_adopt_operation_chat`` binds the chat the turn just created onto the
connection's durable operation record. Guards first: no chat id, no operation
context, no fence, or an operation that already carries its conversation are
all silent no-ops. Both chat-creating call sites in the ``chat_message``
branch must reach the adoption, and a fresh ``new_chat`` greets with the
welcome examples (FR-024). Harness style follows
``test_voice_new_chat_activation_065.py``: a ``SimpleNamespace`` fake with
the REAL ``Orchestrator`` methods bound onto it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import types
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from orchestrator.orchestrator import _CONNECTION_OPERATION_CONTEXT, Orchestrator
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    InMemoryWorkAdmissionRepository,
    OperationOwner,
    OperationRequest,
    OwnerScope,
    WorkAdmissionCoordinator,
)


@dataclass
class _FakeClock:
    current: datetime = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _owner(user_id: str = "owner-066") -> OperationOwner:
    return OperationOwner(
        owner_scope=OwnerScope.USER,
        owner_user_id=user_id,
        connection_scope_id=None,
    )


def _request(label: str, *, chat_id: str | None) -> OperationRequest:
    submission_id = uuid.uuid4()
    return OperationRequest(
        operation_kind="connection_frame",
        admission_class=AdmissionClass.INTERACTIVE,
        owner=_owner(),
        submission_id=submission_id,
        idempotency_namespace="ui_submission",
        idempotency_key=str(submission_id),
        normalized_input_digest=hashlib.sha256(label.encode("utf-8")).hexdigest(),
        chat_id=chat_id,
        parent_operation_id=None,
        connection_generation=uuid.uuid4(),
        request_generation=uuid.uuid4(),
    )


def _coordinator(clock: _FakeClock) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.INTERACTIVE,
                parent_class_name=None,
                active_limit=2,
                queue_limit=2,
                max_wait_ms=5_000,
                config_revision="test-066",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=clock,
        operation_retention=timedelta(hours=24),
    )


def _claimed(coordinator, request):
    accepted = coordinator.submit(request)
    assert accepted.accepted is True
    claim = coordinator.claim_operation(
        AdmissionClass.INTERACTIVE, accepted.operation_id
    )
    assert claim is not None
    return accepted, claim


def _adopting_fake(coordinator) -> SimpleNamespace:
    fake = SimpleNamespace(work_admission=coordinator)
    fake._call_work_admission = types.MethodType(
        Orchestrator._call_work_admission, fake
    )
    fake._adopt_operation_chat = types.MethodType(
        Orchestrator._adopt_operation_chat, fake
    )
    return fake


# ---------------------------------------------------------------------------
# _adopt_operation_chat: guards + happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adopt_returns_before_any_lookup_without_a_chat_id() -> None:
    # work_admission=None would explode if the guard ever fell through.
    fake = _adopting_fake(coordinator=None)
    token = _CONNECTION_OPERATION_CONTEXT.set({"operation": object()})
    try:
        assert await fake._adopt_operation_chat("") is None
        assert await fake._adopt_operation_chat(None) is None
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_adopt_is_a_no_op_without_an_operation_context() -> None:
    fake = _adopting_fake(coordinator=None)
    # The unset default (None) and a corrupt non-dict value both bail out.
    assert await fake._adopt_operation_chat("chat-new") is None
    token = _CONNECTION_OPERATION_CONTEXT.set("not-a-dict")
    try:
        assert await fake._adopt_operation_chat("chat-new") is None
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_adopt_skips_foreign_missing_fence_and_already_bound() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    _, unbound = _claimed(coordinator, _request("unbound", chat_id=None))
    _, bound = _claimed(coordinator, _request("bound", chat_id="chat-a"))

    for context in (
        {"operation": "not-an-operation", "execution_fence": unbound.fence},
        {"operation": unbound.operation, "execution_fence": None},
        {"operation": bound.operation, "execution_fence": bound.fence},
    ):
        original = context["operation"]
        fake = _adopting_fake(coordinator)
        token = _CONNECTION_OPERATION_CONTEXT.set(context)
        try:
            # The already-bound case would raise ValueError (cross-
            # conversation) if the guard fell through to bind_chat.
            await fake._adopt_operation_chat("chat-b")
        finally:
            _CONNECTION_OPERATION_CONTEXT.reset(token)
        assert context["operation"] is original
    # Nothing was bound onto the None-chat operation.
    assert coordinator.assert_current_execution(unbound.fence).chat_id is None


@pytest.mark.asyncio
async def test_adopt_binds_the_operation_and_refreshes_the_context() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    request = _request("first-message", chat_id=None)
    accepted, claim = _claimed(coordinator, request)
    context = {"operation": claim.operation, "execution_fence": claim.fence}
    fake = _adopting_fake(coordinator)

    token = _CONNECTION_OPERATION_CONTEXT.set(context)
    try:
        await fake._adopt_operation_chat("chat-created-066")
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)

    # The in-context record is refreshed to the durably bound revision.
    assert context["operation"] is not claim.operation
    assert context["operation"].chat_id == "chat-created-066"
    projection = coordinator.query_operation(
        owner=request.owner, operation_id=accepted.operation_id
    )
    assert projection.chat_id == "chat-created-066"


# ---------------------------------------------------------------------------
# chat_message call sites + the fresh-chat welcome render
# ---------------------------------------------------------------------------


async def _noop_ws_action(**_kwargs: object) -> None:
    return None


def _chat_fake(websocket, history) -> tuple[SimpleNamespace, list, list, list]:
    """A handle_ui_message host with recorders on every 066 seam."""
    sent: list[dict[str, object]] = []
    adopted: list[str] = []
    turns: list[tuple[str, str]] = []

    fake = SimpleNamespace(
        ui_sessions={websocket: {"sub": "owner-066"}},
        history=history,
        _ws_active_chat={},
        cancelled_sessions={},
    )
    fake._get_user_id = lambda _websocket: "owner-066"
    fake._parsed_ui_frame = Orchestrator._parsed_ui_frame

    async def retire_welcome(_websocket: object) -> None:
        return None

    fake._retire_welcome_canvas = retire_welcome

    async def adopt(chat_id: str) -> None:
        adopted.append(chat_id)

    fake._adopt_operation_chat = adopt

    async def safe_send(_websocket: object, raw: str) -> bool:
        sent.append(json.loads(raw))
        return True

    fake._safe_send = safe_send

    async def serialized_chat(
        _websocket: object, message: str, chat_id: str, _display, **_kwargs
    ) -> None:
        turns.append((message, chat_id))

    fake._serialized_chat = serialized_chat
    fake.handle_ui_message = types.MethodType(Orchestrator.handle_ui_message, fake)
    return fake, sent, adopted, turns


@pytest.mark.asyncio
async def test_first_message_of_a_new_chat_adopts_the_created_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import audit.hooks

    monkeypatch.setattr(audit.hooks, "record_ws_action", _noop_ws_action)
    websocket = object()
    chat_id = str(uuid.uuid4())
    created: list[str] = []
    history = SimpleNamespace(
        create_chat=lambda *, user_id: (created.append(user_id) or chat_id),
    )
    fake, sent, adopted, turns = _chat_fake(websocket, history)

    await fake.handle_ui_message(
        websocket,
        json.dumps({
            "type": "ui_event",
            "action": "chat_message",
            "payload": {"message": "first message"},
        }),
    )
    await asyncio.sleep(0)

    assert created == ["owner-066"]
    assert adopted == [chat_id]
    assert {
        "type": "chat_created",
        "payload": {"chat_id": chat_id, "from_message": True},
    } in sent
    assert turns == [("first message", chat_id)]
    assert fake._ws_active_chat[id(websocket)] == chat_id


@pytest.mark.asyncio
async def test_unknown_client_supplied_chat_id_is_created_and_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import audit.hooks

    monkeypatch.setattr(audit.hooks, "record_ws_action", _noop_ws_action)
    websocket = object()
    chat_id = str(uuid.uuid4())
    created: list[str] = []
    history = SimpleNamespace(
        get_chat=lambda _chat_id, *, user_id: None,
        create_chat=lambda supplied, *, user_id: (
            created.append(supplied) or supplied
        ),
    )
    fake, sent, adopted, turns = _chat_fake(websocket, history)

    await fake.handle_ui_message(
        websocket,
        json.dumps({
            "type": "ui_event",
            "action": "chat_message",
            "payload": {"message": "resumed message", "chat_id": chat_id},
        }),
    )
    await asyncio.sleep(0)

    assert created == [chat_id]
    assert adopted == [chat_id]
    assert {
        "type": "chat_created",
        "payload": {"chat_id": chat_id, "from_message": True},
    } in sent
    assert turns == [("resumed message", chat_id)]


@pytest.mark.asyncio
async def test_new_chat_greets_with_the_welcome_examples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """066 FR-024: a fresh chat renders the welcome canvas before chat_created."""
    import audit.hooks

    monkeypatch.setattr(audit.hooks, "record_ws_action", _noop_ws_action)
    websocket = object()
    chat_id = str(uuid.uuid4())
    sent: list[dict[str, object]] = []
    rendered: list[tuple[list, bool]] = []

    fake = SimpleNamespace(
        ui_sessions={websocket: {"sub": "owner-066"}},
        history=SimpleNamespace(create_chat=lambda *, user_id: chat_id),
        _ws_welcome={},
    )
    fake._get_user_id = lambda _websocket: "owner-066"
    fake._parsed_ui_frame = Orchestrator._parsed_ui_frame
    fake.compute_tools_available_for_user = lambda _user_id: True

    async def send_ui_render(_websocket: object, components, speak=True) -> None:
        rendered.append((components, speak))

    fake.send_ui_render = send_ui_render

    async def safe_send(_websocket: object, raw: str) -> bool:
        sent.append(json.loads(raw))
        return True

    fake._safe_send = safe_send
    fake.handle_ui_message = types.MethodType(Orchestrator.handle_ui_message, fake)

    await fake.handle_ui_message(
        websocket,
        json.dumps({"type": "ui_event", "action": "new_chat", "payload": {}}),
    )
    await asyncio.sleep(0)

    # The welcome canvas rendered silently, then chat_created followed.
    assert len(rendered) == 1
    components, speak = rendered[0]
    assert speak is False
    assert components and all(isinstance(c, dict) for c in components)
    assert fake._ws_welcome[id(websocket)] is True
    assert sent == [
        {
            "type": "chat_created",
            "payload": {"chat_id": chat_id, "from_message": False},
        }
    ]
