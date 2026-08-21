"""Contract tests for the feature-074 component boundary ports."""

from __future__ import annotations

import ast
import inspect
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, get_args, get_origin, get_type_hints

import pytest

from orchestrator import component_ports
from orchestrator.component_ports import (
    DurableOwner,
    DurableStateCommand,
    DurableStateCommandResult,
    DurableStatePage,
    DurableStateQuery,
    DurableStateRecord,
    DurableStateService,
    DurableStateTransaction,
    PresentationCommand,
    PresentationCommandPort,
    PresentationCommandResult,
    PresentationContext,
    PresentationQuery,
    PresentationQueryPort,
    PresentationView,
)


_FORBIDDEN_COMPONENT_ROOTS = {
    "astralplane",
    "astralprimitives",
    "astralprims",
    "astralprojection",
    "lets",
    "rote",
    "webrender",
}


def _annotation_modules(annotation: Any) -> set[str]:
    modules: set[str] = set()
    module = getattr(annotation, "__module__", None)
    if isinstance(module, str):
        modules.add(module)
    origin = get_origin(annotation)
    if origin is not None:
        origin_module = getattr(origin, "__module__", None)
        if isinstance(origin_module, str):
            modules.add(origin_module)
    for argument in get_args(annotation):
        modules.update(_annotation_modules(argument))
    return modules


def test_component_ports_do_not_import_component_implementations() -> None:
    source_path = Path(component_ports.__file__).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots.isdisjoint(_FORBIDDEN_COMPONENT_ROOTS)


def test_public_annotations_contain_only_host_neutral_types() -> None:
    leaked_modules: set[str] = set()
    for name in component_ports.__all__:
        value = getattr(component_ports, name)
        if inspect.isclass(value):
            leaked_modules.update(
                module
                for annotation in get_type_hints(value).values()
                for module in _annotation_modules(annotation)
            )
            for member in vars(value).values():
                if inspect.isfunction(member):
                    leaked_modules.update(
                        module
                        for annotation in get_type_hints(member).values()
                        for module in _annotation_modules(annotation)
                    )

    assert not any(
        module.split(".", 1)[0] in _FORBIDDEN_COMPONENT_ROOTS
        for module in leaked_modules
    )


class _PresentationAdapter:
    def query(self, query: PresentationQuery, /) -> PresentationView:
        return PresentationView(
            surface=query.surface,
            revision=4,
            model={"operation": query.operation, "items": ["first"]},
        )

    def execute(self, command: PresentationCommand, /) -> PresentationCommandResult:
        return PresentationCommandResult(
            accepted=True,
            code="updated",
            revision=(command.expected_revision or 0) + 1,
            model={"action": command.action},
        )


def test_presentation_values_are_frozen_and_ports_are_structural() -> None:
    source_arguments = {"filters": ["active"], "nested": {"count": 1}}
    context = PresentationContext(
        owner_id="owner-1",
        actor_id="actor-1",
        correlation_id="request-1",
        tenant_id="tenant-1",
    )
    query = PresentationQuery(
        surface="agents",
        operation="list",
        context=context,
        arguments=source_arguments,
    )
    source_arguments["filters"].append("later")
    source_arguments["nested"]["count"] = 2

    assert isinstance(query.arguments, MappingProxyType)
    assert query.arguments == {"filters": ("active",), "nested": {"count": 1}}
    with pytest.raises(TypeError):
        query.arguments["new"] = "value"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        context.owner_id = "someone-else"  # type: ignore[misc]

    adapter = _PresentationAdapter()
    assert isinstance(adapter, PresentationQueryPort)
    assert isinstance(adapter, PresentationCommandPort)
    assert adapter.query(query).model["operation"] == "list"

    result = adapter.execute(
        PresentationCommand(
            surface="agents",
            action="rename",
            context=context,
            arguments={"name": "Research"},
            expected_revision=4,
            idempotency_key="rename-1",
        )
    )
    assert result.accepted is True
    assert result.revision == 5


class _Transaction:
    def __init__(self, record: DurableStateRecord) -> None:
        self.record = record
        self.commands: list[DurableStateCommand] = []

    def fetch_one(self, query: DurableStateQuery, /) -> DurableStateRecord | None:
        return self.record if query.owner == self.record.owner else None

    def fetch_page(self, query: DurableStateQuery, /) -> DurableStatePage:
        record = self.fetch_one(query)
        return DurableStatePage(records=(record,) if record else ())

    def execute(self, command: DurableStateCommand, /) -> DurableStateCommandResult:
        self.commands.append(command)
        return DurableStateCommandResult(
            applied=True,
            code="updated",
            revision=self.record.revision + 1,
            record=self.record,
        )


class _DurableAdapter:
    def __init__(self, transaction: _Transaction) -> None:
        self._transaction = transaction
        self.read_only: bool | None = None

    @contextmanager
    def transaction(
        self, *, read_only: bool = False
    ) -> Iterator[DurableStateTransaction]:
        self.read_only = read_only
        yield self._transaction


def test_durable_state_service_uses_an_explicit_structural_transaction() -> None:
    owner = DurableOwner(namespace="user", owner_id="owner-1")
    record = DurableStateRecord(
        domain="workspace",
        record_id="workspace-1",
        owner=owner,
        revision=3,
        values={"title": "Research", "tags": ["clinical"]},
    )
    transaction = _Transaction(record)
    service = _DurableAdapter(transaction)

    assert isinstance(transaction, DurableStateTransaction)
    assert isinstance(service, DurableStateService)

    with service.transaction(read_only=False) as unit_of_work:
        query = DurableStateQuery(
            domain="workspace",
            operation="by_id",
            owner=owner,
            arguments={"record_id": "workspace-1"},
        )
        assert unit_of_work.fetch_one(query) == record
        result = unit_of_work.execute(
            DurableStateCommand(
                domain="workspace",
                operation="rename",
                owner=owner,
                values={"title": "Updated"},
                expected_revision=3,
                idempotency_key="workspace-rename-1",
            )
        )

    assert service.read_only is False
    assert result.applied is True
    assert result.revision == 4
    assert record.values == {"tags": ("clinical",), "title": "Research"}


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: PresentationContext(
                owner_id=" owner-1",
                actor_id="actor-1",
                correlation_id="request-1",
            ),
            "owner_id",
        ),
        (
            lambda: PresentationQuery(
                surface="agents",
                operation="list",
                context=PresentationContext("owner", "actor", "request"),
                arguments={"unsupported": object()},
            ),
            "host-neutral",
        ),
        (
            lambda: DurableStateRecord(
                domain="workspace",
                record_id="workspace-1",
                owner=DurableOwner("user", "owner"),
                revision=-1,
            ),
            "revision",
        ),
        (
            lambda: DurableStateRecord(
                domain="workspace",
                record_id="workspace-1",
                owner=DurableOwner("user", "owner"),
                revision=None,  # type: ignore[arg-type]
            ),
            "revision",
        ),
    ],
)
def test_boundary_values_fail_closed(factory: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


def test_boundary_values_reject_scalar_subclasses_and_cycles() -> None:
    class ComponentString(str):
        pass

    cyclic: list[object] = []
    cyclic.append(cyclic)
    context = PresentationContext("owner", "actor", "request")

    with pytest.raises(ValueError, match="host-neutral"):
        PresentationQuery(
            surface="agents",
            operation="list",
            context=context,
            arguments={"component_value": ComponentString("value")},
        )
    with pytest.raises(ValueError, match="reference cycle"):
        PresentationQuery(
            surface="agents",
            operation="list",
            context=context,
            arguments={"cycle": cyclic},
        )


def test_boundary_values_reject_excessive_depth() -> None:
    nested: object = "leaf"
    for _ in range(34):
        nested = [nested]

    with pytest.raises(ValueError, match="nesting limit"):
        PresentationView(surface="agents", model={"nested": nested})


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PresentationQuery(
            surface="bad surface",
            operation="list",
            context=PresentationContext("owner", "actor", "request"),
        ),
        lambda: PresentationQuery(
            surface="agents",
            operation="list",
            context=object(),  # type: ignore[arg-type]
        ),
        lambda: PresentationView(surface="agents", revision=-1),
        lambda: PresentationCommand(
            surface="agents",
            action="rename",
            context=object(),  # type: ignore[arg-type]
        ),
        lambda: PresentationCommandResult(accepted=1, code="updated"),  # type: ignore[arg-type]
        lambda: DurableStateQuery(
            domain="workspace",
            operation="list",
            owner=object(),  # type: ignore[arg-type]
        ),
        lambda: DurableStateQuery(
            domain="workspace",
            operation="list",
            owner=DurableOwner("user", "owner"),
            limit=0,
        ),
        lambda: DurableStateRecord(
            domain="workspace",
            record_id="record",
            owner=object(),  # type: ignore[arg-type]
            revision=1,
        ),
        lambda: DurableStatePage(records=(object(),)),  # type: ignore[arg-type]
        lambda: DurableStateCommand(
            domain="workspace",
            operation="update",
            owner=object(),  # type: ignore[arg-type]
        ),
        lambda: DurableStateCommandResult(applied=1, code="updated"),  # type: ignore[arg-type]
        lambda: DurableStateCommandResult(
            applied=True,
            code="updated",
            record=object(),  # type: ignore[arg-type]
        ),
    ],
)
def test_boundary_records_reject_invalid_types_and_bounds(factory: Any) -> None:
    with pytest.raises(ValueError):
        factory()


def test_boundary_values_reject_nonfinite_nonstring_keys_and_oversize_values() -> None:
    context = PresentationContext("owner", "actor", "request")
    with pytest.raises(ValueError, match="finite"):
        PresentationQuery("agents", "list", context, {"score": float("nan")})
    with pytest.raises(ValueError, match="keys must be strings"):
        PresentationQuery("agents", "list", context, {1: "bad"})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="string limit"):
        PresentationQuery(
            "agents",
            "list",
            context,
            {"value": "x" * (component_ports._MAX_BOUNDARY_STRING + 1)},
        )
    with pytest.raises(ValueError, match="item limit"):
        PresentationQuery(
            "agents",
            "list",
            context,
            {"values": list(range(component_ports._MAX_BOUNDARY_ITEMS))},
        )
