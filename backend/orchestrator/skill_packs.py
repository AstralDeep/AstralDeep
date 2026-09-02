"""Feature 040 (US4) — on-demand skill-pack injection (progressive disclosure).

Builds a bounded, relevance-selected digest of capability/technique guidance for
ONLY the agents in play on a given turn (not every agent every turn), wiring the
previously-dormant ``KnowledgeIndex.get_techniques_for_agent`` into chat. Authored
packs (committed under ``knowledge_packs/``) take precedence over auto-synthesized
knowledge; see ``knowledge_synthesis.get_techniques_for_agent``.

Fail-open: any error yields an empty digest so the turn proceeds exactly as it
does with ``FF_SKILL_PACKS`` off.
"""
from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger("SkillPacks")

#: Bounds keep the per-turn context cache-stable and avoid token bloat. 077
#: raised the total to make room for the user's own skills (which come first).
MAX_DIGEST_CHARS = 2500
MAX_PACKS = 3
MAX_PER_PACK_CHARS = 600
MAX_USER_SKILL_CHARS = 1200


def build_skill_digest(knowledge_index, agent_ids: Iterable[str],
                       max_chars: int = MAX_DIGEST_CHARS,
                       max_packs: int = MAX_PACKS, *, orch=None,
                       owner: str | None = None) -> str:
    """Return a bounded skill-pack digest for ``agent_ids`` (the agents in play).

    Feature 077: when ``orch``/``owner`` are given, the owner's enabled skills
    lead the digest — *always* skills on every turn, agent-scoped ones when
    that agent is in play — under their own bound. Then the authored /
    synthesized packs as before. Returns ``""`` when nothing relevant exists or
    on any error (fail-open).
    """
    try:
        user_sections: list = []
        if orch is not None and owner:
            try:
                from orchestrator import user_skills
                user_sections = user_skills.digest_lines(
                    orch, owner, agent_ids, max_chars=MAX_USER_SKILL_CHARS)
            except Exception:
                logger.debug("skill_packs.fallback: user skills skipped", exc_info=True)
                user_sections = []
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
