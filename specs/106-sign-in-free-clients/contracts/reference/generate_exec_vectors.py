"""Generates the execution policy vectors from fixed public values and checks each stated outcome against the reference module.
`--check` fails when the committed file differs from a fresh render.
"""

import argparse
import copy
import importlib
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
exec_policy = importlib.import_module("exec_policy")

OUTPUT = HERE.parent / "vectors" / "exec-policy-v1.json"
HOME = "/Users/test"
WORKSPACE = "/private/tmp/astral/ws"
DATA_DIR_DARWIN = "/Users/test/Library/Application Support/Astral/node"
DATA_DIR_LINUX = "/var/lib/astral/node"
EGRESS = "/run/user/1000/astral/call-1.sock"

MINIMAL: dict[str, Any] = {
    "v": 1,
    "reads": [],
    "workspace": WORKSPACE,
    "hosts": [],
    "limits": {
        "memory_bytes": 536870912,
        "processes": 1,
        "open_files": 256,
        "file_bytes": 104857600,
    },
    "time": {"wall_ms": 30000},
}


def variant(**changes: Any) -> dict[str, Any]:
    policy = copy.deepcopy(MINIMAL)
    for key, value in changes.items():
        if key in policy["limits"]:
            policy["limits"][key] = value
        else:
            policy[key] = value
    return policy


def hosts(*pairs: tuple[str, int]) -> list[dict[str, Any]]:
    return [{"host": host, "port": port} for host, port in pairs]


FULL = variant(
    reads=["/Users/test/.local/share/python/3.11", "/opt/astral/node-pack"],
    hosts=hosts(
        ("api.example.com", 80), ("api.example.com", 443), ("files.example.org", 443)
    ),
    processes=8,
)
READS_64 = [f"/opt/r/r{index:02d}" for index in range(64)]
HOSTS_64 = hosts(*[(f"h{index:02d}.example.com", 443) for index in range(64)])


def with_field(policy: dict[str, Any], path: list[Any], value: Any) -> dict[str, Any]:
    out = copy.deepcopy(policy)
    target = out
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return out


def without(policy: dict[str, Any], *path: Any) -> dict[str, Any]:
    out = copy.deepcopy(policy)
    target = out
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    return out


def host_of_length(length: int) -> str:
    labels = ["a" * 63, "b" * 63, "c" * 63, "d" * (length - 192)]
    host = ".".join(labels)
    assert len(host) == length
    return host


def validation_cases() -> list[dict[str, Any]]:
    cases: list[tuple[str, str, Any, bool, str]] = []

    def policy(name: str, code: str, value: Any, platform: str = "linux") -> None:
        cases.append((name, code, value, False, platform))

    def raw(name: str, code: str, text: str) -> None:
        cases.append((name, code, text, True, "linux"))

    ok, malformed = "ok", "policy.malformed"
    unknown, bad_path = "policy.unknown_field", "policy.path_invalid"
    many, bad_host = "policy.too_many_entries", "policy.host_invalid"
    bad_limit, bad_time = "policy.limit_out_of_range", "policy.time_out_of_range"
    policy("minimal_valid", ok, MINIMAL)
    policy("full_valid", ok, FULL)
    policy("reads_64_accepted", ok, variant(reads=READS_64))
    policy("reads_65", many, variant(reads=READS_64 + ["/opt/r/r64"]))
    policy("hosts_64_accepted", ok, variant(hosts=HOSTS_64))
    policy("hosts_65", many, variant(hosts=HOSTS_64 + hosts(("h64.example.com", 443))))
    policy(
        "hosts_punycode_and_port_80",
        ok,
        variant(hosts=hosts(("xn--bcher-kva.example", 80))),
    )
    policy(
        "limits_at_minimum",
        ok,
        with_field(
            with_field(
                with_field(
                    variant(memory_bytes=16777216), ["limits", "open_files"], 16
                ),
                ["limits", "file_bytes"],
                4096,
            ),
            ["limits", "processes"],
            1,
        ),
    )
    policy(
        "limits_at_maximum",
        ok,
        with_field(
            with_field(
                with_field(
                    variant(memory_bytes=68719476736), ["limits", "open_files"], 65536
                ),
                ["limits", "file_bytes"],
                1099511627776,
            ),
            ["limits", "processes"],
            4096,
        ),
    )
    policy("wall_ms_at_minimum", ok, with_field(MINIMAL, ["time", "wall_ms"], 1))
    policy("wall_ms_at_maximum", ok, with_field(MINIMAL, ["time", "wall_ms"], 86400000))
    raw("not_json", malformed, "{")
    raw(
        "duplicate_key",
        malformed,
        '{"v":1,"v":1,"reads":[],"workspace":"/w/x","hosts":[],'
        '"limits":{"memory_bytes":536870912,"processes":1,"open_files":256,'
        '"file_bytes":104857600},"time":{"wall_ms":30000}}',
    )
    raw(
        "float_number",
        malformed,
        json.dumps(MINIMAL, sort_keys=True).replace('"v": 1', '"v": 1.0'),
    )
    policy("array_root", malformed, [MINIMAL])
    policy("version_missing", malformed, without(MINIMAL, "v"))
    policy("version_two", malformed, with_field(MINIMAL, ["v"], 2))
    policy("version_true", malformed, with_field(MINIMAL, ["v"], True))
    policy("reads_missing", malformed, without(MINIMAL, "reads"))
    policy("reads_not_list", malformed, with_field(MINIMAL, ["reads"], "/usr"))
    policy("read_not_string", malformed, with_field(MINIMAL, ["reads"], [7]))
    policy("workspace_not_string", malformed, with_field(MINIMAL, ["workspace"], None))
    policy("hosts_not_list", malformed, with_field(MINIMAL, ["hosts"], {}))
    policy("host_entry_not_object", malformed, variant(hosts=["api.example.com"]))
    policy(
        "host_missing_port",
        malformed,
        variant(hosts=[{"host": "api.example.com"}]),
    )
    policy(
        "host_port_not_integer",
        malformed,
        variant(hosts=[{"host": "api.example.com", "port": "443"}]),
    )
    policy("limits_not_object", malformed, with_field(MINIMAL, ["limits"], []))
    policy("limits_missing_key", malformed, without(MINIMAL, "limits", "open_files"))
    policy(
        "limit_not_integer",
        malformed,
        with_field(MINIMAL, ["limits", "processes"], "1"),
    )
    policy("limit_bool", malformed, with_field(MINIMAL, ["limits", "processes"], True))
    policy("time_not_object", malformed, with_field(MINIMAL, ["time"], 30000))
    policy("time_missing_wall_ms", malformed, with_field(MINIMAL, ["time"], {}))
    policy("unknown_top_level_key", unknown, with_field(MINIMAL, ["extra"], 1))
    policy("unknown_listen_field", unknown, with_field(MINIMAL, ["listen"], []))
    policy(
        "unknown_host_key",
        unknown,
        variant(hosts=[{"host": "api.example.com", "port": 443, "path": "/"}]),
    )
    policy("unknown_limits_key", unknown, with_field(MINIMAL, ["limits", "cpu"], 1))
    policy("unknown_time_key", unknown, with_field(MINIMAL, ["time", "cpu_ms"], 1))
    policy(
        "unknown_beats_missing",
        unknown,
        with_field(without(MINIMAL, "reads"), ["extra"], 1),
    )
    for name, value in (
        ("read_relative", "usr/lib"),
        ("read_root", "/"),
        ("read_dotdot", "/opt/../etc"),
        ("read_dot_component", "/opt/./x"),
        ("read_trailing_slash", "/opt/x/"),
        ("read_double_slash", "/opt//x"),
        ("read_nul", "/opt/a\u0000b"),
        ("read_newline", "/opt/a\nb"),
        ("read_too_long", "/" + "a" * 4096),
    ):
        policy(name, bad_path, variant(reads=[value]))
    policy("reads_unsorted", bad_path, variant(reads=["/opt/b", "/opt/a"]))
    policy("reads_duplicate", bad_path, variant(reads=["/opt/a", "/opt/a"]))
    policy("workspace_relative", bad_path, variant(workspace="tmp/ws"))
    policy("workspace_root", bad_path, variant(workspace="/"))
    policy("read_inside_workspace", bad_path, variant(reads=[WORKSPACE + "/sub"]))
    policy("read_contains_workspace", bad_path, variant(reads=["/private/tmp"]))
    policy("read_equals_workspace", bad_path, variant(reads=[WORKSPACE]))
    policy(
        "too_many_beats_bad_entry",
        many,
        variant(reads=["relative"] + READS_64),
    )
    for name, host in (
        ("host_uppercase", "API.example.com"),
        ("host_no_dot", "localhost"),
        ("host_ipv4_literal", "192.168.0.1"),
        ("host_ipv6_literal", "2001:db8::1"),
        ("host_wildcard", "*.example.com"),
        ("host_trailing_dot", "example.com."),
        ("host_underscore", "my_host.example.com"),
        ("host_label_too_long", "a" * 64 + ".example.com"),
        ("host_numeric_tld", "example.123"),
        ("host_empty_label", "a..example.com"),
    ):
        policy(name, bad_host, variant(hosts=hosts((host, 443))))
    policy("host_port_8443", bad_host, variant(hosts=hosts(("api.example.com", 8443))))
    policy("host_port_zero", bad_host, variant(hosts=hosts(("api.example.com", 0))))
    policy(
        "hosts_unsorted",
        bad_host,
        variant(hosts=hosts(("b.example.com", 443), ("a.example.com", 443))),
    )
    policy(
        "hosts_duplicate",
        bad_host,
        variant(hosts=hosts(("a.example.com", 443), ("a.example.com", 443))),
    )
    for name, key, value in (
        ("memory_below_minimum", "memory_bytes", 16777215),
        ("memory_above_maximum", "memory_bytes", 68719476737),
        ("processes_zero", "processes", 0),
        ("processes_above_maximum", "processes", 4097),
        ("open_files_below_minimum", "open_files", 15),
        ("open_files_above_maximum", "open_files", 65537),
        ("file_bytes_below_minimum", "file_bytes", 4095),
        ("file_bytes_above_maximum", "file_bytes", 1099511627777),
    ):
        policy(name, bad_limit, variant(**{key: value}))
    policy("wall_ms_zero", bad_time, with_field(MINIMAL, ["time", "wall_ms"], 0))
    policy(
        "wall_ms_above_maximum",
        bad_time,
        with_field(MINIMAL, ["time", "wall_ms"], 86400001),
    )
    policy("wall_ms_negative", bad_time, with_field(MINIMAL, ["time", "wall_ms"], -1))
    policy(
        "path_beats_limit",
        bad_path,
        with_field(variant(reads=["relative"]), ["limits", "processes"], 0),
    )
    policy(
        "limit_beats_time",
        bad_limit,
        with_field(variant(processes=0), ["time", "wall_ms"], 0),
    )
    nfc, nfd = "/opt/caf\u00e9", "/opt/cafe\u0301"
    policy("read_del_character", bad_path, variant(reads=["/opt/a\u007fb"]))
    policy("read_4096_bytes_accepted", ok, variant(reads=["/" + "a" * 4095]))
    policy("workspace_dotdot", bad_path, variant(workspace="/tmp/../x"))
    policy("workspace_trailing_slash", bad_path, variant(workspace="/tmp/ws/"))
    policy("reads_nfc_nfd_pair_linux", ok, variant(reads=[nfd, nfc]))
    policy("reads_nfc_nfd_pair_darwin", bad_path, variant(reads=[nfd, nfc]), "darwin")
    policy("reads_case_pair_linux", ok, variant(reads=["/opt/A", "/opt/a"]))
    policy(
        "reads_case_pair_darwin",
        bad_path,
        variant(reads=["/opt/A", "/opt/a"]),
        "darwin",
    )
    policy("read_case_variant_of_workspace_linux", ok, variant(reads=["/Private/tmp"]))
    policy(
        "read_case_variant_of_workspace_darwin",
        bad_path,
        variant(reads=["/Private/tmp"]),
        "darwin",
    )
    policy(
        "host_over_253_characters",
        bad_host,
        variant(hosts=hosts((".".join(["a" * 60] * 5 + ["com"]), 443))),
    )
    policy(
        "host_leading_hyphen", bad_host, variant(hosts=hosts(("-a.example.com", 443)))
    )
    policy(
        "host_trailing_hyphen", bad_host, variant(hosts=hosts(("a-.example.com", 443)))
    )
    policy(
        "host_ipv4_with_hex_octet", bad_host, variant(hosts=hosts(("1.2.3.0x4", 443)))
    )
    policy("host_hex_ipv4", bad_host, variant(hosts=hosts(("0x7f.0.0.0x1", 443))))
    policy("host_bare_0x_label", bad_host, variant(hosts=hosts(("example.0x", 443))))
    policy(
        "host_digits_then_letters_ok",
        ok,
        variant(hosts=hosts(("1.2.3.4a.example", 443))),
    )
    policy("host_not_string", malformed, variant(hosts=[{"host": 5, "port": 443}]))
    policy(
        "host_port_true",
        malformed,
        variant(hosts=[{"host": "api.example.com", "port": True}]),
    )
    policy("wall_ms_true", malformed, with_field(MINIMAL, ["time", "wall_ms"], True))
    policy(
        "wall_ms_string", malformed, with_field(MINIMAL, ["time", "wall_ms"], "30000")
    )
    policy(
        "bad_workspace_beats_bad_host",
        bad_path,
        variant(workspace="relative", hosts=hosts(("NOPE", 443))),
    )
    policy(
        "bad_host_beats_unknown_time_key",
        bad_host,
        with_field(variant(hosts=hosts(("NOPE", 443))), ["time", "cpu_ms"], 1),
    )
    policy(
        "overlap_beats_bad_host",
        bad_path,
        variant(reads=[WORKSPACE], hosts=hosts(("NOPE", 443))),
    )
    policy(
        "bad_limit_beats_unknown_time_key",
        bad_limit,
        with_field(variant(processes=0), ["time", "cpu_ms"], 1),
    )
    base_text = json.dumps(MINIMAL, sort_keys=True)
    raw("float_in_extra_key", malformed, base_text[:-1] + ', "extra": 1.5}')
    raw(
        "two_to_the_64_in_extra_key",
        malformed,
        base_text[:-1] + ', "extra": 18446744073709551616}',
    )
    raw(
        "float_in_limit",
        malformed,
        base_text.replace('"processes": 1', '"processes": 1.0'),
    )
    raw(
        "two_to_the_64_in_limit",
        malformed,
        base_text.replace(
            '"memory_bytes": 536870912', '"memory_bytes": 18446744073709551616'
        ),
    )
    multibyte_over = "/" + "\u00e9" * 2048
    multibyte_exact = "/" + "\u00e9" * 2047 + "a"
    policy("read_multibyte_over_4096_bytes", bad_path, variant(reads=[multibyte_over]))
    policy("read_multibyte_4096_bytes_accepted", ok, variant(reads=[multibyte_exact]))
    policy(
        "reads_sharp_s_pair_darwin",
        bad_path,
        variant(reads=["/opt/STRASSE", "/opt/stra\u00dfe"]),
        "darwin",
    )
    policy(
        "reads_sharp_s_pair_linux",
        ok,
        variant(reads=["/opt/STRASSE", "/opt/stra\u00dfe"]),
    )
    policy(
        "reads_ligature_pair_darwin",
        bad_path,
        variant(reads=["/opt/file", "/opt/\ufb01le"]),
        "darwin",
    )
    policy(
        "reads_ligature_pair_linux",
        ok,
        variant(reads=["/opt/file", "/opt/\ufb01le"]),
    )
    policy(
        "read_unassigned_darwin", bad_path, variant(reads=["/opt/a\u0378"]), "darwin"
    )
    policy("read_unassigned_linux", ok, variant(reads=["/opt/a\u0378"]))
    policy(
        "workspace_unassigned_darwin",
        bad_path,
        variant(workspace="/tmp/\u0378"),
        "darwin",
    )
    for name, pair, code in (
        ("reads_hangul_pair_darwin", ["/opt/\u1100\u1161", "/opt/\uac00"], bad_path),
        (
            "reads_hangul_tail_pair_darwin",
            ["/opt/\u1100\u1161\u11a8", "/opt/\uac01"],
            bad_path,
        ),
        (
            "reads_mark_order_pair_darwin",
            ["/opt/e\u0301\u0327", "/opt/e\u0327\u0301"],
            bad_path,
        ),
        (
            "reads_mark_order_pair_before_base_darwin",
            ["/opt/e\u0301\u0327x", "/opt/e\u0327\u0301x"],
            bad_path,
        ),
        (
            "reads_same_class_marks_distinct_darwin",
            ["/opt/a\u0301\u0302", "/opt/a\u0302\u0301"],
            ok,
        ),
    ):
        policy(name, code, variant(reads=pair), "darwin")
    policy(
        "reads_ypogegrammeni_pair_darwin",
        bad_path,
        variant(reads=["/opt/e\u0301\u0345", "/opt/e\u0345\u0301"]),
        "darwin",
    )
    policy(
        "reads_ypogegrammeni_pair_linux",
        ok,
        variant(reads=["/opt/e\u0301\u0345", "/opt/e\u0345\u0301"]),
    )
    policy(
        "read_assigned_range_start_darwin", ok, variant(reads=["/opt/\u037a"]), "darwin"
    )
    policy(
        "read_assigned_range_end_darwin", ok, variant(reads=["/opt/\u0377"]), "darwin"
    )
    policy(
        "read_unassigned_after_range_darwin",
        bad_path,
        variant(reads=["/opt/\u0379"]),
        "darwin",
    )
    policy(
        "host_253_characters_accepted",
        ok,
        variant(hosts=hosts((host_of_length(253), 443))),
    )
    policy(
        "host_254_characters",
        bad_host,
        variant(hosts=hosts((host_of_length(254), 443))),
    )
    policy(
        "host_digit_then_letter_label_ok", ok, variant(hosts=hosts(("example.1a", 443)))
    )
    policy("host_bad_hex_label_ok", ok, variant(hosts=hosts(("example.0xg", 443))))
    for name, pair in (
        (
            "reads_precomposed_acute_dot_below_pair",
            ["/opt/e\u0323\u0301", "/opt/\u00e9\u0323"],
        ),
        ("reads_last_hangul_syllable_pair", ["/opt/\u1112\u1175\u11c2", "/opt/\ud7a3"]),
        ("reads_precomposed_capital_pair", ["/opt/\u00c9", "/opt/\u00e9"]),
        (
            "reads_precomposed_ypogegrammeni_acute_pair",
            ["/opt/\u03b1\u0301\u0345", "/opt/\u1fb3\u0301"],
        ),
        (
            "reads_precomposed_psili_ypogegrammeni_grave_pair",
            ["/opt/\u03b1\u0313\u0300\u0345", "/opt/\u1f80\u0300"],
        ),
    ):
        policy(name + "_darwin", bad_path, variant(reads=pair), "darwin")
        policy(name + "_linux", ok, variant(reads=pair))
    policy("read_name_prefix_of_workspace_ok", ok, variant(reads=[WORKSPACE[:-1]]))
    policy(
        "read_name_prefix_of_workspace_darwin",
        ok,
        variant(reads=["/Private/tmp/astral/W"]),
        "darwin",
    )
    policy("read_unit_separator", bad_path, variant(reads=["/opt/a\u001fb"]))
    policy(
        "read_case_variant_inside_workspace_darwin",
        bad_path,
        variant(reads=["/private/tmp/astral/WS/lib"]),
        "darwin",
    )
    policy(
        "read_case_variant_inside_workspace_linux",
        ok,
        variant(reads=["/private/tmp/astral/WS/lib"]),
    )
    out = []
    for name, code, value, is_raw, platform in cases:
        entry: dict[str, Any] = {"name": name, "code": code}
        entry["raw" if is_raw else "policy"] = value
        entry["platform"] = platform
        try:
            parsed = exec_policy.parse(value, platform)
            got = "ok"
            entry["digest"] = exec_policy.digest(parsed)
        except exec_policy.PolicyError as error:
            got = error.code
        if got != code:
            raise SystemExit(f"{name}: expected {code}, reference gave {got}")
        out.append(entry)
    return out


def profile_cases() -> list[dict[str, Any]]:
    quoting = variant(
        workspace='/private/tmp/astral/ws "q" \\ é',
        reads=['/opt/a"b', "/opt/café dir"],
    )
    named = [
        ("minimal", MINIMAL, None, None),
        ("processes_two", variant(processes=2), None, None),
        (
            "network_one_process",
            variant(hosts=hosts(("api.example.com", 443))),
            49152,
            EGRESS,
        ),
        ("full", FULL, 49152, EGRESS),
        ("quoting", quoting, None, None),
    ]
    out = []
    for name, policy, port, socket in named:
        out.append(
            {
                "name": name,
                "policy": policy,
                "context": {"home": HOME, "proxy_port": port, "egress_socket": socket},
                "digest": exec_policy.digest(policy),
                "seatbelt": exec_policy.seatbelt_profile(policy, HOME, port),
                "linux": exec_policy.linux_plan(policy, socket),
            }
        )
    return out


def facts(platform: str, **changes: Any) -> dict[str, Any]:
    base = {
        "platform": platform,
        "landlock_abi": 6 if platform == "linux" else 0,
        "bwrap": platform == "linux",
        "userns": platform == "linux",
        "sandbox_exec": platform == "darwin",
        "app_sandboxed": False,
    }
    base.update(changes)
    return base


def selected(
    policy: dict[str, Any], platform_facts: dict[str, Any], data_dir: str
) -> dict[str, Any]:
    try:
        return exec_policy.select(policy, platform_facts, data_dir)
    except exec_policy.PolicyError as error:
        return {"policy_error": error.code}


def selection_cases() -> list[dict[str, Any]]:
    darwin, linux = facts("darwin"), facts("linux")
    rows = [
        ("darwin_ok", MINIMAL, darwin, DATA_DIR_DARWIN),
        ("linux_ok", MINIMAL, linux, DATA_DIR_LINUX),
        ("linux_abi_7_ok", MINIMAL, facts("linux", landlock_abi=7), DATA_DIR_LINUX),
        ("linux_abi_5", MINIMAL, facts("linux", landlock_abi=5), DATA_DIR_LINUX),
        ("linux_abi_0", MINIMAL, facts("linux", landlock_abi=0), DATA_DIR_LINUX),
        ("linux_no_bwrap", MINIMAL, facts("linux", bwrap=False), DATA_DIR_LINUX),
        ("linux_userns_denied", MINIMAL, facts("linux", userns=False), DATA_DIR_LINUX),
        (
            "linux_abi_beats_bwrap",
            MINIMAL,
            facts("linux", landlock_abi=5, bwrap=False, userns=False),
            DATA_DIR_LINUX,
        ),
        (
            "linux_bwrap_beats_userns",
            MINIMAL,
            facts("linux", bwrap=False, userns=False),
            DATA_DIR_LINUX,
        ),
        (
            "darwin_no_sandbox_exec",
            MINIMAL,
            facts("darwin", sandbox_exec=False),
            DATA_DIR_DARWIN,
        ),
        (
            "darwin_app_sandboxed",
            MINIMAL,
            facts("darwin", app_sandboxed=True),
            DATA_DIR_DARWIN,
        ),
        (
            "darwin_missing_beats_sandboxed",
            MINIMAL,
            facts("darwin", sandbox_exec=False, app_sandboxed=True),
            DATA_DIR_DARWIN,
        ),
        ("windows", MINIMAL, facts("win32"), "/var/astral/node"),
        (
            "shadow_refused_darwin",
            MINIMAL,
            facts("darwin", mode="shadow"),
            DATA_DIR_DARWIN,
        ),
        (
            "shadow_refused_linux",
            MINIMAL,
            facts("linux", mode="shadow"),
            DATA_DIR_LINUX,
        ),
        (
            "read_root_contains_data_dir",
            variant(reads=["/var/lib"]),
            linux,
            DATA_DIR_LINUX,
        ),
        (
            "read_root_equals_data_dir",
            variant(reads=[DATA_DIR_LINUX]),
            linux,
            DATA_DIR_LINUX,
        ),
        (
            "workspace_contains_data_dir",
            variant(workspace="/var/lib/astral"),
            linux,
            DATA_DIR_LINUX,
        ),
        (
            "base_root_contains_data_dir_darwin",
            MINIMAL,
            darwin,
            "/Library/Application Support/Astral/node",
        ),
        ("base_root_contains_data_dir_linux", MINIMAL, linux, "/etc/astral/node"),
        (
            "workspace_inside_data_dir_refused",
            variant(workspace=DATA_DIR_DARWIN + "/ws"),
            darwin,
            DATA_DIR_DARWIN,
        ),
        (
            "workspace_equals_data_dir_refused",
            variant(workspace=DATA_DIR_DARWIN),
            darwin,
            DATA_DIR_DARWIN,
        ),
        (
            "workspace_under_data_exec_ok",
            variant(workspace=DATA_DIR_DARWIN + "/exec/ws"),
            darwin,
            DATA_DIR_DARWIN,
        ),
        (
            "workspace_equals_data_exec_refused",
            variant(workspace=DATA_DIR_DARWIN + "/exec"),
            darwin,
            DATA_DIR_DARWIN,
        ),
        (
            "workspace_exec_lookalike_refused",
            variant(workspace=DATA_DIR_DARWIN + "/executable/ws"),
            darwin,
            DATA_DIR_DARWIN,
        ),
        (
            "workspace_under_data_exec_linux_ok",
            variant(workspace=DATA_DIR_LINUX + "/exec/ws"),
            linux,
            DATA_DIR_LINUX,
        ),
        (
            "read_root_inside_data_dir_refused",
            variant(reads=[DATA_DIR_LINUX + "/pack"]),
            linux,
            DATA_DIR_LINUX,
        ),
        (
            "case_variant_read_root_darwin",
            variant(reads=["/Users/test/Library"]),
            darwin,
            "/users/test/library/application support/astral/node",
        ),
        (
            "case_variant_read_root_linux_ok",
            variant(reads=["/Users/test/Library"]),
            linux,
            "/users/test/library/application support/astral/node",
        ),
        (
            "case_variant_workspace_under_exec_darwin_ok",
            variant(
                workspace="/USERS/TEST/LIBRARY/APPLICATION SUPPORT/ASTRAL/NODE/EXEC/ws"
            ),
            darwin,
            DATA_DIR_DARWIN,
        ),
        (
            "case_variant_workspace_outside_exec_darwin",
            variant(workspace="/USERS/TEST/LIBRARY/APPLICATION SUPPORT/ASTRAL/NODE/ws"),
            darwin,
            DATA_DIR_DARWIN,
        ),
        (
            "nfd_variant_read_root_darwin",
            variant(reads=["/Users/test/cafe\u0301"]),
            darwin,
            "/Users/test/caf\u00e9/node",
        ),
        (
            "nfd_variant_read_root_linux_ok",
            variant(reads=["/Users/test/cafe\u0301"]),
            linux,
            "/Users/test/caf\u00e9/node",
        ),
        ("sibling_of_data_dir_ok", MINIMAL, darwin, DATA_DIR_DARWIN),
        (
            "darwin_case_pair_in_reads",
            variant(reads=["/opt/A", "/opt/a"]),
            darwin,
            DATA_DIR_DARWIN,
        ),
        (
            "linux_case_pair_in_reads_ok",
            variant(reads=["/opt/A", "/opt/a"]),
            linux,
            DATA_DIR_LINUX,
        ),
        (
            "unicode_16_case_pair_read_root_darwin",
            variant(reads=["/Users/test/\ua7dc"]),
            darwin,
            "/Users/test/\u019b/node",
        ),
        (
            "unicode_16_case_pair_read_root_linux_ok",
            variant(reads=["/Users/test/\ua7dc"]),
            linux,
            "/Users/test/\u019b/node",
        ),
        (
            "ypogegrammeni_pair_read_root_darwin",
            variant(reads=["/Users/test/e\u0345\u0301"]),
            darwin,
            "/Users/test/e\u0301\u0345/node",
        ),
        (
            "ypogegrammeni_pair_read_root_linux_ok",
            variant(reads=["/Users/test/e\u0345\u0301"]),
            linux,
            "/Users/test/e\u0301\u0345/node",
        ),
        (
            "case_variant_read_root_inside_data_dir_darwin",
            variant(reads=[DATA_DIR_DARWIN.upper() + "/pack"]),
            darwin,
            DATA_DIR_DARWIN,
        ),
        (
            "case_variant_read_root_inside_data_dir_linux_ok",
            variant(reads=[DATA_DIR_DARWIN.upper() + "/pack"]),
            linux,
            DATA_DIR_DARWIN,
        ),
        (
            "data_dir_inside_case_variant_workspace_darwin",
            variant(workspace="/Users/test/WS"),
            darwin,
            "/Users/test/ws/node",
        ),
        (
            "data_dir_inside_case_variant_workspace_linux_ok",
            variant(workspace="/Users/test/WS"),
            linux,
            "/Users/test/ws/node",
        ),
        (
            "precomposed_ypogegrammeni_read_root_darwin",
            variant(reads=["/Users/test/\u1f80\u0300"]),
            darwin,
            "/Users/test/\u03b1\u0313\u0300\u0345/node",
        ),
        (
            "precomposed_ypogegrammeni_read_root_linux_ok",
            variant(reads=["/Users/test/\u1f80\u0300"]),
            linux,
            "/Users/test/\u03b1\u0313\u0300\u0345/node",
        ),
        (
            "darwin_unassigned_in_reads",
            variant(reads=["/opt/a\u0378"]),
            darwin,
            DATA_DIR_DARWIN,
        ),
        (
            "overlap_beats_shadow",
            variant(reads=["/var/lib"]),
            facts("linux", mode="shadow"),
            DATA_DIR_LINUX,
        ),
        (
            "overlap_beats_unavailable",
            variant(reads=["/var/lib"]),
            facts("win32"),
            DATA_DIR_LINUX,
        ),
        (
            "shadow_beats_unavailable",
            MINIMAL,
            facts("linux", landlock_abi=0, mode="shadow"),
            DATA_DIR_LINUX,
        ),
    ]
    return [
        {
            "name": name,
            "policy": policy,
            "facts": platform_facts,
            "data_dir": data_dir,
            "result": selected(policy, platform_facts, data_dir),
        }
        for name, policy, platform_facts, data_dir in rows
    ]


CONTEXT_ERRORS = [
    (
        "facts_platform_missing",
        {k: v for k, v in facts("linux").items() if k != "platform"},
        DATA_DIR_LINUX,
    ),
    ("facts_platform_null", {**facts("linux"), "platform": None}, DATA_DIR_LINUX),
    ("data_dir_unassigned_darwin", facts("darwin"), "/Users/test/\u0378"),
    ("facts_unknown_key", {**facts("linux"), "kernel": "7.0"}, DATA_DIR_LINUX),
    ("facts_missing_key", {"platform": "linux"}, DATA_DIR_LINUX),
    (
        "facts_probe_error_null",
        {**facts("linux"), "landlock_abi": None},
        DATA_DIR_LINUX,
    ),
    ("facts_probe_error_text", {**facts("linux"), "bwrap": "error"}, DATA_DIR_LINUX),
    ("facts_abi_bool", {**facts("linux"), "landlock_abi": True}, DATA_DIR_LINUX),
    ("facts_abi_negative", {**facts("linux"), "landlock_abi": -1}, DATA_DIR_LINUX),
    ("facts_bad_mode", {**facts("linux"), "mode": "audit"}, DATA_DIR_LINUX),
    ("facts_not_object", [], DATA_DIR_LINUX),
    ("data_dir_relative", facts("linux"), "var/astral"),
    ("data_dir_root", facts("linux"), "/"),
]


def context_error_cases() -> list[dict[str, Any]]:
    out = []
    for name, platform_facts, data_dir in CONTEXT_ERRORS:
        try:
            exec_policy.select(MINIMAL, platform_facts, data_dir)
        except exec_policy.ContextError:
            pass
        else:
            raise SystemExit(f"{name}: expected a context error")
        out.append({"name": name, "facts": platform_facts, "data_dir": data_dir})
    return out


def profile_error_cases() -> list[dict[str, Any]]:
    named = [
        ("darwin_case_pair_in_reads", variant(reads=["/opt/A", "/opt/a"])),
        (
            "darwin_nfc_nfd_pair_in_reads",
            variant(reads=["/opt/cafe\u0301", "/opt/caf\u00e9"]),
        ),
        ("darwin_unassigned_in_workspace", variant(workspace="/tmp/\u0378")),
        (
            "darwin_precomposed_ypogegrammeni_pair_in_reads",
            variant(reads=["/opt/\u03b1\u0313\u0300\u0345", "/opt/\u1f80\u0300"]),
        ),
    ]
    out = []
    for name, policy in named:
        try:
            exec_policy.seatbelt_profile(policy, HOME, None)
        except exec_policy.PolicyError as error:
            code = error.code
        else:
            raise SystemExit(f"{name}: expected a policy error")
        out.append(
            {
                "name": name,
                "policy": policy,
                "context": {"home": HOME, "proxy_port": None, "egress_socket": None},
                "code": code,
                "linux": exec_policy.linux_plan(policy, None),
            }
        )
    return out


def generate() -> dict[str, Any]:
    return {
        "constants": {
            "format": exec_policy.FORMAT,
            "darwin_base_reads": list(exec_policy.DARWIN_BASE),
            "darwin_keychain_denials": list(exec_policy.DARWIN_KEYCHAINS),
            "linux_base_reads": list(exec_policy.LINUX_BASE),
            "linux_device_files": list(exec_policy.LINUX_DEVICES),
            "limits": {k: list(v) for k, v in exec_policy.LIMITS.items()},
            "wall_ms": list(exec_policy.WALL_MS),
            "core_limit_bytes": exec_policy.CORE_LIMIT_BYTES,
            "max_reads": exec_policy.MAX_READS,
            "max_hosts": exec_policy.MAX_HOSTS,
            "max_path_bytes": exec_policy.MAX_PATH_BYTES,
            "host_ports": list(exec_policy.HOST_PORTS),
            "darwin_exec_base": list(exec_policy.DARWIN_EXEC_BASE),
            "darwin_exec_only": list(exec_policy.DARWIN_EXEC_ONLY),
            "darwin_metadata_only": list(exec_policy.DARWIN_METADATA_ONLY),
            "denied_services": list(exec_policy.DENIED_SERVICES),
            "denied_log_services": list(exec_policy.DENIED_LOG_SERVICES),
            "syslog_socket": exec_policy.SYSLOG_SOCKET,
            "path_fold_unicode": exec_policy._TABLE["unicode"],
            "dnssd_service": exec_policy.DNSSD_SERVICE,
            "exec_subdirectory": exec_policy.EXEC_SUBDIRECTORY,
            "fact_keys": list(exec_policy.FACT_KEYS),
            "landlock_abi": exec_policy.LANDLOCK_ABI,
            "proxy_port": exec_policy.PROXY_PORT,
            "egress_socket": exec_policy.EGRESS_SOCKET,
            "validation_codes": list(exec_policy.VALIDATION_CODES),
            "backend_reasons": list(exec_policy.BACKEND_REASONS),
            "messages": exec_policy.MESSAGES,
        },
        "validation": validation_cases(),
        "profiles": profile_cases(),
        "profile_errors": profile_error_cases(),
        "selection": selection_cases(),
        "context_errors": context_error_cases(),
    }


def render() -> str:
    return json.dumps(generate(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = render()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != content:
            raise SystemExit("exec-policy-v1 vectors differ")
    else:
        OUTPUT.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
