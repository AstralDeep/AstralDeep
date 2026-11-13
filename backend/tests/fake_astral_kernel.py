"""A fake of the kernel's Python binding that keeps chats and requests in memory and lets a test join the node to a mesh, so node mesh code is tested without the native library.
It mirrors the names and shapes of the generated binding used by orchestrator/node_mesh.py, including the request states, peer turn recording rules and ticket verification errors."""

from __future__ import annotations

import enum
import itertools
from dataclasses import dataclass
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


def _variant(name):
    def init(self, code, message=""):
        KernelError.__init__(self, code)
        self.code = code
        self.message = message

    return type(name, (KernelError,), {"__init__": init})


for _name in ("State", "InvalidInput", "Verification", "Store", "Network", "Custody", "Denied", "Mesh"):
    setattr(KernelError, _name, _variant(_name))


class PeerConfidentiality(enum.Enum):
    PUBLIC = 0
    PRIVATE = 1
    PHI = 2
    SECRET = 3


class PeerIntegrity(enum.Enum):
    OWNER = 0
    TOOL_TRUSTED = 1
    UNTRUSTED = 2


class RequestState(enum.Enum):
    PENDING = 0
    CLAIMED = 1
    ANSWERED = 2
    REFUSED = 3
    CANCELLED = 4


@dataclass
class PeerLabels:
    confidentiality: Any
    integrity: Any


@dataclass
class PeerCall:
    call_id: str
    tool: str
    arguments_json: str
    action_json: str
    ticket_json: str


@dataclass
class PeerToolStep:
    call_id: str
    tool: str
    arguments_json: str
    tool_def: str
    reasons: List[str]
    receipt: Optional[str]
    consent: Any
    status: str
    content: str
    code: Optional[str]
    labels: Any


@dataclass
class PeerTurn:
    continues: Optional[str]
    message: Optional[str]
    answers: Optional[str]
    provider: str
    model: str
    labels: Any
    steps: List[Any]
    text: str
    reason: str
    code: Optional[str]


@dataclass
class RequestView:
    id: str
    chat_id: str
    from_device: Optional[str]
    to_device: str
    state: Any
    claim: Optional[str]
    turn: Optional[str]
    code: Optional[str]
    text: str
    continues: Optional[str]
    call: Optional[Any]
    labels: Any
    clock: Any = None


@dataclass
class VerifiedTicket:
    mesh: str
    device: str
    feed: str
    stop_counter: int
    turn: str
    agents: List[str]
    action_digest: str
    labels: Any
    sources: List[str]
    consented: bool


def peer_labels(confidentiality="PRIVATE", integrity="OWNER"):
    return PeerLabels(PeerConfidentiality[confidentiality], PeerIntegrity[integrity])


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
        self.recorded: List[tuple] = []
        self.requests: dict = {}
        self.refused: List[tuple] = []
        self.claimed: List[str] = []
        self.verified: List[tuple] = []
        self.ticket_error: Optional[str] = None
        self.ticket_labels = peer_labels()
        self.record_error: Optional[str] = None
        self.floor = ("PUBLIC", "OWNER")
        self.phi_member = False
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

    def roster(self):
        return SimpleNamespace(
            members=[SimpleNamespace(device_id=self.device_id, is_local=True, phi=self.phi_member),
                     SimpleNamespace(device_id="dev-phone", is_local=False, phi=True)]
        )

    def add_request(self, request_id, chat_id, text="do the thing", *, to=None, author="dev-phone", state="PENDING",
                    continues=None, call=None, labels=None):
        view = RequestView(
            id=request_id, chat_id=chat_id, from_device=author, to_device=to or self.device_id,
            state=RequestState[state], claim=None, turn=None, code=None, text=text, continues=continues,
            call=call, labels=labels or peer_labels(),
        )
        self.requests[request_id] = view
        return view

    def pending_requests_for_me(self):
        return [r for r in self.requests.values() if r.state == RequestState.PENDING and r.to_device == self.device_id]

    def chat_requests(self, chat_id):
        return [r for r in self.requests.values() if r.chat_id == chat_id]

    def request(self, request_id):
        if request_id not in self.requests:
            raise KernelError.InvalidInput("request.unknown", "no such request")
        return self.requests[request_id]

    def claim_request(self, request_id):
        view = self.request(request_id)
        if view.state == RequestState.CLAIMED:
            return view.claim
        if view.state != RequestState.PENDING:
            raise KernelError.State(f"request.{view.state.name.lower()}", "closed")
        view.state = RequestState.CLAIMED
        view.claim = f"claim-{next(self._ids)}"
        self.claimed.append(request_id)
        return view.claim

    def refuse_request(self, request_id, code):
        view = self.request(request_id)
        if view.state not in (RequestState.PENDING, RequestState.CLAIMED):
            raise KernelError.State(f"request.{view.state.name.lower()}", "closed")
        view.state = RequestState.REFUSED
        view.code = code
        self.refused.append((request_id, code))
        return f"ev-{next(self._ids)}"

    def verify_ticket(self, ticket_json, action_json, effect_kind):
        self.verified.append((ticket_json, action_json, effect_kind))
        if self.ticket_error is not None:
            raise KernelError.Verification(self.ticket_error, "refused")
        import json

        action = json.loads(action_json)
        return VerifiedTicket(
            mesh=action.get("mesh", self.mesh_id), device=action.get("device", "dev-phone"), feed="feed", stop_counter=0,
            turn=action.get("turn", "turn-x"), agents=["kernel"], action_digest="sha256:" + "0" * 64,
            labels=self.ticket_labels, sources=[], consented=False,
        )

    def record_peer_turn(self, chat_id, turn):
        if self.record_error is not None:
            raise KernelError.InvalidInput(self.record_error, "refused")
        floor_c, floor_i = self.floor
        rank_c = [c.name for c in PeerConfidentiality]
        rank_i = [i.name for i in PeerIntegrity]
        if rank_c.index(turn.labels.confidentiality.name) < rank_c.index(floor_c) or rank_i.index(turn.labels.integrity.name) < rank_i.index(floor_i):
            raise KernelError.InvalidInput("peer_turn.labels_too_low", "too low")
        if turn.labels.confidentiality == PeerConfidentiality.SECRET:
            raise KernelError.InvalidInput("peer_turn.secret_label", "secret")
        if turn.answers is not None:
            view = self.request(turn.answers)
            if view.state != RequestState.CLAIMED:
                raise KernelError.State("request.not_claimed", "claim first")
            view.state = RequestState.ANSWERED
        turn_id = f"turn-{next(self._ids)}"
        if turn.answers is not None:
            view.turn = turn_id
        entry = self.chats.setdefault(chat_id, SimpleNamespace(messages=[], turns=[]))
        entry.turns.append(SimpleNamespace(id=turn_id, status=SimpleNamespace(name="COMPLETE"), author=self.device_id, branch=None))
        if turn.text:
            entry.messages.append(message(f"asst-{turn_id}", turn.text, role="ASSISTANT", status="COMPLETE", author=self.device_id, turn=turn_id))
        self.recorded.append((chat_id, turn))
        return turn_id


def message(message_id, text, *, role="USER", status="COMPLETE", author="dev-phone", branch=None, kind="chat.user_message",
            turn=None, confidentiality="private", integrity="owner"):
    return SimpleNamespace(
        id=message_id,
        text=text,
        role=SimpleNamespace(name=role),
        status=SimpleNamespace(name=status),
        author=author,
        branch=branch,
        kind=kind,
        turn=turn,
        labels=SimpleNamespace(confidentiality=confidentiality, integrity=integrity),
    )


def turn(turn_id, *, status="RUNNING", author="dev-node", branch=None):
    return SimpleNamespace(id=turn_id, status=SimpleNamespace(name=status), author=author, branch=branch)


