"""Classifies a pending tool call's risk (egress, cross-principal, irreversible,
untrusted-tainted) and builds the confirmation card and escalation signal shown
before high-risk calls run; used by hitl_confirmation.py.
"""

from __future__ import annotations

import os
import ipaddress
import re
from dataclasses import dataclass, field
from importlib import import_module
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlsplit

EGRESS, CROSS_PRINCIPAL, IRREVERSIBLE, UNTRUSTED_TAINTED = (
    "egress",
    "cross_principal",
    "irreversible",
    "untrusted_tainted",
)

_EGRESS_TOOLS = {
    "send_email",
    "send_message",
    "fetch_page",
    "http_get",
    "http_post",
    "webhook",
}
_EGRESS_PREFIXES = ("http_", "send_", "post_", "fetch_", "upload_")
_IRREVERSIBLE_PREFIXES = (
    "delete_",
    "drop_",
    "wipe_",
    "purge_",
    "transfer_",
    "pay_",
    "deploy_",
)

# Host-owned only — never a model or remote agent's claim
_PUBLIC_READS = {
    ("web-research-1", "web_search"): ("web_research", "WebResearchAgent", "query", {"max_results"}),
    ("web-research-1", "fetch_page"): ("web_research", "WebResearchAgent", "url", set()),
    ("web-research-1", "research_brief"): ("web_research", "WebResearchAgent", "topic", {"depth"}),
    ("general-1", "search_arxiv"): ("general", "GeneralAgent", "query", {"max_results"}),
    ("summarizer-1", "summarize_url"): ("summarizer", "SummarizerAgent", "url", set()),
}
_SENSITIVE_INPUT = re.compile(
    r"(?i)(?:\b(?:api[_ -]?key|access[_ -]?token|secret|password|authorization|bearer|"
    r"patient|medical.record|mrn|ssn|date.of.birth|dob|credential|token|signature)\b|-----BEGIN|"
    r"\bsk-[\w-]{12,}|\beyJ[\w-]+\.[\w-]+\.[\w-]+|"
    r"\b\d{3}-\d{2}-\d{4}\b|\b\d{7,}\b|[\w.+-]+@[\w-]+\.[\w.-]+|"
    r"\b\d{3}[- .]\d{3}[- .]\d{4}\b)"
)
_SENSITIVE_QUERY_KEYS = frozenset({
    "key", "api_key", "apikey", "token", "access_token", "auth", "authorization",
    "password", "secret", "code", "state", "sig", "signature", "session", "session_id",
    "x-amz-signature", "x-amz-credential",
})


def sensitive_url(url) -> bool:
    return bool(url.username or url.password or any(
        key.lower() in _SENSITIVE_QUERY_KEYS for key, _ in parse_qsl(url.query)))


def registered_public_reader(orch, agent_id: str, tool_name: str) -> bool:
    contract = _PUBLIC_READS.get((agent_id, tool_name))
    if contract is None:
        return False
    package, class_name, _, _ = contract
    try:
        module = import_module(f"agents.{package}.{package}_agent")
        agent = getattr(orch, "local_agents", {}).get(agent_id)
        registry = import_module(f"agents.{package}.mcp_tools").TOOL_REGISTRY
        entry = agent.mcp_server.tools.get(tool_name)
        return (type(agent) is getattr(module, class_name)
                and entry["function"] is registry[tool_name]["function"]
                and entry.get("scope") in {"tools:read", "tools:search"})
    except (AttributeError, ImportError, KeyError, TypeError):
        return False


def public_read_arguments(agent_id: str, tool_name: str, args: Optional[dict]) -> bool:
    contract = _PUBLIC_READS.get((agent_id, tool_name))
    if contract is None or not isinstance(args, dict):
        return False
    _, _, value_key, options = contract
    if set(args) - ({value_key} | options):
        return False
    value = args.get(value_key)
    if not isinstance(value, str) or not 0 < len(value.strip()) <= 4096:
        return False
    decoded = unquote(unquote(value))
    if any(ord(char) < 32 for char in decoded) or _SENSITIVE_INPUT.search(decoded):
        return False
    if "max_results" in args and (type(args["max_results"]) is not int
                                  or not 1 <= args["max_results"] <= 20):
        return False
    if "depth" in args and args["depth"] not in ("shallow", "standard"):
        return False
    if value_key == "url":
        try:
            url = urlsplit(decoded)
            host = url.hostname
            if (url.scheme not in ("http", "https") or not host or sensitive_url(url)
                    or url.fragment or url.port not in (None, 80, 443)
                    or host == "localhost" or host.endswith((".local", ".internal"))):
                return False
            try:
                if not ipaddress.ip_address(host).is_global:
                    return False
            except ValueError:
                if "." not in host:
                    return False
        except ValueError:
            return False
    return True

_RISK_PHRASES = {
    EGRESS: "send data off this system",
    IRREVERSIBLE: "make an irreversible change",
    CROSS_PRINCIPAL: "act on another account",
    UNTRUSTED_TAINTED: "use untrusted data",
}


def hitl_enabled() -> bool:
    return os.getenv("FF_HITL_HIGHRISK", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _is_egress(tool_name: str) -> bool:
    name = tool_name or ""
    return name in _EGRESS_TOOLS or name.startswith(_EGRESS_PREFIXES)


def _is_irreversible(tool_name: str) -> bool:
    return (tool_name or "").startswith(_IRREVERSIBLE_PREFIXES)


def assess_risk(
    tool_name: str,
    args: Optional[dict] = None,
    *,
    actor_principal: Optional[str] = None,
    target_principal: Optional[str] = None,
    trust: str = "trusted",
    agent_id: Optional[str] = None,
    public_reader: bool = False,
) -> List[str]:
    risks: List[str] = []
    public_read = public_reader and public_read_arguments(agent_id, tool_name, args)
    if not public_read and (_is_egress(tool_name) or (agent_id, tool_name) in _PUBLIC_READS):
        risks.append(EGRESS)
    if _is_irreversible(tool_name):
        risks.append(IRREVERSIBLE)
    if actor_principal and target_principal and actor_principal != target_principal:
        risks.append(CROSS_PRINCIPAL)
    if trust == "untrusted" and not public_read:
        risks.append(UNTRUSTED_TAINTED)
    return sorted(risks)


def requires_confirmation(risks: List[str]) -> bool:
    return bool(risks)


def is_high_risk(risks: List[str]) -> bool:
    return (
        IRREVERSIBLE in risks
        or CROSS_PRINCIPAL in risks
        or len(risks) >= 2
    )


@dataclass(frozen=True)
class ConfirmationRequest:
    tool: str
    risks: Tuple[str, ...]
    summary: str
    provenance: Dict = field(default_factory=dict)


def _summarize(risks: List[str]) -> str:
    phrases = [_RISK_PHRASES[r] for r in risks if r in _RISK_PHRASES]
    if not phrases:
        return "This will run a sensitive action — confirm?"
    if len(phrases) == 1:
        joined = phrases[0]
    elif len(phrases) == 2:
        joined = " and ".join(phrases)
    else:
        joined = ", ".join(phrases[:-1]) + ", and " + phrases[-1]
    return f"This will {joined} — confirm?"


def confirmation_request(
    tool_name: str,
    risks: List[str],
    *,
    provenance: Optional[dict] = None,
) -> ConfirmationRequest:
    return ConfirmationRequest(
        tool=tool_name,
        risks=tuple(risks),
        summary=_summarize(risks),
        provenance=dict(provenance) if provenance else {},
    )


def escalation_needed(
    risks: List[str],
    *,
    denied_attempts: int = 0,
    denial_threshold: int = 2,
) -> bool:
    return is_high_risk(risks) and denied_attempts >= denial_threshold
