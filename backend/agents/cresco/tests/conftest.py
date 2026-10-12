"""Stages a disposable TLS wsapi gateway with deterministic allowed and denied read
exchanges for the Cresco client and tool suites. The gateway listens on loopback with a
self-signed certificate, checks the cresco_service_key header, and serves the four
read verbs from a pinned topology so every exchange is reproducible.
"""

import datetime
import hashlib
import http
import ipaddress
import json
import queue
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from websockets.datastructures import Headers
from websockets.http11 import Response
from websockets.sync.server import serve

GATEWAY_HOST = "127.0.0.1"
SERVICE_KEY = "staged-cresco-service-key"

TOPOLOGY = {
    "regions": [
        {"region": "global", "agents": 2, "status": "online"},
        {"region": "edge-west", "agents": 3, "status": "online"},
    ],
    "agents": [
        {"region": "global", "agent": "agent-1", "status": "connected"},
        {"region": "global", "agent": "agent-2", "status": "connected"},
        {"region": "edge-west", "agent": "edge-a", "status": "connected"},
    ],
    "agent_info": {
        "agent_status": "connected",
        "plugins": [
            {"plugin": "executor", "state": "STARTED", "health": "HEALTHY"},
            {"plugin": "filerepo", "state": "STARTED", "health": "DEGRADED"},
        ],
    },
    "sysinfo": {"hostname": "edge-host-1", "os": "Linux", "cores": "16"},
}

FLOOD_REGIONS = [
    {"region": f"region-{index:03d}", "agents": 1, "status": "online"} for index in range(250)
]


def _reply_body(action, params):
    if action == "listregions":
        return {"regions": TOPOLOGY["regions"]}
    if action == "listagents":
        wanted = params.get("region")
        agents = [a for a in TOPOLOGY["agents"] if not wanted or a["region"] == wanted]
        return {"agents": agents}
    if action == "getagentinfo":
        return dict(TOPOLOGY["agent_info"])
    if action == "getsysinfo":
        return {"sysinfo": TOPOLOGY["sysinfo"]}
    return {"error": "unknown action"}


def _certificate(san_ip):
    key = ec.generate_private_key(ec.SECP384R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "staged-gateway")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address(san_ip))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA384())
    )
    return cert, key


class Gateway:
    def __init__(self, port, cert_pem, fingerprint, captured, state):
        self.port = port
        self.cert_pem = cert_pem
        self.fingerprint = fingerprint
        self.captured = captured
        self.state = state

    @property
    def requests(self):
        return self.state["requests"]

    @property
    def url(self):
        return f"wss://{GATEWAY_HOST}:{self.port}"

    def configure(self, monkeypatch, *, ca=True, pin=None, url=None, key=SERVICE_KEY,
                  allow_private=True, **extra):
        monkeypatch.setenv("CRESCO_WSAPI_URL", url or self.url)
        monkeypatch.setenv("CRESCO_SERVICE_KEY", key)
        if ca:
            monkeypatch.setenv("CRESCO_CA_BUNDLE", self.cert_pem)
        else:
            monkeypatch.delenv("CRESCO_CA_BUNDLE", raising=False)
        if pin is not None:
            monkeypatch.setenv("CRESCO_TLS_FINGERPRINT", pin)
        else:
            monkeypatch.delenv("CRESCO_TLS_FINGERPRINT", raising=False)
        if allow_private:
            monkeypatch.setenv("CRESCO_ALLOW_PRIVATE_HOST", "true")
        else:
            monkeypatch.delenv("CRESCO_ALLOW_PRIVATE_HOST", raising=False)
        for name, value in extra.items():
            monkeypatch.setenv(name, value)


@pytest.fixture
def gateway(tmp_path):
    cert, key = _certificate(GATEWAY_HOST)
    pem_path = tmp_path / "gateway-cert.pem"
    pem_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path = tmp_path / "gateway-key.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    fingerprint = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    captured = queue.Queue()
    requests = []
    state = {"mode": "ok", "key": SERVICE_KEY, "requests": requests}

    def process_request(connection, request):
        if request.path != "/api/apisocket":
            return Response(http.HTTPStatus.NOT_FOUND, "Not Found", Headers())
        if state["mode"] == "auth":
            return Response(http.HTTPStatus.UNAUTHORIZED, "Unauthorized", Headers())
        if state["mode"] == "refused":
            state["mode"] = "ok"
            return Response(http.HTTPStatus.SERVICE_UNAVAILABLE, "Busy", Headers())
        presented = request.headers.get("cresco_service_key", "")
        captured.put(presented)
        if presented != state["key"]:
            return Response(http.HTTPStatus.UNAUTHORIZED, "Unauthorized", Headers())
        return None

    def handler(ws):
        if state["mode"] == "closed":
            ws.close(code=1011)
            return
        try:
            raw = ws.recv(timeout=5)
            if state["mode"] == "slow":
                threading.Event().wait(2.5)
                ws.close(code=1011)
                return
            envelope = json.loads(raw)
            requests.append(envelope)
            payload = envelope["message_payload"]
            if state["mode"] == "malformed":
                ws.send("this is not json")
                return
            if state["mode"] == "corrupt":
                ws.send(json.dumps({"status": "10", "client_rpc_id": "not-the-id"}))
                return
            if state["mode"] == "badstatus":
                ws.send(json.dumps({"status": "17", "client_rpc_id": payload["client_rpc_id"]}))
                return
            if state["mode"] == "nondict":
                ws.send(json.dumps(["not", "an", "envelope"]))
                return
            if state["mode"] == "empty":
                body = {"regions": []}
            elif state["mode"] == "flood":
                body = {"regions": FLOOD_REGIONS}
            elif state["mode"] == "badshape":
                body = {"regions": {"not": "a list"}}
            elif state["mode"] == "nosysinfo":
                body = {"sysinfo": "not-a-dict"}
            elif state["mode"] == "twocompressed":
                from agents.cresco.wsapi_client import encode_compressed

                half = {"pad": "x" * (3 << 20)}
                body = {"first": encode_compressed(half), "second": encode_compressed(half)}
            elif state["mode"] == "compressed":
                from agents.cresco.wsapi_client import encode_compressed

                body = {"regions": encode_compressed(TOPOLOGY["regions"])}
            elif state["mode"] == "bigcompressed":
                from agents.cresco.wsapi_client import encode_compressed

                body = {"blob": encode_compressed({"pad": "x" * (5 << 20)})}
            else:
                body = _reply_body(payload.get("action"), payload)
            ws.send(json.dumps({"status": "10", "client_rpc_id": payload["client_rpc_id"], **body}))
        except Exception:
            pass

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(pem_path), str(key_path))
    listener = serve(
        handler,
        GATEWAY_HOST,
        0,
        ssl=server_context,
        process_request=process_request,
        compression=None,
    )
    worker = threading.Thread(target=listener.serve_forever, daemon=True)
    worker.start()
    port = listener.socket.getsockname()[1]
    try:
        yield Gateway(port, str(pem_path), fingerprint, captured, state)
    finally:
        listener.shutdown()
        worker.join(timeout=5)


@pytest.fixture(autouse=True)
def _isolate_cresco_env(monkeypatch):
    for name in (
        "CRESCO_WSAPI_URL",
        "CRESCO_SERVICE_KEY",
        "CRESCO_CA_BUNDLE",
        "CRESCO_TLS_FINGERPRINT",
        "CRESCO_ALLOW_PRIVATE_HOST",
    ):
        monkeypatch.delenv(name, raising=False)
