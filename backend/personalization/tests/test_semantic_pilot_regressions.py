"""Exercise native TEI wire shapes, inference admission, and signed-row freshness through the pilot's real boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from personalization import memory_guard
from personalization.semantic_pilot import providers
from personalization.semantic_pilot.bounds import Bounds, BoundsExceeded
from personalization.semantic_pilot.candidates import rerank_candidates, semantic_candidates
from personalization.semantic_pilot.corpus import load_corpus
from personalization.semantic_pilot.providers import (
    ProviderFault,
    ProviderIdentity,
    ProviderUsage,
    VectorGateway,
    resolve_providers,
    scripted_transport,
)
from personalization.semantic_pilot.snapshot import build_tools


def _identity():
    return ProviderIdentity(
        profile="scripted-double", model="embed", revision="e1", tokenizer_revision="et1",
        dimension=4, license="Apache-2.0", notice="synthetic", qualifier="provider_double_only",
        rerank_model="rank", rerank_revision="r1", rerank_capable=True,
    )


def _config(**overrides):
    config = {
        "base_url": "http://embed.internal:8080", "embed_model": "embed", "embed_revision": "e1",
        "tokenizer_revision": "et1", "dimension": 4, "license": "Apache-2.0",
        "rerank_base_url": "http://rank.internal:8081", "rerank_model": "rank",
        "rerank_revision": "r1", "rerank_tokenizer_revision": "rt1",
        "embedding_runtime_digest": "sha256:" + "a" * 64,
        "embedding_assets_digest": "sha256:" + "b" * 64,
        "rerank_runtime_digest": "sha256:" + "c" * 64,
        "rerank_assets_digest": "sha256:" + "d" * 64,
        "api_key": "synthetic-embed-key", "rerank_api_key": "synthetic-rank-key",
    }
    config.update(overrides)
    return config


def test_native_tei_http_routes_and_rank_array(monkeypatch):
    seen = []
    contract = json.loads((Path(__file__).parent / "fixtures/tei-wire-contract.json").read_text())

    def validate(payload, name):
        schema = {"$ref": f"#/components/schemas/{name}", "components": contract["components"]}
        jsonschema.validate(payload, schema)

    def request(method, url, **kwargs):
        seen.append((url, kwargs["json_body"]))
        if url == "http://embed.internal:8080/v1/embeddings":
            assert kwargs["api_key"] == "synthetic-embed-key"
            validate(kwargs["json_body"], "OpenAICompatRequest")
            body = {
                "object": "list", "model": "embed",
                "data": [{"object": "embedding", "index": 0, "embedding": [1., 0., 0., 0.]}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            }
            validate(body, "OpenAICompatResponse")
        elif url == "http://rank.internal:8081/rerank":
            assert kwargs["api_key"] == "synthetic-rank-key"
            validate(kwargs["json_body"], "RerankRequest")
            body = [{"index": 1, "score": 0.9}, {"index": 0, "score": 0.1}]
            validate(body, "RerankResponse")
        else:
            pytest.fail(f"unexpected endpoint {url}")
        return type("Response", (), {"content": json.dumps(body).encode()})()

    monkeypatch.setattr(providers.external_http, "request", request)
    gateway, identity, status = resolve_providers("tei-http", _config())
    usage = ProviderUsage()
    assert gateway.embed(["q"], Bounds(), usage) == [[1., 0., 0., 0.]]
    assert gateway.rerank("q", ["a", "b"], Bounds(), usage) == [(1, 0.9), (0, 0.1)]
    assert seen[1][1] == {"query": "q", "texts": ["a", "b"], "truncate": False,
                          "return_text": False}
    assert identity.to_dict()["embedding_endpoint"] == "http://embed.internal:8080"
    assert identity.to_dict()["rerank_endpoint"] == "http://rank.internal:8081"
    assert identity.to_dict()["rerank_tokenizer_revision"] == "rt1"
    assert status["state"] == "available"
    assert identity.qualifier == "unqualified_unattested"


def test_tei_reranker_requires_its_own_explicit_endpoint_and_tokenizer():
    for field in ("rerank_base_url", "rerank_tokenizer_revision"):
        config = _config()
        config.pop(field)
        _, _, status = resolve_providers("tei-http", config)
        assert status["state"] == "unavailable"
        assert field in status["reason"]


def test_endpoint_identity_changes_invalidate_profile_digest():
    _, first, _ = resolve_providers("tei-http", _config())
    _, second, _ = resolve_providers("tei-http", _config(rerank_base_url="http://other:8081"))
    assert first.digest != second.digest


@pytest.mark.parametrize("route", ["embed", "rerank"])
def test_token_limit_blocks_retry_before_inference(route):
    calls = []

    def transport(path, payload):
        calls.append(path)
        raise ProviderFault("http_unreachable", "synthetic transient failure")

    gateway = VectorGateway(_identity(), transport)
    usage = ProviderUsage()
    with pytest.raises(BoundsExceeded, match="max_total_tokens"):
        if route == "embed":
            gateway.embed(["abcd"], Bounds(max_total_tokens=1, max_retries=1), usage)
        else:
            gateway.rerank("abcd", ["abcd"], Bounds(max_total_tokens=2, max_retries=1), usage)
    assert len(calls) == 1
    assert usage.tokens_estimated == (1 if route == "embed" else 2)


def test_rerank_preadmission_counts_query_for_every_pair():
    calls = []

    def transport(path, payload):
        calls.append(path)
        return [{"index": 0, "score": .9}, {"index": 1, "score": .1}]

    gateway = VectorGateway(_identity(), transport)
    usage = ProviderUsage(tokens_estimated=1)
    with pytest.raises(BoundsExceeded, match="max_total_tokens"):
        gateway.rerank("abcdefgh", ["abcd", "abcd"], Bounds(max_total_tokens=6), usage)
    assert calls == []
    assert usage.tokens_estimated == 1


def test_snapshot_admission_counts_utf8_bytes():
    rows = [{"id": "synthetic", "value": "\U0001f30c" * 3}]
    assert Bounds(max_snapshot_bytes=13).validate_snapshot(rows) == 13
    with pytest.raises(BoundsExceeded, match="max_snapshot_bytes"):
        Bounds(max_snapshot_bytes=12).validate_snapshot(rows)


@pytest.mark.parametrize("phase", ["embed", "before_rerank", "rerank"])
def test_signed_edit_drops_old_snapshot_before_return(monkeypatch, phase):
    monkeypatch.setenv("MEMORY_HMAC_KEY", "synthetic-regression-key")
    monkeypatch.setenv("FF_MEMORY_GUARD", "1")
    monkeypatch.setenv("FF_MEMORY_TEMPORAL", "1")
    monkeypatch.setenv("FF_PROJECT_MEMORY", "1")
    selection, _, _ = load_corpus()
    case = next(case for case in selection if case.case_id == "sel-02-ambiguity-portal")
    tools, store = build_tools(case)
    row_id = case.rows[0].row_id
    native_double = scripted_transport(dimension=4)
    sent_texts = []

    def edit():
        row = store._rows_by_id[row_id]
        row["value"] = "replacement value after authorized update"
        row["updated_at"] += 1
        row["signature"] = memory_guard.sign_fields(
            row_id, case.owner_id, row["category"], row["value"], row["source"])

    def transport(path, payload):
        if path == "/rerank":
            sent_texts.extend(payload["texts"])
        if (phase == "embed" and path == "/v1/embeddings") or (phase == "rerank" and path == "/rerank"):
            edit()
        return native_double(path, payload)

    gateway = VectorGateway(_identity(), transport)
    usage = ProviderUsage()
    run = semantic_candidates(
        tools, store, gateway, user_id=case.owner_id, project_id=case.project_id,
        query=case.query, top_k=3, bounds=Bounds(), usage=usage)
    if phase != "embed":
        if phase == "before_rerank":
            edit()
        run = rerank_candidates(
            tools, store, gateway, run, user_id=case.owner_id, project_id=case.project_id,
            query=case.query, bounds=Bounds(), usage=usage)
    assert row_id not in run.ranked_ids
    assert {"id": row_id, "reason": "row_changed"} in run.revalidation_drops
    if phase == "before_rerank":
        assert case.rows[0].value not in sent_texts


def test_retry_usage_counts_only_dispatched_attempts():
    def transport(path, payload):
        raise ProviderFault("http_unreachable", "synthetic failure")

    usage = ProviderUsage()
    with pytest.raises(BoundsExceeded):
        VectorGateway(_identity(), transport).embed(["a"], Bounds(max_total_tokens=1), usage)
    assert usage.retries == 0


def test_attestation_cannot_qualify_a_changed_rerank_profile():
    from personalization.semantic_pilot.report import profile_qualification

    _, original, status = resolve_providers("tei-http", _config())
    _, changed, _ = resolve_providers("tei-http", _config(rerank_base_url="http://replacement:8081"))
    attestation = {
        "profile": "tei-http", "model_revision": "e1", "corpus_digest": "synthetic-corpus",
        "performed_at": "2026-10-28T00:00:00Z", "operator": "synthetic",
        "provider_identity_digest": original.digest,
    }
    assert profile_qualification(original.to_dict(), status, attestation, "synthetic-corpus")[
        "state"] == "qualified_manual_run"
    result = profile_qualification(changed.to_dict(), status, attestation, "synthetic-corpus")
    assert result["state"] == "executed_unattested"
    assert "identity digest" in result["reason"]


@pytest.mark.parametrize("field", ["embedding_runtime_digest", "embedding_assets_digest",
                                   "rerank_runtime_digest", "rerank_assets_digest"])
def test_tei_configuration_requires_immutable_runtime_and_asset_identities(field):
    config = _config()
    config.pop(field)
    _, _, status = resolve_providers("tei-http", config)
    assert status["state"] == "unavailable"
    assert field in status["reason"]
    config[field] = "floating-latest"
    _, _, status = resolve_providers("tei-http", config)
    assert status["state"] == "unavailable"
    assert field in status["reason"]
