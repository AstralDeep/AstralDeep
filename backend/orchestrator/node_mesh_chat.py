"""Moves chat activity between the node's own Python turn loop and the owner's mesh log: a turn the node runs for a thin client is written as signed events, and a request a device leaves in a mesh chat is run here.
Requests run through the node's own machine-authority and gate stack, never the device's gates, and chats of an owner under PHI protection are neither mirrored nor picked up because the log write carries no label.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from orchestrator.node_mesh import MeshUnavailable, NodeMesh, NodeMeshError

logger = logging.getLogger("Orchestrator.NodeMeshChat")

REQUESTS_ENV = "ASTRAL_NODE_MESH_REQUESTS"
POLL_SECONDS = 15.0
TURN_SECONDS = 600.0
MAX_CLAIMS = 5000
MAX_TEXT = 32768
MAX_STEP_TEXT = 4096
STATE_FILE = "mesh-chats.json"
NODE_MARKER = re.compile(r"^\s*@node\b[:,]?\s*", re.IGNORECASE)
MAX_CONCURRENT_TURNS = 2

TurnRunner = Callable[[str, str, str], Awaitable[None]]
PhiCheck = Callable[[str], bool]


@dataclass(frozen=True)
class RequestPolicy:
    mode: str = "addressed"

    @classmethod
    def from_env(cls) -> "RequestPolicy":
        mode = os.getenv(REQUESTS_ENV, "addressed").strip().lower()
        return cls(mode if mode in {"addressed", "all", "off"} else "addressed")

    def accepts(self, text: str) -> bool:
        if self.mode == "off":
            return False
        return self.mode == "all" or NODE_MARKER.match(text) is not None

    def prompt(self, text: str) -> str:
        return NODE_MARKER.sub("", text, count=1).strip() if self.mode != "off" else text


class ChatState:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {"mesh_to_local": {}, "imported": {}, "claims": {}}
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

    def claim(self, message_id: str, turn_id: str) -> bool:
        with self._lock:
            claims = self._data["claims"]
            if message_id in claims:
                return False
            claims[message_id] = turn_id
            while len(claims) > MAX_CLAIMS:
                claims.pop(next(iter(claims)))
            self._save()
            return True

    def claimed(self, message_id: str) -> bool:
        with self._lock:
            return message_id in self._data["claims"]

    def release(self, message_id: str) -> None:
        with self._lock:
            if self._data["claims"].pop(message_id, None) is not None:
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
    request_message: Optional[str] = None
    turn_id: Optional[str] = None
    steps: List[Dict[str, Any]] = field(default_factory=list)
    answer: Optional[str] = None
    done: asyncio.Event = field(default_factory=asyncio.Event)


class MeshChatBridge:
    def __init__(
        self,
        mesh: NodeMesh,
        *,
        owner_id: Any,
        history: Any,
        runner: TurnRunner,
        state_path: Path,
        policy: Optional[RequestPolicy] = None,
        phi_active: Optional[PhiCheck] = None,
        poll_seconds: float = POLL_SECONDS,
        turn_seconds: float = TURN_SECONDS,
    ):
        self._mesh = mesh
        self._owner_of = owner_id if callable(owner_id) else (lambda: owner_id)
        self._history = history
        self._runner = runner
        self._state = ChatState(state_path)
        self._policy = policy or RequestPolicy.from_env()
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

    @property
    def _owner(self) -> Optional[str]:
        return self._owner_of()

    @property
    def state(self) -> ChatState:
        return self._state

    async def start(self) -> None:
        if not self._mesh.turn_events_supported:
            logger.warning("node_mesh_chat_disabled: turn_events_unsupported")
            raise MeshUnavailable("turn_events_unsupported")
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
            if previous is not None and previous.request_message is not None and previous.user_text is not None:
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
        if session is None or len(session.steps) >= 256:
            return
        session.steps.append(step)

    async def _finish(self, session: _Session) -> None:
        self._sessions.pop(session.local_chat, None)
        try:
            await self._write_turn(session)
        finally:
            session.done.set()

    async def _write_turn(self, session: _Session) -> None:
        mesh = self._mesh
        mesh_chat = session.mesh_chat
        if mesh_chat is None:
            title = _clip((session.user_text or "Node chat").strip().splitlines()[0] if session.user_text else "Node chat", 80)
            mesh_chat = await mesh.create_chat(title or "Node chat")
            self._state.link(mesh_chat, session.local_chat)
        message = session.request_message
        imported: List[str] = []
        if message is None:
            message = await mesh.send_message(mesh_chat, _clip(session.user_text or "", MAX_TEXT), None)
            imported.append(message)
        events: List[Dict[str, Any]] = []
        if session.turn_id is None:
            events.append({"kind": "chat.turn_start", "body": {"continues": None, "message": message}})
        for step in session.steps:
            events.extend(_step_events(step))
        events.append({"kind": "chat.assistant_message", "body": {"text": _clip(session.answer or "", MAX_TEXT)}})
        events.append({"kind": "chat.turn_end", "body": {}})
        written = await mesh.append_chat_events(mesh_chat, events)
        for event, event_id in zip(events, written):
            if event["kind"] == "chat.assistant_message":
                imported.append(event_id)
        self._state.mark_imported(mesh_chat, imported)

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
        if self._policy.mode == "off":
            return 0
        started = 0
        own = (await self._mesh.status()).get("member_id")
        if own is None or self._owner is None:
            return 0
        for summary in await self._mesh.list_chats():
            view = (await self._mesh.chat(summary.chat_id)).view
            await self._recover(view, own)
            for message in view.messages:
                if not self._requestable(message, own):
                    continue
                if message.id in self._running or self._state.claimed(message.id):
                    continue
                if await asyncio.to_thread(self._phi_active, self._owner):
                    logger.info("node_mesh_request_skipped: phi_protection_active")
                    return started
                self._running.add(message.id)
                self._tasks.append(asyncio.create_task(self._serve(view, message), name="node-mesh-request"))
                started += 1
        self._tasks = [task for task in self._tasks if not task.done()]
        return started

    def _requestable(self, message: Any, own: str) -> bool:
        return (
            message.role.name == "USER"
            and message.status.name == "QUEUED"
            and message.author != own
            and bool(message.text)
            and self._policy.accepts(message.text)
        )

    async def _recover(self, view: Any, own: str) -> None:
        for turn in view.turns:
            if turn.status.name == "RUNNING" and turn.author == own and turn.id not in self._active_turns():
                await self._mesh.append_chat_events(
                    view.chat_id, [{"kind": "chat.turn_outcome_unknown", "body": {"turn": turn.id}}]
                )

    def _active_turns(self) -> set:
        return {session.turn_id for session in self._sessions.values() if session.turn_id}

    async def _serve(self, view: Any, message: Any) -> None:
        session: Optional[_Session] = None
        try:
            async with self._turn_slots:
                local = await self._local_chat(view, message)
                continues = next((t.id for t in reversed(view.turns) if t.branch == message.branch), None)
                written = await self._mesh.append_chat_events(
                    view.chat_id,
                    [{"kind": "chat.turn_start", "body": {"continues": continues, "message": message.id}}],
                )
                turn_id = written[0]
                if not self._state.claim(message.id, turn_id):
                    return
                session = _Session(
                    self._owner, local, mesh_chat=view.chat_id, user_text=message.text,
                    request_message=message.id, turn_id=turn_id,
                )
                self._sessions[local] = session
                try:
                    await asyncio.wait_for(
                        self._runner(self._owner, local, self._policy.prompt(message.text)),
                        self._turn_seconds,
                    )
                    await asyncio.wait_for(session.done.wait(), 5.0)
                except asyncio.TimeoutError:
                    await self._abandon(session, "turn_timeout")
                except asyncio.CancelledError:
                    raise
                except NodeMeshError as exc:
                    await self._refuse(session, exc.code)
                except Exception as exc:
                    logger.warning("node_mesh_request_failed: %s", type(exc).__name__)
                    await self._abandon(session, "turn_failed")
        except (NodeMeshError, MeshUnavailable) as exc:
            logger.warning("node_mesh_request_unwritten: %s", exc.code)
        finally:
            self._running.discard(message.id)

    async def _refuse(self, session: _Session, code: str) -> None:
        self._sessions.pop(session.local_chat, None)
        if session.done.is_set() or session.mesh_chat is None:
            return
        session.done.set()
        text = "The node did not run this request."
        await self._mesh.append_chat_events(
            session.mesh_chat,
            [
                {"kind": "chat.assistant_message", "body": {"text": text, "refused": code}},
                {"kind": "chat.turn_end", "body": {}},
            ],
        )

    async def _abandon(self, session: _Session, reason: str) -> None:
        self._sessions.pop(session.local_chat, None)
        if session.done.is_set() or session.mesh_chat is None:
            return
        session.done.set()
        await self._mesh.append_chat_events(
            session.mesh_chat,
            [{"kind": "chat.turn_outcome_unknown", "body": {"turn": session.turn_id, "reason": reason}}],
        )

    async def _local_chat(self, view: Any, request: Any) -> str:
        local = self._state.local_for(view.chat_id)
        if local is None:
            local = await asyncio.to_thread(self._history.create_chat, user_id=self._owner)
            self._state.link(view.chat_id, local)
        known = self._state.imported(view.chat_id)
        fresh = [
            m for m in view.messages
            if m.id != request.id and m.id not in known and m.text and m.role.name in {"USER", "ASSISTANT"}
            and m.status.name == "COMPLETE"
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


def _step_events(step: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = _clip(step.get("name") or "tool", 128)
    proposed = {"tool": name, "arguments": _clip(step.get("args") or "", MAX_STEP_TEXT)}
    result: Dict[str, Any] = {"tool": name, "status": step.get("status") or "completed"}
    if step.get("error"):
        result["error"] = _clip(step["error"], MAX_STEP_TEXT)
    else:
        result["result"] = _clip(step.get("result") or "", MAX_STEP_TEXT)
    return [{"kind": "chat.tool_proposed", "body": proposed}, {"kind": "chat.tool_result", "body": result}]


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
    if mesh is None or not mesh.turn_events_supported:
        return
    bridge = MeshChatBridge(
        mesh,
        owner_id=node_identity.bound_owner,
        history=orch.history,
        runner=orchestrator_runner(orch),
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
