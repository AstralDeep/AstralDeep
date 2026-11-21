"""Checks retirement of the product planning target and the frozen prompt used by the offline benchmark."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from prompt_optimization import task

REPO_ROOT = Path(task.__file__).resolve().parents[2]


def test_loads_frozen_synthetic_planning_prompt():
    prompt = task.load_task_prompt(REPO_ROOT, task.SYNTHETIC_TASK_ID)
    assert prompt.task_id == task.SYNTHETIC_TASK_ID
    assert prompt.prompt_digest == "9ae14ce7f70e2ec975965030f76b44ef83142e4587f92964324485cbe1e2e148"
    assert task.INSTRUCTION_MARKER in prompt.head + task.INSTRUCTION_MARKER + prompt.rules
    assert prompt.head.strip()
    assert prompt.rules.strip()
    assert prompt.prompt_digest == hashlib.sha256(
        (prompt.head + task.INSTRUCTION_MARKER + prompt.rules).encode("utf-8")
    ).hexdigest()
    assert prompt.source_digest == hashlib.sha256(
        (REPO_ROOT / task.SOURCE_PATH).read_bytes()
    ).hexdigest()


@pytest.mark.parametrize("task_id", [task.RETIRED_TASK_ID, "unknown"])
def test_unavailable_tasks_fail_before_loading_a_fixture(tmp_path, task_id):
    with pytest.raises(task.TaskSeamError, match="retired|unknown"):
        task.load_task_prompt(tmp_path, task_id)


def test_render_replaces_only_allowlisted_field():
    prompt = task.load_task_prompt(REPO_ROOT, task.SYNTHETIC_TASK_ID)
    rendered = prompt.render("Custom instruction line.\n")
    head, rules = task.split_rendered(rendered)
    assert head == prompt.head
    assert rules == "Custom instruction line."
    assert rendered.count(task.INSTRUCTION_MARKER) == 1


def test_render_validates_instruction():
    prompt = task.load_task_prompt(REPO_ROOT, task.SYNTHETIC_TASK_ID)
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


@pytest.mark.parametrize("raw", [b"", b"no marker", b"head\nRules:\n", b"\nRules:\nrules",
                                 b"head\nRules:\none\nRules:\ntwo", b"\xff"])
def test_malformed_fixture_fails_closed(tmp_path, raw):
    fixture = tmp_path / task.SOURCE_PATH
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(raw)
    with pytest.raises(task.TaskSeamError):
        task.load_task_prompt(tmp_path, task.SYNTHETIC_TASK_ID)


def test_missing_fixture_fails_closed(tmp_path):
    with pytest.raises(task.TaskSeamError, match="unreadable"):
        task.load_task_prompt(tmp_path, task.SYNTHETIC_TASK_ID)


def test_split_rendered_requires_single_marker():
    with pytest.raises(task.TaskSeamError):
        task.split_rendered("no marker")
    with pytest.raises(task.TaskSeamError):
        task.split_rendered("a\nRules:\nb\nRules:\nc")
