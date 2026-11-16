"""Pins every exec policy vector: each validation code, the canonical Seatbelt text, the Linux plan and every backend selection outcome.
A failure means the contract document, the committed vectors and the reference module disagree.
"""

import copy
import json
from pathlib import Path

import exec_policy
import pytest

VECTORS = Path(__file__).resolve().parents[2] / "vectors" / "exec-policy-v1.json"
DOC = json.loads(VECTORS.read_text(encoding="utf-8"))
VALIDATION = {case["name"]: case for case in DOC["validation"]}
PROFILES = {case["name"]: case for case in DOC["profiles"]}
SELECTION = {case["name"]: case for case in DOC["selection"]}

EXPECTED_VALIDATION = {
    "minimal_valid": "ok",
    "full_valid": "ok",
    "reads_64_accepted": "ok",
    "reads_65": "policy.too_many_entries",
    "hosts_64_accepted": "ok",
    "hosts_65": "policy.too_many_entries",
    "hosts_punycode_and_port_80": "ok",
    "limits_at_minimum": "ok",
    "limits_at_maximum": "ok",
    "wall_ms_at_minimum": "ok",
    "wall_ms_at_maximum": "ok",
    "not_json": "policy.malformed",
    "duplicate_key": "policy.malformed",
    "float_number": "policy.malformed",
    "array_root": "policy.malformed",
    "version_missing": "policy.malformed",
    "version_two": "policy.malformed",
    "version_true": "policy.malformed",
    "reads_missing": "policy.malformed",
    "reads_not_list": "policy.malformed",
    "read_not_string": "policy.malformed",
    "workspace_not_string": "policy.malformed",
    "hosts_not_list": "policy.malformed",
    "host_entry_not_object": "policy.malformed",
    "host_missing_port": "policy.malformed",
    "host_port_not_integer": "policy.malformed",
    "limits_not_object": "policy.malformed",
    "limits_missing_key": "policy.malformed",
    "limit_not_integer": "policy.malformed",
    "limit_bool": "policy.malformed",
    "time_not_object": "policy.malformed",
    "time_missing_wall_ms": "policy.malformed",
    "unknown_top_level_key": "policy.unknown_field",
    "unknown_listen_field": "policy.unknown_field",
    "unknown_host_key": "policy.unknown_field",
    "unknown_limits_key": "policy.unknown_field",
    "unknown_time_key": "policy.unknown_field",
    "unknown_beats_missing": "policy.unknown_field",
    "read_relative": "policy.path_invalid",
    "read_root": "policy.path_invalid",
    "read_dotdot": "policy.path_invalid",
    "read_dot_component": "policy.path_invalid",
    "read_trailing_slash": "policy.path_invalid",
    "read_double_slash": "policy.path_invalid",
    "read_nul": "policy.path_invalid",
    "read_newline": "policy.path_invalid",
    "read_too_long": "policy.path_invalid",
    "reads_unsorted": "policy.path_invalid",
    "reads_duplicate": "policy.path_invalid",
    "workspace_relative": "policy.path_invalid",
    "workspace_root": "policy.path_invalid",
    "read_inside_workspace": "policy.path_invalid",
    "read_contains_workspace": "policy.path_invalid",
    "read_equals_workspace": "policy.path_invalid",
    "too_many_beats_bad_entry": "policy.too_many_entries",
    "host_uppercase": "policy.host_invalid",
    "host_no_dot": "policy.host_invalid",
    "host_ipv4_literal": "policy.host_invalid",
    "host_ipv6_literal": "policy.host_invalid",
    "host_wildcard": "policy.host_invalid",
    "host_trailing_dot": "policy.host_invalid",
    "host_underscore": "policy.host_invalid",
    "host_label_too_long": "policy.host_invalid",
    "host_numeric_tld": "policy.host_invalid",
    "host_empty_label": "policy.host_invalid",
    "host_port_8443": "policy.host_invalid",
    "host_port_zero": "policy.host_invalid",
    "hosts_unsorted": "policy.host_invalid",
    "hosts_duplicate": "policy.host_invalid",
    "memory_below_minimum": "policy.limit_out_of_range",
    "memory_above_maximum": "policy.limit_out_of_range",
    "processes_zero": "policy.limit_out_of_range",
    "processes_above_maximum": "policy.limit_out_of_range",
    "open_files_below_minimum": "policy.limit_out_of_range",
    "open_files_above_maximum": "policy.limit_out_of_range",
    "file_bytes_below_minimum": "policy.limit_out_of_range",
    "file_bytes_above_maximum": "policy.limit_out_of_range",
    "wall_ms_zero": "policy.time_out_of_range",
    "wall_ms_above_maximum": "policy.time_out_of_range",
    "wall_ms_negative": "policy.time_out_of_range",
    "path_beats_limit": "policy.path_invalid",
    "limit_beats_time": "policy.limit_out_of_range",
}

EXPECTED_SELECTION = {
    "darwin_ok": ("ok", "seatbelt"),
    "linux_ok": ("ok", "linux"),
    "linux_abi_7_ok": ("ok", "linux"),
    "linux_abi_5": ("exec.backend_unavailable", "landlock_abi"),
    "linux_abi_0": ("exec.backend_unavailable", "landlock_abi"),
    "linux_no_bwrap": ("exec.backend_unavailable", "bwrap_missing"),
    "linux_userns_denied": ("exec.backend_unavailable", "userns_denied"),
    "linux_abi_beats_bwrap": ("exec.backend_unavailable", "landlock_abi"),
    "linux_bwrap_beats_userns": ("exec.backend_unavailable", "bwrap_missing"),
    "darwin_no_sandbox_exec": ("exec.backend_unavailable", "sandbox_exec_missing"),
    "darwin_app_sandboxed": ("exec.backend_unavailable", "app_sandboxed"),
    "darwin_missing_beats_sandboxed": (
        "exec.backend_unavailable",
        "sandbox_exec_missing",
    ),
    "windows": ("exec.backend_unavailable", "platform_unsupported"),
    "shadow_refused_darwin": ("exec.shadow_refused", None),
    "shadow_refused_linux": ("exec.shadow_refused", None),
    "read_root_contains_data_dir": ("exec.policy_overlaps_data", None),
    "read_root_equals_data_dir": ("exec.policy_overlaps_data", None),
    "workspace_contains_data_dir": ("exec.policy_overlaps_data", None),
    "base_root_contains_data_dir_darwin": ("exec.policy_overlaps_data", None),
    "base_root_contains_data_dir_linux": ("exec.policy_overlaps_data", None),
    "workspace_inside_data_dir_ok": ("ok", "seatbelt"),
    "sibling_of_data_dir_ok": ("ok", "seatbelt"),
    "overlap_beats_shadow": ("exec.policy_overlaps_data", None),
    "overlap_beats_unavailable": ("exec.policy_overlaps_data", None),
    "shadow_beats_unavailable": ("exec.shadow_refused", None),
}


def outcome(case: dict) -> str:
    try:
        exec_policy.parse(case["raw"] if "raw" in case else case["policy"])
    except exec_policy.PolicyError as error:
        return error.code
    return "ok"


def test_validation_case_names_match_expectations() -> None:
    assert set(VALIDATION) == set(EXPECTED_VALIDATION)


@pytest.mark.parametrize("name", sorted(EXPECTED_VALIDATION))
def test_validation_case(name: str) -> None:
    case = VALIDATION[name]
    assert case["code"] == EXPECTED_VALIDATION[name]
    assert outcome(case) == EXPECTED_VALIDATION[name]


def test_every_validation_code_has_a_case() -> None:
    seen = {code for code in EXPECTED_VALIDATION.values() if code != "ok"}
    assert seen == set(exec_policy.VALIDATION_CODES)


def test_unknown_key_is_refused_not_ignored() -> None:
    policy = copy.deepcopy(PROFILES["minimal"]["policy"])
    policy["extra"] = 1
    with pytest.raises(exec_policy.PolicyError) as caught:
        exec_policy.parse(policy)
    assert caught.value.code == "policy.unknown_field"


def test_parse_accepts_bytes_and_text_with_one_digest() -> None:
    policy = PROFILES["minimal"]["policy"]
    raw = json.dumps(policy, indent=3)
    from_text = exec_policy.parse(raw)
    from_bytes = exec_policy.parse(raw.encode("utf-8"))
    assert from_text == from_bytes == policy
    assert exec_policy.digest(from_text) == PROFILES["minimal"]["digest"]


def test_digest_is_sha256_over_lets_cj_bytes() -> None:
    import hashlib

    policy = PROFILES["full"]["policy"]
    canonical = json.dumps(
        policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert (
        exec_policy.digest(policy) == "sha256:" + hashlib.sha256(canonical).hexdigest()
    )
    assert PROFILES["full"]["digest"] == exec_policy.digest(policy)


MINIMAL_PROFILE = """\
(version 1)
(deny default)
(import "system.sb")

(allow process-exec*)
(allow process-info* (target self))
(allow signal (target self))
(allow mach-lookup)
(allow sysctl-read)
(allow file-ioctl)

(allow file-read* (subpath "/usr"))
(allow file-read* (subpath "/System"))
(allow file-read* (subpath "/Library"))
(allow file-read* (subpath "/private/etc"))
(allow file-read* (subpath "/private/var/db/dyld"))
(allow file-read* (subpath "/dev"))
(allow file-read* file-write* (subpath "/private/tmp/astral/ws"))

(deny process-exec* (subpath "/private/tmp/astral/ws"))
(deny file-read* (subpath "/Users/test/Library/Keychains"))
(deny file-read* (subpath "/Library/Keychains"))
(deny file-read* (subpath "/System/Library/Keychains"))
"""


def test_minimal_profile_is_the_golden_text() -> None:
    case = PROFILES["minimal"]
    ctx = case["context"]
    text = exec_policy.seatbelt_profile(case["policy"], ctx["home"], ctx["proxy_port"])
    assert text == MINIMAL_PROFILE
    assert case["seatbelt"] == MINIMAL_PROFILE


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_profile_matches_vector(name: str) -> None:
    case = PROFILES[name]
    ctx = case["context"]
    text = exec_policy.seatbelt_profile(case["policy"], ctx["home"], ctx["proxy_port"])
    assert text == case["seatbelt"]
    plan = exec_policy.linux_plan(case["policy"], ctx["egress_socket"])
    assert plan == case["linux"]
    assert exec_policy.digest(case["policy"]) == case["digest"]


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_profile_never_opens_unwanted_rights(name: str) -> None:
    text = PROFILES[name]["seatbelt"]
    assert '(allow file-read* (subpath "/"))' not in text
    for banned in (
        "network-bind",
        "network-inbound",
        "mDNSResponder",
        "ipc-sysv",
        "network*",
    ):
        assert banned not in text
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert text.index("(deny process-exec*") > text.index("(allow process-exec*)")
    assert text.index("(deny file-read*") > text.rindex("(allow file-read*")


def test_processes_one_has_no_fork_and_two_has_fork() -> None:
    one, two = PROFILES["minimal"], PROFILES["processes_two"]
    assert one["policy"]["limits"]["processes"] == 1
    assert two["policy"]["limits"]["processes"] == 2
    assert "process-fork" not in one["seatbelt"]
    assert two["seatbelt"].count("(allow process-fork)\n") == 1
    assert one["policy"]["workspace"] == two["policy"]["workspace"]
    assert two["seatbelt"].replace("(allow process-fork)\n", "") == one["seatbelt"]
    assert one["linux"]["seccomp"]["deny_fork"] is True
    assert two["linux"]["seccomp"]["deny_fork"] is False


def test_empty_hosts_means_no_network_and_nonempty_means_proxy_port_only() -> None:
    quiet, hosted = PROFILES["minimal"], PROFILES["full"]
    assert "network" not in quiet["seatbelt"]
    assert quiet["linux"]["egress"] is None
    assert quiet["linux"]["landlock"]["tcp_connect"] == []
    assert quiet["linux"]["seccomp"]["inet_stream_sockets"] is False
    port = hosted["context"]["proxy_port"]
    lines = [line for line in hosted["seatbelt"].splitlines() if "network" in line]
    assert lines == [f'(allow network-outbound (remote ip "localhost:{port}"))']
    assert hosted["linux"]["landlock"]["tcp_connect"] == [3128]
    assert hosted["linux"]["egress"] == {
        "listen": "127.0.0.1:3128",
        "socket": "/run/astral/egress.sock",
    }
    assert hosted["linux"]["seccomp"]["inet_stream_sockets"] is True
    args = hosted["linux"]["bwrap"]
    at = args.index("--bind", args.index("--proc"))
    assert args[at : at + 3] == [
        "--bind",
        hosted["context"]["egress_socket"],
        "/run/astral/egress.sock",
    ]


def test_hosts_without_a_proxy_port_or_socket_cannot_render() -> None:
    policy = PROFILES["full"]["policy"]
    with pytest.raises(exec_policy.ContextError):
        exec_policy.seatbelt_profile(policy, "/Users/test", None)
    with pytest.raises(exec_policy.ContextError):
        exec_policy.linux_plan(policy, None)
    with pytest.raises(exec_policy.ContextError):
        exec_policy.seatbelt_profile(policy, "relative/home", 3128)
    with pytest.raises(exec_policy.ContextError):
        exec_policy.seatbelt_profile(policy, "/Users/test", 70000)


def test_quoting_escapes_backslash_and_quote_only() -> None:
    text = PROFILES["quoting"]["seatbelt"]
    workspace = PROFILES["quoting"]["policy"]["workspace"]
    assert '"' in workspace and "\\" in workspace
    escaped = workspace.replace("\\", "\\\\").replace('"', '\\"')
    assert f'(subpath "{escaped}")' in text
    assert "é" in text


def test_linux_plan_shape() -> None:
    plan = PROFILES["full"]["linux"]
    args = plan["bwrap"]
    for flag in (
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
    ):
        assert flag in args
    base = DOC["constants"]["linux_base_reads"]
    assert "/opt" not in base
    assert plan["landlock"]["exec_read"][: len(base)] == base
    assert plan["landlock"]["abi"] == 6
    assert plan["landlock"]["workspace"] == PROFILES["full"]["policy"]["workspace"]
    assert (
        plan["rlimits"]["RLIMIT_AS"]
        == PROFILES["full"]["policy"]["limits"]["memory_bytes"]
    )
    assert "shmget" in plan["seccomp"]["deny_syscalls"]
    assert "setsid" in plan["seccomp"]["deny_syscalls"]
    assert plan["seccomp"]["deny_socket_types"] == ["SOCK_DGRAM", "SOCK_RAW"]


@pytest.mark.parametrize("name", sorted(EXPECTED_SELECTION))
def test_selection_case(name: str) -> None:
    case = SELECTION[name]
    code, reason = EXPECTED_SELECTION[name]
    policy = case["policy"]
    result = exec_policy.select(policy, case["facts"], case["data_dir"])
    assert result == case["result"]
    if code == "ok":
        assert result == {"ok": True, "backend": reason}
    else:
        assert result["ok"] is False and result["code"] == code
        assert result.get("reason") == reason
        assert result["message"]


def test_selection_case_names_match_expectations() -> None:
    assert set(SELECTION) == set(EXPECTED_SELECTION)


def test_every_selection_reason_has_a_case() -> None:
    reasons = {reason for _, reason in EXPECTED_SELECTION.values() if reason}
    assert reasons - {"seatbelt", "linux"} == set(exec_policy.BACKEND_REASONS)


def test_landlock_refusal_reports_observed_and_required_abi() -> None:
    result = SELECTION["linux_abi_5"]["result"]
    assert result["observed"] == 5 and result["required"] == 6
    assert "5" in result["message"] and "6" in result["message"]


def test_selection_rejects_malformed_facts_and_data_dir() -> None:
    policy = PROFILES["minimal"]["policy"]
    facts = SELECTION["linux_ok"]["facts"]
    with pytest.raises(exec_policy.ContextError):
        exec_policy.select(policy, {"platform": "linux"}, "/var/astral")
    with pytest.raises(exec_policy.ContextError):
        exec_policy.select(policy, facts, "relative")
    with pytest.raises(exec_policy.ContextError):
        exec_policy.select(policy, {**facts, "landlock_abi": True}, "/var/astral")
    with pytest.raises(exec_policy.ContextError):
        exec_policy.select(policy, {**facts, "mode": "audit"}, "/var/astral")


def test_every_code_has_a_message() -> None:
    assert set(DOC["constants"]["messages"]) == set(exec_policy.MESSAGES)
    for code in (
        "exec.agent_server_unsupported",
        "exec.shadow_refused",
        "exec.policy_overlaps_data",
        "exec.time_limit",
        "exec.memory_limit",
        "exec.process_limit",
        "exec.file_limit",
        "exec.launch_failed",
        "exec.backend_unavailable",
    ):
        assert code in exec_policy.MESSAGES


def test_constants_in_vectors_match_reference() -> None:
    constants = DOC["constants"]
    assert constants["format"] == "astral.exec-policy/v1"
    assert constants["darwin_base_reads"] == list(exec_policy.DARWIN_BASE)
    assert constants["linux_base_reads"] == list(exec_policy.LINUX_BASE)
    assert constants["limits"] == {
        key: list(bounds) for key, bounds in exec_policy.LIMITS.items()
    }
    assert constants["wall_ms"] == list(exec_policy.WALL_MS)
    assert constants["landlock_abi"] == 6


def test_generator_output_matches_committed_vectors() -> None:
    import generate_exec_vectors

    assert generate_exec_vectors.render() == VECTORS.read_text(encoding="utf-8")
