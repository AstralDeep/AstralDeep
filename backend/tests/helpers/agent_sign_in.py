"""Gives the fake kernel in tests/fake_astral_kernel.py the connection sign-in calls for an A2A connection, speaking real OAuth authorization-code and device grants over loopback HTTP to a fixture A2A agent.
test_connection_gates_phase3.py installs it on the node's fake kernel because the pinned kernel admits a node only to a non-local https connection, so the operator routes cannot reach a loopback agent on the real binding.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace
from typing import Any, Dict

from tests import fake_astral_kernel as fake

CALLBACK = "https://astraldeep.github.io/oauth/callback"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
EXPIRES_MS = 600_000


def connection(connection_id: str, name: str, card_url: str) -> SimpleNamespace:
    device = SimpleNamespace(device="dev-node", label="node", state="not_signed_in", reason=None, issuer=None, ms=0)
    return SimpleNamespace(
        id=connection_id, name=name, kind="a2a", endpoint=card_url, local=False, scheme="oauth", issuer=None,
        scopes=["read"], flow="private", egress=[], tools=[], pending_tools=None, approved_by="dev-phone",
        approved_by_removed_device=False, supported_here=True, unsupported=None, attention=None,
        this_device=device, other_devices=[],
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _post(url: str, data: Dict[str, str]) -> Any:
    request = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status, json.loads(answer.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def approve(authorization_url: str) -> str:
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(authorization_url, timeout=10)
    except urllib.error.HTTPError as exc:
        return exc.headers["location"]
    raise AssertionError("the agent did not redirect")


def _state(code: str) -> fake.KernelError:
    return fake.KernelError.State(code, "refused")


class AgentSignIn:
    def __init__(self, kernel: Any, agent: Any, record: SimpleNamespace):
        self.kernel, self.agent, self.record = kernel, agent, record
        self.flows: Dict[str, Dict[str, Any]] = {}
        kernel.connection_list = [record]
        kernel.begin_connection_sign_in = self.begin
        kernel.complete_connection_sign_in = self.complete
        kernel.poll_connection_sign_in = self.poll
        kernel.cancel_connection_sign_in = self.cancel

    async def begin(self, connection_id: str, redirect: Any) -> Any:
        if connection_id != self.record.id:
            raise fake.KernelError.InvalidInput("connection.unknown", "no such connection")
        flow, origin = secrets.token_urlsafe(12), self.agent.origin
        if isinstance(redirect, fake.SignInRedirect.DEVICE_CODE):
            status, body = _post(origin + "/oauth/device", {"client_id": "node"})
            self.flows[flow] = {"kind": "device", "device_code": body["device_code"], "interval": body["interval"]}
            return SimpleNamespace(
                flow=flow, authorization_url=None, redirect_uri=CALLBACK, user_code=body["user_code"],
                verification_uri=body["verification_uri"], verification_uri_complete=None, interval_s=body["interval"],
                expires_ms=EXPIRES_MS,
            )
        uri = f"http://127.0.0.1:{redirect.port}/callback" if isinstance(redirect, fake.SignInRedirect.LOOPBACK) else CALLBACK
        verifier, state = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.flows[flow] = {"kind": "code", "verifier": verifier, "state": state, "uri": uri}
        query = urllib.parse.urlencode({
            "response_type": "code", "client_id": "node", "redirect_uri": uri, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        return SimpleNamespace(
            flow=flow, authorization_url=f"{origin}/oauth/authorize?{query}", redirect_uri=uri, user_code=None,
            verification_uri=None, verification_uri_complete=None, interval_s=None, expires_ms=EXPIRES_MS,
        )

    async def complete(self, flow: str, redirect_url: str) -> Any:
        entry = self.flows.pop(flow, None)
        if entry is None or entry["kind"] != "code":
            raise _state("connection.flow_unknown")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(redirect_url).query))
        if not redirect_url.startswith(entry["uri"] + "?") or query.get("state") != entry["state"]:
            raise _state("oauth.state_mismatch")
        status, _ = _post(self.agent.origin + "/oauth/token", {
            "grant_type": "authorization_code", "code": query.get("code", ""), "code_verifier": entry["verifier"],
            "client_id": "node", "redirect_uri": entry["uri"],
        })
        if status != 200:
            raise _state("oauth.token_refused")
        return self._signed_in()

    async def poll(self, flow: str) -> Any:
        entry = self.flows.get(flow)
        if entry is None or entry["kind"] != "device":
            raise _state("connection.flow_unknown")
        status, _ = _post(self.agent.origin + "/oauth/token", {
            "grant_type": DEVICE_GRANT, "device_code": entry["device_code"], "client_id": "node",
        })
        if status != 200:
            return fake.SignInPoll.PENDING(entry["interval"])
        del self.flows[flow]
        return fake.SignInPoll.DONE(self._signed_in())

    def cancel(self, flow: str) -> None:
        self.flows.pop(flow, None)

    def _signed_in(self) -> SimpleNamespace:
        self.record.this_device.state = "signed_in"
        return SimpleNamespace(connection=self.record.id, state="signed_in", reason=None, tools_pending=False)
