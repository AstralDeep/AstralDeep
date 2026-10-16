"""Fixtures for the Cresco bridge suite: a staged in-process multi-node fabric speaking the
pinned wsapi profile over real loopback TLS, isolated bridge environment, and an audit
capture hook, so dispatch exchanges run deterministically without any external service.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import ssl
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from websockets.sync.server import serve

from agents.cresco import mcp_server as cresco_mcp_server

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

SERVICE_KEY = "staged-service-key"


class StagedFabric:
    def __init__(self, tmp_path: Path):
        self.regions = {"r1": ["a1", "a2"], "r2": ["a3"]}
        self.agent_plugins = {"r1_a1": ["executor"], "r1_a2": ["filerepo"], "r2_a3": []}
        self.work_results: dict[str, dict] = {}
        self.command_results: dict[str, dict] = {}
        self.errors_by_target: dict[str, str] = {}
        self.behavior = "normal"
        self.delay_before_reply = 0.0
        self.reject_auth = False
        self.reject_status = 401
        self.bulk_override = None
        self.echo_service_key = False
        self.requests: list[dict] = []
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
        return f"wss://127.0.0.1:{self.port}"

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
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _process_request(self, connection, request):
        if self.reject_auth:
            return connection.respond(self.reject_status, "staged fabric rejection")
        return None

    def _handle(self, connection) -> None:
        try:
            while True:
                raw = connection.recv(timeout=10)
                request = json.loads(raw)
                info = request.get("message_info") or {}
                payload = request.get("message_payload") or {}
                with self._lock:
                    self.requests.append(payload)
                if self.delay_before_reply:
                    time.sleep(self.delay_before_reply)
                connection.send(json.dumps(self._respond(connection, info.get("message_id", ""), payload)))
        except Exception:
            return

    def _respond(self, connection, message_id: str, payload: dict) -> dict:
        if self.behavior == "close_after_send":
            connection.close(code=1011, reason="staged fabric close")
            raise ConnectionError("staged close")
        if self.behavior == "never_reply":
            time.sleep(6)
        if self.behavior == "malformed":
            return {"message_info": {"message_id": message_id}, "message_payload": "not-a-mapping"}
        if self.behavior == "stray_then_result":
            connection.send(json.dumps({
                "message_info": {"message_id": "unrelated-frame"},
                "message_payload": {"action": "telemetry", "value": 1},
            }))
        action = payload.get("action")
        body, error = self._result_for(action, payload)
        if self.behavior == "bad_bulk":
            body = {"result_bulk": "!!not-gzip-base64!!"}
        elif self.bulk_override is not None:
            body = {"result_bulk": self.bulk_override}
        frame = {
            "message_info": {
                "message_type": "RESPONSE", "message_event_type": "EVENT",
                "is_rpc": False, "message_id": message_id,
            },
            "message_payload": dict(body, action="result"),
        }
        if error:
            frame["message_payload"] = {"action": "result", "error": error}
        return frame

    def _result_for(self, action: str, payload: dict) -> tuple[dict, object]:
        target = str(payload.get("region_agent") or "")
        if target in self.errors_by_target:
            return {}, self.errors_by_target[target]
        if action == "listregions":
            return {"regions": [{"region": name} for name in sorted(self.regions)]}, None
        if action == "listagents":
            region = payload.get("region")
            return {"agents": [{"agent": f"{region or 'r1'}_{name}" if region else name}
                               for name in (self.regions.get(region) or [])]}, None
        if action == "agentinfo":
            return {"info": {"region_agent": target, "state": "running",
                             "plugins": self.agent_plugins.get(target, []),
                             "service_key_echo": SERVICE_KEY if self.echo_service_key else None}}, None
        if action == "submitwork":
            return {"work_id": payload.get("work_id"), "state": "completed",
                    "result": dict(self.work_results.get("default", {"accepted": True, "kind": "staged"}))}, None
        if action == "command":
            return {"command_id": payload.get("command_id"), "state": "completed",
                    "result": dict(self.command_results.get("default", {"exit_code": 0, "stdout": "staged"}))}, None
        return {}, "unmapped_action"


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


@pytest.fixture
def captured_audit(monkeypatch):
    import audit.hooks as hooks

    rows = []

    async def fake_record_generic(**kwargs):
        rows.append(kwargs)

    monkeypatch.setattr(hooks, "record_generic", fake_record_generic)
    return rows
