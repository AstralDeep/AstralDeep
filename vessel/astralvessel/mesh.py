# The executor's membership in the owner's mesh, through the kernel's Python binding: it learns the node's mailbox,
# opens its own kernel there, offers pairing to the owner, and verifies a ticket against its synced roster and clock.
# executor.py asks it to verify every call before the sandbox runs it, and refuses everything while it is not enrolled.

from __future__ import annotations

import asyncio
import importlib
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .keys import ExecutorKeys, PresenceRefused

log = logging.getLogger("astralvessel")
BINDING = "astral_kernel"
ENROLLED = {"ENROLLED", "RESTORED"}
DOTTED = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
SYNC_INTERVAL_MS = 2000
CONSENT_SYNC_SECONDS = 3 * SYNC_INTERVAL_MS / 1000
CONSENT_RECHECK_SECONDS = 0.25
PENDING_LINEAGE = "ticket.consent_sync_pending"


class Refused(Exception):
    # A refusal with a dotted code, for the caller and the audit trail; never carries kernel internals.
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def dotted(code: object, prefix: str = "executor") -> str:
    text = str(code or "").lower()
    if DOTTED.fullmatch(text):
        return text[:80]
    return f"{prefix}.{re.sub(r'[^a-z0-9_]+', '_', text).strip('_') or 'failed'}"[:80]


@dataclass(frozen=True)
class Verified:
    mesh: str
    device: str
    turn: str
    action_digest: str
    expires_s: int
    agents: tuple[str, ...] = ()


def load_binding() -> Any:
    try:
        return importlib.import_module(BINDING)
    except ImportError:
        return None


class KernelVerifier:
    def __init__(self, data_dir: Path, *, label: str = "executor", binding: Any = None,
                 mailbox: Callable[[], Awaitable[str | None]], interval: float = 5.0, loopback: bool = False):
        self._data_dir = Path(data_dir)
        self._label = label
        self._binding = binding if binding is not None else load_binding()
        self._mailbox = mailbox
        self._interval = interval
        self._loopback = loopback
        self._kernel: Any = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._binding is None:
            log.warning("the kernel binding is not installed, so every ticket is refused")
            return
        self._task = asyncio.create_task(self._open_when_the_node_has_a_mailbox())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._kernel is not None:
            await self._kernel.stop_network()
            self._kernel = None

    async def _open_when_the_node_has_a_mailbox(self) -> None:
        # The mailbox exists once the node has joined the owner's mesh; the executor joins through it.
        address = None
        while not self._loopback:
            address = await self._mailbox()
            if address:
                break
            await asyncio.sleep(self._interval)
        binding = self._binding
        keys = await asyncio.to_thread(ExecutorKeys.load_or_create, self._data_dir,
                                       new_secret=lambda: bytes(binding.new_operations_secret()))
        config = binding.KernelConfig(
            data_dir=str(self._data_dir / "kernel"), operations_secret=keys.operations_secret,
            wrap_secret=keys.wrap_secret, restore_marker=keys.restore_marker, device_label=self._label,
            device_kind=binding.DeviceKind.NODE,
            network=binding.NetworkConfig(relay=binding.RelayChoice.DISABLED(), local_discovery=False,
                                          internet_lookup=False, loopback_only=self._loopback,
                                          sync_interval_ms=SYNC_INTERVAL_MS, mailbox=address))
        (self._data_dir / "kernel").mkdir(mode=0o700, parents=True, exist_ok=True)
        kernel = await asyncio.to_thread(binding.Kernel.open, config, _signer(binding, keys), None, None,
                                        _listener(binding))
        await kernel.start_network()
        self._kernel = kernel

    async def state(self) -> str:
        if self._binding is None:
            return "no_kernel"
        if self._kernel is None:
            return "waiting_for_node"
        status = await self._kernel.mesh_status()
        return "enrolled" if getattr(status.state, "name", str(status.state)) in ENROLLED else "not_enrolled"

    async def mesh_id(self) -> str | None:
        if await self.state() != "enrolled":
            return None
        return (await self._kernel.mesh_status()).mesh_id

    async def pairing_offer(self) -> dict[str, Any]:
        state = await self.state()
        if state != "not_enrolled":
            raise Refused(f"executor.{state}")
        try:
            offer = await self._kernel.create_pairing_offer()
        except self._binding.KernelError as exc:
            raise Refused(dotted(getattr(exc, "code", ""))) from None
        return {"code": offer.code, "payload": offer.payload, "expires_ms": int(offer.expires_ms)}

    async def verify(self, ticket_json: str, action_json: str, effect: str) -> Verified:
        kernel = self._kernel
        if kernel is None:
            raise Refused(f"executor.{await self.state()}")

        async def verify_current() -> Verified:
            state = await self.state()
            if state != "enrolled":
                raise Refused(f"executor.{state}")
            claims = await asyncio.to_thread(kernel.verify_ticket, ticket_json, action_json, effect)
            expiry = getattr(claims, "expires_s", None)
            if type(expiry) is not int or not 0 < expiry < 2**63 - 1:
                raise Refused("ticket.bad_expiry")
            return Verified(claims.mesh, claims.device, claims.turn, claims.action_digest, expiry,
                            tuple(getattr(claims, "agents", ()) or ()))

        try:
            return await verify_current()
        except self._binding.KernelError as exc:
            code = dotted(getattr(exc, "code", ""), "ticket")
            if code != PENDING_LINEAGE:
                raise Refused(code) from None

        try:
            async with asyncio.timeout(CONSENT_SYNC_SECONDS):
                await kernel.sync_now()
                while True:
                    try:
                        return await verify_current()
                    except self._binding.KernelError as exc:
                        if dotted(getattr(exc, "code", ""), "ticket") != PENDING_LINEAGE:
                            raise
                    await asyncio.sleep(CONSENT_RECHECK_SECONDS)
        except TimeoutError:
            raise Refused(PENDING_LINEAGE) from None
        except self._binding.KernelError as exc:
            raise Refused(dotted(getattr(exc, "code", ""), "ticket")) from None


def _signer(binding: Any, keys: ExecutorKeys) -> Any:
    class ExecutorSigner(binding.HostSigner):
        def identity_public_key(self):
            return keys.identity_public_key()

        def identity_sign(self, message):
            return keys.identity_sign(bytes(message))

        def presence_public_key(self):
            return keys.presence_public_key()

        def hardware_backed(self):
            return False

        def presence_sign(self, prompt, message):
            kind = "OTHER" if prompt.kind == binding.ConsentKind.OTHER else str(prompt.kind)
            try:
                return keys.presence_sign(kind, bytes(message))
            except PresenceRefused as exc:
                raise binding.HostSignerError.Denied(str(exc)) from exc

    return ExecutorSigner()


def _listener(binding: Any) -> Any:
    class Quiet(binding.KernelListener):
        def on_change(self, change):
            return None

    return Quiet()
