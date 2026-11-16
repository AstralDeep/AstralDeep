"""Pins the Linux plan, the Landlock rules, the seccomp program, the mount table check, the forwarder and the inner launcher's steps on any host, without bubblewrap.
The plan must equal every committed vector byte for byte, and the confined runs live in test_exec_linux_live.py.
"""

from __future__ import annotations

import pytest
from astral_exec import ContextError, Policy, canonjson, linux
from exec_vectors import PROFILE_ERRORS, PROFILES, loaded, minimal_policy


@pytest.mark.parametrize("case", PROFILES, ids=[c["name"] for c in PROFILES])
def test_the_plan_equals_each_profile_vector_byte_for_byte(case):
    policy = Policy.parse(case["policy"], "linux")
    built = linux.plan(policy, case["context"]["egress_socket"])
    assert canonjson.canonical(built) == canonjson.canonical(case["linux"])


@pytest.mark.parametrize(
    "case", PROFILE_ERRORS, ids=[c["name"] for c in PROFILE_ERRORS]
)
def test_a_policy_the_darwin_fold_refuses_still_plans_on_linux(case):
    policy = Policy.parse(case["policy"], "linux")
    built = linux.plan(policy, case["context"].get("egress_socket"))
    assert canonjson.canonical(built) == canonjson.canonical(case["linux"])


HOSTED = minimal_policy(hosts=[{"host": "api.example.com", "port": 443}])


@pytest.mark.parametrize("socket", [None, "", "relative.sock", "/a/../b", 7])
def test_a_policy_with_hosts_needs_an_absolute_egress_socket(socket):
    with pytest.raises(ContextError):
        linux.plan(Policy.parse(HOSTED, "linux"), socket)


def test_a_policy_without_hosts_ignores_the_egress_socket():
    plain = Policy.parse(minimal_policy(), "linux")
    assert linux.plan(plain, None) == linux.plan(plain, "/run/x.sock")
    assert linux.plan(plain, None)["egress"] is None


def test_the_plan_refuses_a_policy_loaded_for_another_platform():
    with pytest.raises(ContextError):
        linux.plan(loaded(minimal_policy(), "darwin"), None)
    with pytest.raises(ContextError):
        linux.plan(minimal_policy(), None)  # type: ignore[arg-type]
