"""Strict, redacted LETS host-configuration and readiness tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import orchestrator.lets_config as lets_config
from orchestrator.lets_config import (
    INITIAL_GOVERNED_COHORTS,
    LetsConfigError,
    LetsHostConfig,
    load_lets_config,
)

POLICY_DIGEST = "sha256:" + "1" * 64
MACHINE_DIGEST = "sha256:" + "2" * 64


def _authenticate(_raw: bytes, _document: object) -> bool:
    return True


def _write_manifest(path: Path, endpoint: str = "https://warden.example") -> None:
    resources = [{"id": f"scope-{index}", "unit": "call"} for index in range(6)]
    path.write_text(
        json.dumps(
            {
                "api_version": "lets.manifest/v1",
                "tenant_id": "tenant-a",
                "envelope_id": "agents-a",
                "config_epoch": 7,
                "created_at": "2026-08-14T00:00:00Z",
                "resources": resources,
                "initial_budget": [100] * 6,
                "wardens": [
                    {
                        "warden_id": "warden-a",
                        "peer_endpoint": endpoint,
                        "client_endpoint": endpoint,
                        "initial_share": [100] * 6,
                        "keys": [],
                    }
                ],
                "policies": [
                    {
                        "policy_id": "astral",
                        "policy_version": "1",
                        "policy_digest": POLICY_DIGEST,
                        "machine_digest": MACHINE_DIGEST,
                        "resources": resources,
                        "machine": {"machine_digest": MACHINE_DIGEST},
                        "max_lease_ttl_ns": 120_000_000_000,
                    }
                ],
                "signatures": [
                    {
                        "key_id": "operator-a",
                        "algorithm": "Ed25519",
                        "signature": "not-verified-by-the-parser",
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _active_environment(
    tmp_path: Path,
    *,
    mode: str = "enforce",
    endpoint: str = "https://warden.example",
    environment: str = "production",
) -> dict[str, str]:
    token = tmp_path / "service-token"
    token.write_bytes(b"\xff\x00opaque-token")
    manifest = tmp_path / "trust-manifest.json"
    _write_manifest(manifest, endpoint)
    executor_db = tmp_path / "executor-db"
    authority = tmp_path / "executor-authority"
    executor_db.mkdir()
    authority.mkdir()
    return {
        "ASTRAL_ENV": environment,
        "FF_LETS_EXTERNAL_WARDEN": "true",
        "LETS_MODE": mode,
        "LETS_GOVERNED_COHORTS": "server_dynamic,byo_user",
        "LETS_GOVERNED_AGENT_ALLOWLIST": "agent-a,agent-b",
        "LETS_WARDEN_URL": endpoint,
        "LETS_SERVICE_TOKEN_FILE": str(token),
        "LETS_TENANT_ID": "tenant-a",
        "LETS_ENVELOPE_ID": "agents-a",
        "LETS_POLICY_DIGEST": POLICY_DIGEST,
        "LETS_MACHINE_DIGEST": MACHINE_DIGEST,
        "LETS_DEFAULT_ALLOCATION": "2,2,2,2,2,2",
        "LETS_DEFAULT_TTL_SECONDS": "60",
        "LETS_REQUEST_TIMEOUT_SECONDS": "2.5",
        "LETS_REQUEST_ATTEMPTS": "2",
        "LETS_SIGNED_TRUST_MANIFEST": str(manifest),
        "LETS_EXECUTOR_INSTANCE_ID": "astral-gateway-a",
        "LETS_EXECUTOR_DB_ROOT": str(executor_db),
        "LETS_EXECUTOR_AUTHORITY_ROOT": str(authority),
    }


def test_off_is_safe_default_and_ignores_dormant_values() -> None:
    loaded = load_lets_config(
        {
            "LETS_MODE": "off",
            "LETS_WARDEN_URL": "not a URL",
            "LETS_SERVICE_TOKEN_FILE": "missing",
        }
    )

    assert loaded.config is not None
    assert loaded.config.master_enabled is False
    assert loaded.config.governed_cohorts == INITIAL_GOVERNED_COHORTS
    assert loaded.readiness.status == "disabled"
    assert loaded.readiness.application_ready is True
    assert loaded.readiness.governed_effects_permitted is True


def test_valid_enforce_configuration_is_bounded_and_redacted(tmp_path: Path) -> None:
    values = _active_environment(tmp_path)
    ca_bundle = tmp_path / "ca.pem"
    client_cert = tmp_path / "client.pem"
    client_key = tmp_path / "client.key"
    for path in (ca_bundle, client_cert, client_key):
        path.write_bytes(b"nonempty")
    values.update(
        {
            "LETS_CA_BUNDLE": str(ca_bundle),
            "LETS_CLIENT_CERT_FILE": str(client_cert),
            "LETS_CLIENT_KEY_FILE": str(client_key),
        }
    )

    loaded = load_lets_config(values, authenticate_manifest=_authenticate)
    config = loaded.config

    assert config is not None
    assert config.mode == "enforce"
    assert config.warden_origin == "https://warden.example"
    assert config.default_allocation == (2, 2, 2, 2, 2, 2)
    assert config.default_ttl_seconds == 60
    assert config.request_timeout_seconds == 2.5
    assert config.request_attempts == 2
    assert config.follow_redirects is False
    assert config.tls_verification_required is True
    assert config.trust_manifest is not None
    assert config.trust_manifest.config_epoch == 7
    assert loaded.readiness.status == "configured"
    assert loaded.readiness.governed_effects_permitted is True

    rendered = repr(config) + repr(config.redacted())
    for sensitive in (
        values["LETS_SERVICE_TOKEN_FILE"],
        values["LETS_CLIENT_KEY_FILE"],
        values["LETS_SIGNED_TRUST_MANIFEST"],
        "opaque-token",
    ):
        assert sensitive not in rendered


@pytest.mark.parametrize("mode", ["", "ENFORCE", " enforce", "audit"])
def test_invalid_mode_is_a_startup_error(mode: str) -> None:
    with pytest.raises(LetsConfigError, match="^invalid_lets_mode$"):
        load_lets_config({"FF_LETS_EXTERNAL_WARDEN": "true", "LETS_MODE": mode})


def test_active_mode_requires_master_flag() -> None:
    with pytest.raises(LetsConfigError, match="^master_flag_disabled$"):
        load_lets_config({"LETS_MODE": "enforce"})
    with pytest.raises(LetsConfigError, match="^invalid_master_flag$"):
        load_lets_config({"FF_LETS_EXTERNAL_WARDEN": "sometimes", "LETS_MODE": "off"})


@pytest.mark.parametrize(
    ("mode", "application_ready", "effects_permitted", "status"),
    [
        ("shadow", True, True, "degraded"),
        ("enforce", False, False, "blocked"),
    ],
)
def test_missing_secret_has_mode_specific_readiness(
    tmp_path: Path,
    mode: str,
    application_ready: bool,
    effects_permitted: bool,
    status: str,
) -> None:
    values = _active_environment(tmp_path, mode=mode)
    secret_path = values["LETS_SERVICE_TOKEN_FILE"]
    Path(secret_path).unlink()

    with pytest.raises(LetsConfigError, match="^invalid_service_token_file$") as caught:
        LetsHostConfig.from_environ(values, authenticate_manifest=_authenticate)
    assert secret_path not in str(caught.value)

    loaded = load_lets_config(values, authenticate_manifest=_authenticate)
    assert loaded.config is None
    assert loaded.readiness.reason == "invalid_service_token_file"
    assert loaded.readiness.status == status
    assert loaded.readiness.application_ready is application_ready
    assert loaded.readiness.governed_effects_permitted is effects_permitted


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://warden.example", "insecure_warden_url"),
        ("https://user:secret@warden.example", "invalid_warden_url"),
        ("https://warden.example/redirect", "invalid_warden_url"),
        ("https://warden.example?next=other", "invalid_warden_url"),
        ("https://warden.example#fragment", "invalid_warden_url"),
    ],
)
def test_production_url_denials_are_value_free(
    tmp_path: Path,
    url: str,
    reason: str,
) -> None:
    values = _active_environment(tmp_path)
    values["LETS_WARDEN_URL"] = url
    loaded = load_lets_config(values, authenticate_manifest=_authenticate)
    assert loaded.config is None
    assert loaded.readiness.reason == reason
    assert url not in repr(loaded.readiness)


def test_explicit_development_allows_http_but_never_redirects(tmp_path: Path) -> None:
    values = _active_environment(
        tmp_path,
        endpoint="http://127.0.0.1:8080",
        environment="development",
    )
    values.pop("LETS_EXECUTOR_AUTHORITY_ROOT")

    config = LetsHostConfig.from_environ(
        values,
        authenticate_manifest=_authenticate,
    )

    assert config.warden_origin == "http://127.0.0.1:8080"
    assert config.executor_authority_root is None
    assert config.follow_redirects is False


def test_production_requires_independent_executor_authority_root(
    tmp_path: Path,
) -> None:
    values = _active_environment(tmp_path)
    values.pop("LETS_EXECUTOR_AUTHORITY_ROOT")

    loaded = load_lets_config(values, authenticate_manifest=_authenticate)

    assert loaded.config is None
    assert loaded.readiness.reason == "missing_executor_authority_root"
    assert loaded.readiness.status == "blocked"


def test_manifest_must_be_authenticated_and_identity_bound(tmp_path: Path) -> None:
    values = _active_environment(tmp_path)

    denied = load_lets_config(values, authenticate_manifest=lambda _raw, _doc: False)
    assert denied.readiness.reason == "trust_manifest_authentication_failed"

    values["LETS_POLICY_DIGEST"] = "sha256:" + "3" * 64
    mismatched = load_lets_config(values, authenticate_manifest=_authenticate)
    assert mismatched.readiness.reason == "trust_manifest_policy_mismatch"


@pytest.mark.parametrize(
    ("name", "value", "reason"),
    [
        (
            "LETS_GOVERNED_COHORTS",
            "server_dynamic,external",
            "invalid_governed_cohorts",
        ),
        ("LETS_DEFAULT_ALLOCATION", "1,1,1,1,1", "invalid_default_allocation"),
        ("LETS_DEFAULT_ALLOCATION", "0,0,0,0,0,0", "invalid_default_allocation"),
        ("LETS_REQUEST_TIMEOUT_SECONDS", "31", "invalid_request_timeout"),
        ("LETS_REQUEST_ATTEMPTS", "4", "invalid_request_attempts"),
    ],
)
def test_malformed_active_settings_fail_closed(
    tmp_path: Path,
    name: str,
    value: str,
    reason: str,
) -> None:
    values = _active_environment(tmp_path)
    values[name] = value

    loaded = load_lets_config(values, authenticate_manifest=_authenticate)

    assert loaded.config is None
    assert loaded.readiness.reason == reason
    assert loaded.readiness.governed_effects_permitted is False


def test_mtls_identity_requires_both_files(tmp_path: Path) -> None:
    values = _active_environment(tmp_path)
    cert = tmp_path / "client.pem"
    cert.write_bytes(b"certificate")
    values["LETS_CLIENT_CERT_FILE"] = str(cert)

    loaded = load_lets_config(values, authenticate_manifest=_authenticate)

    assert loaded.readiness.reason == "incomplete_mtls_identity"
    assert str(cert) not in repr(loaded.readiness)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        (" YES ", True),
        ("off", False),
        (" False ", False),
    ],
)
def test_master_flag_accepts_only_explicit_boolean_spellings(
    raw: str,
    expected: bool,
) -> None:
    assert lets_config._master_flag({"FF_LETS_EXTERNAL_WARDEN": raw}) is expected


def test_scalar_parsers_reject_wrong_types_and_control_characters() -> None:
    with pytest.raises(LetsConfigError, match="^invalid_master_flag$"):
        lets_config._master_flag({"FF_LETS_EXTERNAL_WARDEN": 1})
    with pytest.raises(LetsConfigError, match="^invalid_lets_mode$"):
        lets_config._mode({"LETS_MODE": None})
    assert lets_config._environment({"ASTRAL_ENV": None}) == "production"
    assert lets_config._environment({"ASTRAL_ENV": " TeSt "}) == "test"

    for value in (None, "", " padded ", "line\nbreak", "delete\x7f"):
        with pytest.raises(LetsConfigError, match="^invalid_value$"):
            lets_config._required({"VALUE": value}, "VALUE", "invalid_value")


@pytest.mark.parametrize(
    "raw",
    [None, "", "server_dynamic,server_dynamic", "server_dynamic,", "external"],
)
def test_governed_cohort_parser_rejects_ambiguous_sets(raw: object) -> None:
    with pytest.raises(LetsConfigError, match="^invalid_governed_cohorts$"):
        lets_config._governed_cohorts({"LETS_GOVERNED_COHORTS": raw})


@pytest.mark.parametrize(
    "raw",
    [None, "agent-a,agent-a", "agent-a,", "bad\x7fagent"],
)
def test_agent_allowlist_rejects_noncanonical_entries(raw: object) -> None:
    with pytest.raises(
        LetsConfigError,
        match="^invalid_governed_agent_allowlist$",
    ):
        lets_config._agent_allowlist({"LETS_GOVERNED_AGENT_ALLOWLIST": raw})


def test_agent_allowlist_blank_value_is_an_empty_allowlist() -> None:
    assert lets_config._agent_allowlist({"LETS_GOVERNED_AGENT_ALLOWLIST": "  "}) == ()
    assert lets_config._agent_allowlist(
        {"LETS_GOVERNED_AGENT_ALLOWLIST": " agent-a, agent-b "}
    ) == ("agent-a", "agent-b")


@pytest.mark.parametrize(
    "url",
    [
        "https://warden.example:invalid",
        "ftp://warden.example",
        "https://",
        "https://warden.example:0",
        "https://bad_label.example",
        "https://123.456.789.999",
        "https://wärden.example",
        "https://warden.example\\escape",
        f"https://{'a' * 250}.example",
    ],
)
def test_warden_origin_rejects_noncanonical_authorities(url: str) -> None:
    with pytest.raises(LetsConfigError):
        lets_config._warden_origin(url, production=True)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://WARDEN.EXAMPLE:443/", "https://warden.example"),
        ("https://127.0.0.1:8443", "https://127.0.0.1:8443"),
        ("https://[2001:0db8::1]:444", "https://[2001:db8::1]:444"),
    ],
)
def test_warden_origin_canonicalizes_valid_hosts(url: str, expected: str) -> None:
    assert lets_config._warden_origin(url, production=True) == expected


def test_file_reference_helpers_reject_missing_and_unsafe_paths(tmp_path: Path) -> None:
    with pytest.raises(LetsConfigError, match="^missing$"):
        lets_config._required_file(
            {},
            "FILE",
            missing_code="missing",
            invalid_code="invalid",
        )
    assert lets_config._optional_file({}, "FILE", invalid_code="invalid") is None
    assert (
        lets_config._optional_file(
            {"FILE": "  "},
            "FILE",
            invalid_code="invalid",
        )
        is None
    )
    with pytest.raises(LetsConfigError, match="^invalid$"):
        lets_config._optional_file({"FILE": 1}, "FILE", invalid_code="invalid")

    empty = tmp_path / "empty"
    empty.touch()
    for value in ("relative", str(tmp_path / "missing"), str(tmp_path), str(empty)):
        with pytest.raises(LetsConfigError, match="^invalid$"):
            lets_config._file_reference(value, "invalid")

    secret = tmp_path / "secret"
    secret.write_bytes(b"opaque")
    reference = lets_config._file_reference(str(secret), "invalid")
    assert reference.size_bytes == 6
    assert reference.redacted() == "<configured>"


def test_identifier_digest_and_numeric_helpers_cover_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for value in ("", "toolong", " padded", "bad\x00value"):
        with pytest.raises(LetsConfigError, match="^invalid_identifier$"):
            lets_config._require_identifier_value(
                value,
                "invalid_identifier",
                maximum=5,
            )
    assert (
        lets_config._require_identifier_value("valid", "invalid_identifier", maximum=5)
        == "valid"
    )

    for value in (None, 1, "sha256:ABC", "sha256:" + "0" * 63):
        with pytest.raises(LetsConfigError, match="^invalid_digest$"):
            lets_config._digest({"DIGEST": value}, "DIGEST", "invalid_digest")
    assert (
        lets_config._digest(
            {"DIGEST": "sha256:" + "a" * 64},
            "DIGEST",
            "invalid_digest",
        )
        == "sha256:" + "a" * 64
    )

    for value in (None, "1,2", "1,1,1,1,1,-1", f"{1 << 63},1,1,1,1,1"):
        with pytest.raises(LetsConfigError, match="^invalid_default_allocation$"):
            lets_config._allocation({"LETS_DEFAULT_ALLOCATION": value})
    assert lets_config._allocation({"LETS_DEFAULT_ALLOCATION": "0,1,0,0,0,0"}) == (
        0,
        1,
        0,
        0,
        0,
        0,
    )
    monkeypatch.setattr(
        lets_config,
        "validate_allocation",
        lambda _allocation: (_ for _ in ()).throw(ValueError("denied")),
    )
    with pytest.raises(LetsConfigError, match="^invalid_default_allocation$"):
        lets_config._allocation({"LETS_DEFAULT_ALLOCATION": "1,1,1,1,1,1"})


@pytest.mark.parametrize("raw", [None, "0", "01", "4"])
def test_positive_integer_parser_rejects_invalid_values(raw: object) -> None:
    with pytest.raises(LetsConfigError, match="^invalid_integer$"):
        lets_config._positive_integer(
            {"VALUE": raw},
            "VALUE",
            "invalid_integer",
            maximum=3,
        )


def test_timeout_and_path_helpers_cover_invalid_boundaries(tmp_path: Path) -> None:
    for raw in (None, "1e2", "0", "9" * 400):
        with pytest.raises(LetsConfigError, match="^invalid_request_timeout$"):
            lets_config._request_timeout({"LETS_REQUEST_TIMEOUT_SECONDS": raw})
    assert lets_config._request_timeout({"LETS_REQUEST_TIMEOUT_SECONDS": "30"}) == 30

    for raw in (None, "", " relative ", "relative"):
        with pytest.raises(LetsConfigError, match="^invalid_path$"):
            lets_config._required_path_text({"PATH": raw}, "PATH", "invalid_path")
    absolute = tmp_path / "manifest.json"
    assert (
        lets_config._required_path_text(
            {"PATH": str(absolute)},
            "PATH",
            "invalid_path",
        )
        == absolute
    )


def test_data_root_helpers_reject_repository_and_non_directory_paths(
    tmp_path: Path,
) -> None:
    with pytest.raises(LetsConfigError, match="^missing$"):
        lets_config._required_data_root({}, "ROOT", "missing", "invalid")
    assert lets_config._optional_data_root({}, "ROOT", "invalid") is None
    assert lets_config._optional_data_root({"ROOT": " "}, "ROOT", "invalid") is None
    with pytest.raises(LetsConfigError, match="^invalid$"):
        lets_config._optional_data_root({"ROOT": 1}, "ROOT", "invalid")

    regular_file = tmp_path / "file"
    regular_file.write_text("not a directory", encoding="utf-8")
    for raw in (
        "relative",
        str(tmp_path / "missing"),
        str(regular_file),
        str(lets_config._REPOSITORY_ROOT),
        str(lets_config._REPOSITORY_ROOT / "backend"),
    ):
        with pytest.raises(LetsConfigError, match="^invalid$"):
            lets_config._data_root(raw, "invalid")
    assert lets_config._data_root(str(tmp_path), "invalid") == tmp_path.resolve()


def _rewrite_manifest(values: dict[str, str], case: str) -> None:
    path = Path(values["LETS_SIGNED_TRUST_MANIFEST"])
    document = json.loads(path.read_text(encoding="utf-8"))
    if case == "unknown_field":
        document["unknown"] = True
    elif case == "api_version":
        document["api_version"] = "lets.manifest/v2"
    elif case == "unsigned":
        document["signatures"] = []
    elif case == "signature_algorithm":
        document["signatures"][0]["algorithm"] = "RSA"
    elif case == "signature_key":
        document["signatures"][0]["key_id"] = None
    elif case == "signature_value":
        document["signatures"][0]["signature"] = None
    elif case == "tenant":
        document["tenant_id"] = "other"
    elif case == "envelope":
        document["envelope_id"] = "other"
    elif case == "epoch_bool":
        document["config_epoch"] = True
    elif case == "epoch_zero":
        document["config_epoch"] = 0
    elif case == "resources_type":
        document["resources"] = {}
    elif case == "resources_length":
        document["resources"].pop()
    elif case == "resource_item":
        document["resources"][0] = None
    elif case == "wardens_empty":
        document["wardens"] = []
    elif case == "warden_item":
        document["wardens"] = [None]
    elif case == "warden_fields":
        document["wardens"][0].pop("client_endpoint")
    elif case == "warden_endpoint":
        document["wardens"][0]["client_endpoint"] = "invalid"
    elif case == "warden_mismatch":
        document["wardens"][0]["client_endpoint"] = "https://other.example"
    elif case == "warden_duplicate":
        document["wardens"].append(document["wardens"][0].copy())
    elif case == "warden_id":
        document["wardens"][0]["warden_id"] = "x" * 129
    elif case == "policies_empty":
        document["policies"] = []
    elif case == "policy_item":
        document["policies"] = [None]
    elif case == "policy_mismatch":
        document["policies"][0]["policy_digest"] = "sha256:" + "9" * 64
    elif case == "policy_duplicate":
        document["policies"].append(document["policies"][0].copy())
    elif case == "machine":
        document["policies"][0]["machine_digest"] = "sha256:" + "8" * 64
    elif case == "policy_resources":
        document["policies"][0]["resources"] = []
    elif case == "ttl_bool":
        document["policies"][0]["max_lease_ttl_ns"] = True
    elif case == "ttl_zero":
        document["policies"][0]["max_lease_ttl_ns"] = 0
    else:
        raise AssertionError(f"unknown manifest case: {case}")
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("unknown_field", "invalid_signed_trust_manifest"),
        ("api_version", "invalid_signed_trust_manifest"),
        ("unsigned", "unsigned_trust_manifest"),
        ("signature_algorithm", "invalid_signed_trust_manifest"),
        ("signature_key", "invalid_signed_trust_manifest"),
        ("signature_value", "invalid_signed_trust_manifest"),
        ("tenant", "trust_manifest_tenant_mismatch"),
        ("envelope", "trust_manifest_envelope_mismatch"),
        ("epoch_bool", "invalid_signed_trust_manifest"),
        ("epoch_zero", "invalid_signed_trust_manifest"),
        ("resources_type", "trust_manifest_resource_mismatch"),
        ("resources_length", "trust_manifest_resource_mismatch"),
        ("resource_item", "trust_manifest_resource_mismatch"),
        ("wardens_empty", "invalid_signed_trust_manifest"),
        ("warden_item", "invalid_signed_trust_manifest"),
        ("warden_fields", "invalid_signed_trust_manifest"),
        ("warden_endpoint", "invalid_signed_trust_manifest"),
        ("warden_mismatch", "trust_manifest_warden_mismatch"),
        ("warden_duplicate", "trust_manifest_warden_mismatch"),
        ("warden_id", "invalid_signed_trust_manifest"),
        ("policies_empty", "invalid_signed_trust_manifest"),
        ("policy_item", "invalid_signed_trust_manifest"),
        ("policy_mismatch", "trust_manifest_policy_mismatch"),
        ("policy_duplicate", "trust_manifest_policy_mismatch"),
        ("machine", "trust_manifest_machine_mismatch"),
        ("policy_resources", "trust_manifest_resource_mismatch"),
        ("ttl_bool", "invalid_signed_trust_manifest"),
        ("ttl_zero", "invalid_signed_trust_manifest"),
    ],
)
def test_manifest_structure_and_identity_fail_closed(
    tmp_path: Path,
    case: str,
    reason: str,
) -> None:
    values = _active_environment(tmp_path)
    _rewrite_manifest(values, case)

    loaded = load_lets_config(values, authenticate_manifest=_authenticate)

    assert loaded.config is None
    assert loaded.readiness.reason == reason


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        b'{"api_version":"lets.manifest/v1","api_version":"duplicate"}',
        b'{"value":NaN}',
        b"[]",
    ],
)
def test_manifest_requires_strict_json_object(tmp_path: Path, raw: bytes) -> None:
    values = _active_environment(tmp_path)
    Path(values["LETS_SIGNED_TRUST_MANIFEST"]).write_bytes(raw)

    loaded = load_lets_config(values, authenticate_manifest=_authenticate)

    assert loaded.readiness.reason == "invalid_signed_trust_manifest"


def test_manifest_authenticator_exceptions_and_non_boolean_success_fail_closed(
    tmp_path: Path,
) -> None:
    values = _active_environment(tmp_path)

    def broken_authenticator(_raw: bytes, _document: object) -> bool:
        raise RuntimeError("private verifier detail")

    for authenticator in (broken_authenticator, lambda _raw, _document: 1):
        loaded = load_lets_config(values, authenticate_manifest=authenticator)
        assert loaded.readiness.reason == "trust_manifest_authentication_failed"
        assert "private verifier detail" not in repr(loaded.readiness)


def test_manifest_file_and_authenticator_are_mandatory(tmp_path: Path) -> None:
    values = _active_environment(tmp_path)
    without_authenticator = load_lets_config(values)
    assert (
        without_authenticator.readiness.reason
        == "trust_manifest_authenticator_required"
    )

    Path(values["LETS_SIGNED_TRUST_MANIFEST"]).unlink()
    missing = load_lets_config(values, authenticate_manifest=_authenticate)
    assert missing.readiness.reason == "invalid_signed_trust_manifest"


def test_nested_machine_digest_and_shadow_configuration_are_supported(
    tmp_path: Path,
) -> None:
    values = _active_environment(tmp_path, mode="shadow")
    path = Path(values["LETS_SIGNED_TRUST_MANIFEST"])
    document = json.loads(path.read_text(encoding="utf-8"))
    document["policies"][0].pop("machine_digest")
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")

    loaded = load_lets_config(values, authenticate_manifest=_authenticate)

    assert loaded.config is not None
    assert loaded.readiness.status == "configured"
    assert loaded.readiness.diagnostic_only is True


def test_default_ttl_cannot_exceed_authenticated_policy(tmp_path: Path) -> None:
    values = _active_environment(tmp_path)
    values["LETS_DEFAULT_TTL_SECONDS"] = "121"

    loaded = load_lets_config(values, authenticate_manifest=_authenticate)

    assert loaded.readiness.reason == "default_ttl_exceeds_policy"


def test_default_environment_mapping_can_be_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FF_LETS_EXTERNAL_WARDEN", "false")
    monkeypatch.setenv("LETS_MODE", "off")
    monkeypatch.setenv("ASTRAL_ENV", " TEST ")

    direct = LetsHostConfig.from_environ()
    loaded = load_lets_config()

    assert direct.environment == "test"
    assert loaded.config == direct
