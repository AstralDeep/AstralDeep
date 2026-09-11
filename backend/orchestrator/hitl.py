"""Runtime human-in-the-loop for high-risk actions — 033 Wave-4 (C-S11).

A pending tool call is classified by **typed risk codes** — egress (data leaves
this system), cross_principal (the call acts on a different account than the
actor), irreversible (the action can't be undone), and untrusted_tainted (the
call is built from untrusted data). Any risk means the user is shown a
provenance-bearing **confirmation card** before the call runs; the strongest
classes (irreversible / cross_principal) and any *compounded* risk (≥2 codes at
once) mark the call **high-risk**.

When a high-risk action keeps being attempted — the user is repeatedly asked, or
the same risky call is retried past a threshold — that is an **escalation
signal**: hand off warm to a human/operator instead of looping on confirmations.

Pure + deterministic; stdlib only. **No new dependency.** Flag
``FF_HITL_HIGHRISK`` (default OFF) gates the dispatch enforcement; the
classification helpers themselves are side-effect free and always safe to call.
"""
from __future__ import annotations

import os
import ipaddress
import re
from dataclasses import dataclass, field
from importlib import import_module
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlsplit

# ── Typed risk codes ─────────────────────────────────────────────────────────
EGRESS, CROSS_PRINCIPAL, IRREVERSIBLE, UNTRUSTED_TAINTED = (
    "egress",
    "cross_principal",
    "irreversible",
    "untrusted_tainted",
)

#: Tools whose name alone marks the call as egress (data leaves the system).
_EGRESS_TOOLS = {
    "send_email",
    "send_message",
    "fetch_page",
    "http_get",
    "http_post",
    "webhook",
}
#: Name prefixes that also indicate egress.
_EGRESS_PREFIXES = ("http_", "send_", "post_", "fetch_", "upload_")
#: Name prefixes that indicate an irreversible (non-undoable) action.
_IRREVERSIBLE_PREFIXES = (
    "delete_",
    "drop_",
    "wipe_",
    "purge_",
    "transfer_",
    "pay_",
    "deploy_",
)

# Host-owned contracts, not model annotations or a remote agent's claimed name.
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
    """Credential-bearing URL forms never qualify as ordinary public reads."""
    return bool(url.username or url.password or any(
        key.lower() in _SENSITIVE_QUERY_KEYS for key, _ in parse_qsl(url.query)))


def registered_public_reader(orch, agent_id: str, tool_name: str) -> bool:
    """Verify the actual in-process first-party class, function and read scope.

    A remote/user agent cannot opt into this contract by copying its names or
    metadata. Transport fallback conservatively retains confirmation.
    """
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
    """Recognize bounded public query/GET arguments, never arbitrary payloads.

    This only narrows the redundant HITL notice. DNS/redirect/SSRF validation,
    PHI hooks, policy and tool grants remain independent dispatch requirements.
    Unknown fields, credentials, identifiers and non-public URL forms do not
    receive the public-read exception.
    """
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

#: Human phrase shown on the confirmation card for each risk code.
_RISK_PHRASES = {
    EGRESS: "send data off this system",
    IRREVERSIBLE: "make an irreversible change",
    CROSS_PRINCIPAL: "act on another account",
    UNTRUSTED_TAINTED: "use untrusted data",
}


def hitl_enabled() -> bool:
    """FF_HITL_HIGHRISK feature flag (default OFF; feature 033 C-S11)."""
    return os.getenv("FF_HITL_HIGHRISK", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _is_egress(tool_name: str) -> bool:
    """Whether ``tool_name`` sends data off the system (exact name or prefix)."""
    name = tool_name or ""
    return name in _EGRESS_TOOLS or name.startswith(_EGRESS_PREFIXES)


def _is_irreversible(tool_name: str) -> bool:
    """Whether ``tool_name`` performs a non-undoable action (prefix match)."""
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
    """Classify a pending tool call into the sorted list of risk codes that apply.

    Deterministic; an empty list means the call carries no recognized risk.

    - :data:`EGRESS` — the tool sends data off this system (name or prefix).
    - :data:`IRREVERSIBLE` — the tool performs a non-undoable action (prefix).
    - :data:`CROSS_PRINCIPAL` — ``actor_principal`` and ``target_principal`` are
      both set and differ (the call acts on someone else's account).
    - :data:`UNTRUSTED_TAINTED` — ``trust`` is ``"untrusted"`` (the call is built
      from untrusted data).

    ``public_reader`` is a server-verified registration, never a client hint.
    Its bounded public queries/GETs are already authorized by the research
    request. Sensitive/unknown arguments retain egress review. Untrusted source
    URLs may be read, but never gain authority to write or send other payloads.
    """
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
    """True when *any* risk applies — the user must confirm before the call runs."""
    return bool(risks)


def is_high_risk(risks: List[str]) -> bool:
    """True for the strongest risk classes or any compounded risk.

    The irreversible and cross_principal classes are high-risk on their own; so
    is any call carrying two or more risk codes at once (compounded risk).
    """
    return (
        IRREVERSIBLE in risks
        or CROSS_PRINCIPAL in risks
        or len(risks) >= 2
    )


@dataclass(frozen=True)
class ConfirmationRequest:
    """An immutable, provenance-bearing confirmation prompt for a pending call.

    - ``tool`` — the tool the call would invoke.
    - ``risks`` — the applicable risk codes (as a tuple, so the record is hashable).
    - ``summary`` — a human sentence naming what the call will do.
    - ``provenance`` — supporting context shown on the card (where the data /
      authority came from).
    """

    tool: str
    risks: Tuple[str, ...]
    summary: str
    provenance: Dict = field(default_factory=dict)


def _summarize(risks: List[str]) -> str:
    """A human sentence: 'This will <phrase>[ and <phrase>…] — confirm?'."""
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
    """Build the confirmation card for a pending high-/any-risk call.

    The ``summary`` names each applicable risk in a human sentence; ``provenance``
    (defaulting to ``{}``) carries the context shown to the user so they can see
    *why* the call is risky before approving it.
    """
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
    """Whether a high-risk action that keeps being attempted should escalate.

    True when the call is high-risk AND it has been denied / re-prompted at least
    ``denial_threshold`` times — i.e. the user keeps being asked or a risky action
    keeps being retried, which is the signal to hand off warm to a human rather
    than loop on confirmations.
    """
    return is_high_risk(risks) and denied_attempts >= denial_threshold
