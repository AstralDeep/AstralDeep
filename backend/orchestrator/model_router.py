"""Device-capability-aware model router.

A cheap-first cascade in front of the LLM client factory. Each task starts on
the cheapest model tier that plausibly handles it (capped by the connecting
device), and escalates one tier on a low-confidence response. An optional
on-device lane is advertised for simple tasks on clients that carry a browser-AI
capability — but the server stays authoritative (the lane is a hint; the server
model is always resolved as the fallback).

Pure + deterministic; stdlib only. Flag ``FF_MODEL_ROUTER`` (default OFF) gates
the dispatch hook, which is additive + fail-open: with the flag off, or when no
per-tier models are configured, the caller keeps using its already-resolved
model unchanged.

Tier→model strings come from the ``MODEL_TIERS`` env (JSON, e.g.
``{"small":"…-8b","medium":"…-70b","large":"…-405b"}``); any tier without a
configured model resolves to the caller's default, so a single-model deployment
behaves exactly as today.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("orchestrator.model_router")

# Tier ladder (ascending capability / cost).
ONDEVICE, SMALL, MEDIUM, LARGE = 0, 1, 2, 3
_MAX_TIER = LARGE
_TIER_NAMES = {ONDEVICE: "ondevice", SMALL: "small", MEDIUM: "medium", LARGE: "large"}

#: Default task/feature → starting tier (cheap-first). Anything unlisted → MEDIUM.
_TASK_TIER = {
    "chat_title": SMALL, "narrative": SMALL, "summarize": SMALL,
    "summarize_text": SMALL, "classification": SMALL, "keyword": SMALL,
    "tool_dispatch": MEDIUM,
    "ui_designer": LARGE, "code_generation": LARGE, "agentic_creation": LARGE,
}

#: Hedging / refusal markers ⇒ low confidence ⇒ escalate one tier.
_HEDGE_MARKERS = (
    "i'm not sure", "i am not sure", "not certain", "cannot determine",
    "i cannot", "i can't", "unable to", "as an ai", "i don't have enough",
    "insufficient information", "it is unclear", "it's unclear",
)


def router_enabled() -> bool:
    """FF_MODEL_ROUTER feature flag (default OFF)."""
    return os.getenv("FF_MODEL_ROUTER", "false").strip().lower() in ("1", "true", "yes", "on")


def tier_name(tier: Optional[int]) -> str:
    return _TIER_NAMES.get(tier if tier is not None else MEDIUM, "medium")


def tier_for_task(feature: Optional[str], *, hint: Optional[int] = None) -> int:
    """The cheap-first starting tier for a task/feature."""
    if isinstance(hint, int) and ONDEVICE <= hint <= LARGE:
        return hint
    return _TASK_TIER.get(str(feature or "").strip().lower(), MEDIUM)


def device_cap_tier(device_type: Optional[str]) -> int:
    """The MAX server tier appropriate for a device. Constrained clients
    (watch/voice) cap low to favor latency/cost; mobile caps at MEDIUM; richer
    clients (tablet/browser/unknown) allow LARGE. Server models aren't
    device-bound, but a tiny client rarely needs a frontier model's verbosity."""
    dt = str(device_type or "").strip().lower()
    if dt in ("watch", "voice"):
        return SMALL
    if dt == "mobile":
        return MEDIUM
    return LARGE


def _has_browser_ai(device_caps: Any) -> bool:
    if device_caps is None:
        return False
    if isinstance(device_caps, dict):
        return bool(device_caps.get("has_browser_ai"))
    return bool(getattr(device_caps, "has_browser_ai", False))


def can_use_ondevice(device_caps: Any, feature: Optional[str]) -> bool:
    """On-device lane eligibility: the client advertises a browser-AI capability
    AND the task is simple (cheap-tier). The server still resolves a fallback."""
    return _has_browser_ai(device_caps) and tier_for_task(feature) <= SMALL


def escalate(tier: Optional[int]) -> Optional[int]:
    """The next-more-capable tier, or None when already at the top."""
    return tier + 1 if isinstance(tier, int) and tier < _MAX_TIER else None


def confidence_ok(text: Optional[str], *, min_chars: int = 1) -> bool:
    """Deterministic low-confidence heuristic for an escalation decision: an
    empty/too-short response, or one carrying a hedging/refusal marker, is NOT
    confident. Conservative — anything else is treated as confident."""
    s = (text or "").strip()
    if len(s) < max(1, min_chars):
        return False
    low = s.lower()
    return not any(m in low for m in _HEDGE_MARKERS)


_BAD_TIER_MAP_WARNED = False


def _warn_bad_tier_map_once(reason: str) -> None:
    """Log a malformed ``MODEL_TIERS`` exactly once per process (the map is
    re-read per call; a per-call WARNING would flood the log). The raw value
    is never logged."""
    global _BAD_TIER_MAP_WARNED
    if _BAD_TIER_MAP_WARNED:
        return
    _BAD_TIER_MAP_WARNED = True
    logger.warning("model_router: %s — ignoring MODEL_TIERS, "
                   "every tier falls back to the default model", reason)


def _env_tier_map() -> Dict[int, str]:
    raw = os.getenv("MODEL_TIERS")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        _warn_bad_tier_map_once("MODEL_TIERS is not valid JSON")
        return {}
    if not isinstance(data, dict):
        _warn_bad_tier_map_once("MODEL_TIERS must be a JSON object of tier -> model")
        return {}
    by_name = {n: t for t, n in _TIER_NAMES.items()}
    out: Dict[int, str] = {}
    for name, model in data.items():
        tier = by_name.get(str(name).strip().lower())
        if tier is not None and isinstance(model, str) and model.strip():
            out[tier] = model.strip()
    return out


def resolve_model(tier: int, default_model: str, *,
                  tier_map: Optional[Dict[int, str]] = None) -> str:
    """The concrete model string for a tier — the configured ``MODEL_TIERS``
    entry, else the caller's default. Always returns a usable model (fail-open)."""
    tm = tier_map if tier_map is not None else _env_tier_map()
    return tm.get(tier) or default_model


@dataclass(frozen=True)
class RouteDecision:
    tier: int
    model: str
    ondevice: bool = False


def route(feature: Optional[str], *, default_model: str,
          device_type: Optional[str] = None, device_caps: Any = None,
          tier_map: Optional[Dict[int, str]] = None) -> RouteDecision:
    """Cheap-first model selection: the task's starting tier, capped by the
    device, floored at SMALL (ONDEVICE is a client lane, not a server model),
    resolved to a concrete model (default when unmapped)."""
    start = max(SMALL, min(tier_for_task(feature), device_cap_tier(device_type)))
    return RouteDecision(
        tier=start,
        model=resolve_model(start, default_model, tier_map=tier_map),
        ondevice=can_use_ondevice(device_caps, feature),
    )
