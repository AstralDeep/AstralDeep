"""Pins every exec policy vector: each validation code, the canonical Seatbelt text, the Linux plan and every backend selection outcome.
A failure means the contract document, the committed vectors and the reference module disagree.
"""

import copy
import json
import re
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
    "read_del_character": "policy.path_invalid",
    "read_4096_bytes_accepted": "ok",
    "workspace_dotdot": "policy.path_invalid",
    "workspace_trailing_slash": "policy.path_invalid",
    "reads_nfc_nfd_pair_linux": "ok",
    "reads_nfc_nfd_pair_darwin": "policy.path_invalid",
    "reads_case_pair_linux": "ok",
    "reads_case_pair_darwin": "policy.path_invalid",
    "read_case_variant_of_workspace_linux": "ok",
    "read_case_variant_of_workspace_darwin": "policy.path_invalid",
    "host_over_253_characters": "policy.host_invalid",
    "host_leading_hyphen": "policy.host_invalid",
    "host_trailing_hyphen": "policy.host_invalid",
    "host_ipv4_with_hex_octet": "policy.host_invalid",
    "host_hex_ipv4": "policy.host_invalid",
    "host_bare_0x_label": "policy.host_invalid",
    "host_digits_then_letters_ok": "ok",
    "host_not_string": "policy.malformed",
    "host_port_true": "policy.malformed",
    "wall_ms_true": "policy.malformed",
    "wall_ms_string": "policy.malformed",
    "bad_workspace_beats_bad_host": "policy.path_invalid",
    "bad_host_beats_unknown_time_key": "policy.host_invalid",
    "overlap_beats_bad_host": "policy.path_invalid",
    "bad_limit_beats_unknown_time_key": "policy.limit_out_of_range",
    "float_in_extra_key": "policy.malformed",
    "two_to_the_64_in_extra_key": "policy.malformed",
    "float_in_limit": "policy.malformed",
    "two_to_the_64_in_limit": "policy.malformed",
    "read_multibyte_over_4096_bytes": "policy.path_invalid",
    "read_multibyte_4096_bytes_accepted": "ok",
    "reads_sharp_s_pair_darwin": "policy.path_invalid",
    "reads_sharp_s_pair_linux": "ok",
    "reads_ligature_pair_darwin": "policy.path_invalid",
    "reads_ligature_pair_linux": "ok",
    "read_unassigned_darwin": "policy.path_invalid",
    "read_unassigned_linux": "ok",
    "workspace_unassigned_darwin": "policy.path_invalid",
    "reads_hangul_pair_darwin": "policy.path_invalid",
    "reads_hangul_tail_pair_darwin": "policy.path_invalid",
    "reads_mark_order_pair_darwin": "policy.path_invalid",
    "reads_mark_order_pair_before_base_darwin": "policy.path_invalid",
    "reads_same_class_marks_distinct_darwin": "ok",
    "reads_ypogegrammeni_pair_darwin": "policy.path_invalid",
    "reads_ypogegrammeni_pair_linux": "ok",
    "read_assigned_range_start_darwin": "ok",
    "read_assigned_range_end_darwin": "ok",
    "read_unassigned_after_range_darwin": "policy.path_invalid",
    "host_253_characters_accepted": "ok",
    "host_254_characters": "policy.host_invalid",
    "host_digit_then_letter_label_ok": "ok",
    "host_bad_hex_label_ok": "ok",
    "reads_precomposed_acute_dot_below_pair_darwin": "policy.path_invalid",
    "reads_precomposed_acute_dot_below_pair_linux": "ok",
    "reads_last_hangul_syllable_pair_darwin": "policy.path_invalid",
    "reads_last_hangul_syllable_pair_linux": "ok",
    "reads_precomposed_capital_pair_darwin": "policy.path_invalid",
    "reads_precomposed_capital_pair_linux": "ok",
    "reads_precomposed_ypogegrammeni_acute_pair_darwin": "policy.path_invalid",
    "reads_precomposed_ypogegrammeni_acute_pair_linux": "ok",
    "reads_precomposed_psili_ypogegrammeni_grave_pair_darwin": "policy.path_invalid",
    "reads_precomposed_psili_ypogegrammeni_grave_pair_linux": "ok",
    "read_name_prefix_of_workspace_ok": "ok",
    "read_name_prefix_of_workspace_darwin": "ok",
    "read_unit_separator": "policy.path_invalid",
    "read_case_variant_inside_workspace_darwin": "policy.path_invalid",
    "read_case_variant_inside_workspace_linux": "ok",
    "utf8_bom_prefix": "policy.malformed",
    "utf16le_with_bom": "policy.malformed",
    "utf16le_no_bom": "policy.malformed",
    "utf16be_with_bom": "policy.malformed",
    "utf16be_no_bom": "policy.malformed",
    "utf32le_with_bom": "policy.malformed",
    "utf32be_with_bom": "policy.malformed",
    "invalid_utf8_continuation": "policy.malformed",
    "invalid_utf8_overlong": "policy.malformed",
}

EXPECTED_SELECTION = {
    "darwin_ok": ("ok", "seatbelt"),
    "linux_ok": ("ok", "linux"),
    "linux_abi_7_ok": ("ok", "linux"),
    "linux_abi_5": ("exec.backend_unavailable", "landlock_abi"),
    "linux_abi_0": ("exec.backend_unavailable", "landlock_abi"),
    "linux_no_bwrap": ("exec.backend_unavailable", "bwrap_missing"),
    "linux_userns_denied": ("exec.backend_unavailable", "userns_denied"),
    "linux_root_node": ("exec.backend_unavailable", "root_node"),
    "linux_userns_beats_root": ("exec.backend_unavailable", "userns_denied"),
    "darwin_root_ok": ("ok", "seatbelt"),
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
    "workspace_inside_data_dir_refused": ("exec.policy_overlaps_data", None),
    "workspace_equals_data_dir_refused": ("exec.policy_overlaps_data", None),
    "workspace_under_data_exec_ok": ("ok", "seatbelt"),
    "darwin_case_pair_in_reads": ("policy.path_invalid", None),
    "linux_case_pair_in_reads_ok": ("ok", "linux"),
    "unicode_16_case_pair_read_root_darwin": ("exec.policy_overlaps_data", None),
    "unicode_16_case_pair_read_root_linux_ok": ("ok", "linux"),
    "darwin_unassigned_in_reads": ("policy.path_invalid", None),
    "ypogegrammeni_pair_read_root_darwin": ("exec.policy_overlaps_data", None),
    "ypogegrammeni_pair_read_root_linux_ok": ("ok", "linux"),
    "workspace_equals_data_exec_refused": ("exec.policy_overlaps_data", None),
    "workspace_exec_lookalike_refused": ("exec.policy_overlaps_data", None),
    "workspace_under_data_exec_linux_ok": ("ok", "linux"),
    "read_root_inside_data_dir_refused": ("exec.policy_overlaps_data", None),
    "case_variant_read_root_darwin": ("exec.policy_overlaps_data", None),
    "case_variant_read_root_linux_ok": ("ok", "linux"),
    "case_variant_workspace_under_exec_darwin_ok": ("ok", "seatbelt"),
    "case_variant_workspace_outside_exec_darwin": ("exec.policy_overlaps_data", None),
    "nfd_variant_read_root_darwin": ("exec.policy_overlaps_data", None),
    "nfd_variant_read_root_linux_ok": ("ok", "linux"),
    "sibling_of_data_dir_ok": ("ok", "seatbelt"),
    "overlap_beats_shadow": ("exec.policy_overlaps_data", None),
    "overlap_beats_unavailable": ("exec.policy_overlaps_data", None),
    "shadow_beats_unavailable": ("exec.shadow_refused", None),
    "case_variant_read_root_inside_data_dir_darwin": (
        "exec.policy_overlaps_data",
        None,
    ),
    "case_variant_read_root_inside_data_dir_linux_ok": ("ok", "linux"),
    "data_dir_inside_case_variant_workspace_darwin": (
        "exec.policy_overlaps_data",
        None,
    ),
    "data_dir_inside_case_variant_workspace_linux_ok": ("ok", "linux"),
    "precomposed_ypogegrammeni_read_root_darwin": ("exec.policy_overlaps_data", None),
    "precomposed_ypogegrammeni_read_root_linux_ok": ("ok", "linux"),
}


def outcome(case: dict) -> str:
    try:
        exec_policy.parse(
            bytes.fromhex(case["bytes_hex"])
            if "bytes_hex" in case
            else case["raw"]
            if "raw" in case
            else case["policy"],
            case.get("platform"),
        )
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
        exec_policy.parse(policy, None)
    assert caught.value.code == "policy.unknown_field"


def test_parse_accepts_bytes_and_text_with_one_digest() -> None:
    policy = PROFILES["minimal"]["policy"]
    raw = json.dumps(policy, indent=3)
    from_text = exec_policy.parse(raw, None)
    from_bytes = exec_policy.parse(raw.encode("utf-8"), None)
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

(allow process-exec* (subpath "/usr"))
(allow process-exec* (subpath "/System"))
(allow process-exec* (subpath "/Library"))
(allow process-exec* (subpath "/private/var/db/dyld"))
(allow process-exec* (subpath "/bin"))
(allow process-exec* (subpath "/sbin"))
(allow process-info* (target self))
(allow signal (target self))
(allow signal (target children))
(allow sysctl-read)
(allow file-ioctl)

(allow file-read* (subpath "/usr"))
(allow file-read* (subpath "/System"))
(allow file-read* (subpath "/Library"))
(allow file-read* (subpath "/private/etc"))
(allow file-read* (subpath "/private/var/db/dyld"))
(allow file-read* (subpath "/dev"))
(allow file-read-metadata (subpath "/bin") (subpath "/sbin"))
(allow file-read* file-write* (subpath "/private/tmp/astral/ws"))

(deny process-exec* (subpath "/private/tmp/astral/ws"))
(deny file-map-executable (subpath "/private/tmp/astral/ws"))
(deny mach-lookup (global-name "com.apple.dnssd.service"))
(deny mach-lookup (global-name "com.apple.trustd") (global-name "com.apple.trustd.agent") (global-name "com.apple.system.notification_center") (global-name "com.apple.analyticsd") (global-name "com.apple.analyticsd.messagetracer") (global-name "com.apple.diagnosticd") (global-name "com.apple.osanalytics.osanalyticshelper") (xpc-service-name-prefix ""))
(deny mach-lookup (global-name "com.apple.logd") (global-name "com.apple.logd.events") (global-name "com.apple.system.logger"))
(deny network-outbound (literal "/private/var/run/syslog"))
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
    assert not unknown_lines(text)
    assert text.index("(deny process-exec*") > text.rindex("(allow process-exec*")
    assert text.index("(deny mach-lookup") > text.rindex("(allow file-read*")
    assert text.index("(deny file-read*") > text.rindex("(allow file-read*")


PATH = r'"(?:[^"\\]|\\.)*"'
NAME = r'"[A-Za-z0-9._-]+"'
LINE_FORMS = [
    re.compile(pattern)
    for pattern in (
        r"\(version 1\)",
        r"\(deny default\)",
        r'\(import "system\.sb"\)',
        rf"\(allow process-exec\* \(subpath {PATH}\)\)",
        r"\(allow process-fork\)",
        r"\(allow process-info\* \(target self\)\)",
        r"\(allow signal \(target (?:self|children)\)\)",
        r"\(allow (?:sysctl-read|file-ioctl)\)",
        rf"\(allow file-read\* \(subpath {PATH}\)\)",
        r'\(allow file-read-metadata \(subpath "/bin"\) \(subpath "/sbin"\)\)',
        rf"\(allow file-read\* file-write\* \(subpath {PATH}\)\)",
        r'\(allow network-outbound \(remote ip "localhost:[0-9]{1,5}"\)\)',
        rf"\(deny (?:process-exec\*|file-map-executable|file-read\*) \(subpath {PATH}\)\)",
        rf'\(deny mach-lookup(?:(?: \(global-name {NAME}\))+(?: \(xpc-service-name-prefix ""\))?| \(xpc-service-name-prefix ""\))\)',
        rf"\(deny network-outbound \(literal {PATH}\)\)",
    )
]


def unknown_lines(text: str) -> list[str]:
    text = re.sub(r"\(\s+", "(", text)
    return [
        line
        for line in text.splitlines()
        if line.strip() and not any(form.fullmatch(line) for form in LINE_FORMS)
    ]


@pytest.mark.parametrize(
    "text",
    [
        "(allow mach-lookup)",
        "(allow mach*)\n",
        "(allow process-fork mach-lookup)",
        "(allow process-exec*)",
        "(allow process*)",
        "(allow default)",
        "(allow signal)",
        '(allow mach-lookup (xpc-service-name-prefix ""))',
        '(allow mach-lookup (global-name "a") (xpc-service-name-prefix ""))',
        '(allow mach-lookup (global-name "com.apple.example"))',
        '(allow mach-lookup (global-name-regex #"^com"))',
        '(allow file-read* (regex #"^/"))',
        '(allow file-read* (subpath "/"))\n(allow default)',
        '(allow file-read* (literal "/x"))',
        '(allow network-outbound (remote ip "*:*"))',
        '(allow network-outbound (remote tcp "*:443"))',
        '(allow file-read* (subpath "/a")) (allow default)',
        "(version 1)\n(allow  mach-lookup )\n",
        "( allow default)",
        "(version 1)\n(deny default)\n(\nallow default)",
        "(\tallow mach-lookup)",
        "(ALLOW default)",
        "(allow default) ; a comment",
        '(allow file-read*\n(subpath "/"))',
        '(allow file-read*  (subpath "/x"))',
        '(allow file-read* (subpath "/x") )',
        '(deny file-read* (subpath "/x")) (allow default)',
        "(deny mach-lookup)",
        '(import "bsd.sb")',
        "(version 2)",
        "(debug deny)",
        '(trace "/private/tmp/trace.sb")',
        "(define (opened) (allow default))",
        "(with-filter (process-attribute is-platform-binary) (allow default))",
        "allow default",
        ")",
    ],
)
def test_checker_refuses_every_line_outside_the_known_forms(text: str) -> None:
    assert unknown_lines(text)


@pytest.mark.parametrize(
    "text",
    [
        MINIMAL_PROFILE,
        '(deny mach-lookup (global-name "com.apple.dnssd.service"))',
        "(allow signal (target self))",
        '(allow process-exec* (subpath "/usr"))',
        '(deny mach-lookup (xpc-service-name-prefix ""))',
        '(allow file-read* file-write* (subpath "/a \\"q\\" b"))',
        '(allow network-outbound (remote ip "localhost:3128"))',
        '(deny network-outbound (literal "/private/var/run/syslog"))',
        "( version 1)\n\n(deny default)",
    ],
)
def test_checker_passes_known_forms(text: str) -> None:
    assert not unknown_lines(text)


def test_dnssd_lookup_is_denied_and_signals_are_scoped() -> None:
    for case in PROFILES.values():
        text = case["seatbelt"]
        assert '(deny mach-lookup (global-name "com.apple.dnssd.service"))\n' in text
        assert (
            "(allow signal (target self))\n(allow signal (target children))\n" in text
        )
        assert "(allow mach-lookup" not in text
        assert (
            f'(deny file-map-executable (subpath "{case["policy"]["workspace"].replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"))'
            in text
        )


def test_exec_scope_is_base_reads_and_bin_sbin_only() -> None:
    case = PROFILES["full"]
    lines = [
        line
        for line in case["seatbelt"].splitlines()
        if line.startswith("(allow process-exec*")
    ]
    base = DOC["constants"]["darwin_exec_base"]
    assert base == ["/usr", "/System", "/Library", "/private/var/db/dyld"]
    assert "/dev" not in base and "/private/etc" not in base
    roots = base + case["policy"]["reads"] + DOC["constants"]["darwin_exec_only"]
    assert lines == [f'(allow process-exec* (subpath "{root}"))' for root in roots]
    assert DOC["constants"]["darwin_exec_only"] == ["/bin", "/sbin"]
    reads = [
        line
        for line in case["seatbelt"].splitlines()
        if line.startswith("(allow file-read*")
    ]
    assert not any('"/bin"' in line or '"/sbin"' in line for line in reads)


def test_workspace_is_never_executable_on_either_backend() -> None:
    for case in PROFILES.values():
        assert case["linux"]["workspace_noexec"] is True
        assert "inet_stream_sockets" not in case["linux"]["seccomp"]
        assert case["linux"]["seccomp"]["allow_socketpair"] is True
        grants = case["linux"]["landlock"]["workspace_rights"]
        assert grants == "all_except_execute_and_make_sock"


@pytest.mark.parametrize("name", [c["name"] for c in DOC["context_errors"]])
def test_context_errors_are_refused(name: str) -> None:
    case = next(c for c in DOC["context_errors"] if c["name"] == name)
    with pytest.raises(exec_policy.ContextError):
        exec_policy.select(
            PROFILES["minimal"]["policy"], case["facts"], case["data_dir"]
        )


def test_context_error_vectors_cover_unknown_keys_and_probe_errors() -> None:
    names = {c["name"] for c in DOC["context_errors"]}
    assert {"facts_unknown_key", "facts_probe_error_null", "facts_missing_key"} <= names
    assert {"facts_euid_null", "facts_euid_negative"} <= names


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
    assert "(allow network" not in quiet["seatbelt"]
    assert quiet["linux"]["egress"] is None
    assert quiet["linux"]["landlock"]["tcp_connect"] == []
    assert quiet["linux"]["seccomp"]["socket_allow"] == []
    assert quiet["linux"]["seccomp"]["allow_socketpair"] is True
    port = hosted["context"]["proxy_port"]
    lines = [
        line for line in hosted["seatbelt"].splitlines() if "(allow network" in line
    ]
    assert lines == [f'(allow network-outbound (remote ip "localhost:{port}"))']
    assert hosted["linux"]["landlock"]["tcp_connect"] == [3128]
    assert hosted["linux"]["egress"] == {
        "listen": "127.0.0.1:3128",
        "socket": "/run/astral/egress.sock",
    }
    assert hosted["linux"]["seccomp"]["socket_allow"] == [
        {"domain": "AF_INET", "type": "SOCK_STREAM"},
        {"domain": "AF_INET6", "type": "SOCK_STREAM"},
    ]
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
    assert plan["rlimits"]["RLIMIT_CORE"] == exec_policy.CORE_LIMIT_BYTES == 0
    assert (
        plan["rlimits"]["RLIMIT_AS"]
        == PROFILES["full"]["policy"]["limits"]["memory_bytes"]
    )
    assert "shmget" in plan["seccomp"]["deny_syscalls"]
    assert "setsid" in plan["seccomp"]["deny_syscalls"]
    for call in ("io_uring_setup", "io_uring_enter", "io_uring_register"):
        assert call in plan["seccomp"]["deny_syscalls"]
        assert call in PROFILES["minimal"]["linux"]["seccomp"]["deny_syscalls"]


@pytest.mark.parametrize("name", sorted(EXPECTED_SELECTION))
def test_selection_case(name: str) -> None:
    case = SELECTION[name]
    code, reason = EXPECTED_SELECTION[name]
    policy = case["policy"]
    if code.startswith("policy."):
        with pytest.raises(exec_policy.PolicyError) as caught:
            exec_policy.select(policy, case["facts"], case["data_dir"])
        assert caught.value.code == code
        assert case["result"] == {"policy_error": code}
        return
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
    assert constants["core_limit_bytes"] == exec_policy.CORE_LIMIT_BYTES == 0
    assert constants["landlock_abi"] == 6


def test_generator_output_matches_committed_vectors() -> None:
    import generate_exec_vectors

    assert generate_exec_vectors.render() == VECTORS.read_text(encoding="utf-8")


def test_denied_services_and_syslog_are_in_every_profile() -> None:
    constants = DOC["constants"]
    for case in PROFILES.values():
        text = case["seatbelt"]
        for name in constants["denied_services"] + constants["denied_log_services"]:
            assert f'(global-name "{name}")' in text
        assert '(xpc-service-name-prefix "")' in text
        assert text.count("(deny mach-lookup") == 3
        assert (
            f'(deny network-outbound (literal "{constants["syslog_socket"]}"))' in text
        )
        assert '(allow file-read-metadata (subpath "/bin") (subpath "/sbin"))\n' in text


def test_bin_and_sbin_are_metadata_only_on_macos() -> None:
    for case in PROFILES.values():
        for line in case["seatbelt"].splitlines():
            if line.startswith("(allow file-read*"):
                assert '"/bin"' not in line and '"/sbin"' not in line


def test_lone_surrogate_path_is_refused() -> None:
    policy = copy.deepcopy(PROFILES["minimal"]["policy"])
    policy["reads"] = ["/opt/\ud800"]
    for platform in (None, "darwin", "linux"):
        with pytest.raises(exec_policy.PolicyError) as caught:
            exec_policy.parse(policy, platform)
        assert caught.value.code == "policy.path_invalid"


def test_platform_is_a_required_argument() -> None:
    with pytest.raises(TypeError):
        exec_policy.parse(PROFILES["minimal"]["policy"])  # type: ignore[call-arg]


@pytest.mark.parametrize("name", [c["name"] for c in DOC["profile_errors"]])
def test_renderers_pass_their_platform_to_parse(name: str) -> None:
    case = next(c for c in DOC["profile_errors"] if c["name"] == name)
    ctx = case["context"]
    with pytest.raises(exec_policy.PolicyError) as caught:
        exec_policy.seatbelt_profile(case["policy"], ctx["home"], ctx["proxy_port"])
    assert caught.value.code == case["code"] == "policy.path_invalid"
    assert exec_policy.linux_plan(case["policy"], None) == case["linux"]


def test_path_fold_table_matches_its_stated_pairs() -> None:
    fold = exec_policy._fold
    assert DOC["constants"]["path_fold_unicode"] == "16.0.0"
    assert fold("/A", "darwin") == fold("/a", "darwin") == "/a"
    assert fold("/STRASSE", "darwin") == fold("/stra\u00dfe", "darwin")
    assert fold("/\ufb01", "darwin") == "/fi"
    assert fold("/\ua7dc", "darwin") == fold("/\u019b", "darwin")
    assert fold("/caf\u00e9", "darwin") == fold("/cafe\u0301", "darwin")
    assert fold("/e\u0327\u0301", "darwin") == fold("/e\u0301\u0327", "darwin")
    assert fold("/\uac01", "darwin") == "/\u1100\u1161\u11a8"
    assert fold("/A", "linux") == "/A"
    assert not exec_policy._assigned("\u0378")
    assert exec_policy._assigned("\ua7dc")


def test_fold_table_generator_refuses_other_unicode_versions() -> None:
    import generate_path_fold

    if exec_policy._TABLE["unicode"] != "16.0.0":
        pytest.fail("the committed table is not Unicode 16.0")
    assert generate_path_fold.UNICODE_VERSION == "16.0.0"


def test_renderers_refuse_bad_context_values() -> None:
    policy = PROFILES["full"]["policy"]
    exec_policy.seatbelt_profile(policy, "/Users/test", 65535)
    exec_policy.seatbelt_profile(policy, "/Users/test", 1)
    for port in (0, 65536, True, None):
        with pytest.raises(exec_policy.ContextError):
            exec_policy.seatbelt_profile(policy, "/Users/test", port)
    with pytest.raises(exec_policy.ContextError):
        exec_policy.seatbelt_profile(policy, "/Users/te\u0378st", 3128)
    exec_policy.linux_plan(policy, "/run/user/1000/astral/call-1.sock")


def test_validation_cases_always_name_their_platform() -> None:
    assert {case["platform"] for case in DOC["validation"]} == {"linux", "darwin"}
    for name, case in VALIDATION.items():
        assert case["platform"] == ("darwin" if name.endswith("_darwin") else "linux")


def test_fold_orders_marks_before_and_after_the_table() -> None:
    fold = exec_policy._fold
    assert fold("/e\u0301\u0345", "darwin") == fold("/e\u0345\u0301", "darwin")
    assert fold("/e\u0345\u0301", "darwin") == "/e\u0301\u03b9"


def test_fold_decomposes_before_ordering_and_folding() -> None:
    fold = exec_policy._fold
    assert fold("/\u00e9\u0323", "darwin") == fold("/e\u0323\u0301", "darwin")
    assert fold("/\u00e9\u0323", "darwin") == "/e\u0323\u0301"
    assert fold("/\u1fb3\u0301", "darwin") == "/\u03b1\u0301\u03b9"
    assert fold("/\u1f80\u0300", "darwin") == "/\u03b1\u0313\u0300\u03b9"
    assert fold("/\u1f88", "darwin") == fold("/\u1f80", "darwin")
    assert fold("/\u00c9", "darwin") == "/e\u0301"
    assert fold("/\ud7a3", "darwin") == "/\u1112\u1175\u11c2"
    assert fold("/\u0130", "darwin") == "/i\u0307"


def test_fold_tables_hold_no_runtime_unicode_data() -> None:
    source = Path(exec_policy.__file__).read_text(encoding="utf-8")
    assert "unicodedata" not in source
    table = exec_policy._TABLE
    assert set(table) == {"unicode", "assigned", "ccc", "decompose", "fold"}
    assert not set(table["decompose"]) & set(table["fold"])
    assert table["decompose"]["1F80"] == "\u03b1\u0313\u0345"
    assert "1F80" not in table["fold"] and table["fold"]["0345"] == "\u03b9"
