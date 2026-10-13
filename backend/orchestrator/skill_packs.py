import logging
from typing import Dict, List, Optional, Tuple
from uuid import UUID

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Constants for digest generation
MAX_DIGEST_LENGTH = 1200 # As per problem description

def _build_skill_digest(name: str, description: str, instruction_body: str) -> str:
    """
    Constructs a bounded skill digest.
    This function explicitly truncates the instruction body.
    """
    # Combine name, description, and instruction body
    combined_text = f"Skill Name: {name}\nDescription: {description}\nInstructions: {instruction_body}"

    # Truncate to MAX_DIGEST_LENGTH
    if len(combined_text) > MAX_DIGEST_LENGTH:
        digest = combined_text[:MAX_DIGEST_LENGTH - 3] + "..." # Add ellipsis to indicate truncation
    else:
        digest = combined_text
    return digest

# Existing classes and methods...

# No other changes needed here for this bounty, as the digest construction
# is intended to remain truncated. The new functionality provides an *alternative*
# read path for the full content.
