"""Exercises the loopback A2A agents in fixtures/connections scheme by scheme and checks the cards and task states they serve against the contract's a2a-card-v1.json vectors.
The vector copy beside the fixtures is compared with the contract file when the specs folder is present, and the contract reference reader reads the served cards when it can be imported.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import json
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from tests.fixtures.connections import AgentConfig, FixtureAgent
from tests.fixtures.connections import a2a_agent as agent_module
from tests.fixtures.connections.a2a_agent import (
    AUTH_REQUIRED,
    COMPLETED,
    DEVICE_GRANT,
    OAUTH_SCHEMES,
    SCHEMES,
    SIGNER_KIDS,
    b64url,
    b64url_decode,
    canonical_card,
    card_digest,
    jwk_thumbprint,
    signer_jwk,
)
from tests.test_connection_fixtures import self_signed

HERE = Path(__file__).resolve().parent
VECTORS = HERE / "fixtures" / "connections" / "a2a-card-v1.json"
SPECS = HERE.parents[1] / "specs"
CONTRACT = SPECS / "106-sign-in-free-clients" / "contracts" / "vectors" / "a2a-card-v1.json"
REFERENCE = SPECS / "106-sign-in-free-clients" / "contracts" / "reference"
MESH_REFERENCE = SPECS / "105-decentralized-personal-mesh" / "contracts" / "reference"
REDIRECT = "https://astraldeep.github.io/oauth/callback"
VERIFIER = "v" * 43
SIGNER_BY_KID = {kid: name for name, kid in SIGNER_KIDS.items()}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def http(url, method="GET", body=None, headers=None, context=None):
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=context))
    try:
        response = opener.open(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    raw = response.read()
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    return response.status, parsed, response.headers


def rpc(agent, method, params=None, headers=None, target=None, context=None):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    sent = {"content-type": "application/json", **(headers if headers is not None else agent.credential_headers())}
    status, body, response_headers = http(target or agent.rpc_target, "POST", json.dumps(payload).encode(), sent, context)
    return status, body, response_headers


def message(text="hello"):
    return {"message": {"role": "ROLE_USER", "parts": [{"text": text}]}}


def pkce():
    return b64url(hashlib.sha256(VERIFIER.encode()).digest())


def sign_in_with_code(agent, verifier=VERIFIER, redirect=REDIRECT):
    query = urlencode({"response_type": "code", "client_id": "astral", "redirect_uri": redirect, "state": "s1", "code_challenge": pkce(), "code_challenge_method": "S256"})
    status, _, headers = http(f"{agent.origin}/oauth/authorize?{query}")
    assert status == 302
    params = parse_qs(urlsplit(headers["location"]).query)
    assert params["state"] == ["s1"] and params["iss"] == [agent.origin]
    form = urlencode({"grant_type": "authorization_code", "code": params["code"][0], "code_verifier": verifier, "redirect_uri": redirect}).encode()
    return http(f"{agent.origin}/oauth/token", "POST", form, {"content-type": "application/x-www-form-urlencoded"})


def sign_in_with_device(agent):
    status, grant, _ = http(f"{agent.origin}/oauth/device", "POST", b"", {})
    assert status == 200
    form = urlencode({"grant_type": DEVICE_GRANT, "device_code": grant["device_code"]}).encode()
    pending = http(f"{agent.origin}/oauth/token", "POST", form, {"content-type": "application/x-www-form-urlencoded"})
    assert pending[0] == 400 and pending[1]["error"] == "authorization_pending"
    agent.approve_device()
    return http(f"{agent.origin}/oauth/token", "POST", form, {"content-type": "application/x-www-form-urlencoded"})


def sign_in(agent):
    if agent.scheme == "oauth2_device_code":
        status, token, _ = sign_in_with_device(agent)
    else:
        status, token, _ = sign_in_with_code(agent)
    assert status == 200 and token["token_type"] == "Bearer"
    return token


@pytest.fixture
def make_agent():
    started = []

    def make(scheme, signed=False, **config):
        agent = FixtureAgent(scheme, signed, AgentConfig(**config))
        started.append(agent)
        return agent

    yield make
    for agent in started:
        agent.stop()


@pytest.fixture(params=SCHEMES)
def scheme_agent(request, make_agent):
    return make_agent(request.param)


def test_each_scheme_refuses_a_missing_credential_and_a_wrong_one(scheme_agent):
    status, _, headers = rpc(scheme_agent, "SendMessage", message(), headers={})
    assert status == 401 and headers["www-authenticate"]
    wrong = {"authorization": "Bearer wrong"} if scheme_agent.scheme not in ("api_key", "http_basic") else {"x-api-key": "wrong", "authorization": "Basic d3Jvbmc6d3Jvbmc="}
    assert rpc(scheme_agent, "SendMessage", message(), headers=wrong)[0] == 401
    assert scheme_agent.refusals == ["missing", "wrong"] and scheme_agent.sends == 0
    assert [call.authorized for call in scheme_agent.calls] == [False, False]


def test_each_scheme_accepts_the_right_credential(scheme_agent):
    if scheme_agent.scheme in OAUTH_SCHEMES:
        sign_in(scheme_agent)
    status, body, _ = rpc(scheme_agent, "SendMessage", message("ping"))
    task = body["result"]["task"]
    assert status == 200 and task["status"]["state"] == COMPLETED
    assert task["artifacts"][0]["parts"][0]["text"] == "ping"
    assert scheme_agent.sends == 1 and scheme_agent.calls[-1].authorized


@pytest.mark.parametrize("location", ["header", "query", "cookie"])
def test_api_key_travels_in_the_declared_location(make_agent, location):
    agent = make_agent("api_key", api_key_location=location)
    scheme = agent.card()["securitySchemes"]["key"]["apiKeySecurityScheme"]
    assert scheme["location"] == location
    assert rpc(agent, "SendMessage", message(), headers={}, target=agent.rpc_url)[0] == 401
    assert rpc(agent, "SendMessage", message())[0] == 200


def test_basic_and_bearer_scheme_words_are_case_insensitive(make_agent):
    bearer = make_agent("http_bearer")
    assert rpc(bearer, "SendMessage", message(), headers={"authorization": f"bearer {bearer.issued.bearer}"})[0] == 200
    basic = make_agent("http_basic")
    pair = base64.b64encode(f"{basic.issued.basic_user}:{basic.issued.basic_password}".encode()).decode()
    assert rpc(basic, "SendMessage", message(), headers={"authorization": f"BASIC {pair}"})[0] == 200
    assert rpc(basic, "SendMessage", message(), headers={"authorization": f"Bearer {pair}"})[0] == 401


def test_the_card_is_public_and_counted(scheme_agent):
    status, card, _ = http(scheme_agent.card_url)
    assert status == 200 and card == scheme_agent.card() and scheme_agent.card_fetches == 1
    assert "signatures" not in card and card["supportedInterfaces"][0]["url"] == scheme_agent.rpc_url
    assert card["securityRequirements"][0]["schemes"]


def test_card_security_scheme_matches_the_agent(make_agent):
    kinds = {}
    for scheme in SCHEMES:
        agent = make_agent(scheme)
        kinds[scheme] = next(iter(next(iter(agent.card()["securitySchemes"].values()))))
    assert kinds == {
        "api_key": "apiKeySecurityScheme",
        "http_bearer": "httpAuthSecurityScheme",
        "http_basic": "httpAuthSecurityScheme",
        "oauth2_authorization_code": "oauth2SecurityScheme",
        "oauth2_device_code": "oauth2SecurityScheme",
        "openid_connect": "openIdConnectSecurityScheme",
    }


def test_authorization_code_requires_pkce_and_a_single_use_code(make_agent):
    agent = make_agent("oauth2_authorization_code")
    assert http(f"{agent.origin}/oauth/authorize?response_type=code&redirect_uri={REDIRECT}")[0] == 400
    assert http(f"{agent.origin}/oauth/authorize?response_type=token&redirect_uri={REDIRECT}&code_challenge_method=S256&code_challenge=x")[0] == 400
    status, error, _ = sign_in_with_code(agent, verifier="x" * 43)
    assert status == 400 and error["error"] == "invalid_grant"
    status, token, _ = sign_in_with_code(agent)
    assert status == 200 and token["access_token"] in agent.issued.access_tokens
    assert len(agent.authorize_requests) == 4 and agent.token_requests[-1]["grant_type"] == "authorization_code"


def test_authorization_code_binds_the_redirect_uri(make_agent):
    agent = make_agent("oauth2_authorization_code")
    query = urlencode({"response_type": "code", "redirect_uri": REDIRECT + "?a=1", "code_challenge": pkce(), "code_challenge_method": "S256"})
    _, _, headers = http(f"{agent.origin}/oauth/authorize?{query}")
    assert "?a=1&code=" in headers["location"]
    code = parse_qs(urlsplit(headers["location"]).query)["code"][0]
    form = urlencode({"grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER, "redirect_uri": "https://other.example/cb"}).encode()
    assert http(f"{agent.origin}/oauth/token", "POST", form, {})[0] == 400
    assert http(f"{agent.origin}/oauth/token", "POST", form, {})[1]["error"] == "invalid_grant"


def test_refresh_token_issues_a_new_access_token_and_junk_is_refused(make_agent):
    agent = make_agent("oauth2_authorization_code")
    token = sign_in(agent)
    form = urlencode({"grant_type": "refresh_token", "refresh_token": token["refresh_token"]}).encode()
    status, refreshed, _ = http(f"{agent.origin}/oauth/token", "POST", form, {})
    assert status == 200 and refreshed["access_token"] != token["access_token"]
    assert rpc(agent, "SendMessage", message())[0] == 200
    bad = urlencode({"grant_type": "refresh_token", "refresh_token": "nope"}).encode()
    assert http(f"{agent.origin}/oauth/token", "POST", bad, {})[0] == 400
    assert http(f"{agent.origin}/oauth/token", "POST", b"grant_type=password", {})[1]["error"] == "unsupported_grant_type"


def test_device_grant_is_pending_until_approved_and_checks_the_device_code(make_agent):
    agent = make_agent("oauth2_device_code")
    sign_in(agent)
    wrong = urlencode({"grant_type": DEVICE_GRANT, "device_code": "other"}).encode()
    assert http(f"{agent.origin}/oauth/token", "POST", wrong, {})[1]["error"] == "invalid_grant"
    assert http(f"{agent.origin}/oauth/authorize")[0] == 404


def test_openid_configuration_names_the_fixture_endpoints(make_agent):
    agent = make_agent("openid_connect")
    status, document, _ = http(agent.origin + "/.well-known/openid-configuration")
    assert status == 200 and document["issuer"] == agent.origin and document["token_endpoint"].endswith("/oauth/token")
    assert agent.card()["securitySchemes"]["oidc"]["openIdConnectSecurityScheme"]["openIdConnectUrl"].endswith("openid-configuration")
    assert http(make_agent("http_bearer").origin + "/.well-known/openid-configuration")[0] == 404


def test_unknown_paths_methods_and_tasks_are_refused_cleanly(make_agent):
    agent = make_agent("http_bearer")
    assert http(agent.origin + "/nowhere")[0] == 404
    assert rpc(agent, "NoSuchMethod")[1]["error"]["code"] == -32601
    assert rpc(agent, "GetTask", {"id": "missing"})[1]["error"]["code"] == -32001
    assert rpc(agent, "tasks/get", {"id": "missing"})[0] == 200
    assert rpc(agent, "SendMessage", {"message": "not an object"})[1]["result"]["task"]["status"]["state"] == COMPLETED
    assert rpc(agent, "SendMessage", None, headers={"authorization": "Bearer"})[0] == 401


def test_auth_required_after_n_sends_and_resolution_through_get_task(make_agent):
    agent = make_agent("http_bearer", auth_required_after=1)
    first = rpc(agent, "SendMessage", message())[1]["result"]["task"]
    second = rpc(agent, "message/send", message())[1]["result"]["task"]
    assert (first["status"]["state"], second["status"]["state"]) == (COMPLETED, AUTH_REQUIRED)
    assert "artifacts" not in second and agent.sends == 2
    assert rpc(agent, "GetTask", {"id": second["id"]})[1]["result"]["task"]["status"]["state"] == AUTH_REQUIRED
    agent.complete_task(second["id"])
    assert rpc(agent, "GetTask", {"id": second["id"]})[1]["result"]["task"]["status"]["state"] == COMPLETED
    assert [call.state for call in agent.calls if call.method == "GetTask"] == [AUTH_REQUIRED, COMPLETED]


def test_auth_required_on_the_first_send_with_a_valid_credential(make_agent):
    agent = make_agent("http_bearer", auth_required_after=0)
    task = rpc(agent, "SendMessage", message())[1]["result"]["task"]
    assert task["status"]["state"] == AUTH_REQUIRED and agent.calls[-1].authorized


def test_auth_required_without_a_credential_when_the_agent_answers_in_the_task(make_agent):
    agent = make_agent("http_bearer", unauthenticated_task=True)
    status, body, _ = rpc(agent, "SendMessage", message(), headers={})
    assert status == 200 and body["result"]["task"]["status"]["state"] == AUTH_REQUIRED
    assert agent.calls[-1].authorized is False and agent.sends == 0
    assert rpc(agent, "GetTask", {"id": "x"}, headers={})[0] == 401


@pytest.mark.parametrize("state", ["TASK_STATE_WORKING", "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_FAILED", "TASK_STATE_UNSPECIFIED", "auth_required"])
def test_forced_state_is_reported_verbatim(make_agent, state):
    agent = make_agent("http_bearer", forced_state=state)
    assert rpc(agent, "SendMessage", message())[1]["result"]["task"]["status"]["state"] == state


def test_extended_card_needs_the_credential_and_adds_a_skill(make_agent):
    agent = make_agent("http_bearer", signed=False)
    assert rpc(agent, "GetExtendedAgentCard", headers={})[0] == 401
    status, body, _ = rpc(agent, "GetExtendedAgentCard")
    extended = body["result"]
    assert status == 200 and [s["id"] for s in extended["skills"]] == ["echo", "admin"]
    assert card_digest(extended) == agent.extended_digest() != card_digest(agent.card())
    assert agent.extended_fetches == 1 and agent.card()["capabilities"]["extendedAgentCard"] is True
    assert rpc(agent, "agent/getAuthenticatedExtendedCard")[0] == 200


def test_extended_card_can_be_switched_off(make_agent):
    agent = make_agent("http_bearer", extended_card=False)
    assert "extendedAgentCard" not in agent.card()["capabilities"]
    assert rpc(agent, "GetExtendedAgentCard")[1]["error"]["code"] == -32007


def verify_signature(card, jwk):
    entry = card["signatures"][0]
    header = json.loads(b64url_decode(entry["protected"]))
    message_bytes = f"{entry['protected']}.{b64url(canonical_card(card))}".encode()
    raw = b64url_decode(entry["signature"])
    try:
        if header["alg"] == "ES256":
            key = ec.EllipticCurvePublicNumbers(int.from_bytes(b64url_decode(jwk["x"]), "big"), int.from_bytes(b64url_decode(jwk["y"]), "big"), ec.SECP256R1()).public_key()
            key.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")), message_bytes, ec.ECDSA(hashes.SHA256()))
        else:
            ed25519.Ed25519PublicKey.from_public_bytes(b64url_decode(jwk["x"])).verify(raw, message_bytes)
    except (InvalidSignature, ValueError, KeyError):
        return False
    return True


@pytest.mark.parametrize("signer", ["one", "two", "ed"])
def test_signed_cards_verify_against_the_phase_zero_public_keys(make_agent, signer):
    agent = make_agent("http_bearer", signed=True, signer=signer)
    card = http(agent.card_url)[1]
    header = json.loads(b64url_decode(card["signatures"][0]["protected"]))
    assert header["kid"] == SIGNER_KIDS[signer] and header["typ"] == "JOSE"
    assert header["alg"] == ("EdDSA" if signer == "ed" else "ES256")
    assert verify_signature(card, agent.public_key)
    other = "two" if signer == "one" else "one"
    assert not verify_signature(card, signer_jwk(other))
    extended = rpc(agent, "GetExtendedAgentCard")[1]["result"]
    assert verify_signature(extended, agent.public_key) and card_digest(extended) == agent.extended_digest()


def test_unsigned_cards_carry_no_signature_and_tampered_cards_fail(make_agent):
    assert "signatures" not in make_agent("http_bearer").card()
    agent = make_agent("http_bearer", signed=True, tamper_card=True)
    card = agent.card()
    assert card["name"].startswith("Tampered") and not verify_signature(card, agent.public_key)


def test_redefining_a_signed_card_signs_it_again_with_a_new_digest(make_agent):
    agent = make_agent("http_bearer", signed=True)
    before = agent.card()
    agent.redefine(card_version="2.0.0")
    after = agent.card()
    assert verify_signature(after, agent.public_key) and card_digest(after) != card_digest(before)
    agent.redefine(interface_url="https://elsewhere.example.org/a2a")
    assert agent.card()["supportedInterfaces"][0]["url"] == "https://elsewhere.example.org/a2a"
    with pytest.raises(AttributeError):
        agent.redefine(nonsense=1)


def test_unknown_scheme_and_bad_config_are_refused():
    with pytest.raises(ValueError):
        FixtureAgent("mutual_tls")
    for raw in ([], {"nonsense": 1}, {"signer": "three"}, {"api_key_location": "body"}):
        with pytest.raises(ValueError):
            AgentConfig.from_json(raw)
    for raw in ([], {"scheme": "api_key", "extra": 1}, {"scheme": "nope"}):
        with pytest.raises(ValueError):
            FixtureAgent.from_json(raw)
    with FixtureAgent.from_json({"scheme": "http_basic", "signed": True, "config": {"signer": "ed"}}) as agent:
        assert agent.signed and agent.config.signer == "ed"


def test_the_agent_serves_https_when_given_a_certificate(tmp_path):
    cert, key = self_signed(tmp_path)
    context = ssl.create_default_context(cafile=str(cert))
    with FixtureAgent("http_bearer", True, tls=(str(cert), str(key))) as agent:
        assert agent.origin.startswith("https://127.0.0.1:")
        status, card, _ = http(agent.card_url, context=context)
        assert status == 200 and card["supportedInterfaces"][0]["url"].startswith("https://127.0.0.1:")
        assert rpc(agent, "SendMessage", message(), context=context)[0] == 200


def load_vectors():
    return json.loads(VECTORS.read_text())


def of_kind(kind):
    return [case for case in load_vectors()["cases"] if case["kind"] == kind]


def test_vector_copy_matches_the_contract_when_present():
    if CONTRACT.exists():
        assert VECTORS.read_bytes() == CONTRACT.read_bytes()
    document = load_vectors()
    assert (document["format"], document["name"], len(document["cases"])) == ("astral.vectors/v1", "a2a-card-v1", 213)


def test_vector_canonical_form_and_digest_agree_with_what_the_fixture_signs():
    cases = of_kind("a2a_card_canonical")
    refused = 0
    for case in cases:
        try:
            canonical = canonical_card(case["input"]["card"]).decode()
        except ValueError:
            assert case["expect"] == {"error": "a2a.float_in_card"}
            refused += 1
            continue
        assert canonical == case["expect"]["ok"]["canonical"], case["name"]
        assert card_digest(case["input"]["card"]) == case["expect"]["ok"]["digest"], case["name"]
    assert (len(cases), refused) == (12, 1)


def test_vector_thumbprints_agree_with_the_fixture_and_the_signer_keys():
    cases = of_kind("a2a_thumbprint")
    refused = 0
    for case in cases:
        if "error" in case["expect"]:
            with pytest.raises(ValueError):
                jwk_thumbprint(case["input"]["jwk"])
            refused += 1
        else:
            assert jwk_thumbprint(case["input"]["jwk"]) == case["expect"]["ok"]["thumbprint"], case["name"]
    assert (len(cases), refused) == (6, 1)
    pins = {case["input"]["pin"] for case in of_kind("a2a_card_verify") if case["expect"].get("ok", {}).get("status") == "verified_pinned"}
    assert jwk_thumbprint(signer_jwk("one")) in pins and jwk_thumbprint(signer_jwk("ed")) in pins


def test_vector_signer_keys_are_the_ones_the_fixture_derives():
    seen = {}
    for case in of_kind("a2a_card_verify") + of_kind("a2a_card_check"):
        for jwk in case["input"].get("keys", []):
            if jwk.get("kid") in SIGNER_BY_KID:
                seen[jwk["kid"]] = jwk
    assert seen
    for kid, jwk in seen.items():
        assert jwk == signer_jwk(SIGNER_BY_KID[kid])


def signature_kid(card):
    try:
        return json.loads(b64url_decode(card["signatures"][0]["protected"]))["kid"]
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def test_vector_signed_cards_verify_under_the_fixture_keys_and_canonical_form():
    verified = invalid = other = 0
    for case in of_kind("a2a_card_verify"):
        card = case["input"]["card"]
        kid = signature_kid(card)
        if kid not in SIGNER_BY_KID or kid not in {jwk.get("kid") for jwk in case["input"]["keys"]}:
            continue
        status = case["expect"]["ok"]["status"]
        agrees = verify_signature(card, signer_jwk(SIGNER_BY_KID[kid]))
        if status.startswith("verified") and agrees:
            verified += 1
        elif status == "invalid" and not agrees:
            invalid += 1
        else:
            other += 1
    assert (verified, invalid, other) == (7, 3, 2)


def test_vector_task_states_are_all_emittable_and_auth_required_is_a_real_transition(make_agent):
    cases = of_kind("a2a_task_state")
    states = {case["input"]["state"] for case in cases}
    assert len(cases) == 14 and AUTH_REQUIRED in states
    for state in sorted(states - {AUTH_REQUIRED}):
        agent = make_agent("http_bearer", forced_state=state)
        assert rpc(agent, "SendMessage", message())[1]["result"]["task"]["status"]["state"] == state
    with_credential = make_agent("http_bearer", auth_required_after=0)
    without = make_agent("http_bearer", unauthenticated_task=True)
    assert rpc(with_credential, "SendMessage", message())[1]["result"]["task"]["status"]["state"] == AUTH_REQUIRED
    assert rpc(without, "SendMessage", message(), headers={})[1]["result"]["task"]["status"]["state"] == AUTH_REQUIRED
    assert [call.authorized for call in with_credential.calls + without.calls] == [True, False]


def test_every_vector_kind_is_accounted_for():
    exercised = {"a2a_card_canonical", "a2a_thumbprint", "a2a_card_verify", "a2a_task_state"}
    client_only = {"a2a_card_read", "a2a_card_select", "a2a_card_propose", "a2a_card_check", "a2a_card_extended"}
    assert set(Counter(case["kind"] for case in load_vectors()["cases"])) == exercised | client_only


@pytest.fixture(scope="module")
def reference():
    if not (REFERENCE / "a2a_card.py").exists() or not MESH_REFERENCE.exists():
        pytest.skip("the contract reference is not in this checkout")
    for path in (REFERENCE, MESH_REFERENCE):
        sys.path.insert(0, str(path))
    stubbed = importlib.util.find_spec("biscuit_auth") is None
    if stubbed:
        sys.modules["biscuit_auth"] = MagicMock()  # the reader never touches the ticket code that imports it
    try:
        yield importlib.import_module("a2a_card")
    finally:
        for path in (REFERENCE, MESH_REFERENCE):
            sys.path.remove(str(path))
        if stubbed:
            del sys.modules["biscuit_auth"]


def test_reference_reader_accepts_every_served_card_over_tls(tmp_path, reference):
    cert, key = self_signed(tmp_path)
    for scheme in SCHEMES:
        with FixtureAgent(scheme, False, tls=(str(cert), str(key))) as agent:
            card = http(agent.card_url, context=ssl.create_default_context(cafile=str(cert)))[1]
            reading = reference.read_card(card)
            assert reading.usable(True) == (0,) and reading.signature_count == 0
            assert reading.interface_urls == (agent.rpc_url,)
            assert reference.propose(card, agent.card_url, [])["signed"] is False
            if scheme in OAUTH_SCHEMES:
                assert reading.auth_hosts() == ("127.0.0.1",)


def test_reference_reader_pins_signed_cards_and_detects_fixture_changes(tmp_path, reference):
    cert, key = self_signed(tmp_path)
    with FixtureAgent("http_bearer", True, AgentConfig(host_name="agent.localhost"), tls=(str(cert), str(key))) as agent:
        card = agent.card()
        proposal = reference.propose(card, agent.card_url, [agent.public_key])
        assert proposal["signed"] and proposal["signer"] == agent.thumbprint and proposal["hash"] == card_digest(card)
        pin = {"hash": proposal["hash"], "origin": proposal["origin"], "signer": proposal["signer"]}
        egress = ["agent.localhost:" + agent.origin.rsplit(":", 1)[1]]
        case = {"pin": pin, "card": card, "fetched_from": agent.card_url, "keys": [agent.public_key], "egress": egress}
        assert reference.check(case) == {"hash": proposal["hash"], "signed": True}
        extended = agent.card(extended=True)
        assert reference.check_extended({"card": extended, "extended_hash": agent.extended_digest()})["hash"] == agent.extended_digest()
        agent.redefine(card_version="2.0.0")
        changed = agent.card()
        with pytest.raises(reference.CardRefusal) as refusal:
            reference.check({**case, "card": changed})
        assert refusal.value.code == "a2a.card_changed"
        agent.redefine(tamper_card=True)
        with pytest.raises(reference.CardRefusal) as tampered:
            reference.check({**case, "card": agent.card()})
        assert tampered.value.code == "a2a.signature_invalid"
        agent.redefine(tamper_card=False, signer="two")
        with pytest.raises(reference.CardRefusal) as swapped:
            reference.check({**case, "card": agent.card(), "keys": [signer_jwk("one"), signer_jwk("two")]})
        assert swapped.value.code == "a2a.signer_changed"


def test_reference_reader_flags_an_offsite_interface(tmp_path, reference):
    cert, key = self_signed(tmp_path)
    with FixtureAgent("http_bearer", False, AgentConfig(interface_url="https://elsewhere.example.org/a2a"), tls=(str(cert), str(key))) as agent:
        card = http(agent.card_url, context=ssl.create_default_context(cafile=str(cert)))[1]
        pin = {"hash": card_digest(card), "origin": agent.origin, "signer": None}
        with pytest.raises(reference.CardRefusal) as refusal:
            reference.check({"pin": pin, "card": card, "fetched_from": agent.card_url, "keys": [], "egress": []})
        assert refusal.value.code == "a2a.interface_offsite"


def test_reference_reader_agrees_the_auth_required_state_needs_sign_in(reference):
    assert reference.task_state(AUTH_REQUIRED, True)["reason"] == "refused"
    assert reference.task_state(AUTH_REQUIRED, False)["reason"] == "not_signed_in"


def start_runner(*args):
    return subprocess.Popen(
        [sys.executable, "-m", "tests.fixtures.connections.serve", *args],
        cwd=HERE.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def stop_runner(process):
    process.terminate()
    code = process.wait(timeout=30)
    process.stdout.close()
    process.stderr.close()
    return code


def test_standalone_runner_serves_requested_agents():
    specs = [json.dumps({"scheme": "http_bearer", "signed": True}), json.dumps({"scheme": "api_key", "config": {"api_key_location": "query"}})]
    process = start_runner(*[arg for spec in specs for arg in ("--a2a", spec)])
    try:
        report = json.loads(process.stdout.readline())
        bearer, key = report["a2a"]
        assert (bearer["scheme"], bearer["signed"], key["scheme"]) == ("http_bearer", True, "api_key")
        card = http(bearer["card"])[1]
        assert card["signatures"] and verify_signature(card, signer_jwk("one"))
        assert rpc_to(bearer["rpc"], {})[0] == 401
        assert rpc_to(bearer["rpc"], bearer["credential_headers"])[0] == 200
        assert rpc_to(key["rpc"], {})[0] == 401
        assert rpc_to(key["rpc_target"], {})[0] == 200
    finally:
        assert stop_runner(process) == 0


def rpc_to(url, headers):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": message()}).encode()
    return http(url, "POST", payload, {"content-type": "application/json", **headers})


def test_standalone_runner_without_agents_reports_no_a2a():
    process = start_runner()
    try:
        assert "a2a" not in json.loads(process.stdout.readline())
    finally:
        assert stop_runner(process) == 0


@pytest.mark.parametrize("spec", ["{", "[]", '{"scheme": "nope"}', '{"scheme": "api_key", "config": {"x": 1}}'])
def test_standalone_runner_rejects_a_bad_agent_spec(spec):
    process = start_runner("--a2a", spec)
    assert process.wait(timeout=30) == 2
    assert "--a2a" in process.stderr.read()
    process.stdout.close()
    process.stderr.close()


def test_standalone_runner_serves_agents_over_tls(tmp_path):
    cert, key = self_signed(tmp_path)
    process = start_runner("--tls-cert", str(cert), "--tls-key", str(key), "--a2a", json.dumps({"scheme": "oauth2_device_code", "signed": True}))
    try:
        report = json.loads(process.stdout.readline())
        agent = report["a2a"][0]
        assert agent["card"].startswith("https://127.0.0.1:") and agent["credential_headers"] == {}
        context = ssl.create_default_context(cafile=str(cert))
        card = http(agent["card"], context=context)[1]
        assert card["supportedInterfaces"][0]["url"] == agent["rpc"] and verify_signature(card, signer_jwk("one"))
    finally:
        assert stop_runner(process) == 0


def test_module_exports_the_scheme_names_the_card_vectors_use():
    assert set(SCHEMES) == {"api_key", "http_bearer", "http_basic", "oauth2_authorization_code", "oauth2_device_code", "openid_connect"}
    assert agent_module.SEND_METHODS[0] == "SendMessage"
