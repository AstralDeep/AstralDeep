"""Checks Cresco transport admission before credential delivery and bounds untrusted bulk
responses through the real TLS fixture and the public frame decoder.
"""

import base64
import gzip
import json
import ssl

import pytest

from agents.cresco.wsapi_client import (
    FabricConfig, FabricFault, WsapiClient, build_ssl_context, decode_bulk, encode_bulk, parse_frame,
)
from agents.cresco.mcp_server import MCPServer
from shared.protocol import MCPRequest


def test_pin_mismatch_refuses_before_http_credentials(staged_fabric, bridge_env):
    config = FabricConfig(
        wsapi_url=staged_fabric.wsapi_url, service_key="synthetic-withheld-service-key",
        ca_bundle=staged_fabric.ca_file, allow_private_host="127.0.0.1",
        tls_fingerprint="ab" * 32,
    )
    with pytest.raises(FabricFault) as refused:
        WsapiClient(config).call("listregions", {}, attempts=1)
    assert refused.value.state == "tls"
    assert staged_fabric.upgrade_credentials == []


def test_fingerprint_never_disables_certificate_validation():
    context = build_ssl_context(FabricConfig(
        wsapi_url="wss://fabric.example:8282", service_key="synthetic-key",
        tls_fingerprint="ab" * 32,
    ))
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_bulk_expansion_is_refused_before_json_materialization():
    encoded = base64.b64encode(gzip.compress(json.dumps({"text": "x" * 4_194_304}).encode())).decode()
    with pytest.raises(ValueError):
        decode_bulk(encoded)


def test_invalid_utf8_is_a_typed_protocol_refusal():
    with pytest.raises(FabricFault) as refused:
        parse_frame(b'{"message_payload":{"value":"\xff"}}')
    assert refused.value.state == "protocol"


def test_bulk_encoding_has_a_platform_independent_header():
    encoded = encode_bulk({"limit": 128, "region": "global"})
    assert encoded == "H4sIAAAAAAAC/6tWysnMzSxRsjI0stBRKkpNz8zPU7JSSs/JT0rMUaoFAA/AW+ofAAAA"


def test_sysinfo_retains_bounded_read_only_topology(staged_fabric, bridge_env):
    server = MCPServer()
    response = server.process_request(MCPRequest(request_id="sysinfo", method="tools/call",
        params={"name": "cresco_get_sysinfo", "arguments": {"target": "r1_a1", "user_id": "owner"}}))
    assert response.error is None
    assert response.result["target"] == "r1_a1"
    assert response.result["sysinfo"] == {"platform": "synthetic-node", "cpus": 4}
    assert response.ui_components
    refused = server.process_request(MCPRequest(request_id="unadmitted", method="tools/call",
        params={"name": "cresco_get_sysinfo", "arguments": {"target": "r9_unknown", "user_id": "owner"}}))
    assert refused.error["code"] == "CRESCO_TARGET_REFUSED"
    assert len(staged_fabric.requests) == 1


@pytest.mark.parametrize("failure", ["missing", "unreadable", "invalid_pem"])
@pytest.mark.parametrize("pinned", [False, True])
def test_ca_configuration_failure_is_typed_audited_and_prevents_dial(
    staged_fabric, bridge_env, captured_audit, monkeypatch, tmp_path, failure, pinned,
):
    from agents.cresco import wsapi_client
    from .conftest import SERVICE_KEY

    ca_path = tmp_path / (SERVICE_KEY + "-ca.pem")
    if failure != "missing":
        ca_path.write_text("invalid synthetic PEM" if failure == "invalid_pem" else "synthetic unreadable CA")
    if failure == "unreadable":
        def unavailable_ca(*args, **kwargs):
            raise PermissionError(f"synthetic CA denied: {ca_path}")

        monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", unavailable_ca)
    monkeypatch.setenv("CRESCO_CA_BUNDLE", str(ca_path))
    monkeypatch.setenv("CRESCO_TLS_FINGERPRINT", staged_fabric.fingerprint if pinned else "")

    def forbidden_connect(*args, **kwargs):
        pytest.fail("invalid CA configuration reached credential-bearing connect")

    monkeypatch.setattr(wsapi_client, "connect", forbidden_connect)
    response = MCPServer().process_request(MCPRequest(request_id="invalid-ca", method="tools/call",
        params={"name": "cresco_list_regions", "arguments": {"user_id": "owner"}}))
    assert response.error["code"] == "CRESCO_TLS_FAILED"
    assert response.error["data"]["detail"] == "trust_configuration_failed"
    assert response.error["retryable"] is False
    assert response.result is None
    assert [row["outcome"] for row in captured_audit] == ["in_progress", "failure"]
    assert captured_audit[-1]["outcome_detail"] == "trust_configuration_failed"
    assert str(ca_path) not in json.dumps(response.error)
    assert SERVICE_KEY not in json.dumps(response.error)
    assert SERVICE_KEY not in json.dumps(captured_audit, default=str)
    assert staged_fabric.requests == [] and staged_fabric.upgrade_credentials == []
