"""Value-level data-flow taint tracking: a value's trust is the minimum over its
ancestors, and untrusted data reaching a write/egress sink is denied, surviving
laundering through intermediate tools. Enforced in orchestrator.py's dispatch path.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from typing import Any, Dict, Iterable, List, Optional

TRUSTED, INTERNAL, UNTRUSTED = 2, 1, 0
_NAMES = {TRUSTED: "trusted", INTERNAL: "internal", UNTRUSTED: "untrusted"}

_UNTRUSTED_TOOLS = {
    "web_search", "fetch_page", "research_brief",
    "summarize_url", "compare_documents",
}
_UNTRUSTED_AGENTS = {"web-research-1", "summarizer-1",
                     "remote-compute-1",
                     "computer-use-1"}

_SINK_TOOLS = {
    "send_*", "post_*", "create_*", "update_*",
    "delete_*", "write_*", "execute_*", "upload_*", "webhook*",
    "http_*", "wire_*", "transfer_*",
}


def taint_enabled() -> bool:
    return os.getenv("FF_TAINT_TRACKING", "false").strip().lower() in ("1", "true", "yes", "on")


def trust_name(trust: Optional[int]) -> str:
    return _NAMES.get(trust if trust is not None else TRUSTED, "untrusted")


def combine(trusts: Iterable[Optional[int]]) -> int:
    vals = [t for t in trusts if t is not None]
    return min(vals) if vals else TRUSTED


def classify_source(agent: Optional[str], tool: Optional[str]) -> int:
    if (tool or "") in _UNTRUSTED_TOOLS or (agent or "") in _UNTRUSTED_AGENTS:
        return UNTRUSTED
    return INTERNAL


def is_sink(agent: Optional[str], tool: Optional[str]) -> bool:
    name = tool or ""
    return any(fnmatch.fnmatchcase(name, pat) for pat in _SINK_TOOLS)


def check_flow(trust: int) -> str:
    if trust <= UNTRUSTED:
        return "deny"
    if trust <= INTERNAL:
        return "escalate"
    return "allow"


def _iter_strings(value: Any) -> Iterable[str]:
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


def user_supplied(value: str, user_text: Optional[str]) -> bool:
    if not user_text or not isinstance(value, str):
        return False
    v = value.strip()
    return bool(v) and v in user_text


class TaintTracker:
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
        return self._trust.get(self.fingerprint(value), TRUSTED)

    def effective_trust_of_args(self, args: Any,
                                user_text: Optional[str] = None) -> int:
        return combine(
            TRUSTED if user_supplied(s, user_text) else self.trust_of(s)
            for s in _iter_strings(args))

    def record_output(self, output: Any, source_trust: int, input_trust: int) -> int:
        t = combine([source_trust, input_trust])
        if t < TRUSTED:
            for s in _iter_strings(output):
                self.mark(s, t)
        return t

    def known(self) -> List[str]:
        return list(self._trust)
