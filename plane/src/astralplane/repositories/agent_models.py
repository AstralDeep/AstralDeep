"""Detached command and receipt types for declarative agent authoring; carry no
execution, consent, or provider authority of their own. Consumed by
repositories/agents.py, which validates policy and caller before committing.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from astralplane.repositories import RepositoryValidationError, _canonical_json, _freeze

if TYPE_CHECKING:
    from astralplane.repositories.agents import AgentRevisionRecord, UserAgentRecord


def _integer(value: object, name: str, *, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise RepositoryValidationError(f"{name} must be a bounded integer")
    return value


def _text(value: object, name: str, maximum: int = 512) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum or "\x00" in value:
        raise RepositoryValidationError(f"{name} must be bounded text")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise RepositoryValidationError(f"{name} contains invalid Unicode") from exc
    return value


def _uuid(value: object, name: str) -> str:
    try:
        parsed = uuid.UUID(value) if type(value) is str else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.version != 4 or str(parsed) != value:
        raise RepositoryValidationError(f"{name} must be a canonical UUID4")
    return value


def definition_snapshot(value: object) -> tuple[Mapping[str, Any], str]:
    if not isinstance(value, Mapping) or type(value.get("version")) is not int:
        raise RepositoryValidationError("definition must be a versioned object")
    if value["version"] != 1:
        raise RepositoryValidationError("unsupported definition version")
    count = 0

    def inspect(item: object, depth: int) -> None:
        nonlocal count
        count += 1
        if depth > 16 or count > 4096:
            raise RepositoryValidationError("definition exceeds structural bounds")
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if type(key) is not str or "\x00" in key:
                    raise RepositoryValidationError("definition keys must be strings")
                inspect(nested, depth + 1)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                inspect(nested, depth + 1)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise RepositoryValidationError("definition contains unsupported data")
        elif type(item) is str and "\x00" in item:
            raise RepositoryValidationError("definition contains invalid Unicode")

    inspect(value, 0)
    try:
        encoded = _canonical_json(value, "definition").encode("utf-8")
    except UnicodeError as exc:
        raise RepositoryValidationError("definition contains invalid Unicode") from exc
    if len(encoded) > 65536:
        raise RepositoryValidationError("definition exceeds encoded bounds")
    return _freeze(json.loads(encoded)), hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class DeclarativeAgentCommand:
    owner_id: str = field(repr=False)
    agent_id: str
    command_id: str
    command: str
    expected_revision: int | None = None
    revision_id: str | None = None
    parent_revision_id: str | None = None
    display_name: str | None = field(default=None, repr=False)
    definition: Mapping[str, Any] | None = field(default=None, repr=False)
    source_agent_id: str | None = None
    source_revision_id: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        self.validate()
        if self.definition is not None:
            object.__setattr__(self, "definition", definition_snapshot(self.definition)[0])

    def validate(self) -> None:
        _text(self.owner_id, "owner_id")
        _text(self.agent_id, "agent_id", 255)
        _uuid(self.command_id, "command_id")
        if type(self.version) is not int or self.version != 1:
            raise RepositoryValidationError("unsupported command version")
        shapes = {
            "create": {"revision_id", "display_name", "definition"},
            "revise": {
                "expected_revision",
                "revision_id",
                "parent_revision_id",
                "display_name",
                "definition",
            },
            "activate": {"expected_revision", "revision_id"},
            "archive": {"expected_revision"},
            "delete": {"expected_revision"},
            "clone": {"revision_id", "display_name", "source_agent_id", "source_revision_id"},
        }
        optional = (
            "expected_revision",
            "revision_id",
            "parent_revision_id",
            "display_name",
            "definition",
            "source_agent_id",
            "source_revision_id",
        )
        if type(self.command) is not str or self.command not in shapes:
            raise RepositoryValidationError("unsupported declarative command")
        if {name for name in optional if getattr(self, name) is not None} != shapes[self.command]:
            raise RepositoryValidationError("declarative command fields do not match its kind")
        if self.expected_revision is not None:
            _integer(self.expected_revision, "expected_revision")
        for name in ("revision_id", "parent_revision_id", "source_revision_id"):
            if getattr(self, name) is not None:
                _uuid(getattr(self, name), name)
        if self.display_name is not None:
            _text(self.display_name, "display_name", 1024)
        if self.source_agent_id is not None:
            _text(self.source_agent_id, "source_agent_id", 255)
            if self.source_agent_id == self.agent_id:
                raise RepositoryValidationError("clone requires a distinct new identity")
        if self.definition is not None:
            definition_snapshot(self.definition)

    @property
    def request_digest(self) -> str:
        self.validate()
        value = {
            "version": self.version,
            "kind": "declarative_agent_command",
            "owner_id": self.owner_id,
            "agent_id": self.agent_id,
            "command": self.command,
        }
        for name in (
            "expected_revision",
            "revision_id",
            "parent_revision_id",
            "display_name",
            "source_agent_id",
            "source_revision_id",
        ):
            if getattr(self, name) is not None:
                value[name] = getattr(self, name)
        if self.definition is not None:
            value["definition_digest"] = definition_snapshot(self.definition)[1]
        return hashlib.sha256(_canonical_json(value, "command").encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DeclarativeAgentReceipt:
    owner_id: str = field(repr=False)
    agent_id: str
    command_id: str
    command: str
    request_digest: str
    result_state_revision: int
    result_definition_revision_id: str | None
    created_at: datetime
    version: int = 1


@dataclass(frozen=True, slots=True)
class DeclarativeAgentPreparation:
    command: DeclarativeAgentCommand = field(repr=False)
    request_digest: str
    agent: UserAgentRecord | None = field(repr=False)
    revision: AgentRevisionRecord | None = field(repr=False)
    receipt: DeclarativeAgentReceipt | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class DeclarativeAgentResult:
    agent: UserAgentRecord = field(repr=False)
    revision: AgentRevisionRecord | None = field(repr=False)
    receipt: DeclarativeAgentReceipt
    replayed: bool
