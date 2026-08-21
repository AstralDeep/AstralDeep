"""Deterministic Analyze gate (feature 057).

Validates a DRAFTED agent spec against the agent constitution's A–L checklist
BEFORE any code is generated (contracts/analyze-gate.md). Distinct from the
code-level gates (``code_security``/``agent_validator``) which run on generated
code, post-generation. Each A–L check is a rule over declared fields (pass/fail
decidable); the plain-language message is the only free-text part. A failing
draft never reaches ``generate_code`` (FR-003/SC-004).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from orchestrator.tool_permissions import VALID_SCOPES

# This is intentionally independent from the baked constitution version. Any
# deterministic change to the Analyze rules below must increment this positive
# integer so already-validated user agents are rechecked on the next boot.
ANALYZE_POLICY_REVISION = "2"

# Reserved / non-user identity prefixes and stems a user agent may not take
# (Constitution H — non-colliding identity). Meta pseudo-agents use "__".
_RESERVED_PREFIXES = ("__",)
_RESERVED_STEMS = frozenset({
    "orchestrator", "scheduler", "memory", "subtasks", "desktop_codegen",
    "general", "connectors", "weather", "medical", "ml-services", "ml_services",
    "summarizer", "web-research", "web_research", "journal-review", "journal_review",
    "dice-roller", "dice_roller", "cresco",
})

# Red-flag markers (case-insensitive substrings) that indicate a constitution
# violation in the drafted spec text. Kept tight to avoid false positives.
_CROSS_USER = re.compile(
    r"\b(another|other|different|someone else'?s|all)\s+users?\b|\bother user'?s\b|\bevery user\b",
    re.I)
_TRUST_BYPASS = re.compile(
    r"bypass(ing)?\s+(the\s+)?(boundary|gate|orchestrator|server|permission)"
    r"|trust\s+the\s+client|client-?side\s+(check|only)\s+(is\s+)?(enough|sufficient)"
    r"|ignore\s+the\s+(gate|boundary|server)", re.I)
_UNBOUNDED = re.compile(r"\bunbounded\b|\bno\s+limit\b|\bwhile\s+true\b|\binfinite(ly)?\b|\bforever\b", re.I)
_SECRET_EXFIL = re.compile(
    r"read\s+(the\s+)?env(ironment)?\b|environment\s+variables|"
    r"other\s+agents?'?\s+(tokens?|credentials?)|orchestrator\s+internals?|"
    r"dump\s+(secrets?|credentials?)|steal\s+(the\s+)?(token|key|password|secret)", re.I)
_SHARE = re.compile(r"\b(share|publish|make\s+public|transfer)\b.{0,30}\b(agent|it|this|with)\b", re.I)
_SHARE_TOOL = re.compile(r"share|publish|make_public|make_shared|transfer_agent", re.I)
_URLISH = re.compile(r"^(https?://|[\w.-]+\.[a-z]{2,}(/|$)|[\w.+-]+@[\w.-]+)", re.I)


@dataclass
class Violation:
    principle: str            # "A".."L"
    title: str
    plain_language: str
    offending_field: str


@dataclass
class AnalyzeResult:
    passed: bool
    constitution_version: Optional[str]
    violations: List[Violation] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        policy_revision = (
            None
            if self.constitution_version is None
            else (
                f"constitution={self.constitution_version};"
                f"analyze={ANALYZE_POLICY_REVISION}"
            )
        )
        return {
            "passed": self.passed,
            "constitution_version": self.constitution_version,
            "policy_revision": policy_revision,
            "violations": [
                {"principle": v.principle, "title": v.title,
                 "plain_language": v.plain_language, "offending_field": v.offending_field}
                for v in self.violations
            ],
        }


def _text_of(spec: Dict[str, Any]) -> str:
    parts = [str(spec.get("display_name") or ""), str(spec.get("description") or "")]
    plan = spec.get("plan")
    if plan is not None:
        parts.append(json.dumps(plan, default=str))
    for a in (spec.get("clarify_answers") or []):
        parts.append(str(a))
    return "\n".join(parts)


def check(draft_spec: Dict[str, Any], *, constitution_version: Optional[str] = None,
          db=None) -> AnalyzeResult:
    """Evaluate ``draft_spec`` against A–L. Returns an :class:`AnalyzeResult`;
    ``passed`` is True only if no violation fires. Never raises on a normal spec.

    ``draft_spec`` keys: ``display_name``, ``description``, ``agent_id`` (proposed),
    ``owner_user_id``, ``declared_tools`` (list[str]), ``declared_scopes`` (list[str]),
    ``declared_egress`` (list[str]|None), ``plan`` (dict; may carry ``tools_used`` and
    ``tool_scopes``), ``clarify_answers`` (list). ``db`` enables the H collision check.
    """
    if constitution_version is None:
        # The policy owner validates the baked asset at import time. Propagate
        # any loader/validation failure instead of returning a passing result
        # without a version binding.
        from orchestrator.agent_constitution import AGENT_CONSTITUTION_VERSION

        constitution_version = AGENT_CONSTITUTION_VERSION

    v: List[Violation] = []
    text = _text_of(draft_spec)
    declared_tools = [str(t) for t in (draft_spec.get("declared_tools") or [])]
    declared_scopes = [str(s) for s in (draft_spec.get("declared_scopes") or [])]
    declared_egress = draft_spec.get("declared_egress")
    plan = draft_spec.get("plan") or {}
    agent_id = str(draft_spec.get("agent_id") or "")
    owner = draft_spec.get("owner_user_id")

    # A — owner-delegated authority only (no scope of its own; scopes must be
    # platform scope-level claims the owner could hold).
    bad_scopes = [s for s in declared_scopes if s not in VALID_SCOPES]
    if bad_scopes:
        v.append(Violation("A", "Owner-delegated authority only",
                           f"Your agent requests {bad_scopes}, which are not scopes a person can "
                           f"grant it. It may only use the platform's standard permissions "
                           f"({', '.join(VALID_SCOPES)}).", "declared_scopes"))

    # B — declared capability surface: everything the plan uses must be declared.
    used = [str(t) for t in (plan.get("tools_used") or [])]
    undeclared = [t for t in used if t not in declared_tools]
    if undeclared:
        v.append(Violation("B", "Declared capability surface",
                           f"The plan uses tools that were not declared: {undeclared}. Declare "
                           f"every tool the agent will use.", "plan.tools_used"))

    # B/C — every used capability has one valid, explicitly declared scope, and
    # every declared scope is justified by at least one capability.  Treat a
    # missing mapping as a denial: code generation otherwise supplies a default
    # that the owner never approved.
    tool_scopes = plan.get("tool_scopes")
    if isinstance(tool_scopes, dict):
        missing_scope = [tool for tool in used if tool not in tool_scopes]
        if missing_scope:
            v.append(Violation(
                "B",
                "Declared capability surface",
                f"These used tools have no declared permission: {missing_scope}.",
                "plan.tool_scopes",
            ))
        mapped_scopes = {str(name): str(scope) for name, scope in tool_scopes.items()}
        invalid_mapped = sorted({
            scope for scope in mapped_scopes.values() if scope not in VALID_SCOPES
        })
        if invalid_mapped:
            v.append(Violation(
                "A",
                "Owner-delegated authority only",
                f"The plan maps tools to invalid permissions: {invalid_mapped}.",
                "plan.tool_scopes",
            ))
        undeclared_mapped = sorted({
            scope
            for scope in mapped_scopes.values()
            if scope not in declared_scopes
        })
        if undeclared_mapped:
            v.append(Violation(
                "B",
                "Declared capability surface",
                f"The plan uses permissions that were not declared: {undeclared_mapped}.",
                "plan.tool_scopes",
            ))
        used_scopes = {str(s) for s in tool_scopes.values()}
        unused = [s for s in declared_scopes if s not in used_scopes]
        if unused:
            v.append(Violation("C", "Least privilege",
                               f"These requested permissions are not used by any capability: "
                               f"{unused}. Request only what the agent needs.", "declared_scopes"))
    elif used or declared_scopes:
        v.append(Violation("C", "Least privilege",
                           "Every used capability must map to one explicitly declared permission.",
                           "plan.tool_scopes"))

    # D — no cross-user reach.
    if _CROSS_USER.search(text):
        v.append(Violation("D", "No cross-user reach",
                           "The agent describes reaching another user's data or identity. A personal "
                           "agent may only touch its own owner's resources.", "description"))

    # E/F — untrusted-at-the-boundary / fail-closed & honest (red-flag markers).
    if _TRUST_BYPASS.search(text):
        v.append(Violation("E", "Untrusted at the boundary",
                           "The agent relies on a client-side check or tries to bypass the "
                           "orchestrator's checks. The boundary re-verifies everything; do not depend "
                           "on local trust.", "description"))

    # G — bounded resource use (declared-intent red flags; runtime cap enforces the rest).
    if _UNBOUNDED.search(text):
        v.append(Violation("G", "Bounded resource use",
                           "The agent describes unbounded/looping work. Bound its work so it can't run "
                           "away.", "description"))

    # H — registration & identity integrity.
    if agent_id:
        if agent_id.startswith(_RESERVED_PREFIXES) or agent_id in _RESERVED_STEMS \
                or any(agent_id.startswith(s + "-") or agent_id == s for s in _RESERVED_STEMS):
            v.append(Violation("H", "Registration & identity integrity",
                               f"The name '{agent_id}' is reserved or collides with a built-in agent. "
                               f"Choose a distinct name.", "agent_id"))
        elif db is not None and owner:
            try:
                from orchestrator.user_agents import get_user_agent
                existing = get_user_agent(db, agent_id)
                if existing is not None and existing.get("owner_user_id") != owner:
                    v.append(Violation("H", "Registration & identity integrity",
                                       "That name is already used by another user's agent.", "agent_id"))
            except Exception:
                pass

    # I — no secret / internal exfiltration.
    if _SECRET_EXFIL.search(text):
        v.append(Violation("I", "No secret or internal exfiltration",
                           "The agent describes reading secrets, environment, or platform internals. "
                           "It may not exfiltrate credentials or internals.", "description"))

    # J — declared, gated external egress.
    if isinstance(declared_egress, list):
        malformed = [e for e in declared_egress if not _URLISH.match(str(e).strip())]
        if malformed:
            v.append(Violation("J", "Declared, gated external egress",
                               f"These declared egress targets don't look like URLs/hosts: {malformed}. "
                               f"Declare concrete destinations.", "declared_egress"))

    # K — privacy by construction (no share/publish/transfer capability).
    share_tools = [t for t in declared_tools if _SHARE_TOOL.search(t)]
    if share_tools or _SHARE.search(text):
        v.append(Violation("K", "Privacy by construction",
                           "The agent includes a share/publish/transfer capability. User agents are "
                           "private; there is no in-product way to share one.",
                           "declared_tools" if share_tools else "description"))

    # L — version binding: always stamped on the result (structural).
    return AnalyzeResult(passed=(len(v) == 0), constitution_version=constitution_version,
                         violations=v)
