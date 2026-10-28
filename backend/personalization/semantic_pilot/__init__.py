"""Read-only qualification pilot for owner-scoped semantic memory candidate generation
and optional reranking over the frozen synthetic corpus; compares the actual lexical
baseline through memory_tools.py's non-reinforcing seams without changing production
retrieval defaults.
"""

from .bounds import Bounds, BoundsExceeded, estimate_tokens
from .cache import EphemeralEmbeddingCache
from .candidates import rerank_candidates, semantic_candidates
from .corpus import CORPUS_ID, CORPUS_DIGEST, MemoryCase, MemoryRowSpec, load_corpus
from .providers import (
    ProviderFault,
    ProviderIdentity,
    ProviderUsage,
    VectorGateway,
    resolve_providers,
)
from .report import ADOPTION_STATES, build_report, validate_report, write_report
from .runner import PilotConfig, RunCancelled, run_pilot

__all__ = [
    "ADOPTION_STATES",
    "CORPUS_DIGEST",
    "CORPUS_ID",
    "Bounds",
    "BoundsExceeded",
    "EphemeralEmbeddingCache",
    "MemoryCase",
    "MemoryRowSpec",
    "PilotConfig",
    "ProviderFault",
    "ProviderIdentity",
    "ProviderUsage",
    "RunCancelled",
    "VectorGateway",
    "build_report",
    "estimate_tokens",
    "load_corpus",
    "rerank_candidates",
    "resolve_providers",
    "run_pilot",
    "semantic_candidates",
    "validate_report",
    "write_report",
]
