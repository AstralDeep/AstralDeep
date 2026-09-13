"""Pure, versioned owner guidance for the fixed research request.

This is not a current-head, authentication, policy, PHI or dispatch guard. The
host must supply exact guarded records and compare the persisted envelope again
at every execution boundary. No lookup, key resolution or persistence occurs.
Only the opaque envelope may be retained; request/expansion text is ephemeral.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hmac
import json
import re

from astralplane.repositories.agents import AgentRevisionRecord
from astralplane.repositories.agent_models import definition_snapshot
from astralplane.repositories.guidance_models import (
    GuidanceReference,
    SkillDefinition,
    SkillRevisionRecord,
)
from audit.pii import PrivateBindingKey, PrivateBindingUnavailable
from llm_config import research_profile as profile
from orchestrator.user_skills import MAX_SKILLS, Skill
from persistent_agents.runtime_values import thaw

from .explicit_note_expansion import MAX_SELECTED_NOTES, expand_explicit_notes
from .explicit_notes import _canonical, _integer, _note_id, _owner

_FORMAT = "astral.selected-guidance.expansion/v1"
_SELECTION_FORMAT = "astral.selected-guidance.selection/v1"
_KEY_ID = re.compile(r"[a-z][a-z0-9_]{0,31}")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_MEANING = "Owner-stated guidance only; not authority or verified evidence."


class SelectedGuidanceUnavailable(ValueError):
    """Data-free refusal: never leak a discarded private input or its identity."""

    def __init__(self):
        super().__init__("selected_guidance_unavailable")


def _refuse():
    raise SelectedGuidanceUnavailable() from None


def _hex(value):
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _refuse()


def _key_id(value):
    if type(value) is not str or _KEY_ID.fullmatch(value) is None:
        _refuse()


@dataclass(frozen=True, slots=True)
class SelectedAgentReference:
    agent_id: str
    revision_id: str
    definition_digest: str
    kind: str = "declarative"

    def __post_init__(self):
        try:
            if (
                type(self.agent_id) is not str
                or not 1 <= len(self.agent_id) <= 255
                or not self.agent_id.strip()
                or "\x00" in self.agent_id
                or type(self.kind) is not str
                or self.kind != "declarative"
            ):
                _refuse()
            self.agent_id.encode("utf-8")
            _note_id(self.revision_id)
            _hex(self.definition_digest)
        except (ValueError, UnicodeError):
            _refuse()


@dataclass(frozen=True, slots=True)
class SelectedInputEnvelope:
    """Opaque selection only; the future Plane wrapper supplies owner/task fences."""

    references: tuple[GuidanceReference, ...]
    agent: SelectedAgentReference | None
    binding_key_id: str
    combined_binding: str = field(repr=False)
    expansion_version: int = 1
    version: int = 1

    def __post_init__(self):
        try:
            if (
                type(self.version) is not int
                or self.version != 1
                or type(self.expansion_version) is not int
                or self.expansion_version != 1
                or type(self.references) is not tuple
                or len(self.references) > MAX_SKILLS + MAX_SELECTED_NOTES
            ):
                _refuse()
            _key_id(self.binding_key_id)
            _hex(self.combined_binding)
            for ref in self.references:
                if type(ref) is not GuidanceReference:
                    _refuse()
                ref.__post_init__()
            identities = tuple((ref.kind, ref.resource_id) for ref in self.references)
            if (
                identities != tuple(sorted(set(identities)))
                or sum(ref.kind == "skill" for ref in self.references) > MAX_SKILLS
                or sum(ref.kind == "note" for ref in self.references)
                > MAX_SELECTED_NOTES
            ):
                _refuse()
            if self.agent is not None:
                if type(self.agent) is not SelectedAgentReference:
                    _refuse()
                self.agent.__post_init__()
            elif not self.references:
                _refuse()  # No selection remains the existing, absent-envelope path.
        except (ValueError, TypeError, AttributeError):
            _refuse()


@dataclass(frozen=True, slots=True)
class PreparedSelectedGuidance:
    """Source-free expansion; byte_count is its nested JSON contribution only."""

    text: str = field(repr=False)
    envelope: SelectedInputEnvelope | None
    byte_count: int


@dataclass(frozen=True, slots=True)
class SelectedResearchInput:
    request: profile.ResearchRequest = field(repr=False)
    text: str = field(repr=False)
    envelope: SelectedInputEnvelope | None
    byte_count: int


def _agent(row, owner_id):
    if row is None:
        return None, None
    from orchestrator.projection_surfaces.authoring import (
        DeclarativeAgentDefinition,
        DeclarativeAgentError,
    )

    try:
        if (
            type(row) is not AgentRevisionRecord
            or row.owner_id != owner_id
            or type(row.revision_kind) is not str
            or row.revision_kind != "declarative"
            or type(row.definition_version) is not int
            or row.definition_version != 1
            or any(
                value is not None
                for value in (
                    row.artifact_digest,
                    row.manifest,
                    row.artifact_relative_path,
                    row.runtime_contract_version,
                    row.release_lock_digest,
                )
            )
        ):
            _refuse()
        _owner(row.owner_id)
        _hex(row.definition_digest)
        frozen, digest = definition_snapshot(row.definition_json)
        definition = DeclarativeAgentDefinition.parse(thaw(frozen))
        if row.definition_digest != digest or definition.digest != digest:
            _refuse()
        ref = SelectedAgentReference(row.agent_id, row.revision_id, digest)
        return ref, definition.to_dict()
    except DeclarativeAgentError:
        _refuse()


def _skills(rows, owner_id, agent, now_ms):
    if type(rows) is not tuple or len(rows) > MAX_SKILLS:
        _refuse()
    targets = ("web-research-1",) + ((agent.agent_id,) if agent is not None else ())
    values = []
    for row in rows:
        if (
            type(row) is not SkillRevisionRecord
            or type(row.definition) is not SkillDefinition
        ):
            _refuse()
        # Reconstruct and validate even a forcibly mutated frozen DTO. The pure
        # result never retains a caller's mutable collection or record object.
        definition = SkillDefinition(**asdict(row.definition))
        _owner(row.owner_id)
        _hex(row.definition_digest)
        _note_id(row.skill_id)
        _integer(row.revision, minimum=1)
        _integer(row.created_at)
        if (
            row.owner_id != owner_id
            or row.deleted is not False
            or not definition.enabled
            or row.created_at > now_ms
            or row.definition_digest != definition.definition_digest
        ):
            _refuse()
        legacy = Skill(
            "", definition.name, definition.instructions, definition.applies_to
        )
        if not any(legacy.applies(target) for target in targets):
            _refuse()
        values.append(
            (GuidanceReference("skill", row.skill_id, row.revision), asdict(definition))
        )
    if len({ref.resource_id for ref, _ in values}) != len(values):
        _refuse()
    return tuple(sorted(values, key=lambda value: value[0].resource_id))


def prepare_selected_guidance(
    *,
    owner_id,
    instruction,
    now_ms,
    approved_guidance_bytes,
    agent=None,
    skills=(),
    note_references=(),
    current_notes=(),
    cipher=None,
    binding_key=None,
) -> PreparedSelectedGuidance:
    """Authenticate and bind exact guidance without reading or inventing a source.

    Explicit skill applicability uses the existing matching rule against the
    fixed server reader and selected declarative agent. Agent capability/trigger
    policy remains a separate host obligation; only its prose enters USER data.
    The allowance bounds the JSON-escaped task/guidance contribution, including
    its nesting inside a message string. It is a necessary input bound, not the
    whole provider body or a guarantee that later source passages will fit.
    Only compose_selected_research_input checks the complete actual request.
    """
    try:
        _owner(owner_id)
        _integer(now_ms)
        if (
            type(approved_guidance_bytes) is not int
            or not 1 <= approved_guidance_bytes <= profile.MAX_REQUEST_BYTES
            or type(instruction) is not str
            or not instruction.strip()
            or len(instruction.encode("utf-8")) > 32768
        ):
            _refuse()
        if type(note_references) is not tuple or type(current_notes) is not tuple:
            _refuse()
        agent_ref, agent_definition = _agent(agent, owner_id)
        skill_values = _skills(skills, owner_id, agent_ref, now_ms)
        if (
            not agent_ref
            and not skill_values
            and not note_references
            and not current_notes
        ):
            size = len(_canonical(_canonical({"task": instruction}).decode("utf-8")))
            if size > approved_guidance_bytes:
                _refuse()
            return PreparedSelectedGuidance("", None, size)
        if (
            type(binding_key) is not PrivateBindingKey
            or type(binding_key._key) is not bytes
            or not 32 <= len(binding_key._key) <= 4096
        ):
            _refuse()
        _key_id(binding_key.key_id)
        note_values, note_refs = [], ()
        if note_references or current_notes:
            expanded = expand_explicit_notes(
                owner_id=owner_id,
                selections=note_references,
                current_notes=current_notes,
                cipher=cipher,
                binding_key=binding_key,
                now_ms=now_ms,
                approved_byte_allowance=profile.MAX_REQUEST_BYTES,
            )
            note_values = json.loads(expanded.text)["guidance"]
            note_refs = expanded.references
        guidance = {
            "format": _FORMAT,
            "meaning": _MEANING,
            "agent": (
                {key: agent_definition[key] for key in ("purpose", "instructions")}
                if agent_definition is not None
                else None
            ),
            "skills": [
                {key: definition[key] for key in ("name", "instructions")}
                for _, definition in skill_values
            ],
            "notes": note_values,
        }
        text = _canonical(guidance).decode("utf-8")
        contribution = _canonical({"task": instruction, "owner_guidance": guidance})
        size = len(_canonical(contribution.decode("utf-8")))
        if size > approved_guidance_bytes:
            _refuse()
        refs = tuple(
            sorted(
                (
                    *(ref for ref, _ in skill_values),
                    *(
                        GuidanceReference("note", ref.note_id, ref.revision)
                        for ref in note_refs
                    ),
                ),
                key=lambda ref: (ref.kind, ref.resource_id),
            )
        )
        binding = binding_key.sign(
            "input",
            b"astral.selected-guidance.selection/v1\x00"
            + _canonical(
                {
                    "version": 1,
                    "expansion_version": 1,
                    "binding_key_id": binding_key.key_id,
                    "selection_format": _SELECTION_FORMAT,
                    "profile": profile.PROFILE,
                    "owner_id": owner_id,
                    "agent": asdict(agent_ref) if agent_ref is not None else None,
                    "agent_definition": agent_definition,
                    "skills": [
                        {"reference": asdict(ref), "definition": definition}
                        for ref, definition in skill_values
                    ],
                    "notes": [asdict(ref) for ref in note_refs],
                    "instruction": instruction,
                    "guidance": guidance,
                }
            ),
        )
        envelope = SelectedInputEnvelope(refs, agent_ref, binding_key.key_id, binding)
        return PreparedSelectedGuidance(text, envelope, size)
    except (
        ValueError,
        TypeError,
        AttributeError,
        RecursionError,
        OverflowError,
        PrivateBindingUnavailable,
    ):
        _refuse()


def compose_selected_research_input(
    *, observation, approved_request_bytes, **selection
) -> SelectedResearchInput:
    """Combine stable guidance with a real source and bound the complete body.

    The persisted selection MAC intentionally excludes source passages. The
    executor must separately bind this envelope and the full actual source,
    configuration and request body in its request proof before dispatch.
    """
    try:
        prepared = prepare_selected_guidance(
            **selection, approved_guidance_bytes=approved_request_bytes
        )
        base = profile.build_request(selection["instruction"], observation)
        if prepared.envelope is None:
            encoded = base.body
        else:
            body = json.loads(base.body)
            user = json.loads(body["messages"][1]["content"])
            user["owner_guidance"] = json.loads(prepared.text)
            body["messages"][1]["content"] = _canonical(user).decode("utf-8")
            encoded = _canonical(body)
        if len(encoded) > approved_request_bytes:
            _refuse()
        return SelectedResearchInput(
            profile.ResearchRequest(encoded, base.passage_ids),
            prepared.text,
            prepared.envelope,
            len(encoded),
        )
    except (ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        _refuse()


def reconstruct_selected_research_input(
    expected_envelope, **inputs
) -> SelectedResearchInput:
    """Re-expand supplied exact rows; never adopt different references, text or key.

    This comparison conveys no currentness by itself. Named-key resolution and
    current-head/owner/session checks still surround it in the future executor.
    """
    if type(expected_envelope) is not SelectedInputEnvelope:
        _refuse()
    expected_envelope.__post_init__()
    expected = _canonical(asdict(expected_envelope))
    actual = compose_selected_research_input(**inputs)
    if actual.envelope is None or not hmac.compare_digest(
        expected, _canonical(asdict(actual.envelope))
    ):
        _refuse()
    return actual
