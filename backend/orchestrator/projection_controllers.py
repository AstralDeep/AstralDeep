"""Deep-owned authorization boundary for AstralProjection chrome surfaces.

AstralProjection owns pure view models and rendering.  AstralDeep continues to
own the authenticated query and command decisions that supply those models.
This module keeps that boundary explicit: a surface cannot be registered
without real authorization gates, state handlers, and Projection view
builders, and every unregistered or failed path degrades closed.

The controllers implement the synchronous host-neutral ports declared in
``component_ports``.  Runtime adapters for asynchronous stores must finish
their I/O before returning from the injected handler; the later composition
cutover owns that wiring and does not change this security boundary.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from orchestrator.component_ports import (
    BoundaryMapping,
    BoundaryValue,
    PresentationCommand,
    PresentationCommandResult,
    PresentationQuery,
    PresentationView,
)

logger = logging.getLogger("Orchestrator.Projection")

QUERY_VIEW = "view"

PROJECTION_SURFACE_GROUPS: Mapping[str, str] = MappingProxyType(
    {
        # Audit/feedback/onboarding/admin supplied-state builders.
        "audit": "admin",
        "tour": "admin",
        "admin_tools": "admin",
        # Agent/authoring/draft/attachment supplied-state builders.
        "agents": "agents",
        "agent_authoring": "agents",
        "drafts": "agents",
        "attachments": "agents",
        # LLM/profile/memory/skills/scheduler/dreaming/pulse/theme builders.
        "llm": "personalization",
        "llm_system": "personalization",
        "personalization": "personalization",
        "pulse": "personalization",
        "theme": "personalization",
        # Remote-machine/workspace/history/timeline builders.
        "remote_machines": "workspace",
        "feature_flags": "workspace",
        "workspace": "workspace",
        "history": "workspace",
        "workspace_timeline": "workspace",
    }
)

PROJECTION_QUERY_OPERATIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {surface: frozenset({QUERY_VIEW}) for surface in PROJECTION_SURFACE_GROUPS}
)

PROJECTION_COMMAND_ACTIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "audit": frozenset({"chrome_audit_page"}),
        "tour": frozenset({"chrome_tour_event"}),
        "admin_tools": frozenset(
            {
                "chrome_admin_proposal_decide",
                "chrome_admin_step_save",
                "chrome_admin_step_archive",
                "chrome_admin_step_restore",
            }
        ),
        "agents": frozenset(
            {
                "chrome_perms_save",
                "chrome_visibility_set",
                "chrome_safe_set",
                "chrome_credentials_save",
                "chrome_credential_delete",
                "chrome_agent_enabled",
            }
        ),
        "agent_authoring": frozenset(
            {
                "chrome_author_start",
                "chrome_author_draft",
                "chrome_author_edit",
                "chrome_author_advance",
                "chrome_author_specify",
                "chrome_author_plan",
                "chrome_author_tasks",
                "chrome_author_clarify",
                "chrome_author_analyze",
                "chrome_author_generate",
                "chrome_author_list",
                "chrome_author_delete",
                "chrome_author_revise",
            }
        ),
        "drafts": frozenset({"chrome_draft_create"}),
        "attachments": frozenset({"chrome_attachment_delete"}),
        "llm": frozenset(
            {
                "chrome_llm_models",
                "chrome_llm_test",
                "chrome_llm_save",
                "chrome_llm_clear",
            }
        ),
        "llm_system": frozenset(
            {
                "chrome_llm_sys_models",
                "chrome_llm_sys_test",
                "chrome_llm_sys_save",
                "chrome_llm_sys_clear",
            }
        ),
        "personalization": frozenset(
            {
                "chrome_profile_save",
                "chrome_memory_update",
                "chrome_memory_delete",
                "chrome_skill_toggle",
                "chrome_job_pause",
                "chrome_job_resume",
                "chrome_job_delete",
                "chrome_job_run_now",
                "chrome_dreaming_toggle",
                "chrome_dreaming_trigger",
            }
        ),
        "pulse": frozenset(),
        "theme": frozenset({"chrome_theme_preset"}),
        "remote_machines": frozenset(
            {
                "chrome_machine_add",
                "chrome_machine_probe",
                "chrome_machine_delete",
                "chrome_machine_credential_set",
                "chrome_machine_credential_delete",
                "chrome_machine_retrust",
            }
        ),
        "feature_flags": frozenset(),
        "workspace": frozenset(),
        "history": frozenset(),
        "workspace_timeline": frozenset(
            {
                "chrome_workspace_timeline_view",
                "chrome_workspace_timeline_live",
            }
        ),
    }
)

_RESERVED_IDENTITY_FIELDS = frozenset(
    {
        "actor_id",
        "correlation_id",
        "owner_id",
        "principal_id",
        "tenant_id",
        "user_id",
    }
)
_DEFAULT_SENSITIVE_FIELDS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization_header",
        "bearer_token",
        "client_secret",
        "credential_ciphertext",
        "credential_secret",
        "credential_value",
        "id_token",
        "password",
        "private_key",
        "refresh_token",
        "secret",
    }
)
_CAMEL_FIELD_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_FIELD_SEPARATORS = re.compile(r"[-\s]+")


class ProjectionViewModel(Protocol):
    """Structural subset of ``astralprojection.models.ChromeViewModel``."""

    def to_dict(self) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class ProjectionPublicError:
    """Bounded non-sensitive failure passed to a Projection status builder."""

    code: str
    message: str
    denied: bool = False


@dataclass(frozen=True, slots=True)
class ProjectionQueryState:
    """Supplied state and optional optimistic revision returned by Deep."""

    state: Mapping[str, object]
    revision: int | None = None


@dataclass(frozen=True, slots=True)
class ProjectionCommandOutcome:
    """Actual Deep mutation outcome, optionally followed by refreshed state."""

    accepted: bool
    code: str
    revision: int | None = None
    state: Mapping[str, object] | None = None


QueryAuthorizer = Callable[[PresentationQuery], bool]
CommandAuthorizer = Callable[[PresentationCommand], bool]
QueryHandler = Callable[[PresentationQuery], ProjectionQueryState]
CommandHandler = Callable[[PresentationCommand], ProjectionCommandOutcome]
ViewBuilder = Callable[[BoundaryMapping], ProjectionViewModel | Mapping[str, object]]
StatusViewBuilder = Callable[
    [ProjectionPublicError], ProjectionViewModel | Mapping[str, object]
]


@dataclass(frozen=True, slots=True)
class ProjectionSurfaceControllerSpec:
    """Complete Deep wiring required before one extracted surface can run."""

    surface: str
    group: str
    query_authorizer: QueryAuthorizer
    query_handlers: Mapping[str, QueryHandler]
    command_authorizer: CommandAuthorizer | None
    command_handlers: Mapping[str, CommandHandler]
    view_builder: ViewBuilder
    denied_view_builder: StatusViewBuilder
    failure_view_builder: StatusViewBuilder
    sensitive_fields: frozenset[str] = field(
        default_factory=lambda: _DEFAULT_SENSITIVE_FIELDS
    )

    def __post_init__(self) -> None:
        expected_group = PROJECTION_SURFACE_GROUPS.get(self.surface)
        if expected_group is None:
            raise ValueError(f"unsupported Projection surface: {self.surface!r}")
        if self.group != expected_group:
            raise ValueError(
                f"surface {self.surface!r} belongs to group {expected_group!r}"
            )

        expected_queries = PROJECTION_QUERY_OPERATIONS[self.surface]
        query_handlers = dict(self.query_handlers)
        if set(query_handlers) != expected_queries:
            raise ValueError(
                f"surface {self.surface!r} requires query operations "
                f"{sorted(expected_queries)!r}"
            )
        if not callable(self.query_authorizer) or not all(
            callable(handler) for handler in query_handlers.values()
        ):
            raise TypeError("query authorizer and handlers must be callable")

        expected_commands = PROJECTION_COMMAND_ACTIONS[self.surface]
        command_handlers = dict(self.command_handlers)
        if set(command_handlers) != expected_commands:
            raise ValueError(
                f"surface {self.surface!r} requires command actions "
                f"{sorted(expected_commands)!r}"
            )
        if expected_commands and not callable(self.command_authorizer):
            raise TypeError("a command authorizer is required for command surfaces")
        if not all(callable(handler) for handler in command_handlers.values()):
            raise TypeError("command handlers must be callable")

        if not all(
            callable(builder)
            for builder in (
                self.view_builder,
                self.denied_view_builder,
                self.failure_view_builder,
            )
        ):
            raise TypeError("view, denied, and failure builders must be callable")

        if any(
            not isinstance(field_name, str) or not field_name.strip()
            for field_name in self.sensitive_fields
        ):
            raise ValueError("sensitive field names must not be blank")
        sensitive_fields = _DEFAULT_SENSITIVE_FIELDS | frozenset(
            _normalize_field(field_name) for field_name in self.sensitive_fields
        )
        object.__setattr__(self, "query_handlers", MappingProxyType(query_handlers))
        object.__setattr__(self, "command_handlers", MappingProxyType(command_handlers))
        object.__setattr__(self, "sensitive_fields", sensitive_fields)


class ProjectionControllerRegistry:
    """Immutable exact registry shared by the query and command controllers."""

    def __init__(self, specs: Iterable[ProjectionSurfaceControllerSpec]) -> None:
        registered: dict[str, ProjectionSurfaceControllerSpec] = {}
        for spec in specs:
            if not isinstance(spec, ProjectionSurfaceControllerSpec):
                raise TypeError(
                    "registry entries must be ProjectionSurfaceControllerSpec"
                )
            if spec.surface in registered:
                raise ValueError(f"duplicate Projection surface: {spec.surface}")
            registered[spec.surface] = spec
        self._specs = MappingProxyType(registered)

    @property
    def specs(self) -> Mapping[str, ProjectionSurfaceControllerSpec]:
        return self._specs

    def get(self, surface: str) -> ProjectionSurfaceControllerSpec | None:
        return self._specs.get(surface)


class ProjectionQueryController:
    """Authorized owner-scoped query boundary implementing the query port."""

    def __init__(self, registry: ProjectionControllerRegistry) -> None:
        if not isinstance(registry, ProjectionControllerRegistry):
            raise TypeError("registry must be a ProjectionControllerRegistry")
        self._registry = registry

    def query(self, query: PresentationQuery, /) -> PresentationView:
        if not isinstance(query, PresentationQuery):
            raise TypeError("query must be a PresentationQuery")
        spec = self._registry.get(query.surface)
        if spec is None:
            return _fallback_view(
                query.surface,
                _UNSUPPORTED_SURFACE,
            )
        if query.operation not in PROJECTION_QUERY_OPERATIONS[query.surface]:
            return _status_view(spec, spec.failure_view_builder, _UNSUPPORTED_OPERATION)
        if _contains_reserved_identity(query.arguments):
            return _status_view(spec, spec.denied_view_builder, _INVALID_IDENTITY)

        try:
            allowed = spec.query_authorizer(query)
        except Exception as exc:  # noqa: BLE001 - injected auth must fail closed
            _log_failure("query_authorization", query, exc)
            return _status_view(spec, spec.denied_view_builder, _FORBIDDEN)
        if allowed is not True:
            return _status_view(spec, spec.denied_view_builder, _FORBIDDEN)

        try:
            loaded = spec.query_handlers[query.operation](query)
            if not isinstance(loaded, ProjectionQueryState):
                raise TypeError("query handler returned an invalid state")
            state = _validated_state(query.surface, loaded.state)
            state = _redact_mapping(state, spec.sensitive_fields)
            model = _build_model(spec, spec.view_builder, state)
            return PresentationView(
                surface=query.surface,
                revision=loaded.revision,
                model=model,
            )
        except Exception as exc:  # noqa: BLE001 - injected boundary must fail closed
            _log_failure("query", query, exc)
            return _status_view(spec, spec.failure_view_builder, _QUERY_FAILED)


class ProjectionCommandController:
    """Authorized owner-scoped mutation boundary implementing the command port."""

    def __init__(self, registry: ProjectionControllerRegistry) -> None:
        if not isinstance(registry, ProjectionControllerRegistry):
            raise TypeError("registry must be a ProjectionControllerRegistry")
        self._registry = registry

    def execute(self, command: PresentationCommand, /) -> PresentationCommandResult:
        if not isinstance(command, PresentationCommand):
            raise TypeError("command must be a PresentationCommand")
        spec = self._registry.get(command.surface)
        if spec is None:
            return _command_failure(command.surface, _UNSUPPORTED_SURFACE)
        if command.action not in PROJECTION_COMMAND_ACTIONS[command.surface]:
            return _command_status(spec, spec.failure_view_builder, _UNSUPPORTED_ACTION)
        if _contains_reserved_identity(command.arguments):
            return _command_status(spec, spec.denied_view_builder, _INVALID_IDENTITY)

        try:
            authorizer = spec.command_authorizer
            allowed = authorizer(command) if authorizer is not None else False
        except Exception as exc:  # noqa: BLE001 - injected auth must fail closed
            _log_failure("command_authorization", command, exc)
            return _command_status(spec, spec.denied_view_builder, _FORBIDDEN)
        if allowed is not True:
            return _command_status(spec, spec.denied_view_builder, _FORBIDDEN)

        try:
            outcome = spec.command_handlers[command.action](command)
            if not isinstance(outcome, ProjectionCommandOutcome):
                raise TypeError("command handler returned an invalid outcome")
            # Validate accepted/code/revision before attempting a follow-up view.
            validated = PresentationCommandResult(
                accepted=outcome.accepted,
                code=outcome.code,
                revision=outcome.revision,
            )
        except Exception as exc:  # noqa: BLE001 - mutation boundary must fail closed
            _log_failure("command", command, exc)
            return _command_status(spec, spec.failure_view_builder, _COMMAND_FAILED)

        if outcome.state is None:
            return validated
        try:
            state = _validated_state(command.surface, outcome.state)
            state = _redact_mapping(state, spec.sensitive_fields)
            model = _build_model(spec, spec.view_builder, state)
        except Exception as exc:  # noqa: BLE001 - preserve known mutation outcome
            # The mutation outcome is already known.  A rendering failure must
            # not be relabelled as a failed mutation or produce false retry.
            _log_failure("command_view", command, exc)
            return validated
        return PresentationCommandResult(
            accepted=validated.accepted,
            code=validated.code,
            revision=validated.revision,
            model=model,
        )


_UNSUPPORTED_SURFACE = ProjectionPublicError(
    "unsupported_surface",
    "This presentation surface is unavailable.",
)
_UNSUPPORTED_OPERATION = ProjectionPublicError(
    "unsupported_operation",
    "This presentation request is not supported.",
)
_UNSUPPORTED_ACTION = ProjectionPublicError(
    "unsupported_action",
    "This presentation action is not supported.",
)
_INVALID_IDENTITY = ProjectionPublicError(
    "invalid_identity",
    "The request contains an invalid identity override.",
    denied=True,
)
_FORBIDDEN = ProjectionPublicError(
    "forbidden",
    "You are not allowed to perform this presentation operation.",
    denied=True,
)
_QUERY_FAILED = ProjectionPublicError(
    "query_failed",
    "This presentation surface could not be loaded.",
)
_COMMAND_FAILED = ProjectionPublicError(
    "command_failed",
    "This presentation action could not be completed.",
)


def _normalize_field(value: object) -> str:
    separated = _CAMEL_FIELD_BOUNDARY.sub("_", str(value).strip())
    return _FIELD_SEPARATORS.sub("_", separated).casefold()


def _matches_field(value: object, candidates: frozenset[str]) -> bool:
    normalized = _normalize_field(value)
    if normalized in candidates:
        return True
    compact = normalized.replace("_", "")
    return any(compact == candidate.replace("_", "") for candidate in candidates)


def _contains_reserved_identity(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            _matches_field(key, _RESERVED_IDENTITY_FIELDS)
            or _contains_reserved_identity(item)
            for key, item in value.items()
        )
    if isinstance(value, (tuple, list)):
        return any(_contains_reserved_identity(item) for item in value)
    return False


def _redact_value(
    value: BoundaryValue, sensitive_fields: frozenset[str]
) -> BoundaryValue:
    if isinstance(value, Mapping):
        return {
            key: (
                "[redacted]"
                if _matches_field(key, sensitive_fields)
                else _redact_value(item, sensitive_fields)
            )
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_redact_value(item, sensitive_fields) for item in value)
    return value


def _redact_mapping(
    value: BoundaryMapping,
    sensitive_fields: frozenset[str],
) -> BoundaryMapping:
    redacted = _redact_value(value, sensitive_fields)
    if not isinstance(redacted, Mapping):  # pragma: no cover - mapping in, mapping out
        raise TypeError("redacted view state must remain a mapping")
    return redacted


def _validated_state(surface: str, state: object) -> BoundaryMapping:
    if not isinstance(state, Mapping):
        raise TypeError("controller state must be a mapping")
    return PresentationView(surface=surface, model=state).model


def _build_model(
    spec: ProjectionSurfaceControllerSpec,
    builder: ViewBuilder | StatusViewBuilder,
    value: BoundaryMapping | ProjectionPublicError,
) -> BoundaryMapping:
    built = builder(value)  # type: ignore[arg-type]
    if isinstance(built, Mapping):
        raw_model = built
    else:
        to_dict = getattr(built, "to_dict", None)
        if not callable(to_dict):
            raise TypeError("Projection builder must return a mapping or to_dict model")
        raw_model = to_dict()
    if not isinstance(raw_model, Mapping):
        raise TypeError("Projection model to_dict() must return a mapping")

    model = PresentationView(surface=spec.surface, model=raw_model).model
    if model.get("surface") != spec.surface:
        raise ValueError("Projection model surface does not match its controller")
    if not isinstance(model.get("title"), str) or not model["title"].strip():
        raise ValueError("Projection model must contain a non-blank title")
    components = model.get("components")
    if not isinstance(components, tuple) or not all(
        isinstance(component, Mapping)
        and isinstance(component.get("type"), str)
        and bool(component["type"])
        for component in components
    ):
        raise ValueError("Projection model must contain typed component mappings")
    if not all(
        isinstance(model.get(name), Mapping)
        for name in ("theme", "layout", "degradation")
    ):
        raise ValueError("Projection model is missing adaptation metadata")
    return _redact_mapping(model, spec.sensitive_fields)


def _status_view(
    spec: ProjectionSurfaceControllerSpec,
    builder: StatusViewBuilder,
    error: ProjectionPublicError,
) -> PresentationView:
    try:
        model = _build_model(spec, builder, error)
        return PresentationView(surface=spec.surface, model=model)
    except Exception as exc:  # noqa: BLE001 - status builder has safe fallback
        _log_builder_failure(spec.surface, error.code, exc)
        return _fallback_view(spec.surface, error)


def _command_status(
    spec: ProjectionSurfaceControllerSpec,
    builder: StatusViewBuilder,
    error: ProjectionPublicError,
) -> PresentationCommandResult:
    view = _status_view(spec, builder, error)
    return PresentationCommandResult(
        accepted=False,
        code=error.code,
        model=view.model,
    )


def _command_failure(
    surface: str,
    error: ProjectionPublicError,
) -> PresentationCommandResult:
    return PresentationCommandResult(
        accepted=False,
        code=error.code,
        model=_fallback_view(surface, error).model,
    )


def _fallback_view(surface: str, error: ProjectionPublicError) -> PresentationView:
    model = {
        "surface": surface,
        "title": "Presentation unavailable",
        "components": (
            {
                "type": "alert",
                "variant": "error",
                "title": "Presentation unavailable",
                "message": error.message,
                "code": error.code,
            },
        ),
        "theme": {
            "name": "midnight",
            "colors": {},
            "color_scheme": "dark",
            "contrast": "normal",
        },
        "layout": {
            "mode": "standard",
            "columns": 1,
            "density": "comfortable",
            "areas": (),
        },
        "degradation": {
            "active": True,
            "reason": error.message,
            "unsupported_components": (),
            "fallback": "alert",
        },
    }
    return PresentationView(surface=surface, model=model)


def _log_failure(
    stage: str,
    request: PresentationQuery | PresentationCommand,
    exc: Exception,
) -> None:
    # Never interpolate exception text or request arguments: both can contain
    # credentials or upstream response bodies.  Correlation identity is enough
    # for operators to join the separately governed audit trail.
    operation = (
        request.operation if isinstance(request, PresentationQuery) else request.action
    )
    logger.error(
        "projection %s failed surface=%s operation=%s correlation_id=%s error_type=%s",
        stage,
        request.surface,
        operation,
        request.context.correlation_id,
        type(exc).__name__,
    )


def _log_builder_failure(surface: str, code: str, exc: Exception) -> None:
    logger.error(
        "projection status builder failed surface=%s code=%s error_type=%s",
        surface,
        code,
        type(exc).__name__,
    )


__all__ = [
    "PROJECTION_COMMAND_ACTIONS",
    "PROJECTION_QUERY_OPERATIONS",
    "PROJECTION_SURFACE_GROUPS",
    "QUERY_VIEW",
    "ProjectionCommandController",
    "ProjectionCommandOutcome",
    "ProjectionControllerRegistry",
    "ProjectionPublicError",
    "ProjectionQueryController",
    "ProjectionQueryState",
    "ProjectionSurfaceControllerSpec",
    "ProjectionViewModel",
]
