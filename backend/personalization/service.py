"""Assembles the per-user system-prompt fragment (memory recall, context, skill
guidance, then personality) that orchestrator.py injects after its compliance
preamble; personality is always framed as subordinate style, last. Reads via
repository.py.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .repository import PersonalizationRepository

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
        if not user_id:
            return ""

        parts: List[str] = []

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

        if skill_lines:
            parts.append("ENABLED SKILLS (how you can help this user):\n" + "\n".join(skill_lines))

        if profile:
            persona = self._render_personality(profile.get("personality"))
            if persona:
                parts.append(f"{PERSONALITY_PREAMBLE}\n{persona}")

        return "\n\n".join(parts)
