"""Gives the fake kernel in tests/fake_astral_kernel.py the connection sign-in calls, speaking real OAuth over loopback HTTP to the fixture server in tests/fixtures/connections/oauth_server.py, with the kernel's flow limit, expiry, consumption and device poll pacing on a clock the test moves.
test_node_connection_api.py installs it on the node's fake kernel, because the pinned kernel refuses a node approving a connection and a node can use only a connection that is not local to another device.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace
from typing import Any, Dict, Optional

from tests import fake_astral_kernel as fake

MAX_FLOWS = 4
FLOW_TTL_MS = 600_000
CALLBACK = "https://astraldeep.github.io/oauth/callback"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
DEVICE_STEP = 5


def connection(connection_id: str, name: str, endpoint: str) -> SimpleNamespace:
    device = SimpleNamespace(device="dev-node", label="node", state="not_signed_in", reason=None, issuer=None, ms=0)
    return SimpleNamespace(
        id=connection_id, name=name, kind="mcp", endpoint=endpoint, local=False, scheme="oauth2", issuer=None,
        scopes=[], flow="private", egress=[], tools=[], pending_tools=None, approved_by="dev-phone",
        approved_by_removed_device=False, supported_here=True, unsupported=None, attention=None,
        this_device=device, other_devices=[],
    )


def _post(url: str, data: Dict[str, str]) -> Any:
    request = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status, json.loads(answer.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _register(url: str, redirect: str) -> str:
    request = urllib.request.Request(
        url, data=json.dumps({"redirect_uris": [redirect]}).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as answer:
        return json.loads(answer.read())["client_id"]


def _state(code: str, message: str = "refused") -> fake.KernelError:
    return fake.KernelError.State(code, message)


class FixtureSignIn:
    def __init__(self, kernel: Any, server: Any, record: SimpleNamespace):
        self.kernel, self.server, self.record = kernel, server, record
        self.now_ms = time.time_ns() // 1_000_000
        self.flows: Dict[str, Dict[str, Any]] = {}
        self.cancelled: list = []
        kernel.connection_list = [record]
        kernel.begin_connection_sign_in = self.begin
        kernel.complete_connection_sign_in = self.complete
        kernel.poll_connection_sign_in = self.poll
        kernel.cancel_connection_sign_in = self.cancel

    def advance(self, seconds: float) -> None:
        self.now_ms += int(seconds * 1000)

    def _live(self) -> None:
        for name in [name for name, flow in self.flows.items() if flow["expires_ms"] <= self.now_ms]:
            del self.flows[name]

    def _open(self, flow: str, kind: str) -> Dict[str, Any]:
        self._live()
        if flow not in self.flows:
            raise _state("connection.flow_unknown", "the sign-in is not open")
        if self.flows[flow]["kind"] != kind:
            raise _state("connection.flow_kind", "wrong kind")
        return self.flows[flow]

    async def begin(self, connection_id: str, redirect: Any) -> Any:
        if connection_id != self.record.id:
            raise fake.KernelError.InvalidInput("connection.unknown", "no such connection")
        self._live()
        if len(self.flows) >= MAX_FLOWS:
            raise _state("connection.too_many_flows", "too many sign-ins are open")
        flow, origin = secrets.token_urlsafe(12), self.server.urls.as_origin
        device = isinstance(redirect, fake.SignInRedirect.DEVICE_CODE)
        uri = f"http://127.0.0.1:{redirect.port}/callback" if isinstance(redirect, fake.SignInRedirect.LOOPBACK) else CALLBACK
        client = _register(origin + "/register", uri)
        entry = {"kind": "device" if device else "code", "client": client, "uri": uri, "resource": self.server.rs_url,
                 "expires_ms": self.now_ms + FLOW_TTL_MS}
        if device:
            status, body = _post(origin + "/device", {"client_id": client, "resource": self.server.rs_url})
            entry.update(device_code=body["device_code"], interval=body["interval"], next_ms=self.now_ms + body["interval"] * 1000)
            self.flows[flow] = entry
            return SimpleNamespace(flow=flow, authorization_url=None, redirect_uri=uri, user_code=body["user_code"],
                                   verification_uri=body["verification_uri"], verification_uri_complete=None,
                                   interval_s=body["interval"], expires_ms=entry["expires_ms"])
        verifier, state = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        entry.update(verifier=verifier, state=state)
        self.flows[flow] = entry
        query = urllib.parse.urlencode({
            "response_type": "code", "client_id": client, "redirect_uri": uri, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256", "resource": self.server.rs_url})
        return SimpleNamespace(flow=flow, authorization_url=f"{origin}/authorize?{query}", redirect_uri=uri,
                               user_code=None, verification_uri=None, verification_uri_complete=None, interval_s=None,
                               expires_ms=entry["expires_ms"])

    async def complete(self, flow: str, redirect_url: str) -> Any:
        entry = self._open(flow, "code")
        del self.flows[flow]
        parts = urllib.parse.urlsplit(redirect_url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        if not redirect_url.startswith(entry["uri"] + "?") or query.get("state") != entry["state"]:
            raise _state("oauth.state_mismatch")
        if "error" in query or "code" not in query:
            raise _state("oauth.authorization_failed")
        status, _ = _post(self.server.urls.as_origin + "/token", {
            "grant_type": "authorization_code", "code": query["code"], "code_verifier": entry["verifier"],
            "client_id": entry["client"], "redirect_uri": entry["uri"], "resource": entry["resource"]})
        if status != 200:
            raise _state("oauth.token_refused")
        return self._signed_in()

    async def poll(self, flow: str) -> Any:
        entry = self._open(flow, "device")
        if self.now_ms < entry["next_ms"]:
            return fake.SignInPoll.PENDING(entry["interval"])
        status, body = _post(self.server.urls.as_origin + "/token", {
            "grant_type": DEVICE_GRANT, "device_code": entry["device_code"], "client_id": entry["client"],
            "resource": entry["resource"]})
        error: Optional[str] = body.get("error")
        if status == 200:
            del self.flows[flow]
            return fake.SignInPoll.DONE(self._signed_in())
        if error == "slow_down":
            entry["interval"] += DEVICE_STEP
        elif error in ("access_denied", "expired_token"):
            del self.flows[flow]
            raise _state("oauth.device_denied" if error == "access_denied" else "oauth.device_expired")
        entry["next_ms"] = self.now_ms + entry["interval"] * 1000
        return fake.SignInPoll.PENDING(entry["interval"])

    def cancel(self, flow: str) -> None:
        self.cancelled.append(flow)
        self.flows.pop(flow, None)

    def _signed_in(self) -> SimpleNamespace:
        self.record.this_device.state = "signed_in"
        return SimpleNamespace(connection=self.record.id, state="signed_in", reason=None, tools_pending=False)
