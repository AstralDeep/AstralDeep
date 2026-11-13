"""Moves chat activity between the node's own Python turn loop and the owner's mesh log: a request addressed to the node is claimed, run through the node's own gates and answered with a recorded turn, and a turn the node runs for a thin client is recorded into the owner's chat.
The node computes the labels of what it records, refuses PHI work unless the roster lists it as a PHI member, and trusts nothing a requesting device's gates decided."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from orchestrator.node_mesh import MeshUnavailable, NodeMesh, NodeMeshError, StepRecord
from orchestrator.node_mesh_calls import CallRefused, OrchestratorCalls, dotted

logger = logging.getLogger("Orchestrator.NodeMeshChat")

REQUESTS_ENV = "ASTRAL_NODE_MESH_REQUESTS"
POLL_SECONDS = 15.0
TURN_SECONDS = 600.0
ANSWER_GRACE_SECONDS = 5.0
MAX_TEXT = 32768
MAX_STEP_TEXT = 16384
MAX_STEPS = 64
STATE_FILE = "mesh-chats.json"
MAX_CONCURRENT_TURNS = 2
PROVIDER = "node"
MODEL = "node-turn-loop"
CONFIDENTIALITY = ("public", "private", "phi", "secret")
INTEGRITY = ("owner", "tool_trusted", "untrusted")
BASE_LABELS = ("private", "tool_trusted")
OFF_VALUES = {"0", "false", "no", "off"}

Labels = Tuple[str, str]
TurnRunner = Callable[[str, str, str], Awaitable[None]]
PhiCheck = Callable[[str], bool]


class Refusal(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class NodePolicy:
    serve_requests: bool = True

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "NodePolicy":
        values = os.environ if env is None else env
        return cls(serve_requests=values.get(REQUESTS_ENV, "").strip().lower() not in OFF_VALUES)


def _name(value: Any) -> str:
    return str(getattr(value, "name", value)).strip().lower()


def labels_of(source: Any) -> Labels:
    confidentiality = _name(source.confidentiality)
    integrity = _name(source.integrity)
    return (
        confidentiality if confidentiality in CONFIDENTIALITY else "secret",
        integrity if integrity in INTEGRITY else "untrusted",
    )


def join_labels(*items: Labels) -> Labels:
    return (
        max((item[0] for item in items), key=CONFIDENTIALITY.index),
        max((item[1] for item in items), key=INTEGRITY.index),
    )


def args_digest(arguments_json: str) -> str:
    return "sha256:" + hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()


def _tool_def(name: str) -> str:
    return "sha256:" + hashlib.sha256(json.dumps({"tool": name}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ChatState:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {"mesh_to_local": {}, "imported": {}}
        if self._path.exists():
            try:
                loaded = json.loads(self._path.read_text())
            except ValueError:
                loaded = None
            if isinstance(loaded, dict):
                for key in self._data:
                    if isinstance(loaded.get(key), dict):
                        self._data[key] = loaded[key]

    def _save(self) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self._path.parent, prefix=".mesh-chats")
        try:
            with os.fdopen(descriptor, "w") as handle:
                json.dump(self._data, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def local_for(self, mesh_chat: str) -> Optional[str]:
        with self._lock:
            return self._data["mesh_to_local"].get(mesh_chat)

    def mesh_for(self, local_chat: str) -> Optional[str]:
        with self._lock:
            for mesh_chat, local in self._data["mesh_to_local"].items():
                if local == local_chat:
                    return mesh_chat
        return None

    def link(self, mesh_chat: str, local_chat: str) -> None:
        with self._lock:
            self._data["mesh_to_local"][mesh_chat] = local_chat
            self._save()

    def imported(self, mesh_chat: str) -> set:
        with self._lock:
            return set(self._data["imported"].get(mesh_chat, []))

    def mark_imported(self, mesh_chat: str, event_ids: List[str]) -> None:
        with self._lock:
            known = self._data["imported"].setdefault(mesh_chat, [])
            known.extend(item for item in event_ids if item not in known)
            self._save()


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= limit else text[:limit]


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    found: List[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 8 or len(found) > 64:
            return
        if isinstance(node, dict):
            if node.get("type") in {"text", "markdown"} and isinstance(node.get("content"), str):
                found.append(node["content"])
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    walk(content)
    return "\n\n".join(found)


@dataclass
class _Session:
    owner_id: str
    local_chat: str
    mesh_chat: Optional[str] = None
    user_text: Optional[str] = None
    request: Optional[Any] = None
    steps: List[Dict[str, Any]] = field(default_factory=list)
    answer: Optional[str] = None
    done: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Admitted:
    floor: Labels
    view: Any
    resolved: Optional[Any] = None
    authority: Optional[Any] = None
    arguments: Optional[Dict[str, Any]] = None
    action: Optional[Dict[str, Any]] = None


class MeshChatBridge:
    def __init__(
        self,
        mesh: NodeMesh,
        *,
        owner_id: Any,
        history: Any,
        runner: TurnRunner,
        state_path: Path,
        calls: Optional[Any] = None,
        policy: Optional[NodePolicy] = None,
        phi_active: Optional[PhiCheck] = None,
        poll_seconds: float = POLL_SECONDS,
        turn_seconds: float = TURN_SECONDS,
    ):
        self._mesh = mesh
        self._owner_of = owner_id if callable(owner_id) else (lambda: owner_id)
        self._history = history
        self._runner = runner
        self._calls = calls
        self._state = ChatState(state_path)
        self._policy = policy or NodePolicy.from_env()
        self._phi_active = phi_active or _protection_enabled
        self._poll_seconds = poll_seconds
        self._turn_seconds = turn_seconds
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: "asyncio.Queue[tuple]" = asyncio.Queue()
        self._wake = asyncio.Event()
        self._sessions: Dict[str, _Session] = {}
        self._suppressed: set = set()
        self._tasks: List[asyncio.Task] = []
        self._turn_slots = asyncio.Semaphore(MAX_CONCURRENT_TURNS)
        self._running: set = set()
        self._recovered = False

    @property
    def _owner(self) -> Optional[str]:
        return self._owner_of()

    @property
    def state(self) -> ChatState:
        return self._state

    async def start(self) -> None:
        if not self._mesh.requests_supported:
            logger.warning("node_mesh_chat_disabled: requests_unsupported")
            raise MeshUnavailable("requests_unsupported")
        self._loop = asyncio.get_running_loop()
        self._mesh.subscribe(self._on_change)
        self._tasks = [
            asyncio.create_task(self._consume(), name="node-mesh-chat-consume"),
            asyncio.create_task(self._pickup_loop(), name="node-mesh-chat-pickup"),
        ]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._loop = None

    def _on_change(self, change: Any) -> None:
        loop = self._loop
        if loop is not None and type(change).__name__ in {"CHAT_CHANGED", "ChatChanged"}:
            loop.call_soon_threadsafe(self._wake.set)

    def observe_message(self, owner_id: str, chat_id: str, role: str, content: Any) -> None:
        self._enqueue(("message", owner_id, chat_id, role, content))

    def observe_step(self, owner_id: str, chat_id: str, step: Dict[str, Any]) -> None:
        self._enqueue(("step", owner_id, chat_id, step))

    def _enqueue(self, item: tuple) -> None:
        loop = self._loop
        if loop is not None and item[1] == self._owner:
            loop.call_soon_threadsafe(self._queue.put_nowait, item)

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item[0] == "message":
                    await self._handle_message(*item[1:])
                else:
                    self._handle_step(*item[1:])
            except Exception:
                logger.exception("node_mesh_chat_observe_failed")

    async def _handle_message(self, owner_id: str, chat_id: str, role: str, content: Any) -> None:
        if chat_id in self._suppressed:
            return
        text = message_text(content)
        if role == "user":
            previous = self._sessions.get(chat_id)
            if previous is not None and previous.request is not None:
                return
            if await asyncio.to_thread(self._phi_active, owner_id):
                self._sessions.pop(chat_id, None)
                return
            self._sessions[chat_id] = _Session(
                owner_id, chat_id, mesh_chat=self._state.mesh_for(chat_id), user_text=text
            )
        elif role == "assistant":
            session = self._sessions.get(chat_id)
            if session is None or not text.strip():
                return
            session.answer = text
            await self._finish(session)

    def _handle_step(self, owner_id: str, chat_id: str, step: Dict[str, Any]) -> None:
        session = self._sessions.get(chat_id)
        if session is None or len(session.steps) >= MAX_STEPS:
            return
        session.steps.append(step)

    async def _finish(self, session: _Session) -> None:
        if session.request is not None:
            session.done.set()
            return
        self._sessions.pop(session.local_chat, None)
        try:
            await self._write_turn(session)
        finally:
            session.done.set()

    async def _write_turn(self, session: _Session) -> None:
        mesh = self._mesh
        mesh_chat = session.mesh_chat
        if mesh_chat is None:
            first = (session.user_text or "").strip().splitlines()
            mesh_chat = await mesh.create_chat(_clip(first[0] if first else "Node chat", 80) or "Node chat")
            self._state.link(mesh_chat, session.local_chat)
        message = await mesh.send_message(mesh_chat, _clip(session.user_text or "", MAX_TEXT), None)
        view = (await mesh.chat(mesh_chat)).view
        floor = join_labels(BASE_LABELS, _chat_floor(view))
        await self._record(
            mesh_chat,
            continues=view.turns[-1].id if view.turns else None,
            message=message,
            answers=None,
            floor=floor,
            steps=session.steps,
            text=session.answer or "",
        )
        self._state.mark_imported(mesh_chat, [message])

    async def _pickup_loop(self) -> None:
        while True:
            try:
                await self.scan()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("node_mesh_chat_scan_failed")
            try:
                await asyncio.wait_for(self._wake.wait(), self._poll_seconds)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def scan(self) -> int:
        if not self._policy.serve_requests:
            return 0
        own = (await self._mesh.status()).get("member_id")
        if own is None or self._owner is None:
            return 0
        if not self._recovered:
            await self._recover(own)
            self._recovered = True
        started = 0
        for request in await self._mesh.pending_requests():
            if request.id in self._running:
                continue
            self._running.add(request.id)
            self._tasks.append(asyncio.create_task(self._serve(request), name="node-mesh-request"))
            started += 1
        self._tasks = [task for task in self._tasks if not task.done()]
        return started

    async def _recover(self, own: str) -> None:
        for summary in await self._mesh.list_chats():
            for request in await self._mesh.chat_requests(summary.chat_id):
                if request.state.name == "CLAIMED" and request.to_device == own and request.id not in self._running:
                    await self._refuse(request, "node.interrupted")

    async def _serve(self, request: Any) -> None:
        try:
            async with self._turn_slots:
                await self._answer(request)
        except asyncio.CancelledError:
            raise
        except (NodeMeshError, MeshUnavailable) as exc:
            logger.warning("node_mesh_request_unwritten: %s", exc.code)
        except Exception:
            logger.exception("node_mesh_request_failed")
        finally:
            self._running.discard(request.id)

    async def _answer(self, request: Any) -> None:
        try:
            admitted = await self._admit(request)
        except Refusal as denial:
            await self._refuse(request, denial.code)
            return
        try:
            await self._mesh.claim_request(request.id)
        except NodeMeshError as exc:
            logger.info("node_mesh_request_not_claimed: %s", exc.code)
            return
        try:
            await self._execute(request, admitted)
        except asyncio.CancelledError:
            raise
        except Refusal as denial:
            await self._refuse(request, denial.code)
        except asyncio.TimeoutError:
            await self._refuse(request, "node.turn_timeout")
        except NodeMeshError as exc:
            await self._refuse(request, exc.code)
        except Exception as exc:
            logger.warning("node_mesh_request_failed: %s", type(exc).__name__)
            await self._refuse(request, "node.turn_failed")

    async def _refuse(self, request: Any, code: str) -> None:
        try:
            await self._mesh.refuse_request(request.id, dotted(code))
        except NodeMeshError as exc:
            logger.warning("node_mesh_request_unrefused: %s", exc.code)

    async def _admit(self, request: Any) -> _Admitted:
        if not request.from_device:
            raise Refusal("request.malformed")
        view = (await self._mesh.chat(request.chat_id)).view
        floor = join_labels(BASE_LABELS, labels_of(request.labels), _chat_floor(view))
        if await asyncio.to_thread(self._phi_active, self._owner):
            floor = join_labels(floor, ("phi", "owner"))
        await self._check_floor(floor)
        admitted = _Admitted(floor, view)
        if request.call is not None:
            await self._admit_call(request, admitted)
        return admitted

    async def _check_floor(self, floor: Labels) -> None:
        if floor[0] == "secret":
            raise Refusal("node.secret_label")
        if floor[0] == "phi" and not await self._mesh.phi_member():
            raise Refusal("node.phi_not_approved")

    async def _admit_call(self, request: Any, admitted: _Admitted) -> None:
        call = request.call
        if self._calls is None:
            raise Refusal("node.calls_unavailable")
        try:
            action = json.loads(call.action_json)
            arguments = json.loads(call.arguments_json)
        except ValueError:
            raise Refusal("request.malformed") from None
        if not isinstance(action, dict) or not isinstance(arguments, dict):
            raise Refusal("request.malformed")
        if action.get("tool") != call.tool or action.get("args_digest") != args_digest(call.arguments_json):
            raise Refusal("request.call_mismatch")
        if action.get("device") != request.from_device:
            raise Refusal("ticket.device_mismatch")
        try:
            resolved = await self._calls.resolve(self._owner, call.tool)
        except CallRefused as exc:
            raise Refusal(exc.code) from None
        if action.get("tool_def") != resolved.definition:
            raise Refusal("gate.tool_def_changed")
        try:
            verified = await self._mesh.verify_ticket(call.ticket_json, call.action_json, resolved.effect_kind)
        except NodeMeshError as exc:
            raise Refusal(exc.code) from None
        if (
            verified.device != request.from_device
            or verified.turn != action.get("turn")
            or verified.mesh != action.get("mesh")
        ):
            raise Refusal("ticket.unauthorized")
        admitted.floor = join_labels(admitted.floor, labels_of(verified.labels))
        await self._check_floor(admitted.floor)
        try:
            admitted.authority = await self._calls.authorize(self._owner, resolved)
        except CallRefused as exc:
            raise Refusal(exc.code) from None
        admitted.resolved, admitted.arguments, admitted.action = resolved, arguments, action

    async def _execute(self, request: Any, admitted: _Admitted) -> None:
        local = await self._local_chat(admitted.view)
        if request.call is not None:
            steps, text = await self._run_call(request, admitted, local), ""
        else:
            steps, text = await self._run_turn(request, local)
        try:
            await self._record(
                request.chat_id,
                continues=request.continues,
                message=None,
                answers=request.id,
                floor=admitted.floor,
                steps=steps,
                text=text,
            )
        except NodeMeshError as exc:
            raise Refusal(exc.code) from None

    async def _run_call(self, request: Any, admitted: _Admitted, local: str) -> List[Any]:
        call = request.call
        outcome = await asyncio.wait_for(
            self._calls.run(self._owner, local, admitted.resolved, admitted.authority, admitted.arguments),
            self._turn_seconds,
        )
        return [
            {
                "call_id": call.call_id,
                "tool": call.tool,
                "arguments_json": call.arguments_json,
                "tool_def": admitted.action["tool_def"],
                "status": outcome.status,
                "content": outcome.content,
                "code": outcome.code,
            }
        ]

    async def _run_turn(self, request: Any, local: str) -> Tuple[List[Dict[str, Any]], str]:
        session = _Session(self._owner, local, mesh_chat=request.chat_id, user_text=request.text, request=request)
        self._sessions[local] = session
        try:
            await asyncio.wait_for(self._runner(self._owner, local, request.text), self._turn_seconds)
            try:
                await asyncio.wait_for(session.done.wait(), ANSWER_GRACE_SECONDS)
            except asyncio.TimeoutError:
                pass
        finally:
            self._sessions.pop(local, None)
        if not (session.answer or "").strip():
            raise Refusal("node.no_answer")
        return session.steps, session.answer

    async def _record(
        self,
        chat_id: str,
        *,
        continues: Optional[str],
        message: Optional[str],
        answers: Optional[str],
        floor: Labels,
        steps: List[Any],
        text: str,
    ) -> str:
        turn_id = await self._mesh.record_turn(
            chat_id,
            continues=continues,
            message=message,
            answers=answers,
            provider=PROVIDER,
            model=MODEL,
            labels=floor,
            steps=_step_records(steps, floor),
            text=_clip(text, MAX_TEXT),
            reason="completed",
        )
        try:
            view = (await self._mesh.chat(chat_id)).view
            self._state.mark_imported(chat_id, [m.id for m in view.messages if getattr(m, "turn", None) == turn_id])
        except Exception:
            logger.warning("node_mesh_chat_mark_failed")
        return turn_id

    async def _local_chat(self, view: Any) -> str:
        local = self._state.local_for(view.chat_id)
        if local is None:
            local = await asyncio.to_thread(self._history.create_chat, user_id=self._owner)
            self._state.link(view.chat_id, local)
        known = self._state.imported(view.chat_id)
        phi_ready = await self._mesh.phi_member()
        fresh = [
            m for m in view.messages
            if m.id not in known and m.text and m.role.name in {"USER", "ASSISTANT"}
            and m.status.name == "COMPLETE" and _importable(m, phi_ready)
        ]
        if fresh:
            self._suppressed.add(local)
            try:
                for item in fresh:
                    role = "user" if item.role.name == "USER" else "assistant"
                    await asyncio.to_thread(self._history.add_message, local, role, item.text, self._owner)
            finally:
                self._suppressed.discard(local)
            self._state.mark_imported(view.chat_id, [m.id for m in fresh])
        return local


def _importable(message: Any, phi_ready: bool) -> bool:
    confidentiality = labels_of(message.labels)[0]
    return confidentiality != "secret" and (confidentiality != "phi" or phi_ready)


def _chat_floor(view: Any) -> Labels:
    return join_labels(("public", "owner"), *(labels_of(m.labels) for m in view.messages))


def _step_records(steps: List[Any], floor: Labels) -> List[StepRecord]:
    records: List[StepRecord] = []
    for index, step in enumerate(steps[:MAX_STEPS]):
        if not isinstance(step, dict):
            continue
        if "call_id" in step:
            records.append(
                StepRecord(
                    call_id=step["call_id"], tool=step["tool"], arguments_json=step["arguments_json"],
                    tool_def=step["tool_def"], status="ok" if step["status"] == "ok" else "error",
                    content=step["content"], labels=floor, code=step["code"],
                )
            )
            continue
        name = _clip(step.get("name") or "tool", 128)
        failed = bool(step.get("error")) or step.get("status") != "completed"
        records.append(
            StepRecord(
                call_id=f"node-step-{index + 1}",
                tool=name,
                arguments_json=_arguments(step.get("args")),
                tool_def=_tool_def(name),
                status="error" if failed else "ok",
                content=_clip(step.get("error") if failed and step.get("error") else step.get("result") or "", MAX_STEP_TEXT),
                labels=floor,
                code="node.tool_failed" if failed else None,
            )
        )
    return records


def _arguments(raw: Any) -> str:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        parsed = {"text": _clip(raw, MAX_STEP_TEXT)}
    if not isinstance(parsed, dict):
        parsed = {} if parsed is None else {"value": parsed}
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), default=str)


def _protection_enabled(owner_id: str) -> bool:
    from personalization.phi_policy import protection_enabled

    return bool(protection_enabled(owner_id))


_bridge: Optional[MeshChatBridge] = None
_bridge_lock = threading.Lock()


def active() -> Optional[MeshChatBridge]:
    return _bridge


def install(bridge: Optional[MeshChatBridge]) -> None:
    global _bridge
    with _bridge_lock:
        _bridge = bridge


def observe_message(owner_id: str, chat_id: str, role: str, content: Any) -> None:
    bridge = _bridge
    if bridge is not None:
        bridge.observe_message(owner_id, chat_id, role, content)


def observe_step(owner_id: str, chat_id: str, step: Dict[str, Any]) -> None:
    bridge = _bridge
    if bridge is not None:
        bridge.observe_step(owner_id, chat_id, step)


def orchestrator_runner(orch: Any) -> TurnRunner:
    async def run(owner_id: str, local_chat: str, text: str) -> None:
        from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
        from orchestrator.chain_authority import AuthoritySkip

        authority = await orch.derive_machine_authority(
            user_id=owner_id, agent_id=None, turn_class="mesh_request", consented_scopes=[]
        )
        if isinstance(authority, AuthoritySkip):
            session_reason = getattr(authority, "reason", "authority_unavailable")
            raise NodeMeshError(f"authority_{session_reason}")
        task = BackgroundTask(task_id=f"mesh-{int(time.time() * 1000)}", chat_id=local_chat, user_id=owner_id)
        vws = VirtualWebSocket(task)
        orch._bind_machine_turn(vws, authority)
        try:
            await orch.handle_chat_message(vws, text, local_chat, user_id=owner_id)
        finally:
            orch._unbind_machine_turn(vws)
            await vws.close()

    return run


async def start_for(orch: Any) -> None:
    from orchestrator import node_mesh
    from shared import node_identity

    mesh = await node_mesh.start_configured()
    if mesh is None or not mesh.requests_supported:
        return
    bridge = MeshChatBridge(
        mesh,
        owner_id=node_identity.bound_owner,
        history=orch.history,
        runner=orchestrator_runner(orch),
        calls=OrchestratorCalls(orch),
        state_path=node_identity.data_dir() / STATE_FILE,
    )
    await bridge.start()
    install(bridge)


async def stop_all() -> None:
    from orchestrator import node_mesh

    bridge = _bridge
    install(None)
    try:
        if bridge is not None:
            await bridge.stop()
    finally:
        await node_mesh.shutdown()
