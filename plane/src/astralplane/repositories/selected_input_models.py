"""Immutable references identifying a selected agent or private input, never their
expanded values or authority. Host authenticates the key and binding; used by
repositories/assignments.py and orchestrator/work_submit.py.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from astralplane.repositories import RepositoryValidationError
from astralplane.repositories.guidance_models import (
    GuidanceReference,
    digest,
    identifier,
    integer,
    owner,
    text,
)


def references(value):
    if type(value) is not tuple or any(type(ref) is not GuidanceReference for ref in value):
        raise RepositoryValidationError("typed selected references required")
    copied = tuple(GuidanceReference(ref.kind, ref.resource_id, ref.revision) for ref in value)
    if (
        sum(ref.kind == "skill" for ref in copied) > 20
        or sum(ref.kind == "note" for ref in copied) > 8
        or len({(ref.kind, ref.resource_id) for ref in copied}) != len(copied)
    ):
        raise RepositoryValidationError("selected references exceed bounds")
    return tuple(sorted(copied, key=lambda ref: (ref.kind, ref.resource_id)))


@dataclass(frozen=True, slots=True)
class SelectedAgentReference:
    agent_id: str
    revision_id: str
    definition_digest: str = field(repr=False)
    kind: str = "declarative"

    def __post_init__(self):
        text(self.agent_id, maximum=255)
        if self.agent_id != self.agent_id.strip():
            raise RepositoryValidationError("invalid selected agent identity")
        identifier(self.revision_id)
        digest(self.definition_digest)
        if type(self.kind) is not str or self.kind != "declarative":
            raise RepositoryValidationError("unsupported selected agent kind")


@dataclass(frozen=True, slots=True)
class SelectedInputEnvelope:
    references: tuple[GuidanceReference, ...] = field(repr=False)
    agent: SelectedAgentReference | None = field(repr=False)
    binding_key_id: str = field(repr=False)
    combined_binding: str = field(repr=False)
    expansion_version: int = 1
    version: int = 1

    def __post_init__(self):
        object.__setattr__(self, "references", references(self.references))
        if self.agent is not None:
            if type(self.agent) is not SelectedAgentReference:
                raise RepositoryValidationError("typed selected agent required")
            object.__setattr__(self, "agent", SelectedAgentReference(**asdict(self.agent)))
        if not self.references and self.agent is None:
            raise RepositoryValidationError("empty selection has no input envelope")
        if (
            type(self.version) is not int
            or self.version != 1
            or type(self.expansion_version) is not int
            or self.expansion_version != 1
            or type(self.binding_key_id) is not str
            or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", self.binding_key_id) is None
        ):
            raise RepositoryValidationError("unsupported selected input envelope")
        digest(self.combined_binding)


@dataclass(frozen=True, slots=True)
class AssignmentSelectedInput:
    owner_id: str = field(repr=False)
    assignment_id: str
    instruction_revision: int
    envelope: SelectedInputEnvelope | None = field(repr=False)
    references: tuple[GuidanceReference, ...] = field(repr=False)

    def __post_init__(self):
        owner(self.owner_id)
        identifier(self.assignment_id)
        integer(self.instruction_revision, minimum=1)
        object.__setattr__(self, "references", references(self.references))
        if self.envelope is not None:
            copied = copy_envelope(self.envelope)
            if copied.references != self.references:
                raise RepositoryValidationError("selected envelope references differ")
            object.__setattr__(self, "envelope", copied)


def copy_envelope(value):
    if type(value) is not SelectedInputEnvelope:
        raise RepositoryValidationError("typed selected input envelope required")
    return SelectedInputEnvelope(
        value.references,
        value.agent,
        value.binding_key_id,
        value.combined_binding,
        value.expansion_version,
        value.version,
    )


def decode_envelope(value):
    if (
        type(value) is not dict
        or set(value)
        != {
            "references",
            "agent",
            "binding_key_id",
            "combined_binding",
            "expansion_version",
            "version",
        }
        or type(value["references"]) is not list
    ):
        raise RepositoryValidationError("invalid selected input shape")
    refs = []
    for ref in value["references"]:
        if type(ref) is not dict or set(ref) != {"kind", "resource_id", "revision"}:
            raise RepositoryValidationError("invalid selected reference shape")
        refs.append(GuidanceReference(**ref))
    agent = value["agent"]
    if agent is not None:
        if type(agent) is not dict or set(agent) != {
            "agent_id",
            "revision_id",
            "definition_digest",
            "kind",
        }:
            raise RepositoryValidationError("invalid selected agent shape")
        agent = SelectedAgentReference(**agent)
    result = SelectedInputEnvelope(
        tuple(refs),
        agent,
        value["binding_key_id"],
        value["combined_binding"],
        value["expansion_version"],
        value["version"],
    )
    if tuple(refs) != result.references:
        raise RepositoryValidationError("stored selected references are not canonical")
    return result
