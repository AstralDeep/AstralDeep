"""A fake of the kernel's Python binding that keeps chats in memory and lets a test join the node to a mesh, so node mesh code is tested without the native library.
It mirrors the names and shapes of the generated binding used by orchestrator/node_mesh.py and implements append_chat_events as the node's turn writer needs it."""

from __future__ import annotations

import enum
import itertools
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, List, Optional


class HostSigner:
    pass


class KernelListener:
    pass


class ConsentKind(enum.Enum):
    ROSTER_ADD = 0
    ROSTER_REMOVE = 1
    STOP = 2
    TOOL_EFFECT = 3
    OTHER = 4


class DeviceKind(enum.Enum):
    DESKTOP = 0
    NODE = 3


class EnrollmentState(enum.Enum):
    NOT_ENROLLED = 0
    ENROLLED = 3


class HostSignerError(Exception):
    class Denied(Exception):
        def __init__(self, message):
            super().__init__(message)
            self.message = message


class KernelError(Exception):
    pass


class _State(KernelError):
    def __init__(self, code, message=""):
        super().__init__(code)
        self.code = code
        self.message = message


KernelError.State = _State


class RelayChoice:
    class DEFAULT:
        pass

    class DISABLED:
        pass

    class CUSTOM:
        def __init__(self, urls):
            self.urls = urls


@dataclass
class NetworkConfig:
    relay: Any
    local_discovery: bool
    internet_lookup: bool
    loopback_only: bool
    sync_interval_ms: int
    mailbox: Optional[str]


@dataclass
class IssuerConfig:
    issuer: str
    client_id: str
    jwks_json: str


@dataclass
class KernelConfig:
    data_dir: str
    operations_secret: bytes
    wrap_secret: bytes
    restore_marker: Optional[bytes]
    device_label: str
    device_kind: Any
    issuers: List[Any]
    network: Any


@dataclass
class NewChatEvent:
    kind: str
    body_json: str


class CHAT_CHANGED:
    pass


def new_operations_secret() -> bytes:
    return bytes(range(32))


def operations_public_key(secret: bytes) -> str:
    return "pk-" + secret[:4].hex()


class Kernel:
    instances: List["Kernel"] = []

    def __init__(self, config, signer, listener):
        self.config = config
        self.signer = signer
        self.listener = listener
        self.enrolled = False
        self.mesh_id: Optional[str] = None
        self.device_id = "dev-node"
        self.network = False
        self.offers = 0
        self.cancelled = 0
        self.chats: dict = {}
        self.appended: List[tuple] = []
        self.sent: List[tuple] = []
        self.created: List[tuple] = []
        self.address_error: Optional[Exception] = None
        self._ids = itertools.count(1)
        Kernel.instances.append(self)

    @classmethod
    def open(cls, config, signer, model=None, tools=None, listener=None):
        return cls(config, signer, listener)

    def join(self, mesh_id: str = "mesh-1") -> None:
        self.enrolled = True
        self.mesh_id = mesh_id

    def add_chat(self, chat_id: str, messages: List[Any], turns: Optional[List[Any]] = None) -> None:
        self.chats[chat_id] = SimpleNamespace(messages=messages, turns=turns or [])

    def notify(self) -> None:
        if self.listener is not None:
            self.listener.on_change(CHAT_CHANGED())

    def version(self):
        return "0.0-fake"

    async def start_network(self):
        self.network = True
        return "node-endpoint"

    async def stop_network(self):
        self.network = False

    async def endpoint_address(self):
        if self.address_error is not None:
            raise self.address_error
        return '{"id":"endpoint-1","addrs":["127.0.0.1:1"]}'

    async def mesh_status(self):
        return SimpleNamespace(
            state=EnrollmentState.ENROLLED if self.enrolled else EnrollmentState.NOT_ENROLLED,
            mesh_id=self.mesh_id,
            device_id=self.device_id,
            members=2 if self.enrolled else 1,
            hardware_backed=False,
            pairing_open=self.offers > self.cancelled,
        )

    async def create_pairing_offer(self):
        if self.enrolled:
            raise KernelError.State("pairing.already_enrolled")
        self.offers += 1
        return SimpleNamespace(code="123-456-789", payload="payload-" + str(self.offers), expires_ms=1234)

    def cancel_pairing_offer(self):
        self.cancelled += 1

    async def sync_now(self):
        return None

    def list_chats(self):
        return [SimpleNamespace(chat_id=chat_id) for chat_id in self.chats]

    def chat(self, chat_id):
        entry = self.chats[chat_id]
        return SimpleNamespace(
            view=SimpleNamespace(chat_id=chat_id, messages=entry.messages, turns=entry.turns)
        )

    def create_chat(self, title):
        chat_id = f"chat-{next(self._ids)}"
        self.chats[chat_id] = SimpleNamespace(messages=[], turns=[])
        self.created.append((chat_id, title))
        return chat_id

    def send_message(self, chat_id, text, continues):
        message_id = f"msg-{next(self._ids)}"
        self.sent.append((chat_id, text, continues, message_id))
        return message_id

    def append_chat_events(self, chat_id, events):
        ids = [f"ev-{next(self._ids)}" for _ in events]
        self.appended.append((chat_id, [(e.kind, e.body_json) for e in events]))
        return ids


def message(message_id, text, *, role="USER", status="QUEUED", author="dev-phone", branch=None, kind="chat.user_message"):
    return SimpleNamespace(
        id=message_id,
        text=text,
        role=SimpleNamespace(name=role),
        status=SimpleNamespace(name=status),
        author=author,
        branch=branch,
        kind=kind,
    )


def turn(turn_id, *, status="RUNNING", author="dev-node", branch=None):
    return SimpleNamespace(id=turn_id, status=SimpleNamespace(name=status), author=author, branch=branch)


