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
from astral_exec.constants import BWRAP_PATHS, MESSAGES
from astral_exec.facts import Facts
from astral_exec.select import _decide
from exec_vectors import (
    CONSTANTS,
    CONTEXT_ERRORS,
    PROFILE_ERRORS,
    DATA_DIR,
    SELECTION,
    good_facts,
    loaded,
    minimal_policy,
)


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
            _decide(policy, case["facts"], case["data_dir"])
        assert caught.value.code == expected["policy_error"]
        return
    assert (
        _as_vector_result(_decide(policy, case["facts"], case["data_dir"])) == expected
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
        _decide(policy, case["facts"], case["data_dir"])


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
    assert astral_exec.constants.AGENT_SERVER_REASONS == tuple(CONSTANTS["agent_server_reasons"])


def test_refusal_messages_name_the_missing_piece():
    old = select(loaded(minimal_policy()), good_facts(landlock_abi=5), DATA_DIR)
    assert isinstance(old, Refusal)
    assert old.code == "exec.backend_unavailable"
    assert old.reason == "landlock_abi"
    assert "ABI 5" in old.message and "ABI 6" in old.message
    assert old.details == {"observed": 5, "required": 6}
    plain = select(loaded(minimal_policy()), good_facts(bwrap=False), DATA_DIR)
    assert isinstance(plain, Refusal) and "bubblewrap" in plain.message
    other = select(
        loaded(minimal_policy(), "win32"), good_facts(platform="win32"), DATA_DIR
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
    outcome = select(loaded(minimal_policy()), good_facts(), DATA_DIR)
    assert outcome == Backend("linux") and outcome.bwrap == "/usr/bin/bwrap"
    for key in ("landlock_abi", "bwrap", "userns", "sandbox_exec", "app_sandboxed"):
        broken = good_facts()
        del broken[key]
        with pytest.raises(ContextError):
            select(loaded(minimal_policy()), broken, DATA_DIR)


def test_select_rejects_an_unvalidated_policy_argument():
    with pytest.raises(TypeError):
        select(minimal_policy(), good_facts(), DATA_DIR)  # type: ignore[arg-type]


def test_a_parse_only_policy_never_gets_a_backend():
    parsed = Policy.parse(minimal_policy(), "linux")
    assert parsed.loaded_for is None
    with pytest.raises(ContextError, match="loaded"):
        select(parsed, good_facts(), DATA_DIR)


def test_a_loaded_policy_records_its_platform_data_dir_and_home():
    policy = loaded(minimal_policy())
    assert policy.loaded_for == ("linux", DATA_DIR, "/home/test")


def test_a_different_data_dir_or_platform_is_refused():
    policy = loaded(minimal_policy())
    with pytest.raises(ContextError, match="data directory"):
        select(policy, good_facts(), "/var/lib/astral/other")
    with pytest.raises(ContextError, match="platform"):
        select(policy, good_facts(platform="darwin"), DATA_DIR)
    with pytest.raises(ContextError, match="platform"):
        select(policy, {"landlock_abi": 6}, DATA_DIR)
    with pytest.raises(ContextError, match="platform"):
        select(policy, None, DATA_DIR)  # type: ignore[arg-type]


def test_a_loaded_darwin_policy_selects_seatbelt():
    policy = loaded(minimal_policy(), "darwin")
    facts = Facts(
        platform="darwin",
        landlock_abi=0,
        bwrap=False,
        userns=False,
        euid=501,
        sandbox_exec=True,
        app_sandboxed=False,
    )
    outcome = select(policy, facts, DATA_DIR)
    assert outcome == Backend("seatbelt") and outcome.bwrap is None


def test_linux_needs_a_carried_bwrap_path_from_the_fixed_list():
    policy = loaded(minimal_policy())
    plain = dict(good_facts())
    for facts in (plain, Facts(plain)):
        outcome = select(policy, facts, DATA_DIR)
        assert isinstance(outcome, Refusal) and outcome.reason == "bwrap_missing"
    for path in ("bwrap", "/usr/local/bin/bwrap", "./bwrap", "/tmp/bwrap", ""):
        facts = good_facts()
        facts.bwrap_path = path
        outcome = select(policy, facts, DATA_DIR)
        assert isinstance(outcome, Refusal) and outcome.reason == "bwrap_missing"
    for path in BWRAP_PATHS:
        facts = good_facts()
        facts.bwrap_path = path
        assert select(policy, facts, DATA_DIR).bwrap == path


def test_the_vector_table_still_accepts_plain_dict_facts():
    outcome = _decide(
        Policy.parse(minimal_policy(), "linux"), dict(good_facts()), DATA_DIR
    )
    assert outcome == Backend("linux")


def test_no_environment_or_option_turns_a_refusal_into_a_run(monkeypatch):
    for name in (
        "ASTRAL_ENV",
        "ASTRAL_EXEC_MODE",
        "ASTRAL_EXEC_ALLOW_UNCONFINED",
        "FF_EXEC",
    ):
        monkeypatch.setenv(name, "1")
    outcome = select(loaded(minimal_policy()), good_facts(bwrap=False), DATA_DIR)
    assert isinstance(outcome, Refusal) and outcome.reason == "bwrap_missing"
    assert list(inspect.signature(select).parameters) == ["policy", "facts", "data_dir"]


ENVIRONMENT_NAMES = {"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"}


def environment_reads(source: str) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.alias):
            names.add(node.name.split(".")[-1])
    return names & ENVIRONMENT_NAMES


def test_the_environment_scanner_sees_every_spelling():
    for source in (
        "import os\nos.environ",
        "from os import environ as e",
        "from os import getenv",
        "from os import environb",
        "import os\nos.unsetenv('x')",
        "import os\nos.putenv('x', 'y')",
        "import os.environ",
    ):
        assert environment_reads(source), source
    assert environment_reads("import os\nos.stat('/')") == set()


def test_no_module_except_facts_reads_the_environment():
    package = pathlib.Path(astral_exec.__file__).parent
    modules = sorted(package.rglob("*.py"))
    assert {m.name for m in modules} >= {"fold_table.py", "select.py", "policy.py"}
    for module in modules:
        if module.name == "facts.py" or "conformance" in module.parts:
            continue
        assert environment_reads(module.read_text(encoding="utf-8")) == set(), (
            module.name
        )


def test_a_refusal_is_falsy_and_a_backend_truthy_so_truth_tests_cannot_run_a_refusal():
    refused = select(loaded(minimal_policy()), good_facts(bwrap=False), DATA_DIR)
    granted = select(loaded(minimal_policy()), good_facts(), DATA_DIR)
    assert refused.ok is False and not refused
    assert granted.ok is True and granted
    assert (refused.to_dict()["ok"], granted.ok) == (False, True)
    for case in SELECTION:
        if "policy_error" in case["result"]:
            continue
        outcome = _decide(
            Policy.parse(case["policy"], "linux"), case["facts"], case["data_dir"]
        )
        assert bool(outcome) is case["result"]["ok"]
        assert outcome.ok is case["result"]["ok"]
