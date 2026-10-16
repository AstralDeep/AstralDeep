"""Exercises the wsapi client against the staged gateway: allowed read exchanges, every
typed failure, verified-TLS modes, egress gating, bounded retries, and service-key hygiene.
"""

import json

import pytest
from cryptography.hazmat.primitives import serialization
from shared import external_http

from agents.cresco.wsapi_client import (
    CrescoAuthError,
    CrescoInvalidArgument,
    CrescoProtocolError,
    CrescoTLSError,
    CrescoUnavailable,
    CrescoUnreachable,
    FabricConfig,
    WsapiClient,
    decode_compressed,
    encode_compressed,
)

BAD_FINGERPRINT = "ab" * 32
OTHER_FINGERPRINT = "cd" * 32


def _client(**overrides) -> WsapiClient:
    return WsapiClient(FabricConfig.from_environment())


def test_compressed_bulk_params_round_trip():
    payload = {"regions": [{"name": "global", "agents": 4}]}
    encoded = encode_compressed(payload)
    assert isinstance(encoded, str) and encoded.startswith("H4sI")
    assert decode_compressed(encoded) == payload


def test_compressed_decode_rejects_garbage_and_bombs():
    with pytest.raises(CrescoProtocolError):
        decode_compressed("not-base64!!")
    with pytest.raises(CrescoProtocolError):
        decode_compressed(encode_compressed({"blob": "x" * 100})[:-2] + "zz")


def test_config_and_endpoint_parsing(monkeypatch):
    monkeypatch.setenv("CRESCO_WSAPI_URL", "ws://gateway.test:8282")
    monkeypatch.setenv("CRESCO_SERVICE_KEY", "k")
    with pytest.raises(CrescoUnavailable):
        FabricConfig.from_environment().endpoint()
    monkeypatch.setenv("CRESCO_WSAPI_URL", "wss://gateway.test:99999")
    with pytest.raises(CrescoUnavailable):
        FabricConfig.from_environment().endpoint()
    monkeypatch.setenv("CRESCO_WSAPI_URL", "wss://gateway.test:0")
    with pytest.raises(CrescoUnavailable):
        FabricConfig.from_environment().endpoint()
    monkeypatch.setenv("CRESCO_WSAPI_URL", "wss://gateway.test")
    host, port = FabricConfig.from_environment().endpoint()
    assert (host, port) == ("gateway.test", 8282)


def test_invalid_fingerprint_format_is_unavailable(monkeypatch):
    monkeypatch.setenv("CRESCO_WSAPI_URL", "wss://gateway.test")
    monkeypatch.setenv("CRESCO_SERVICE_KEY", "k")
    monkeypatch.setenv("CRESCO_TLS_FINGERPRINT", "zz" * 32)
    with pytest.raises(CrescoUnavailable):
        FabricConfig.from_environment().tls_context()


def test_non_json_bulk_param_is_rejected():
    with pytest.raises(CrescoInvalidArgument):
        encode_compressed({"bad": {1, 2}})


def test_compressed_decode_enforces_expansion_bound():
    with pytest.raises(CrescoProtocolError):
        decode_compressed(encode_compressed({"pad": "x" * (4 << 20)}))


def test_compressed_decode_rejects_oversized_input():
    with pytest.raises(CrescoProtocolError):
        decode_compressed("H4sI" + "A" * ((1 << 20) + 8))


def test_failure_classifier_covers_transport_kinds():
    import ssl

    from websockets.datastructures import Headers
    from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
    from websockets.http11 import Response

    from agents.cresco.wsapi_client import _typed_failure

    assert isinstance(_typed_failure(ssl.SSLError("no shared cipher")), CrescoTLSError)
    assert isinstance(
        _typed_failure(InvalidStatus(Response(401, "denied", Headers()))), CrescoAuthError
    )
    assert isinstance(
        _typed_failure(InvalidStatus(Response(503, "busy", Headers()))), CrescoUnreachable
    )
    assert isinstance(_typed_failure(InvalidHandshake("bad handshake")), CrescoUnreachable)
    assert isinstance(_typed_failure(TimeoutError("late")), CrescoUnreachable)
    assert isinstance(_typed_failure(OSError("refused")), CrescoUnreachable)
    assert isinstance(_typed_failure(ConnectionClosed(1006, None)), CrescoUnreachable)
    assert isinstance(_typed_failure(ValueError("surprising")), CrescoUnreachable)


def test_unconfigured_client_reports_unavailable():
    with pytest.raises(CrescoUnavailable):
        WsapiClient(FabricConfig()).call("listregions")
    with pytest.raises(CrescoUnavailable):
        WsapiClient(FabricConfig(url="wss://gateway.test")).call("listregions")


def test_allowed_read_exchange_over_verified_tls(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    reply = _client().call("listregions")
    assert [region["region"] for region in reply["regions"]] == ["global", "edge-west"]
    assert gateway.captured.get() == "staged-cresco-service-key"


def test_all_read_verbs_and_region_filter(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    client = _client()
    assert len(client.call("listagents")["agents"]) == 3
    filtered = client.call("listagents", {"region": "global"}, region="global")
    assert [agent["agent"] for agent in filtered["agents"]] == ["agent-1", "agent-2"]
    info = client.call("getagentinfo", {"region": "global", "agent": "agent-1"}, region="global", agent="agent-1")
    assert info["agent_status"] == "connected" and len(info["plugins"]) == 2
    sysinfo = client.call("getsysinfo", {"region": "global", "agent": "agent-1"}, region="global", agent="agent-1")
    assert sysinfo["sysinfo"]["hostname"] == "edge-host-1"


def test_envelope_carries_pinned_fields(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    _client().call("getagentinfo", {"region": "r", "agent": "a"}, region="r", agent="a")
    envelope = gateway.requests[0]
    info = envelope["message_info"]
    payload = envelope["message_payload"]
    assert info["is_rpc"] == "true" and info["message_type"] == "global_plugin_msgevent"
    assert info["dst_region"] == "r" and info["dst_agent"] == "a"
    assert info["rpc_timeout_ms"].isdigit()
    assert payload["action"] == "getagentinfo" and payload["region"] == "r" and payload["agent"] == "a"
    assert "cresco_service_key" not in json.dumps(envelope)
    assert len(payload["client_rpc_id"]) == 32


def test_correlation_mismatch_fails_safe(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    gateway.state["mode"] = "corrupt"
    with pytest.raises(CrescoProtocolError):
        _client().call("listregions")


def test_malformed_reply_fails_safe(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    gateway.state["mode"] = "malformed"
    with pytest.raises(CrescoProtocolError):
        _client().call("listregions")


def test_bad_status_fails_safe(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    gateway.state["mode"] = "badstatus"
    with pytest.raises(CrescoProtocolError):
        _client().call("listregions")


def test_rejected_service_key_is_an_auth_failure(gateway, monkeypatch):
    gateway.configure(monkeypatch, key="wrong-key")
    with pytest.raises(CrescoAuthError):
        _client().call("listregions")
    with pytest.raises(CrescoAuthError):
        _client().call("listregions")


def test_closed_gateway_maps_to_unreachable_then_recovers(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    gateway.state["mode"] = "closed"
    with pytest.raises(CrescoUnreachable):
        _client().call("listregions")
    gateway.state["mode"] = "ok"
    assert _client().call("listregions")["regions"]


def test_slow_reply_is_bounded(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    gateway.state["mode"] = "slow"
    with pytest.raises(CrescoUnreachable):
        _client().call("listregions", timeout=1.0)


def test_dial_retry_recovers_from_one_failed_open(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    gateway.state["mode"] = "refused"
    reply = _client().call("listregions")
    assert reply["regions"][0]["region"] == "global"
    assert gateway.captured.qsize() >= 1


def test_connection_refused_without_listener(monkeypatch):
    monkeypatch.setenv("CRESCO_WSAPI_URL", "wss://127.0.0.1:9")
    monkeypatch.setenv("CRESCO_SERVICE_KEY", "k")
    monkeypatch.setenv("CRESCO_ALLOW_PRIVATE_HOST", "true")
    with pytest.raises(CrescoUnreachable):
        _client().call("listregions")


def test_self_signed_rejected_without_trust(gateway, monkeypatch):
    gateway.configure(monkeypatch, ca=False)
    with pytest.raises(CrescoTLSError):
        _client().call("listregions")


def test_ca_bundle_hostname_mismatch_rejected(gateway, monkeypatch, tmp_path):
    from agents.cresco.tests.conftest import _certificate

    other, other_key = _certificate("203.0.113.5")
    mismatch = tmp_path / "mismatch.pem"
    mismatch.write_bytes(other.public_bytes(serialization.Encoding.PEM))
    gateway.configure(monkeypatch, ca=False)
    monkeypatch.setenv("CRESCO_CA_BUNDLE", str(mismatch))
    with pytest.raises(CrescoTLSError):
        _client().call("listregions")


def test_pinned_fingerprint_accepts_exact_cert(gateway, monkeypatch):
    gateway.configure(monkeypatch, ca=False, pin=gateway.fingerprint)
    assert _client().call("listregions")["regions"]
    assert gateway.captured.get() == "staged-cresco-service-key"


def test_pin_mismatch_fails_before_credentials_are_sent(gateway, monkeypatch):
    gateway.configure(monkeypatch, ca=False, pin=OTHER_FINGERPRINT)
    with pytest.raises(CrescoTLSError):
        _client().call("listregions")
    assert gateway.captured.empty


def test_egress_gate_runs_before_any_dial(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    calls = []

    def _gate(url, allowed_private_hosts=None):
        calls.append(url)
        raise external_http.EgressBlockedError("blocked by test")

    monkeypatch.setattr(external_http, "validate_egress_url", _gate)
    with pytest.raises(CrescoUnreachable):
        _client().call("listregions")
    assert calls == [f"https://127.0.0.1:{gateway.port}"]
    assert gateway.captured.empty


def test_private_host_blocked_without_override(gateway, monkeypatch):
    gateway.configure(monkeypatch, allow_private=False)
    with pytest.raises(CrescoUnreachable):
        _client().call("listregions")
    assert gateway.captured.empty


def test_egress_block_is_not_retryable(gateway, monkeypatch):
    gateway.configure(monkeypatch, allow_private=False)
    with pytest.raises(CrescoUnreachable) as excinfo:
        _client().call("listregions")
    assert excinfo.value.retryable is False


def test_oversized_request_is_rejected_locally(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    with pytest.raises(CrescoInvalidArgument):
        _client().call("listregions", {"pad": "x" * (2 << 20)})


def test_non_json_request_params_are_rejected(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    with pytest.raises(CrescoInvalidArgument):
        _client().call("listregions", {"weird": {1, 2}})


def test_unbounded_action_names_are_rejected(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    with pytest.raises(CrescoInvalidArgument):
        _client().call("bad action!")


def test_non_dict_reply_fails_safe(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    gateway.state["mode"] = "nondict"
    with pytest.raises(CrescoProtocolError):
        _client().call("listregions")


def test_compressed_reply_values_are_decoded(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    gateway.state["mode"] = "compressed"
    reply = _client().call("listregions")
    assert [region["region"] for region in reply["regions"]] == ["global", "edge-west"]


def test_compressed_reply_expansion_is_bounded(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    gateway.state["mode"] = "bigcompressed"
    with pytest.raises(CrescoProtocolError):
        _client().call("listregions")


def test_pin_mode_fails_against_a_plain_tcp_peer(gateway, monkeypatch):
    import socket as socket_module
    import threading

    gateway.configure(monkeypatch, ca=False, pin=gateway.fingerprint)
    plain = socket_module.socket()
    plain.bind(("127.0.0.1", 0))
    plain.listen(1)
    port = plain.getsockname()[1]
    accepted = threading.Event()

    def _accept_once():
        connection, _ = plain.accept()
        accepted.set()
        connection.close()

    worker = threading.Thread(target=_accept_once, daemon=True)
    worker.start()
    monkeypatch.setenv("CRESCO_WSAPI_URL", f"wss://127.0.0.1:{port}")
    with pytest.raises(CrescoTLSError):
        _client().call("listregions")
    plain.close()


def test_service_key_never_reaches_failures_or_logs(gateway, monkeypatch, caplog):
    gateway.configure(monkeypatch, key="wrong-key")
    failures = []
    for mode in ("auth", "closed", "malformed", "badstatus"):
        gateway.state["mode"] = mode
        try:
            _client().call("listregions")
        except Exception as exc:
            failures.append(exc)
    gateway.configure(monkeypatch, ca=False)
    try:
        _client().call("listregions")
    except Exception as exc:
        failures.append(exc)
    assert failures
    import logging

    with caplog.at_level(logging.DEBUG):
        for exc in failures:
            assert "staged-cresco-service-key" not in str(exc)
            assert "staged-cresco-service-key" not in repr(exc)
        assert all(
            "staged-cresco-service-key" not in str(record.getMessage())
            for record in caplog.records
        )


def test_multi_value_reply_expansion_is_bounded(gateway, monkeypatch):
    gateway.configure(monkeypatch)
    gateway.state["mode"] = "twocompressed"
    with pytest.raises(CrescoProtocolError):
        _client().call("listregions")


@pytest.mark.parametrize("pinned", [False, True])
def test_failed_open_closes_owned_socket(monkeypatch, pinned):
    import socket
    import ssl

    raw, peer = socket.socketpair()
    upgraded = None
    try:
        client = WsapiClient(FabricConfig(
            url="wss://gateway.test", service_key="synthetic",
            fingerprint=BAD_FINGERPRINT if pinned else "",
        ))
        monkeypatch.setattr(client, "_dial", lambda *_: raw)
        if pinned:
            upgraded = raw.dup()
            raw.close()
            monkeypatch.setattr(client, "_pin_wrap", lambda *_: upgraded)

        def refuse_open(*args, **kwargs):
            raise ssl.SSLError("synthetic opening refusal")

        monkeypatch.setattr("websockets.sync.client.connect", refuse_open)
        with pytest.raises(CrescoTLSError):
            client._open("gateway.test", 8282)
        assert (upgraded if pinned else raw).fileno() == -1
    finally:
        raw.close()
        peer.close()
        if upgraded is not None:
            upgraded.close()
