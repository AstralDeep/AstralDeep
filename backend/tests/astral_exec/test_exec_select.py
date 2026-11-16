"""Runs every committed selection, context error and profile error vector through astral_exec.select, and pins that no setting can turn a refusal into a run.
It needs no conftest and no network, so the Linux harness runs it as is.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import astral_exec
import pytest
from astral_exec import (
    Backend,
    ContextError,
    ExecRefused,
    Policy,
    PolicyError,
    Refusal,
    select,
)
from astral_exec.constants import MESSAGES
from exec_vectors import (
    CONSTANTS,
    CONTEXT_ERRORS,
    PROFILE_ERRORS,
    SELECTION,
    minimal_policy,
)

GOOD_FACTS = {
    "platform": "linux",
    "landlock_abi": 6,
    "bwrap": True,
    "userns": True,
    "sandbox_exec": False,
    "app_sandboxed": False,
}


def _as_vector_result(outcome):
    if isinstance(outcome, Backend):
        return {"ok": True, "backend": outcome.name}
    assert isinstance(outcome, Refusal)
    return outcome.to_dict()


@pytest.mark.parametrize("case", SELECTION, ids=[c["name"] for c in SELECTION])
def test_selection_vector(case):
    expected = case["result"]
    policy = Policy.parse(case["policy"], "linux")
    if "policy_error" in expected:
        with pytest.raises(PolicyError) as caught:
            select(policy, case["facts"], case["data_dir"])
        assert caught.value.code == expected["policy_error"]
        return
    assert (
        _as_vector_result(select(policy, case["facts"], case["data_dir"])) == expected
    )


@pytest.mark.parametrize(
    "case", CONTEXT_ERRORS, ids=[c["name"] for c in CONTEXT_ERRORS]
)
def test_context_error_vector(case):
    platform = (
        case["facts"].get("platform") if isinstance(case["facts"], dict) else None
    )
    policy = Policy.parse(
        minimal_policy(), platform if isinstance(platform, str) else "linux"
    )
    with pytest.raises(ContextError):
        select(policy, case["facts"], case["data_dir"])


@pytest.mark.parametrize(
    "case", PROFILE_ERRORS, ids=[c["name"] for c in PROFILE_ERRORS]
)
def test_profile_error_vector(case):
    with pytest.raises(PolicyError) as caught:
        Policy.parse(case["policy"], "darwin")
    assert caught.value.code == case["code"]
    assert Policy.parse(case["policy"], "linux").document == case["policy"]


def test_selection_covers_every_reason_and_both_backends():
    results = [c["result"] for c in SELECTION]
    assert {r.get("reason") for r in results if not r.get("ok", True)} >= set(
        CONSTANTS["backend_reasons"]
    )
    assert {r.get("backend") for r in results if r.get("ok")} == {"seatbelt", "linux"}


def test_constants_match_the_committed_vectors():
    assert dict(MESSAGES) == CONSTANTS["messages"]
    assert set(astral_exec.constants.EXEC_CODES) == {
        code for code in CONSTANTS["messages"] if code.startswith("exec.")
    }
    assert set(astral_exec.constants.MESSAGES) == set(CONSTANTS["messages"])
    assert astral_exec.constants.LINUX_BASE == tuple(CONSTANTS["linux_base_reads"])
    assert astral_exec.constants.DARWIN_BASE == tuple(CONSTANTS["darwin_base_reads"])
    assert astral_exec.constants.LANDLOCK_ABI == CONSTANTS["landlock_abi"]
    assert astral_exec.constants.CORE_LIMIT_BYTES == CONSTANTS["core_limit_bytes"]
    assert astral_exec.constants.EXEC_SUBDIRECTORY == CONSTANTS["exec_subdirectory"]
    assert astral_exec.constants.FACT_KEYS == tuple(CONSTANTS["fact_keys"])
    assert astral_exec.constants.BACKEND_REASONS == tuple(CONSTANTS["backend_reasons"])


def test_refusal_messages_name_the_missing_piece():
    old = select(
        Policy.parse(minimal_policy(), "linux"),
        {**GOOD_FACTS, "landlock_abi": 5},
        "/var/lib/astral/node",
    )
    assert isinstance(old, Refusal)
    assert old.code == "exec.backend_unavailable"
    assert old.reason == "landlock_abi"
    assert "ABI 5" in old.message and "ABI 6" in old.message
    assert old.details == {"observed": 5, "required": 6}
    plain = select(
        Policy.parse(minimal_policy(), "linux"),
        {**GOOD_FACTS, "bwrap": False},
        "/var/lib/astral/node",
    )
    assert isinstance(plain, Refusal) and "bubblewrap" in plain.message
    other = select(
        Policy.parse(minimal_policy(), "win32"),
        {**GOOD_FACTS, "platform": "win32"},
        "/var/lib/astral/node",
    )
    assert isinstance(other, Refusal) and "win32" in other.message


def test_refusal_can_be_raised_and_keeps_its_fields():
    refusal = Refusal("exec.shadow_refused", MESSAGES["exec.shadow_refused"])
    error = ExecRefused(refusal)
    assert error.refusal is refusal
    assert error.code == "exec.shadow_refused"
    assert str(error) == MESSAGES["exec.shadow_refused"]
    assert refusal.to_dict() == {
        "ok": False,
        "code": "exec.shadow_refused",
        "message": MESSAGES["exec.shadow_refused"],
    }


def test_a_refusal_is_hashable_and_immutable():
    refusal = Refusal(
        "exec.backend_unavailable",
        "landlock bad",
        reason="landlock_abi",
        extras=(("observed", 1),),
    )
    assert hash(refusal) == hash(
        Refusal(
            "exec.backend_unavailable",
            "landlock bad",
            reason="landlock_abi",
            extras=(("observed", 1),),
        )
    )
    with pytest.raises(AttributeError):
        refusal.code = "x"  # type: ignore[misc]


def test_a_backend_is_never_chosen_without_all_facts():
    outcome = select(
        Policy.parse(minimal_policy(), "linux"), GOOD_FACTS, "/var/lib/astral/node"
    )
    assert outcome == Backend("linux")
    for key in ("landlock_abi", "bwrap", "userns", "sandbox_exec", "app_sandboxed"):
        broken = {k: v for k, v in GOOD_FACTS.items() if k != key}
        with pytest.raises(ContextError):
            select(
                Policy.parse(minimal_policy(), "linux"), broken, "/var/lib/astral/node"
            )


def test_select_rejects_an_unvalidated_policy_argument():
    with pytest.raises(TypeError):
        select(minimal_policy(), GOOD_FACTS, "/var/lib/astral/node")  # type: ignore[arg-type]


def test_no_environment_or_option_turns_a_refusal_into_a_run(monkeypatch):
    for name in (
        "ASTRAL_ENV",
        "ASTRAL_EXEC_MODE",
        "ASTRAL_EXEC_ALLOW_UNCONFINED",
        "FF_EXEC",
    ):
        monkeypatch.setenv(name, "1")
    facts = {**GOOD_FACTS, "bwrap": False}
    outcome = select(
        Policy.parse(minimal_policy(), "linux"), facts, "/var/lib/astral/node"
    )
    assert isinstance(outcome, Refusal) and outcome.reason == "bwrap_missing"
    assert list(inspect.signature(select).parameters) == ["policy", "facts", "data_dir"]


def test_the_selection_modules_never_read_the_environment():
    package = pathlib.Path(astral_exec.__file__).parent
    for name in ("select.py", "policy.py", "errors.py", "constants.py"):
        tree = ast.parse((package / name).read_text(encoding="utf-8"))
        used = {
            node.attr if isinstance(node, ast.Attribute) else node.id
            for node in ast.walk(tree)
            if isinstance(node, (ast.Attribute, ast.Name))
        }
        assert not used & {"environ", "getenv", "putenv"}, name
