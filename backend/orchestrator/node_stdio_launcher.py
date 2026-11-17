"""The node's launcher for local stdio MCP servers: it starts each server through a sandbox backend, serves the child's stdio to the kernel as JSON-RPC over a loopback HTTP endpoint guarded by a per-launch bearer, and stops the child and endpoint on removal.
Production installs no backend, so every launch refuses with connection.sandbox_unavailable; node_mesh.py installs it on the kernel and node_connections.py reports the refusal beside each stdio connection.
"""

from __future__ import annotations

import hmac
import http.server
import json
import logging
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Dict, Mapping, Optional, Protocol, Sequence, Tuple

logger = logging.getLogger("Orchestrator.NodeStdioLauncher")

SANDBOX_UNAVAILABLE = "connection.sandbox_unavailable"
RUNS_ELSEWHERE = "connection.runs_elsewhere"
ENDPOINT_PATH = "/mcp"
LOOPBACK = "127.0.0.1"
MAX_REQUEST_BYTES = 1 << 20
MAX_RESPONSE_BYTES = 16 << 20
REPLY_SECONDS = 60.0
STOP_SECONDS = 5.0


class LaunchRefused(Exception):
    def __init__(self, code: str = SANDBOX_UNAVAILABLE):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LaunchSpec:
    id: str
    command: str
    args: Tuple[str, ...]
    env: Mapping[str, str] = field(repr=False)
    egress: Tuple[str, ...]
    policy: Mapping[str, Any]
    policy_digest: str


class SandboxedProcess(Protocol):
    stdin: BinaryIO
    stdout: BinaryIO

    def poll(self) -> Optional[int]: ...

    def stop(self) -> None: ...


class SandboxBackend(Protocol):
    def spawn(self, spec: LaunchSpec) -> SandboxedProcess: ...


class _Bridge:
    def __init__(self, process: SandboxedProcess, bearer: str):
        self.process = process
        self.bearer = bearer
        self.pipe = threading.Lock()
        self.server: Optional[http.server.ThreadingHTTPServer] = None

    def authorized(self, header: Optional[str]) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[len("Bearer "):].encode(), self.bearer.encode())

    def exchange(self, request: Dict[str, Any]) -> Optional[bytes]:
        line = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        with self.pipe:
            if self.process.poll() is not None:
                raise BrokenPipeError("exited")
            self.process.stdin.write(line)
            self.process.stdin.flush()
            if "id" not in request:
                return None
            return self._reply_to(request["id"])

    def _reply_to(self, ident: Any) -> bytes:
        result: Dict[str, Any] = {}

        def read() -> None:
            try:
                while True:
                    raw = self.process.stdout.readline(MAX_RESPONSE_BYTES + 1)
                    if not raw or len(raw) > MAX_RESPONSE_BYTES:
                        return
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(message, dict) and message.get("id") == ident and ("result" in message or "error" in message):
                        result["line"] = raw.strip()
                        return
            except (OSError, ValueError):
                return

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(REPLY_SECONDS)
        if reader.is_alive():
            self.process.stop()
            reader.join(STOP_SECONDS)
            raise TimeoutError("reply")
        if "line" not in result:
            raise BrokenPipeError("closed")
        return result["line"]


def _handler(bridge: _Bridge, port_of: Any) -> type:
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:
            return

        def _send(self, status: int, body: bytes = b"", content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _refuse(self, status: int) -> None:
            self.close_connection = True
            self._send(status)

        def do_POST(self) -> None:
            if self.path != ENDPOINT_PATH:
                return self._refuse(404)
            if self.headers.get("Origin") is not None or self.headers.get("Host") != f"{LOOPBACK}:{port_of()}":
                return self._refuse(403)
            if not bridge.authorized(self.headers.get("Authorization")):
                return self._refuse(401)
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._refuse(411)
            if length < 0 or length > MAX_REQUEST_BYTES:
                return self._refuse(413)
            try:
                request = json.loads(self.rfile.read(length))
            except ValueError:
                return self._refuse(400)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                return self._refuse(400)
            try:
                reply = bridge.exchange(request)
            except TimeoutError:
                return self._refuse(504)
            except (OSError, ValueError):
                return self._refuse(502)
            if reply is None:
                return self._send(202)
            self._send(200, reply)

        def do_GET(self) -> None:
            self._refuse(405)

        do_PUT = do_DELETE = do_PATCH = do_GET

    return Handler


class NodeStdioLauncher:
    def __init__(self, backend: Optional[SandboxBackend] = None):
        self._backend = backend
        self._bridges: Dict[str, _Bridge] = {}
        self._lock = threading.Lock()

    @property
    def refusal(self) -> Optional[str]:
        return None if self._backend is not None else SANDBOX_UNAVAILABLE

    def start(self, spec: LaunchSpec) -> Tuple[str, str]:
        if self._backend is None:
            raise LaunchRefused(SANDBOX_UNAVAILABLE)
        self.stop(spec.id)
        try:
            process = self._backend.spawn(spec)
        except Exception as exc:
            logger.warning("node_stdio_spawn_failed id=%s kind=%s", spec.id, type(exc).__name__)
            raise LaunchRefused(SANDBOX_UNAVAILABLE) from exc
        bearer = secrets.token_urlsafe(32)
        bridge = _Bridge(process, bearer)
        try:
            server = http.server.ThreadingHTTPServer((LOOPBACK, 0), _handler(bridge, lambda: server.server_address[1]))
        except OSError as exc:
            process.stop()
            raise LaunchRefused(SANDBOX_UNAVAILABLE) from exc
        server.daemon_threads = True
        bridge.server = server
        threading.Thread(target=server.serve_forever, name=f"node-stdio-{spec.id[:8]}", daemon=True).start()
        with self._lock:
            self._bridges[spec.id] = bridge
        logger.info("node_stdio_started id=%s", spec.id)
        return f"http://{LOOPBACK}:{server.server_address[1]}{ENDPOINT_PATH}", bearer

    def stop(self, connection_id: str) -> None:
        with self._lock:
            bridge = self._bridges.pop(connection_id, None)
        if bridge is None:
            return
        if bridge.server is not None:
            bridge.server.shutdown()
            bridge.server.server_close()
        bridge.process.stop()
        logger.info("node_stdio_stopped id=%s", connection_id)

    def stop_all(self) -> None:
        with self._lock:
            held = list(self._bridges)
        for connection_id in held:
            self.stop(connection_id)

    def running(self) -> Sequence[str]:
        with self._lock:
            return tuple(self._bridges)


def spec_of(request: Any) -> LaunchSpec:
    try:
        policy = json.loads(request.policy_json)
    except ValueError as exc:
        raise LaunchRefused(SANDBOX_UNAVAILABLE) from exc
    return LaunchSpec(
        id=request.id,
        command=request.command,
        args=tuple(request.args),
        env={entry.name: entry.value for entry in request.env},
        egress=tuple(request.egress),
        policy=policy,
        policy_digest=request.policy_digest,
    )


def host_launcher(binding: Any, launcher: NodeStdioLauncher) -> Any:
    class Host(binding.HostLauncher):  # type: ignore[name-defined, misc]
        def start(self, request):
            try:
                url, bearer = launcher.start(spec_of(request))
            except LaunchRefused as exc:
                raise binding.HostLauncherError.Failed(exc.code) from exc
            return binding.HostLaunchedServer(url=url, bearer=bearer)

        def stop(self, id):
            launcher.stop(id)

    return Host()
