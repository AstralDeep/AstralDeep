"""Routes personal node text requests through the Kernel's sealed provider book and serves ticketed peer calls through the orchestrator's normal gates.
Every text request runs on the Kernel engine, and node_mesh_admission.py bounds each ticketed call to its exact tool and credentials.
The bridge imports signed history for peer calls, computes label floors and recovers requests without replaying unknown effects."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from orchestrator import push_wake
from orchestrator.node_mesh import PHI_CODES, MeshUnavailable, NodeMesh, NodeMeshError, RequestReference, StepRecord
from orchestrator import vessel_executor
from orchestrator.node_mesh_calls import CallRefused, OrchestratorCalls, dotted

logger = logging.getLogger("Orchestrator.NodeMeshChat")

REQUESTS_ENV = "ASTRAL_NODE_MESH_REQUESTS"
POLL_SECONDS = 15.0
CONSENT_SYNC_DELAYS = (0.25, 0.5, 1.0, 2.0, 4.0)
TURN_SECONDS = 600.0


STATE_FILE = "mesh-chats.json"
MAX_CONCURRENT_TURNS = 2
MAX_CONCURRENT_CALLS = 2
PROVIDER = "node"
MODEL = "tool-runner"
CONFIDENTIALITY = ("public", "private", "phi", "secret")
INTEGRITY = ("owner", "tool_trusted", "untrusted")
BASE_LABELS = ("private", "tool_trusted")
OFF_VALUES = {"0", "false", "no", "off"}

Labels = Tuple[str, str]


PhiCheck = Callable[[str], bool]
Admission = Callable[[Any, Labels], Awaitable[None]]


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


def output_labels(floor: Labels, effect_kind: str) -> Labels:
    # why: the Kernel labels an outward read's output untrusted in answer_call.rs, and a node answer must not record it higher
    return join_labels(floor, (floor[0], "untrusted")) if effect_kind == "outward_read" else floor


def args_digest(arguments_json: str) -> str:
    return "sha256:" + hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()


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


@dataclass
class _Admitted:
    floor: Labels
    view: Any
    reference: RequestReference
    resolved: Optional[Any] = None
    authority: Optional[Any] = None
    arguments: Optional[Dict[str, Any]] = None
    action: Optional[Dict[str, Any]] = None
    owner: Optional[str] = None


class MeshChatBridge:
    def __init__(
        self,
        mesh: NodeMesh,
        *,
        owner_id: Any,
        history: Any,
        state_path: Path,
        calls: Optional[Any] = None,
        policy: Optional[NodePolicy] = None,
        phi_active: Optional[PhiCheck] = None,
        admission: Optional[Admission] = None,
        owner_setup: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
        poll_seconds: float = POLL_SECONDS,
        turn_seconds: float = TURN_SECONDS,
    ):
        self._mesh = mesh
        self._owner_of = owner_id if callable(owner_id) else (lambda: owner_id)
        self._history = history
        self._calls = calls
        self._state = ChatState(state_path)
        self._policy = policy or NodePolicy.from_env()
        self._phi_active = phi_active or _protection_enabled
        self._admission = admission
        self._owner_setup = owner_setup
        self._poll_seconds = poll_seconds
        self._turn_seconds = turn_seconds
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake = asyncio.Event()
        self._history_lock = asyncio.Lock()
        self._tasks: List[asyncio.Task] = []
        self._turn_slots = asyncio.Semaphore(MAX_CONCURRENT_TURNS)
        self._call_slots = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
        self._running: set = set()
        self._recovered = False
        self._published: Optional[str] = None

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
        if loop is not None and type(change).__name__.rsplit(".", 1)[-1] in {"CHAT_CHANGED", "ChatChanged", "ROSTER_CHANGED", "RosterChanged"}:
            loop.call_soon_threadsafe(self._wake.set)

    async def _pickup_loop(self) -> None:
        while True:
            try:
                await self.scan()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("node_mesh_chat_scan_failed")
            try:
                async with asyncio.timeout(self._poll_seconds):
                    await self._wake.wait()
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def scan(self) -> int:
        if self._owner_setup is not None and await self._owner_setup() is None:
            return 0
        if not self._policy.serve_requests:
            return 0
        own = (await self._mesh.status()).get("member_id")
        if own is None or self._owner is None:
            return 0
        await self._publish_catalog()
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

    async def _publish_catalog(self) -> None:
        selection = getattr(self._calls, "published_selection", None)
        listing = getattr(self._calls, "published_tools", None)
        omitted = 0
        if selection is not None:
            tools, omitted = selection(self._owner)
        else:
            tools = listing(self._owner) if listing is not None else []
        if not tools:
            return
        digest = hashlib.sha256(json.dumps(tools, sort_keys=True).encode()).hexdigest()
        if digest == self._published:
            return
        if omitted:
            logger.warning("node_mesh_catalog_truncated: omitted %d tools", omitted)
        try:
            await self._mesh.publish_tool_catalog(tools)
        except (NodeMeshError, MeshUnavailable) as exc:
            logger.warning("node_mesh_catalog_unpublished: %s", exc.code)
            return
        self._published = digest

    async def _recover(self, own: str) -> None:
        for summary in await self._mesh.list_chats():
            for request in await self._mesh.chat_requests(summary.chat_id):
                if request.to_device != own or request.id in self._running:
                    continue
                if request.state.name == "CLAIMED":
                    if request.call is None:
                        await self._refuse(request, "node.interrupted")
                    else:
                        logger.warning("node_mesh_call_needs_owner: %s", request.id)
                elif request.call is None and request.state.name == "ANSWERED":
                    view = (await self._mesh.chat(request.chat_id)).view
                    if any(turn.id == request.turn and turn.author == own and _name(turn.status) == "running"
                           for turn in view.turns):
                        self._running.add(request.id)
                        self._tasks.append(asyncio.create_task(self._serve(request), name="node-mesh-request-recovery"))

    async def _serve(self, request: Any) -> None:
        request_id = request.id
        try:
            slots = self._call_slots if request.call is not None else self._turn_slots
            async with slots:
                await self._answer(request)
        except asyncio.CancelledError:
            raise
        except (NodeMeshError, MeshUnavailable) as exc:
            logger.warning("node_mesh_request_unwritten: %s", exc.code)
        except Exception:
            logger.exception("node_mesh_request_failed")
        finally:
            self._running.discard(request_id)

    async def _answer(self, request: Any) -> None:
        waits = iter(CONSENT_SYNC_DELAYS)
        while True:
            try:
                admitted = await self._admit(request)
                break
            except Refusal as denial:
                delay = next(waits, None)
                if denial.code != "ticket.consent_sync_pending" or delay is None:
                    await self._refuse(request, denial.code)
                    return
                await asyncio.sleep(delay)
        request = admitted.reference
        if request.call is None:
            await self._answer_kernel(request)
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
            await self._execution_failed(request, "node.turn_timeout")
        except NodeMeshError as exc:
            await self._execution_failed(request, exc.code)
        except Exception as exc:
            logger.warning("node_mesh_request_failed: %s", type(exc).__name__)
            await self._execution_failed(request, "node.turn_failed")

    async def _execution_failed(self, request: Any, code: str) -> None:
        if request.call is not None:
            logger.warning("node_mesh_call_outcome_unknown: %s", code)
        else:
            await self._refuse(request, code)

    async def _answer_kernel(self, request: Any) -> None:
        kernel, owner = self._mesh.kernel, self._owner
        try:
            await self._mesh.personal_request_context(RequestReference.capture(request), owner, kernel)
            if self._owner != owner:
                raise NodeMeshError("node.owner_mismatch")
        except NodeMeshError as exc:
            await self._refuse(request, exc.code)
            return
        task = asyncio.create_task(self._mesh.answer_request(request.id, kernel))
        try:
            await asyncio.wait_for(asyncio.shield(task), self._turn_seconds)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            await self._interrupt_native(task, request.id, kernel)
            try:
                await asyncio.shield(task)
            except Exception:
                logger.exception("node_mesh_request_interrupted_outcome_unknown")
            if asyncio.current_task().cancelling():
                raise
        except Exception as exc:
            current = await self._mesh.request(request.id)
            if current.state.name == "PENDING":
                await self._refuse(request, exc.code if isinstance(exc, NodeMeshError) else "node.turn_failed")
            elif isinstance(exc, NodeMeshError) and exc.code in PHI_CODES and current.state.name not in ("ANSWERED", "REFUSED"):
                await self._refuse(request, exc.code)
            elif current.state.name == "CLAIMED":
                await self._refuse(request, "node.outcome_unknown")
            else:
                logger.warning("node_mesh_request_outcome_unknown: %s", exc.code if isinstance(exc, NodeMeshError) else type(exc).__name__)

    async def _interrupt_native(self, task: asyncio.Task, request_id: str, kernel: Any) -> None:
        delay = 0.001
        while not task.done():
            try:
                acknowledged = await self._mesh.interrupt_request(request_id, kernel)
            except NodeMeshError:
                acknowledged = False
            if acknowledged is True:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 0.05)

    async def _refuse(self, request: Any, code: str) -> None:
        try:
            await self._mesh.refuse_request(request.id, dotted(code))
        except NodeMeshError as exc:
            logger.warning("node_mesh_request_unrefused: %s", exc.code)

    async def _admit(self, request: Any) -> _Admitted:
        if not request.from_device:
            raise Refusal("request.malformed")
        try:
            reference = RequestReference.capture(request)
        except NodeMeshError as exc:
            raise Refusal(exc.code) from exc
        view = (await self._mesh.chat(reference.chat_id)).view
        floor = join_labels(BASE_LABELS, labels_of(request.labels), _chat_floor(view))
        if await asyncio.to_thread(self._phi_active, self._owner):
            floor = join_labels(floor, ("phi", "owner"))
        await self._check_floor(floor, request)
        admitted = _Admitted(floor, view, reference)
        if request.call is not None:
            await self._admit_call(request, admitted)
        return admitted

    async def _check_floor(self, floor: Labels, request: Any) -> None:
        if self._admission is not None:
            await self._admission(request, floor)
        if floor[0] == "secret":
            raise Refusal("node.secret_label")
        if floor[0] == "phi" and not await self._mesh.phi_member():
            raise Refusal("node.phi_not_approved")

    async def _admit_call(self, request: Any, admitted: _Admitted) -> None:
        owner = self._owner
        reference = admitted.reference
        _call_id, tool, arguments_json, action_json, ticket_json = reference.call
        if self._calls is None:
            raise Refusal("node.calls_unavailable")
        try:
            action = json.loads(action_json)
            arguments = json.loads(arguments_json)
        except ValueError:
            raise Refusal("request.malformed") from None
        if not isinstance(action, dict) or not isinstance(arguments, dict):
            raise Refusal("request.malformed")
        if action.get("tool") != tool or action.get("args_digest") != args_digest(arguments_json):
            raise Refusal("request.call_mismatch")
        if action.get("device") != reference.from_device:
            raise Refusal("ticket.device_mismatch")
        try:
            resolved = await self._calls.resolve(owner, tool)
        except CallRefused as exc:
            raise Refusal(exc.code) from None
        if action.get("tool_def") != resolved.definition:
            raise Refusal("gate.tool_def_changed")
        try:
            verified = await self._mesh.verify_ticket(ticket_json, action_json, resolved.effect_kind)
        except NodeMeshError as exc:
            raise Refusal(exc.code) from None
        if (
            verified.device != reference.from_device
            or verified.turn != action.get("turn")
            or verified.mesh != action.get("mesh")
        ):
            raise Refusal("ticket.unauthorized")
        admitted.floor = join_labels(admitted.floor, labels_of(verified.labels))
        await self._check_floor(admitted.floor, request)

        admitted.resolved, admitted.arguments, admitted.action, admitted.owner = resolved, arguments, action, owner

    async def _execute(self, request: Any, admitted: _Admitted) -> None:
        owner = admitted.owner
        async with self._history_lock:
            if not owner or owner != self._owner:
                raise Refusal("node.owner_mismatch")
            local = await self._local_chat(admitted.view, owner)
        step = await self._run_call(request, admitted, local)
        turn_id = await self._mesh.record_turn(
            request.chat_id,
            continues=request.continues,
            message=None,
            answers=request.id,
            provider=PROVIDER,
            model=MODEL,
            labels=admitted.floor,
            steps=[step],
            text="",
            reason="completed",
        )
        try:
            view = (await self._mesh.chat(request.chat_id)).view
            self._state.mark_imported(request.chat_id, [m.id for m in view.messages if getattr(m, "turn", None) == turn_id])
        except Exception:
            logger.warning("node_mesh_chat_mark_failed")

    async def _run_call(self, request: Any, admitted: _Admitted, local: str) -> StepRecord:
        call_id, tool, arguments_json, action_json, ticket_json = admitted.reference.call
        from orchestrator import node_mesh_admission

        owner = admitted.owner
        if not owner or self._owner != owner:
            raise Refusal("node.owner_mismatch")
        ticket = vessel_executor.CallTicket(ticket_json, action_json, arguments_json)

        async def check_current() -> None:
            try:
                await self._check_floor(admitted.floor, request)
            except Refusal as exc:
                raise NodeMeshError(exc.code) from exc

        with node_mesh_admission.binding(
            self._mesh, lambda: self._owner, owner, local, admitted.resolved, ticket, request=request,
            check_current=check_current,
        ), vessel_executor.carrying(ticket):
            try:
                admitted.authority = await self._calls.authorize(owner, admitted.resolved)
            except CallRefused as exc:
                raise Refusal(exc.code) from None
            outcome = await asyncio.wait_for(
                self._calls.run(
                    owner, local, admitted.resolved, admitted.authority,
                    admitted.arguments, ticket,
                ),
                self._turn_seconds,
            )
        if outcome.code == "node.outcome_unknown":
            raise NodeMeshError(outcome.code)
        return StepRecord(
            call_id=call_id,
            tool=tool,
            arguments_json=arguments_json,
            tool_def=admitted.action["tool_def"],
            status="ok" if outcome.status == "ok" else "error",
            content=outcome.content,
            code=outcome.code,
            labels=output_labels(admitted.floor, admitted.resolved.effect_kind),
        )

    async def _local_chat(self, view: Any, owner: str) -> str:
        local = self._state.local_for(view.chat_id)
        if local is None:
            local = await asyncio.to_thread(self._history.create_chat, user_id=owner)
            self._state.link(view.chat_id, local)
        known = self._state.imported(view.chat_id)
        phi_ready = await self._mesh.phi_member()
        imported = []
        try:
            for item in view.messages:
                if (item.id in known or not item.text or item.role.name not in {"USER", "ASSISTANT"}
                        or item.status.name != "COMPLETE" or not _importable(item, phi_ready)):
                    continue
                role = "user" if item.role.name == "USER" else "assistant"
                await asyncio.to_thread(self._history.add_message, local, role, item.text, owner)
                imported.append(item.id)
        finally:
            if imported:
                self._state.mark_imported(view.chat_id, imported)
        return local


def _importable(message: Any, phi_ready: bool) -> bool:
    confidentiality = labels_of(message.labels)[0]
    return confidentiality != "secret" and (confidentiality != "phi" or phi_ready)


def _chat_floor(view: Any) -> Labels:
    return join_labels(("public", "owner"), *(labels_of(m.labels) for m in view.messages))


def _protection_enabled(owner_id: str) -> bool:
    from personalization.phi_policy import protection_enabled

    return bool(protection_enabled(owner_id))


_bridge: Optional[Any] = None
_wake: Optional[Any] = None
_bridge_lock = threading.Lock()


def active() -> Optional[Any]:
    return _bridge


def install(bridge: Optional[Any]) -> None:
    global _bridge
    with _bridge_lock:
        _bridge = bridge


async def start_for(orch: Any) -> None:
    from orchestrator import node_mesh
    from shared import node_identity

    mesh = await node_mesh.start_configured()
    if mesh is None or not mesh.requests_supported:
        return
    bridge = MeshChatBridge(
        mesh,
        owner_id=mesh.personal_owner_id,
        owner_setup=mesh.refresh_personal_owner,
        history=orch.history,
        calls=OrchestratorCalls(orch),
        state_path=node_identity.data_dir() / STATE_FILE,
    )
    await bridge.start()
    install(bridge)
    gateway = push_wake.gateway_for(orch)
    if gateway is not None:
        global _wake
        _wake = push_wake.WakeTriggers(gateway)
        _wake.attach(mesh, mesh.personal_owner_id)


async def stop_all() -> None:
    from orchestrator import node_mesh

    global _wake
    bridge, wake, _wake = _bridge, _wake, None
    install(None)
    try:
        if wake is not None:
            await wake.stop()
        if bridge is not None:
            await bridge.stop()
    finally:
        await node_mesh.shutdown()
