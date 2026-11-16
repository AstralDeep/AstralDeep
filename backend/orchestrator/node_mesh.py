"""The node's peer in its owner's mesh: it opens the pinned kernel binding with the node's own keys, a software signer and a node device kind, serves a pairing offer only to a holder of the node's private pairing secret, reports mesh status for the descriptor, and refuses to bind, serve or sync a mesh listing a seeded development device (shared/dev_mesh_keys.py) unless ASTRAL_ENV is development.
node_api.py serves it, node_mailbox.py supervises the node's mailbox beside it, node_mesh_chat.py moves chat activity through it, and nothing else imports the kernel binding.
"""

from __future__ import annotations

import asyncio
import gc
import hmac
import inspect
import importlib
import json
import logging
import os
import re
import secrets
import stat
import tempfile
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from orchestrator import node_mesh_keys
from orchestrator.node_mailbox import MailboxError, MailboxProcess, MailboxSettings
from shared import dev_mesh_keys, node_identity

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
PUBLISH_EXPORT = "publish_tool_catalog"
ENROLLED_STATES = frozenset({"ENROLLED", "RESTORED"})
ENVIRONMENT_ENV = "ASTRAL_ENV"
DEVELOPMENT = "development"
DEVELOPMENT_MESH = "node.development_mesh"
ROSTER_CHANGES = frozenset({"ROSTER_CHANGED", "RosterChanged", "ENROLLMENT", "Enrollment"})
PAIRING_SECRET_FILE = "pairing-secret"
PAIRING_SECRET_BYTES = 32
OFFERS_PER_WINDOW = 5
OFFER_WINDOW_SECONDS = 60.0


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


def _development_allowed() -> bool:
    return os.environ.get(ENVIRONMENT_ENV) == DEVELOPMENT


def _variant(value: Any) -> str:
    return type(value).__name__.rsplit(".", 1)[-1]


def refuse_development_mesh(roster: Any) -> None:
    if _development_allowed():
        return
    try:
        mesh_id = roster.mesh_id
        listed = [member.device_id for member in roster.members] + [device.device_id for device in roster.removed]
    except (AttributeError, TypeError):
        raise NodeMeshError(DEVELOPMENT_MESH) from None
    if any(type(value) is not str for value in (mesh_id, *listed)) or not set(listed).isdisjoint(dev_mesh_keys.device_ids().values()):
        raise NodeMeshError(DEVELOPMENT_MESH)


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


class PairingSecret:
    def __init__(self, directory: Path):
        self._directory = Path(directory)
        self._path = self._directory / PAIRING_SECRET_FILE
        self._lock = threading.Lock()
        self._closed = False

    def open(self) -> None:
        with self._lock:
            self._closed = False
            if not self._trusted():
                self._write()

    def ensure(self) -> None:
        with self._lock:
            if not self._closed and not self._trusted():
                self._write()

    def rotate(self) -> None:
        with self._lock:
            if not self._closed:
                self._write()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._path.unlink(missing_ok=True)

    def matches(self, presented: str) -> bool:
        with self._lock:
            if not self._trusted():
                return False
            stored = self._path.read_bytes()
            return bool(stored) and hmac.compare_digest(stored, presented.encode())

    def _trusted(self) -> bool:
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return False
        return stat.S_ISREG(info.st_mode) and not stat.S_IMODE(info.st_mode) & 0o077

    def _write(self) -> None:
        node_identity._ensure_private_dir(self._directory)
        descriptor, temporary = tempfile.mkstemp(prefix=".pairing-secret-", dir=self._directory)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(secrets.token_urlsafe(PAIRING_SECRET_BYTES).encode())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


def _relay(binding: Any, relay: Sequence[str]) -> Any:
    if relay == ("default",):
        return binding.RelayChoice.DEFAULT()
    if relay == ("none",):
        return binding.RelayChoice.DISABLED()
    return binding.RelayChoice.CUSTOM(urls=list(relay))


@dataclass(frozen=True)
class PeerStanding:
    device_id: str
    phi: bool
    external_identity_available: bool = False


@dataclass(frozen=True)
class RequestReference:
    id: str
    chat_id: str
    from_device: str
    to_device: str
    text: str
    continues: Optional[str]
    call: Optional[tuple]
    turn: Optional[str] = None

    @classmethod
    def capture(cls, request: Any) -> "RequestReference":
        if isinstance(request, cls):
            return request
        values = tuple(getattr(request, name, None) for name in ("id", "chat_id", "from_device", "to_device", "text"))
        if any(type(value) is not str or not value for value in values):
            raise NodeMeshError("request.malformed")
        continues = getattr(request, "continues", None)
        if continues is not None and (type(continues) is not str or not continues):
            raise NodeMeshError("request.malformed")
        call = getattr(request, "call", None)
        if call is not None:
            call = tuple(getattr(call, name, None) for name in ("call_id", "tool", "arguments_json", "action_json", "ticket_json"))
            if any(type(value) is not str or not value for value in call):
                raise NodeMeshError("request.malformed")
        turn = getattr(request, "turn", None)
        if turn is not None and (type(turn) is not str or not turn):
            raise NodeMeshError("request.malformed")
        return cls(*values, continues, call, turn)


@dataclass(frozen=True)
class PersonalAuthorityContext:
    mesh_id: str
    member_id: str
    request: RequestReference


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


class NodeMesh:
    def __init__(
        self,
        *,
        data_dir: Path,
        settings: Optional[MeshSettings] = None,
        binding: Any = None,
        mailbox: Optional[MailboxProcess] = None,
        mailbox_address: Optional[str] = None,
    ):
        self._mailbox_address = mailbox_address
        self._data_dir = Path(data_dir)
        self._settings = settings or MeshSettings()
        self._binding = binding if binding is not None else load_binding()
        self._mailbox = mailbox
        self._kernel: Any = None
        self._keys: Optional[node_mesh_keys.NodeMeshKeys] = None
        self._listener: Optional[_Listener] = None
        self._started = False
        self._mailbox_error: Optional[str] = None
        self._personal_owner: Optional[str] = None
        self._owner_binding = {"state": "setup_required", "code": "mesh.not_enrolled"}
        self._lock = asyncio.Lock()
        self._owner_lock = asyncio.Lock()
        self._quarantined = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._checks: set = set()
        self._pairing = PairingSecret(self._data_dir)
        self._pairing_checks: set = set()
        self._offer_lock = asyncio.Lock()
        self._served: List[float] = []

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
            self._personal_owner = None
            self._owner_binding = {"state": "setup_required", "code": "mesh.not_enrolled"}
            binding = self._binding
            keys = await asyncio.to_thread(
                node_mesh_keys.NodeMeshKeys.load_or_create,
                self._data_dir,
                new_secret=lambda: bytes(binding.new_operations_secret()),
            )
            self._keys = keys
            self._listener = _Listener(binding)
            self._listener.subscribe(self._watch)
            self._listener.subscribe(self._watch_pairing)
            self._loop = asyncio.get_running_loop()
            self._quarantined = False
            (self._data_dir / KERNEL_DIR).mkdir(mode=0o700, parents=True, exist_ok=True)
            checked = await self._check_stored_mesh(keys)
            if self._quarantined:
                self._pairing.close()
                self._started = True
                return
            await self._start_mailbox()
            try:
                self._kernel = checked if checked is not None else await self._open_kernel(
                    keys, self._mailbox.snapshot()["address"] if self._mailbox is not None else self._mailbox_address
                )
                try:
                    await self._kernel.start_network()
                    if self._enrollment(await self._kernel.mesh_status()) in ENROLLED_STATES:
                        self._pairing.close()
                    elif node_identity.is_personal():
                        await asyncio.to_thread(self._pairing.open)
                except BaseException:
                    await self._kernel.stop_network()
                    raise
            except BaseException:
                await self._stop_mailbox()
                self._kernel = None
                raise
            self._started = True

    async def _open_kernel(self, keys: node_mesh_keys.NodeMeshKeys, mailbox: Optional[str]) -> Any:
        binding = self._binding
        config = binding.KernelConfig(
            data_dir=str(self._data_dir / KERNEL_DIR),
            operations_secret=keys.operations_secret,
            wrap_secret=keys.wrap_secret,
            restore_marker=keys.restore_marker,
            device_label=self._settings.label,
            device_kind=binding.DeviceKind.NODE,
            network=binding.NetworkConfig(
                relay=_relay(binding, self._settings.relay),
                local_discovery=self._settings.local_discovery,
                internet_lookup=self._settings.internet_lookup,
                loopback_only=self._settings.loopback_only,
                sync_interval_ms=self._settings.sync_interval_ms,
                mailbox=mailbox,
            ),
        )
        return await asyncio.to_thread(binding.Kernel.open, config, _signer(binding, keys), None, None, self._listener.bridge)

    def _supervises_mailbox(self) -> bool:
        return self._mailbox is not None or self._settings.mailbox.enabled

    async def _check_stored_mesh(self, keys: node_mesh_keys.NodeMeshKeys) -> Optional[Any]:
        if _development_allowed() or not any((self._data_dir / KERNEL_DIR).iterdir()):
            return None
        kernel = await self._open_kernel(keys, self._mailbox_address)
        if await self._holds_development_mesh(kernel):
            logger.warning("node_mesh_development_mesh_refused")
            self._kernel = kernel
            self._owner_binding = {"state": "refused", "code": DEVELOPMENT_MESH}
            self._quarantined = True
            return None
        if not self._supervises_mailbox():
            return kernel
        released = weakref.ref(kernel)
        del kernel
        if released() is not None:
            gc.collect()
        if released() is not None:
            raise MeshUnavailable("kernel_store_busy")
        return None

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

    async def _holds_development_mesh(self, kernel: Any) -> bool:
        if _development_allowed() or self._enrollment(await kernel.mesh_status()) not in ENROLLED_STATES:
            return False
        try:
            refuse_development_mesh(await asyncio.to_thread(kernel.roster))
        except NodeMeshError:
            return True
        return False

    def _watch(self, change: Any) -> None:
        if _development_allowed():
            return
        progress = getattr(change, "progress", None)
        stage = _variant(progress) if progress is not None else None
        if stage == "OFFER_SEEN":
            kernel = self._kernel
            if kernel is not None and getattr(progress, "peer", None) in dev_mesh_keys.device_ids().values():
                logger.warning("node_mesh_development_pairing_refused")
                kernel.cancel_pairing_offer()
            return
        loop = self._loop
        if loop is not None and (stage == "JOINED" or _variant(change) in ROSTER_CHANGES):
            loop.call_soon_threadsafe(self._schedule_check)

    def _watch_pairing(self, change: Any) -> None:
        progress = getattr(change, "progress", None)
        joined = progress is not None and _variant(progress) == "JOINED"
        if joined:
            self._pairing.close()
        loop = self._loop
        if loop is not None and (joined or _variant(change) in ROSTER_CHANGES):
            loop.call_soon_threadsafe(self._schedule_pairing_check)

    def _schedule_pairing_check(self) -> None:
        task = asyncio.get_running_loop().create_task(self._check_pairing(), name="node-mesh-pairing-check")
        self._pairing_checks.add(task)
        task.add_done_callback(self._pairing_checks.discard)

    async def _check_pairing(self) -> None:
        async with self._offer_lock:
            if not self._started or self._kernel is None:
                return
            try:
                status = await self._request_call(self._kernel.mesh_status)
            except NodeMeshError as exc:
                logger.warning("node_mesh_pairing_check_failed: %s", exc.code)
                return
            if self._enrollment(status) in ENROLLED_STATES:
                self._pairing.close()
            elif node_identity.is_personal() and not self._quarantined:
                await asyncio.to_thread(self._pairing.open)

    def _schedule_check(self) -> None:
        task = asyncio.get_running_loop().create_task(self._check_development_mesh(), name="node-mesh-development-check")
        self._checks.add(task)
        task.add_done_callback(self._checks.discard)

    async def _check_development_mesh(self) -> None:
        async with self._owner_lock:
            kernel = self._kernel
            if kernel is None or not self._started or self._quarantined:
                return
            try:
                if not await self._holds_development_mesh(kernel):
                    return
                self._personal_owner = None
                self._owner_binding = {"state": "refused", "code": DEVELOPMENT_MESH}
                await self._quarantine()
            except self._binding.KernelError as exc:
                logger.warning("node_mesh_development_check_failed: %s", _code_of(exc))

    async def _quarantine(self) -> None:
        if self._quarantined:
            return
        logger.warning("node_mesh_development_mesh_refused")
        try:
            await self.kernel.stop_network()
        finally:
            await self._stop_mailbox()
        self._quarantined = True

    async def stop(self) -> None:
        async with self._lock:
            if not self._started:
                return
            self._started = False
            self._personal_owner = None
            checks = [*self._checks, *self._pairing_checks]
            self._checks, self._pairing_checks = set(), set()
            for task in checks:
                task.cancel()
            await asyncio.gather(*checks, return_exceptions=True)
            try:
                await self._kernel.stop_network()
            finally:
                await self._stop_mailbox()

    async def _state(self) -> Any:
        return await self.kernel.mesh_status()

    @staticmethod
    def _enrollment(status: Any) -> str:
        return getattr(status.state, "name", str(status.state))

    def personal_owner_id(self) -> Optional[str]:
        return self._personal_owner if self._started else None

    async def refresh_personal_owner(self) -> Optional[str]:
        async with self._owner_lock:
            return await self._refresh_personal_owner()

    async def _refresh_personal_owner(self) -> Optional[str]:
        try:
            if not node_identity.is_personal() or self._data_dir.resolve() != node_identity.data_dir().resolve():
                raise NodeMeshError("node.personal_mesh_required")
            kernel = self.kernel
            status = await self._request_call(kernel.mesh_status)
            if self._enrollment(status) not in ENROLLED_STATES:
                try:
                    await asyncio.to_thread(node_identity.bound_mesh_owner)
                except node_identity.LegacyOwner:
                    raise NodeMeshError("node.legacy_owner") from None
                except node_identity.NodeIdentityError:
                    raise NodeMeshError("node.owner_binding_unavailable") from None
                self._personal_owner = None
                self._owner_binding = {"state": "setup_required", "code": "mesh.not_enrolled"}
                return None
            self._pairing.close()

            def bind():
                principal = getattr(kernel, "mesh_principal", None)
                if not callable(principal):
                    raise MeshUnavailable("principal_unsupported")
                proof = principal()
                mesh_id = getattr(proof, "mesh_id", None)
                if type(mesh_id) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", mesh_id) is None:
                    raise NodeMeshError("node.owner_unproven")
                roster = kernel.roster()
                refuse_development_mesh(roster)
                if mesh_id != status.mesh_id or mesh_id != roster.mesh_id:
                    raise NodeMeshError("node.mesh_mismatch")
                if roster.stopped:
                    raise NodeMeshError("gate.mesh_stopped")
                if not any(member.is_local and member.device_id == status.device_id for member in roster.members):
                    raise NodeMeshError("gate.device_not_on_roster")
                if getattr(principal(), "mesh_id", None) != mesh_id or self.kernel is not kernel or not self._started:
                    raise NodeMeshError("node.mesh_admission_unavailable")
                try:
                    return node_identity.bind_mesh_owner(mesh_id)
                except node_identity.LegacyOwner:
                    raise NodeMeshError("node.legacy_owner") from None
                except node_identity.OwnerMismatch:
                    raise NodeMeshError("node.owner_mismatch") from None
                except node_identity.NodeIdentityError:
                    raise NodeMeshError("node.owner_binding_unavailable") from None

            owner = await self._request_call(bind)
            if self.kernel is not kernel or not self._started:
                raise NodeMeshError("node.mesh_admission_unavailable")
            self._personal_owner = owner
            self._owner_binding = {"state": "ready", "code": None}
            return owner
        except NodeMeshError as exc:
            self._personal_owner = None
            self._owner_binding = {"state": "refused", "code": exc.code}
            if exc.code == DEVELOPMENT_MESH:
                await self._quarantine()
            return None

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
            "owner_binding": dict(self._owner_binding),
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

    async def serve_offer(self, presented: str) -> Dict[str, Any]:
        if not node_identity.is_personal():
            raise NodeMeshError("node_profile_inactive", 404)
        async with self._offer_lock:
            if await asyncio.to_thread(self._pairing.matches, presented):
                status = await self._request_call(self.kernel.mesh_status)
            else:
                try:
                    status = await self._request_call(self.kernel.mesh_status)
                except NodeMeshError:
                    raise NodeMeshError("node.pairing_secret_required", 401) from None
                if self._enrollment(status) not in ENROLLED_STATES:
                    await asyncio.to_thread(self._pairing.ensure)
                    raise NodeMeshError("node.pairing_secret_required", 401)
            try:
                owner = node_identity.bound_owner()
            except node_identity.NodeIdentityError:
                raise NodeMeshError("node.owner_binding_unavailable", 503) from None
            if owner is not None and not node_identity.is_mesh_owner(owner):
                raise NodeMeshError("node.legacy_owner", 409)
            if self._enrollment(status) in ENROLLED_STATES:
                self._pairing.close()
                return {"state": "enrolled", "mesh_id": status.mesh_id, "member_id": status.device_id}
            now = time.monotonic()
            self._served = [served for served in self._served if now - served < OFFER_WINDOW_SECONDS]
            if len(self._served) >= OFFERS_PER_WINDOW:
                raise NodeMeshError("node.pairing_rate_limited", 429)
            offer = await self.pairing_offer()
            self._served.append(now)
            await asyncio.to_thread(self._pairing.rotate)
            return {"state": "offer", "code": offer["code"], "payload": offer["payload"], "expires_ms": offer["expires_ms"]}

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

    async def peer_standing(self, device_id: str) -> Optional[PeerStanding]:
        roster = await asyncio.to_thread(self.kernel.roster)
        for device in roster.members:
            if device.device_id == device_id:
                return PeerStanding(device_id, bool(device.phi))
        return None

    async def sync_peers(self) -> List[Any]:
        return list(await self.kernel.sync_state())

    async def pending_requests(self) -> List[Any]:
        return await asyncio.to_thread(self.kernel.pending_requests_for_me)

    async def chat_requests(self, chat_id: str) -> List[Any]:
        return await asyncio.to_thread(self.kernel.chat_requests, chat_id)

    async def request(self, request_id: str) -> Any:
        return await self._request_call(self.kernel.request, request_id)

    def require_request_runtime(self, kernel: Any) -> None:
        if self.kernel is not kernel:
            raise NodeMeshError("node.mesh_admission_unavailable")
        capability = getattr(kernel, "supports_node_request_tools", None)
        if (not callable(capability) or capability() is not True
                or not callable(getattr(kernel, "answer_request", None))
                or not callable(getattr(kernel, "interrupt_request", None))):
            raise MeshUnavailable("node.request_runtime_unsupported")

    async def answer_request(self, request_id: str, kernel: Any) -> Any:
        self.require_request_runtime(kernel)
        try:
            return await kernel.answer_request(request_id)
        except self._binding.KernelError as exc:
            raise NodeMeshError(_code_of(exc)) from exc

    async def interrupt_request(self, request_id: str, kernel: Any) -> bool:
        if self.kernel is not kernel:
            raise NodeMeshError("node.mesh_admission_unavailable")
        return await self._request_call(kernel.interrupt_request, request_id)

    async def claim_request(self, request_id: str) -> str:
        return await self._request_call(self.kernel.claim_request, request_id)

    async def personal_authority_context(
        self, request: RequestReference, owner: str, kernel: Any,
    ) -> PersonalAuthorityContext:
        return await self._personal_context(request, owner, kernel, claimed=True)

    async def personal_request_context(
        self, request: RequestReference, owner: str, kernel: Any,
    ) -> PersonalAuthorityContext:
        self.require_request_runtime(kernel)
        return await self._personal_context(request, owner, kernel, claimed=False)

    async def _personal_context(
        self, request: RequestReference, owner: str, kernel: Any, *, claimed: bool,
    ) -> PersonalAuthorityContext:
        if self.kernel is not kernel:
            raise NodeMeshError("node.mesh_admission_unavailable")
        try:
            status = await kernel.mesh_status()
        except self._binding.KernelError as exc:
            raise NodeMeshError(_code_of(exc)) from exc

        def inspect():
            if self.kernel is not kernel:
                raise NodeMeshError("node.mesh_admission_unavailable")
            principal = getattr(kernel, "mesh_principal", None)
            if not callable(principal):
                raise MeshUnavailable("principal_unsupported")
            principal()
            roster = kernel.roster()
            current = kernel.request(request.id)
            proof = principal()
            if self.kernel is not kernel:
                raise NodeMeshError("node.mesh_admission_unavailable")
            mesh_id = getattr(proof, "mesh_id", None)
            if type(mesh_id) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", mesh_id) is None:
                raise NodeMeshError("node.owner_unproven")
            if self._enrollment(status) not in ENROLLED_STATES:
                raise NodeMeshError("mesh.not_enrolled")
            refuse_development_mesh(roster)
            if proof.mesh_id != status.mesh_id or proof.mesh_id != roster.mesh_id:
                raise NodeMeshError("node.mesh_mismatch")
            if roster.stopped:
                raise NodeMeshError("gate.mesh_stopped")
            if not any(member.is_local and member.device_id == status.device_id for member in roster.members):
                raise NodeMeshError("gate.device_not_on_roster")
            if not any(member.device_id == request.from_device for member in roster.members):
                raise NodeMeshError("ticket.device_removed")
            if request.to_device != status.device_id or RequestReference.capture(current) != request:
                raise NodeMeshError("request.call_mismatch")
            state = getattr(current.state, "name", None)
            if claimed:
                if state != "CLAIMED":
                    raise NodeMeshError("request.not_claimed")
            else:
                if request.call is not None:
                    raise NodeMeshError("request.call_mismatch")
                if state == "ANSWERED":
                    view = kernel.chat(request.chat_id).view
                    if not getattr(current, "claim", None) or not any(turn.id == current.turn and turn.author == current.to_device
                               and getattr(turn.status, "name", None) == "RUNNING" for turn in view.turns):
                        raise NodeMeshError("request.closed")
                elif state != "PENDING":
                    raise NodeMeshError("request.closed")
            if owner != node_identity.owner_id_for_mesh(mesh_id):
                raise NodeMeshError("node.owner_mismatch")
            return PersonalAuthorityContext(proof.mesh_id, status.device_id, request)

        try:
            return await self._request_call(inspect)
        except NodeMeshError as exc:
            if exc.code == DEVELOPMENT_MESH:
                await self._quarantine()
            raise

    async def refuse_request(self, request_id: str, code: str) -> str:
        return await self._request_call(self.kernel.refuse_request, request_id, code)

    async def grants(self) -> List[Any]:
        return list(await self._request_call(self.kernel.grants))

    async def verify_ticket(self, ticket_json: str, action_json: str, effect_kind: str) -> Any:
        return await self._request_call(self.kernel.verify_ticket, ticket_json, action_json, effect_kind)

    async def check_host_call(self, request_id: str, current_definition: str, effect_kind: str) -> Any:
        check = getattr(self.kernel, "check_host_call", None)
        if not callable(check):
            raise MeshUnavailable("host_call_check_unsupported")
        return await self._request_call(check, request_id, current_definition, effect_kind)

    async def claim_host_call(self, request_id: str, current_definition: str, effect_kind: str) -> Any:
        claim = getattr(self.kernel, "claim_host_call", None)
        if not callable(claim):
            raise MeshUnavailable("host_call_claim_unsupported")
        return await self._request_call(claim, request_id, current_definition, effect_kind)

    async def mint_ticket(
        self, request_id: str, *, call_id: str, tool: str, arguments_json: str,
        tool_def: str, effect_kind: str, labels: tuple, untrusted_arguments: bool,
    ) -> Any:
        mint = getattr(self.kernel, "mint_ticket_for_request", None)
        if mint is None:
            raise MeshUnavailable("mint_unsupported")
        call = self._binding.MintCall(
            call_id=call_id, tool=tool, arguments_json=arguments_json,
            tool_def_hash=tool_def, effect_kind=effect_kind,
            labels=self._peer_labels(labels), influence=[],
            untrusted_arguments=untrusted_arguments, consent_json=None,
        )
        return await self._request_call(mint, request_id, call)

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

    async def publish_tool_catalog(self, tools: Sequence[Dict[str, Any]]) -> str:
        publish = getattr(self._kernel, PUBLISH_EXPORT, None)
        if publish is None:
            raise MeshUnavailable("catalog_unsupported")
        binding = self._binding
        specs = [
            binding.LocalToolSpec(
                name=tool["name"],
                description=tool["description"],
                input_schema_json=json.dumps(tool["input_schema"], sort_keys=True),
                annotations_json=json.dumps(tool["annotations"], sort_keys=True),
                effect=getattr(binding.LocalToolEffect, tool["effect"].upper()),
                confidentiality=binding.LocalToolConfidentiality.PRIVATE,
            )
            for tool in tools
        ]
        return await self._request_call(publish, specs)

    async def _request_call(self, function: Callable[..., Any], *arguments: Any) -> Any:
        try:
            if inspect.iscoroutinefunction(function):
                return await function(*arguments)
            result = await asyncio.to_thread(function, *arguments)
            return await result if inspect.isawaitable(result) else result
        except self._binding.KernelError as exc:
            raise NodeMeshError(_code_of(exc)) from exc

    async def sync_now(self) -> None:
        if self._quarantined:
            raise NodeMeshError(DEVELOPMENT_MESH)
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
