# Semantic memory qualification pilot

This read-only pilot measures the frozen synthetic non-PHI personalization corpus against the actual non-reinforcing lexical baseline, semantic candidates, and optional reranking. It never changes production retrieval, writes embeddings to the product store, or promotes a profile automatically.

Required CI uses deterministic doubles. The native HTTP adapter follows the TEI API at [56964585069318d01ddeeec9454a952a66f1d3a2](https://github.com/huggingface/text-embeddings-inference/blob/56964585069318d01ddeeec9454a952a66f1d3a2/docs/openapi.json), accessed 2026-10-28: OpenAI-compatible `/v1/embeddings` returns indexed embeddings; native `/rerank` returns a rank array. The extracted schemas in `../tests/fixtures/tei-wire-contract.json` retain the source digest and Apache-2.0 notice. A passing adapter fixture establishes wire compatibility only.

## Operator configuration

Use an independently provisioned, approved local TEI embedding service and a separately declared sequence-classification service for reranking. The native rerank request has no model selector; pointing both operations at one model server does not load a second model. There is no endpoint fallback, automatic asset download, or ambient provider credential lookup.

The private JSON passed with `--run-config` requires:

- `base_url`, `embed_model`, `embed_revision`, `tokenizer_revision`, `dimension`, and `license` for embedding.
- `embedding_runtime_digest` and `embedding_assets_digest`, each `sha256:<64 lowercase hex digits>`. The runtime digest identifies the exact executable/package/image. The assets digest identifies the frozen local model/tokenizer asset manifest, including revisions, file digests, licenses, and notices.
- For reranking, all of `rerank_base_url`, `rerank_model`, `rerank_revision`, `rerank_tokenizer_revision`, `rerank_runtime_digest`, and `rerank_assets_digest`.
- Optional `api_key` and `rerank_api_key` are distinct, explicit credentials for their respective endpoints. Optional `allow_private_endpoint` permits the configured private hosts through the existing approved egress boundary; it does not disable TLS verification or other outbound controls. `timeout_seconds` and `max_response_bytes` bound each request.

Missing or malformed runtime/asset digest declarations make the TEI profile unavailable. Supplied identities are operator declarations: this adapter does not independently inspect a remote process or its asset files. A declaration alone never establishes model/runtime qualification. Keep the private configuration and credentials outside Git.

```sh
PYTHONPATH=backend python -m personalization.semantic_pilot \
  --profile tei-http --run-config /private/path/tei-profile.json \
  --report /private/path/unattested-run.json
```

An actual bounded manual synthetic run must retain the exact candidate SHA, runtime and asset manifests, endpoint/model/tokenizer identities, command and bound settings, fixture/report digests, output/failure evidence, and process cleanup evidence. `--attestation` accepts `profile`, `model_revision`, `provider_identity_digest`, `corpus_digest`, `performed_at`, and `operator`; the identity digest is SHA-256 of the report's complete provider identity encoded as UTF-8 JSON with sorted keys and compact separators. Changing either endpoint, runtime, assets, tokenizer, or model invalidates that receipt. CI does not independently authenticate an operator's manual attestation. Keep an unexecuted native profile unqualified; an offline diagnostic or scripted double cannot substitute for the model run.

## Bounds and freshness

Snapshot text limits count UTF-8 bytes plus one separator byte per row. Token accounting remains the pilot's declared four-character estimate, rather than a tokenizer-exact measure; preadmission includes every dispatched attempt, and reranking counts the query for every document pair. Retries that cannot fit the remaining estimate or call budget are refused before dispatch. Provider byte/call usage includes dispatched retries.

Rows are authorized before inference and compared against current signed content, revision, scope, validity, and supersession fields before return. Reranking also revalidates before sending candidate text. A changed row is dropped with `row_changed`; fresh content is never returned under the old embedding score. Provider indices remain bound to the exact admitted set.
