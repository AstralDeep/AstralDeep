"""Builds a bounded digest of skill/technique guidance for the agents active on a turn,
leading with the user's own enabled skills ahead of authored/synthesized knowledge
packs. Called from orchestrator.py; fails open to an empty digest on error.
"""

from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger("SkillPacks")

MAX_DIGEST_CHARS = 2500
MAX_PACKS = 3
MAX_PER_PACK_CHARS = 600
MAX_USER_SKILL_CHARS = 1200


def build_skill_digest(knowledge_index, agent_ids: Iterable[str],
                       max_chars: int = MAX_DIGEST_CHARS,
                       max_packs: int = MAX_PACKS, *, user_skills=()) -> str:
    try:
        from orchestrator.user_skills import digest_lines
        user_sections = digest_lines(user_skills, agent_ids, max_chars=MAX_USER_SKILL_CHARS)
        packs = []
        for aid in sorted(set(agent_ids)):
            try:
                content = knowledge_index.get_techniques_for_agent(aid)
            except Exception:
                logger.debug("skill_packs.fallback: lookup failed for %s", aid, exc_info=True)
                content = ""
            if content and content.strip():
                packs.append((aid, content.strip()))
            if len(packs) >= max_packs:
                break
        if not packs and not user_sections:
            return ""
        out = ["## Skill guidance for the agents in this turn",
               "Treat the following as how-to guidance for using these agents' tools well. "
               "Sections titled \"Your skill\" are the user's own standing instructions — follow them."]
        total = sum(len(section) for section in user_sections)
        out.extend(user_sections)
        for aid, content in packs:
            snippet = content[:MAX_PER_PACK_CHARS]
            if total + len(snippet) > max_chars:
                break
            out.append(f"### {aid}\n{snippet}")
            total += len(snippet)
        return "\n\n".join(out) if len(out) > 2 else ""
    except Exception:
        logger.debug("skill_packs.fallback: digest build failed", exc_info=True)
        return ""
