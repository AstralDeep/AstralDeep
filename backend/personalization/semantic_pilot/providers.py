"""Bounded provider boundary for embeddings and reranking: one gateway over swappable
transports (approved-egress TEI HTTP, deterministic offline diagnostic, scripted CI
double) sharing strict response validation for finite vectors/scores, dimensions, and
duplicate/missing/out-of-range row indices, with truthful unavailable status and no
ambient keys or endpoint fallback.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from shared import external_http

from .bounds import Bounds, estimate_tokens

TEI_EMBEDDINGS_ROUTE = "/v1/embeddings"
TEI_RERANK_ROUTE = "/rerank"
OFFLINE_DIMENSION = 256

QUALIFIER_DIAGNOSTIC = "diagnostic_only"
QUALIFIER_DOUBLE = "provider_double_only"
QUALIFIER_UNQUALIFIED = "unqualified_unattested"

EmbedTransport = Callable[[str, Dict[str, Any]], Any]
CancelCheck = Optional[Callable[[], bool]]

_TRANSIENT_HTTP_FAULTS = ("http_unreachable", "http_rate_limited")


class ProviderFault(RuntimeError):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail}


class RunCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderIdentity:
    profile: str
    model: str
    revision: str
    tokenizer_revision: str
    dimension: int
    license: str
    notice: str
    qualifier: str
    rerank_model: Optional[str] = None
    rerank_revision: Optional[str] = None
    rerank_capable: bool = False
    embedding_endpoint: Optional[str] = None
    rerank_endpoint: Optional[str] = None
    rerank_tokenizer_revision: Optional[str] = None
    embedding_runtime_digest: Optional[str] = None
    embedding_assets_digest: Optional[str] = None
    rerank_runtime_digest: Optional[str] = None
    rerank_assets_digest: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile": self.profile,
            "model": self.model,
            "revision": self.revision,
            "tokenizer_revision": self.tokenizer_revision,
            "dimension": self.dimension,
            "license": self.license,
            "notice": self.notice,
            "qualifier": self.qualifier,
            "rerank_model": self.rerank_model,
            "rerank_revision": self.rerank_revision,
            "rerank_capable": self.rerank_capable,
            "embedding_endpoint": self.embedding_endpoint,
            "rerank_endpoint": self.rerank_endpoint,
            "rerank_tokenizer_revision": self.rerank_tokenizer_revision,
            "embedding_runtime_digest": self.embedding_runtime_digest,
            "embedding_assets_digest": self.embedding_assets_digest,
            "rerank_runtime_digest": self.rerank_runtime_digest,
            "rerank_assets_digest": self.rerank_assets_digest,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


@dataclass
class ProviderUsage:
    embedding_calls: int = 0
    rerank_calls: int = 0
    retries: int = 0
    vectors: int = 0
    tokens_estimated: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    faults: List[Dict[str, Any]] = field(default_factory=list)

    def merge(self, other: "ProviderUsage") -> None:
        self.embedding_calls += other.embedding_calls
        self.rerank_calls += other.rerank_calls
        self.retries += other.retries
        self.vectors += other.vectors
        self.tokens_estimated += other.tokens_estimated
        self.bytes_sent += other.bytes_sent
        self.bytes_received += other.bytes_received
        self.faults.extend(other.faults)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "embedding_calls": self.embedding_calls,
            "rerank_calls": self.rerank_calls,
            "retries": self.retries,
            "vectors": self.vectors,
            "tokens_estimated": self.tokens_estimated,
            "bytes_sent": self.bytes_sent,
            "bytes_received": self.bytes_received,
            "faults": list(self.faults),
        }


@dataclass(frozen=True)
class TeiEndpoint:
    base_url: str
    api_key: str = ""
    timeout_seconds: int = 20
    max_response_bytes: int = 4_000_000
    allow_private_endpoint: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_url": self.base_url,
            "api_key_present": bool(self.api_key),
            "timeout_seconds": self.timeout_seconds,
            "max_response_bytes": self.max_response_bytes,
            "allow_private_endpoint": self.allow_private_endpoint,
        }


def build_embedding_payload(texts: Sequence[str], model: str) -> Dict[str, Any]:
    return {"model": model, "input": list(texts)}


def build_rerank_payload(query: str, texts: Sequence[str]) -> Dict[str, Any]:
    return {"query": query, "texts": list(texts), "truncate": False, "return_text": False}


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_embedding_payload(payload: Any, *, count: int,
                            dimension: int) -> List[List[float]]:
    if not isinstance(payload, dict):
        raise ProviderFault("malformed_output", "embedding payload is not an object")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ProviderFault("malformed_output", "embedding payload has no data list")
    vectors: List[Optional[List[float]]] = [None] * count
    seen: set = set()
    for entry in data:
        if not isinstance(entry, dict):
            raise ProviderFault("malformed_output", "embedding row is not an object")
        index = entry.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            raise ProviderFault("malformed_output", f"embedding index {index!r} is not an int")
        if index < 0 or index >= count:
            raise ProviderFault("out_of_range_index", f"embedding index {index} out of range")
        if index in seen:
            raise ProviderFault("duplicate_index", f"embedding index {index} appears twice")
        seen.add(index)
        vector = entry.get("embedding")
        if not isinstance(vector, list) or len(vector) != dimension:
            raise ProviderFault(
                "dimension_mismatch",
                f"embedding for index {index} has length "
                f"{len(vector) if isinstance(vector, list) else 'n/a'}, expected {dimension}")
        if not all(_finite_number(v) for v in vector):
            raise ProviderFault("non_finite_value", f"embedding for index {index} is not finite")
        vectors[index] = [float(v) for v in vector]
    missing = [i for i, v in enumerate(vectors) if v is None]
    if missing:
        raise ProviderFault("missing_index", f"embedding rows missing for indices {missing}")
    return [v for v in vectors if v is not None]


def parse_rerank_payload(payload: Any, *, count: int) -> List[Tuple[int, float]]:
    if not isinstance(payload, list):
        raise ProviderFault("malformed_output", "rerank payload is not a rank array")
    pairs: Dict[int, float] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            raise ProviderFault("malformed_output", "rerank result is not an object")
        index = entry.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            raise ProviderFault("malformed_output", f"rerank index {index!r} is not an int")
        if index < 0 or index >= count:
            raise ProviderFault("out_of_range_index", f"rerank index {index} out of range")
        if index in pairs:
            raise ProviderFault("duplicate_index", f"rerank index {index} appears twice")
        score = entry.get("score")
        if not _finite_number(score):
            raise ProviderFault("non_finite_value", f"rerank score for {index} is not finite")
        pairs[index] = float(score)
    missing = [i for i in range(count) if i not in pairs]
    if missing:
        raise ProviderFault("missing_index", f"rerank results missing for indices {missing}")
    return sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0]))


def _parse_json_response(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            parsed = json.loads(bytes(raw).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ProviderFault("malformed_output", "response is not valid JSON") from None
        if not isinstance(parsed, (dict, list)):
            raise ProviderFault("malformed_output", "response JSON is not an object or array")
        return parsed
    if isinstance(raw, str):
        return _parse_json_response(raw.encode("utf-8"))
    raise ProviderFault("malformed_output", "response has unsupported type")


class VectorGateway:
    def __init__(self, identity: ProviderIdentity, transport: EmbedTransport) -> None:
        self.identity = identity
        self._transport = transport

    def embed(self, texts: Sequence[str], bounds: Bounds, usage: ProviderUsage,
              cancel_check: CancelCheck = None) -> List[List[float]]:
        if not texts:
            return []
        texts = list(texts)
        total_tokens = sum(estimate_tokens(t) for t in texts)
        bounds.validate_token_total(usage.tokens_estimated + total_tokens)
        vectors: List[Optional[List[float]]] = [None] * len(texts)
        for start in range(0, len(texts), bounds.max_batch_rows):
            if cancel_check is not None and cancel_check():
                raise RunCancelled("cancelled before embedding batch")
            batch = texts[start:start + bounds.max_batch_rows]
            bounds.validate_batch(batch)
            payload = build_embedding_payload(batch, self.identity.model)
            raw = self._call_transport(TEI_EMBEDDINGS_ROUTE, payload, bounds, usage,
                                       cancel_check, sum(estimate_tokens(t) for t in batch))
            usage.bytes_received += len(json.dumps(raw).encode("utf-8"))
            try:
                parsed = parse_embedding_payload(raw, count=len(batch),
                                                 dimension=self.identity.dimension)
            except ProviderFault as fault:
                usage.faults.append(fault.to_dict())
                raise
            for offset, vector in enumerate(parsed):
                vectors[start + offset] = vector
            usage.vectors += len(parsed)
        return [v for v in vectors if v is not None]

    def rerank(self, query: str, texts: Sequence[str], bounds: Bounds,
               usage: ProviderUsage, cancel_check: CancelCheck = None) -> List[Tuple[int, float]]:
        if not self.identity.rerank_capable:
            raise ProviderFault("rerank_unavailable", "profile has no rerank route")
        if not texts:
            return []
        texts = list(texts)
        bounds.validate_candidates(len(texts))
        bounds.validate_query(query)
        total_tokens = sum(estimate_tokens(query) + estimate_tokens(t) for t in texts)
        bounds.validate_token_total(usage.tokens_estimated + total_tokens)
        if cancel_check is not None and cancel_check():
            raise RunCancelled("cancelled before rerank call")
        payload = build_rerank_payload(query, texts)
        raw = self._call_transport(TEI_RERANK_ROUTE, payload, bounds, usage, cancel_check,
                                   total_tokens)
        usage.bytes_received += len(json.dumps(raw).encode("utf-8"))
        try:
            return parse_rerank_payload(raw, count=len(texts))
        except ProviderFault as fault:
            usage.faults.append(fault.to_dict())
            raise

    def _call_transport(self, route: str, payload: Dict[str, Any], bounds: Bounds,
                        usage: ProviderUsage, cancel_check: CancelCheck,
                        tokens: int) -> Any:
        attempts = 0
        max_attempts = 1 + bounds.max_retries
        while attempts < max_attempts:
            if cancel_check is not None and cancel_check():
                raise RunCancelled("cancelled before provider call")
            bounds.validate_call_count(
                usage.embedding_calls + usage.rerank_calls + 1)
            bounds.validate_token_total(usage.tokens_estimated + tokens)
            attempts += 1
            if attempts > 1:
                usage.retries += 1
            usage.tokens_estimated += tokens
            usage.bytes_sent += len(json.dumps(payload).encode("utf-8"))
            try:
                if route == TEI_EMBEDDINGS_ROUTE:
                    usage.embedding_calls += 1
                else:
                    usage.rerank_calls += 1
                raw = self._transport(route, payload)
                if cancel_check is not None and cancel_check():
                    raise RunCancelled("cancelled during provider call")
                parsed = _parse_json_response(raw)
                if cancel_check is not None and cancel_check():
                    raise RunCancelled("cancelled during provider response parsing")
                return parsed
            except ProviderFault as fault:
                if cancel_check is not None and cancel_check():
                    raise RunCancelled("cancelled during failed provider call") from None
                if fault.kind in _TRANSIENT_HTTP_FAULTS and attempts < max_attempts:
                    continue
                usage.faults.append(fault.to_dict())
                raise
        raise RuntimeError("provider call loop exited without a verdict")


def tei_http_transport(endpoint: TeiEndpoint,
                       rerank_endpoint: Optional[TeiEndpoint] = None) -> EmbedTransport:
    def transport(route: str, payload: Dict[str, Any]) -> Any:
        selected = endpoint if route == TEI_EMBEDDINGS_ROUTE else rerank_endpoint
        if selected is None:
            raise ProviderFault("rerank_unavailable", "no explicit rerank endpoint configured")
        base_url = external_http.normalize_url(selected.base_url)
        allowed_private = ([urlparse(base_url).hostname]
                           if selected.allow_private_endpoint else None)
        url = base_url + route
        try:
            response = external_http.request(
                "POST",
                url,
                api_key=selected.api_key,
                json_body=payload,
                timeout=selected.timeout_seconds,
                max_response_bytes=selected.max_response_bytes,
                allowed_private_hosts=allowed_private,
            )
        except external_http.EgressBlockedError as exc:
            raise ProviderFault("egress_blocked", str(exc)) from None
        except external_http.AuthFailedError as exc:
            raise ProviderFault("http_auth_failed", str(exc)) from None
        except external_http.RateLimitedError as exc:
            raise ProviderFault("http_rate_limited", str(exc)) from None
        except external_http.BadRequestError as exc:
            raise ProviderFault("http_bad_request", str(exc)) from None
        except external_http.ServiceUnreachableError as exc:
            raise ProviderFault("http_unreachable", str(exc)) from None
        except external_http.ExternalHttpError as exc:
            raise ProviderFault("http_error", str(exc)) from None
        return response.content

    return transport


def _char_trigram_vector(text: str) -> List[float]:
    padded = f" {(' '.join((text or '').lower().split()))} "
    vector = [0.0] * OFFLINE_DIMENSION
    for position in range(max(0, len(padded) - 2)):
        gram = padded[position:position + 3]
        digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest, "big") % OFFLINE_DIMENSION
        vector[index] += 1.0
    norm = math.sqrt(sum(v * v for v in vector))
    if norm > 0:
        vector = [v / norm for v in vector]
    return vector


def offline_diagnostic_transport() -> EmbedTransport:
    def transport(route: str, payload: Dict[str, Any]) -> Any:
        if route == TEI_EMBEDDINGS_ROUTE:
            texts = payload.get("input") or []
            return {
                "data": [
                    {"index": i, "embedding": _char_trigram_vector(str(text))}
                    for i, text in enumerate(texts)
                ],
            }
        query = str(payload.get("query") or "")
        texts = payload.get("texts") or []
        query_vector = _char_trigram_vector(query)
        results = []
        for i, text in enumerate(texts):
            text_vector = _char_trigram_vector(str(text))
            score = sum(a * b for a, b in zip(query_vector, text_vector))
            results.append({"index": i, "score": score})
        return results

    return transport


def scripted_transport(*, dimension: int = OFFLINE_DIMENSION,
                       seed: str = "scripted-double",
                       faults: Optional[Dict[str, str]] = None) -> EmbedTransport:
    faults = dict(faults or {})

    def deterministic_vector(text: str) -> List[float]:
        vector = [0.0] * dimension
        material = f"{seed}\x1f{text}"
        for word_position, word in enumerate((text or "").lower().split()):
            digest = hashlib.blake2b(
                f"{material}\x1f{word_position}\x1f{word}".encode("utf-8"),
                digest_size=8).digest()
            index = int.from_bytes(digest, "big") % dimension
            vector[index] += 1.0 + (word_position % 3) * 0.25
        norm = math.sqrt(sum(v * v for v in vector))
        if norm > 0:
            vector = [v / norm for v in vector]
        return vector

    def transport(route: str, payload: Dict[str, Any]) -> Any:
        fault = faults.get(route)
        if fault == "http_unreachable":
            raise ProviderFault("http_unreachable", "scripted transport unreachable")
        if fault == "http_auth_failed":
            raise ProviderFault("http_auth_failed", "scripted transport denied credentials")
        if fault == "http_bad_request":
            raise ProviderFault("http_bad_request", "scripted transport rejected request")
        if route == TEI_EMBEDDINGS_ROUTE:
            texts = payload.get("input") or []
            if fault == "truncated_payload":
                return b'{"data": [{"index": 0'
            rows = [
                {"index": i, "embedding": deterministic_vector(str(text))}
                for i, text in enumerate(texts)
            ]
            if fault == "non_finite_value" and rows:
                rows[0]["embedding"] = [float("nan")] * dimension
            if fault == "dimension_mismatch" and rows:
                rows[0]["embedding"] = [0.0] * (dimension + 1)
            if fault == "duplicate_index" and len(rows) > 1:
                rows[1]["index"] = rows[0]["index"]
            if fault == "missing_index" and rows:
                rows.pop()
            if fault == "out_of_range_index" and rows:
                rows[-1]["index"] = len(rows)
            if fault == "malformed_output":
                return {"unexpected": True}
            return {"data": rows}
        texts = payload.get("texts") or []
        if fault == "truncated_payload":
            return b'[{"index": 0'
        query_vector = _char_trigram_vector(str(payload.get("query") or ""))
        results = []
        for i, text in enumerate(texts):
            text_vector = deterministic_vector(str(text))
            score = sum(a * b for a, b in zip(query_vector, text_vector))
            results.append({"index": i, "score": score})
        if fault == "non_finite_value" and results:
            results[0]["score"] = float("inf")
        if fault == "out_of_range_index" and results:
            results[-1]["index"] = len(results)
        if fault == "duplicate_index" and len(results) > 1:
            results[1]["index"] = results[0]["index"]
        if fault == "missing_index" and len(results) > 1:
            results = results[:-1]
        if fault == "malformed_output":
            return [1, 2, 3]
        return results

    return transport


class UnavailableProvider(RuntimeError):
    pass


def resolve_providers(profile: str,
                      run_config: Optional[Dict[str, Any]] = None
                      ) -> Tuple[VectorGateway, ProviderIdentity, Dict[str, Any]]:
    if profile == "offline-diagnostic":
        identity = ProviderIdentity(
            profile="offline-diagnostic",
            model="offline-char-trigram",
            revision="offline-diagnostic-v1",
            tokenizer_revision="identity-char-level-v1",
            dimension=OFFLINE_DIMENSION,
            license="first-party deterministic diagnostic (Apache-2.0 repository license)",
            notice="Deterministic character-trigram vectors over synthetic non-PHI data "
                   "only; not a semantic model and never model/runtime qualification.",
            qualifier=QUALIFIER_DIAGNOSTIC,
            rerank_model="offline-char-trigram",
            rerank_revision="offline-diagnostic-v1",
            rerank_capable=True,
        )
        gateway = VectorGateway(identity, offline_diagnostic_transport())
        return gateway, identity, {"state": "available", "reason": "local deterministic diagnostic"}
    if profile == "scripted-double":
        faults = {}
        if isinstance(run_config, dict):
            faults = dict(run_config.get("faults") or {})
        identity = ProviderIdentity(
            profile="scripted-double",
            model="scripted-double",
            revision="scripted-double-v1",
            tokenizer_revision="identity-word-level-v1",
            dimension=OFFLINE_DIMENSION,
            license="first-party deterministic test double (Apache-2.0 repository license)",
            notice="Deterministic scripted provider double for offline CI; fault injection "
                   "exercises validation paths and never establishes model qualification.",
            qualifier=QUALIFIER_DOUBLE,
            rerank_model="scripted-double",
            rerank_revision="scripted-double-v1",
            rerank_capable=True,
        )
        gateway = VectorGateway(identity, scripted_transport(faults=faults))
        return gateway, identity, {"state": "available", "reason": "deterministic CI double"}
    if profile == "tei-http":
        if run_config is None:
            return _unavailable_tei("no run configuration provided for the tei-http profile")
        if not isinstance(run_config, dict):
            return _unavailable_tei("run configuration must be a JSON object")
        if not run_config:
            return _unavailable_tei("no run configuration provided for the tei-http profile")
        base_url = run_config.get("base_url")
        embed_model = run_config.get("embed_model")
        embed_revision = run_config.get("embed_revision")
        tokenizer_revision = run_config.get("tokenizer_revision")
        dimension = run_config.get("dimension")
        license_name = run_config.get("license")
        missing = [
            name for name, value in (
                ("base_url", base_url),
                ("embed_model", embed_model),
                ("embed_revision", embed_revision),
                ("tokenizer_revision", tokenizer_revision),
                ("license", license_name),
            ) if not value
        ]
        if dimension is None:
            missing.append("dimension")
        if missing:
            return _unavailable_tei(f"run configuration is missing: {sorted(missing)}")
        if not isinstance(base_url, str) or not base_url.lower().startswith(("http://", "https://")):
            return _unavailable_tei("base_url must be an http(s) URL")
        if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension <= 0:
            return _unavailable_tei("dimension must be a positive integer")
        if not isinstance(embed_model, str) or not str(embed_model).strip():
            return _unavailable_tei("embed_model must be a non-empty string")
        rerank_model = run_config.get("rerank_model")
        rerank_revision = run_config.get("rerank_revision")
        rerank_base_url = run_config.get("rerank_base_url")
        rerank_tokenizer_revision = run_config.get("rerank_tokenizer_revision")
        if bool(rerank_model) != bool(rerank_revision):
            return _unavailable_tei("rerank_model and rerank_revision must be declared together")
        if any((rerank_model, rerank_base_url, rerank_tokenizer_revision)):
            missing = [name for name, value in (
                ("rerank_model", rerank_model), ("rerank_revision", rerank_revision),
                ("rerank_base_url", rerank_base_url),
                ("rerank_tokenizer_revision", rerank_tokenizer_revision),
            ) if not isinstance(value, str) or not value.strip()]
            if missing:
                return _unavailable_tei(f"rerank configuration is missing: {sorted(missing)}")
            if not rerank_base_url.lower().startswith(("http://", "https://")):
                return _unavailable_tei("rerank_base_url must be an http(s) URL")
        digest_names = ["embedding_runtime_digest", "embedding_assets_digest"]
        if rerank_model:
            digest_names.extend(("rerank_runtime_digest", "rerank_assets_digest"))
        for name in digest_names:
            value = run_config.get(name)
            if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
                return _unavailable_tei(f"{name} must be an immutable sha256 digest")
        identity = ProviderIdentity(
            profile="tei-http",
            model=str(embed_model),
            revision=str(embed_revision),
            tokenizer_revision=str(tokenizer_revision),
            dimension=dimension,
            license=str(license_name),
            notice="TEI exposes /v1/embeddings and a separate /rerank route; qualification "
                   "requires a bounded manual local run attested by the operator.",
            qualifier=QUALIFIER_UNQUALIFIED,
            rerank_model=str(rerank_model) if rerank_model else None,
            rerank_revision=str(rerank_revision) if rerank_revision else None,
            rerank_capable=bool(rerank_model),
            embedding_endpoint=external_http.normalize_url(base_url),
            rerank_endpoint=(external_http.normalize_url(rerank_base_url)
                             if rerank_base_url else None),
            rerank_tokenizer_revision=rerank_tokenizer_revision,
            embedding_runtime_digest=run_config["embedding_runtime_digest"],
            embedding_assets_digest=run_config["embedding_assets_digest"],
            rerank_runtime_digest=run_config.get("rerank_runtime_digest") if rerank_model else None,
            rerank_assets_digest=run_config.get("rerank_assets_digest") if rerank_model else None,
        )
        endpoint = TeiEndpoint(
            base_url=str(base_url),
            api_key=str(run_config.get("api_key") or ""),
            timeout_seconds=int(run_config.get("timeout_seconds") or 20),
            max_response_bytes=int(run_config.get("max_response_bytes") or 4_000_000),
            allow_private_endpoint=bool(run_config.get("allow_private_endpoint") or False),
        )
        rerank_endpoint = (TeiEndpoint(
            base_url=identity.rerank_endpoint,
            api_key=str(run_config.get("rerank_api_key") or ""),
            timeout_seconds=endpoint.timeout_seconds,
            max_response_bytes=endpoint.max_response_bytes,
            allow_private_endpoint=endpoint.allow_private_endpoint,
        ) if identity.rerank_capable else None)
        gateway = VectorGateway(identity, tei_http_transport(endpoint, rerank_endpoint))
        return gateway, identity, {
            "state": "available",
            "reason": "operator-provided run configuration",
            "endpoint": endpoint.to_dict(),
            "rerank_endpoint": rerank_endpoint.to_dict() if rerank_endpoint else None,
        }
    raise UnavailableProvider(f"unknown provider profile {profile!r}")


def _unavailable_tei(reason: str) -> Tuple[VectorGateway, ProviderIdentity, Dict[str, Any]]:
    identity = ProviderIdentity(
        profile="tei-http",
        model="unconfigured",
        revision="unconfigured",
        tokenizer_revision="unconfigured",
        dimension=0,
        license="unknown",
        notice="TEI exposes /v1/embeddings and a separate /rerank route.",
        qualifier=QUALIFIER_UNQUALIFIED,
    )
    return VectorGateway(identity, tei_http_transport(TeiEndpoint(base_url="http://unconfigured.invalid"))), identity, {
        "state": "unavailable",
        "reason": reason,
    }
