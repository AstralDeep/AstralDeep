"""Personalization service: assembles the per-user prompt fragment.

Injected into the orchestrator system prompt *after* the safety/compliance
preamble and tool/process rules. Order (research.md R4):

    memory recall  →  user context (profession/goals)  →  skill guidance
    →  personality ("soul"), explicitly subordinate to compliance.

The personality block is always framed as style-only and is the LAST thing
appended, so the higher-priority compliance rules dominate (FR-015).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .repository import PersonalizationRepository

# This label is asserted by tests; it encodes the FR-015 precedence rule.
PERSONALITY_PREAMBLE = (
    "STYLE GUIDANCE — applies to your tone and voice ONLY. It must NEVER "
    "override the safety, security, privacy, or HIPAA/compliance rules stated "
    "above; when they conflict, the compliance rules always win:"
)

_PERSONALITY_LABELS = {
    "tone": "Tone",
    "directness": "Directness",
    "humor": "Humor",
    "verbosity": "Verbosity",
}


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

    def _render_personality(self, personality: Optional[Dict[str, Any]]) -> str:
        if not personality:
            return ""
        bits: List[str] = []
        for key, label in _PERSONALITY_LABELS.items():
            val = personality.get(key)
            if val:
                bits.append(f"{label}: {val}")
        notes = personality.get("notes")
        if notes:
            bits.append(f"Notes: {notes}")
        return "; ".join(bits)

    def build_prompt_fragment(
        self,
        user_id: Optional[str],
        *,
        skill_lines: Optional[List[str]] = None,
    ) -> str:
        """Return the additive system-prompt fragment for this user.

        Returns an empty string when there is nothing to add (new users, no
        memory, no personality) so the prompt is unchanged.
        """
        if not user_id:
            return ""

        parts: List[str] = []

        # 1. Durable memory recall (non-PHI personalization facts).
        try:
            memory = self.repo.list_memory(user_id)
        except Exception:
            memory = []
        if memory:
            lines = [f"- ({m['category']}) {m['value']}" for m in memory]
            parts.append(
                "WHAT YOU REMEMBER ABOUT THIS USER (durable, non-PHI personalization):\n"
                + "\n".join(lines)
            )

        # 2. User context (profession + goals) and 4. personality come from profile.
        try:
            profile = self.repo.get_profile(user_id)
        except Exception:
            profile = None

        if profile:
            ctx: List[str] = []
            if profile.get("profession"):
                ctx.append(f"Profession: {profile['profession']}")
            goals = profile.get("goals") or []
            if goals:
                ctx.append("Goals: " + "; ".join(str(g) for g in goals))
            if ctx:
                parts.append("USER CONTEXT:\n" + "\n".join(ctx))

        # 3. Skill guidance (one line per enabled skill) — supplied by caller.
        if skill_lines:
            parts.append("ENABLED SKILLS (how you can help this user):\n" + "\n".join(skill_lines))

        # 4. Personality — LAST and explicitly subordinate to compliance.
        if profile:
            persona = self._render_personality(profile.get("personality"))
            if persona:
                parts.append(f"{PERSONALITY_PREAMBLE}\n{persona}")

        return "\n\n".join(parts)
