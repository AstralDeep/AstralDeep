"""Bounded notification foreground leases are tied to authenticated UI connection
incarnations and current owner-scoped destination generations. notification_api
updates leases; orchestrator registration and disconnect control their lifetime.
"""

from dataclasses import dataclass
import time


LEASE_SECONDS = 45
MAX_CONNECTIONS = 4096
MAX_SEQUENCE = 2**53 - 1


class NotificationPresenceError(Exception):
    def __init__(self, code="notification_presence_conflict", status=409):
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass
class _Connection:
    websocket: object
    owner: str
    device: str
    generation: str
    sequence: int = 0
    expires: float = 0
    destination: tuple | None = None


class NotificationPresence:
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._connections: dict[int, _Connection] = {}

    def register(self, websocket, owner, device, generation):
        prior = self._connections.get(id(websocket))
        if prior is not None and (prior.owner, prior.device, prior.generation) == (owner, device, generation):
            return
        self.clear(websocket)
        if not all(isinstance(value, str) and value and len(value) <= 128 for value in (owner, device, generation)):
            return
        for socket_id, connection in list(self._connections.items()):
            if (connection.owner, connection.device) == (owner, device):
                self._connections.pop(socket_id, None)
        if len(self._connections) < MAX_CONNECTIONS:
            self._connections[id(websocket)] = _Connection(websocket, owner, device, generation)

    def clear(self, websocket):
        self._connections.pop(id(websocket), None)

    def _current(self, connection, sessions):
        claims = sessions.get(connection.websocket) or {}
        expiry = claims.get("exp")
        return (claims.get("sub") == connection.owner and type(expiry) in {int, float}
                and expiry > time.time() and claims.get("machine_class") != "scheduled")

    def update(self, owner, device, generation, sequence, foreground, sessions, destination_identity):
        if type(sequence) is not int or not 1 <= sequence <= MAX_SEQUENCE or type(foreground) is not bool:
            raise NotificationPresenceError("notification_presence_invalid", 422)
        connection = next((item for item in self._connections.values()
                           if (item.owner, item.device, item.generation) == (owner, device, generation)), None)
        if (connection is None or not self._current(connection, sessions)
                or sequence <= connection.sequence):
            raise NotificationPresenceError()
        connection.sequence = sequence
        connection.expires = 0
        if foreground:
            try:
                identity = destination_identity(owner, device)
            except Exception:
                raise NotificationPresenceError("notification_presence_unavailable", 503) from None
            if identity is not None and identity[0] != "active":
                raise NotificationPresenceError()
            connection.destination = identity
            connection.expires = self._clock() + LEASE_SECONDS
        return {"accepted_sequence": sequence, "expires_in_seconds": LEASE_SECONDS if foreground else 0}

    def active(self, owner, sessions, destination_identity):
        now = self._clock()
        for item in list(self._connections.values()):
            if item.owner != owner or item.expires <= now:
                continue
            if not self._current(item, sessions):
                item.expires = 0
                continue
            sequence = item.sequence
            try:
                identity = destination_identity(owner, item.device)
            except Exception:
                if item.sequence == sequence:
                    item.expires = 0
                continue
            if (self._connections.get(id(item.websocket)) is not item or item.sequence != sequence
                    or item.expires <= self._clock() or not self._current(item, sessions)):
                continue
            if identity != item.destination:
                item.expires = 0
                continue
            return True
        return False
