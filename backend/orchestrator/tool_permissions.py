"""Per-user, per-agent tool authorization backed by PostgreSQL's
agent_scopes/tool_overrides tables (six scopes:
read/write/search/system/files/execute); orchestrator.py and chain_authority.py
enforce its decisions at dispatch.
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
    token = _TURN_PERMISSION_MEMO.set({})
    try:
        yield
    finally:
        _TURN_PERMISSION_MEMO.reset(token)

VALID_SCOPES = ["tools:read", "tools:write", "tools:search", "tools:system",
                "tools:files", "tools:execute"]


class FixedReaderPolicyError(ValueError):
    def __init__(self, code: str = "assignment_source_permission_unavailable"):
        self.code = code
        super().__init__(code)


def _runtime_override_decision(rows, tool_name: str, required_scope: str):
    kind_row = next((row for row in rows if row.tool_name == tool_name
                     and row.permission_kind == required_scope), None)
    if kind_row is not None:
        return kind_row.enabled
    legacy_row = next((row for row in rows if row.tool_name == tool_name
                       and row.permission_kind is None), None)
    return False if legacy_row is not None and not legacy_row.enabled else None


def _runtime_scope_decision(rows, required_scope: str):
    row = next((row for row in rows if row.scope == required_scope), None)
    return None if row is None else row.enabled


def resolve_effective_tool_permissions(
    tool_scope_map: Mapping[str, str],
    *,
    owner_id: str,
    agent_id: str,
    scope_rows: Iterable[object],
    override_rows: Iterable[object],
    safe_default: bool = False,
) -> Dict[str, Dict[str, bool]]:
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
        self._tool_scope_map: Dict[str, Dict[str, str]] = {}
        self._safe_cache: Dict[str, tuple] = {}
        self._reject_legacy_json()

    def _reject_legacy_json(self) -> None:
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

    def register_tool_scopes(self, agent_id: str, tool_scope_map: Dict[str, str]):
        self._tool_scope_map[agent_id] = tool_scope_map
        logger.info(f"Registered tool scopes for agent={agent_id}: {len(tool_scope_map)} tools")

    def get_tool_scope(self, agent_id: str, tool_name: str) -> str:
        agent_map = self._tool_scope_map.get(agent_id, {})
        return agent_map.get(tool_name, "tools:read")

    def get_tool_scope_map(self, agent_id: str) -> Dict[str, str]:
        return self._tool_scope_map.get(agent_id, {})

    def get_agent_scopes(self, user_id: str, agent_id: str) -> Dict[str, bool]:
        rows = self._policy.call(
            self._policy.repository.list_scopes,
            owner_id=user_id,
            agent_id=agent_id,
        )
        stored = {row.scope: row.enabled for row in rows}
        return {scope: stored.get(scope, False) for scope in VALID_SCOPES}

    def has_any_enabled_scope(self, user_id: str) -> bool:
        return self._policy.call(
            self._policy.repository.has_any_enabled_scope,
            owner_id=user_id,
        )

    def list_disabled_agents(self, user_id: str) -> tuple[str, ...]:
        return self._policy.call(
            self._policy.repository.list_disabled_agents,
            owner_id=user_id,
        )

    def is_agent_disabled(self, user_id: str, agent_id: str) -> bool:
        return agent_id in self.list_disabled_agents(user_id)

    def set_agent_disabled(
        self,
        user_id: str,
        agent_id: str,
        disabled: bool,
    ) -> bool:
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
        selected = self._policy.call(
            self._policy.repository.set_tool_selection,
            owner_id=user_id,
            agent_id=agent_id,
            selected_tools=selected_tools,
            updated_at=int(time.time() * 1000),
        )
        return list(selected)

    def clear_tool_selection(self, user_id: str, agent_id: str) -> bool:
        return self._policy.call(
            self._policy.repository.clear_tool_selection,
            owner_id=user_id,
            agent_id=agent_id,
            updated_at=int(time.time() * 1000),
        )

    def scopes_required_by_tools(self, agent_id: str, exclude=("tools:write",)) -> List[str]:
        used = set(self._tool_scope_map.get(agent_id, {}).values()) or {"tools:read"}
        return sorted(s for s in used if s in VALID_SCOPES and s not in exclude)

    def is_scope_enabled(self, user_id: str, agent_id: str, scope: str) -> bool:
        rows = self._policy.call(
            self._policy.repository.list_scopes,
            owner_id=user_id,
            agent_id=agent_id,
        )
        return next((row.enabled for row in rows if row.scope == scope), False)

    def is_skill_authorized(self, user_id: str, agent_id: str, tool_name: str) -> bool:
        scope = self._tool_scope_map.get(agent_id, {}).get(tool_name)
        if scope not in VALID_SCOPES:
            return False
        try:
            agent = self._agents.call(
                self._agents.repository.get_agent_for_administration, agent_id=agent_id,
            )
            if agent is not None and (agent.deleted_at is not None or agent.owner_id != user_id):
                return False
            if self.is_tool_allowed(user_id, agent_id, tool_name):
                return True
            rows = self._policy.call(
                self._policy.repository.list_scopes, owner_id=user_id, agent_id=agent_id,
            )
            explicit = next((row for row in rows if row.scope == scope), None)
            if explicit is not None:
                return explicit.enabled
            return ((self._is_safe_agent(agent_id) and self._safe_flip_allowed(agent_id))
                    or self._is_owned_user_agent(user_id, agent_id))
        except Exception:
            logger.debug("skill authorization check failed", exc_info=True)
            return False

    def set_agent_scopes(self, user_id: str, agent_id: str, scopes: Dict[str, bool]):
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

    def get_tool_overrides(self, user_id: str, agent_id: str) -> Dict[str, bool]:
        rows = self._policy.call(
            self._policy.repository.list_overrides,
            owner_id=user_id,
            agent_id=agent_id,
        )
        return {row.tool_name: row.enabled for row in rows}

    def set_tool_overrides(self, user_id: str, agent_id: str, overrides: Dict[str, bool]):
        now = int(time.time() * 1000)
        for tool_name, enabled in overrides.items():
            if enabled:
                self._policy.call(
                    self._policy.repository.clear_tool_override,
                    owner_id=user_id,
                    agent_id=agent_id,
                    tool_name=tool_name,
                    permission_kind=None,
                )
            else:
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

    def is_tool_allowed(self, user_id: str, agent_id: str, tool_name: str) -> bool:
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
        override_rows = self._policy.call(
            self._policy.repository.list_overrides,
            owner_id=user_id,
            agent_id=agent_id,
        )
        override = _runtime_override_decision(override_rows, tool_name, required_scope)
        if override is not None:
            return override
        scope_rows = self._policy.call(
            self._policy.repository.list_scopes,
            owner_id=user_id,
            agent_id=agent_id,
        )
        scope = _runtime_scope_decision(scope_rows, required_scope)
        if scope is not None:
            return scope
        if required_scope not in VALID_SCOPES:
            return False
        if self._is_safe_agent(agent_id) and self._safe_flip_allowed(agent_id):
            return True
        if self._is_owned_user_agent(user_id, agent_id):
            return True
        return False

    def assert_fixed_reader_current(
        self, transaction, *, owner_id: str, plane_runtime, orchestrator,
        identity_claims,
    ) -> str:
        agent_id, tool_name, scope = "web-research-1", "fetch_page", "tools:read"
        try:
            from astralplane.repositories.tool_policy import FixedReaderPolicySnapshot
            from orchestrator.agent_identity import identity_requirement_satisfied
            from shared.feature_flags import flags

            if (plane_runtime is not self._policy.plane_runtime
                    or plane_runtime is not self._agents.plane_runtime
                    or orchestrator.tool_permissions is not self):
                raise FixedReaderPolicyError()
            snapshot = self._policy.repository.lock_fixed_reader_policy_snapshot(
                transaction, owner_id=owner_id,
            )
            if type(snapshot) is not FixedReaderPolicySnapshot or snapshot.owner_id != owner_id:
                raise FixedReaderPolicyError()
            card = orchestrator.agent_cards.get(agent_id)
            allowed = bool(
                card is not None
                and (agent_id in orchestrator.agents or agent_id in orchestrator.local_agents)
                and any(getattr(skill, "id", None) == tool_name for skill in card.skills)
                and identity_requirement_satisfied(card, identity_claims)
                and not orchestrator.security_flags.get(agent_id, {}).get(tool_name, {}).get("blocked")
                and self.get_tool_scope(agent_id, tool_name) == scope
                and not snapshot.disabled
                and not snapshot.user_agent_deleted
                and snapshot.user_agent_owner in (None, owner_id)
                and not (hasattr(orchestrator, "lifecycle_manager")
                         and snapshot.draft_status not in (None, "live")
                         and snapshot.is_public is not True)
            )
            decision = _runtime_override_decision(snapshot.overrides, tool_name, scope)
            if decision is None:
                decision = _runtime_scope_decision(snapshot.scopes, scope)
            if decision is None:
                decision = bool(
                    (flags.is_enabled("safe_agents") and snapshot.is_safe
                     and snapshot.is_public is not False)
                    or snapshot.user_agent_owner == owner_id
                )
            if not allowed or not decision:
                raise FixedReaderPolicyError("assignment_scope_revoked")
            return scope
        except FixedReaderPolicyError:
            raise
        except Exception:
            raise FixedReaderPolicyError() from None

    def _is_owned_user_agent(self, user_id: str, agent_id: str) -> bool:
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
            return False
        val = True if ownership is None else ownership.is_public
        cache[agent_id] = (val, now + 30.0)
        return val

    # Must write the per-kind row too, or the toggle silently no-ops
    def set_skill_enabled(self, user_id: str, agent_id: str, tool_name: str,
                          enabled: bool) -> None:
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
            self.set_tool_overrides(user_id, agent_id, {tool_name: enabled})

    def get_effective_tool_permissions(
        self, user_id: str, agent_id: str, safe_default: Optional[bool] = None
    ) -> Dict[str, Dict[str, bool]]:
        scope_map = self._tool_scope_map.get(agent_id, {})
        if not scope_map:
            return {}
        if safe_default is None:
            safe_default = self._is_safe_agent(agent_id) and self._safe_flip_allowed(agent_id)
        scope_rows = self._policy.call(
            self._policy.repository.list_scopes,
            owner_id=user_id,
            agent_id=agent_id,
        )
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
        if permission_kind not in VALID_SCOPES:
            raise ValueError(
                f"Invalid permission_kind {permission_kind!r}; must be one of {VALID_SCOPES}"
            )
        now = int(time.time() * 1000)
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
        scope_map = self._tool_scope_map.get(agent_id, {})
        if not scope_map:
            return 0
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
        return [
            tool for tool in available_tools
            if self.is_tool_allowed(user_id, agent_id, tool)
        ]

    def get_enabled_scope_names(self, user_id: str, agent_id: str) -> List[str]:
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
            enabled.add("tools:read")
        return [scope for scope in VALID_SCOPES if scope in enabled]

    def get_effective_permissions(
        self, user_id: str, agent_id: str, available_tools: list
    ) -> Dict[str, bool]:
        return {
            tool: self.is_tool_allowed(user_id, agent_id, tool)
            for tool in available_tools
        }

    def get_all_agent_permissions(self, user_id: str) -> Dict[str, Dict[str, bool]]:
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
        self._policy.call(
            self._policy.repository.remove_owner_state,
            owner_id=user_id,
        )

    def remove_agent_permissions(self, user_id: str, agent_id: str):
        self._policy.call(
            self._policy.repository.remove_agent_state,
            owner_id=user_id,
            agent_id=agent_id,
        )

    def cleanup_stale_tool_overrides(self, agent_id: str, live_tool_names) -> int:
        deleted = self._policy.call(
            self._policy.repository.prune_agent_overrides,
            agent_id=agent_id,
            live_tool_names=tuple(live_tool_names),
        )
        if deleted > 0:
            logger.info("Pruned %d stale tool_override row(s) for agent=%s", deleted, agent_id)
        return deleted
