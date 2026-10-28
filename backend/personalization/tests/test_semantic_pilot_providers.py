"""Tests for the provider boundary: strict payload validation (finite values, dimensions,
row association, duplicate/missing/out-of-range indices, malformed/truncated output),
transport fault mapping, retry and cancellation bounds, deterministic offline diagnostic
behavior, and truthful unavailable status for unqualified profiles.
"""

from __future__ import annotations

import json

import pytest

from personalization.semantic_pilot import providers as providers_module
from personalization.semantic_pilot.bounds import Bounds
from personalization.semantic_pilot.providers import (
    OFFLINE_DIMENSION,
    ProviderFault,
    ProviderIdentity,
    ProviderUsage,
    TeiEndpoint,
    UnavailableProvider,
    VectorGateway,
    build_embedding_payload,
    build_rerank_payload,
    parse_embedding_payload,
    parse_rerank_payload,
    resolve_providers,
    scripted_transport,
    tei_http_transport,
)

BOUNDS = Bounds()
USAGE = ProviderUsage()

IDENTITY = ProviderIdentity(
    profile="tei-http", model="m", revision="r1", tokenizer_revision="t1",
    dimension=4, license="Apache-2.0", notice="test", qualifier="unqualified_unattested",
    rerank_model="m", rerank_revision="r1", rerank_capable=True,
)


def _vec(value=1.0, dimension=4):
    return [value] * dimension


def test_build_payloads_shape():
    assert build_embedding_payload(["a"], "m") == {"model": "m", "input": ["a"]}
    assert build_rerank_payload("q", ["a"], "m") == {
        "model": "m", "query": "q", "texts": ["a"]}


def test_parse_embedding_payload_golden_row_association():
    payload = {
        "data": [
            {"index": 1, "embedding": _vec(2.0)},
            {"index": 0, "embedding": _vec(1.0)},
        ],
        "model": "m",
    }
    vectors = parse_embedding_payload(payload, count=2, dimension=4)
    assert vectors[0] == _vec(1.0)
    assert vectors[1] == _vec(2.0)


def test_parse_embedding_payload_rejects_duplicate_index():
    payload = {"data": [{"index": 0, "embedding": _vec()}, {"index": 0, "embedding": _vec()}]}
    with pytest.raises(ProviderFault, match="duplicate_index"):
        parse_embedding_payload(payload, count=2, dimension=4)


def test_parse_embedding_payload_rejects_missing_index():
    payload = {"data": [{"index": 0, "embedding": _vec()}]}
    with pytest.raises(ProviderFault, match="missing_index"):
        parse_embedding_payload(payload, count=2, dimension=4)


def test_parse_embedding_payload_rejects_out_of_range_index():
    payload = {"data": [{"index": 5, "embedding": _vec()}]}
    with pytest.raises(ProviderFault, match="out_of_range_index"):
        parse_embedding_payload(payload, count=1, dimension=4)


def test_parse_embedding_payload_rejects_non_finite_and_dimension():
    with pytest.raises(ProviderFault, match="non_finite_value"):
        parse_embedding_payload(
            {"data": [{"index": 0, "embedding": [float("nan")] * 4}]}, count=1, dimension=4)
    with pytest.raises(ProviderFault, match="dimension_mismatch"):
        parse_embedding_payload(
            {"data": [{"index": 0, "embedding": _vec(dimension=3)}]}, count=1, dimension=4)


def test_parse_embedding_payload_rejects_malformed_shapes():
    with pytest.raises(ProviderFault, match="malformed_output"):
        parse_embedding_payload([1, 2], count=1, dimension=4)
    with pytest.raises(ProviderFault, match="malformed_output"):
        parse_embedding_payload({"nope": 1}, count=1, dimension=4)
    with pytest.raises(ProviderFault, match="malformed_output"):
        parse_embedding_payload({"data": ["x"]}, count=1, dimension=4)
    with pytest.raises(ProviderFault, match="malformed_output"):
        parse_embedding_payload({"data": [{"index": "0", "embedding": _vec()}]},
                                count=1, dimension=4)


def test_parse_rerank_payload_sorts_by_score():
    payload = {"results": [{"index": 0, "score": 0.1}, {"index": 1, "score": 0.9}]}
    assert parse_rerank_payload(payload, count=2) == [(1, 0.9), (0, 0.1)]


def test_parse_rerank_payload_rejects_bad_indices_and_scores():
    with pytest.raises(ProviderFault, match="duplicate_index"):
        parse_rerank_payload(
            {"results": [{"index": 0, "score": 1.0}, {"index": 0, "score": 0.5}]}, count=2)
    with pytest.raises(ProviderFault, match="missing_index"):
        parse_rerank_payload({"results": [{"index": 0, "score": 1.0}]}, count=2)
    with pytest.raises(ProviderFault, match="out_of_range_index"):
        parse_rerank_payload({"results": [{"index": 9, "score": 1.0}]}, count=1)
    with pytest.raises(ProviderFault, match="non_finite_value"):
        parse_rerank_payload({"results": [{"index": 0, "score": float("inf")}]}, count=1)
    with pytest.raises(ProviderFault, match="malformed_output"):
        parse_rerank_payload({"unexpected": True}, count=1)


def _gateway(transport, **identity_overrides):
    fields = dict(
        profile="tei-http", model="m", revision="r1", tokenizer_revision="t1",
        dimension=4, license="Apache-2.0", notice="test", qualifier="unqualified_unattested",
        rerank_model="m", rerank_revision="r1", rerank_capable=True,
    )
    fields.update(identity_overrides)
    return VectorGateway(ProviderIdentity(**fields), transport)


def test_gateway_embed_batches_and_accounts_usage():
    calls = []

    def transport(route, payload):
        texts = list(payload["input"])
        calls.append((route, texts))
        return {"data": [
            {"index": i, "embedding": _vec(float(len(texts)))}
            for i, _ in enumerate(texts)
        ]}

    gateway = _gateway(transport)
    usage = ProviderUsage()
    vectors = gateway.embed(["a", "b", "c"], Bounds(max_batch_rows=2), usage)
    assert len(vectors) == 3
    assert [route for route, _ in calls] == [
        providers_module.TEI_EMBEDDINGS_ROUTE] * 2
    assert calls[0][1] == ["a", "b"]
    assert calls[1][1] == ["c"]
    assert usage.embedding_calls == 2
    assert usage.vectors == 3
    assert usage.tokens_estimated > 0
    assert usage.bytes_sent > 0 and usage.bytes_received > 0


def test_gateway_retries_transient_fault_then_succeeds():
    attempts = {"n": 0}

    def transport(route, payload):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ProviderFault("http_unreachable", "first attempt fails")
        return {"data": [{"index": 0, "embedding": _vec()}]}

    gateway = _gateway(transport)
    usage = ProviderUsage()
    vectors = gateway.embed(["a"], Bounds(max_retries=1), usage)
    assert len(vectors) == 1
    assert usage.retries == 1
    assert usage.faults == []


def test_gateway_does_not_retry_auth_fault():
    def transport(route, payload):
        raise ProviderFault("http_auth_failed", "denied")

    gateway = _gateway(transport)
    usage = ProviderUsage()
    with pytest.raises(ProviderFault, match="http_auth_failed"):
        gateway.embed(["a"], Bounds(max_retries=3), usage)
    assert usage.retries == 0
    assert usage.faults[0]["kind"] == "http_auth_failed"


def test_gateway_cancels_between_batches():
    def transport(route, payload):
        return {"data": [{"index": 0, "embedding": _vec()}]}

    gateway = _gateway(transport)
    usage = ProviderUsage()
    with pytest.raises(providers_module.RunCancelled):
        gateway.embed(["a", "b"], Bounds(max_batch_rows=1), usage,
                      cancel_check=lambda: True)


def test_gateway_enforces_call_and_token_bounds():
    def transport(route, payload):
        return {"data": [{"index": 0, "embedding": _vec()}]}

    gateway = _gateway(transport)
    with pytest.raises(Bounds.__class__.__mro__[0].__name__ if False else Exception):
        gateway.embed(["a"], Bounds(max_provider_calls=0), ProviderUsage())
    usage = ProviderUsage()
    usage.tokens_estimated = Bounds().max_total_tokens
    with pytest.raises(Exception):
        gateway.embed(["a"], BOUNDS, usage)


def test_gateway_rerank_requires_capable_identity():
    gateway = _gateway(scripted_transport(), rerank_capable=False)
    with pytest.raises(ProviderFault, match="rerank_unavailable"):
        gateway.rerank("q", ["a"], BOUNDS, ProviderUsage())


def test_scripted_faults_drive_validation_paths():
    expected = {
        "non_finite_value": "non_finite_value",
        "dimension_mismatch": "dimension_mismatch",
        "duplicate_index": "duplicate_index",
        "missing_index": "missing_index",
        "out_of_range_index": "out_of_range_index",
        "malformed_output": "malformed_output",
        "truncated_payload": "malformed_output",
    }
    for fault, kind in expected.items():
        gateway = _gateway(scripted_transport(dimension=4, faults={
            providers_module.TEI_EMBEDDINGS_ROUTE: fault}), rerank_capable=False)
        usage = ProviderUsage()
        with pytest.raises(ProviderFault) as excinfo:
            gateway.embed(["a", "b"], BOUNDS, usage)
        assert excinfo.value.kind == kind
        assert usage.faults[-1]["kind"] == kind


def test_scripted_rerank_faults_drive_validation_paths():
    expected = {
        "non_finite_value": "non_finite_value",
        "duplicate_index": "duplicate_index",
        "missing_index": "missing_index",
        "out_of_range_index": "out_of_range_index",
        "malformed_output": "malformed_output",
        "truncated_payload": "malformed_output",
    }
    for fault, kind in expected.items():
        gateway = _gateway(scripted_transport(dimension=4, faults={
            providers_module.TEI_RERANK_ROUTE: fault}))
        with pytest.raises(ProviderFault) as excinfo:
            gateway.rerank("q", ["a", "b"], BOUNDS, ProviderUsage())
        assert excinfo.value.kind == kind


def test_scripted_transport_http_faults():
    for fault in ("http_unreachable", "http_auth_failed", "http_bad_request"):
        gateway = _gateway(scripted_transport(dimension=4, faults={
            providers_module.TEI_EMBEDDINGS_ROUTE: fault}), rerank_capable=False)
        with pytest.raises(ProviderFault, match=fault):
            gateway.embed(["a"], BOUNDS, ProviderUsage())


def test_offline_diagnostic_is_deterministic_and_normalized():
    gateway, identity, status = resolve_providers("offline-diagnostic")
    assert status["state"] == "available"
    assert identity.qualifier == providers_module.QUALIFIER_DIAGNOSTIC
    usage = ProviderUsage()
    first = gateway.embed(["drinks oat-milk lattes"], BOUNDS, usage)
    second = gateway.embed(["drinks oat-milk lattes"], BOUNDS, usage)
    assert first == second
    vector = first[0]
    assert len(vector) == OFFLINE_DIMENSION
    assert abs(sum(v * v for v in vector) - 1.0) < 1e-9
    order = gateway.rerank("conference deadlines", ["a", "b"], BOUNDS, ProviderUsage())
    assert sorted(index for index, _ in order) == [0, 1]
    assert order[0][1] >= order[1][1]


def test_resolve_scripted_double_available():
    gateway, identity, status = resolve_providers("scripted-double")
    assert status["state"] == "available"
    assert identity.qualifier == providers_module.QUALIFIER_DOUBLE


def test_resolve_tei_http_without_config_is_unavailable():
    _, identity, status = resolve_providers("tei-http", None)
    assert status["state"] == "unavailable"
    assert "no run configuration" in status["reason"]
    assert identity.qualifier == providers_module.QUALIFIER_UNQUALIFIED


def test_resolve_tei_http_partial_config_lists_missing_fields():
    _, _, status = resolve_providers("tei-http", {"base_url": "http://tei:80"})
    assert status["state"] == "unavailable"
    for field in ("embed_model", "embed_revision", "tokenizer_revision", "dimension",
                  "license"):
        assert field in status["reason"]


@pytest.mark.parametrize("config,fragment", [
    ({"base_url": "ftp://x", "embed_model": "m", "embed_revision": "r",
      "tokenizer_revision": "t", "dimension": 4, "license": "L"}, "http(s) URL"),
    ({"base_url": "http://x", "embed_model": "m", "embed_revision": "r",
      "tokenizer_revision": "t", "dimension": 0, "license": "L"}, "positive integer"),
    ({"base_url": "http://x", "embed_model": "m", "embed_revision": "r",
      "tokenizer_revision": "t", "dimension": 4, "license": "L", "rerank_model": "m"},
     "together"),
    ("not-a-dict", "JSON object"),
])
def test_resolve_tei_http_rejects_bad_fields(config, fragment):
    _, _, status = resolve_providers("tei-http", config)
    assert status["state"] == "unavailable"
    assert fragment in status["reason"]


def test_resolve_tei_http_full_config_is_available():
    config = {
        "base_url": "http://tei.internal:8080",
        "embed_model": "model-a", "embed_revision": "sha-a",
        "tokenizer_revision": "tok-a", "dimension": 768,
        "license": "Apache-2.0", "rerank_model": "model-b", "rerank_revision": "sha-b",
        "api_key": "explicit-operator-key",
        "allow_private_endpoint": True,
    }
    gateway, identity, status = resolve_providers("tei-http", config)
    assert status["state"] == "available"
    assert status["endpoint"]["api_key_present"] is True
    assert status["endpoint"]["allow_private_endpoint"] is True
    assert identity.rerank_capable is True
    assert identity.dimension == 768


def test_resolve_unknown_profile_raises():
    with pytest.raises(UnavailableProvider):
        resolve_providers("quantum-embedder")


def test_identity_digest_is_stable_and_usage_merges():
    identity = ProviderIdentity(
        profile="p", model="m", revision="r", tokenizer_revision="t", dimension=2,
        license="l", notice="n", qualifier="q")
    assert identity.digest == identity.digest
    usage_a = ProviderUsage(embedding_calls=1, faults=[{"kind": "x"}])
    usage_b = ProviderUsage(embedding_calls=2)
    usage_b.merge(usage_a)
    assert usage_b.embedding_calls == 3
    assert len(usage_b.faults) == 1
    assert usage_b.to_dict()["vectors"] == 0


class _FakeResponse:
    def __init__(self, content):
        self.content = content


def test_tei_http_transport_success_and_headers(monkeypatch):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["api_key"] = kwargs.get("api_key")
        captured["allowed"] = kwargs.get("allowed_private_hosts")
        return _FakeResponse(json.dumps({
            "data": [{"index": 0, "embedding": [1.0] * OFFLINE_DIMENSION}]
        }).encode())

    monkeypatch.setattr(providers_module.external_http, "request", fake_request)
    endpoint = TeiEndpoint(base_url="http://tei.internal:8080/", api_key="k",
                           allow_private_endpoint=True)
    gateway = _gateway(tei_http_transport(endpoint), dimension=OFFLINE_DIMENSION)
    usage = ProviderUsage()
    vectors = gateway.embed(["a"], BOUNDS, usage)
    assert vectors[0] == [1.0] * OFFLINE_DIMENSION
    assert usage.embedding_calls == 1
    assert captured["method"] == "POST"
    assert captured["url"] == "http://tei.internal:8080/v1/embeddings"
    assert captured["api_key"] == "k"
    assert captured["allowed"] == ["tei.internal:8080"]


def test_tei_http_transport_private_not_allowed_by_default(monkeypatch):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["allowed"] = kwargs.get("allowed_private_hosts")
        return _FakeResponse(b"{}")

    monkeypatch.setattr(providers_module.external_http, "request", fake_request)
    transport = tei_http_transport(TeiEndpoint(base_url="http://tei.internal:8080"))
    transport(providers_module.TEI_EMBEDDINGS_ROUTE, {"input": []})
    assert captured["allowed"] is None


@pytest.mark.parametrize("error,kind", [
    (providers_module.external_http.EgressBlockedError("no"), "egress_blocked"),
    (providers_module.external_http.AuthFailedError("no"), "http_auth_failed"),
    (providers_module.external_http.RateLimitedError("no"), "http_rate_limited"),
    (providers_module.external_http.BadRequestError("no"), "http_bad_request"),
    (providers_module.external_http.ServiceUnreachableError("no"), "http_unreachable"),
    (providers_module.external_http.ExternalHttpError("no"), "http_error"),
])
def test_tei_http_transport_maps_egress_errors(monkeypatch, error, kind):
    def fake_request(method, url, **kwargs):
        raise error

    monkeypatch.setattr(providers_module.external_http, "request", fake_request)
    transport = tei_http_transport(TeiEndpoint(base_url="http://tei.internal:8080"))
    with pytest.raises(ProviderFault, match=kind):
        transport(providers_module.TEI_EMBEDDINGS_ROUTE, {"input": []})


def test_gateway_transient_retry_then_permanent_fails_closed():
    attempts = {"n": 0}

    def transport(route, payload):
        attempts["n"] += 1
        raise ProviderFault("http_unreachable", "down")

    gateway = _gateway(transport)
    usage = ProviderUsage()
    with pytest.raises(ProviderFault):
        gateway.embed(["a", "b"], Bounds(max_batch_rows=1, max_retries=1), usage)
    assert attempts["n"] == 2
    assert usage.embedding_calls == 2
    assert usage.retries == 1
    assert len(usage.faults) == 1


def test_parse_json_response_variants():
    assert providers_module._parse_json_response({"a": 1}) == {"a": 1}
    assert providers_module._parse_json_response(b'{"a": 1}') == {"a": 1}
    assert providers_module._parse_json_response('{"a": 1}') == {"a": 1}
    with pytest.raises(ProviderFault, match="not valid JSON"):
        providers_module._parse_json_response(b"{truncated")
    with pytest.raises(ProviderFault, match="not an object"):
        providers_module._parse_json_response(b"[1,2]")
    with pytest.raises(ProviderFault, match="unsupported type"):
        providers_module._parse_json_response(42)


def test_gateway_embed_empty_texts_makes_no_call():
    calls = []

    def transport(route, payload):
        calls.append(route)
        return {"data": []}

    gateway = _gateway(transport)
    usage = ProviderUsage()
    assert gateway.embed([], BOUNDS, usage) == []
    assert calls == []
    assert usage.tokens_estimated == 0


def test_gateway_rerank_empty_texts_makes_no_call():
    calls = []

    def transport(route, payload):
        calls.append(route)
        return {"results": []}

    gateway = _gateway(transport)
    usage = ProviderUsage()
    assert gateway.rerank("q", [], BOUNDS, usage) == []
    assert calls == []


def test_gateway_rerank_cancels_before_call():
    def transport(route, payload):
        return {"results": []}

    gateway = _gateway(transport)
    with pytest.raises(providers_module.RunCancelled):
        gateway.rerank("q", ["a"], BOUNDS, ProviderUsage(), cancel_check=lambda: True)


def test_gateway_enforces_call_bound_mid_loop():
    def transport(route, payload):
        return {"data": [{"index": 0, "embedding": _vec()}]}

    gateway = _gateway(transport)
    usage = ProviderUsage()
    usage.embedding_calls = 1
    with pytest.raises(Exception, match="max_provider_calls"):
        gateway.embed(["a"], Bounds(max_provider_calls=1), usage)


def test_parse_rerank_entry_shapes():
    with pytest.raises(ProviderFault, match="not an object"):
        parse_rerank_payload({"results": ["x"]}, count=1)
    with pytest.raises(ProviderFault, match="not an int"):
        parse_rerank_payload({"results": [{"index": "0", "score": 1.0}]}, count=1)
    with pytest.raises(ProviderFault, match="not an int"):
        parse_rerank_payload({"results": [{"index": True, "score": 1.0}]}, count=1)


def test_resolve_tei_http_empty_config_and_empty_model():
    _, _, status = resolve_providers("tei-http", {})
    assert "no run configuration" in status["reason"]
    complete = {
        "base_url": "http://tei:8080", "embed_model": " ", "embed_revision": "r",
        "tokenizer_revision": "t", "dimension": 4, "license": "L",
    }
    _, _, status = resolve_providers("tei-http", complete)
    assert "embed_model must be a non-empty string" in status["reason"]
