"""A threaded HTTP or HTTPS server on 127.0.0.1 that hands each request to a plain handler function.
The OAuth, MCP and model fixtures are built on it, so none of them opens a socket of its own.
"""

from __future__ import annotations

import json
import ssl
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Request:
    method: str
    target: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> dict:
        try:
            parsed = json.loads(self.body or b"{}")
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}


@dataclass
class Response:
    status: int = 200
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    drop: bool = False

    @staticmethod
    def of_json(body: object, status: int = 200, headers: list[tuple[str, str]] | None = None) -> Response:
        return Response(status, [("content-type", "application/json"), *(headers or [])], json.dumps(body).encode())


Handler = Callable[[Request], Response]


class Loopback:
    def __init__(self, handler: Handler, tls: tuple[str, str] | None = None) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _bind(handler))
        self._server.daemon_threads = True
        self.scheme = "http"
        if tls is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls)
            self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
            self.scheme = "https"
        self.port = self._server.server_port
        self.url = f"{self.scheme}://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


def _bind(handler: Handler) -> type[BaseHTTPRequestHandler]:
    class Bound(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: object) -> None:
            return

        def _serve(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            request = Request(
                self.command,
                self.path,
                {name.lower(): value for name, value in self.headers.items()},
                self.rfile.read(length) if length else b"",
            )
            response = handler(request)
            if response.drop:
                self.close_connection = True
                return
            self.send_response(response.status)
            for name, value in response.headers:
                self.send_header(name, value)
            self.send_header("content-length", str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)

        do_GET = do_POST = do_PUT = do_DELETE = _serve

    return Bound
