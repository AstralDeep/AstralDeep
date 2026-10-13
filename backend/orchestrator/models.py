from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field

class SkillStatus(str, Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    ARCHIVED = "archived"

class SkillRevision(BaseModel):
    """Represents a specific revision of a skill."""
    revision_id: int = Field(..., description="Unique identifier for this revision of the skill.")
    name: str = Field(..., description="Name of the skill.")
    description: str = Field(..., description="Brief description of what the skill does.")
    instruction_body: str = Field(..., description="The full, detailed instructions for the skill.")
    created_at: str = Field(..., description="Timestamp of when this revision was created (ISO 8601).")
    # Add any other metadata relevant to a revision, e.g., author, version notes

class Skill(BaseModel):
    """Represents a skill's metadata, pointing to its latest revision."""
    id: UUID = Field(..., description="Unique identifier for the skill.")
    owner_id: UUID = Field(..., description="ID of the user/entity that owns this skill.")
    name: str = Field(..., description="Current name of the skill.")
    description: str = Field(..., description="Current description of the skill.")
    status: SkillStatus = Field(SkillStatus.ENABLED, description="Current status of the skill.")
    latest_revision_id: int = Field(..., description="The ID of the latest revision for this skill.")
    # Add any other top-level skill metadata

# No changes needed here, just ensuring the models are shown for context.
