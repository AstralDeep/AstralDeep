"""Deterministic pre-action policy engine: evaluates ordered, data-driven rules
(POLICY_RULES) against a tool call's context and returns allow/deny/confirm/rewrite.
Consulted by orchestrator.py's gate stack.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger("orchestrator.policy")

ALLOW, DENY, CONFIRM, REWRITE = "allow", "deny", "confirm", "rewrite"
REQUIRE_TOKEN = "require_token"
_EFFECTS = (ALLOW, DENY, CONFIRM, REWRITE, REQUIRE_TOKEN)


@dataclass(frozen=True)
class PolicyDecision:
    effect: str = ALLOW
    reason: str = ""
    rule_id: str = ""
    args: Optional[Dict[str, Any]] = None


def policy_enabled() -> bool:
    return os.getenv("FF_POLICY_ENGINE", "false").strip().lower() in ("1", "true", "yes", "on")


def _glob(pattern: Any, value: Any) -> bool:
    return fnmatch.fnmatchcase(str("" if value is None else value), str(pattern))


def _matches(when: Dict[str, Any], ctx: Dict[str, Any]) -> bool:
    if not isinstance(when, dict):
        return False
    roles = {str(r).lower() for r in (ctx.get("roles") or [])}
    if "tool" in when and not _glob(when["tool"], ctx.get("tool")):
        return False
    if "agent" in when and not _glob(when["agent"], ctx.get("agent")):
        return False
    if "role" in when and str(when["role"]).lower() not in roles:
        return False
    if "not_role" in when and str(when["not_role"]).lower() in roles:
        return False
    if "args_regex" in when:
        try:
            if not re.search(str(when["args_regex"]),
                             json.dumps(ctx.get("args") or {}, default=str), re.IGNORECASE):
                return False
        except re.error:
            return False
    return True


def _apply_rewrite(spec: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(args)
    if isinstance(spec, dict):
        for name in (spec.get("redact_args") or []):
            if name in out:
                out[name] = "[redacted by policy]"
    return out


def evaluate_policy(rules: List[Dict[str, Any]], context: Dict[str, Any]) -> PolicyDecision:
    base_args = context.get("args") or {}
    args = dict(base_args)
    ctx = {**context, "args": args}
    for rule in (rules or []):
        if not isinstance(rule, dict):
            continue
        effect = str(rule.get("effect", "")).strip().lower()
        if effect not in _EFFECTS:
            continue
        try:
            if not _matches(rule.get("when") or {}, ctx):
                continue
        except Exception:
            logger.debug("policy: rule %r failed to evaluate — skipping",
                         rule.get("id"), exc_info=True)
            continue
        if effect == REWRITE:
            args = _apply_rewrite(rule.get("rewrite"), args)
            ctx = {**ctx, "args": args}
            continue
        return PolicyDecision(effect=effect, reason=str(rule.get("reason", "")),
                              rule_id=str(rule.get("id", "")),
                              args=(args if args != base_args else None))
    return PolicyDecision(effect=ALLOW, args=(args if args != base_args else None))


_SEED_RULES: List[Dict[str, Any]] = []


def load_rules() -> List[Dict[str, Any]]:
    raw = os.getenv("POLICY_RULES")
    if not raw:
        return list(_SEED_RULES)
    try:
        rules = json.loads(raw)
    except (ValueError, TypeError) as exc:
        logger.warning("POLICY_RULES ignored (%s); using seed rules", exc)
        return list(_SEED_RULES)
    return rules if isinstance(rules, list) else list(_SEED_RULES)
