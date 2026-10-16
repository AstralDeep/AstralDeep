"""Stages the source-verified Cresco control dialect over real loopback TLS.
Synthetic topology and fault responses exercise the bridge without running upstream
fabric code or claiming live fabric or work-dispatch qualification.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import ssl
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from websockets.sync.server import serve

from agents.cresco.wsapi_client import encode_bulk

from agents.cresco import mcp_server as cresco_mcp_server

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

SERVICE_KEY = "staged-service-key"


class StagedFabric:
    def __init__(self, tmp_path: Path):
        self.regions = {"r1": ["a1", "a2"], "r2": ["a3"]}
        self.agent_plugins = {"r1_a1": ["executor"], "r1_a2": ["filerepo"], "r2_a3": []}
        self.errors_by_target: dict[str, str] = {}
        self.behavior = "normal"
        self.reject_auth = False
        self.reject_status = 401
        self.bulk_override = None
        self.echo_service_key = False
        self.requests: list[dict] = []
        self.wire_requests: list[dict] = []
        self._connections = set()
        self._handlers = set()
        self.upgrade_credentials: list[str | None] = []
        self._lock = threading.Lock()
        self._server = None
        self._thread = None
        cert, key, self._leaf_der = _materialize_certificate(tmp_path)
        self.ca_file = str(self._write(tmp_path / "ca.pem", cert))
        self.cert_file = str(self._write(tmp_path / "cert.pem", cert))
        self.key_file = str(self._write(tmp_path / "key.pem", key))
        self.fingerprint = hashlib.sha256(self._leaf_der).hexdigest()

    @staticmethod
    def _write(path: Path, pem: bytes) -> Path:
        path.write_bytes(pem)
        return path

    @property
    def port(self) -> int:
        return self._server.socket.getsockname()[1]

    @property
    def wsapi_url(self) -> str:
        return f"wss://127.0.0.1:{self.port}/api/apisocket"

    def start(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert_file, self.key_file)
        self._server = serve(
            self._handle, host="127.0.0.1", port=0, ssl=context,
            process_request=self._process_request,
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            connections, handlers = list(self._connections), list(self._handlers)
        for connection in connections:
            connection.close()
        for handler in handlers:
            handler.join(timeout=5)
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _process_request(self, connection, request):
        self.upgrade_credentials.append(request.headers.get("cresco_service_key"))
        if self.reject_auth:
            return connection.respond(self.reject_status, "staged fabric rejection")
        return None

    def _handle(self, connection) -> None:
        handler = threading.current_thread()
        with self._lock:
            self._connections.add(connection)
            self._handlers.add(handler)
        try:
            while True:
                raw = connection.recv(timeout=10)
                request = json.loads(raw)
                info = request.get("message_info") or {}
                payload = request.get("message_payload") or {}
                with self._lock:
                    self.requests.append(payload)
                    self.wire_requests.append(request)
                if not all(isinstance(value, str) for part in (info, payload) for value in part.values()):
                    raise ValueError("source-verified wsapi parameters must be strings")
                if info.get("is_rpc") != "true" or info.get("message_event_type") not in ("EXEC", "CONFIG"):
                    raise ValueError("source-verified wsapi RPC/event type mismatch")
                action = payload.get("action")
                route = "global_agent_msgevent" if action == "getagentinfo" else "global_controller_msgevent"
                event = "CONFIG" if action == "getagentinfo" else "EXEC"
                if info.get("message_type") != route or info.get("message_event_type") != event:
                    raise ValueError("source-verified wsapi route mismatch")
                response = self._respond(connection, info, payload)
                if response is not None:
                    connection.send(json.dumps(response))
        except Exception:
            return
        finally:
            with self._lock:
                self._connections.discard(connection)
                self._handlers.discard(handler)

    def _respond(self, connection, info: dict, payload: dict) -> dict | None:
        if self.behavior == "close_after_send":
            connection.close(code=1011, reason="staged fabric close")
            return None
        if self.behavior == "never_reply":
            return None
        if self.behavior == "malformed":
            return {"client_rpc_id": payload.get("client_rpc_id"), "nested": {"unsupported": True}}
        if self.behavior == "stray_then_result":
            connection.send(json.dumps({"client_rpc_id": "unrelated-frame", "action": "telemetry", "value": "1"}))
        action = payload.get("action")
        region = info.get("dst_region") or payload.get("action_region")
        agent = info.get("dst_agent") or payload.get("action_agent")
        target = f"{region}_{agent}"
        if target in self.errors_by_target:
            return dict(payload, error=self.errors_by_target[target])
        if action == "listregions":
            body = {"regionslist": encode_bulk({"regions": [{"name": name, "type": "local", "agents": str(len(self.regions[name]))} for name in sorted(self.regions)]})}
        elif action == "listagents":
            rows = [{"agent_id": name, "region_id": name_region, "status_desc": "running", "plugins": "0"} for name_region, names in self.regions.items()
                    if region is None or name_region == region for name in names]
            body = {"agentslist": encode_bulk({"agents": rows})}
        elif action == "getagentinfo":
            body = {"agent-data": "cresco-data" + (SERVICE_KEY if self.echo_service_key else "")}
        elif action == "resourceinfo":
            perf = {"platform": "synthetic-node", "cpus": 4}
            body = {"resourceinfo": encode_bulk({"agentresourceinfo": [{"region": region, "agent": agent,
                                                                     "perf": json.dumps(perf)}]})}
        else:
            return dict(payload, error="unsupported operation")
        if self.behavior == "bad_bulk":
            body = {next(iter(body)): "!!not-gzip-base64!!"}
        elif self.bulk_override is not None:
            body = {next(iter(body)): self.bulk_override}
        return dict(payload, **body)


def _materialize_certificate(tmp_path: Path) -> tuple[bytes, bytes, bytes]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "staged-cresco-fabric")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                x509.DNSName("localhost"),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    from cryptography.hazmat.primitives.serialization import Encoding

    der = cert.public_bytes(Encoding.DER)
    return cert_pem, key_pem, der


@pytest.fixture(autouse=True)
def _strip_ambient_cresco_environment(monkeypatch):
    monkeypatch.delenv("FF_CRESCO", raising=False)
    for name in ("CRESCO_WSAPI_URL", "CRESCO_SERVICE_KEY", "CRESCO_CA_BUNDLE",
                 "CRESCO_TLS_FINGERPRINT", "CRESCO_ALLOW_PRIVATE_HOST",
                 "CRESCO_APPROVED_TARGETS", "CRESCO_EXECUTOR_ENABLED",
                 "CRESCO_CONNECT_TIMEOUT", "CRESCO_RPC_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def staged_fabric(tmp_path):
    fabric = StagedFabric(tmp_path)
    fabric.start()
    try:
        yield fabric
    finally:
        fabric.stop()


@pytest.fixture
def bridge_env(monkeypatch, staged_fabric):
    monkeypatch.setitem(cresco_mcp_server.flags._flags, "cresco", True)
    monkeypatch.setenv("CRESCO_WSAPI_URL", staged_fabric.wsapi_url)
    monkeypatch.setenv("CRESCO_SERVICE_KEY", SERVICE_KEY)
    monkeypatch.setenv("CRESCO_CA_BUNDLE", staged_fabric.ca_file)
    monkeypatch.setenv("CRESCO_ALLOW_PRIVATE_HOST", "127.0.0.1")
    monkeypatch.setenv("CRESCO_APPROVED_TARGETS", "r1_a1,r1_a2_executor,r2_a3")
    monkeypatch.setenv("CRESCO_CONNECT_TIMEOUT", "20")
    monkeypatch.setenv("CRESCO_BACKOFF_SECONDS", "0.05")
    return staged_fabric


@pytest.fixture(autouse=True)
def captured_audit(monkeypatch):
    from types import SimpleNamespace

    from audit import recorder

    rows = []

    def fake_record_blocking(event):
        rows.append(event.model_dump())
        return object()

    monkeypatch.setattr(recorder, "get_recorder", lambda: SimpleNamespace(record_blocking=fake_record_blocking))
    return rows
