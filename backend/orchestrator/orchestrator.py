import logging
from typing import Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from backend.orchestrator.models import Skill, SkillRevision, SkillStatus
from backend.orchestrator.user_skill_catalog import UserSkillCatalog
from backend.orchestrator.work_submit import WorkGuidance

logger = logging.getLogger(__name__)

class Orchestrator:
    """
    The main orchestrator handling agent interactions, skill selection, and execution.
    """
    def __init__(self, user_skill_catalog: UserSkillCatalog):
        self.user_skill_catalog = user_skill_catalog
        # In a real system, this would manage active turns, model interactions, etc.
        self._active_sessions: Dict[UUID, Dict] = {} # Example: {session_id: {caller_id: UUID, ...}}

    def start_session(self, caller_id: UUID) -> UUID:
        session_id = uuid4()
        self._active_sessions[session_id] = {"caller_id": caller_id, "loaded_skills": {}}
        logger.info(f"Started new session {session_id} for caller {caller_id}")
        return session_id

    def end_session(self, session_id: UUID) -> None:
        if session_id in self._active_sessions:
            del self._active_sessions[session_id]
            logger.info(f"Ended session {session_id}")

    def get_available_skill_digests(self, session_id: UUID) -> List[Tuple[Skill, str, int]]:
        """
        Retrieves digests of available skills for the current session's caller.
        """
        session_data = self._active_sessions.get(session_id)
        if not session_data:
            logger.warning(f"Session {session_id} not found.")
            return []
        
        caller_id = session_data["caller_id"]
        available_skills = self.user_skill_catalog.list_skills_for_owner(caller_id)
        
        digests = []
        for skill_metadata in available_skills:
            # Always get the latest revision for digest listing
            digest_info = self.user_skill_catalog.get_skill_digest(
                caller_id, skill_metadata.id, skill_metadata.latest_revision_id
            )
            if digest_info:
                digests.append(digest_info)
        return digests

    def load_full_skill_for_model(
        self,
        session_id: UUID,
        skill_id: UUID,
        revision_id: int,
        max_length: int = 12000 # Pass budget from orchestrator config
    ) -> Optional[Tuple[Skill, str, int]]:
        """
        Allows the model (via orchestrator) to request a full skill revision.
        This is the new entry point for the model-selectable read path.
        """
        session_data = self._active_sessions.get(session_id)
        if not session_data:
            logger.warning(f"Session {session_id} not found. Cannot load full skill.")
            return None
        
        caller_id = session_data["caller_id"]

        # Check if the skill is already loaded in this session to avoid unnecessary reloads
        # This is a simple in-session cache. More sophisticated caching might be needed.
        if skill_id in session_data["loaded_skills"] and \
           session_data["loaded_skills"][skill_id]["revision_id"] == revision_id:
            logger.info(f"Skill {skill_id} revision {revision_id} already loaded in session {session_id}.")
            return (
                session_data["loaded_skills"][skill_id]["metadata"],
                session_data["loaded_skills"][skill_id]["full_body"],
                session_data["loaded_skills"][skill_id]["revision_id"]
            )

        full_skill_info = self.user_skill_catalog.load_full_skill_revision(
            caller_id=caller_id,
            skill_id=skill_id,
            revision_id=revision_id,
            max_instruction_length=max_length
        )

        if full_skill_info:
            skill_metadata, full_body, actual_revision_id = full_skill_info
            # Cache the loaded skill in the session
            session_data["loaded_skills"][skill_id] = {
                "metadata": skill_metadata,
                "full_body": full_body,
                "revision_id": actual_revision_id
            }
            logger.info(f"Full skill {skill_id} revision {actual_revision_id} loaded and cached for session {session_id}.")
            return full_skill_info
        else:
            logger.warning(f"Failed to load full skill {skill_id} revision {revision_id} for session {session_id}.")
            return None

    def process_model_request(self, session_id: UUID, model_input: str) -> str:
        """
        Simulates a model processing step.
        This is where the model might decide to request a full skill.
        For demonstration, we'll hardcode a trigger.
        """
        session_data = self._active_sessions.get(session_id)
        if not session_data:
            return "Error: Session not found."

        caller_id = session_data["caller_id"]
        response = f"Model processed: '{model_input}'.\n"

        # Example: Model decides it needs a full skill based on input
        if "load full skill" in model_input.lower():
            parts = model_input.lower().split("load full skill")
            if len(parts) > 1:
                try:
                    # Assuming input format like "load full skill <skill_uuid> <revision_id>"
                    skill_info_str = parts[1].strip()
                    skill_id_str, revision_id_str = skill_info_str.split(" ")
                    skill_id = UUID(skill_id_str)
                    revision_id = int(revision_id_str)

                    full_skill_data = self.load_full_skill_for_model(session_id, skill_id, revision_id)
                    if full_skill_data:
                        skill_metadata, full_body, actual_revision_id = full_skill_data
                        response += (
                            f"Successfully loaded full skill '{skill_metadata.name}' (ID: {skill_id}, "
                            f"Revision: {actual_revision_id}).\nFull Instructions:\n{full_body}\n"
                        )
                    else:
                        response += (
                            f"Failed to load full skill {skill_id} revision {revision_id}. "
                            "Check authorization, existence, or budget.\n"
                        )
                except (ValueError, IndexError) as e:
                    response += f"Invalid 'load full skill' command format: {e}. Expected 'load full skill <uuid> <int>'.\n"
            else:
                response += "Please specify skill ID and revision ID for 'load full skill'.\n"
        
        # Add logic for handling WorkGuidance if applicable
        # Existing captured Work guidance is immutable and should not be re-fetched
        # via this new path unless it's for a *new* selection by the model.
        # The problem statement says: "Work can load only guidance permitted by its captured binding."
        # This implies WorkGuidance already has its full content.
        # So, this new path is primarily for *model-selectable* on-demand loading.

        return response

    # Existing methods for work submission, etc., would remain unchanged.
    def submit_work(self, session_id: UUID, work_guidance: WorkGuidance) -> str:
        """
        Simulates submitting work with captured guidance.
        This path should already have the full guidance.
        """
        session_data = self._active_sessions.get(session_id)
        if not session_data:
            return "Error: Session not found for work submission."
        
        # In a real system, this would process the work_guidance
        # and ensure it's immutable and correctly bound.
        logger.info(f"Work submitted for session {session_id} with guidance for skill {work_guidance.skill_id} revision {work_guidance.revision_id}.")
        return f"Work submitted with guidance for skill {work_guidance.skill_id} revision {work_guidance.revision_id}. Guidance body length: {len(work_guidance.full_instruction_body)}"

