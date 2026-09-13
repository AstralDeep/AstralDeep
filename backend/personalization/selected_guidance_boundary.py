"""Exact selected-row capture under caller-owned authority and ordered locks.

This boundary checks application identity, not IAM, permission, or dispatch.
Its caller holds owner/session and any assignment/action locks, supplies a fresh
database-time observation, and performs the final Plane selected-input assertion
after all other waits. No durable prompt or latest-head fallback is returned.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType

from astralplane.repositories.agents import AgentRevisionRecord, UserAgentRecord
from astralplane.repositories.guidance_models import (
    ExplicitNoteRecord, GuidanceReference, SkillHead,
)
from persistent_agents.models import AssignmentError
from personalization.explicit_note_expansion import capture_note_reference
from personalization.explicit_notes import ExplicitNoteUnavailable
from personalization.selected_guidance import (
    SelectedGuidanceUnavailable, prepare_selected_guidance,
    reconstruct_selected_research_input,
)


@dataclass(frozen=True, slots=True, repr=False)
class CapturedSelectedGuidance:
    prepared: object
    agent_revision: object
    _inputs: object = field(repr=False)
    _boundary: object = field(repr=False)

    def compose(self, *, observation, approved_request_bytes):
        """Use only captured rows; exact stable MAC detects any input mutation."""
        self._boundary.assert_current()
        return reconstruct_selected_research_input(self.prepared.envelope,
            **self._inputs, observation=observation, approved_request_bytes=approved_request_bytes)


class SelectedGuidanceBoundary:
    def __init__(self, orchestrator, *, plane_runtime, include_notes):
        if type(include_notes) is not bool:
            raise AssignmentError("work_selection_unavailable", 503)
        self.orch = orchestrator
        self.runtime = plane_runtime
        self.repositories = plane_runtime.repositories
        self.note_service = getattr(orchestrator, "explicit_notes", None) if include_notes else None
        self.include_notes = include_notes
        self.assert_current()

    def assert_current(self):
        """Local identity only. A caller must separately prove current authority."""
        plane = getattr(getattr(self.orch, "runtime_composition", None), "plane", None)
        if (getattr(plane, "runtime", None) is not self.runtime
                or getattr(plane, "repositories", None) is not self.repositories
                or self.runtime.repositories is not self.repositories):
            raise AssignmentError("work_selection_unavailable", 503)
        if self.include_notes:
            from personalization.explicit_note_service import ExplicitNoteService
            service = self.note_service
            if (type(service) is not ExplicitNoteService
                    or getattr(self.orch, "explicit_notes", None) is not service
                    or service.orch is not self.orch or service.runtime is not self.runtime):
                raise AssignmentError("work_selection_unavailable", 503)
            service._current()

    def capture(self, tx, *, owner_id, instruction, agent, references, binding_key, now):
        """Capture requested current rows under the caller's existing owner79 lock."""
        self.assert_current()
        if (type(references) is not tuple or any(type(ref) is not GuidanceReference for ref in references)
                or len({(r.kind, r.resource_id) for r in references}) != len(references)
                or sum(r.kind == "skill" for r in references) > 20
                or sum(r.kind == "note" for r in references) > 8
                or any(r.kind == "note" for r in references) != self.include_notes
                or type(now) is not datetime or now.tzinfo is None
                or (agent is None and not references)):
            raise AssignmentError("work_selection_unavailable", 503)
        for ref in references:
            ref.__post_init__()
        row = None
        if agent is not None:
            if type(agent) is not tuple or len(agent) != 2:
                raise AssignmentError("work_selection_unavailable", 503)
            agent_id, revision_id = agent
            head = self.repositories.agents.get_agent(tx, owner_id=owner_id, agent_id=agent_id)
            if (type(head) is not UserAgentRecord or head.owner_id != owner_id
                    or head.agent_kind != "declarative" or head.status != "active"
                    or head.deleted_at is not None or head.selected_definition_revision_id != revision_id):
                raise AssignmentError("work_selection_not_found", 404)
            row = self.repositories.agents.get_revision(tx, owner_id=owner_id,
                agent_id=agent_id, revision_id=revision_id)
            if (type(row) is not AgentRevisionRecord or row.owner_id != owner_id
                    or row.agent_id != agent_id or row.revision_id != revision_id):
                raise AssignmentError("work_selection_not_found", 404)
        skills, notes, note_refs = [], [], []
        now_ms = int(now.timestamp() * 1000)
        try:
            for ref in sorted(references, key=lambda item: (item.kind, item.resource_id)):
                if ref.kind == "skill":
                    head = self.repositories.preferences.skills.get(tx, owner_id=owner_id, skill_id=ref.resource_id)
                    if (type(head) is not SkillHead or head.owner_id != owner_id or head.revision != ref.revision
                            or not head.enabled or head.deleted_at is not None):
                        raise AssignmentError("work_selection_not_found", 404)
                    revision = self.repositories.preferences.skills.get_revision(tx, owner_id=owner_id,
                        skill_id=ref.resource_id, revision=ref.revision)
                    if (revision is None or revision.definition_digest != head.definition_digest
                            or revision.owner_id != owner_id or revision.skill_id != ref.resource_id
                            or revision.revision != ref.revision):
                        raise AssignmentError("work_selection_unavailable", 503)
                    skills.append(revision)
                else:
                    note = self.repositories.preferences.personalization.get_explicit_note(
                        tx, owner_id=owner_id, note_id=ref.resource_id, include_disabled=False)
                    if (type(note) is not ExplicitNoteRecord or note.owner_id != owner_id
                            or note.note_id != ref.resource_id or note.revision != ref.revision):
                        raise AssignmentError("work_selection_not_found", 404)
                    encrypted = self.note_service._encrypted(note)
                    notes.append(encrypted)
                    note_refs.append(capture_note_reference(encrypted, cipher=self.note_service.cipher,
                        binding_key=binding_key, owner_id=owner_id, now_ms=now_ms))
            inputs = dict(owner_id=owner_id, instruction=instruction, now_ms=now_ms,
                agent=row, skills=tuple(skills), current_notes=tuple(notes), note_references=tuple(note_refs),
                cipher=None if self.note_service is None else self.note_service.cipher, binding_key=binding_key)
            prepared = prepare_selected_guidance(**inputs, approved_guidance_bytes=65536)
        except (SelectedGuidanceUnavailable, ExplicitNoteUnavailable):
            raise AssignmentError("work_selection_unavailable", 503) from None
        self.assert_current()
        return CapturedSelectedGuidance(prepared, row, MappingProxyType(inputs), self)
