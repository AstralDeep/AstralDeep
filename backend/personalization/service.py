"""Assembles the per-user system-prompt fragment (memory recall, context, skill
guidance, then personality) that orchestrator.py injects after its compliance
preamble; personality is always framed as subordinate style, last. Reads via
repository.py and recalls only the rows memory_tools.py's live_items keeps.
"""

from __future__ import annotations


from .repository import PersonalizationRepository

class PersonalizationService:
    def __init__(
        self,
        db,
        *,
        plane_runtime=None,
        plane_repositories=None,
    ) -> None:
        self.repo = PersonalizationRepository(
            db,
            plane_runtime=plane_runtime,
            plane_repositories=plane_repositories,
        )
