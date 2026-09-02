"""Feature 077 — user-authored skills (US3).

A skill is one markdown file the user writes in the product: a name, when it
applies (every chat, or named agents), an optional ``/command`` alias, and the
instructions. It lives in the SAME format as an authored skill pack
(``knowledge_packs/README.md``) under the runtime knowledge directory —
``<knowledge>/user_skills/<owner-hash>/<slug>.md`` — which Compose bind-mounts,
so no schema change (D2). Path components are derived from a hash of the owner
and a slug of the name, never from user text verbatim.

What the rest of the product does with them:

* :func:`skill_packs.build_skill_digest` puts enabled *always* skills first in
  the per-turn guidance and agent-scoped skills alongside that agent's pack.
* :func:`slash_commands.expand_message` expands ``/<command> text`` into the
  skill's instructions plus the user's text; ``/help`` lists them.
* The *My agents & skills* surface lists, creates, edits, toggles and deletes.

Bounded: ``MAX_SKILLS`` per owner, ``MAX_INSTRUCTIONS_CHARS`` per skill; reads
are cached by directory mtime. ``FF_USER_SKILLS`` (default on) gates every entry
point; off ⇒ no digest lines, no expansion, no surface section.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("UserSkills")

MAX_SKILLS = 20
MAX_NAME_CHARS = 60
MAX_INSTRUCTIONS_CHARS = 4000
MAX_APPLIES_TO = 8
SUBDIR = "user_skills"
ALWAYS = "always"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_COMMAND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.S)


def enabled() -> bool:
    try:
        from shared.feature_flags import flags
        return bool(flags.is_enabled("user_skills"))
    except Exception:  # noqa: BLE001 — a test double without the registry
        return True


@dataclass(frozen=True)
class Skill:
    slug: str
    name: str
    instructions: str
    applies_to: Tuple[str, ...]   # (ALWAYS,) or agent ids
    command: str = ""
    enabled: bool = True
    updated_at: int = 0

    @property
    def always(self) -> bool:
        return ALWAYS in self.applies_to

    def applies(self, agent_id: str) -> bool:
        return self.always or agent_id in self.applies_to

    def public(self) -> Dict[str, Any]:
        return {
            "slug": self.slug, "name": self.name, "instructions": self.instructions,
            "applies_to": list(self.applies_to), "command": self.command,
            "enabled": self.enabled, "updated_at": self.updated_at,
        }


class SkillValidationError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Paths + format
# ---------------------------------------------------------------------------

def owner_dir(knowledge_dir: str, owner: str) -> str:
    digest = hashlib.sha256(str(owner).encode("utf-8")).hexdigest()[:24]
    return os.path.join(knowledge_dir, SUBDIR, digest)


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    return slug[:48] or "skill"


def _fm_escape(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _fm_unescape(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def render_markdown(skill: Skill, owner: str) -> str:
    applies = ", ".join(skill.applies_to)
    return (
        "---\n"
        f"name: {_fm_escape(skill.name)}\n"
        "type: user_skill\n"
        f"owner: {_fm_escape(owner)}\n"
        f"slug: {skill.slug}\n"
        f"command: {skill.command}\n"
        f"applies_to: [{applies}]\n"
        f"enabled: {'true' if skill.enabled else 'false'}\n"
        f"updated_at: {skill.updated_at}\n"
        "---\n\n"
        "## Instructions\n\n"
        f"{skill.instructions.strip()}\n"
    )


def parse_markdown(text: str) -> Optional[Skill]:
    match = _FRONTMATTER_RE.match(text or "")
    if not match:
        return None
    meta: Dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    if meta.get("type") != "user_skill":
        return None
    body = match.group(2)
    if "## Instructions" in body:
        body = body.split("## Instructions", 1)[1]
    raw_applies = meta.get("applies_to", "").strip().strip("[]")
    applies = tuple(a.strip() for a in raw_applies.split(",") if a.strip()) or (ALWAYS,)
    try:
        updated = int(meta.get("updated_at") or 0)
    except ValueError:
        updated = 0
    return Skill(
        slug=meta.get("slug") or slugify(_fm_unescape(meta.get("name", ""))),
        name=_fm_unescape(meta.get("name", "")) or "Untitled skill",
        instructions=body.strip()[:MAX_INSTRUCTIONS_CHARS],
        applies_to=applies,
        command=(meta.get("command") or "").strip(),
        enabled=meta.get("enabled", "true").strip().lower() != "false",
        updated_at=updated,
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class UserSkillStore:
    """Owner-scoped skill files with an mtime cache (one directory per owner)."""

    def __init__(self, knowledge_dir: str) -> None:
        self.knowledge_dir = knowledge_dir
        self._cache: Dict[str, Tuple[float, List[Skill]]] = {}

    def _dir(self, owner: str) -> str:
        return owner_dir(self.knowledge_dir, owner)

    def list(self, owner: str) -> List[Skill]:
        directory = self._dir(owner)
        try:
            mtime = os.stat(directory).st_mtime
        except OSError:
            return []
        cached = self._cache.get(directory)
        if cached is not None and cached[0] == mtime:
            return list(cached[1])
        skills: List[Skill] = []
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            names = []
        for fname in names:
            if not fname.endswith(".md"):
                continue
            try:
                with open(os.path.join(directory, fname), "r", encoding="utf-8") as fh:
                    parsed = parse_markdown(fh.read(MAX_INSTRUCTIONS_CHARS + 2048))
            except OSError:
                parsed = None
            if parsed is not None:
                skills.append(parsed)
        skills.sort(key=lambda s: s.name.lower())
        self._cache[directory] = (mtime, list(skills))
        return skills

    def get(self, owner: str, slug: str) -> Optional[Skill]:
        for skill in self.list(owner):
            if skill.slug == slug:
                return skill
        return None

    def enabled(self, owner: str) -> List[Skill]:
        return [s for s in self.list(owner) if s.enabled]

    def command_map(self, owner: str) -> Dict[str, Skill]:
        return {s.command: s for s in self.enabled(owner) if s.command}

    def save(self, owner: str, *, name: str, instructions: str, applies_to: Any,
             command: str = "", enabled: bool = True, slug: str = "",
             reserved_commands: Any = ()) -> Skill:
        """Create (``slug`` empty) or replace one skill. Raises
        :class:`SkillValidationError` with a user-facing message."""
        name = (name or "").strip()
        if len(name) < 2 or len(name) > MAX_NAME_CHARS:
            raise SkillValidationError(f"Give the skill a name (2–{MAX_NAME_CHARS} characters).")
        instructions = (instructions or "").strip()
        if len(instructions) < 10:
            raise SkillValidationError("Write the instructions (at least a sentence).")
        if len(instructions) > MAX_INSTRUCTIONS_CHARS:
            raise SkillValidationError(
                f"Instructions are limited to {MAX_INSTRUCTIONS_CHARS} characters.")
        command = (command or "").strip().lstrip("/").lower()
        if command and not _COMMAND_RE.match(command):
            raise SkillValidationError(
                "A command is 1–24 lowercase letters, digits, - or _ (for example: standup).")
        if command and command in set(reserved_commands or ()):
            raise SkillValidationError(f"/{command} is a built-in command — pick another name.")
        applies = _normalise_applies(applies_to)
        existing = self.list(owner)
        target_slug = slug or slugify(name)
        others = [s for s in existing if s.slug != target_slug]
        if not slug and any(s.slug == target_slug for s in existing):
            raise SkillValidationError("You already have a skill with that name.")
        if slug and not any(s.slug == slug for s in existing):
            raise SkillValidationError("That skill no longer exists.")
        if command and any(s.command == command for s in others):
            raise SkillValidationError(f"/{command} is already used by another of your skills.")
        if not slug and len(existing) >= MAX_SKILLS:
            raise SkillValidationError(f"You can keep up to {MAX_SKILLS} skills — delete one first.")
        skill = Skill(slug=target_slug, name=name, instructions=instructions,
                      applies_to=applies, command=command, enabled=bool(enabled),
                      updated_at=int(time.time()))
        directory = self._dir(owner)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{skill.slug}.md")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(skill, owner))
        os.replace(tmp, path)
        os.utime(directory, None)
        self._cache.pop(directory, None)
        return skill

    def set_enabled(self, owner: str, slug: str, enabled: bool) -> Optional[Skill]:
        skill = self.get(owner, slug)
        if skill is None:
            return None
        return self.save(owner, name=skill.name, instructions=skill.instructions,
                         applies_to=skill.applies_to, command=skill.command,
                         enabled=enabled, slug=slug)

    def delete(self, owner: str, slug: str) -> bool:
        directory = self._dir(owner)
        path = os.path.join(directory, f"{slugify(slug)}.md")
        try:
            os.remove(path)
        except FileNotFoundError:
            return False
        try:
            os.utime(directory, None)
        except OSError:
            pass
        self._cache.pop(directory, None)
        return True


def _normalise_applies(value: Any) -> Tuple[str, ...]:
    if isinstance(value, str):
        parts = [p.strip() for p in re.split(r"[,\n]", value) if p.strip()]
    else:
        parts = [str(p).strip() for p in (value or []) if str(p).strip()]
    if not parts or any(p.lower() in (ALWAYS, "all", "*", "every chat") for p in parts):
        return (ALWAYS,)
    out: List[str] = []
    for part in parts:
        if not _AGENT_ID_RE.match(part):
            raise SkillValidationError(f"“{part}” is not an agent id.")
        if part not in out:
            out.append(part)
    return tuple(out[:MAX_APPLIES_TO])


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------

def store_for(orch) -> Optional[UserSkillStore]:
    """The orchestrator's store (created on first use next to the knowledge
    index), or None when the feature is off."""
    if not enabled():
        return None
    store = getattr(orch, "_user_skill_store", None)
    if not isinstance(store, UserSkillStore):
        knowledge_dir = None
        index = getattr(orch, "knowledge_index", None)
        if index is not None:
            knowledge_dir = getattr(index, "knowledge_dir", None)
        if not knowledge_dir:
            from orchestrator.knowledge_synthesis import DEFAULT_KNOWLEDGE_DIR
            knowledge_dir = DEFAULT_KNOWLEDGE_DIR
        store = UserSkillStore(knowledge_dir)
        try:
            orch._user_skill_store = store
        except Exception:  # noqa: BLE001 — a frozen test double
            pass
    return store


def digest_lines(orch, owner: Optional[str], agent_ids: Any, *, max_chars: int) -> List[str]:
    """Bounded ``### <skill>`` sections for the skill digest: always-skills
    first, then skills scoped to an agent in play."""
    if not owner:
        return []
    store = store_for(orch)
    if store is None:
        return []
    try:
        skills = store.enabled(owner)
    except Exception:  # noqa: BLE001 — fail-open
        logger.debug("user_skills: digest read failed", exc_info=True)
        return []
    in_play = set(agent_ids or ())
    picked = [s for s in skills if s.always] + [
        s for s in skills if not s.always and any(s.applies(a) for a in in_play)]
    out: List[str] = []
    total = 0
    for skill in picked:
        body = skill.instructions.strip()
        section = f"### Your skill: {skill.name}\n{body}"
        if total + len(section) > max_chars:
            remaining = max_chars - total
            if remaining < 80:
                break
            section = section[:remaining]
        out.append(section)
        total += len(section)
    return out
