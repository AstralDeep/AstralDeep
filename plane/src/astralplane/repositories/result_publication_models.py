"""Closed metadata types for an owner-approved internal canvas publication: proposal,
content, receipt, and preparation. Carry no worker permit or authentication; used by
repositories/result_publications.py and orchestrator/work_publication.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from astralplane.repositories.assignment_models import (
    AssignmentActionRecord,
    AssignmentRecord,
    _Record,
)
from astralplane.repositories.workspaces import PublicationRebaseComponent, PublicationRebaseLayout


@dataclass(frozen=True, slots=True)
class ResultPublicationProposal(_Record):
    action_id: str
    result_action_id: str
    result_digest: str
    publication_id: str
    conversation_id: str
    component_id: str
    base_render_revision: int
    base_publication_id: str | None
    content_digest: str
    stage_digest: str
    permission_digest: str
    precondition_digest: str
    expires_at: datetime
    version: int = 1


@dataclass(frozen=True, slots=True)
class ResultPublicationContent(_Record):
    components: tuple[PublicationRebaseComponent, ...] = field(repr=False)
    layouts: tuple[PublicationRebaseLayout, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class ResultPublicationReceipt(_Record):
    owner_id: str = field(repr=False)
    assignment_id: str
    action_id: str
    request_digest: str
    decision_id: str
    decision_digest: str
    publication_id: str
    conversation_id: str
    component_id: str
    content_digest: str
    stage_digest: str
    result_action_id: str
    result_digest: str
    selected_digest: str = field(repr=False)
    instruction_revision: int
    control_epoch: int
    committed_render_revision: int
    committed_at: datetime
    version: int = 1


@dataclass(frozen=True, slots=True)
class ResultPublicationPreparation(_Record):
    assignment: AssignmentRecord = field(repr=False)
    action: AssignmentActionRecord = field(repr=False)
    proposal: ResultPublicationProposal
    receipt: ResultPublicationReceipt | None
    replayed: bool
