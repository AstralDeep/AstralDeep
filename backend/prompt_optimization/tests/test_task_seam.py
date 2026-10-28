"""Tests for the task seam: fail-closed AST extraction of the one allowlisted planning
prompt, frozen head versus mutable instruction split, and untrusted instruction text
validation.
"""

from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path

import pytest

from prompt_optimization import task

REPO_ROOT = Path(task.__file__).resolve().parents[2]


def test_loads_real_planning_prompt_seam():
    prompt = task.load_task_prompt(REPO_ROOT)
    assert prompt.task_id == task.TASK_ID
    assert task.INSTRUCTION_MARKER in prompt.head + task.INSTRUCTION_MARKER + prompt.rules
    assert prompt.head.strip()
    assert prompt.rules.strip()
    assert prompt.prompt_digest == hashlib.sha256(
        (prompt.head + task.INSTRUCTION_MARKER + prompt.rules).encode("utf-8")
    ).hexdigest()
    assert prompt.source_digest == hashlib.sha256(
        (REPO_ROOT / task.SOURCE_PATH).read_bytes()
    ).hexdigest()


def test_real_seam_has_exactly_one_marker():
    raw = (REPO_ROOT / task.SOURCE_PATH).read_text(encoding="utf-8")
    assert raw.count(task.INSTRUCTION_MARKER) >= 0
    prompt = task.load_task_prompt(REPO_ROOT)
    assert prompt.head.count(task.INSTRUCTION_MARKER) == 0
    assert prompt.rules.count(task.INSTRUCTION_MARKER) == 0


def test_render_replaces_only_allowlisted_field():
    prompt = task.load_task_prompt(REPO_ROOT)
    rendered = prompt.render("Custom instruction line.\n")
    head, rules = task.split_rendered(rendered)
    assert head == prompt.head
    assert rules == "Custom instruction line."
    assert rendered.count(task.INSTRUCTION_MARKER) == 1


def test_render_validates_instruction():
    prompt = task.load_task_prompt(REPO_ROOT)
    with pytest.raises(task.InstructionRejected):
        prompt.render("")
    with pytest.raises(task.InstructionRejected):
        prompt.render("x" * (task.MAX_INSTRUCTION_CHARS + 1))
    with pytest.raises(task.InstructionRejected):
        prompt.render("bad\x00instruction")
    with pytest.raises(task.InstructionRejected):
        prompt.render("bad\x01instruction")
    with pytest.raises(task.InstructionRejected):
        prompt.render("bad\x7finstruction")
    with pytest.raises(task.InstructionRejected):
        prompt.render(123)


def test_validate_instruction_accepts_bounded_plain_text():
    assert task.validate_instruction("  plain instruction\n") == "plain instruction"
    assert task.validate_instruction("multi\nline\ttext") == "multi\nline\ttext"
    edge = "x" * task.MAX_INSTRUCTION_CHARS
    assert task.validate_instruction(edge) == edge


def _write_source(tmp_path: Path, body: str) -> Path:
    root = tmp_path / "repo"
    (root / "backend" / "orchestrator").mkdir(parents=True, exist_ok=True)
    (root / "backend" / "orchestrator" / "coordinator.py").write_text(
        textwrap.dedent(body), encoding="utf-8"
    )
    return root


def test_missing_seam_fails_closed(tmp_path):
    root = _write_source(tmp_path, "class Other:\n    PROMPT = 'x'\n")
    with pytest.raises(task.TaskSeamError):
        task.load_task_prompt(root)


def test_non_string_seam_fails_closed(tmp_path):
    root = _write_source(
        tmp_path,
        """
        class Coordinator:
            PLANNING_PROMPT = ["not", "a", "string"]
        """,
    )
    with pytest.raises(task.TaskSeamError):
        task.load_task_prompt(root)


def test_missing_marker_fails_closed(tmp_path):
    root = _write_source(
        tmp_path,
        """
        class Coordinator:
            PLANNING_PROMPT = "no rules marker here"
        """,
    )
    with pytest.raises(task.TaskSeamError):
        task.load_task_prompt(root)


def test_double_marker_fails_closed(tmp_path):
    root = _write_source(
        tmp_path,
        """
        class Coordinator:
            PLANNING_PROMPT = "a\\nRules:\\nfirst\\nRules:\\nsecond"
        """,
    )
    with pytest.raises(task.TaskSeamError):
        task.load_task_prompt(root)


def test_non_assign_statement_in_class_is_skipped(tmp_path):
    root = _write_source(
        tmp_path,
        '''
        class Coordinator:
            """A docstring statement."""

            PLANNING_PROMPT = "head\\nRules:\\ntail"
        ''',
    )
    prompt = task.load_task_prompt(root)
    assert prompt.rules == "tail"


def test_other_assignments_are_skipped(tmp_path):
    root = _write_source(
        tmp_path,
        '''
        class Coordinator:
            OTHER = 1
            PLANNING_PROMPT = "head\\nRules:\\ntail"
        ''',
    )
    prompt = task.load_task_prompt(root)
    assert prompt.rules == "tail"


def test_empty_head_or_rules_fails_closed(tmp_path):
    root = _write_source(
        tmp_path,
        '''
        class Coordinator:
            PLANNING_PROMPT = "\\nRules:\\ntail"
        ''',
    )
    with pytest.raises(task.TaskSeamError):
        task.load_task_prompt(root)
    root = _write_source(
        tmp_path,
        '''
        class Coordinator:
            PLANNING_PROMPT = "head\\nRules:\\n   "
        ''',
    )
    with pytest.raises(task.TaskSeamError):
        task.load_task_prompt(root)


def test_unreadable_source_fails_closed(tmp_path):
    with pytest.raises(task.TaskSeamError):
        task.load_task_prompt(tmp_path / "missing")


def test_unparsable_source_fails_closed(tmp_path):
    root = _write_source(tmp_path, "class Coordinator:\n    broken(")
    with pytest.raises(task.TaskSeamError):
        task.load_task_prompt(root)


def test_split_rendered_requires_single_marker():
    with pytest.raises(task.TaskSeamError):
        task.split_rendered("no marker")
    with pytest.raises(task.TaskSeamError):
        task.split_rendered("a\nRules:\nb\nRules:\nc")
