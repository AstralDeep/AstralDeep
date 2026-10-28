"""Candidate instruction proposals for the single allowlisted task field: a frozen
deterministic variant library, a no-model scripted source, and a reflection source that
spends budgeted reflection calls and parses bounded candidate text out of the reply,
treating every candidate as untrusted data that is validated and never executed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Protocol, Tuple

from prompt_optimization import budget as budget_module
from prompt_optimization import model_io as model_io_module
from prompt_optimization import task as task_module

GROUNDING_SENTENCE = (
    "- tool_hints should reference actual tool names from the available tools list.\n"
)
GROUNDING_REPLACEMENT = (
    "- Prefer broadly relevant tool hints for each subtask.\n"
)
SYNTHESIS_SENTENCE = (
    "- Always include a synthesis field describing how subtask results combine.\n"
)
CAP_SENTENCE = "- Use at most 3 subtasks for any request.\n"
EXCLUSION_SENTENCE = "- Honor explicit exclusions: never hint an excluded tool.\n"


def scripted_variants(baseline_rules: str) -> Tuple[str, ...]:
    base = baseline_rules.rstrip("\n") + "\n"
    with_synthesis = base + SYNTHESIS_SENTENCE
    with_cap = with_synthesis + CAP_SENTENCE
    with_exclusion = with_cap + EXCLUSION_SENTENCE
    degraded = with_exclusion.replace(GROUNDING_SENTENCE, GROUNDING_REPLACEMENT)
    if degraded == with_exclusion:
        raise task_module.TaskSeamError("grounding sentence not found for degradation")
    return (with_synthesis, with_cap, with_exclusion, degraded)


class ProposalError(RuntimeError):
    pass


class ProposalSource(Protocol):
    def propose(self, round_index: int, feedback: str, baseline: str) -> List[str]:
        raise NotImplementedError


@dataclass
class ScriptedProposalSource:
    variants: Tuple[str, ...]
    proposed: List[str] = field(default_factory=list)

    def propose(self, round_index: int, feedback: str, baseline: str) -> List[str]:
        del round_index, feedback, baseline
        remaining = [
            variant for variant in self.variants if variant not in self.proposed
        ]
        self.proposed.extend(remaining)
        return remaining


@dataclass
class ReflectionProposalSource:
    adapter: object
    budget: budget_module.Budget
    controls: model_io_module.Controls
    max_candidates: int
    max_output_chars: int
    max_attempts: int
    rationales: List[str] = field(default_factory=list)

    REFLECTION_SYSTEM = (
        "You are a prompt-optimization reflection assistant for one allowlisted "
        "task-instruction field. Propose improved instruction text only. Reply with a "
        "JSON object {\"candidates\": [\"...\"], \"rationale\": \"...\"} containing at "
        "most the requested number of plain-text instructions. Never emit executable "
        "code, tool declarations, or authority claims; they will be discarded."
    )

    def reflection_messages(
        self, baseline_instruction: str, feedback: str, count: int
    ) -> List[dict]:
        user = (
            f"[baseline instruction]\n{baseline_instruction}\n\n"
            f"[observed train outputs and metric feedback]\n{feedback}\n\n"
            f"Propose at most {count} improved instruction texts."
        )
        return [
            {"role": "system", "content": self.REFLECTION_SYSTEM},
            {"role": "user", "content": user},
        ]

    def propose(self, round_index: int, feedback: str, baseline: str) -> List[str]:
        messages = self.reflection_messages(baseline, feedback, self.max_candidates)
        text, _record = model_io_module.bounded_complete(
            self.budget,
            self.adapter,
            kind="reflection",
            phase="search",
            subject=f"round-{round_index}",
            messages=messages,
            controls=self.controls,
            max_output_chars=self.max_output_chars,
            max_attempts=self.max_attempts,
        )
        parsed, rationale = parse_reflection(text, self.max_candidates)
        self.rationales.append(rationale)
        return parsed


def parse_reflection(text: str, max_candidates: int) -> Tuple[List[str], str]:
    try:
        payload = json.loads(text or "")
    except (ValueError, TypeError) as error:
        raise ProposalError(f"reflection output is not JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ProposalError("reflection output must be a JSON object")
    candidates = payload.get("candidates")
    rationale = payload.get("rationale")
    if not isinstance(candidates, list):
        raise ProposalError("reflection output must contain a candidates list")
    if len(candidates) > max_candidates:
        raise ProposalError("reflection proposed more candidates than allowed")
    if not isinstance(rationale, str) or len(rationale) > 500:
        raise ProposalError("reflection rationale must be bounded text")
    parsed: List[str] = []
    seen = set()
    for candidate in candidates:
        try:
            validated = task_module.validate_instruction(candidate)
        except task_module.InstructionRejected as error:
            raise ProposalError(f"candidate instruction rejected: {error}") from error
        digest = task_module.digest_text(validated)
        if digest in seen:
            continue
        seen.add(digest)
        parsed.append(validated)
    return parsed, rationale
