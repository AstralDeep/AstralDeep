# A real ticket end to end: an owner device and the executor's own kernel pair in one process, the device grants a tool
# the executor's kernel published, a real turn mints the ticket, and the executor verifies it and runs the sandbox.
# It needs the pinned kernel wheel, which only installs on Linux, so it is skipped where the binding is absent.

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from test_deploy import deployment_text

from astralvessel import sandbox
from astralvessel.deployment import load_deployment
from astralvessel.executor import Replays, agent_token, catalog, effect_kind, executor_app
from astralvessel.mesh import KernelVerifier

k = pytest.importorskip("astral_kernel")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, utils  # noqa: E402

pytestmark = pytest.mark.asyncio(loop_scope="module")

ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
ISSUER, CLIENT, SUBJECT = "https://auth.example.test/realms/astral", "astral-desktop", "owner-1"
MASTER = "m" * 20
SANDBOX_TOKEN = "s" * 16
ANSWER = "the model saw the tool output"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def point(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def sign(key, message: bytes) -> bytes:
    r, s = utils.decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(32, "big") + (ORDER - s if s > ORDER // 2 else s).to_bytes(32, "big")


class Signer(k.HostSigner):
    def __init__(self):
        self.identity, self.presence = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())

    def identity_public_key(self):
        return point(self.identity)

    def identity_sign(self, message):
        return sign(self.identity, message)

    def presence_public_key(self):
        return point(self.presence)

    def presence_sign(self, prompt, message):
        return sign(self.presence, message)

    def hardware_backed(self):
        return False


class Model(BaseHTTPRequestHandler):
    plan: list = []
    seen: list = []

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))))
        Model.seen.append(request)
        usage = {"prompt_tokens": 12, "completion_tokens": 6}
        if Model.plan and "response_format" not in request and not any(m.get("role") == "tool"
                                                                        for m in request.get("messages", [])):
            call_id, name, arguments = Model.plan.pop(0)
            call = {"index": 0, "id": call_id, "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)}}
            events = [{"choices": [{"delta": {"tool_calls": [call]}}]},
                      {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": usage}]
        else:
            events = [{"choices": [{"delta": {"content": ANSWER}}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage}]
        body = ("".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n").encode()
        self.send_response(200)
        for header, value in (("content-type", "text/event-stream"), ("content-length", str(len(body))),
                              ("connection", "close")):
            self.send_header(header, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


async def until(condition, what: str, seconds: int = 40):
    for _ in range(seconds * 10):
        if condition():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


async def until_async(condition, what: str, seconds: int = 40):
    for _ in range(seconds * 10):
        if await condition():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    issuer_key = ec.generate_private_key(ec.SECP256R1())
    raw = point(issuer_key)
    jwk = {"kid": "k1", "kty": "EC", "crv": "P-256", "x": b64(raw[1:33]), "y": b64(raw[33:])}
    jwks = json.dumps({"keys": [jwk]})
    owner = k.Kernel.open(k.KernelConfig(
        data_dir=str(tmp / "owner"), operations_secret=k.new_operations_secret(), wrap_secret=k.new_operations_secret(),
        restore_marker=None, device_label="owner phone", device_kind=k.DeviceKind.DESKTOP,
        issuers=[k.IssuerConfig(issuer=ISSUER, client_id=CLIENT, jwks_json=jwks)],
        network=k.NetworkConfig(relay=k.RelayChoice.DISABLED(), local_discovery=False, internet_lookup=False,
                                loopback_only=True, sync_interval_ms=500, mailbox=None)), Signer())
    request = owner.begin_mesh_creation()
    header = b64(json.dumps({"alg": "ES256", "kid": "k1", "typ": "JWT"}, separators=(",", ":")).encode())
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": CLIENT, "sub": SUBJECT, "iat": now, "exp": now + 300, "nonce": request.nonce}
    signing = f"{header}.{b64(json.dumps(claims, sort_keys=True, separators=(',', ':')).encode())}"
    owner.complete_mesh_creation(f"{signing}.{b64(sign(issuer_key, signing.encode()))}")
    await owner.start_network()
    owner.set_model_provider(k.ModelProviderConfig(
        name="fake", base_url=f"http://127.0.0.1:{server.server_address[1]}/v1", models=["fake-1"],
        api_keys=["sk-e2e"], fallback_order=0, self_hosted=True, strict_tools=False))

    work = tmp / "work"
    (work / "notes").mkdir(parents=True)
    (work / "notes" / "ada.md").write_text("Analytical engine.\n")
    package = tmp / "agent"
    (package / "tools").mkdir(parents=True)
    (package / "vessel.toml").write_text(
        f'[agent]\nname = "tester"\ndescription = "A test agent."\n[tools]\nbuiltin = ["read", "list", "bash"]\n'
        f'[sandbox]\nworkdir = "{work}"\ndeadline_seconds = 20\ntool_timeout_seconds = 5\n')
    path = tmp / "deployment.toml"
    path.write_text(deployment_text(package, mounts="", egress=False).replace(
        'client_id = "astral-app"', f'client_id = "astral-app"\nsubject = "{SUBJECT}"'))
    deployment = load_deployment(path)
    sandbox_app = sandbox.sandbox_app(str(deployment.agents["guide"].package.root), token=SANDBOX_TOKEN, confine=False)
    runner = sandbox.RemoteRunner("http://sb", SANDBOX_TOKEN, 5, [])
    runner._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=sandbox_app), base_url="http://sb",
                                       headers={"Authorization": "Bearer " + SANDBOX_TOKEN})

    async def no_mailbox():
        return None

    verifier = KernelVerifier(tmp / "executor", mailbox=no_mailbox, loopback=True)
    app = executor_app(deployment, verifier, {"guide": runner}, "o" * 40, {"guide": agent_token(MASTER, "guide")},
                       Replays(tmp / "spent.db"))
    async with app.router.lifespan_context(app):
        await until_async(lambda: _state(verifier, "not_enrolled"), "the executor to offer pairing")
        offer = await verifier.pairing_offer()
        await owner.approve_pairing(offer["payload"], k.HistoryGrant.ALL)
        await until_async(lambda: _state(verifier, "enrolled"), "the executor to enroll")
        service = verifier._kernel
        device = (await service.mesh_status()).device_id
        tools = {t["name"]: t for t in catalog(deployment)["guide"]["tools"]}
        specs = [_spec(tools["bash"]), _spec(tools["read"]), _spec(tools["list"], description="not the catalog's")]
        for _ in range(300):
            try:
                service.publish_tool_catalog(specs)
                break
            except k.KernelError.State:
                await asyncio.sleep(0.1)
        await until(lambda: len(owner.peer_tool_catalog()) == 3, "the executor's catalog to reach the owner")
        listed = {t.tool.name: t for t in owner.peer_tool_catalog()}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ex") as http:
            yield SimpleNamespace(owner=owner, service=service, device=device, http=http, tools=tools, listed=listed,
                                  deployment=deployment, server=server)
    await owner.stop_network()
    server.shutdown()


async def _state(verifier, wanted):
    return await verifier.state() == wanted


def _spec(tool, description=None):
    return k.LocalToolSpec(
        name=tool["name"], description=description or tool["description"],
        input_schema_json=json.dumps(tool["input_schema"]), annotations_json=json.dumps({"agent": "guide"}),
        effect=getattr(k.LocalToolEffect, effect_kind(tool["scope"]).upper()),
        confidentiality=k.LocalToolConfidentiality.PRIVATE)


async def mint(world, tool: str, arguments: dict):
    # A real turn on the owner's device calls the tool; the request it sends carries the ticket.
    world.owner.grant_tool(world.listed[tool].grant_name, "*")
    known = {r.id for r in world.service.pending_requests_for_me()}
    Model.plan = [("call-1", tool, arguments)]
    chat = world.owner.create_chat("e2e")
    world.owner.send_message(chat, "run it", None)
    running = asyncio.ensure_future(world.owner.run_turn(chat))
    await until(lambda: any(r.id not in known for r in world.service.pending_requests_for_me()), "the tool request")
    asked = next(r for r in world.service.pending_requests_for_me() if r.id not in known)
    return asked, chat, running


def body(world, asked, **over):
    call = asked.call
    return {"agent": "guide", "tool": call.tool, "arguments_json": call.arguments_json,
            "action_json": call.action_json, "ticket_json": call.ticket_json,
            "owner": world.deployment.owner.owner_id, **over}


def headers():
    return {"Authorization": f"Bearer {agent_token(MASTER, 'guide')}"}


async def finish(world, asked, chat, running, text: str):
    world.service.claim_request(asked.id)
    step = k.PeerToolStep(
        call_id=asked.call.call_id, tool=asked.call.tool, arguments_json=asked.call.arguments_json,
        tool_def=json.loads(asked.call.action_json)["tool_def"], reasons=[], receipt=None, consent=None,
        status="ok", content=text, code=None, labels=asked.labels)
    world.service.record_peer_turn(chat, k.PeerTurn(
        continues=asked.continues, message=None, answers=asked.id, provider="node", model="node-model",
        labels=asked.labels, steps=[step], text="done", reason="completed", code=None))
    return await running


async def test_the_kernels_hash_of_a_published_tool_is_the_executors_definition(world):
    assert world.listed["bash"].hash == world.tools["bash"]["definition"]
    assert world.listed["read"].hash == world.tools["read"]["definition"]
    assert world.listed["list"].hash != world.tools["list"]["definition"]
    name = world.listed["bash"].grant_name
    assert world.owner.grant_tool(name, "*")
    assert any(g.tool == name and g.consented for g in world.owner.grants())


async def test_a_ticket_from_a_real_turn_runs_the_tool_in_the_sandbox(world):
    asked, chat, running = await mint(world, "bash", {"command": "echo ran-in-the-sandbox"})
    response = await world.http.post("/run", json=body(world, asked), headers=headers())
    answer = response.json()
    assert response.status_code == 200 and answer["output"].startswith("ran-in-the-sandbox"), answer
    assert answer["ticket"]["device"] == (await world.owner.mesh_status()).device_id
    assert len(answer["workspace"]) == 32 and not answer["error"]
    result = await finish(world, asked, chat, running, answer["output"])
    assert result.status == "completed" and result.text == ANSWER
    assert any("ran-in-the-sandbox" in json.dumps(request) for request in Model.seen)


async def test_a_tampered_action_or_argument_is_refused_and_nothing_runs(world, tmp_path):
    marker = tmp_path / "marker"
    asked, chat, running = await mint(world, "bash", {"command": f"touch {marker}"})
    good = body(world, asked)
    action = json.loads(good["action_json"])
    swapped = {**good, "arguments_json": json.dumps({"command": f"touch {marker}x"})}
    other_turn = {**good, "action_json": json.dumps({**action, "turn": "sha256:" + "0" * 64})}
    other_mesh = {**good, "action_json": json.dumps({**action, "mesh": "sha256:" + "0" * 64})}
    other_tool = {**good, "action_json": json.dumps({**action, "tool": "list"})}
    codes = []
    for tampered in (swapped, other_turn, other_mesh, other_tool):
        refused = await world.http.post("/run", json=tampered, headers=headers())
        assert refused.status_code == 403
        codes.append(refused.json()["code"])
    assert codes[0] == "request.call_mismatch" and codes[3] == "request.call_mismatch"
    assert codes[1].startswith("ticket.") and codes[2].startswith("ticket.")
    assert not marker.exists()
    await finish(world, asked, chat, running, "not run")


async def test_a_ticket_for_a_tool_defined_differently_is_refused(world):
    asked, chat, running = await mint(world, "list", {"path": "notes"})
    refused = await world.http.post("/run", json=body(world, asked), headers=headers())
    assert (refused.status_code, refused.json()["code"]) == (403, "gate.tool_def_changed")
    await finish(world, asked, chat, running, "not run")


async def test_a_ticket_runs_once_and_never_for_another_agents_token(world):
    asked, chat, running = await mint(world, "bash", {"command": "echo once"})
    assert (await world.http.post("/run", json=body(world, asked), headers=headers())).status_code == 200
    replayed = await world.http.post("/run", json=body(world, asked), headers=headers())
    assert (replayed.status_code, replayed.json()["code"]) == (403, "ticket.replayed")
    foreign = {"Authorization": f"Bearer {agent_token(MASTER, 'another')}"}
    assert (await world.http.post("/run", json=body(world, asked), headers=foreign)).status_code == 401
    await finish(world, asked, chat, running, "once")


async def test_a_revoked_grant_mints_no_further_ticket(world):
    name = world.listed["read"].grant_name
    asked, chat, running = await mint(world, "read", {"path": "notes/ada.md"})
    world.owner.revoke_tool(name, "*")
    assert not any(g.tool == name for g in world.owner.grants())
    known = {r.id for r in world.service.pending_requests_for_me()}
    Model.plan = [("call-revoked", "read", {"path": "notes/ada.md"})]
    after = world.owner.create_chat("after revoke")
    world.owner.send_message(after, "run it", None)
    await asyncio.wait_for(world.owner.run_turn(after), 30)
    assert {r.id for r in world.service.pending_requests_for_me()} == known
    await finish(world, asked, chat, running, "late")


async def test_a_stopped_mesh_refuses_a_ticket_minted_before_the_stop(world):
    asked, chat, running = await mint(world, "bash", {"command": "echo stopped"})
    assert world.owner.raise_stop() >= 1
    await until_async(lambda: _executor_stopped(world), "the stop to reach the executor")
    refused = await world.http.post("/run", json=body(world, asked), headers=headers())
    assert refused.status_code == 403 and refused.json()["code"] in {"ticket.stopped", "gate.mesh_stopped"}
    world.owner.resume_stop()
    await finish(world, asked, chat, running, "stopped")


async def _executor_stopped(world):
    return (await world.service.mesh_status()).stopped
