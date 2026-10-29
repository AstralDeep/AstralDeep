"""Validate same-session UI access-token renewal and resolve live credentials for fenced work."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping


_CLOSE_CODES = frozenset({
    "invalid", "identity_changed", "stale_connection", "authority_changed",
    "session_unavailable",
})


def _stable_pair(claims: Mapping[str, Any], preferred: str, fallback: str) -> tuple[str, str] | None:
    key = preferred if preferred in claims else fallback
    value = claims.get(key)
    if isinstance(value, str) and value.strip():
        return key, value
    return None


def session_identity(claims: Mapping[str, Any]) -> tuple[Any, ...] | None:
    if not isinstance(claims, Mapping):
        return None
    subject = claims.get("sub")
    issuer = claims.get("iss")
    client = _stable_pair(claims, "azp", "client_id")
    session = _stable_pair(claims, "sid", "session_state")
    if not all(isinstance(item, str) and item.strip() for item in (subject, issuer)):
        return None
    if client is None or session is None:
        return None
    return subject, issuer, client, session


def subject_binding(claims: Mapping[str, Any]) -> tuple[Any, ...] | None:
    if not isinstance(claims, Mapping):
        return None
    subject = claims.get("sub")
    issuer = claims.get("iss")
    client = _stable_pair(claims, "azp", "client_id")
    if not all(isinstance(item, str) and item.strip() for item in (subject, issuer)):
        return None
    if client is None:
        return None
    return subject, issuer, client


def _client_identity_is_configured(claims: Mapping[str, Any]) -> bool:
    client = _stable_pair(claims, "azp", "client_id")
    if client is None:
        return False
    if client[0] == "azp":
        return True
    expected = os.getenv("KEYCLOAK_CLIENT_ID", "").strip()
    return bool(expected and client[1] == expected)


def subject_auth_snapshot(claims: Mapping[str, Any]) -> dict[str, Any]:
    names = {
        "sub", "iss", "azp", "client_id", "sid", "session_state", "aud", "scope",
        "groups", "realm_access", "resource_access", "iat", "exp", "jti",
    }
    return {key: claims[key] for key in names if key in claims} | {
        "_raw_token": claims.get("_raw_token"),
        "_auth_connection_generation": claims.get("_auth_connection_generation"),
        "_auth_context_generation": claims.get("_auth_context_generation"),
    }


def _claim_set(claims: Mapping[str, Any], name: str) -> frozenset[str] | None:
    value = claims.get(name, [])
    if value is None:
        value = []
    if isinstance(value, str):
        value = value.split()
    if not isinstance(value, (list, tuple, set, frozenset)):
        return None
    if any(not isinstance(item, str) or not item for item in value):
        return None
    return frozenset(value)


def _authorization_claims(claims: Mapping[str, Any]) -> tuple[Any, ...] | None:
    audience = _claim_set(claims, "aud")
    scope = _claim_set(claims, "scope")
    groups = _claim_set(claims, "groups")
    realm = claims.get("realm_access", {})
    resources = claims.get("resource_access", {})
    if realm is None:
        realm = {}
    if resources is None:
        resources = {}
    if not isinstance(realm, Mapping) or not isinstance(resources, Mapping):
        return None
    realm_roles = _claim_set(realm, "roles")
    if realm_roles is None:
        return None
    resource_roles: list[tuple[str, frozenset[str]]] = []
    for client, access in resources.items():
        if not isinstance(client, str) or not isinstance(access, Mapping):
            return None
        roles = _claim_set(access, "roles")
        if roles is None:
            return None
        resource_roles.append((client, roles))
    return audience, scope, realm_roles, tuple(sorted(resource_roles)), groups


def refresh_claims_match(old: Mapping[str, Any], fresh: Mapping[str, Any]) -> bool:
    identity = session_identity(old)
    if identity is None or session_identity(fresh) != identity:
        return False
    old_authorization = _authorization_claims(old)
    return old_authorization is not None and _authorization_claims(fresh) == old_authorization


def parse_refresh_frame(frame: Any) -> tuple[str, str, str] | None:
    if not isinstance(frame, Mapping) or set(frame) != {
        "type", "request_id", "connection_generation", "token"
    } or frame.get("type") != "refresh_ui_auth":
        return None
    request_id = frame.get("request_id")
    generation = frame.get("connection_generation")
    token = frame.get("token")
    for value in (request_id, generation):
        try:
            parsed = uuid.UUID(value) if isinstance(value, str) else None
        except (TypeError, ValueError, AttributeError):
            return None
        if parsed is None or parsed.version != 4 or str(parsed) != value:
            return None
    if not isinstance(token, str) or not token or len(token.encode("utf-8")) > 32768:
        return None
    return request_id, generation, token


def parse_refresh_message(raw: str) -> dict[str, Any] | None:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate frame key")
            result[key] = value
        return result

    try:
        frame = json.loads(raw, object_pairs_hook=unique_pairs)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if parse_refresh_frame(frame) is None:
        return None
    return frame


def ready_for_renewal(session: Mapping[str, Any], generation: str, *, mock_auth: bool) -> bool:
    claims = session_identity(session)
    issued = session.get("iat")
    expires = session.get("exp")
    token_id = session.get("jti")
    return bool(
        not mock_auth
        and claims is not None
        and _client_identity_is_configured(session)
        and _authorization_claims(session) is not None
        and isinstance(issued, (int, float))
        and not isinstance(issued, bool)
        and isinstance(expires, (int, float))
        and not isinstance(expires, bool)
        and isinstance(token_id, str)
        and token_id
        and session.get("_auth_connection_generation") == generation
        and "ui_auth_refresh_v1" in (session.get("_client_capabilities") or ())
    )


def bind_registered_session(
    claims: dict[str, Any],
    *,
    token: str,
    capabilities: Any,
    connection_generation: str | None,
    mock_auth: bool,
) -> bool:
    if not isinstance(claims, dict):
        return False
    offered = capabilities if isinstance(capabilities, (list, tuple, set)) else ()
    claims["_raw_token"] = token
    claims["_client_capabilities"] = [
        value for value in offered if isinstance(value, str)
    ]
    claims["_auth_connection_generation"] = connection_generation
    claims["_auth_context_generation"] = connection_generation
    enabled = bool(
        isinstance(connection_generation, str)
        and ready_for_renewal(
            claims,
            connection_generation,
            mock_auth=mock_auth,
        )
    )
    claims["_auth_refresh_enabled"] = enabled
    return enabled


@dataclass(frozen=True)
class RegisteredSubjectTokenResolver:
    sessions: Mapping[Any, Mapping[str, Any]] = field(repr=False)
    contexts: Mapping[int, Any] = field(repr=False)
    websocket: Any
    owner_id: str
    connection_generation: str
    identity: tuple[Any, ...] | None = field(repr=False)
    operation_id: Any = None
    execution_fence: Any = None
    operation: Any = field(default=None, repr=False)
    operation_owner_scope: Any = field(default=None, init=False, repr=False)
    operation_owner_user_id: Any = field(default=None, init=False, repr=False)
    operation_connection_scope_id: Any = field(default=None, init=False, repr=False)
    operation_task: Any = field(default=None, init=False, repr=False)
    original_binding: tuple[Any, ...] | None = None
    original_token: str | None = field(default=None, repr=False)
    original_exp: int | float | None = None
    renewable: bool = False
    authorization: tuple[Any, ...] | None = None

    def __post_init__(self) -> None:
        owner = getattr(self.operation, "owner", None)
        scope = getattr(owner, "owner_scope", None)
        object.__setattr__(self, "operation_owner_scope", getattr(scope, "value", None))
        object.__setattr__(
            self, "operation_owner_user_id", getattr(owner, "owner_user_id", None)
        )
        object.__setattr__(
            self,
            "operation_connection_scope_id",
            getattr(owner, "connection_scope_id", None),
        )
        object.__setattr__(self, "operation_task", getattr(self.operation, "task", None))

    def is_current(self) -> bool:
        context = self.contexts.get(id(self.websocket))
        session = self.sessions.get(self.websocket)
        if (
            context is None
            or getattr(context, "websocket", None) is not self.websocket
            or not getattr(context, "registered", False)
            or getattr(context, "closing", True)
            or str(getattr(context, "connection_generation", "")) != self.connection_generation
            or not isinstance(session, Mapping)
            or session.get("sub") != self.owner_id
            or session.get("_auth_context_generation") != self.connection_generation
            or subject_binding(session) != self.original_binding
        ):
            return False
        if self.identity is not None and session_identity(session) != self.identity:
            return False
        if (
            self.authorization is not None
            and _authorization_claims(session) != self.authorization
        ):
            return False
        if self.operation_id is not None:
            operation = getattr(context, "operations", {}).get(self.operation_id)
            if (
                operation is None
                or operation is not self.operation
                or getattr(operation, "lease_lost", True)
                or getattr(operation, "fence", None) != self.execution_fence
                or self.execution_fence is None
                or getattr(
                    getattr(getattr(operation, "owner", None), "owner_scope", None),
                    "value",
                    None,
                )
                != self.operation_owner_scope
                or getattr(getattr(operation, "owner", None), "owner_user_id", None)
                != self.operation_owner_user_id
                or getattr(getattr(operation, "owner", None), "connection_scope_id", None)
                != self.operation_connection_scope_id
                or getattr(operation, "task", None) is not self.operation_task
            ):
                return False
            owner = getattr(operation, "owner", None)
            owner_scope = getattr(getattr(owner, "owner_scope", None), "value", None)
            if (
                owner_scope == "connection"
                and (
                    getattr(owner, "owner_user_id", None) is not None
                    or type(getattr(context, "connection_scope_id", None)) is not uuid.UUID
                    or getattr(owner, "connection_scope_id", None)
                    != getattr(context, "connection_scope_id", None)
                    or session.get("sub") != self.owner_id
                )
            ):
                return False
            if owner_scope not in {"user", "connection"}:
                return False
            if self.operation_task is not None:
                done = getattr(self.operation_task, "done", None)
                cancelling = getattr(self.operation_task, "cancelling", None)
                if (
                    not callable(done)
                    or not callable(cancelling)
                    or done()
                    or cancelling()
                ):
                    return False
            try:
                current_task = asyncio.current_task()
            except RuntimeError:
                current_task = None
            if current_task is not None and current_task.cancelling():
                return False
        return True

    def resolve(self) -> str | None:
        if not self.is_current():
            return None
        session = self.sessions.get(self.websocket)
        if (
            not isinstance(session, Mapping)
            or session.get("_auth_refresh_required") is True
        ):
            return None
        exp = session.get("exp") if self.renewable else self.original_exp
        if isinstance(exp, bool) or not isinstance(exp, (int, float)) or exp <= time.time():
            return None
        token = session.get("_raw_token") if self.renewable else self.original_token
        return token if isinstance(token, str) and token else None

    def unavailable_reason(self) -> str:
        if not self.is_current():
            return "binding_not_current"
        session = self.sessions.get(self.websocket)
        if not isinstance(session, Mapping):
            return "session_missing"
        if session.get("_auth_refresh_required") is True:
            return "refresh_required"
        expires = session.get("exp") if self.renewable else self.original_exp
        if isinstance(expires, bool) or not isinstance(expires, (int, float)):
            return "expiry_unavailable"
        if expires <= time.time():
            return "credential_expired"
        token = session.get("_raw_token") if self.renewable else self.original_token
        if not isinstance(token, str) or not token:
            return "credential_unavailable"
        return "credential_available"


def capture_subject_token_resolver(
    sessions: Mapping[Any, Mapping[str, Any]],
    contexts: Mapping[int, Any],
    websocket: Any,
    owner_id: str,
    active_operation: Mapping[str, Any] | None = None,
) -> RegisteredSubjectTokenResolver | None:
    resolver, _reason = capture_subject_token_resolver_with_reason(
        sessions, contexts, websocket, owner_id, active_operation
    )
    return resolver


def capture_subject_token_resolver_with_reason(
    sessions: Mapping[Any, Mapping[str, Any]],
    contexts: Mapping[int, Any],
    websocket: Any,
    owner_id: str,
    active_operation: Mapping[str, Any] | None = None,
) -> tuple[RegisteredSubjectTokenResolver | None, str | None]:
    session = sessions.get(websocket)
    context = contexts.get(id(websocket))
    if not isinstance(session, Mapping):
        return None, "session_missing"
    if context is None:
        return None, "context_missing"
    if getattr(context, "websocket", None) is not websocket:
        return None, "socket_mismatch"
    if not getattr(context, "registered", False):
        return None, "not_registered"
    if getattr(context, "closing", True):
        return None, "connection_closing"
    if session.get("sub") != owner_id:
        return None, "owner_mismatch"
    generation = session.get("_auth_context_generation")
    identity = session_identity(session)
    original_binding = subject_binding(session)
    authorization = _authorization_claims(session)
    if not isinstance(generation, str) or not generation:
        return None, "generation_missing"
    if str(getattr(context, "connection_generation", "")) != generation:
        return None, "generation_mismatch"
    if original_binding is None:
        return None, "subject_binding_invalid"
    if authorization is None:
        return None, "authorization_claims_invalid"
    operation_id = None
    execution_fence = None
    operation = None
    operation_owner_scope = None
    operation_owner_user_id = None
    operation_connection_scope_id = None
    if active_operation is not None:
        operation_obj = active_operation.get("operation")
        operation_id = getattr(operation_obj, "operation_id", None)
        operation = getattr(context, "operations", {}).get(operation_id)
        execution_fence = active_operation.get("execution_fence")
        if operation is None or operation_id is None:
            return None, "operation_missing"
        operation_owner = getattr(operation, "owner", None)
        operation_owner_scope = getattr(
            getattr(operation_owner, "owner_scope", None), "value", None
        )
        operation_owner_user_id = getattr(operation_owner, "owner_user_id", None)
        operation_connection_scope_id = getattr(
            operation_owner, "connection_scope_id", None
        )
        if operation_owner_scope == "user":
            if operation_owner_user_id != owner_id:
                return None, "operation_owner_mismatch"
        elif operation_owner_scope == "connection":
            if (
                operation_owner_user_id is not None
                or type(getattr(context, "connection_scope_id", None)) is not uuid.UUID
                or operation_connection_scope_id
                != getattr(context, "connection_scope_id", None)
            ):
                return None, "operation_owner_mismatch"
        else:
            return None, "operation_owner_mismatch"
        if getattr(operation, "lease_lost", True):
            return None, "operation_lease_lost"
        operation_fence = getattr(operation, "fence", None)
        if execution_fence is None or operation_fence is None:
            return None, "operation_fence_missing"
        if operation_fence != execution_fence:
            return None, "operation_fence_mismatch"
        if active_operation.get("owner") != operation_owner:
            return None, "operation_owner_binding_mismatch"
        if str(active_operation.get("connection_generation")) != generation:
            return None, "operation_generation_mismatch"
        baseline = getattr(operation, "subject_auth_claims", None)
        if not isinstance(baseline, Mapping):
            return None, "operation_subject_claims_missing"
        if baseline.get("sub") != owner_id:
            return None, "operation_subject_binding_mismatch"
        authorization = _authorization_claims(baseline)
        if subject_binding(baseline) != original_binding:
            return None, "operation_subject_binding_mismatch"
        if authorization is None:
            return None, "operation_authorization_claims_invalid"
        if baseline.get("_auth_context_generation") != generation:
            return None, "operation_generation_mismatch"
        identity = session_identity(baseline)
        original_token = baseline.get("_raw_token")
        original_exp = baseline.get("exp")
        renewable = (
            session.get("_auth_refresh_enabled") is True
            and identity is not None
        )
    else:
        original_token = session.get("_raw_token")
        original_exp = session.get("exp")
        renewable = session.get("_auth_refresh_enabled") is True and identity is not None
    resolver = RegisteredSubjectTokenResolver(
        sessions=sessions,
        contexts=contexts,
        websocket=websocket,
        owner_id=owner_id,
        connection_generation=generation,
        identity=identity,
        operation_id=operation_id,
        execution_fence=execution_fence,
        operation=operation,
        original_binding=original_binding,
        original_token=(original_token if isinstance(original_token, str) else None),
        original_exp=original_exp,
        renewable=renewable,
        authorization=authorization,
    )
    return resolver, None


class UIAuthRenewal:
    def __init__(
        self,
        sessions: dict[Any, dict[str, Any]],
        contexts: Mapping[int, Any],
        verifier: Callable[[str], Awaitable[Mapping[str, Any] | None]],
        *,
        mock_auth: bool = False,
    ) -> None:
        self.sessions = sessions
        self.contexts = contexts
        self.verifier = verifier
        self.mock_auth = mock_auth
        self._locks: dict[int, asyncio.Lock] = {}
        self._events: dict[int, asyncio.Event] = {}

    def signal(self, websocket: Any) -> None:
        self._events.setdefault(id(websocket), asyncio.Event()).set()

    async def wait_for_token(
        self,
        resolver: RegisteredSubjectTokenResolver,
        *,
        timeout: float = 30.0,
    ) -> str | None:
        if not resolver.renewable:
            return None
        event = self._events.setdefault(id(resolver.websocket), asyncio.Event())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(max(timeout, 0.0), 30.0)
        while resolver.is_current():
            token = resolver.resolve()
            if token is not None:
                return token
            event.clear()
            token = resolver.resolve()
            if token is not None:
                return token
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(event.wait(), timeout=min(0.25, remaining))
            except TimeoutError:
                continue
            if resolver.resolve() is None:
                return None
        return None

    async def handle(self, websocket: Any, frame: Any) -> dict[str, Any] | None:
        parsed = parse_refresh_frame(frame)
        if parsed is None:
            return None
        request_id, generation, token = parsed
        active_ids = set(self.contexts)
        for socket_id in tuple(self._locks):
            if socket_id not in active_ids:
                self._locks.pop(socket_id, None)
                self._events.pop(socket_id, None)
        lock = self._locks.setdefault(id(websocket), asyncio.Lock())
        async with lock:
            session = self.sessions.get(websocket)
            context = self.contexts.get(id(websocket))
            if (
                self.mock_auth
                or not isinstance(session, dict)
                or context is None
                or getattr(context, "websocket", None) is not websocket
                or not getattr(context, "registered", False)
                or getattr(context, "closing", True)
                or str(getattr(context, "connection_generation", "")) != generation
                or not ready_for_renewal(session, generation, mock_auth=self.mock_auth)
            ):
                return _ack(request_id, generation, False, "stale_connection")
            seen = session.get("_auth_refresh_request_ids")
            if isinstance(seen, (list, tuple, set, frozenset)) and request_id in seen:
                return _ack(request_id, generation, False, "invalid")
            try:
                fresh = await self.verifier(token)
            except Exception:
                fresh = None
            if (
                self.sessions.get(websocket) is not session
                or self.contexts.get(id(websocket)) is not context
                or not getattr(context, "registered", False)
                or getattr(context, "closing", True)
                or str(getattr(context, "connection_generation", "")) != generation
            ):
                return _ack(request_id, generation, False, "stale_connection")
            if (
                not isinstance(fresh, Mapping)
                or not _client_identity_is_configured(fresh)
                or not refresh_claims_match(session, fresh)
            ):
                return _ack(request_id, generation, False, "identity_changed")
            old_iat = session.get("iat")
            old_exp = session.get("exp")
            new_iat = fresh.get("iat")
            new_exp = fresh.get("exp")
            old_jti = session.get("jti")
            new_jti = fresh.get("jti")
            if (
                isinstance(old_iat, bool) or not isinstance(old_iat, (int, float))
                or isinstance(old_exp, bool) or not isinstance(old_exp, (int, float))
                or isinstance(new_iat, bool) or not isinstance(new_iat, (int, float))
                or isinstance(new_exp, bool) or not isinstance(new_exp, (int, float))
                or new_iat < old_iat or new_exp <= old_exp or new_exp <= time.time()
                or not isinstance(old_jti, str) or not old_jti
                or not isinstance(new_jti, str) or not new_jti
                or old_jti == new_jti
            ):
                return _ack(request_id, generation, False, "invalid")
            updated = dict(fresh)
            updated.update({
                key: value for key, value in session.items()
                if key.startswith("_") and key not in {"_raw_token", "_auth_refresh_request_ids"}
            })
            updated["_raw_token"] = token
            updated["_auth_refresh_request_ids"] = tuple(
                list(seen or ())[-63:] + [request_id]
            )
            updated.pop("_auth_refresh_required", None)
            updated.pop("_delegation_fault", None)
            session.clear()
            session.update(updated)
            self.signal(websocket)
            return _ack(request_id, generation, True)


def _ack(request_id: str, generation: str, accepted: bool, closed_code: str | None = None) -> dict[str, Any]:
    result = {
        "type": "ui_auth_refreshed",
        "request_id": request_id,
        "connection_generation": generation,
        "accepted": accepted,
    }
    if not accepted and closed_code in _CLOSE_CODES:
        result["closed_code"] = closed_code
    return result
