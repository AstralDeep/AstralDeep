"""Defines current owner authority and truthful execution capability for terminal entry points.
The terminal API, agent tools and orchestrator share these fail-closed guards.
"""

from dataclasses import dataclass, field
import math
import time
from typing import Any, Callable

from shared.feature_flags import flags


AGENT_ID = "terminal-shell-1"
EXECUTION_TOOLS = frozenset({"start_terminal", "terminal_write"})


class TerminalError(Exception):
    def __init__(self, code: str, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def terminal_capability(manager=None, owner_id: str | None = None) -> dict[str, Any]:
    try:
        enabled = flags.is_enabled("terminal_shell") is True
    except Exception:
        enabled = False
    reason = "terminal_disabled"
    if enabled:
        reason = "terminal_isolation_unavailable"
        if manager is not None and owner_id:
            try:
                require_terminal_execution(manager, owner_id)
                return {"enabled": True, "available": True, "reason": None}
            except TerminalError as exc:
                reason = exc.code
    return {"enabled": enabled, "available": False, "reason": reason}


def require_terminal_enabled() -> None:
    if not terminal_capability()["enabled"]:
        raise TerminalError("terminal_disabled", 404)


def require_terminal_execution(manager=None, owner_id: str | None = None) -> None:
    require_terminal_enabled()
    from orchestrator.terminal_sessions import TerminalSessionManager

    if not isinstance(manager, TerminalSessionManager) or not owner_id:
        raise TerminalError("terminal_isolation_unavailable", 503)
    manager.require_execution(owner_id)


def assert_terminal_execution_available(tool_name: str, manager=None, owner_id=None) -> None:
    if tool_name in EXECUTION_TOOLS:
        require_terminal_execution(manager, owner_id)


def terminal_dispatch_arguments(agent_id: str, arguments):
    if agent_id != AGENT_ID:
        return arguments
    return {key: value for key, value in arguments.items() if key != "_terminal_authority"}


def assert_terminal_local_dispatch(orchestrator) -> None:
    from agents.terminal_shell.terminal_shell_agent import TerminalShellAgent

    require_terminal_enabled()
    try:
        enabled = flags.is_enabled("inprocess_agents") is True
    except Exception:
        enabled = False
    local = getattr(orchestrator, "local_agents", None)
    if not enabled or not isinstance(local, dict) or not isinstance(local.get(AGENT_ID), TerminalShellAgent):
        raise TerminalError("terminal_transport_unavailable", 503)


def assert_human_owner(claims: Any, user_id: Any) -> None:
    from orchestrator.auth import _extract_roles

    if not isinstance(claims, dict) or not isinstance(user_id, str) or not user_id.strip():
        raise TerminalError("terminal_owner_required", 403)
    if claims.get("sub") != user_id or "act" in claims or "machine_class" in claims:
        raise TerminalError("terminal_owner_required", 403)
    try:
        has_role = bool({"user", "admin"}.intersection(_extract_roles(claims)))
    except (AttributeError, TypeError):
        has_role = False
    if not has_role:
        raise TerminalError("terminal_owner_required", 403)
    expires = claims.get("exp")
    not_before = claims.get("nbf")
    now = time.time()
    if (
        isinstance(expires, bool) or not isinstance(expires, (int, float))
        or not math.isfinite(expires) or expires <= now
        or (not_before is not None and (
            isinstance(not_before, bool) or not isinstance(not_before, (int, float))
            or not math.isfinite(not_before) or not_before > now
        ))
    ):
        raise TerminalError("terminal_authority_expired", 401)


def assert_terminal_identity(orchestrator, websocket, user_id: str) -> None:
    require_terminal_enabled()
    sessions = getattr(orchestrator, "ui_sessions", None)
    claims = sessions.get(websocket) if isinstance(sessions, dict) else None
    assert_human_owner(claims, user_id)


def terminal_connection_context(orchestrator, websocket):
    from orchestrator.orchestrator import ConnectionContext

    contexts = getattr(orchestrator, "_connection_contexts", None)
    context = contexts.get(id(websocket)) if type(contexts) is dict else None
    if (type(context) is not ConnectionContext or context.websocket is not websocket
            or context.registered is not True or context.closing is not False):
        raise TerminalError("terminal_connection_unavailable", 409)
    return contexts, context


def assert_terminal_connection(orchestrator, websocket, captured) -> None:
    current = terminal_connection_context(orchestrator, websocket)
    if (type(captured) is not tuple or len(captured) != 2
            or current[0] is not captured[0] or current[1] is not captured[1]):
        raise TerminalError("terminal_connection_unavailable", 409)


def _assert_terminal_security(orchestrator, tool_name: str) -> None:
    from orchestrator.orchestrator import Orchestrator
    from orchestrator.terminal_host_review import assert_current_review

    if isinstance(orchestrator, Orchestrator) and (tool_name in EXECUTION_TOOLS
            or getattr(orchestrator, "_terminal_host_review", None) is not None):
        assert_current_review(orchestrator)
    all_flags = getattr(orchestrator, "security_flags", None)
    flags_for_agent = all_flags.get(AGENT_ID, {}) if isinstance(all_flags, dict) else None
    tool_flag = flags_for_agent.get(tool_name, {}) if isinstance(flags_for_agent, dict) else None
    if not isinstance(tool_flag, dict) or (
        tool_name in flags_for_agent and tool_flag.get("blocked") is not False
    ):
        raise TerminalError("terminal_security_blocked", 403)


def _assert_current_permission(orchestrator, user_id: str, tool_name: str,
                               identity_check: Callable[[], None]) -> None:
    from orchestrator.tool_permissions import turn_permission_memo

    identity_check()
    _assert_terminal_security(orchestrator, tool_name)
    permissions = getattr(orchestrator, "tool_permissions", None)
    try:
        with turn_permission_memo():
            allowed = permissions.is_tool_allowed(user_id, AGENT_ID, tool_name)
    except Exception:
        allowed = False
    identity_check()
    _assert_terminal_security(orchestrator, tool_name)
    if allowed is not True:
        raise TerminalError("terminal_permission_denied", 403)


def assert_terminal_delivery(orchestrator, claims: dict, user_id: str, tool_name: str) -> None:
    def current_identity():
        require_terminal_enabled()
        assert_human_owner(claims, user_id)

    _assert_current_permission(orchestrator, user_id, tool_name, current_identity)


@dataclass(frozen=True)
class TerminalAuthority:
    owner_id: str
    tool_name: str
    _orchestrator: Any = field(repr=False)
    _websocket: Any = field(repr=False)
    _execution_fence: Any = field(default=None, repr=False)
    _admission: Any = field(default=None, repr=False)
    _runtime: Any = field(default=None, repr=False)
    _request_check: Callable[[], None] | None = field(default=None, repr=False)
    _connection: Any = field(default=None, repr=False)

    def assert_session_current(self, tool_name: str) -> None:
        if self._request_check is not None:
            self._request_check()
        if tool_name != self.tool_name:
            raise TerminalError("terminal_authority_invalid", 403)

        def current_identity():
            assert_terminal_identity(self._orchestrator, self._websocket, self.owner_id)
            if tool_name in EXECUTION_TOOLS:
                assert_terminal_connection(self._orchestrator, self._websocket, self._connection)

        _assert_current_permission(
            self._orchestrator, self.owner_id, tool_name,
            current_identity)
        if self._request_check is not None:
            self._request_check()
        current_identity()

    def assert_current(self, tool_name: str) -> None:
        self.assert_session_current(tool_name)
        if self._execution_fence is not None:
            from orchestrator.plane_repository_context import plane_source_from_orchestrator

            try:
                if (getattr(self._orchestrator, "work_admission", None) is not self._admission
                        or plane_source_from_orchestrator(self._orchestrator).plane_runtime is not self._runtime):
                    raise ValueError()
                with self._runtime.transaction() as transaction:
                    self._admission.assert_current_execution_lease(self._execution_fence, transaction=transaction)
            except Exception:
                raise TerminalError("terminal_execution_authority_lost", 409) from None
            self.assert_session_current(tool_name)

    def matches(self, orchestrator) -> bool:
        return self._orchestrator is orchestrator


def bind_terminal_authority(orchestrator, websocket, user_id: str, tool_name: str) -> TerminalAuthority:
    from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
    from orchestrator.plane_repository_context import plane_source_from_orchestrator
    from orchestrator.work_admission import ExecutionFence
    from orchestrator.terminal_approvals import current_effect_check

    context = _CONNECTION_OPERATION_CONTEXT.get()
    fence = context.get("execution_fence") if isinstance(context, dict) else None
    admission = getattr(orchestrator, "work_admission", None)
    runtime = None
    if fence is not None:
        if type(fence) is not ExecutionFence or admission is None:
            raise TerminalError("terminal_execution_authority_required", 403)
        runtime = plane_source_from_orchestrator(orchestrator).plane_runtime
    elif tool_name in EXECUTION_TOOLS and admission is not None:
        raise TerminalError("terminal_execution_authority_required", 403)
    connection = terminal_connection_context(orchestrator, websocket) if tool_name in EXECUTION_TOOLS else None
    authority = TerminalAuthority(user_id, tool_name, orchestrator, websocket, fence, admission, runtime,
                                  current_effect_check(orchestrator, user_id, tool_name), connection)
    authority.assert_current(tool_name)
    return authority
