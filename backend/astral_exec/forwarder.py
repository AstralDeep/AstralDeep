"""The per-call egress forwarder: a loopback TCP listener on 127.0.0.1:3128 inside the sandbox network namespace that relays each connection to the host's Unix egress socket.
inner.py binds the listener and forks this before it restricts the tool, so the forwarder runs outside Landlock and seccomp and is reaped when the namespace dies. The host-side proxy behind the Unix socket is a separate task.
"""

from __future__ import annotations

import errno
import selectors
import socket
import threading
import time
from collections.abc import Callable

from .constants import PROXY_PORT

LISTEN_HOST = "127.0.0.1"
BACKLOG = 64
CHUNK = 65536
MAX_RELAYS = 64
ACCEPT_BACKOFF_SECONDS = 0.05
_TRANSIENT = (
    errno.ECONNABORTED,
    errno.EMFILE,
    errno.ENFILE,
    errno.ENOBUFS,
    errno.ENOMEM,
)


def build_listener(host: str = LISTEN_HOST, port: int = PROXY_PORT) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen(BACKLOG)
    return listener


def relay(downstream: socket.socket, upstream: socket.socket) -> None:
    selector = selectors.DefaultSelector()
    pairs = {downstream: upstream, upstream: downstream}
    for end in pairs:
        selector.register(end, selectors.EVENT_READ)
    open_ends = set(pairs)
    try:
        while open_ends:
            for key, _events in selector.select():
                source = key.fileobj
                peer = pairs[source]
                try:
                    chunk = source.recv(CHUNK)
                except OSError:
                    chunk = b""
                if chunk:
                    try:
                        peer.sendall(chunk)
                    except OSError:
                        chunk = b""
                if not chunk:
                    selector.unregister(source)
                    open_ends.discard(source)
                    try:
                        peer.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
    finally:
        selector.close()
        for end in pairs:
            end.close()


def serve(
    listener: socket.socket,
    socket_path: str,
    connect: Callable[[str], socket.socket] | None = None,
    pause: Callable[[float], None] = time.sleep,
) -> None:
    connect = connect or _connect_unix
    slots = threading.BoundedSemaphore(MAX_RELAYS)

    def run(downstream: socket.socket, upstream: socket.socket) -> None:
        try:
            relay(downstream, upstream)
        finally:
            slots.release()

    while True:
        try:
            downstream, _address = listener.accept()
        except OSError as error:
            if error.errno in _TRANSIENT:
                pause(ACCEPT_BACKOFF_SECONDS)
                continue
            return
        if not slots.acquire(blocking=False):
            downstream.close()
            continue
        try:
            upstream = connect(socket_path)
        except OSError:
            downstream.close()
            slots.release()
            continue
        try:
            threading.Thread(
                target=run, args=(downstream, upstream), daemon=True
            ).start()
        except RuntimeError:
            downstream.close()
            upstream.close()
            slots.release()


def _connect_unix(socket_path: str) -> socket.socket:
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    upstream.connect(socket_path)
    return upstream
