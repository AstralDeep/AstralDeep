"""The node's peer in its owner's mesh: it opens the pinned kernel binding with the node's own keys, a software signer and a node device kind, shows a pairing offer, and reports mesh status for the descriptor.
node_api.py serves it, node_mailbox.py supervises the node's mailbox beside it, node_mesh_chat.py moves chat activity through it, and nothing else imports the kernel binding.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from orchestrator import node_mesh_keys
from orchestrator.node_mailbox import MailboxError, MailboxProcess, MailboxSettings
from shared import node_identity

logger = logging.getLogger("Orchestrator.NodeMesh")

BINDING_MODULE = "astral_kernel"
ENABLE_ENV = "ASTRAL_NODE_MESH"
LABEL_ENV = "ASTRAL_NODE_MESH_LABEL"
RELAY_ENV = "ASTRAL_NODE_MESH_RELAY"
LOOPBACK_ENV = "ASTRAL_NODE_MESH_LOOPBACK"
DISCOVERY_ENV = "ASTRAL_NODE_MESH_LOCAL_DISCOVERY"
LOOKUP_ENV = "ASTRAL_NODE_MESH_INTERNET_LOOKUP"
SYNC_ENV = "ASTRAL_NODE_MESH_SYNC_MS"
KERNEL_DIR = "kernel"
DEFAULT_SYNC_MS = 5000
MIN_SYNC_MS = 200
MAX_SYNC_MS = 600_000
REQUEST_EXPORT = "record_peer_turn"
ENROLLED_STATES = frozenset({"ENROLLED", "RESTORED"})


class NodeMeshError(Exception):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code = code
        self.status = status


class MeshUnavailable(NodeMeshError):
    def __init__(self, code: str = "mesh_unavailable"):
        super().__init__(code, 503)


@dataclass(frozen=True)
class MeshSettings:
    label: str = "Node"
    relay: tuple = ("default",)
    loopback_only: bool = False
    local_discovery: bool = True
    internet_lookup: bool = True
    sync_interval_ms: int = DEFAULT_SYNC_MS
    mailbox: MailboxSettings = field(default_factory=MailboxSettings)

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "MeshSettings":
        values = os.environ if env is None else env

        def flag(name: str, default: bool) -> bool:
            raw = values.get(name, "").strip().lower()
            if not raw:
                return default
            return raw not in {"0", "false", "no", "off"}

        relay = tuple(part.strip() for part in values.get(RELAY_ENV, "default").split(",") if part.strip())
        try:
            interval = int(values.get(SYNC_ENV, "") or DEFAULT_SYNC_MS)
        except ValueError:
            interval = DEFAULT_SYNC_MS
        loopback = flag(LOOPBACK_ENV, False)
        return cls(
            label=(values.get(LABEL_ENV, "").strip() or "Node")[:64],
            relay=relay or ("default",),
            loopback_only=loopback,
            local_discovery=flag(DISCOVERY_ENV, not loopback),
            internet_lookup=flag(LOOKUP_ENV, not loopback),
            sync_interval_ms=max(MIN_SYNC_MS, min(MAX_SYNC_MS, interval)),
            mailbox=MailboxSettings.from_env(values, loopback=loopback),
        )


def enabled(env: Optional[Dict[str, str]] = None) -> bool:
    values = os.environ if env is None else env
    if not node_identity.is_personal():
        return False
    return values.get(ENABLE_ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def load_binding() -> Any:
    try:
        return importlib.import_module(BINDING_MODULE)
    except ImportError as exc:
        raise MeshUnavailable("kernel_binding_missing") from exc


def _code_of(exc: BaseException) -> str:
    return str(getattr(exc, "code", "") or "kernel_error")


class _Listener:
    def __init__(self, binding: Any):
        self._subscribers: List[Callable[[Any], None]] = []
        self._lock = threading.Lock()
        base = binding.KernelListener
        owner = self

        class Bridge(base):  # type: ignore[valid-type, misc]
            def on_change(self, change):
                owner.dispatch(change)

        self.bridge = Bridge()

    def subscribe(self, callback: Callable[[Any], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def dispatch(self, change: Any) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(change)
            except Exception:
                logger.exception("node_mesh_listener_failed")


def _signer(binding: Any, keys: node_mesh_keys.NodeMeshKeys) -> Any:
    class NodeSigner(binding.HostSigner):  # type: ignore[name-defined, misc]
        def identity_public_key(self):
            return keys.identity_public_key()

        def identity_sign(self, message):
            return keys.identity_sign(bytes(message))

        def presence_public_key(self):
            return keys.presence_public_key()

        def presence_sign(self, prompt, message):
            kind = "OTHER" if prompt.kind == binding.ConsentKind.OTHER else str(prompt.kind)
            try:
                return keys.presence_sign(kind, bytes(message))
            except node_mesh_keys.PresenceRefused as exc:
                raise binding.HostSignerError.Denied(str(exc)) from exc

        def hardware_backed(self):
            return False

    return NodeSigner()


def _relay(binding: Any, relay: Sequence[str]) -> Any:
    if relay == ("default",):
        return binding.RelayChoice.DEFAULT()
    if relay == ("none",):
        return binding.RelayChoice.DISABLED()
    return binding.RelayChoice.CUSTOM(urls=list(relay))


@dataclass(frozen=True)
class StepRecord:
    call_id: str
    tool: str
    arguments_json: str
    tool_def: str
    status: str
    content: str
    labels: tuple
    code: Optional[str] = None
    reasons: tuple = ()
    receipt: Optional[str] = None


IssuerLoader = Callable[[], Awaitable[List[Dict[str, str]]]]


async def default_issuers() -> List[Dict[str, str]]:
    from shared import jwks_cache, token_verify

    client_id = os.getenv("KEYCLOAK_CLIENT_ID", "").strip()
    found: List[Dict[str, str]] = []
    if not client_id:
        return found
    for issuer in node_identity.accepted_issuers():
        try:
            jwks = await jwks_cache.get_jwks(token_verify.jwks_url_for(issuer))
        except Exception:
            logger.warning("node_mesh_issuer_keys_unavailable")
            continue
        found.append({"issuer": issuer, "client_id": client_id, "jwks_json": json.dumps(jwks)})
    return found


class NodeMesh:
    def __init__(
        self,
        *,
        data_dir: Path,
        settings: Optional[MeshSettings] = None,
        binding: Any = None,
        issuers: Optional[IssuerLoader] = None,
        mailbox: Optional[MailboxProcess] = None,
    ):
        self._data_dir = Path(data_dir)
        self._settings = settings or MeshSettings()
        self._binding = binding if binding is not None else load_binding()
        self._issuers = issuers or default_issuers
        self._mailbox = mailbox
        self._kernel: Any = None
        self._keys: Optional[node_mesh_keys.NodeMeshKeys] = None
        self._listener: Optional[_Listener] = None
        self._started = False
        self._mailbox_error: Optional[str] = None
        self._lock = asyncio.Lock()

    @property
    def kernel(self) -> Any:
        if self._kernel is None:
            raise MeshUnavailable("mesh_not_started")
        return self._kernel

    @property
    def started(self) -> bool:
        return self._started

    @property
    def mailbox(self) -> Optional[MailboxProcess]:
        return self._mailbox

    @property
    def requests_supported(self) -> bool:
        return hasattr(self._kernel, REQUEST_EXPORT)

    def subscribe(self, callback: Callable[[Any], None]) -> None:
        if self._listener is None:
            raise MeshUnavailable("mesh_not_started")
        self._listener.subscribe(callback)

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            binding = self._binding
            keys = await asyncio.to_thread(
                node_mesh_keys.NodeMeshKeys.load_or_create,
                self._data_dir,
                new_secret=lambda: bytes(binding.new_operations_secret()),
            )
            issuers = await self._issuers()
            self._keys = keys
            self._listener = _Listener(binding)
            await self._start_mailbox()
            config = binding.KernelConfig(
                data_dir=str(self._data_dir / KERNEL_DIR),
                operations_secret=keys.operations_secret,
                wrap_secret=keys.wrap_secret,
                restore_marker=keys.restore_marker,
                device_label=self._settings.label,
                device_kind=binding.DeviceKind.NODE,
                issuers=[binding.IssuerConfig(**item) for item in issuers],
                network=binding.NetworkConfig(
                    relay=_relay(binding, self._settings.relay),
                    local_discovery=self._settings.local_discovery,
                    internet_lookup=self._settings.internet_lookup,
                    loopback_only=self._settings.loopback_only,
                    sync_interval_ms=self._settings.sync_interval_ms,
                    mailbox=self._mailbox.snapshot()["address"] if self._mailbox is not None else None,
                ),
            )
            (self._data_dir / KERNEL_DIR).mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                self._kernel = await asyncio.to_thread(
                    binding.Kernel.open, config, _signer(binding, keys), None, None, self._listener.bridge
                )
                try:
                    await self._kernel.start_network()
                except BaseException:
                    await self._kernel.stop_network()
                    raise
            except BaseException:
                await self._stop_mailbox()
                self._kernel = None
                raise
            self._started = True

    async def _start_mailbox(self) -> None:
        if self._mailbox is None and self._settings.mailbox.enabled:
            self._mailbox = MailboxProcess(self._settings.mailbox, data_dir=self._data_dir / "mailbox")
        if self._mailbox is None:
            return
        try:
            await self._mailbox.start()
        except MailboxError as exc:
            logger.warning("node_mailbox_unavailable: %s", exc)
            self._mailbox_error = str(exc)
            self._mailbox = None

    async def _stop_mailbox(self) -> None:
        if self._mailbox is not None:
            await self._mailbox.stop()

    async def stop(self) -> None:
        async with self._lock:
            if not self._started:
                return
            self._started = False
            try:
                await self._kernel.stop_network()
            finally:
                await self._stop_mailbox()

    async def _state(self) -> Any:
        return await self.kernel.mesh_status()

    @staticmethod
    def _enrollment(status: Any) -> str:
        return getattr(status.state, "name", str(status.state))

    async def status(self) -> Dict[str, Any]:
        if not self._started:
            return {"available": False, "state": "stopped"}
        status = await self._state()
        enrollment = self._enrollment(status)
        enrolled = enrollment in ENROLLED_STATES
        try:
            address: Optional[str] = await self.kernel.endpoint_address()
        except Exception as exc:
            logger.warning("node_mesh_endpoint_unavailable: %s", _code_of(exc))
            address = None
        mailbox = self._mailbox_snapshot()
        return {
            "available": True,
            "state": enrollment.lower(),
            "enrolled": enrolled,
            "mesh_id": status.mesh_id if enrolled else None,
            "member_id": status.device_id if enrolled else None,
            "members": status.members if enrolled else 0,
            "hardware_backed": bool(status.hardware_backed),
            "pairing_open": bool(status.pairing_open),
            "endpoint_address": address,
            "mailbox": mailbox,
            "requests": self.requests_supported,
        }

    async def pairing_offer(self) -> Dict[str, Any]:
        status = await self._state()
        if self._enrollment(status) in ENROLLED_STATES:
            raise NodeMeshError("mesh_already_joined")
        try:
            offer = await self.kernel.create_pairing_offer()
            address = await self.kernel.endpoint_address()
        except Exception as exc:
            if isinstance(exc, self._binding.KernelError):
                raise NodeMeshError(_code_of(exc)) from exc
            raise
        return {
            "code": offer.code,
            "payload": offer.payload,
            "expires_ms": int(offer.expires_ms),
            "endpoint_address": address,
            "mailbox": self._mailbox_snapshot(),
        }

    def _mailbox_snapshot(self) -> Optional[Dict[str, Any]]:
        if self._mailbox_error is not None:
            return {"running": False, "address": None, "error": self._mailbox_error}
        return self._mailbox.snapshot() if self._mailbox is not None else None

    async def cancel_pairing_offer(self) -> None:
        await asyncio.to_thread(self.kernel.cancel_pairing_offer)

    async def list_chats(self) -> List[Any]:
        return await asyncio.to_thread(self.kernel.list_chats)

    async def chat(self, chat_id: str) -> Any:
        return await asyncio.to_thread(self.kernel.chat, chat_id)

    async def create_chat(self, title: str) -> str:
        return await asyncio.to_thread(self.kernel.create_chat, title)

    async def send_message(self, chat_id: str, text: str, continues: Optional[str] = None) -> str:
        return await asyncio.to_thread(self.kernel.send_message, chat_id, text, continues)

    async def phi_member(self) -> bool:
        roster = await asyncio.to_thread(self.kernel.roster)
        return any(device.is_local and device.phi for device in roster.members)

    async def pending_requests(self) -> List[Any]:
        return await asyncio.to_thread(self.kernel.pending_requests_for_me)

    async def chat_requests(self, chat_id: str) -> List[Any]:
        return await asyncio.to_thread(self.kernel.chat_requests, chat_id)

    async def claim_request(self, request_id: str) -> str:
        return await self._request_call(self.kernel.claim_request, request_id)

    async def refuse_request(self, request_id: str, code: str) -> str:
        return await self._request_call(self.kernel.refuse_request, request_id, code)

    async def verify_ticket(self, ticket_json: str, action_json: str, effect_kind: str) -> Any:
        return await self._request_call(self.kernel.verify_ticket, ticket_json, action_json, effect_kind)

    async def record_turn(
        self,
        chat_id: str,
        *,
        continues: Optional[str],
        message: Optional[str],
        answers: Optional[str],
        provider: str,
        model: str,
        labels: tuple,
        steps: Sequence[StepRecord],
        text: str,
        reason: str,
        code: Optional[str] = None,
    ) -> str:
        binding = self._binding
        turn = binding.PeerTurn(
            continues=continues,
            message=message,
            answers=answers,
            provider=provider,
            model=model,
            labels=self._peer_labels(labels),
            steps=[
                binding.PeerToolStep(
                    call_id=step.call_id,
                    tool=step.tool,
                    arguments_json=step.arguments_json,
                    tool_def=step.tool_def,
                    reasons=list(step.reasons),
                    receipt=step.receipt,
                    consent=None,
                    status=step.status,
                    content=step.content,
                    code=step.code,
                    labels=self._peer_labels(step.labels),
                )
                for step in steps
            ],
            text=text,
            reason=reason,
            code=code,
        )
        return await self._request_call(self.kernel.record_peer_turn, chat_id, turn)

    def _peer_labels(self, labels: tuple) -> Any:
        confidentiality, integrity = labels
        return self._binding.PeerLabels(
            confidentiality=getattr(self._binding.PeerConfidentiality, confidentiality.upper()),
            integrity=getattr(self._binding.PeerIntegrity, integrity.upper()),
        )

    async def _request_call(self, function: Callable[..., Any], *arguments: Any) -> Any:
        try:
            return await asyncio.to_thread(function, *arguments)
        except self._binding.KernelError as exc:
            raise NodeMeshError(_code_of(exc)) from exc

    async def sync_now(self) -> None:
        await self.kernel.sync_now()


_current: Optional[NodeMesh] = None


def current() -> Optional[NodeMesh]:
    return _current


def install(mesh: Optional[NodeMesh]) -> None:
    global _current
    _current = mesh


async def start_configured(data_dir: Optional[Path] = None) -> Optional[NodeMesh]:
    if not enabled():
        return None
    try:
        mesh = NodeMesh(
            data_dir=data_dir or node_identity.data_dir(), settings=MeshSettings.from_env()
        )
    except MeshUnavailable as exc:
        logger.warning("node_mesh_unavailable: %s", exc.code)
        return None
    await mesh.start()
    install(mesh)
    return mesh


async def shutdown() -> None:
    mesh = _current
    install(None)
    if mesh is not None:
        await mesh.stop()
