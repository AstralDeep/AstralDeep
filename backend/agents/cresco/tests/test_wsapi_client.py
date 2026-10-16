"""Exercises the wsapi client against the staged TLS fabric: canonical envelope and bulk
encoding, frame parsing and correlation, TLS/egress posture, the closed fault set, bounded
reconnect, and every staged failure mode (refusal, drift, drop, timeout).
"""

from __future__ import annotations

import copy
import json
import ssl

import pytest

from agents.cresco import mcp_server as cresco_mcp_server
from agents.cresco import wsapi_client as w
from agents.cresco.wsapi_client import (
    FabricConfig,
    FabricFault,
    UnconfirmedResult,
    WsapiClient,
    backoff_delays,
    build_request,
    build_ssl_context,
    canonical_json,
    classify_payload_error,
    decode_bulk,
    encode_bulk,
    parse_frame,
    pin_matches,
    response_matches,
)

from .conftest import SERVICE_KEY


def test_canonical_bulk_encoding_is_deterministic_and_round_trips():
    value = {"kind": "status", "n": 3, "nested": {"b": 1, "a": 2}}
    encoded = encode_bulk(value)
    assert encoded == encode_bulk(copy.deepcopy(value))
    assert decode_bulk(encoded) == value
    raw = decode_bulk.__globals__["gzip"].decompress(__import__("base64").b64decode(encoded))
    assert raw == canonical_json(value)


def test_decode_bulk_refuses_non_string_payloads():
    with pytest.raises(ValueError):
        decode_bulk(7)


def test_build_request_pins_the_message_info_envelope():
    request = build_request("submitwork", {"region_agent": "r1_a1"}, message_id="fixed")
    assert request["message_info"] == {
        "message_type": "REQUEST", "message_event_type": "EVENT",
        "is_rpc": True, "message_id": "fixed",
    }
    assert request["message_payload"]["action"] == "submitwork"


def test_parse_frame_enforces_the_pinned_shape():
    good = json.dumps({"message_info": {"message_id": "m"},
                       "message_payload": {"action": "result"}})
    assert parse_frame(good)["message_payload"]["action"] == "result"
    assert parse_frame(good.encode())["message_payload"]["action"] == "result"
    for bad in ("not json", "[]", json.dumps({"a": 1}), json.dumps({"message_payload": 3}), 5):
        with pytest.raises(FabricFault) as fault:
            parse_frame(bad)
        assert fault.value.state == "protocol" and not fault.value.retryable


def test_response_matches_by_info_id_or_work_id():
    frame = {"message_info": {"message_id": "m1"}, "message_payload": {}}
    assert response_matches(frame, "m1")
    assert not response_matches(frame, "m2")
    by_work = {"message_info": {}, "message_payload": {"work_id": "w9"}}
    assert response_matches(by_work, "w9")


def test_payload_errors_map_into_the_closed_fault_set():
    stale = classify_payload_error({"error": "unknown_agent r2_a3"})
    assert stale.state == "protocol" and stale.detail == "stale_target" and not stale.retryable
    auth = classify_payload_error("service key rejected")
    assert auth.state == "auth" and auth.detail == "fabric_rejected_credential"
    other = classify_payload_error({"error": "something else"})
    assert other.state == "protocol" and other.detail == "fabric_reported_failure"
    unknown_state = FabricFault("nonsense", retryable=True)
    assert unknown_state.state == "protocol"


def test_ssl_context_defaults_to_verified_tls():
    context = build_ssl_context(FabricConfig(wsapi_url="wss://f:8282", service_key="k"))
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_ssl_context_honors_the_operator_ca_bundle(staged_fabric):
    context = build_ssl_context(FabricConfig(wsapi_url="wss://f:8282", service_key="k",
                                             ca_bundle=staged_fabric.ca_file))
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_fingerprint_mode_requires_a_well_formed_pin():
    with pytest.raises(FabricFault) as fault:
        build_ssl_context(FabricConfig(wsapi_url="wss://f:8282", service_key="k",
                                       tls_fingerprint="zz"))
    assert fault.value.state == "tls" and fault.value.detail == "malformed_fingerprint"
    context = build_ssl_context(FabricConfig(wsapi_url="wss://f:8282", service_key="k",
                                             tls_fingerprint="ab" * 32))
    assert context.verify_mode == ssl.CERT_NONE


def test_pin_matching():
    config = FabricConfig(wsapi_url="wss://f:8282", service_key="k",
                          tls_fingerprint=hashlib_sha256(b"der"))
    assert pin_matches(config, b"der")
    assert not pin_matches(config, b"other")
    assert not pin_matches(config, "not-bytes")
    assert pin_matches(FabricConfig(wsapi_url="wss://f:8282", service_key="k"), b"anything")


def hashlib_sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def test_config_bounds_and_https_form(monkeypatch):
    monkeypatch.delenv("CRESCO_WSAPI_URL", raising=False)
    config = FabricConfig.from_env({})
    assert not config.is_configured
    config = FabricConfig.from_env({
        "CRESCO_WSAPI_URL": "wss://fabric.example:8282",
        "CRESCO_SERVICE_KEY": "k",
        "CRESCO_RPC_TIMEOUT": "9999",
        "CRESCO_CONNECT_TIMEOUT": "nonsense",
    })
    assert config.https_form == "https://fabric.example:8282"
    assert config.rpc_timeout == 300.0
    assert config.connect_timeout == 10.0
    plain = FabricConfig.from_env({"CRESCO_WSAPI_URL": "fabric.example:8282",
                                   "CRESCO_SERVICE_KEY": "k"})
    assert plain.https_form == "https://fabric.example:8282"


def test_validate_egress_refuses_insecure_schemes():
    client = WsapiClient(FabricConfig(wsapi_url="ws://fabric.example:8282", service_key="k"))
    with pytest.raises(FabricFault) as fault:
        client.validate_egress()
    assert fault.value.state == "tls" and fault.value.detail == "insecure_scheme"


def test_validate_egress_blocks_private_hosts_without_the_scoped_opt_in(monkeypatch):
    monkeypatch.delenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", raising=False)
    client = WsapiClient(FabricConfig(wsapi_url="wss://127.0.0.1:8282", service_key="k"))
    with pytest.raises(FabricFault) as fault:
        client.validate_egress()
    assert fault.value.state == "tls" and fault.value.detail == "egress_blocked"


def test_validate_egress_requires_the_opt_in_to_name_the_exact_host(monkeypatch):
    monkeypatch.delenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", raising=False)
    client = WsapiClient(FabricConfig(wsapi_url="wss://127.0.0.1:8282", service_key="k",
                                      allow_private_host="other-host"))
    with pytest.raises(FabricFault) as fault:
        client.validate_egress()
    assert fault.value.detail == "private_host_opt_in_mismatch"
    scoped = WsapiClient(FabricConfig(wsapi_url="wss://127.0.0.1:8282", service_key="k",
                                      allow_private_host="127.0.0.1"))
    scoped.validate_egress()


def test_validate_egress_honors_the_shared_private_host_allowlist(monkeypatch):
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "127.0.0.1")
    client = WsapiClient(FabricConfig(wsapi_url="wss://127.0.0.1:8282", service_key="k"))
    client.validate_egress()


def test_backoff_delays_grow_and_cap():
    assert backoff_delays(1, 0.5, 4.0) == []
    assert backoff_delays(3, 0.5, 4.0) == [0.5, 1.0]
    assert backoff_delays(6, 0.5, 1.2) == [0.5, 1.0, 1.2, 1.2, 1.2]


def test_dial_budget_defaults_are_bounded_and_env_shaped(monkeypatch):
    monkeypatch.delenv("CRESCO_DIAL_ATTEMPTS", raising=False)
    monkeypatch.delenv("CRESCO_BACKOFF_SECONDS", raising=False)
    assert w.dial_attempts({}) == 2 and w.dial_backoff_seconds({}) == 0.25
    assert w.dial_attempts({"CRESCO_DIAL_ATTEMPTS": "9"}) == 3
    assert w.dial_attempts({"CRESCO_DIAL_ATTEMPTS": "0"}) == 1
    assert w.dial_attempts({"CRESCO_DIAL_ATTEMPTS": "junk"}) == 2
    assert w.dial_backoff_seconds({"CRESCO_BACKOFF_SECONDS": "-1"}) == 0.0
    assert w.dial_backoff_seconds({"CRESCO_BACKOFF_SECONDS": "99"}) == 2.0
    assert w.dial_backoff_seconds({"CRESCO_BACKOFF_SECONDS": "junk"}) == 0.25


def test_dial_retries_only_retryable_faults():
    attempts = []

    def failing_connect():
        attempts.append(1)
        raise FabricFault("unreachable", retryable=True)

    def refusing_connect():
        attempts.append(1)
        raise FabricFault("auth", retryable=False)

    retrier = WsapiClient(FabricConfig(wsapi_url="wss://f:8282", service_key="k"))
    retrier._connect = failing_connect
    with pytest.raises(FabricFault) as exhausted:
        retrier.dial(attempts=3, base_delay=0.0)
    assert exhausted.value.state == "unreachable" and len(attempts) == 3

    attempts.clear()
    hard = WsapiClient(FabricConfig(wsapi_url="wss://f:8282", service_key="k"))
    hard._connect = refusing_connect
    with pytest.raises(FabricFault) as refused:
        hard.dial(attempts=3, base_delay=0.0)
    assert refused.value.state == "auth" and len(attempts) == 1


def test_call_completes_over_the_staged_tls_fabric(staged_fabric, bridge_env):
    client = WsapiClient(FabricConfig.from_env())
    payload = client.call("listregions", {})
    assert [region["region"] for region in payload["regions"]] == ["r1", "r2"]
    assert staged_fabric.requests[-1]["action"] == "listregions"


def test_call_skips_stray_frames_and_decodes_bulk_results(staged_fabric, bridge_env):
    staged_fabric.behavior = "stray_then_result"
    client = WsapiClient(FabricConfig.from_env())
    payload = client.call("agentinfo", {"region_agent": "r1_a1"})
    assert payload["info"]["region_agent"] == "r1_a1"
    assert payload["info"]["plugins"] == ["executor"]


def test_call_maps_fabric_auth_rejection(staged_fabric, bridge_env):
    staged_fabric.errors_by_target["r1_a1"] = "invalid service key"
    client = WsapiClient(FabricConfig.from_env())
    with pytest.raises(FabricFault) as fault:
        client.call("agentinfo", {"region_agent": "r1_a1"})
    assert fault.value.state == "auth" and not fault.value.retryable


def test_call_fails_safe_on_malformed_frames(staged_fabric, bridge_env):
    staged_fabric.behavior = "malformed"
    client = WsapiClient(FabricConfig.from_env())
    with pytest.raises(FabricFault) as fault:
        client.call("listregions", {})
    assert fault.value.state == "protocol" and fault.value.after_send


def test_call_reports_a_dropped_connection_as_retryable_after_send(staged_fabric, bridge_env):
    staged_fabric.behavior = "close_after_send"
    client = WsapiClient(FabricConfig.from_env())
    with pytest.raises(FabricFault) as fault:
        client.call("listregions", {})
    assert fault.value.state == "unreachable" and fault.value.retryable
    assert fault.value.after_send


def test_call_bounds_the_rpc_window(staged_fabric, bridge_env):
    staged_fabric.behavior = "never_reply"
    client = WsapiClient(FabricConfig.from_env())
    with pytest.raises(FabricFault) as fault:
        client.call("listregions", {}, timeout=0.2)
    assert fault.value.state == "unreachable" and fault.value.detail == "rpc_window_elapsed"


def test_call_rejects_http_upgrade_authentication(staged_fabric, bridge_env):
    staged_fabric.reject_auth = True
    client = WsapiClient(FabricConfig.from_env())
    with pytest.raises(FabricFault) as fault:
        client.call("listregions", {})
    assert fault.value.state == "auth" and not fault.value.retryable


def test_call_refuses_a_self_signed_certificate_without_trust(staged_fabric, monkeypatch):
    monkeypatch.setitem(cresco_mcp_server.flags._flags, "cresco", True)
    monkeypatch.setenv("CRESCO_WSAPI_URL", staged_fabric.wsapi_url)
    monkeypatch.setenv("CRESCO_SERVICE_KEY", SERVICE_KEY)
    monkeypatch.setenv("CRESCO_ALLOW_PRIVATE_HOST", "127.0.0.1")
    monkeypatch.delenv("CRESCO_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CRESCO_TLS_FINGERPRINT", raising=False)
    client = WsapiClient(FabricConfig.from_env())
    with pytest.raises(FabricFault) as fault:
        client.call("listregions", {})
    assert fault.value.state == "tls" and not fault.value.retryable


def test_call_accepts_a_pinned_fingerprint(staged_fabric, monkeypatch):
    monkeypatch.setitem(cresco_mcp_server.flags._flags, "cresco", True)
    monkeypatch.setenv("CRESCO_WSAPI_URL", staged_fabric.wsapi_url)
    monkeypatch.setenv("CRESCO_SERVICE_KEY", SERVICE_KEY)
    monkeypatch.setenv("CRESCO_ALLOW_PRIVATE_HOST", "127.0.0.1")
    monkeypatch.delenv("CRESCO_CA_BUNDLE", raising=False)
    monkeypatch.setenv("CRESCO_TLS_FINGERPRINT", staged_fabric.fingerprint)
    client = WsapiClient(FabricConfig.from_env())
    payload = client.call("listregions", {})
    assert payload["regions"]


def test_call_rejects_a_mismatched_fingerprint(staged_fabric, monkeypatch):
    monkeypatch.setitem(cresco_mcp_server.flags._flags, "cresco", True)
    monkeypatch.setenv("CRESCO_WSAPI_URL", staged_fabric.wsapi_url)
    monkeypatch.setenv("CRESCO_SERVICE_KEY", SERVICE_KEY)
    monkeypatch.setenv("CRESCO_ALLOW_PRIVATE_HOST", "127.0.0.1")
    monkeypatch.delenv("CRESCO_CA_BUNDLE", raising=False)
    monkeypatch.setenv("CRESCO_TLS_FINGERPRINT", "ab" * 32)
    client = WsapiClient(FabricConfig.from_env())
    with pytest.raises(FabricFault) as fault:
        client.call("listregions", {})
    assert fault.value.state == "tls" and fault.value.detail == "fingerprint_mismatch"


def test_call_fails_closed_on_an_unresolvable_host(monkeypatch):
    monkeypatch.setitem(cresco_mcp_server.flags._flags, "cresco", True)
    monkeypatch.delenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", raising=False)
    client = WsapiClient(FabricConfig.from_env({
        "CRESCO_WSAPI_URL": "wss://cresco.invalid.example:8282",
        "CRESCO_SERVICE_KEY": SERVICE_KEY,
    }))
    with pytest.raises(FabricFault) as fault:
        client.call("listregions", {})
    assert fault.value.state == "tls" and fault.value.detail == "egress_blocked"


def test_submit_work_completes_with_a_correlated_result(staged_fabric, bridge_env):
    staged_fabric.work_results["default"] = {"accepted": True, "kind": "status", "n": 2}
    client = WsapiClient(FabricConfig.from_env())
    outcome = client.submit_work("r1_a1", "work-1",
                                 {"kind": "status", "payload_text": "hello",
                                  "requested_by": "user-1"})
    assert outcome["work_id"] == "work-1"
    assert outcome["result"]["accepted"] is True
    sent = staged_fabric.requests[-1]
    assert sent["action"] == "submitwork" and sent["region_agent"] == "r1_a1"
    assert decode_bulk(sent["work_bulk"])["requested_by"] == "user-1"


def test_submit_work_keeps_a_stale_target_a_clean_refusal(staged_fabric, bridge_env):
    staged_fabric.errors_by_target["r2_a3"] = "unknown_agent r2_a3 (revoked)"
    client = WsapiClient(FabricConfig.from_env())
    with pytest.raises(FabricFault) as fault:
        client.submit_work("r2_a3", "work-2", {"kind": "status"})
    assert fault.value.detail == "stale_target" and fault.value.after_send is False


def test_submit_work_marks_a_dropped_connection_unconfirmed(staged_fabric, bridge_env):
    staged_fabric.behavior = "close_after_send"
    client = WsapiClient(FabricConfig.from_env())
    with pytest.raises(FabricFault) as fault:
        client.submit_work("r1_a1", "work-3", {"kind": "status"})
    assert fault.value.after_send is True and fault.value.state == "unreachable"


def test_submit_work_marks_a_late_result_unconfirmed(staged_fabric, bridge_env):
    staged_fabric.behavior = "never_reply"
    client = WsapiClient(FabricConfig.from_env())
    with pytest.raises(UnconfirmedResult) as unconfirmed:
        client.submit_work("r1_a1", "work-4", {"kind": "status"}, timeout=0.2)
    assert unconfirmed.value.work_id == "work-4"
    assert unconfirmed.value.reason == "result_window_elapsed"


def test_submit_work_marks_malformed_results_unconfirmed(staged_fabric, bridge_env):
    staged_fabric.behavior = "malformed"
    client = WsapiClient(FabricConfig.from_env())
    with pytest.raises(FabricFault) as fault:
        client.submit_work("r1_a1", "work-5", {"kind": "status"})
    assert fault.value.after_send is True and fault.value.state == "protocol"


def test_submit_work_reports_undecodable_result_bulk_unconfirmed(staged_fabric, bridge_env):
    staged_fabric.behavior = "bad_bulk"
    client = WsapiClient(FabricConfig.from_env())
    with pytest.raises(UnconfirmedResult) as unconfirmed:
        client.submit_work("r1_a1", "work-6", {"kind": "status"})
    assert unconfirmed.value.reason == "undecodable_result_bulk"


def test_dial_against_a_dead_port_retries_then_fails_unreachable(monkeypatch):
    monkeypatch.setitem(cresco_mcp_server.flags._flags, "cresco", True)
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "127.0.0.1")
    client = WsapiClient(FabricConfig.from_env({
        "CRESCO_WSAPI_URL": "wss://127.0.0.1:1",
        "CRESCO_SERVICE_KEY": SERVICE_KEY,
        "CRESCO_CONNECT_TIMEOUT": "2",
    }))
    with pytest.raises(FabricFault) as fault:
        client.dial(attempts=2, base_delay=0.0)
    assert fault.value.state == "unreachable" and fault.value.retryable


def test_call_rejects_a_non_auth_upgrade_refusal(staged_fabric, bridge_env):
    staged_fabric.reject_auth = True
    staged_fabric.reject_status = 503
    client = WsapiClient(FabricConfig.from_env())
    with pytest.raises(FabricFault) as fault:
        client.call("listregions", {})
    assert fault.value.state == "unreachable"
    assert fault.value.detail == "fabric_refused_connection"
    assert fault.value.retryable is True


def test_call_bounds_an_immediate_zero_window(staged_fabric, bridge_env):
    client = WsapiClient(FabricConfig.from_env())
    with pytest.raises(FabricFault) as fault:
        client.call("listregions", {}, timeout=0)
    assert fault.value.detail == "rpc_window_elapsed" and fault.value.after_send


def test_call_fails_safe_on_undecodable_result_bulk(staged_fabric, bridge_env):
    staged_fabric.behavior = "bad_bulk"
    client = WsapiClient(FabricConfig.from_env())
    with pytest.raises(FabricFault) as fault:
        client.call("listregions", {})
    assert fault.value.state == "protocol"
    assert fault.value.detail == "undecodable_result_bulk"


def test_call_tolerates_a_non_mapping_bulk_result(staged_fabric, monkeypatch, bridge_env):
    from agents.cresco.wsapi_client import encode_bulk

    monkeypatch.setattr(staged_fabric, "bulk_override", encode_bulk(["scalar"]))
    client = WsapiClient(FabricConfig.from_env())
    assert client.call("listregions", {}) == {}


def test_submit_work_bounds_an_immediate_zero_window(staged_fabric, bridge_env):
    client = WsapiClient(FabricConfig.from_env())
    with pytest.raises(UnconfirmedResult) as unconfirmed:
        client.submit_work("r1_a1", "work-0", {"kind": "status"}, timeout=0)
    assert unconfirmed.value.reason == "result_window_elapsed"
