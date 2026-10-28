"""Binds the one allowlisted task prompt: extracts the frozen planning prompt from its
product source seam via AST (no product import), splits it into the frozen head and the
single mutable instruction field, and validates candidate instruction text as untrusted
bounded data.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

SOURCE_PATH = Path("backend/orchestrator/coordinator.py")
TARGET_CLASS = "Coordinator"
TARGET_ATTRIBUTE = "PLANNING_PROMPT"
INSTRUCTION_MARKER = "\nRules:\n"
ALLOWED_FIELDS = ("rules",)
MAX_INSTRUCTION_CHARS = 2000
TASK_ID = "coordinator_planning"


class TaskSeamError(RuntimeError):
    pass


class InstructionRejected(ValueError):
    pass


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest(text: str) -> str:
    return digest_text(text)


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


def _extract_prompt(source: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise TaskSeamError(f"source seam does not parse: {error}") from error
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != TARGET_CLASS:
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Assign):
                continue
            names = [
                target.id
                for target in statement.targets
                if isinstance(target, ast.Name)
            ]
            if TARGET_ATTRIBUTE not in names:
                continue
            value = statement.value
            if isinstance(value, ast.Constant) and type(value.value) is str:
                return value.value
            raise TaskSeamError("prompt seam is not a plain string literal")
    raise TaskSeamError(
        f"prompt seam {TARGET_CLASS}.{TARGET_ATTRIBUTE} not found in {SOURCE_PATH}"
    )


def load_task_prompt(root: Path) -> TaskPrompt:
    path = root / SOURCE_PATH
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise TaskSeamError(f"source seam unreadable: {SOURCE_PATH}") from error
    prompt = _extract_prompt(raw.decode("utf-8"))
    parts = prompt.split(INSTRUCTION_MARKER)
    if len(parts) != 2:
        raise TaskSeamError(
            "prompt seam must contain exactly one instruction marker"
        )
    head, rules = parts
    if not head.strip() or not rules.strip():
        raise TaskSeamError("prompt seam head and instruction must be non-empty")
    return TaskPrompt(
        task_id=TASK_ID,
        source_path=SOURCE_PATH.as_posix(),
        source_digest=hashlib.sha256(raw).hexdigest(),
        prompt_digest=_digest(prompt),
        head=head,
        head_digest=_digest(head),
        rules=rules,
        rules_digest=_digest(rules),
    )


def split_rendered(prompt_text: str) -> Tuple[str, str]:
    parts = prompt_text.split(INSTRUCTION_MARKER)
    if len(parts) != 2:
        raise TaskSeamError("rendered prompt must contain exactly one instruction marker")
    return parts[0], parts[1]
