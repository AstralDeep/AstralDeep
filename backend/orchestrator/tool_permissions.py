"""
Tool Permission Manager — Scope-based agent authorization.

Provides scope-level control over which MCP tools each agent can
execute on behalf of a specific user. Six scopes map to Keycloak
client scopes on the astral-agent-service client — ``VALID_SCOPES``
below is the authoritative list:

  - tools:read    — Read/retrieve data, generate visualizations, analyze
  - tools:write   — Create, modify, delete data; post to external services
  - tools:search  — Query external APIs/databases for information
  - tools:system  — Access system resources (CPU, memory, disk)
  - tools:files   — Read files and mounted volumes (added in feature 027)
  - tools:execute — Run commands/shells (added in feature 039)

Persists to PostgreSQL via the agent_scopes table.

Absent a stored row the baseline is DENY, but two later features resolve
that absence to allow at check time rather than by writing rows: feature
040's owner-approved "safe" agents and the feature 057/058 owned-user-agent
default (see :meth:`ToolPermissionManager._resolve_tool_allowed`). An
explicit row — grant OR opt-out — always outranks both. So "all scopes are
DISABLED until granted" describes the storage default, not the effective
decision; read the effective decision through :meth:`is_tool_allowed` (or
:meth:`get_enabled_scope_names` for the scope-level view) and never from a
raw ``agent_scopes`` query.

Part of the RFC 8693 Delegated Authorization framework.
"""
import contextvars
import time
import logging
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)

logger = logging.getLogger("ToolPermissions")

_TURN_PERMISSION_MEMO: contextvars.ContextVar = contextvars.ContextVar(
    "turn_permission_memo", default=None
)


@contextmanager
def turn_permission_memo():
    """Memoize is_tool_allowed decisions for the duration of one chat turn.

    While active, :meth:`ToolPermissionManager.is_tool_allowed` caches its
    final decision keyed ``(user_id, agent_id, tool_name, required_kind)``,
    so a tool checked repeatedly within a single turn resolves against the
    database once (feature 052, FR-019). The memo lives in a contextvar:
    it propagates into ``asyncio`` tasks and ``asyncio.to_thread`` workers
    spawned inside the block, and two concurrent turns can never observe
    each other's decisions. There is no cross-turn reuse — the memo dies
    with the block, so a revocation is visible on the next message. When
    no memo is active, behavior is exactly the per-call resolution.
    """
    token = _TURN_PERMISSION_MEMO.set({})
    try:
        yield
    finally:
        _TURN_PERMISSION_MEMO.reset(token)

# The canonical scopes aligned with the Keycloak astral-agent-service client.
# "tools:files" (general agent's file/volume readers) was registered by tools
# but missing here, leaving those tools without any permission control surface
# (027 click-through finding) — agent_scopes has no scope CHECK constraint, so
# adding it is purely additive.
# "tools:execute" (feature 039) governs command-execution tools — the Windows
# coding agent's run_command/run_shell declare it. Additive for the same reason.
VALID_SCOPES = ["tools:read", "tools:write", "tools:search", "tools:system",
                "tools:files", "tools:execute"]


def resolve_effective_tool_permissions(
    tool_scope_map: Mapping[str, str],
    *,
    owner_id: str,
    agent_id: str,
    scope_rows: Iterable[object],
    override_rows: Iterable[object],
    safe_default: bool = False,
) -> Dict[str, Dict[str, bool]]:
    """Resolve a detached, exactly owner-scoped permission snapshot.

    This is the canonical pure form of the permission-picker precedence used
    by :meth:`ToolPermissionManager.get_effective_tool_permissions`. Callers
    that already loaded typed Plane rows can reuse it without issuing another
    query, while runtime and UI policy retain identical ordering.

    Every row is re-fenced to ``(owner_id, agent_id)`` before it can affect a
    decision. A mixed-owner or mixed-agent snapshot raises instead of leaking
    another principal's grant into the result.
    """
    if not isinstance(owner_id, str) or not owner_id:
        raise ValueError("owner_id must be a non-empty string")
    if not isinstance(agent_id, str) or not agent_id:
        raise ValueError("agent_id must be a non-empty string")

    explicit_scope: Dict[str, bool] = {}
    for row in scope_rows:
        if (
            getattr(row, "owner_id", None) != owner_id
            or getattr(row, "agent_id", None) != agent_id
        ):
            raise RuntimeError("tool permission scope snapshot crossed its owner fence")
        scope = getattr(row, "scope", None)
        enabled = getattr(row, "enabled", None)
        if not isinstance(scope, str) or not scope or not isinstance(enabled, bool):
            raise RuntimeError("tool permission scope snapshot is invalid")
        explicit_scope[scope] = enabled

    kind_lookup: Dict[str, Dict[str, bool]] = {}
    legacy_disabled: set[str] = set()
    for row in override_rows:
        if (
            getattr(row, "owner_id", None) != owner_id
            or getattr(row, "agent_id", None) != agent_id
        ):
            raise RuntimeError("tool override snapshot crossed its owner fence")
        tool_name = getattr(row, "tool_name", None)
        permission_kind = getattr(row, "permission_kind", None)
        enabled = getattr(row, "enabled", None)
        if (
            not isinstance(tool_name, str)
            or not tool_name
            or (permission_kind is not None and not isinstance(permission_kind, str))
            or not isinstance(enabled, bool)
        ):
            raise RuntimeError("tool override snapshot is invalid")
        if permission_kind is None:
            if not enabled:
                legacy_disabled.add(tool_name)
        else:
            kind_lookup.setdefault(tool_name, {})[permission_kind] = enabled

    result: Dict[str, Dict[str, bool]] = {}
    for tool_name, required_scope in tool_scope_map.items():
        if tool_name in legacy_disabled:
            effective = False
        elif required_scope in kind_lookup.get(tool_name, {}):
            effective = kind_lookup[tool_name][required_scope]
        elif required_scope in explicit_scope:
            effective = explicit_scope[required_scope]
        else:
            effective = bool(safe_default)
        result[tool_name] = {required_scope: effective}
    return result


class ToolPermissionManager:
    """Manages per-user, per-agent scope-based permissions backed by PostgreSQL.

    Structure (logical) — one entry per scope in ``VALID_SCOPES``:
        {
            "<user_id>": {
                "<agent_id>": {
                    "tools:read": true/false,
                    "tools:write": true/false,
                    "tools:search": true/false,
                    "tools:system": true/false,
                    "tools:files": true/false,
                    "tools:execute": true/false,
                }
            }
        }

    Storage default: no row, which resolves to DENY unless a check-time
    baseline (safe agent, owned user agent) says otherwise — see the module
    docstring and :meth:`_resolve_tool_allowed`.
    """

    def __init__(
        self,
        db=None,
        data_dir: str = None,
        database_url: str = None,
        *,
        plane_runtime=None,
        plane_repositories=None,
        plane_repository=None,
        agent_repository=None,
        user_agent_registry=None,
    ):
        if database_url is not None:
            raise ValueError(
                "ToolPermissionManager no longer constructs database runtimes; "
                "inject the application Plane runtime"
            )
        if db is None and plane_runtime is None:
            raise ValueError("ToolPermissionManager requires the application Plane runtime")
        self.db = db
        self.user_agent_registry = user_agent_registry

        self.data_dir = data_dir
        repository, runtime = repository_from(
            "tool_policy_state",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=self.db,
        )
        self._policy = PlaneRepositoryContext(
            repository=plane_repository or repository,
            plane_runtime=runtime,
            legacy_database=self.db,
        )
        agents, agents_runtime = repository_from(
            "agents",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=self.db,
        )
        self._agents = PlaneRepositoryContext(
            repository=agent_repository or agents,
            plane_runtime=agents_runtime,
            legacy_database=self.db,
        )
        # In-memory tool→scope mapping populated by orchestrator on agent registration
        # Structure: { agent_id: { tool_name: scope_string } }
        self._tool_scope_map: Dict[str, Dict[str, str]] = {}
        # Feature 040: short-TTL cache of agent_trust.is_safe so the
        # safe-baseline check stays off the per-call DB hot path.
        self._safe_cache: Dict[str, tuple] = {}
        self._reject_legacy_json()

    def _reject_legacy_json(self) -> None:
        """Fail closed when preserved pre-Plane permission state is present.

        Deep no longer owns a data importer for this durable state.  In
        particular, startup must not parse, rename, or otherwise mutate the
        operator's only legacy copy.  Recovery belongs at an explicit,
        evidence-backed Plane boundary.
        """
        if not self.data_dir:
            return
        json_path = Path(self.data_dir) / "tool_permissions.json"
        if not json_path.is_file() or json_path.stat().st_size == 0:
            return
        raise RuntimeError(
            "legacy tool_permissions.json is preserved and cannot be imported "
            "by AstralDeep; complete an evidence-backed AstralPlane recovery "
            "before startup"
        )

    # ── Tool→Scope Mapping ──────────────────────────────────────────────

    def register_tool_scopes(self, agent_id: str, tool_scope_map: Dict[str, str]):
        """Register the tool→scope mapping for an agent (called on agent registration).

        Args:
            agent_id: The agent's identifier.
            tool_scope_map: Dict of {tool_name: scope} e.g. {"modify_data": "tools:write"}.
        """
        self._tool_scope_map[agent_id] = tool_scope_map
        logger.info(f"Registered tool scopes for agent={agent_id}: {len(tool_scope_map)} tools")

    def get_tool_scope(self, agent_id: str, tool_name: str) -> str:
        """Get the required scope for a specific tool.

        Returns the scope string or "tools:read" as default.
        """
        agent_map = self._tool_scope_map.get(agent_id, {})
        return agent_map.get(tool_name, "tools:read")

    def get_tool_scope_map(self, agent_id: str) -> Dict[str, str]:
        """Get the full tool→scope mapping for an agent."""
        return self._tool_scope_map.get(agent_id, {})

    # ── Scope Queries ───────────────────────────────────────────────────

    def get_agent_scopes(self, user_id: str, agent_id: str) -> Dict[str, bool]:
        """Get scope permissions for a specific user and agent.

        Returns a dict of {scope: enabled} for all 4 scopes.
        Default: all scopes disabled (False).
        """
        rows = self._policy.call(
            self._policy.repository.list_scopes,
            owner_id=user_id,
            agent_id=agent_id,
        )
        stored = {row.scope: row.enabled for row in rows}
        # Fill in defaults for any missing scopes
        return {scope: stored.get(scope, False) for scope in VALID_SCOPES}

    def has_any_enabled_scope(self, user_id: str) -> bool:
        """Return True when the user has at least one enabled scope on any agent.

        Distinguishes a never-configured account (no rows — feature 030 shows
        the enable affordance) from one whose user deliberately disabled
        agents (rows exist, possibly all disabled — affordance still shown)
        or enabled some (no affordance).
        """
        return self._policy.call(
            self._policy.repository.has_any_enabled_scope,
            owner_id=user_id,
        )

    def list_disabled_agents(self, user_id: str) -> tuple[str, ...]:
        """Return the user's explicitly disabled agent identifiers.

        This is the application-facing boundary for the Plane-owned
        ``disabled_agents`` preference.  Callers must not reach through
        ``HistoryManager`` or the retiring Deep database facade to inspect it.
        """
        return self._policy.call(
            self._policy.repository.list_disabled_agents,
            owner_id=user_id,
        )

    def is_agent_disabled(self, user_id: str, agent_id: str) -> bool:
        """Return whether ``agent_id`` is explicitly disabled for ``user_id``."""
        return agent_id in self.list_disabled_agents(user_id)

    def set_agent_disabled(
        self,
        user_id: str,
        agent_id: str,
        disabled: bool,
    ) -> bool:
        """Persist an explicit per-user agent enable/disable preference.

        Returns ``True`` only when the stored preference changed.  Plane owns
        the row lock and JSON preference update in the caller transaction.
        """
        return self._policy.call(
            self._policy.repository.set_agent_disabled,
            owner_id=user_id,
            agent_id=agent_id,
            disabled=disabled,
            updated_at=int(time.time() * 1000),
        )

    def get_tool_selection(
        self,
        user_id: str,
        agent_id: str,
    ) -> Optional[List[str]]:
        """Return the saved tool-picker subset, or ``None`` when unrestricted."""
        selected = self._policy.call(
            self._policy.repository.get_tool_selection,
            owner_id=user_id,
            agent_id=agent_id,
        )
        return None if selected is None else list(selected)

    def set_tool_selection(
        self,
        user_id: str,
        agent_id: str,
        selected_tools: List[str],
    ) -> List[str]:
        """Persist the validated, non-empty tool-picker subset."""
        selected = self._policy.call(
            self._policy.repository.set_tool_selection,
            owner_id=user_id,
            agent_id=agent_id,
            selected_tools=selected_tools,
            updated_at=int(time.time() * 1000),
        )
        return list(selected)

    def clear_tool_selection(self, user_id: str, agent_id: str) -> bool:
        """Remove the saved subset so normal permission resolution applies."""
        return self._policy.call(
            self._policy.repository.clear_tool_selection,
            owner_id=user_id,
            agent_id=agent_id,
            updated_at=int(time.time() * 1000),
        )

    def scopes_required_by_tools(self, agent_id: str, exclude=("tools:write",)) -> List[str]:
        """Scopes the agent's registered tools actually use, minus ``exclude``.

        Drives the feature-030 consent enable: the grant is attenuated to what
        the agent's tool→scope map declares (Constitution VII), defaulting to
        ``tools:read`` for agents that registered no explicit map, and never
        includes excluded scopes (``tools:write`` by default).
        """
        used = set(self._tool_scope_map.get(agent_id, {}).values()) or {"tools:read"}
        return sorted(s for s in used if s in VALID_SCOPES and s not in exclude)

    def is_scope_enabled(self, user_id: str, agent_id: str, scope: str) -> bool:
        """Check if a specific scope is enabled for the user/agent combination.

        Returns False if no record exists (default = disabled).
        """
        rows = self._policy.call(
            self._policy.repository.list_scopes,
            owner_id=user_id,
            agent_id=agent_id,
        )
        return next((row.enabled for row in rows if row.scope == scope), False)

    def set_agent_scopes(self, user_id: str, agent_id: str, scopes: Dict[str, bool]):
        """Set scope permissions for a user/agent combination.

        Args:
            user_id: The user's ID.
            agent_id: The agent's ID.
            scopes: Dict of {scope: enabled} for each scope to set.
        """
        now = int(time.time() * 1000)
        valid: Dict[str, bool] = {}
        for scope, enabled in scopes.items():
            if scope not in VALID_SCOPES:
                logger.warning(f"Ignoring invalid scope: {scope}")
                continue
            valid[scope] = bool(enabled)
        self._policy.call(
            self._policy.repository.set_scopes,
            owner_id=user_id,
            agent_id=agent_id,
            scopes=valid,
            updated_at=now,
        )
        logger.info(
            f"Scopes updated: user={user_id} agent={agent_id} "
            f"scopes={scopes}"
        )

    # ── Per-Tool Overrides ──────────────────────────────────────────────

    def get_tool_overrides(self, user_id: str, agent_id: str) -> Dict[str, bool]:
        """Get per-tool enable/disable overrides for a user/agent.

        Returns a dict of {tool_name: enabled} only for tools that have
        an explicit override. Tools not in this dict follow scope default.
        """
        rows = self._policy.call(
            self._policy.repository.list_overrides,
            owner_id=user_id,
            agent_id=agent_id,
        )
        return {row.tool_name: row.enabled for row in rows}

    def set_tool_overrides(self, user_id: str, agent_id: str, overrides: Dict[str, bool]):
        """Set per-tool enable/disable overrides.

        Args:
            overrides: Dict of {tool_name: enabled}. Only tools explicitly
                       toggled off need entries — scope-enabled tools default to on.
        """
        now = int(time.time() * 1000)
        for tool_name, enabled in overrides.items():
            if enabled:
                # Remove only the legacy NULL-kind row so Feature 013 per-(tool, kind)
                # rows are preserved.
                self._policy.call(
                    self._policy.repository.clear_tool_override,
                    owner_id=user_id,
                    agent_id=agent_id,
                    tool_name=tool_name,
                    permission_kind=None,
                )
            else:
                # Match the 4-col expression-based unique index
                # (user_id, agent_id, tool_name, COALESCE(permission_kind, '')).
                self._policy.call(
                    self._policy.repository.set_tool_override,
                    owner_id=user_id,
                    agent_id=agent_id,
                    tool_name=tool_name,
                    permission_kind=None,
                    enabled=False,
                    updated_at=now,
                )
        logger.info(
            f"Tool overrides updated: user={user_id} agent={agent_id} "
            f"overrides={overrides}"
        )

    # ── Tool-Level Authorization (used by orchestrator) ─────────────────

    def is_tool_allowed(self, user_id: str, agent_id: str, tool_name: str) -> bool:
        """Check if a specific tool is allowed for this user/agent.

        Resolution order (Feature 013 / FR-013):
          1. If a per-(tool, permission_kind) row exists for the tool's
             required kind, that explicit row decides — return its value.
          2. Else, if a legacy tool-wide override row (permission_kind IS
             NULL) exists and is False, the tool is blocked.
          3. Else, fall back to the agent-wide scope (`agent_scopes`).

        Inside an active :func:`turn_permission_memo` block, the final
        decision is memoized per ``(user_id, agent_id, tool_name, kind)``
        for the rest of the turn.
        """
        required_scope = self.get_tool_scope(agent_id, tool_name)
        memo = _TURN_PERMISSION_MEMO.get()
        key = (user_id, agent_id, tool_name, required_scope)
        if memo is not None and key in memo:
            return memo[key]
        allowed = self._resolve_tool_allowed(user_id, agent_id, tool_name, required_scope)
        if memo is not None:
            memo[key] = allowed
        return allowed

    def _resolve_tool_allowed(
        self, user_id: str, agent_id: str, tool_name: str, required_scope: str
    ) -> bool:
        """Resolve a tool decision against the database (unmemoized)."""
        # 0. Feature 057 — user-agent owner isolation (FR-016/019, defense in
        # depth for both dispatch and tool-list build). A user-created agent is
        # private to its owner; a non-owner can neither list nor dispatch its
        # tools, INDEPENDENT of any (stray) scope row. can_user_use_agent returns
        # True for every non-user-agent, so built-ins/public are unaffected.
        try:
            user_agent = self._agents.call(
                self._agents.repository.get_agent_for_administration,
                agent_id=agent_id,
            )
            if user_agent is not None and (
                user_agent.deleted_at is not None or user_agent.owner_id != user_id
            ):
                return False
        except Exception:
            logger.debug(
                "user-agent isolation check failed (allowing normal resolution)",
                exc_info=True,
            )
        # 1. Per-(tool, kind) row takes priority
        override_rows = self._policy.call(
            self._policy.repository.list_overrides,
            owner_id=user_id,
            agent_id=agent_id,
        )
        kind_row = next(
            (
                row
                for row in override_rows
                if row.tool_name == tool_name
                and row.permission_kind == required_scope
            ),
            None,
        )
        if kind_row is not None:
            return kind_row.enabled
        # 2. Legacy tool-wide override (permission_kind IS NULL) can still block
        legacy_row = next(
            (
                row
                for row in override_rows
                if row.tool_name == tool_name and row.permission_kind is None
            ),
            None,
        )
        if legacy_row is not None and not legacy_row.enabled:
            return False
        # 3. Fall back to the agent-wide scope. Feature 040: an owner-approved
        # "safe" agent flips this baseline from deny→allow — but ONLY when the
        # user has no explicit scope row. An explicit grant OR opt-out (a stored
        # agent_scopes row, including enabled=False) always wins. Hard
        # security-flag blocks are an independent upstream gate (orchestrator
        # dispatch), unaffected here.
        scope_rows = self._policy.call(
            self._policy.repository.list_scopes,
            owner_id=user_id,
            agent_id=agent_id,
        )
        scope_row = next((row for row in scope_rows if row.scope == required_scope), None)
        if scope_row is not None:
            return scope_row.enabled
        if required_scope not in VALID_SCOPES:
            # A tool declaring an unknown scope has no grantable permission
            # surface (registration warns and says as much) and no scope the
            # delegation mint could assert — allowing it here would admit a
            # call the token can never carry authority for.
            return False
        if self._is_safe_agent(agent_id) and self._safe_flip_allowed(agent_id):
            return True
        # 4. Feature 057/058 — a user-created agent's OWNER may use its tools by
        # default. They authored it through the mandatory Analyze gate, it is
        # private-by-construction, and there is NO permission UI to grant a scope
        # on it — so requiring an explicit grant would make an authored agent
        # unusable by the very person who created it. Ownership was already proven
        # at step 0 (can_user_use_agent). An explicit opt-out (a scope_row with
        # enabled=False, handled above) still wins, the required scope must be
        # valid (checked above so the delegation mint can assert it), and hard
        # security flags remain an independent upstream gate.
        if self._is_owned_user_agent(user_id, agent_id):
            return True
        return False

    def _is_owned_user_agent(self, user_id: str, agent_id: str) -> bool:
        """Whether ``agent_id`` is a user-created agent owned by ``user_id``
        (features 057/058). Only a genuine user-agent row with a matching owner
        returns True; built-ins/public/drafts return False, so their baseline is
        unchanged. Fails closed on any error."""
        try:
            record = self._agents.call(
                self._agents.repository.get_agent_for_administration,
                agent_id=agent_id,
            )
        except Exception:
            logger.debug("owned-user-agent check failed", exc_info=True)
            return False
        return bool(
            record is not None
            and record.deleted_at is None
            and record.owner_id == user_id
        )

    def _is_safe_agent(self, agent_id: str) -> bool:
        """Whether ``agent_id`` is an owner-approved 'safe' agent (feature 040).

        Gated by ``FF_SAFE_AGENTS``; cached briefly (30s) to avoid a DB hit on
        the per-call permission path. Fails closed (returns False) on any error,
        so a lookup failure can never widen access.
        """
        try:
            from shared.feature_flags import flags
            if not flags.is_enabled("safe_agents"):
                return False
        except Exception:
            return False
        import time
        now = time.time()
        cached = self._safe_cache.get(agent_id)
        if cached is not None and cached[1] > now:
            return cached[0]
        try:
            trust = self._agents.call(
                self._agents.repository.get_trust,
                agent_id=agent_id,
            )
            val = bool(trust is not None and trust.is_safe)
        except Exception:
            val = False
        self._safe_cache[agent_id] = (val, now + 30.0)
        return val

    def _safe_flip_allowed(self, agent_id: str) -> bool:
        """Whether a safe agent's deny→allow baseline flip may apply for any user.

        Feature 040's safe marker auto-allows tools for ALL users. To stop an
        owner from fleet-exposing a PRIVATE agent by marking it safe, only honor
        the flip for a PUBLIC agent — or one with no ownership record at all
        (built-in/system/test agents). A private agent (ownership row with
        ``is_public = False``) still requires an explicit grant. Cached 30s like
        the safe lookup; fails closed on a lookup error.
        """
        import time
        now = time.time()
        cache = getattr(self, "_public_flip_cache", None)
        if cache is None:
            cache = self._public_flip_cache = {}
        cached = cache.get(agent_id)
        if cached is not None and cached[1] > now:
            return cached[0]
        try:
            ownership = self._agents.call(
                self._agents.repository.get_ownership,
                agent_id=agent_id,
            )
        except Exception:
            return False  # fail closed on lookup error (do not cache)
        val = True if ownership is None else ownership.is_public
        cache[agent_id] = (val, now + 30.0)
        return val

    def set_skill_enabled(self, user_id: str, agent_id: str, tool_name: str,
                          enabled: bool) -> None:
        """Toggle a skill through the row that actually wins (027 fix).

        ``is_tool_allowed`` resolves the per-(tool, permission_kind) row FIRST,
        so writing the legacy NULL-kind row (``set_tool_overrides``) is a
        silent no-op whenever a kind row exists — and every Agents &
        permissions save creates those rows. Write the per-kind row for the
        tool's required scope and clear any legacy NULL-kind row so the two
        layers cannot disagree.
        """
        required_scope = self.get_tool_scope(agent_id, tool_name)
        if required_scope in VALID_SCOPES:
            self.set_tool_permission(user_id, agent_id, tool_name, required_scope, enabled)
            self._policy.call(
                self._policy.repository.clear_tool_override,
                owner_id=user_id,
                agent_id=agent_id,
                tool_name=tool_name,
                permission_kind=None,
            )
        else:
            # Unknown/legacy scope — the tool-wide row is all that exists.
            self.set_tool_overrides(user_id, agent_id, {tool_name: enabled})

    # ── Per-Tool Permissions (Feature 013) ──────────────────────────────

    def get_effective_tool_permissions(
        self, user_id: str, agent_id: str, safe_default: Optional[bool] = None
    ) -> Dict[str, Dict[str, bool]]:
        """Return the resolved per-tool, per-permission-kind permission map.

        Output shape:
            { tool_name: { permission_kind: enabled } }

        Only the kinds that apply to each tool (i.e., the tool's required
        scope from the agent's tool→scope map) are included — satisfies
        FR-014 (no greyed-out toggles for inapplicable kinds).

        Resolution per tool mirrors :meth:`is_tool_allowed` so the picker
        matches the runtime gate:
          - A legacy tool-wide override (permission_kind IS NULL, enabled=False)
            forces the kind to False.
          - Else, if a per-kind row exists, use that boolean.
          - Else, if an explicit agent-wide scope row exists, use it.
          - Else fall back to ``safe_default`` — feature 040's deny→allow flip
            for a safe + public agent — so a fresh user sees a safe agent's
            tools ON by default instead of contradicting the runtime gate.

        ``safe_default`` is computed from the cached safe/ownership lookups when
        not supplied; hot callers (the agents surface) pass it from data they
        already read so this stays within their DB round-trip budget.
        """
        scope_map = self._tool_scope_map.get(agent_id, {})
        if not scope_map:
            return {}
        if safe_default is None:
            safe_default = self._is_safe_agent(agent_id) and self._safe_flip_allowed(agent_id)
        # Raw scope rows so an explicit opt-out (enabled=False) is distinguished
        # from an absent scope, which alone falls through to safe_default.
        scope_rows = self._policy.call(
            self._policy.repository.list_scopes,
            owner_id=user_id,
            agent_id=agent_id,
        )
        # Pull per-kind AND legacy override rows in one query, split in Python.
        override_rows = self._policy.call(
            self._policy.repository.list_overrides,
            owner_id=user_id,
            agent_id=agent_id,
        )
        return resolve_effective_tool_permissions(
            scope_map,
            owner_id=user_id,
            agent_id=agent_id,
            scope_rows=scope_rows,
            override_rows=override_rows,
            safe_default=bool(safe_default),
        )

    def set_tool_permission(
        self,
        user_id: str,
        agent_id: str,
        tool_name: str,
        permission_kind: str,
        enabled: bool,
    ) -> None:
        """Set a single per-tool, per-permission-kind permission (Feature 013).

        Args:
            user_id: The user's identifier.
            agent_id: The agent's identifier.
            tool_name: The tool's identifier (must exist on the agent).
            permission_kind: One of VALID_SCOPES.
            enabled: True to allow, False to block.

        Raises:
            ValueError: If ``permission_kind`` is not a valid scope.
        """
        if permission_kind not in VALID_SCOPES:
            raise ValueError(
                f"Invalid permission_kind {permission_kind!r}; must be one of {VALID_SCOPES}"
            )
        now = int(time.time() * 1000)
        # Use the (user_id, agent_id, tool_name, COALESCE(permission_kind, ''))
        # unique index added by the migration. ON CONFLICT requires explicit
        # constraint targeting; use the index-based form.
        self._policy.call(
            self._policy.repository.set_tool_override,
            owner_id=user_id,
            agent_id=agent_id,
            tool_name=tool_name,
            permission_kind=permission_kind,
            enabled=bool(enabled),
            updated_at=now,
        )
        logger.info(
            "Per-tool permission updated: user=%s agent=%s tool=%s kind=%s enabled=%s",
            user_id,
            agent_id,
            tool_name,
            permission_kind,
            bool(enabled),
        )

    def backfill_per_tool_rows(self, user_id: str, agent_id: str) -> int:
        """Idempotent 1:1 carry-forward from agent_scopes to per-tool rows (FR-015).

        For every tool the agent exposes, if a per-(tool, kind) row does
        not yet exist AND the user has an explicit ``agent_scopes`` row for
        that tool's required kind, insert one carrying that row's value.
        Returns the number of rows inserted (zero on subsequent runs).

        Safe to call repeatedly — subsequent calls are no-ops because
        rows already exist. Called from the per-tool permissions
        endpoints on first read so users don't have to re-toggle.

        **Only carries forward what actually exists.** A missing scope row is
        not a stored ``False`` — it is the absence of a decision, and two
        dispatch-allowing baselines resolve it at check time: feature 040's
        safe-agent deny→allow flip and the 057/058 owned-user-agent default
        (both in :meth:`_resolve_tool_allowed`, step 3/4). Materialising
        ``enabled=False`` rows for those tools would hand step 1 an explicit
        row that outranks both baselines *permanently*, so merely opening the
        permissions screen would silently strip every built-in agent's tools
        from a user who had granted nothing and revoked nothing. Skipping the
        write leaves the baseline live and changes no effective decision for
        users who do have scope rows — for them an explicit per-tool row and
        the scope row it was copied from resolve identically.
        """
        scope_map = self._tool_scope_map.get(agent_id, {})
        if not scope_map:
            return 0
        # Raw rows, NOT get_agent_scopes() — that helper fills absent scopes
        # with False, which is exactly the distinction this method turns on.
        now = int(time.time() * 1000)
        inserted = 0
        with self._policy.transaction() as transaction:
            explicit_scopes = {
                row.scope: row.enabled
                for row in self._policy.repository.list_scopes(
                    transaction,
                    owner_id=user_id,
                    agent_id=agent_id,
                )
            }
            if not explicit_scopes:
                return 0
            for tool_name, required_scope in scope_map.items():
                if required_scope not in explicit_scopes:
                    # No agent-wide decision to carry forward — leave the tool to
                    # the resolution baselines rather than freezing a denial.
                    continue
                if self._policy.repository.create_tool_override_if_absent(
                    transaction,
                    owner_id=user_id,
                    agent_id=agent_id,
                    tool_name=tool_name,
                    permission_kind=required_scope,
                    enabled=explicit_scopes[required_scope],
                    updated_at=now,
                ):
                    inserted += 1
        if inserted:
            logger.info(
                "Backfilled %d per-tool permission rows for user=%s agent=%s",
                inserted,
                user_id,
                agent_id,
            )
        return inserted

    def get_allowed_tools(
        self, user_id: str, agent_id: str, available_tools: list
    ) -> list:
        """Return the subset of available tools that the user has allowed.

        Uses :meth:`is_tool_allowed` per-tool so per-(tool, kind) rows
        added in Feature 013 are honored consistently with the
        orchestrator's per-turn filter loop.
        """
        return [
            tool for tool in available_tools
            if self.is_tool_allowed(user_id, agent_id, tool)
        ]

    def get_enabled_scope_names(self, user_id: str, agent_id: str) -> List[str]:
        """Return the scope names a delegation token for this pair may assert.

        This is the scope authority behind every RFC 8693 mint (the flat
        exchange and the recursive-hop child mint both read it), so it must
        agree with the gate that actually admits a tool call: a scope is
        asserted exactly when the user can run at least one tool that requires
        it, per :meth:`is_tool_allowed`. A raw read of ``agent_scopes`` does
        NOT satisfy that — two dispatch-allowing paths write no scope row at
        all (feature 040's safe-agent baseline, and feature 013's
        per-(tool, kind) ``tool_overrides`` grants) — and a token minted from
        such a read carries an empty ``scope`` parameter, which Keycloak
        rejects with ``invalid_scope``, fail-closing every tool call in
        production posture.

        Deriving per tool also keeps the token from over-asserting: a scope
        whose tools the user explicitly opted out of is not minted, so the
        attenuation lives in the token itself and not only in the gate.
        Explicit *enabled* scope rows are always asserted — the user granted
        them agent-wide, whether or not a registered tool currently maps to
        them.
        """
        rows = self._policy.call(
            self._policy.repository.list_scopes,
            owner_id=user_id,
            agent_id=agent_id,
        )
        enabled = {
            row.scope for row in rows
            if row.enabled and row.scope in VALID_SCOPES
        }
        scope_map = self._tool_scope_map.get(agent_id, {})
        if scope_map:
            for tool_name, required_scope in scope_map.items():
                if required_scope in enabled or required_scope not in VALID_SCOPES:
                    continue
                if self.is_tool_allowed(user_id, agent_id, tool_name):
                    enabled.add(required_scope)
        elif self._is_safe_agent(agent_id) and self._safe_flip_allowed(agent_id):
            # Agent registered no tool→scope map (not connected this boot):
            # dispatch resolves its tools at the tools:read default, so the
            # token has to carry the same.
            enabled.add("tools:read")
        return [scope for scope in VALID_SCOPES if scope in enabled]

    # ── Backward Compatibility ──────────────────────────────────────────

    def get_effective_permissions(
        self, user_id: str, agent_id: str, available_tools: list
    ) -> Dict[str, bool]:
        """Get effective permissions for all tools (per-tool, per-kind aware).

        Returns ``{tool_name: allowed}`` using :meth:`is_tool_allowed` so
        per-(tool, kind) rows added in Feature 013 are honored.
        """
        return {
            tool: self.is_tool_allowed(user_id, agent_id, tool)
            for tool in available_tools
        }

    # ── Cleanup ─────────────────────────────────────────────────────────

    def get_all_agent_permissions(self, user_id: str) -> Dict[str, Dict[str, bool]]:
        """Get scope permissions for all agents for a given user.

        Returns:
            Dict of {agent_id: {scope: enabled}}
        """
        rows = self._policy.call(
            self._policy.repository.list_all_scopes,
            owner_id=user_id,
        )
        result: Dict[str, Dict[str, bool]] = {}
        for row in rows:
            agent_id = row.agent_id
            if agent_id not in result:
                result[agent_id] = {s: False for s in VALID_SCOPES}
            result[agent_id][row.scope] = row.enabled
        return result

    def remove_user_permissions(self, user_id: str):
        """Remove all scope permissions and tool overrides for a user."""
        self._policy.call(
            self._policy.repository.remove_owner_state,
            owner_id=user_id,
        )

    def remove_agent_permissions(self, user_id: str, agent_id: str):
        """Remove all scope permissions and tool overrides for a specific agent under a user."""
        self._policy.call(
            self._policy.repository.remove_agent_state,
            owner_id=user_id,
            agent_id=agent_id,
        )

    def cleanup_stale_tool_overrides(self, agent_id: str, live_tool_names) -> int:
        """Delete `tool_overrides` rows for tools no longer in the agent's live registry.

        Called on agent (re)registration to converge the DB toward the
        in-code TOOL_REGISTRY. Prunes both legacy (permission_kind IS NULL)
        and per-(tool, kind) override rows in a single statement, since the
        WHERE clause filters only on tool_name — a tool removed from code
        is gone regardless of its permission_kind variant.

        Args:
            agent_id: The agent whose stale overrides should be pruned.
            live_tool_names: Iterable of tool names currently exposed by the agent.

        Returns:
            Number of rows deleted.
        """
        deleted = self._policy.call(
            self._policy.repository.prune_agent_overrides,
            agent_id=agent_id,
            live_tool_names=tuple(live_tool_names),
        )
        if deleted > 0:
            logger.info("Pruned %d stale tool_override row(s) for agent=%s", deleted, agent_id)
        return deleted
