import logging
from typing import Dict, List, Optional, Tuple
from uuid import UUID

from pydantic import BaseModel, Field

from backend.orchestrator.models import Skill, SkillRevision, SkillStatus
from backend.orchestrator.skill_packs import _build_skill_digest
from backend.orchestrator.work_submit import WorkGuidance

logger = logging.getLogger(__name__)

# Existing classes and methods...

class UserSkillCatalog:
    """Manages user-defined skills and their revisions."""

    def __init__(self):
        # In a real system, this would interact with a database.
        # For MVP, we'll use an in-memory dictionary.
        self._skills: Dict[UUID, Dict[int, SkillRevision]] = {}
        self._skill_metadata: Dict[UUID, Skill] = {} # Stores current metadata for skills

    def add_skill_revision(self, owner_id: UUID, skill_id: UUID, revision: SkillRevision) -> None:
        """Adds a new revision for a skill."""
        if skill_id not in self._skills:
            self._skills[skill_id] = {}
            self._skill_metadata[skill_id] = Skill(
                id=skill_id,
                owner_id=owner_id,
                name=revision.name,
                description=revision.description,
                status=SkillStatus.ENABLED, # Default to enabled on creation
                latest_revision_id=revision.revision_id
            )
        
        # Ensure owner matches
        if self._skill_metadata[skill_id].owner_id != owner_id:
            raise PermissionError(f"Skill {skill_id} does not belong to owner {owner_id}")

        self._skills[skill_id][revision.revision_id] = revision
        # Update latest revision ID if this is a newer revision
        if revision.revision_id > self._skill_metadata[skill_id].latest_revision_id:
            self._skill_metadata[skill_id].latest_revision_id = revision.revision_id
        logger.info(f"Added skill {skill_id} revision {revision.revision_id} for owner {owner_id}")

    def get_skill_digest(self, owner_id: UUID, skill_id: UUID, revision_id: Optional[int] = None) -> Optional[Tuple[Skill, str, int]]:
        """
        Retrieves a skill's metadata, its digest, and the actual revision ID used.
        If revision_id is None, returns the latest enabled revision.
        """
        skill_metadata = self._skill_metadata.get(skill_id)
        if not skill_metadata or skill_metadata.owner_id != owner_id:
            logger.warning(f"Skill {skill_id} not found or owner mismatch for {owner_id}")
            return None

        if skill_metadata.status != SkillStatus.ENABLED:
            logger.info(f"Skill {skill_id} is not enabled.")
            return None

        target_revision_id = revision_id if revision_id is not None else skill_metadata.latest_revision_id
        revision = self._skills.get(skill_id, {}).get(target_revision_id)

        if not revision:
            logger.warning(f"Revision {target_revision_id} for skill {skill_id} not found.")
            return None

        digest = _build_skill_digest(revision.name, revision.description, revision.instruction_body)
        return skill_metadata, digest, target_revision_id

    def list_skills_for_owner(self, owner_id: UUID) -> List[Skill]:
        """Lists all skills owned by a specific user."""
        return [
            skill for skill in self._skill_metadata.values()
            if skill.owner_id == owner_id and skill.status == SkillStatus.ENABLED
        ]

    def get_skill_metadata(self, owner_id: UUID, skill_id: UUID) -> Optional[Skill]:
        """Retrieves a skill's metadata without its full instructions."""
        skill_metadata = self._skill_metadata.get(skill_id)
        if not skill_metadata or skill_metadata.owner_id != owner_id:
            return None
        return skill_metadata

    def update_skill_status(self, owner_id: UUID, skill_id: UUID, new_status: SkillStatus) -> bool:
        """Updates the status of a skill."""
        skill_metadata = self._skill_metadata.get(skill_id)
        if not skill_metadata or skill_metadata.owner_id != owner_id:
            logger.warning(f"Attempt to update status for skill {skill_id} by unauthorized owner {owner_id}")
            return False
        skill_metadata.status = new_status
        logger.info(f"Updated skill {skill_id} status to {new_status}")
        return True

    def load_full_skill_revision(
        self,
        caller_id: UUID,
        skill_id: UUID,
        revision_id: int,
        max_instruction_length: int = 12000 # Example budget, coordinate with #316
    ) -> Optional[Tuple[Skill, str, int]]:
        """
        Loads the full instruction body for a specific skill revision,
        performing authorization checks and budget enforcement.

        Args:
            caller_id: The ID of the entity requesting the skill (e.g., user, agent).
            skill_id: The ID of the skill.
            revision_id: The specific revision ID to load.
            max_instruction_length: The maximum allowed length for the full instruction body.

        Returns:
            A tuple of (Skill metadata, full instruction body, actual revision ID)
            if authorized and within budget, otherwise None.
        """
        skill_metadata = self._skill_metadata.get(skill_id)
        if not skill_metadata:
            logger.warning(f"Skill {skill_id} not found for caller {caller_id}.")
            return None

        # 1. Authorization Check: Owner scope
        if skill_metadata.owner_id != caller_id:
            logger.warning(f"Caller {caller_id} is not the owner of skill {skill_id}. Access denied.")
            return None

        # 2. Authorization Check: Enabled/Applicable status
        if skill_metadata.status != SkillStatus.ENABLED:
            logger.info(f"Skill {skill_id} is not enabled. Access denied for caller {caller_id}.")
            return None

        # 3. Retrieve specific revision
        revision = self._skills.get(skill_id, {}).get(revision_id)
        if not revision:
            logger.warning(f"Revision {revision_id} for skill {skill_id} not found for caller {caller_id}.")
            return None

        full_instruction_body = revision.instruction_body

        # 4. Token Accounting and Budget Enforcement
        # This is a simple character length check. For full token accounting,
        # integrate with the mechanism from #316 (e.g., a tokenizer).
        if len(full_instruction_body) > max_instruction_length:
            logger.warning(
                f"Full instruction body for skill {skill_id} revision {revision_id} "
                f"exceeds max_instruction_length ({len(full_instruction_body)} > {max_instruction_length}). "
                "Refusing to load full guidance."
            )
            # Report omission/refusal explicitly
            return None

        logger.info(f"Successfully loaded full skill {skill_id} revision {revision_id} for caller {caller_id}.")
        return skill_metadata, full_instruction_body, revision_id

# Re-evaluate _build_skill_digest to ensure it correctly indicates truncation
# This function is in skill_packs.py, but we might need to adjust its output
# or how it's used to indicate if the digest is incomplete.
# For now, the existing _build_skill_digest is fine as it always truncates.
# The new load_full_skill_revision explicitly returns the full body or None.
