"""Taint / provenance tracking.

A value-level data-flow control: data carries a trust label whose effective
value is the **minimum over its data ancestors** (a value is only as trusted as
its least-trusted input), and a call that would carry untrusted-tainted values
into a write/egress **sink** is refused.

The defense survives **multi-hop laundering**: taint propagates through any tool
that *consumes* untrusted input — if tool A (an untrusted web source) feeds tool
B, B's output is recorded untrusted too, so washing data through an intermediate
tool doesn't bleach it. (Laundering through an LLM *rephrase* is the separate
spotlight/datamark concern.)

Pure + deterministic; a content fingerprint identifies a value. Flag
``FF_TAINT_TRACKING`` (default OFF) gates the dispatch enforcement, which is
additive + fail-open: an unknown value is treated as trusted (constants, user
intent), so with the flag off — or on with no untrusted sources seen — nothing
changes.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from typing import Any, Dict, Iterable, List, Optional

# Trust lattice — higher is more trusted. Effective trust = MIN over ancestors.
TRUSTED, INTERNAL, UNTRUSTED = 2, 1, 0
_NAMES = {TRUSTED: "trusted", INTERNAL: "internal", UNTRUSTED: "untrusted"}

#: Tools/agents that introduce UNTRUSTED data (web / third-party content).
_UNTRUSTED_TOOLS = {
    "web_search", "fetch_page", "research_brief",
    "summarize_url", "compare_documents",
}
_UNTRUSTED_AGENTS = {"web-research-1", "summarizer-1",
                     # Feature 063 (FR-039): remote machines return untrusted text
                     # (banners, filenames, job names, process command columns).
                     "remote-observe-1", "remote-control-1"}

#: Write / egress sinks — untrusted data must not flow into their arguments.
#: (``fetch_page``/``web_search`` are BOTH sources and sinks: a URL built from
#: untrusted data is an SSRF-class risk.)
_SINK_TOOLS = {
    "send_email", "send_message", "post_*", "create_*", "update_*",
    "delete_*", "write_*", "execute_*", "fetch_page", "web_search",
    "http_*", "wire_*", "transfer_*",
}


def taint_enabled() -> bool:
    """FF_TAINT_TRACKING feature flag (default OFF)."""
    return os.getenv("FF_TAINT_TRACKING", "false").strip().lower() in ("1", "true", "yes", "on")


def trust_name(trust: Optional[int]) -> str:
    return _NAMES.get(trust if trust is not None else TRUSTED, "untrusted")


def combine(trusts: Iterable[Optional[int]]) -> int:
    """Effective trust = the MINIMUM over data ancestors. With no ancestors the
    value is a constant ⇒ trusted."""
    vals = [t for t in trusts if t is not None]
    return min(vals) if vals else TRUSTED


def classify_source(agent: Optional[str], tool: Optional[str]) -> int:
    """Trust of data PRODUCED by an (agent, tool). Web/third-party readers are
    untrusted; everything else is internal."""
    if (tool or "") in _UNTRUSTED_TOOLS or (agent or "") in _UNTRUSTED_AGENTS:
        return UNTRUSTED
    return INTERNAL


def is_sink(agent: Optional[str], tool: Optional[str]) -> bool:
    """Whether a call is a write/egress sink (taint must be checked)."""
    name = tool or ""
    return any(fnmatch.fnmatchcase(name, pat) for pat in _SINK_TOOLS)


def check_flow(trust: int) -> str:
    """The value-level data-flow policy. Untrusted data into a sink ⇒ ``deny``;
    internal ⇒ ``escalate`` (allow but flag); trusted ⇒ ``allow``."""
    if trust <= UNTRUSTED:
        return "deny"
    if trust <= INTERNAL:
        return "escalate"
    return "allow"


def _iter_strings(value: Any) -> Iterable[str]:
    """Yield the string leaves of a value tree, skipping system-injected
    ``_``-prefixed dict keys (credentials, the embedded token, …)."""
    if isinstance(value, str):
        if value.strip():
            yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            if not str(k).startswith("_"):
                yield from _iter_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_strings(v)


class TaintTracker:
    """Per-scope content-taint store. A value is keyed by a content fingerprint;
    its trust is the minimum ever recorded for it. Multi-hop laundering survives
    because a derived value is recorded at the min of its source and its inputs,
    so a later call carrying it still sees the taint."""

    def __init__(self) -> None:
        self._trust: Dict[str, int] = {}

    @staticmethod
    def fingerprint(value: Any) -> str:
        s = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
        s = s.strip()
        return hashlib.sha1(s.encode("utf-8")).hexdigest() if s else ""

    def mark(self, value: Any, trust: int) -> None:
        fp = self.fingerprint(value)
        if fp:
            self._trust[fp] = min(int(trust), self._trust.get(fp, TRUSTED))

    def trust_of(self, value: Any) -> int:
        """Trust of a single value — its recorded label, else TRUSTED (unknown
        values are constants / user intent)."""
        return self._trust.get(self.fingerprint(value), TRUSTED)

    def effective_trust_of_args(self, args: Any) -> int:
        """The call's effective trust = MIN over every string leaf in its args
        (only as trusted as its least-trusted input value)."""
        return combine(self.trust_of(s) for s in _iter_strings(args))

    def record_output(self, output: Any, source_trust: int, input_trust: int) -> int:
        """Record a tool's output values at ``min(source_trust, input_trust)`` so
        taint propagates through the chain. Returns the recorded trust."""
        t = combine([source_trust, input_trust])
        if t < TRUSTED:  # only remember non-trusted values (keeps the store small)
            for s in _iter_strings(output):
                self.mark(s, t)
        return t

    def known(self) -> List[str]:
        return list(self._trust)
