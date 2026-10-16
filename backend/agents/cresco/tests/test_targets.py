"""Exercises the bounded member-target grammar and operator-approved admission: valid
targets parse, every malformed shape is refused, and admission is default-deny with
exact-tuple matching so discovery can never widen the actionable set.
"""

from __future__ import annotations

import pytest

from agents.cresco.targets import (
    MAX_TARGET_CHARS,
    TargetRefused,
    approved_targets,
    parse_target,
    require_approved,
    target_text,
)


@pytest.mark.parametrize("raw, expected", [
    ("r1_a1", ("r1", "a1")),
    ("r1_a1_executor", ("r1", "a1", "executor")),
    ("region-1_agent-2", ("region-1", "agent-2")),
    ("a" * 63 + "_" + "b", ("a" * 63, "b")),
    ("9x_0y", ("9x", "0y")),
])
def test_valid_targets_parse(raw, expected):
    assert parse_target(raw) == expected
    assert target_text(expected) == raw


@pytest.mark.parametrize("raw", [
    "", "r1", "r1_a1_b_c", "R1_a1", "r1__a1", "_a1", "r1_", "r1 a1", "r1/a1",
    "r1.a1", "r1*a1", "-r1_a1", "r1_-a1", "r1_" + "a" * 64, "r1_a1 ",
    "r1_" + "a" * 64 + "_x", "x" * (MAX_TARGET_CHARS + 1) + "_a1",
    "r1_a1_b_c_d", None, 42, ["r1", "a1"], "r1\t_a1", "r1\na1",
])
def test_malformed_targets_are_refused(raw):
    with pytest.raises(TargetRefused) as refused:
        parse_target(raw)
    assert refused.value.code == "invalid_target"


def test_approved_targets_read_the_operator_environment_and_skip_malformed_entries():
    env = {
        "CRESCO_APPROVED_TARGETS": "r1_a1, r1_a2_executor,,BAD..target,r2_a3;r3_a1",
    }
    assert approved_targets(env) == frozenset({"r1_a1", "r1_a2_executor", "r2_a3", "r3_a1"})


def test_approved_targets_default_to_an_empty_deny_set(monkeypatch):
    monkeypatch.delenv("CRESCO_APPROVED_TARGETS", raising=False)
    assert approved_targets() == frozenset()


def test_require_approved_accepts_only_exact_operator_approved_tuples():
    env = {"CRESCO_APPROVED_TARGETS": "r1_a1,r1_a2_executor"}
    assert require_approved("r1_a1", env) == ("r1", "a1")
    assert require_approved("r1_a2_executor", env) == ("r1", "a2", "executor")
    with pytest.raises(TargetRefused) as refused:
        require_approved("r1_a2", env)
    assert refused.value.code == "target_not_approved"
    with pytest.raises(TargetRefused):
        require_approved("r1_a1_extra", env)


def test_operator_revocation_removes_admission_immediately():
    env = {"CRESCO_APPROVED_TARGETS": "r1_a1"}
    assert require_approved("r1_a1", env) == ("r1", "a1")
    revoked = {"CRESCO_APPROVED_TARGETS": ""}
    with pytest.raises(TargetRefused) as refused:
        require_approved("r1_a1", revoked)
    assert refused.value.code == "target_not_approved"


def test_empty_components_and_wildcards_never_become_admitted():
    env = {"CRESCO_APPROVED_TARGETS": "r1_*,*_a1,__,_*"}
    assert approved_targets(env) == frozenset()
