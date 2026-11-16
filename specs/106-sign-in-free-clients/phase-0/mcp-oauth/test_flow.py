"""Drives adapter.py against fixture.py to show discovery, S256 refusal, registration order, iss refusals and token hygiene."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from adapter import Adapter, Keystore, Refused, Settings, call
from fixture import Fixture, FixtureConfig

SITE = Path(__file__).resolve().parents[1] / "site-drafts"
CIMD_URL = "https://astraldeep.github.io/oauth/client-metadata.json"
CALLBACK = "https://astraldeep.github.io/oauth/callback"
CIMD_DOCUMENT = json.loads((SITE / "oauth" / "client-metadata.json").read_text())


class _Stop(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def simulated_browser(url: str) -> str:
    opener = urllib.request.build_opener(_Stop)
    try:
        opener.open(url, timeout=5)
    except urllib.error.HTTPError as error:
        if error.code == 302:
            return error.headers["Location"]
        raise
    raise AssertionError("the authorization server did not redirect")


def run(fixture: Fixture, **overrides: object) -> tuple[Adapter, Keystore]:
    keystore = Keystore()
    settings = Settings(endpoint=fixture.rs_url, connection_id="conn-1", redirect_uri=CALLBACK,
                        browser=simulated_browser, keystore=keystore, allow_loopback_http=True)
    for name, value in overrides.items():
        setattr(settings, name, value)
    return Adapter(settings), keystore


def no_secret_in(text: str, fixture: Fixture) -> None:
    assert fixture.secrets
    for secret in fixture.secrets:
        assert secret not in text


def test_discovery_from_401_with_resource_metadata_then_bearer_call() -> None:
    with Fixture() as fx:
        adapter, keystore = run(fx)
        connection = adapter.connect()
        assert fx.requests_to("rs")[:2] == ["POST /mcp", "GET /.well-known/oauth-protected-resource/mcp"]
        assert fx.requests_to("as")[0] == "GET /.well-known/oauth-authorization-server"
        assert connection.scope == "files:read"
        assert fx.authorize_params[0]["resource"] == fx.rs_url
        assert fx.token_requests[0]["resource"] == fx.rs_url
        assert fx.authorize_params[0]["code_challenge_method"] == "S256"
        assert keystore.get("conn-1", fx.issuer) is not None
        reply = call(connection, fx.rs_url, "tools/list")
        assert reply["result"]["tools"] == [{"name": "search"}]


def test_401_without_resource_metadata_falls_back_path_then_root() -> None:
    with Fixture(FixtureConfig(challenge_mode="bare", wellknown_at="root", scopes_supported=["a", "b"])) as fx:
        adapter, _ = run(fx)
        connection = adapter.connect()
        assert fx.requests_to("rs")[1:3] == [
            "GET /.well-known/oauth-protected-resource/mcp", "GET /.well-known/oauth-protected-resource"]
        assert connection.scope == "a b"


def test_no_scope_anywhere_omits_the_scope_parameter() -> None:
    with Fixture(FixtureConfig(challenge_scope=None)) as fx:
        run(fx)[0].connect()
        assert "scope" not in fx.authorize_params[0]


@pytest.mark.parametrize(("metadata_at", "issuer_path", "expected"), [
    ("oauth", "", ["GET /.well-known/oauth-authorization-server"]),
    ("openid", "", ["GET /.well-known/oauth-authorization-server", "GET /.well-known/openid-configuration"]),
    ("oauth", "/tenant1", ["GET /.well-known/oauth-authorization-server/tenant1"]),
    ("openid", "/tenant1", ["GET /.well-known/oauth-authorization-server/tenant1",
                            "GET /.well-known/openid-configuration/tenant1"]),
    ("openid_append", "/tenant1", ["GET /.well-known/oauth-authorization-server/tenant1",
                                   "GET /.well-known/openid-configuration/tenant1",
                                   "GET /tenant1/.well-known/openid-configuration"]),
])
def test_authorization_server_metadata_order(metadata_at: str, issuer_path: str, expected: list[str]) -> None:
    with Fixture(FixtureConfig(metadata_at=metadata_at, issuer_path=issuer_path)) as fx:
        run(fx)[0].connect()
        assert fx.requests_to("as")[:len(expected)] == expected


@pytest.mark.parametrize("methods", [None, [], ["plain"]])
def test_missing_s256_is_refused_with_nothing_granted(methods: list[str] | None) -> None:
    with Fixture(FixtureConfig(pkce_methods=methods)) as fx:
        adapter, keystore = run(fx)
        with pytest.raises(Refused) as refusal:
            adapter.connect()
        assert refusal.value.code == "no_s256"
        assert fx.authorize_params == [] and fx.token_requests == [] and fx.registrations == []
        assert keystore.items == {}


def test_second_authorization_server_with_s256_is_used_when_first_lacks_it() -> None:
    with Fixture() as good, Fixture(FixtureConfig(pkce_methods=None)) as first:
        first.config.extra_authorization_servers = (good.issuer,)
        connection = run(first)[0].connect()
        assert connection.issuer == good.issuer
        assert first.authorize_params == [] and len(good.authorize_params) == 1


def test_issuer_mismatch_in_metadata_is_refused() -> None:
    with Fixture(FixtureConfig(metadata_issuer_override="https://honest.example")) as fx:
        adapter, keystore = run(fx)
        with pytest.raises(Refused) as refusal:
            adapter.connect()
        assert refusal.value.code == "issuer_mismatch"
        assert fx.authorize_params == [] and keystore.items == {}


def test_registration_prefers_preregistered_over_everything() -> None:
    config = FixtureConfig(cimd_supported=True, cimd_documents={CIMD_URL: CIMD_DOCUMENT},
                           preregistered_client_ids=("known-client",))
    with Fixture(config) as fx:
        adapter, _ = run(fx, client_metadata_url=CIMD_URL, preregistered={fx.issuer: "known-client"})
        connection = adapter.connect()
        assert (connection.registration, connection.client_id) == ("pre-registered", "known-client")
        assert fx.registrations == []


def test_registration_uses_client_id_metadata_document_before_dynamic() -> None:
    config = FixtureConfig(cimd_supported=True, cimd_documents={CIMD_URL: CIMD_DOCUMENT})
    with Fixture(config) as fx:
        adapter, _ = run(fx, client_metadata_url=CIMD_URL)
        connection = adapter.connect()
        assert (connection.registration, connection.client_id) == ("client-id-metadata-document", CIMD_URL)
        assert fx.registrations == []
        assert fx.authorize_params[0]["client_id"] == CIMD_URL


def test_registration_falls_to_dynamic_when_server_has_no_metadata_document_support() -> None:
    with Fixture(FixtureConfig(cimd_supported=False)) as fx:
        adapter, _ = run(fx, client_metadata_url=CIMD_URL)
        connection = adapter.connect()
        assert connection.registration == "dynamic"
        assert fx.registrations[0]["application_type"] == "native"
        assert fx.registrations[0]["redirect_uris"] == [CALLBACK]


def test_dynamic_registration_retries_as_web_when_native_https_is_rejected() -> None:
    with Fixture(FixtureConfig(reject_native_https_registration=True)) as fx:
        connection = run(fx)[0].connect()
        assert connection.registration == "dynamic"
        assert [r["application_type"] for r in fx.registrations] == ["native", "web"]


def test_registration_asks_the_owner_when_nothing_else_is_offered() -> None:
    config = FixtureConfig(registration_endpoint=False, preregistered_client_ids=("owner-typed",))
    with Fixture(config) as fx:
        asked: list[str] = []

        def ask(issuer: str) -> str:
            asked.append(issuer)
            return "owner-typed"

        connection = run(fx, ask_owner=ask)[0].connect()
        assert asked == [fx.issuer]
        assert connection.registration == "owner-supplied"


def test_no_registration_path_and_no_owner_client_id_is_refused() -> None:
    with Fixture(FixtureConfig(registration_endpoint=False)) as fx:
        with pytest.raises(Refused) as refusal:
            run(fx)[0].connect()
        assert refusal.value.code == "no_client_id"
        assert fx.authorize_params == [] and fx.token_requests == []


def test_iss_mismatch_abandons_the_flow_and_never_redeems_the_code() -> None:
    with Fixture(FixtureConfig(iss_mode="wrong")) as fx:
        adapter, keystore = run(fx)
        with pytest.raises(Refused) as refusal:
            adapter.connect()
        assert refusal.value.code == "iss_mismatch"
        assert fx.issuer in str(refusal.value) and "attacker.example" in str(refusal.value)
        assert fx.token_requests == [] and keystore.items == {}


def test_missing_iss_is_refused_when_the_server_advertises_support() -> None:
    with Fixture(FixtureConfig(iss_mode="omit", advertise_iss=True)) as fx:
        with pytest.raises(Refused) as refusal:
            run(fx)[0].connect()
        assert refusal.value.code == "iss_missing"
        assert fx.token_requests == []


def test_missing_iss_proceeds_when_the_server_does_not_advertise_support() -> None:
    with Fixture(FixtureConfig(iss_mode="omit", advertise_iss=False)) as fx:
        assert run(fx)[0].connect().registration == "dynamic"


def test_wrong_iss_is_refused_even_when_support_is_not_advertised() -> None:
    with Fixture(FixtureConfig(iss_mode="wrong", advertise_iss=False)) as fx:
        with pytest.raises(Refused) as refusal:
            run(fx)[0].connect()
        assert refusal.value.code == "iss_mismatch"
        assert fx.token_requests == []


def test_state_mismatch_is_refused() -> None:
    with Fixture() as fx:
        def tampered(url: str) -> str:
            return simulated_browser(url).replace("state=", "state=x")

        with pytest.raises(Refused) as refusal:
            run(fx, browser=tampered)[0].connect()
        assert refusal.value.code == "state_mismatch"
        assert fx.token_requests == []


def test_redirect_to_another_address_is_refused() -> None:
    with Fixture() as fx:
        with pytest.raises(Refused) as refusal:
            run(fx, browser=lambda url: simulated_browser(url).replace("astraldeep.github.io", "evil.example"))[0].connect()
        assert refusal.value.code == "redirect_mismatch"


def test_http_endpoint_is_refused_without_the_loopback_allowance() -> None:
    with Fixture() as fx:
        with pytest.raises(Refused) as refusal:
            run(fx, allow_loopback_http=False)[0].connect()
        assert refusal.value.code == "insecure_transport"
        assert fx.requests == []


def test_tokens_never_appear_in_logs_traces_or_reprs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with Fixture() as fx:
        adapter, keystore = run(fx)
        connection = adapter.connect()
        call(connection, fx.rs_url, "tools/list")
        blob = "\n".join([caplog.text, "\n".join(connection.trace), repr(connection), repr(keystore), str(vars(connection))])
        no_secret_in(blob, fx)
        assert "iss check passed" in caplog.text


def test_refusals_leave_no_secret_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with Fixture(FixtureConfig(iss_mode="wrong")) as fx:
        with pytest.raises(Refused) as refusal:
            run(fx)[0].connect()
        no_secret_in(caplog.text + str(refusal.value), fx)


def test_token_goes_only_to_its_own_server_and_never_in_the_query() -> None:
    with Fixture() as fx:
        connection = run(fx)[0].connect()
        with pytest.raises(Refused) as refusal:
            call(connection, fx.issuer + "/token", "tools/list")
        assert refusal.value.code == "token_passthrough"
        call(connection, fx.rs_url, "tools/list")
        assert all("?" not in entry for entry in fx.requests_to("rs"))


def test_call_without_a_stored_credential_fails_closed() -> None:
    with Fixture() as fx:
        connection = run(fx)[0].connect()
        connection.keystore.items.clear()
        with pytest.raises(Refused) as refusal:
            call(connection, fx.rs_url, "tools/list")
        assert refusal.value.code == "not_signed_in"
