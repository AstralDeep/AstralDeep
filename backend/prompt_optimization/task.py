"""Loads the archived planning fixture for the offline scripted benchmark in optimizer.py.
Only its bounded rules field can vary during a run."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

SOURCE_PATH = Path("backend/prompt_optimization/synthetic_planning_prompt.txt")
INSTRUCTION_MARKER = "\nRules:\n"
ALLOWED_FIELDS = ("rules",)
MAX_INSTRUCTION_CHARS = 2000
RETIRED_TASK_ID = "coordinator_planning"
SYNTHETIC_TASK_ID = "synthetic_coordinator_planning"


class TaskSeamError(RuntimeError):
    pass


class InstructionRejected(ValueError):
    pass


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TaskPrompt:
    task_id: str
    source_path: str
    source_digest: str
    prompt_digest: str
    head: str
    head_digest: str
    rules: str
    rules_digest: str

    def render(self, instruction: str) -> str:
        validated = validate_instruction(instruction)
        return self.head + INSTRUCTION_MARKER + validated


def validate_instruction(text: object) -> str:
    if type(text) is not str:
        raise InstructionRejected("instruction text must be a plain string")
    stripped = text.strip()
    if not stripped:
        raise InstructionRejected("instruction text must be non-empty")
    if len(stripped) > MAX_INSTRUCTION_CHARS:
        raise InstructionRejected(
            f"instruction text exceeds {MAX_INSTRUCTION_CHARS} characters"
        )
    for char in stripped:
        code = ord(char)
        if code < 32 and char not in ("\n", "\t"):
            raise InstructionRejected("instruction text contains control characters")
    if "\x7f" in stripped:
        raise InstructionRejected("instruction text contains control characters")
    return stripped


def load_task_prompt(root: Path, task_id: str = RETIRED_TASK_ID) -> TaskPrompt:
    if task_id == RETIRED_TASK_ID:
        raise TaskSeamError(f"task {task_id} retired with the Python coordinator")
    if task_id != SYNTHETIC_TASK_ID:
        raise TaskSeamError("unknown planning task")
    try:
        raw = (root / SOURCE_PATH).read_bytes()
        prompt = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise TaskSeamError(f"planning fixture unreadable: {SOURCE_PATH}") from error
    parts = prompt.split(INSTRUCTION_MARKER)
    if len(parts) != 2:
        raise TaskSeamError(
            "prompt seam must contain exactly one instruction marker"
        )
    head, rules = parts
    if not head.strip() or not rules.strip():
        raise TaskSeamError("prompt seam head and instruction must be non-empty")
    return TaskPrompt(
        task_id=task_id,
        source_path=SOURCE_PATH.as_posix(),
        source_digest=hashlib.sha256(raw).hexdigest(),
        prompt_digest=digest_text(prompt),
        head=head,
        head_digest=digest_text(head),
        rules=rules,
        rules_digest=digest_text(rules),
    )


def split_rendered(prompt_text: str) -> Tuple[str, str]:
    parts = prompt_text.split(INSTRUCTION_MARKER)
    if len(parts) != 2:
        raise TaskSeamError("rendered prompt must contain exactly one instruction marker")
    return parts[0], parts[1]
