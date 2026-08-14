"""Authorization and isolation tests for Deep's Projection controllers."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import pytest
from orchestrator.component_ports import (
    PresentationCommand,
    PresentationCommandPort,
    PresentationContext,
    PresentationQuery,
    PresentationQueryPort,
)
from orchestrator.projection_controllers import (
    PROJECTION_COMMAND_ACTIONS,
    PROJECTION_QUERY_OPERATIONS,
    PROJECTION_SURFACE_GROUPS,
    ProjectionCommandController,
    ProjectionCommandOutcome,
    ProjectionControllerRegistry,
    ProjectionPublicError,
    ProjectionQueryController,
    ProjectionQueryState,
    ProjectionSurfaceControllerSpec,
)

CONTEXT = PresentationContext(
    owner_id="owner-alice",
    actor_id="actor-alice",
    correlation_id="request-074",
    tenant_id="tenant-clinical",
)


@dataclass
class _ViewModel:
    value: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return self.value


def _model(
    surface: str,
    state: object,
    *,
    title: str = "Agents",
) -> dict[str, object]:
    return {
        "surface": surface,
        "title": title,
        "components": [
            {
                "type": "text",
                "value": state,
            }
        ],
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
            "areas": [],
        },
        "degradation": {
            "active": False,
            "reason": "",
            "unsupported_components": [],
            "fallback": "alert",
        },
    }


def _query_state(_query: PresentationQuery) -> ProjectionQueryState:
    return ProjectionQueryState({"items": []}, revision=1)


def _command_outcome(_command: PresentationCommand) -> ProjectionCommandOutcome:
    return ProjectionCommandOutcome(True, "updated", revision=2)


def _status_model(error: ProjectionPublicError) -> _ViewModel:
    return _ViewModel(
        _model(
            "agents",
            {
                "code": error.code,
                "message": error.message,
                "denied": error.denied,
            },
        )
    )


def _spec(
    *,
    surface: str = "agents",
    query_authorizer: Any = lambda _query: True,
    query_handler: Any = _query_state,
    command_authorizer: Any = lambda _command: True,
    command_handler: Any = _command_outcome,
    view_builder: Any = None,
    denied_builder: Any = None,
    failure_builder: Any = None,
    sensitive_fields: frozenset[str] | None = None,
) -> ProjectionSurfaceControllerSpec:
    if view_builder is None:

        def default_view_builder(state: object) -> _ViewModel:
            return _ViewModel(_model(surface, state))

        view_builder = default_view_builder

    def status_builder(error: ProjectionPublicError) -> _ViewModel:
        return _ViewModel(
            _model(
                surface,
                {
                    "code": error.code,
                    "message": error.message,
                    "denied": error.denied,
                },
            )
        )

    commands = {
        action: command_handler for action in PROJECTION_COMMAND_ACTIONS[surface]
    }
    kwargs: dict[str, object] = {}
    if sensitive_fields is not None:
        kwargs["sensitive_fields"] = sensitive_fields
    return ProjectionSurfaceControllerSpec(
        surface=surface,
        group=PROJECTION_SURFACE_GROUPS[surface],
        query_authorizer=query_authorizer,
        query_handlers={"view": query_handler},
        command_authorizer=(
            command_authorizer if PROJECTION_COMMAND_ACTIONS[surface] else None
        ),
        command_handlers=commands,
        view_builder=view_builder,
        denied_view_builder=denied_builder or status_builder,
        failure_view_builder=failure_builder or status_builder,
        **kwargs,  # type: ignore[arg-type]
    )


def _controllers(
    spec: ProjectionSurfaceControllerSpec,
) -> tuple[ProjectionQueryController, ProjectionCommandController]:
    registry = ProjectionControllerRegistry([spec])
    return ProjectionQueryController(registry), ProjectionCommandController(registry)


def _query(**arguments: object) -> PresentationQuery:
    return PresentationQuery("agents", "view", CONTEXT, arguments)


def _command(
    action: str = "chrome_agent_enabled",
    **arguments: object,
) -> PresentationCommand:
    return PresentationCommand(
        "agents",
        action,
        CONTEXT,
        arguments,
        expected_revision=7,
        idempotency_key="operation-074",
    )


def test_concrete_inventory_covers_the_four_extracted_groups() -> None:
    assert set(PROJECTION_SURFACE_GROUPS.values()) == {
        "admin",
        "agents",
        "personalization",
        "workspace",
    }
    assert set(PROJECTION_QUERY_OPERATIONS) == set(PROJECTION_SURFACE_GROUPS)
    assert set(PROJECTION_COMMAND_ACTIONS) == set(PROJECTION_SURFACE_GROUPS)
    assert all(
        operations == {"view"} for operations in PROJECTION_QUERY_OPERATIONS.values()
    )
    assert {
        "chrome_admin_proposal_decide",
        "chrome_agent_enabled",
        "chrome_llm_save",
        "chrome_workspace_timeline_view",
    } <= set().union(*PROJECTION_COMMAND_ACTIONS.values())
    assert {"feature_flags", "workspace", "history"} <= set(PROJECTION_SURFACE_GROUPS)


def test_surface_registration_is_exact_and_immutable() -> None:
    spec = _spec(sensitive_fields=frozenset({" API-Key "}))
    assert isinstance(spec.query_handlers, MappingProxyType)
    assert isinstance(spec.command_handlers, MappingProxyType)
    assert {"api_key", "access_token", "password"} <= spec.sensitive_fields
    with pytest.raises(TypeError):
        spec.query_handlers["extra"] = _query_state  # type: ignore[index]


@pytest.mark.parametrize(
    ("changes", "error", "message"),
    [
        ({"surface": "unknown"}, ValueError, "unsupported Projection surface"),
        ({"group": "workspace"}, ValueError, "belongs to group"),
        ({"query_handlers": {}}, ValueError, "requires query operations"),
        ({"query_authorizer": None}, TypeError, "query authorizer"),
        ({"query_handlers": {"view": None}}, TypeError, "query authorizer"),
        ({"command_handlers": {}}, ValueError, "requires command actions"),
        (
            {
                "command_handlers": {
                    **_spec().command_handlers,
                    "chrome_agent_enabled": None,
                }
            },
            TypeError,
            "command handlers",
        ),
        ({"command_authorizer": None}, TypeError, "command authorizer"),
        ({"view_builder": None}, TypeError, "builders must be callable"),
        ({"sensitive_fields": frozenset({"  "})}, ValueError, "must not be blank"),
        (
            {"sensitive_fields": frozenset({None})},  # type: ignore[arg-type]
            ValueError,
            "must not be blank",
        ),
    ],
)
def test_incomplete_surface_registration_fails_closed(
    changes: dict[str, object],
    error: type[Exception],
    message: str,
) -> None:
    base = _spec()
    values: dict[str, object] = {
        "surface": base.surface,
        "group": base.group,
        "query_authorizer": base.query_authorizer,
        "query_handlers": base.query_handlers,
        "command_authorizer": base.command_authorizer,
        "command_handlers": base.command_handlers,
        "view_builder": base.view_builder,
        "denied_view_builder": base.denied_view_builder,
        "failure_view_builder": base.failure_view_builder,
        "sensitive_fields": base.sensitive_fields,
    }
    values.update(changes)
    with pytest.raises(error, match=message):
        ProjectionSurfaceControllerSpec(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("surface", ["pulse", "feature_flags", "workspace", "history"])
def test_read_only_surface_requires_no_command_authorizer(surface: str) -> None:
    spec = _spec(surface=surface, command_authorizer=None)
    assert spec.command_handlers == {}
    assert spec.command_authorizer is None


def test_registry_rejects_invalid_and_duplicate_entries() -> None:
    spec = _spec()
    with pytest.raises(TypeError, match="registry entries"):
        ProjectionControllerRegistry([object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="duplicate Projection surface"):
        ProjectionControllerRegistry([spec, spec])
    registry = ProjectionControllerRegistry([spec])
    assert registry.get("agents") is spec
    assert registry.get("missing") is None
    assert isinstance(registry.specs, MappingProxyType)


def test_controller_constructors_and_ports_are_strict_and_structural() -> None:
    registry = ProjectionControllerRegistry([_spec()])
    queries = ProjectionQueryController(registry)
    commands = ProjectionCommandController(registry)
    assert isinstance(queries, PresentationQueryPort)
    assert isinstance(commands, PresentationCommandPort)
    with pytest.raises(TypeError, match="registry"):
        ProjectionQueryController(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="registry"):
        ProjectionCommandController(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="PresentationQuery"):
        queries.query(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="PresentationCommand"):
        commands.execute(object())  # type: ignore[arg-type]


def test_authorized_query_uses_context_owner_and_redacts_before_projection() -> None:
    observed: dict[str, object] = {}

    def load(query: PresentationQuery) -> ProjectionQueryState:
        observed["owner"] = query.context.owner_id
        observed["actor"] = query.context.actor_id
        observed["arguments"] = query.arguments
        return ProjectionQueryState(
            {
                "agent": {
                    "name": "Research",
                    "apiKey": "query-secret",
                    "has_api_key": True,
                },
                "custom-token": "another-secret",
            },
            revision=8,
        )

    def build(state: object) -> _ViewModel:
        observed["builder_state"] = state
        return _ViewModel(_model("agents", state))

    spec = _spec(
        query_handler=load,
        view_builder=build,
        sensitive_fields=frozenset({"api_key", "custom-token"}),
    )
    queries, _ = _controllers(spec)
    result = queries.query(_query(tab="mine"))

    assert observed["owner"] == "owner-alice"
    assert observed["actor"] == "actor-alice"
    assert observed["arguments"] == {"tab": "mine"}
    assert observed["builder_state"] == {
        "agent": {
            "apiKey": "[redacted]",
            "has_api_key": True,
            "name": "Research",
        },
        "custom-token": "[redacted]",
    }
    assert result.revision == 8
    assert "query-secret" not in repr(result.model)
    assert "another-secret" not in repr(result.model)
    assert result.model["surface"] == "agents"


def test_query_identity_override_is_denied_before_authorization_or_state() -> None:
    calls: list[str] = []
    spec = _spec(
        query_authorizer=lambda _query: calls.append("authorize") or True,
        query_handler=lambda _query: calls.append("load") or _query_state(_query),
    )
    queries, _ = _controllers(spec)
    result = queries.query(_query(filters=[{"ownerId": "owner-bob"}]))
    assert calls == []
    assert result.model["components"][0]["value"]["code"] == "invalid_identity"
    assert result.model["components"][0]["value"]["denied"] is True


def test_query_denial_and_authorizer_failure_never_call_state_handler(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []
    queries, _ = _controllers(
        _spec(
            query_authorizer=lambda _query: False,
            query_handler=lambda _query: calls.append("load") or _query_state(_query),
        )
    )
    denied = queries.query(_query())
    assert calls == []
    assert denied.model["components"][0]["value"]["code"] == "forbidden"

    def broken_auth(_query: PresentationQuery) -> bool:
        raise RuntimeError("auth-secret-must-not-escape")

    caplog.clear()
    queries, _ = _controllers(_spec(query_authorizer=broken_auth))
    failed = queries.query(_query())
    assert failed.model["components"][0]["value"]["code"] == "forbidden"
    assert "auth-secret-must-not-escape" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_query_failure_is_redacted_and_status_builder_failure_degrades(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken_query(_query: PresentationQuery) -> ProjectionQueryState:
        raise RuntimeError("database-password=do-not-leak")

    queries, _ = _controllers(_spec(query_handler=broken_query))
    result = queries.query(_query())
    assert result.model["components"][0]["value"]["code"] == "query_failed"
    assert "database-password=do-not-leak" not in repr(result.model)
    assert "database-password=do-not-leak" not in caplog.text

    def broken_status(_error: ProjectionPublicError) -> _ViewModel:
        raise RuntimeError("status-secret")

    queries, _ = _controllers(
        _spec(query_authorizer=lambda _query: False, denied_builder=broken_status)
    )
    fallback = queries.query(_query())
    assert fallback.model["degradation"]["active"] is True
    assert fallback.model["components"][0]["code"] == "forbidden"
    assert "status-secret" not in repr(fallback.model)
    assert "status-secret" not in caplog.text


def test_unknown_query_surface_and_operation_fail_closed() -> None:
    queries, _ = _controllers(_spec())
    unknown_surface = queries.query(PresentationQuery("guide", "view", CONTEXT))
    assert unknown_surface.model["components"][0]["code"] == "unsupported_surface"
    assert unknown_surface.model["degradation"]["active"] is True

    unknown_operation = queries.query(PresentationQuery("agents", "delete", CONTEXT))
    assert (
        unknown_operation.model["components"][0]["value"]["code"]
        == "unsupported_operation"
    )


@pytest.mark.parametrize(
    "loaded",
    [
        object(),
        _ViewModel([]),  # type: ignore[arg-type]
        ProjectionQueryState("not-a-mapping"),  # type: ignore[arg-type]
        ProjectionQueryState({"score": float("nan")}),
        ProjectionQueryState({}, revision=-1),
    ],
)
def test_invalid_query_state_returns_safe_failure(loaded: object) -> None:
    queries, _ = _controllers(_spec(query_handler=lambda _query: loaded))
    result = queries.query(_query())
    assert result.model["components"][0]["value"]["code"] == "query_failed"


@pytest.mark.parametrize(
    "view",
    [
        object(),
        _ViewModel({"surface": "agents"}),
        _ViewModel(_model("workspace_timeline", {})),
        _ViewModel(_model("agents", {}, title=" ")),
        _ViewModel({**_model("agents", {}), "components": [{}]}),
        _ViewModel(
            {
                key: value
                for key, value in _model("agents", {}).items()
                if key != "theme"
            }
        ),
    ],
)
def test_invalid_projection_model_returns_safe_query_failure(view: object) -> None:
    queries, _ = _controllers(_spec(view_builder=lambda _state: view))
    result = queries.query(_query())
    assert result.model["components"][0]["value"]["code"] == "query_failed"


def test_mapping_projection_model_is_supported_and_output_is_redacted() -> None:
    view = _model("agents", {"access_token": "builder-secret"})
    queries, _ = _controllers(_spec(view_builder=lambda _state: view))
    result = queries.query(_query())
    assert result.model["components"][0]["value"]["access_token"] == "[redacted]"


def test_authorized_command_preserves_owner_fences_and_redacts_refreshed_view() -> None:
    observed: dict[str, object] = {}

    def mutate(command: PresentationCommand) -> ProjectionCommandOutcome:
        observed["owner"] = command.context.owner_id
        observed["actor"] = command.context.actor_id
        observed["revision"] = command.expected_revision
        observed["idempotency"] = command.idempotency_key
        return ProjectionCommandOutcome(
            True,
            "updated",
            revision=8,
            state={"agent": {"name": "Research", "api_key": "command-secret"}},
        )

    _, commands = _controllers(_spec(command_handler=mutate))
    result = commands.execute(_command(enabled=True))
    assert observed == {
        "owner": "owner-alice",
        "actor": "actor-alice",
        "revision": 7,
        "idempotency": "operation-074",
    }
    assert result.accepted is True
    assert result.code == "updated"
    assert result.revision == 8
    assert "command-secret" not in repr(result.model)
    assert result.model["components"][0]["value"]["agent"]["api_key"] == "[redacted]"


def test_command_without_refreshed_state_and_domain_denial_are_exact() -> None:
    _, commands = _controllers(_spec())
    accepted = commands.execute(_command())
    assert accepted.accepted is True
    assert accepted.model == {}

    _, commands = _controllers(
        _spec(
            command_handler=lambda _command: ProjectionCommandOutcome(
                False,
                "not_found",
                revision=7,
                state={"items": []},
            )
        )
    )
    denied = commands.execute(_command())
    assert denied.accepted is False
    assert denied.code == "not_found"
    assert denied.revision == 7
    assert denied.model["surface"] == "agents"


def test_command_identity_override_and_authorization_denial_prevent_mutation() -> None:
    calls: list[str] = []
    spec = _spec(
        command_authorizer=lambda _command: calls.append("authorize") or True,
        command_handler=lambda _command: (
            calls.append("mutate") or _command_outcome(_command)
        ),
    )
    _, commands = _controllers(spec)
    invalid = commands.execute(_command(payload={"user-id": "owner-bob"}))
    assert calls == []
    assert invalid.code == "invalid_identity"

    _, commands = _controllers(
        _spec(
            command_authorizer=lambda _command: False,
            command_handler=lambda _command: (
                calls.append("mutate") or _command_outcome(_command)
            ),
        )
    )
    denied = commands.execute(_command())
    assert denied.code == "forbidden"
    assert "mutate" not in calls


def test_command_authorization_failure_is_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken_auth(_command: PresentationCommand) -> bool:
        raise RuntimeError("authorization-token=secret")

    _, commands = _controllers(_spec(command_authorizer=broken_auth))
    result = commands.execute(_command())
    assert result.accepted is False
    assert result.code == "forbidden"
    assert "authorization-token=secret" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_unknown_command_surface_and_action_fail_closed() -> None:
    _, commands = _controllers(_spec())
    unknown_surface = commands.execute(
        PresentationCommand("guide", "chrome_open", CONTEXT)
    )
    assert unknown_surface.code == "unsupported_surface"
    assert unknown_surface.model["degradation"]["active"] is True

    unknown_action = commands.execute(_command("chrome_unknown"))
    assert unknown_action.code == "unsupported_action"
    assert (
        unknown_action.model["components"][0]["value"]["code"] == "unsupported_action"
    )


@pytest.mark.parametrize(
    "outcome",
    [
        object(),
        ProjectionCommandOutcome(1, "updated"),  # type: ignore[arg-type]
        ProjectionCommandOutcome(True, "bad code"),
        ProjectionCommandOutcome(True, "updated", revision=-1),
    ],
)
def test_invalid_command_outcome_returns_safe_failure(outcome: object) -> None:
    _, commands = _controllers(_spec(command_handler=lambda _command: outcome))
    result = commands.execute(_command())
    assert result.accepted is False
    assert result.code == "command_failed"


def test_command_exception_and_status_builder_failure_do_not_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken_command(_command: PresentationCommand) -> ProjectionCommandOutcome:
        raise RuntimeError("credential-secret=do-not-log")

    def broken_status(_error: ProjectionPublicError) -> _ViewModel:
        raise RuntimeError("builder-secret")

    _, commands = _controllers(
        _spec(command_handler=broken_command, failure_builder=broken_status)
    )
    result = commands.execute(_command())
    assert result.code == "command_failed"
    assert result.model["components"][0]["code"] == "command_failed"
    assert "credential-secret=do-not-log" not in caplog.text
    assert "builder-secret" not in caplog.text


def test_post_mutation_view_failure_preserves_known_mutation_outcome() -> None:
    def broken_view(_state: object) -> _ViewModel:
        raise RuntimeError("rendering-secret")

    _, commands = _controllers(
        _spec(
            command_handler=lambda _command: ProjectionCommandOutcome(
                True,
                "updated",
                revision=9,
                state={"items": []},
            ),
            view_builder=broken_view,
        )
    )
    result = commands.execute(_command())
    assert result.accepted is True
    assert result.code == "updated"
    assert result.revision == 9
    assert result.model == {}
