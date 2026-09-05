"""Machine-turn root authority + per-turn chain budget (feature 056).

Two small, one-purpose pieces (mirroring ``offline_grant.py`` /
``concurrency_cap.py``):

``MachineTurnAuthority`` — the ONE shared derivation seam every machine-turn
class (``scheduled_job``, ``parser_replay``, ``draft_self_test``) calls to
obtain fresh, consent-derived, scope-narrowed root authority before running a
real-agent turn (FR-012). Fail-closed: missing/revoked/expired consent, a
failed mint, or an empty (consented ∩ current) scope set yields an
:class:`AuthoritySkip` the caller records and notifies (FR-013) — never a
silent unscoped run. Revocation is re-checked at derivation time, not only at
expiry (FR-006).

``ChainBudget`` — the global per-turn ceiling (cumulative delegation depth,
total hop count, wall clock) bounding ALL nested chaining in one user or
machine turn (FR-021). Distinct from — and composing with — the per-chain
depth bound (048) and the orchestrator's per-turn ``MAX_TURNS`` ReAct bound.
Sub-tasks receive a :meth:`ChainBudget.slice` whose charges also debit the
parent, so per-subtree budgets can never exceed the turn's global ceiling.

No token bytes are ever logged from this module (FR-028).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from orchestrator.delegation import DEFAULT_MAX_DELEGATION_DEPTH

logger = logging.getLogger("orchestrator.chain_authority")

# Conservative defaults (D9): a small hop budget and a wall clock consistent
# with the existing self-test/tool-timeout posture. Operator-tunable.
DEFAULT_MAX_CHAIN_HOPS = int(os.getenv("CHAIN_MAX_HOPS", "12"))
DEFAULT_CHAIN_WALL_CLOCK_S = float(os.getenv("CHAIN_WALL_CLOCK_SECONDS", "120"))

#: The defined machine-turn classes (FR-014). Any future class joins here.
MACHINE_TURN_CLASSES = (
    "scheduled_job", "parser_replay", "draft_self_test", "persistent_assignment",
)


# ---------------------------------------------------------------------------
# Chain budget (FR-021)
# ---------------------------------------------------------------------------

@dataclass
class ChainBudget:
    """Per-turn global ceiling over every chained hop and sub-task.

    ``charge(depth)`` debits one hop at the given delegation depth and returns
    ``None`` on success or a refusal reason string (``depth_exceeded`` /
    ``hop_budget_exhausted`` / ``wall_clock_exhausted``) — callers treat any
    reason as an audited, per-call ``budget_stop`` refusal, never an exception.
    """

    turn_id: str
    chat_id: Optional[str] = None
    max_depth: int = DEFAULT_MAX_DELEGATION_DEPTH
    max_hops: int = DEFAULT_MAX_CHAIN_HOPS
    wall_clock_s: float = DEFAULT_CHAIN_WALL_CLOCK_S
    spent_hops: int = 0
    started_at: float = field(default_factory=time.monotonic)
    parent: Optional["ChainBudget"] = None

    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    def exhausted(self) -> Optional[str]:
        """Why this budget can no longer admit a hop (or ``None``)."""
        if self.spent_hops >= self.max_hops:
            return "hop_budget_exhausted"
        if self.elapsed_s() >= self.wall_clock_s:
            return "wall_clock_exhausted"
        if self.parent is not None:
            return self.parent.exhausted()
        return None

    def charge(self, depth: int = 1) -> Optional[str]:
        """Debit one hop at ``depth``. Returns a refusal reason or ``None``."""
        if depth > self.max_depth:
            return "depth_exceeded"
        reason = self.exhausted()
        if reason is not None:
            return reason
        self.spent_hops += 1
        if self.parent is not None:
            parent_reason = self.parent.charge(depth)
            if parent_reason is not None:
                # The subtree slice admitted the hop but the global turn
                # budget refused — undo the local debit and refuse.
                self.spent_hops -= 1
                return parent_reason
        return None

    def slice(self, *, max_hops: Optional[int] = None,
              wall_clock_s: Optional[float] = None) -> "ChainBudget":
        """A per-subtree budget whose charges also debit this (parent) budget,
        so the global turn ceiling holds across all nesting (FR-020/FR-021)."""
        return ChainBudget(
            turn_id=self.turn_id,
            chat_id=self.chat_id,
            max_depth=self.max_depth,
            max_hops=min(max_hops if max_hops is not None else self.max_hops,
                         self.max_hops),
            wall_clock_s=min(
                wall_clock_s if wall_clock_s is not None else self.wall_clock_s,
                max(self.wall_clock_s - self.elapsed_s(), 0.0)),
            parent=self,
        )


# ---------------------------------------------------------------------------
# Machine-turn authority (FR-012/FR-013/FR-014/FR-015)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MachineAuthority:
    """A derived, per-run machine-turn root authority.

    ``access_token`` is the fresh consent-derived subject token the turn
    threads into dispatch; ``allowed_scopes`` is (consented ∩ current);
    ``principal`` is the defined machine audit identity ``machine:<class>``
    (FR-014). ``machine_claims()`` is the per-turn marker the audit layer
    resolves BEFORE its legacy fallback, attributing records to the owning
    human without ever carrying token bytes.
    """
    access_token: str
    allowed_scopes: List[str]
    principal: str
    user_id: str
    consent_ref: str
    turn_class: str

    def machine_claims(self) -> Dict[str, Any]:
        """Synthetic claims dict for audit attribution (NO token material)."""
        return {
            "sub": self.user_id,
            "machine_class": self.turn_class,
            "consent_ref": self.consent_ref,
        }


@dataclass(frozen=True)
class AuthoritySkip:
    """Fail-closed outcome: no derivable authority for this machine turn.

    ``reason`` ∈ {missing_consent, revoked_or_expired, mint_failed,
    token_endpoint_unconfigured, empty_scopes}. The caller records an
    authority-skip outcome and notifies the user actionably (FR-013) —
    real-agent dispatch must not proceed.
    """
    reason: str
    detail: str = ""


class MachineTurnAuthority:
    """One shared derivation all machine-turn classes inherit (FR-012).

    Consumers: ``scheduler/runner.run_job`` (scheduled runs),
    ``attachment_autoparse.auto_continue_after_go_live`` (parser replay),
    ``agentic_creation._self_test_draft`` (draft self-tests). Each obtains a
    fresh root per run; chains started inside the turn mint children off this
    root exactly as interactive chains do (FR-015 — one authority model, two
    roots).
    """

    def __init__(self, orchestrator, grants) -> None:
        self.orch = orchestrator
        self.grants = grants

    async def derive(
        self, *, user_id: str, agent_id: Optional[str],
        consented_scopes: Optional[List[str]], grant_id: Optional[str],
        turn_class: str,
    ) -> Union[MachineAuthority, AuthoritySkip]:
        """Derive fresh per-run root authority from stored consent.

        Steps (all reusing existing pieces, D7): resolve the grant (explicit
        id, else the user's latest valid grant), re-check revocation at
        derivation time (FR-006), mint a fresh access token, narrow to
        (consented ∩ the user's CURRENT grants). Fail-closed at every step.
        """
        if turn_class not in MACHINE_TURN_CLASSES:
            return AuthoritySkip("missing_consent",
                                 f"unknown machine-turn class: {turn_class}")

        resolved_grant = grant_id
        if turn_class == "persistent_assignment" and not resolved_grant:
            return AuthoritySkip("missing_consent", "assignment grant binding is required")
        if not resolved_grant:
            resolved_grant = await asyncio.to_thread(
                self.grants.latest_valid_for, user_id, agent_id)
        if not resolved_grant:
            self._log_skip(turn_class, user_id, "missing_consent")
            return AuthoritySkip("missing_consent",
                                 "no durable consent (offline grant) on record")

        # FR-006: revocation is part of authority derivation, not only expiry.
        if not await asyncio.to_thread(
            self.grants.is_valid,
            resolved_grant,
            user_id=user_id,
        ):
            self._log_skip(turn_class, user_id, "revoked_or_expired")
            return AuthoritySkip("revoked_or_expired",
                                 "consent revoked or expired; re-consent required")

        try:
            access_token = await self.grants.mint_access_token(
                resolved_grant,
                user_id=user_id,
            )
        except Exception as exc:
            # A missing/unusable IdP token endpoint is an OPERATOR problem:
            # name it, so the owner is not told to re-consent for something
            # re-consent cannot fix.
            from orchestrator.offline_grant import TokenEndpointUnconfigured

            if isinstance(exc, TokenEndpointUnconfigured):
                self._log_skip(turn_class, user_id, "token_endpoint_unconfigured")
                return AuthoritySkip("token_endpoint_unconfigured", str(exc)[:200])
            self._log_skip(turn_class, user_id, "mint_failed")
            return AuthoritySkip("mint_failed", str(exc)[:200])

        allowed_scopes: List[str] = []
        if agent_id:
            from scheduler.runner import _intersect_scopes
            try:
                # EFFECTIVE scopes, not a raw agent_scopes read. Two
                # dispatch-allowing paths write no scope row at all (feature
                # 040's safe-agent baseline, feature 013's per-(tool, kind)
                # grants), so get_agent_scopes would report all-False for a
                # user whose tools demonstrably run — collapsing the
                # intersection to empty and skipping every machine turn they
                # legitimately consented to. get_enabled_scope_names derives
                # per tool through is_tool_allowed, which is the same
                # predicate the dispatch gate applies, so containment here
                # can never exceed what the turn could actually do.
                current_names = await asyncio.to_thread(
                    self.orch.tool_permissions.get_enabled_scope_names,
                    user_id, agent_id)
                current = {scope: True for scope in current_names or []}
            except Exception:
                current = {}
            allowed_scopes = _intersect_scopes(
                list(consented_scopes or []), current or {})
            if consented_scopes and not allowed_scopes:
                # Consent named scopes but the user's CURRENT grants no longer
                # include any of them — never run wider than either (FR-012).
                self._log_skip(turn_class, user_id, "empty_scopes")
                return AuthoritySkip(
                    "empty_scopes",
                    "consented scopes no longer intersect the user's current grants")
        elif consented_scopes:
            # Agent-less machine turn: the run is an ordinary assistant turn
            # whose tool calls route across ALL of the user's eligible agents,
            # so "current" is the union of their effective scopes (the same
            # helper the consent capture used). A legacy job whose consent
            # list is empty keeps asserting nothing — never widened here.
            from orchestrator.tool_visibility import enabled_scope_union
            from scheduler.runner import _intersect_scopes
            try:
                current_names = await asyncio.to_thread(
                    enabled_scope_union, self.orch, user_id)
                current = {scope: True for scope in current_names or []}
            except Exception:
                current = {}
            allowed_scopes = _intersect_scopes(list(consented_scopes), current)
            if not allowed_scopes:
                self._log_skip(turn_class, user_id, "empty_scopes")
                return AuthoritySkip(
                    "empty_scopes",
                    "consented scopes no longer intersect the user's current grants")

        authority = MachineAuthority(
            access_token=access_token,
            allowed_scopes=allowed_scopes,
            principal=f"machine:{turn_class}",
            user_id=user_id,
            consent_ref=resolved_grant,
            turn_class=turn_class,
        )
        logger.info(
            "machine_turn.derived class=%s user=%s agent=%s consent_ref=%s scopes=%s",
            turn_class, user_id, agent_id, resolved_grant, allowed_scopes)
        return authority

    @staticmethod
    def _log_skip(turn_class: str, user_id: str, reason: str) -> None:
        logger.warning("machine_turn.skip class=%s user=%s reason=%s",
                       turn_class, user_id, reason)
