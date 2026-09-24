"""The shared derivation seam (MachineTurnAuthority.derive) that scheduled jobs, parser
replays, and draft self-tests use for consent-derived root authority, plus
ChainBudget's per-turn hop/depth/wall-clock ceiling over nested delegation.
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

DEFAULT_MAX_CHAIN_HOPS = int(os.getenv("CHAIN_MAX_HOPS", "12"))
DEFAULT_CHAIN_WALL_CLOCK_S = float(os.getenv("CHAIN_WALL_CLOCK_SECONDS", "120"))

MACHINE_TURN_CLASSES = (
    "scheduled_job", "parser_replay", "draft_self_test", "persistent_assignment",
)

_MACHINE_SCOPE_KEY = "_machine_authority_scopes"
_STANDING_CONSENT_CLASSES = ("parser_replay", "draft_self_test")


def machine_scope_ceiling(session: Dict[str, Any]) -> Optional[frozenset[str]]:
    if "machine_class" not in session and _MACHINE_SCOPE_KEY not in session:
        return None
    from orchestrator.tool_permissions import VALID_SCOPES

    scopes = session.get(_MACHINE_SCOPE_KEY)
    if (
        session.get("machine_class") in _STANDING_CONSENT_CLASSES
        and _MACHINE_SCOPE_KEY in session
        and scopes is None
    ):
        return None
    if (
        session.get("machine_class") not in MACHINE_TURN_CLASSES
        or not isinstance(scopes, tuple)
        or any(not isinstance(scope, str) or scope not in VALID_SCOPES for scope in scopes)
    ):
        return frozenset()
    return frozenset(scopes)


def machine_session_binding(authority: MachineAuthority) -> Dict[str, Any]:
    from orchestrator.tool_permissions import VALID_SCOPES

    scopes = authority.allowed_scopes
    if (
        authority.turn_class not in MACHINE_TURN_CLASSES
        or not authority.user_id
        or not isinstance(authority.scope_ceiling_required, bool)
        or (not authority.scope_ceiling_required and (
            authority.turn_class not in _STANDING_CONSENT_CLASSES or scopes
        ))
        or (authority.agent_id is not None and (
            not isinstance(authority.agent_id, str) or not authority.agent_id
        ))
        or not isinstance(scopes, (list, tuple))
        or any(not isinstance(scope, str) or scope not in VALID_SCOPES for scope in scopes)
    ):
        raise ValueError("invalid machine authority binding")
    return {
        **authority.machine_claims(),
        "_raw_token": authority.access_token,
        _MACHINE_SCOPE_KEY: tuple(scopes) if authority.scope_ceiling_required else None,
        "_machine_authority_agent": authority.agent_id,
    }


@dataclass
class ChainBudget:
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
        if self.spent_hops >= self.max_hops:
            return "hop_budget_exhausted"
        if self.elapsed_s() >= self.wall_clock_s:
            return "wall_clock_exhausted"
        if self.parent is not None:
            return self.parent.exhausted()
        return None

    def charge(self, depth: int = 1) -> Optional[str]:
        if depth > self.max_depth:
            return "depth_exceeded"
        reason = self.exhausted()
        if reason is not None:
            return reason
        self.spent_hops += 1
        if self.parent is not None:
            parent_reason = self.parent.charge(depth)
            if parent_reason is not None:
                self.spent_hops -= 1
                return parent_reason
        return None

    def slice(self, *, max_hops: Optional[int] = None,
              wall_clock_s: Optional[float] = None) -> "ChainBudget":
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


@dataclass(frozen=True)
class MachineAuthority:
    access_token: str
    allowed_scopes: List[str]
    principal: str
    user_id: str
    consent_ref: str
    turn_class: str
    agent_id: Optional[str] = None
    scope_ceiling_required: bool = True

    def machine_claims(self) -> Dict[str, Any]:
        return {
            "sub": self.user_id,
            "machine_class": self.turn_class,
            "consent_ref": self.consent_ref,
        }


@dataclass(frozen=True)
class AuthoritySkip:
    reason: str
    detail: str = ""


class MachineTurnAuthority:
    def __init__(self, orchestrator, grants) -> None:
        self.orch = orchestrator
        self.grants = grants

    async def derive(
        self, *, user_id: str, agent_id: Optional[str],
        consented_scopes: Optional[List[str]], grant_id: Optional[str],
        turn_class: str,
    ) -> Union[MachineAuthority, AuthoritySkip]:
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
                current_names = await asyncio.to_thread(
                    self.orch.tool_permissions.get_enabled_scope_names,
                    user_id, agent_id)
                current = {scope: True for scope in current_names or []}
            except Exception:
                current = {}
            allowed_scopes = _intersect_scopes(
                list(consented_scopes or []), current or {})
            if consented_scopes and not allowed_scopes:
                self._log_skip(turn_class, user_id, "empty_scopes")
                return AuthoritySkip(
                    "empty_scopes",
                    "consented scopes no longer intersect the user's current grants")
        elif consented_scopes:
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
            agent_id=agent_id,
            scope_ceiling_required=(
                consented_scopes is not None or turn_class not in _STANDING_CONSENT_CLASSES
            ),
        )
        logger.info(
            "machine_turn.derived class=%s user=%s agent=%s consent_ref=%s scopes=%s",
            turn_class, user_id, agent_id, resolved_grant, allowed_scopes)
        return authority

    @staticmethod
    def _log_skip(turn_class: str, user_id: str, reason: str) -> None:
        logger.warning("machine_turn.skip class=%s user=%s reason=%s",
                       turn_class, user_id, reason)
