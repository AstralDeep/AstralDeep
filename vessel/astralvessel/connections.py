# The operator's connection commands: list the node's outside connections and sign one in from this machine. Each call
# runs as a small script inside the node's container, which reads the operator secret there and asks the node over its
# own loopback, so cli.py and operate.py never hold the secret.

from __future__ import annotations

import getpass
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .stack import Stack

LISTEN_HOST = "127.0.0.1"
CALLBACK_PATH = "/callback"
FLOW_WAIT = 600
MAX_URL = 8192
DEFAULT_INTERVAL = 7
MIN_INTERVAL = 7
MAX_INTERVAL = 60
SOCKET_TIMEOUT = 5
CONNECTION_ID = re.compile(r"[0-9a-f]{32}")
FLOW_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
CODE = re.compile(r"[A-Za-z0-9 -]{1,32}")
DETAIL = re.compile(r"[a-z0-9_.]{1,64}")
STATES = ("not_signed_in", "signed_in", "needs_attention")
NODE_CALL = r'''
import json, os, urllib.error, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def node_call(secret_file, header, method, path, data=None, content_type=None):
    directory = os.environ.get("ASTRAL_NODE_DATA_DIR", "/node-data/node")
    try:
        with open(os.path.join(directory, secret_file)) as handle:
            secret = handle.read().strip() or "absent"
    except OSError:
        secret = "absent"
    headers = {header: secret}
    if content_type:
        headers["Content-Type"] = content_type
    call = urllib.request.Request("http://127.0.0.1:%s%s" % (os.environ.get("ORCHESTRATOR_PORT", "8001"), path),
                                  data=data, method=method, headers=headers)
    retry = None
    try:
        status, raw = 200, opener.open(call, timeout=30).read()
    except urllib.error.HTTPError as exc:
        status, raw, retry = exc.code, exc.read(), exc.headers.get("Retry-After")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = {}
    return status, body if isinstance(body, dict) else {}, retry
'''
OPERATOR_SCRIPT = NODE_CALL + r'''
request = json.loads(__REQUEST__)
data = None if request["body"] is None else json.dumps(request["body"]).encode()
status, body, retry = node_call("operator-secret", "Astral-Node-Operator", request["method"], request["path"], data,
                                None if data is None else "application/json")
print(json.dumps({"status": status, "body": body, "retry_after": retry}))
'''
REFUSALS = {
    "node.loopback_required": "the node only serves this to its own machine. Run the command on the node's host",
    "node.operator_secret_required": "the node did not accept its operator secret; restart the node so it writes "
                                     "one, then run the command again",
    "node.rate_limited": "too many sign-in attempts. Wait a minute, then run the command again",
    "node.request_too_large": "the node refused the request as too large. Paste only the address you ended on",
    "node.request_invalid": "the node could not read the request. Check the connection name and the pasted address",
    "node.sign_in_failed": "the connection refused the sign-in. Run the command again and finish before it expires",
    "connection.unknown": "no connection has that name or id. Run astralvessel connections list to see them",
    "connection.flow_unknown": "that sign-in is no longer open. Run astralvessel connections sign-in again",
    "connection.flow_expired": "that sign-in expired. Run astralvessel connections sign-in again",
    "connection.not_signed_in": "the node is not signed in to that connection. Run connections sign-in NAME first",
    "mesh_unavailable": "the node's mesh is not running yet. Wait for astralvessel check to pass, then retry",
    "node_profile_inactive": "this deployment is not a personal node, so it has no outside connections",
}


class ConnectionsError(Exception):
    def __init__(self, message: str, status: int = 0, retry_after: int = 0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def printable(text: Any, limit: int = 80) -> str:
    return "".join(c if c.isprintable() else "?" for c in str(text))[:limit]


def call(stack: Stack, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    script = OPERATOR_SCRIPT.replace("__REQUEST__", repr(json.dumps({"method": method, "path": path, "body": body})))
    try:
        done = stack.python("node", script, timeout=60)
    except subprocess.TimeoutExpired:
        raise ConnectionsError("the node did not answer in time; check that the deployment is running") from None
    except OSError as exc:
        raise ConnectionsError(f"cannot reach the node's container: {exc.strerror or type(exc).__name__}") from None
    try:
        reply = json.loads(done.stdout)
        status, answer = int(reply["status"]), reply["body"]
    except (ValueError, KeyError, TypeError):
        raise ConnectionsError("the node did not answer; check that the deployment is running") from None
    if not isinstance(answer, dict):
        answer = {}
    if status >= 400:
        detail = answer.get("detail")
        detail = detail if isinstance(detail, str) and DETAIL.fullmatch(detail) else f"http.{status}"
        retry = reply.get("retry_after")
        message = f"{REFUSALS[detail]} ({detail})" if detail in REFUSALS else f"the node refused: {detail}"
        raise ConnectionsError(message, status, int(retry) if isinstance(retry, str) and retry.isdigit() else 0)
    return answer


def list_connections(stack: Stack) -> tuple[list[dict[str, Any]], int]:
    answer = call(stack, "GET", "/api/node/connections")
    rows = [row for row in answer.get("connections", []) if isinstance(row, dict)
            and isinstance(row.get("id"), str) and CONNECTION_ID.fullmatch(row["id"])]
    legacy = answer.get("legacy_connectors", 0)
    return rows, legacy if isinstance(legacy, int) and legacy > 0 else 0


def state_text(row: dict[str, Any]) -> str:
    state = row.get("state")
    if state not in STATES:
        return "unknown"
    reason = f" ({printable(row['reason'], 24)})" if state == "needs_attention" and row.get("reason") else ""
    return state.replace("_", " ") + reason


def describe(row: dict[str, Any]) -> str:
    note = "" if row.get("status", "ok") == "ok" else f"  unsupported: {printable(row.get('reason') or 'unknown')}"
    return f"{row['id'][:8]}  {printable(row.get('name') or '-', 40):<40}  {state_text(row)}{note}"


def show(stack: Stack, out: Callable[[str], None] = print) -> None:
    rows, legacy = list_connections(stack)
    for row in rows:
        out(describe(row))
    if not rows:
        out("no connections yet; add one in the AstralDeep app on a device in the owner's mesh")
    if legacy:
        out(f"{legacy} connector(s) added before connections are kept as they are and are not used by this node")


def resolve(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    exact = [r for r in rows if r["id"] == key or r.get("name") == key]
    found = exact or ([r for r in rows if r["id"].startswith(key)] if len(key) >= 4 else [])
    if len(found) == 1:
        return found[0]
    if not found:
        raise ConnectionsError(f"no connection is named or numbered {printable(key)}; "
                               "run astralvessel connections list")
    raise ConnectionsError(f"{printable(key)} matches {len(found)} connections; use more of the id")


class Listener(HTTPServer):
    # Bound before sign-in starts so the redirect's port is known; it keeps only the one callback address.
    def __init__(self, port: int = 0) -> None:
        if sys.platform.startswith("win32"):
            # Windows SO_REUSEADDR permits rebinding a live port; exclusive use keeps the busy-port refusal.
            self.allow_reuse_address = False
        super().__init__((LISTEN_HOST, port), Handler)
        self.captured: str | None = None
        self.expected_state: str | None = None
        self.timeout = 1

    @property
    def port(self) -> int:
        return self.server_address[1]

    def wait(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> str:
        deadline = clock() + seconds
        while self.captured is None:
            if clock() >= deadline:
                raise ConnectionsError("no sign-in came back in time; run the command again")
            self.handle_request()
        return self.captured


class Handler(BaseHTTPRequestHandler):
    server: Listener
    timeout = SOCKET_TIMEOUT

    def do_GET(self) -> None:
        # A browser on this machine only: a foreign Host header is a rebinding attempt.
        local = self.headers.get("Host") == f"{LISTEN_HOST}:{self.server.port}"
        if not local or self.path.split("?", 1)[0] != CALLBACK_PATH or len(self.path) > MAX_URL:
            self.send_response(404 if local else 400)
            self.end_headers()
            return
        expected = self.server.expected_state
        if expected is not None and parse_qs(urlsplit(self.path).query).get("state") != [expected]:
            self.send_response(400)
            self.end_headers()
            return
        if self.server.captured is None:
            self.server.captured = f"http://{LISTEN_HOST}:{self.server.port}{self.path}"
        body = b"<!doctype html><title>Signed in</title><p>The sign-in came back. You can close this tab.</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return None


def read_pasted() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("address after you signed in: ").strip()
    return sys.stdin.readline().strip()


def start(stack: Stack, row: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    answer = call(stack, "POST", f"/api/node/connections/{row['id']}/sign-in", body)
    if not isinstance(answer.get("flow"), str) or not FLOW_ID.fullmatch(answer["flow"]):
        raise ConnectionsError("the node did not start a sign-in")
    return answer


def sign_in_url(answer: dict[str, Any]) -> str:
    url = answer.get("authorization_url")
    if not isinstance(url, str) or len(url) > MAX_URL or not url.isprintable() or " " in url \
            or not url.startswith(("https://", f"http://{LISTEN_HOST}:")):
        raise ConnectionsError("the node sent no usable sign-in address")
    return url


def finish(answer: dict[str, Any], row: dict[str, Any], out: Callable[[str], None]) -> None:
    if answer.get("state") != "signed_in":
        why = f" ({printable(answer['reason'], 24)})" if answer.get("reason") else ""
        raise ConnectionsError(f"{printable(row.get('name') or row['id'][:8], 40)} is not signed in{why}")
    out(f"signed in: {printable(row.get('name') or row['id'][:8], 40)}")


def complete(stack: Stack, flow: str, redirect_url: str) -> dict[str, Any]:
    return call(stack, "POST", f"/api/node/connections/sign-in/{flow}/complete", {"redirect_url": redirect_url})


def cancel(stack: Stack, flow: str) -> None:
    try:
        call(stack, "DELETE", f"/api/node/connections/sign-in/{flow}")
    except ConnectionsError:
        pass


def device_code(stack: Stack, row: dict[str, Any], out: Callable[[str], None], sleep: Callable[[float], None],
                clock: Callable[[], float]) -> None:
    answer = start(stack, row, {"redirect": "device_code"})
    code, uri = answer.get("user_code"), answer.get("verification_uri")
    if not isinstance(code, str) or not CODE.fullmatch(code) or not isinstance(uri, str) \
            or not uri.startswith("https://") or not uri.isprintable() or " " in uri:
        cancel(stack, answer["flow"])
        raise ConnectionsError("the node sent no usable device code")
    out(f"open {uri} on any device and enter the code {code}")
    given = answer.get("interval")
    interval = max(MIN_INTERVAL, min(given, MAX_INTERVAL)) if isinstance(given, int) else DEFAULT_INTERVAL
    expires = answer.get("expires_in")
    deadline = clock() + (min(expires, FLOW_WAIT) if isinstance(expires, int) and expires > 0 else FLOW_WAIT)
    try:
        while clock() < deadline:
            sleep(interval)
            try:
                reply = call(stack, "POST", f"/api/node/connections/sign-in/{answer['flow']}/poll", {})
            except ConnectionsError as exc:
                if exc.status != 429:
                    raise
                interval = min(max(interval + DEFAULT_INTERVAL, exc.retry_after), MAX_INTERVAL)
                continue
            status = reply.get("status")
            if status == "signed_in":
                out(f"signed in: {printable(row.get('name') or row['id'][:8], 40)}")
                return
            if status in ("access_denied", "expired_token"):
                raise ConnectionsError(f"the service ended the sign-in ({status}); run the command again")
            if status == "slow_down":
                interval = min(interval + DEFAULT_INTERVAL, MAX_INTERVAL)
        raise ConnectionsError("the code expired before it was used; run the command again")
    finally:
        cancel(stack, answer["flow"])


def sign_in(stack: Stack, key: str, *, paste: bool = False, use_device_code: bool = False, port: int = 0,
            out: Callable[[str], None] = print, read: Callable[[], str] = read_pasted,
            sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
            listener: Callable[[int], Listener] = Listener) -> None:
    if paste and use_device_code:
        raise ConnectionsError("choose one of --paste and --device-code")
    row = resolve(list_connections(stack)[0], key)
    if use_device_code:
        device_code(stack, row, out, sleep, clock)
        return
    if paste:
        answer = start(stack, row, {"redirect": "pasted"})
        try:
            out(f"open this address, sign in, then copy the address your browser ends on:\n  {sign_in_url(answer)}")
            pasted = read()
            if not pasted.startswith("https://") or len(pasted) > MAX_URL:
                raise ConnectionsError("paste the full https:// address your browser ended on")
            finish(complete(stack, answer["flow"], pasted), row, out)
        finally:
            cancel(stack, answer["flow"])
        return
    try:
        waiting = listener(port)
    except OSError as exc:
        raise ConnectionsError(f"cannot listen on {LISTEN_HOST}:{port}: {exc.strerror}") from None
    answer = None
    try:
        answer = start(stack, row, {"redirect": "loopback", "port": waiting.port})
        url = sign_in_url(answer)
        waiting.expected_state = (parse_qs(urlsplit(url).query).get("state") or [None])[0]
        out(f"open this address in a browser on this machine and sign in:\n  {url}")
        captured = waiting.wait(FLOW_WAIT, clock)
        waiting.server_close()
        finish(complete(stack, answer["flow"], captured), row, out)
    finally:
        waiting.server_close()
        if answer is not None:
            cancel(stack, answer["flow"])
