"""Opt-in local-inference research profile: closed catalog, literal-local
selection, declared accounting bound, byte-identical OpenAI default.

No HTTP, SDK or provider traffic. Feature 088 T016 / FR-019 (spec edge case:
"a custom model provider lacks a qualified accounting bound" -- the bound is
declared here, never derived from a preset).
"""

import json
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from audit.pii import private_binding_key
from llm_config import research_profile as profile
from llm_config.client_factory import LocalInferenceFrame, local_inference_frame
from llm_config.local_endpoint import (
    LOCAL_ENDPOINT_ALLOWLIST_ENV,
    classify_endpoint,
    is_local_endpoint,
)
from llm_config.research_profile import (
    LOCAL_PROFILE,
    OPENAI_PROFILE,
    ResearchProfile,
    ResearchProfileUnavailable,
    select_config,
)
from llm_config.tests.test_research_profile_088 import reply, retained

LOCAL_URL = "http://127.0.0.1:11434/v1"
LOCAL_MODEL = "qwen2.5:7b-instruct-q4_K_M"


@pytest.fixture
def binding(store, monkeypatch):
    store._repository.plane_runtime.repositories.history = SimpleNamespace(
        sessions=SimpleNamespace(bound_request_execution_waits=Mock(return_value=None))
    )
    monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "local_profile_test")
    monkeypatch.setenv(
        "AUDIT_HMAC_SECRET", "synthetic-local-binding-" + "fedcba9876543210" * 2
    )
    monkeypatch.delenv("AUDIT_HMAC_SECRET_LOCAL_PROFILE_TEST", raising=False)
    return private_binding_key()


def seed(store, **changes):
    values = dict(provider="ollama", base_url=LOCAL_URL, model=LOCAL_MODEL, api_key="")
    values.update(changes)
    store.set_sync("alice", **values)
    return store.capture_user_sync("alice")


def select(store, binding, capture, **kwargs):
    return select_config(capture, store=store, binding_key=binding, **kwargs)


# ---------------------------------------------------------------------------
# Endpoint classification is literal and fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base_url,expected",
    [
        ("http://localhost:11434/v1", "loopback"),
        ("http://LOCALHOST/v1", "loopback"),
        ("http://127.0.0.1:1234/v1", "loopback"),
        ("http://127.255.255.254/v1", "loopback"),
        ("http://[::1]:8000/v1", "loopback"),
        ("http://[::ffff:127.0.0.1]:8000/v1", "loopback"),
        ("http://10.0.0.5:8000/v1", "private"),
        ("http://172.16.0.1:8000/v1", "private"),
        ("http://172.31.255.255:8000/v1", "private"),
        ("http://192.168.1.5:8000/v1", "private"),
        ("http://[::ffff:192.168.1.5]:8000/v1", "private"),
        ("http://172.32.0.1:8000/v1", "remote"),
        ("http://169.254.1.1/v1", "remote"),
        ("http://100.64.0.1/v1", "remote"),
        ("http://0.0.0.0/v1", "remote"),
        ("http://[fd00::1]/v1", "remote"),
        ("http://192.0.2.1/v1", "remote"),
        ("https://api.openai.com/v1", "remote"),
        ("http://ollama.lan:11434/v1", "remote"),
        ("http://user:pw@127.0.0.1/v1", "remote"),
        ("ftp://127.0.0.1/v1", "remote"),
        ("127.0.0.1:11434/v1", "remote"),
        ("http:///v1", "remote"),
        ("http://127.0.0.1:99999/v1", "remote"),
        ("http://[::1/v1", "remote"),  # urlsplit ValueError
        ("http://:8000/v1", "remote"),  # empty host
        ("", "remote"),
        (None, "remote"),
        (b"http://127.0.0.1/v1", "remote"),
        pytest.param("http://" + "a" * 32768, "remote", id="oversized-host"),
    ],
)
def test_classification_is_literal_and_never_resolves(base_url, expected, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", Mock(side_effect=AssertionError("resolved")))
    verdict = classify_endpoint(base_url)
    assert verdict.endpoint_class == expected
    assert verdict.local is (expected != "remote")
    assert is_local_endpoint(base_url) is verdict.local
    if verdict.local:
        assert verdict.redacted_base_url == f"{verdict.scheme}://<{expected}>"
        assert "127" not in verdict.redacted_base_url and "192" not in verdict.redacted_base_url
    else:
        assert verdict.redacted_base_url == ""


def test_allowlist_matches_exact_origin_only(monkeypatch):
    monkeypatch.delenv(LOCAL_ENDPOINT_ALLOWLIST_ENV, raising=False)
    assert classify_endpoint("http://ollama.lan:11434/v1").endpoint_class == "remote"
    assert classify_endpoint("http://ollama.lan:11434/v1",
                             allowlist=("http://ollama.lan:11434",)).endpoint_class == "allowlisted"
    assert classify_endpoint("http://ollama.lan/v1",
                             allowlist=("http://ollama.lan:80/v1",)).endpoint_class == "allowlisted"
    for wrong in ("https://ollama.lan:11434", "http://ollama.lan:11435", "http://ollama.lan",
                  "ollama.lan:11434", "", "http://"):
        assert classify_endpoint("http://ollama.lan:11434/v1",
                                 allowlist=(wrong,)).endpoint_class == "remote"
    monkeypatch.setenv(LOCAL_ENDPOINT_ALLOWLIST_ENV, "garbage, ,http://ollama.lan:11434")
    assert classify_endpoint("http://ollama.lan:11434/v1").endpoint_class == "allowlisted"
    assert classify_endpoint("http://ollama.lan:11435/v1").endpoint_class == "remote"
    # An allowlisted origin never promotes a hosted vendor row into "local".
    assert classify_endpoint("https://api.openai.com/v1").endpoint_class == "remote"


# ---------------------------------------------------------------------------
# Catalog + OpenAI default are unchanged
# ---------------------------------------------------------------------------


def test_openai_profile_is_the_default_and_matches_the_legacy_constants():
    assert profile.DEFAULT_PROFILE is OPENAI_PROFILE
    assert profile.PROFILES == (OPENAI_PROFILE, LOCAL_PROFILE)
    assert (OPENAI_PROFILE.name, OPENAI_PROFILE.model, OPENAI_PROFILE.base_url) == (
        profile.PROFILE, profile.MODEL, profile.BASE_URL)
    assert OPENAI_PROFILE.reserved_tokens == profile.RESERVED_TOKENS == 128000 + 1024
    assert OPENAI_PROFILE.reserved_milliseconds == profile.RESERVED_MILLISECONDS == 65000
    assert OPENAI_PROFILE.endpoint == profile.ENDPOINT
    assert OPENAI_PROFILE.reservation() == {
        "model_calls": 1, "tokens": profile.RESERVED_TOKENS,
        "elapsed_ms": profile.RESERVED_MILLISECONDS}
    assert OPENAI_PROFILE.bound and not OPENAI_PROFILE.local
    assert LOCAL_PROFILE.local and not LOCAL_PROFILE.bound
    assert LOCAL_PROFILE.reservation() == {
        "model_calls": 1, "tokens": 32768 + 1024, "elapsed_ms": 120000}
    assert set(LOCAL_PROFILE.reservation()) == set(OPENAI_PROFILE.reservation())
    assert LOCAL_PROFILE.name == "local-page-selection/v1" != OPENAI_PROFILE.name
    with pytest.raises(FrozenInstanceError):
        LOCAL_PROFILE.context_tokens = 1
    with pytest.raises(ResearchProfileUnavailable):
        LOCAL_PROFILE.endpoint
    with pytest.raises(ResearchProfileUnavailable):
        OPENAI_PROFILE.bind(model="x", base_url="http://127.0.0.1/v1")
    bound = LOCAL_PROFILE.bind(model=LOCAL_MODEL, base_url=LOCAL_URL)
    with pytest.raises(ResearchProfileUnavailable):
        bound.bind(model=LOCAL_MODEL, base_url=LOCAL_URL)


def test_default_selection_and_parsing_are_byte_identical_to_the_openai_profile(store, binding):
    capture = seed(store, provider="openai", base_url=profile.BASE_URL,
                   model=profile.MODEL, api_key="synthetic-openai-key")
    default = select(store, binding, capture)
    explicit = select(store, binding, capture, profile=OPENAI_PROFILE)
    assert default == explicit and default.profile is OPENAI_PROFILE
    assert not default.local and default.reservation == OPENAI_PROFILE.reservation()
    request = profile.build_request("Select.", retained())
    assert request == profile.build_request("Select.", retained(), profile=OPENAI_PROFILE)
    assert json.loads(request.body)["model"] == profile.MODEL
    body = json.dumps(reply()).encode()
    parsed = profile.parse_response(body, status_code=200, passage_ids=request.passage_ids)
    assert parsed == profile.parse_response(body, status_code=200,
                                            passage_ids=request.passage_ids, profile=OPENAI_PROFILE)
    assert parsed.usage == profile.ResearchUsage(100, 20, 120)
    assert parsed.usage.context_tokens == profile.CONTEXT_TOKENS
    # A local row is NOT admitted by the default profile (no fallback).
    with pytest.raises(ResearchProfileUnavailable):
        select(store, binding, seed(store))


# ---------------------------------------------------------------------------
# Local selection: exact row, literal-local endpoint, declared bound
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["ollama", "lmstudio", "custom"])
@pytest.mark.parametrize("base_url", [LOCAL_URL, "http://localhost:1234/v1",
                                      "http://10.1.2.3:8000/v1", "https://192.168.0.9/v1"])
def test_local_selection_binds_the_exact_row_and_signs_its_profile(store, binding, provider, base_url):
    capture = seed(store, provider=provider, base_url=base_url)
    selection = select(store, binding, capture, profile=LOCAL_PROFILE)
    assert selection.local and selection.owner_id == "alice"
    assert selection.matches(capture._record)
    assert selection.profile == LOCAL_PROFILE.bind(model=LOCAL_MODEL, base_url=base_url)
    assert selection.profile.endpoint == base_url + "/chat/completions"
    assert selection.reservation == LOCAL_PROFILE.reservation()
    assert selection._api_key == ""
    assert len(selection.revision) == 64
    assert selection == select(store, binding, capture, profile=LOCAL_PROFILE)
    # A pre-bound copy of THIS row is accepted; one naming another row is not.
    assert select(store, binding, capture, profile=selection.profile) == selection
    with pytest.raises(ResearchProfileUnavailable):
        select(store, binding, capture,
               profile=LOCAL_PROFILE.bind(model="other", base_url=base_url))
    assert LOCAL_MODEL not in repr(selection)


def test_local_revision_binds_the_profile_name_and_every_raw_field(store, binding):
    capture = seed(store)
    local = select(store, binding, capture, profile=LOCAL_PROFILE)
    changed = seed(store, model=LOCAL_MODEL + "-v2")
    assert select(store, binding, changed, profile=LOCAL_PROFILE).revision != local.revision
    moved = seed(store, base_url="http://localhost:11435/v1")
    assert select(store, binding, moved, profile=LOCAL_PROFILE).revision != local.revision
    keyed = seed(store, api_key="synthetic-local-key")
    assert select(store, binding, keyed, profile=LOCAL_PROFILE).revision != local.revision
    assert not local.matches(replace(capture._record, model="x"))


@pytest.mark.parametrize("key,expected", [
    ("", ""), ("not-needed", ""), ("synthetic-local-key", "synthetic-local-key"),
])
def test_local_keyless_or_real_secret(store, binding, key, expected):
    selection = select(store, binding, seed(store, api_key=key), profile=LOCAL_PROFILE)
    assert selection._api_key == expected
    if expected:
        assert expected not in repr(selection)


@pytest.mark.parametrize("key", [" key", "key ", "line\nbreak", "é", "x" * 8193])
def test_local_unusable_secret_refuses(store, binding, fake_db, key):
    seed(store)
    fake_db.users["alice"]["api_key_enc"] = store._encrypt_key(key)
    with pytest.raises(ResearchProfileUnavailable):
        select(store, binding, store.capture_user_sync("alice"), profile=LOCAL_PROFILE)


@pytest.mark.parametrize("field,value", [
    ("provider", "openai"), ("provider", "OLLAMA"), ("provider", "groq"), ("provider", "anthropic"),
    ("base_url", "https://api.openai.com/v1"), ("base_url", "http://ollama.lan:11434/v1"),
    ("base_url", "http://169.254.1.1/v1"), ("base_url", ""), ("base_url", LOCAL_URL + "/"),
    ("model", ""), ("model", " " + LOCAL_MODEL), ("model", LOCAL_MODEL + "\n"),
    ("model", "x" * 257), ("model", None), ("base_url", None), ("scope", "system"),
])
def test_local_profile_refuses_any_non_local_or_malformed_row(store, binding, fake_db, field, value):
    seed(store)
    if field == "scope" or value is None:
        # The store never yields such a row; bind refuses it defensively.
        capture = store.capture_user_sync("alice")
        capture = type(capture)(replace(capture._record, **{field: value}))
    else:
        fake_db.users["alice"][field] = value
        capture = store.capture_user_sync("alice")
    with pytest.raises(ResearchProfileUnavailable, match="^research_profile_unavailable$"):
        select(store, binding, capture, profile=LOCAL_PROFILE)


def test_forged_or_foreign_profile_objects_are_refused(store, binding):
    capture = seed(store)
    forged = ResearchProfile("local-page-selection/v1", ("ollama", "lmstudio", "custom"),
                             None, None, 10**9, 1024, 120000, True)
    for candidate in (forged, replace(LOCAL_PROFILE, reserved_milliseconds=1),
                      replace(OPENAI_PROFILE, context_tokens=1), "local-page-selection/v1",
                      None, SimpleNamespace(**{name: getattr(LOCAL_PROFILE, name)
                                              for name in LOCAL_PROFILE.__slots__})):
        with pytest.raises(ResearchProfileUnavailable):
            select(store, binding, capture, profile=candidate)
        with pytest.raises(ResearchProfileUnavailable):
            profile.build_request("Select.", retained(), profile=candidate)
        with pytest.raises(ResearchProfileUnavailable):
            profile.parse_response(json.dumps(reply()).encode(), status_code=200,
                                   passage_ids=("p001",), profile=candidate)
    for allowlist in (["http://x:1"], ("http://x:1", 1), "http://x:1"):
        with pytest.raises(ResearchProfileUnavailable):
            select(store, binding, capture, profile=LOCAL_PROFILE, local_allowlist=allowlist)


def test_unbound_local_profile_cannot_build_or_parse(store, binding):
    with pytest.raises(ResearchProfileUnavailable):
        profile.build_request("Select.", retained(), profile=LOCAL_PROFILE)
    with pytest.raises(ResearchProfileUnavailable):
        profile.parse_response(json.dumps(reply(model=LOCAL_MODEL)).encode(), status_code=200,
                               passage_ids=("p001",), profile=LOCAL_PROFILE)


def test_local_request_keeps_the_fixed_framing_with_the_bound_model(store, binding):
    selection = select(store, binding, seed(store), profile=LOCAL_PROFILE)
    local = json.loads(profile.build_request("Select.", retained(), profile=selection.profile).body)
    openai = json.loads(profile.build_request("Select.", retained()).body)
    assert local["model"] == LOCAL_MODEL and openai["model"] == profile.MODEL
    assert local["max_completion_tokens"] == profile.LOCAL_OUTPUT_TOKENS == 1024
    assert {k: v for k, v in local.items() if k != "model"} == {
        k: v for k, v in openai.items() if k != "model"}
    assert LOCAL_MODEL not in openai["messages"][1]["content"]


def test_local_parse_uses_the_local_bound_and_never_a_synthetic_zero(store, binding):
    selection = select(store, binding, seed(store), profile=LOCAL_PROFILE)
    ids = ("p001",)

    def parse(document):
        return profile.parse_response(json.dumps(document).encode(), status_code=200,
                                      passage_ids=ids, profile=selection.profile)

    evidence = parse(reply(model=LOCAL_MODEL))
    assert evidence.disposition == "evidence" and evidence.passage_ids == ids
    assert evidence.usage == profile.ResearchUsage(100, 20, 120, 32768, 1024)
    assert evidence.usage != profile.ResearchUsage(100, 20, 120)  # OpenAI-bound counters differ
    assert not evidence.usage.exceeds_profile
    # Prompt within OpenAI's 128k but beyond the local 32k bound is an overrun.
    over = parse(reply(model=LOCAL_MODEL, usage={"prompt_tokens": 32769, "completion_tokens": 1,
                                                "total_tokens": 32770}))
    assert over.disposition == "profile_exceeded" and over.usage.total_tokens == 32770
    assert profile.parse_response(json.dumps(reply(usage={"prompt_tokens": 32769,
        "completion_tokens": 1, "total_tokens": 32770})).encode(), status_code=200,
        passage_ids=ids).disposition == "evidence"
    output = parse(reply(model=LOCAL_MODEL, usage={"prompt_tokens": 1, "completion_tokens": 1025,
                                                  "total_tokens": 1026}))
    assert output.disposition == "profile_exceeded"
    # Wrong model on the local profile is an invalid answer with factual usage.
    wrong = parse(reply(model=profile.MODEL))
    assert wrong == profile.ResearchResponse(profile.ResearchUsage(100, 20, 120, 32768, 1024),
                                             None, "answer_invalid")
    # Missing/null usage is unknown, not zero.
    unknown = parse(reply(model=LOCAL_MODEL, usage=None))
    assert unknown == profile.ResearchResponse(None, None, "usage_unknown")
    zero = parse(reply(model=LOCAL_MODEL, usage={"prompt_tokens": 0, "completion_tokens": 0,
                                                "total_tokens": 0}))
    assert zero.usage == profile.ResearchUsage(0, 0, 0, 32768, 1024)
    assert zero.disposition == "evidence"  # a provider's literal zero is factual, not synthetic
    negative = parse(reply(model=LOCAL_MODEL, usage={"prompt_tokens": -1, "completion_tokens": 1,
                                                    "total_tokens": 0}))
    assert negative.usage is None and negative.disposition == "usage_unknown"
    # Envelope denial stays independent of the profile.
    assert profile.parse_response(b"{}", status_code=200, passage_ids=ids,
                                  profile=selection.profile).disposition == "response_invalid"
    assert profile.parse_response(json.dumps(reply(model=LOCAL_MODEL)).encode(), status_code=503,
                                  passage_ids=ids, profile=selection.profile).usage is None


# ---------------------------------------------------------------------------
# client_factory local frame: additive, remote providers byte-identical
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("base_url,api_key,expected", [
    ("http://localhost:11434/v1", "", LocalInferenceFrame(True, "loopback", True, "m", "http://<loopback>")),
    ("http://192.168.1.4:8000/v1", "not-needed",
     LocalInferenceFrame(True, "private", True, "m", "http://<private>")),
    ("https://10.0.0.2/v1", "synthetic-key",
     LocalInferenceFrame(True, "private", False, "m", "https://<private>")),
    ("https://api.openai.com/v1", "synthetic-key",
     LocalInferenceFrame(False, "remote", False, "m", "https://api.openai.com/v1")),
    ("https://custom.example.test/gateway/v1", "",
     LocalInferenceFrame(False, "remote", True, "m", "https://custom.example.test/gateway/v1")),
])
def test_local_inference_frame_tags_without_credentials(base_url, api_key, expected, monkeypatch):
    monkeypatch.delenv(LOCAL_ENDPOINT_ALLOWLIST_ENV, raising=False)
    config = SimpleNamespace(base_url=base_url, api_key=api_key, model="m")
    frame = local_inference_frame(config)
    assert frame == expected
    assert not hasattr(frame, "api_key") and "synthetic" not in repr(frame)
    if frame.local:
        assert base_url not in frame.audit_base_url
    else:
        assert frame.audit_base_url == base_url
    with pytest.raises(FrozenInstanceError):
        frame.local = not frame.local


def test_local_inference_frame_absent_or_malformed_config_is_remote():
    assert local_inference_frame(None) == LocalInferenceFrame(False, "remote", False, "", "")
    frame = local_inference_frame(SimpleNamespace(base_url=None, api_key=None, model=None))
    assert frame == LocalInferenceFrame(False, "remote", True, "", "")
    assert local_inference_frame(SimpleNamespace(base_url="http://ollama.lan:11434/v1",
        api_key="", model="m"), allowlist=("http://ollama.lan:11434",)).endpoint_class == "allowlisted"
