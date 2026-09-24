"""Orchestrator-callable memory tools (remember, memory_search, memory_get,
capture_signal) that gate every write through phi_gate.py and memory_guard.py, then
reconcile, link, and PageRank-rank recall via living_memory.py and repository.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from . import living_memory as lm
from . import memory_guard
from . import project_scope as ps
from .phi_gate import PHIGate, get_phi_gate
from .repository import MEMORY_CATEGORIES
from .retrieval_scoring import multisignal_enabled, score_memory_row

logger = logging.getLogger("personalization.memory")

RECONCILE_MAX_CANDIDATES = 8

_SINGULAR_CATEGORIES = frozenset({"profession"})


def reconcile_enabled() -> bool:
    return os.getenv("FF_MEMORY_RECONCILE", "true").strip().lower() not in ("0", "false", "no", "off")


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


LINK_MIN_OVERLAP = 1
LINK_MAX_NEIGHBORS = 5

_KEYWORD_STOPWORDS = frozenset(
    "the a an of to in on at for and or but with from into about as is are was "
    "were be been being i you he she it we they me my your his her our their "
    "this that these those prefer prefers like likes want wants use uses using "
    "have has had do does did will would can could should note remember".split()
)


def linking_enabled() -> bool:
    return os.getenv("FF_MEMORY_LINKING", "true").strip().lower() not in ("0", "false", "no", "off")


def derive_keywords(value: str, *, limit: int = 8) -> str:
    out: List[str] = []
    for t in re.findall(r"[a-z0-9]{3,}", (value or "").lower()):
        if t in _KEYWORD_STOPWORDS or t in out:
            continue
        out.append(t)
        if len(out) >= limit:
            break
    return " ".join(out)


def pagerank_enabled() -> bool:
    return os.getenv("FF_MEMORY_PAGERANK", "true").strip().lower() not in ("0", "false", "no", "off")


def personalized_pagerank(adjacency: Dict[str, List[str]], seeds: Dict[str, float],
                          *, alpha: float = 0.85, iters: int = 20) -> Dict[str, float]:
    nodes = set(adjacency)
    nodes.update(seeds)
    for nbrs in adjacency.values():
        nodes.update(nbrs)
    if not nodes:
        return {}
    seed_total = sum(w for w in seeds.values() if w > 0)
    if seed_total > 0:
        restart = {n: (max(seeds.get(n, 0.0), 0.0) / seed_total) for n in nodes}
    else:
        restart = {n: 1.0 / len(nodes) for n in nodes}
    rank = dict(restart)
    for _ in range(max(1, iters)):
        nxt = {n: (1.0 - alpha) * restart[n] for n in nodes}
        for n in nodes:
            nbrs = adjacency.get(n) or []
            if nbrs:
                share = alpha * rank[n] / len(nbrs)
                for m in nbrs:
                    nxt[m] = nxt.get(m, 0.0) + share
            else:
                mass = alpha * rank[n]
                for m in nodes:
                    nxt[m] += mass * restart[m]
        rank = nxt
    return rank


def _extract_json(content: str) -> Optional[dict]:
    if not isinstance(content, str):
        return None
    s = content.strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        s = s.strip()
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except (ValueError, TypeError):
                    return None
    return None


def build_reconcile_messages(value: str, category: str,
                             candidates: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    existing = "\n".join(
        f"{i + 1}. [{c.get('category')}] {c.get('value')}"
        for i, c in enumerate(candidates)
    ) or "(none)"
    system = (
        "You maintain a user's long-term memory. Decide how the NEW fact relates "
        "to the EXISTING facts and reply with ONLY a JSON object — no prose.\n"
        "Actions:\n"
        '- "ADD": the new fact is genuinely new; keep it alongside the others.\n'
        '- "UPDATE": the new fact replaces or refines ONE existing fact (same '
        'fact, changed/more-precise value). Set "target" to that fact\'s number '
        'and "value" to the single best merged statement.\n'
        '- "DELETE": the new fact says an existing fact is no longer true and is '
        'not itself worth keeping. Set "target" to that fact\'s number.\n'
        '- "NOOP": the new fact is already captured by an existing one; change '
        "nothing.\n"
        'Reply EXACTLY: {"action":"ADD|UPDATE|DELETE|NOOP","target":<number or '
        'null>,"value":"<text or null>"}'
    )
    user = f"NEW FACT [{category}]: {value}\n\nEXISTING FACTS:\n{existing}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_reconcile_decision(content: str) -> Optional[Dict[str, Any]]:
    obj = _extract_json(content)
    if not isinstance(obj, dict):
        return None
    action = str(obj.get("action") or "").strip().upper()
    if action not in ("ADD", "UPDATE", "DELETE", "NOOP"):
        return None
    raw_target = obj.get("target")
    try:
        target = int(raw_target) if raw_target is not None else None
    except (TypeError, ValueError):
        target = None
    raw_value = obj.get("value")
    value = raw_value.strip() if isinstance(raw_value, str) and raw_value.strip() else None
    return {"action": action, "target": target, "value": value}


class MemoryTools:
    def __init__(self, repo, phi_gate: Optional[PHIGate] = None) -> None:
        self.repo = repo
        self.gate = phi_gate or get_phi_gate()

    def _gate_value(self, category: str, value: str):
        if category not in MEMORY_CATEGORIES:
            category = "context"
        value = (value or "").strip()
        if not value:
            return category, value, {"stored": False, "reason": "nothing to remember"}
        if self.gate.contains_phi(value):
            logger.info("memory.write_refused_phi",
                        extra={"user_id": "?", "category": category})
            return category, value, {
                "stored": False,
                "reason": "That looked like protected health information, so I did not "
                          "save it to long-term memory. I can still use it for this task.",
            }
        if memory_guard.guard_enabled() and memory_guard.is_poisoning_attempt(value):
            logger.warning("memory.write_refused_poison",
                           extra={"user_id": "?", "category": category})
            return category, value, {
                "stored": False,
                "refused": "poisoning",
                "reason": "That reads like an instruction rather than a fact about you, "
                          "so I didn't save it to long-term memory.",
            }
        return category, value, None

    def _read_scope(self, project_id: Optional[str]) -> Optional[str]:
        if not ps.project_scope_enabled():
            return None
        return ps.normalize_project(project_id)

    def _write_project(self, project_id: Optional[str]) -> Optional[str]:
        if not ps.project_scope_enabled():
            return None
        norm = ps.normalize_project(project_id)
        return None if norm == ps.GLOBAL else norm

    def _repo_list(self, user_id: str, project_id: Optional[str]) -> List[Dict[str, Any]]:
        if project_id is None:
            return self.repo.list_memory(user_id)
        return self.repo.list_memory(user_id, project_id=project_id)

    def _repo_create(self, user_id: str, category: str, value: str, *,
                     keywords: Optional[str], project_id: Optional[str]) -> Dict[str, Any]:
        if project_id is None:
            return self.repo.create_memory(user_id, category, value,
                                           source="explicit", keywords=keywords)
        return self.repo.create_memory(user_id, category, value, source="explicit",
                                       keywords=keywords, project_id=project_id)

    def _create_linked(self, user_id: str, category: str, value: str, *,
                       project_id: Optional[str] = None) -> Dict[str, Any]:
        keywords = derive_keywords(value)
        item = self._repo_create(user_id, category, value,
                                 keywords=keywords, project_id=project_id)
        if lm.temporal_enabled():
            try:
                self._stamp_validity(user_id, item, category, value, project_id)
            except Exception:
                logger.debug("memory.temporal_stamp_failed", exc_info=True)
        if linking_enabled():
            try:
                self._link_new(user_id, item["id"], value, keywords)
            except Exception:
                logger.debug("memory.link_failed", exc_info=True)
        return item

    def _stamp_validity(self, user_id: str, item: Dict[str, Any], category: str,
                        value: str, project_id: Optional[str]) -> None:
        now = int(time.time() * 1000)
        self.repo.set_validity(user_id, item["id"], valid_from=now,
                               valid_to=None, ingested_at=now)
        if category not in _SINGULAR_CATEGORIES:
            return
        if ps.project_scope_enabled() and project_id is None:
            cmp_scope: Optional[str] = ps.GLOBAL
        else:
            cmp_scope = project_id
        new_norm = str(value).strip().lower()
        for it in self._repo_list(user_id, cmp_scope):
            if it.get("id") == item["id"] or str(it.get("category", "")) != category:
                continue
            if str(it.get("value", "")).strip().lower() == new_norm:
                continue
            if it.get("valid_to") is not None:
                continue
            self.repo.set_validity(user_id, it["id"],
                                   valid_from=it.get("valid_from"), valid_to=now,
                                   ingested_at=it.get("ingested_at"))

    def _link_new(self, user_id: str, new_id: str, value: str, keywords: str) -> int:
        kw = _tokens(keywords) or _tokens(value)
        if not kw:
            return 0
        scored = []
        for it in self.repo.list_memory(user_id):
            if it.get("id") == new_id:
                continue
            other = _tokens(it.get("keywords") or "") or _tokens(it.get("value", ""))
            overlap = len(kw & other)
            if overlap >= LINK_MIN_OVERLAP:
                scored.append((overlap, it["id"]))
        scored.sort(key=lambda t: -t[0])
        linked = 0
        for _, oid in scored[:LINK_MAX_NEIGHBORS]:
            if self.repo.add_link(user_id, new_id, oid):
                linked += 1
        return linked

    def _do_add(self, user_id: str, category: str, value: str, *,
                project_id: Optional[str] = None) -> Dict[str, Any]:
        item = self._create_linked(user_id, category, value, project_id=project_id)
        logger.info("memory.remembered",
                    extra={"user_id": user_id, "category": category, "memory_id": item["id"]})
        result = {"stored": True, "id": item["id"], "category": category}
        if project_id is not None:
            result["project_id"] = project_id
        return result

    def remember(self, user_id: str, category: str, value: str, *,
                 project_id: Optional[str] = None) -> Dict[str, Any]:
        category, value, refusal = self._gate_value(category, value)
        if refusal is not None:
            return refusal
        return self._do_add(user_id, category, value,
                            project_id=self._write_project(project_id))

    def _candidates(self, user_id: str, category: str, value: str, *,
                    project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        vt = _tokens(value)
        scored = []
        for it in self._repo_list(user_id, project_id):
            overlap = len(vt & _tokens(it.get("value", "")))
            same_cat = it.get("category") == category
            if overlap or same_cat:
                scored.append((overlap + (1 if same_cat else 0), it))
        scored.sort(key=lambda t: -t[0])
        return [it for _, it in scored[:RECONCILE_MAX_CANDIDATES]]

    async def remember_reconciled(
        self, user_id: str, category: str, value: str, *,
        llm_call: Optional[Callable[[List[Dict[str, str]]], Awaitable[Optional[str]]]] = None,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        category, value, refusal = self._gate_value(category, value)
        if refusal is not None:
            return refusal

        read_scope = self._read_scope(project_id)
        write_proj = self._write_project(project_id)
        candidates = (self._candidates(user_id, category, value, project_id=read_scope)
                      if (reconcile_enabled() and llm_call is not None) else [])
        if not candidates:
            return self._do_add(user_id, category, value, project_id=write_proj)

        try:
            content = await llm_call(build_reconcile_messages(value, category, candidates))
            decision = parse_reconcile_decision(content) if content else None
        except Exception:
            logger.debug("memory.reconcile_llm_failed — appending", exc_info=True)
            decision = None
        if not decision:
            return self._do_add(user_id, category, value)

        action = decision["action"]
        target = decision["target"]
        tgt = (candidates[target - 1]
               if isinstance(target, int) and 1 <= target <= len(candidates) else None)

        if action == "NOOP":
            logger.info("memory.reconcile_noop",
                        extra={"user_id": user_id, "category": category})
            return {"stored": False, "action": "noop",
                    "reason": "already remembered"}
        if action == "DELETE" and tgt is not None:
            self.repo.supersede_memory(user_id, tgt["id"], None)
            logger.info("memory.reconcile_delete",
                        extra={"user_id": user_id, "superseded": tgt["id"]})
            return {"stored": False, "action": "delete", "superseded": tgt["id"]}
        if action == "UPDATE" and tgt is not None:
            new_value = decision.get("value") or value
            item = self._create_linked(user_id, category, new_value, project_id=write_proj)
            self.repo.supersede_memory(user_id, tgt["id"], item["id"])
            logger.info("memory.reconcile_update",
                        extra={"user_id": user_id, "category": category,
                               "memory_id": item["id"], "superseded": tgt["id"]})
            return {"stored": True, "id": item["id"], "category": category,
                    "action": "update", "superseded": tgt["id"]}
        result = self._do_add(user_id, category, value, project_id=write_proj)
        result["action"] = "add"
        return result

    def capture_signal(self, user_id: str, category: str, value: str) -> bool:
        if category not in MEMORY_CATEGORIES:
            category = "context"
        value = (value or "").strip()
        if not value or self.gate.contains_phi(value):
            return False
        self.repo.add_signal(user_id, category, value)
        logger.info("memory.signal_captured",
                    extra={"user_id": user_id, "category": category})
        return True

    def _live_memory(self, user_id: str, *,
                     project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        items = self._repo_list(user_id, project_id)
        if lm.temporal_enabled():
            try:
                items = lm.as_of(items, int(time.time() * 1000))
            except Exception:
                logger.debug("memory.temporal_filter_failed", exc_info=True)
        if not memory_guard.guard_enabled():
            return items
        kept = []
        for it in items:
            if memory_guard.trust_of(it) == "tampered":
                logger.warning("memory.tampered_excluded",
                               extra={"user_id": user_id, "memory_id": it.get("id")})
                continue
            kept.append(it)
        return kept

    def _reinforce(self, user_id: str, items: List[Dict[str, Any]]) -> None:
        if not lm.forgetting_enabled() or not items:
            return
        try:
            for it in items:
                mid = it.get("id")
                if mid is not None:
                    self.repo.record_recall(user_id, mid)
        except Exception:
            logger.debug("memory.reinforce_failed", exc_info=True)

    def memory_get(self, user_id: str, *,
                   project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        items = self._live_memory(user_id, project_id=self._read_scope(project_id))
        self._reinforce(user_id, items)
        return items

    def memory_search(self, user_id: str, query: str, *, limit: int = 10,
                      project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        scope = self._read_scope(project_id)
        q = _tokens(query)
        items = self._live_memory(user_id, project_id=scope)
        if not q:
            out = items[:limit]
            self._reinforce(user_id, out)
            return out
        use_ms = multisignal_enabled()
        total = len(items)
        scored = []
        seed_scores: Dict[str, float] = {}
        order: Dict[str, int] = {}
        for idx, it in enumerate(items):
            order[str(it.get("id"))] = idx
            overlap = len(q & _tokens(
                f"{it.get('category','')} {it.get('value','')} {it.get('keywords') or ''}"))
            if not overlap:
                continue
            score = float(overlap)
            if use_ms:
                try:
                    score = score_memory_row(it, index=idx, total=total,
                                             overlap=overlap, query_size=len(q))
                except Exception:
                    logger.debug("memory_search: multi-signal scoring failed — overlap only",
                                 exc_info=True)
                    score = float(overlap)
            seed_scores[str(it.get("id"))] = score
            scored.append((score, idx, it))
        if not seed_scores:
            return []
        if pagerank_enabled():
            try:
                ranked = self._pagerank_rank(user_id, items, seed_scores, order, limit)
                if ranked is not None:
                    self._reinforce(user_id, ranked)
                    return ranked
            except Exception:
                logger.debug("memory_search: pagerank failed — falling back", exc_info=True)
        scored.sort(key=lambda t: (-t[0], t[1]))
        results = [it for _, _, it in scored[:limit]]
        if linking_enabled() and results and len(results) < limit:
            try:
                results = self._expand_with_links(user_id, results, items, limit)
            except Exception:
                logger.debug("memory_search: link expansion failed — direct hits only",
                             exc_info=True)
        self._reinforce(user_id, results)
        return results

    def _pagerank_rank(self, user_id: str, items: List[Dict[str, Any]],
                       seed_scores: Dict[str, float], order: Dict[str, int],
                       limit: int) -> Optional[List[Dict[str, Any]]]:
        list_links = getattr(self.repo, "list_links", None)
        if list_links is None:
            return None
        edges = list_links(user_id)
        if not edges:
            return None
        adjacency: Dict[str, List[str]] = {}
        for e in edges:
            adjacency.setdefault(str(e["memory_id"]), []).append(str(e["linked_id"]))
        ppr = personalized_pagerank(adjacency, seed_scores)
        by_id = {str(it.get("id")): it for it in items}
        ranked_ids = [nid for nid in by_id
                      if nid in seed_scores or ppr.get(nid, 0.0) > 1e-9]
        # Seeds must sort first; pure PPR could outrank them
        ranked_ids.sort(key=lambda nid: (
            0 if nid in seed_scores else 1,
            -seed_scores.get(nid, 0.0),
            -ppr.get(nid, 0.0),
            order.get(nid, 1 << 30),
        ))
        return [by_id[nid] for nid in ranked_ids[:limit]]

    def _expand_with_links(self, user_id: str, results: List[Dict[str, Any]],
                           all_items: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        by_id = {it.get("id"): it for it in all_items}
        seen = {it.get("id") for it in results}
        expanded = list(results)
        for it in results:
            if len(expanded) >= limit:
                break
            for lid in self.repo.linked_ids(user_id, it.get("id")):
                if lid not in seen and lid in by_id:
                    expanded.append(by_id[lid])
                    seen.add(lid)
                    if len(expanded) >= limit:
                        break
        return expanded[:limit]

    def get_persona(self, user_id: str) -> str:
        if not lm.persona_enabled():
            return ""
        row = self.repo.get_persona(user_id)
        return (row or {}).get("persona", "") if row else ""

    def evolve_persona(self, user_id: str, signals: List[str], *,
                       proposal: Optional[str] = None) -> str:
        if not lm.persona_enabled():
            return ""
        try:
            row = self.repo.get_persona(user_id)
            current = (row or {}).get("persona", "") if row else ""
            # Text change means evolve_persona scored a real improvement
            candidate = lm.evolve_persona(current, signals, proposal=proposal)
            if candidate.text != current:
                self.repo.set_persona(user_id, candidate.text, candidate.score)
                return candidate.text
            return current
        except Exception:
            logger.debug("memory.persona_evolve_failed", exc_info=True)
            return ""
