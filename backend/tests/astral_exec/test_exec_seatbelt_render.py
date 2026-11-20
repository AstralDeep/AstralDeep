"""Renders every committed profile vector through astral_exec.seatbelt byte for byte and refuses each profile error vector with its code.
It also pins the public render entry point to policies loaded for darwin, so the vector-only path stays private.
"""

from __future__ import annotations

import pytest
from astral_exec import ContextError, Policy, PolicyError, constants
from astral_exec.seatbelt import _render_document, quote, render
from exec_vectors import CONSTANTS, PROFILE_ERRORS, PROFILES, loaded, minimal_policy

DARWIN_DATA = "/Users/test/Library/Application Support/Astral/node"
DARWIN_HOME = "/Users/test"


def _context(case):
    return case["context"]["home"], case["context"]["proxy_port"]


@pytest.mark.parametrize("case", PROFILES, ids=[c["name"] for c in PROFILES])
def test_profile_vector_renders_byte_for_byte(case):
    home, port = _context(case)
    assert _render_document(case["policy"], home, port) == case["seatbelt"]


@pytest.mark.parametrize("case", PROFILES, ids=[c["name"] for c in PROFILES])
def test_public_render_of_a_darwin_loaded_policy_matches_the_vector(case):
    home, port = _context(case)
    policy = loaded(case["policy"], "darwin", DARWIN_DATA, home)
    assert render(policy, {"proxy_port": port}) == case["seatbelt"]
    assert render(policy, {"proxy_port": port, "home": home}) == case["seatbelt"]


@pytest.mark.parametrize(
    "case", PROFILE_ERRORS, ids=[c["name"] for c in PROFILE_ERRORS]
)
def test_profile_error_vector_is_refused_with_its_code(case):
    home, port = _context(case)
    with pytest.raises(PolicyError) as caught:
        _render_document(case["policy"], home, port)
    assert caught.value.code == case["code"]


def test_the_profile_constants_match_the_committed_vectors():
    assert constants.DARWIN_EXEC_BASE == tuple(CONSTANTS["darwin_exec_base"])
    assert constants.DARWIN_EXEC_ONLY == tuple(CONSTANTS["darwin_exec_only"])
    assert constants.DARWIN_METADATA_ONLY == tuple(CONSTANTS["darwin_metadata_only"])
    assert constants.DARWIN_KEYCHAINS == tuple(CONSTANTS["darwin_keychain_denials"])
    assert constants.DENIED_SERVICES == tuple(CONSTANTS["denied_services"])
    assert constants.DENIED_LOG_SERVICES == tuple(CONSTANTS["denied_log_services"])
    assert constants.DNSSD_SERVICE == CONSTANTS["dnssd_service"]
    assert constants.SYSLOG_SOCKET == CONSTANTS["syslog_socket"]


def test_quote_escapes_backslash_before_quote_and_nothing_else():
    assert quote('a\\b"c') == '"a\\\\b\\"c"'
    assert quote("café dir") == '"café dir"'


@pytest.mark.parametrize(
    "home",
    [None, "", "Users/test", "/Users/test/", "/Users/../x", "/Users/t\x01", "/x/͸"],
)
def test_a_bad_home_is_a_caller_error(home):
    with pytest.raises(ContextError):
        _render_document(minimal_policy(), home, None)


@pytest.mark.parametrize("port", [None, 0, 65536, -1, True, "443", 443.0])
def test_a_policy_with_hosts_needs_a_proxy_port_in_range(port):
    policy = minimal_policy(hosts=[{"host": "api.example.com", "port": 443}])
    with pytest.raises(ContextError):
        _render_document(policy, DARWIN_HOME, port)


def test_a_policy_with_hosts_renders_at_both_port_bounds():
    policy = minimal_policy(hosts=[{"host": "api.example.com", "port": 443}])
    for port in (1, 65535):
        text = _render_document(policy, DARWIN_HOME, port)
        assert text.endswith(
            f'\n\n(allow network-outbound (remote ip "localhost:{port}"))\n'
        )


def test_render_refuses_a_policy_not_loaded_for_darwin():
    with pytest.raises(ContextError):
        render(Policy.parse(minimal_policy(), "darwin"), {"proxy_port": None})
    with pytest.raises(ContextError):
        render(loaded(minimal_policy(), "linux"), {"proxy_port": None})
    with pytest.raises(ContextError):
        render(minimal_policy(), {"proxy_port": None})  # type: ignore[arg-type]


def test_render_refuses_a_bad_context():
    policy = loaded(minimal_policy(), "darwin", DARWIN_DATA, DARWIN_HOME)
    for context in (
        None,
        {},
        {"home": DARWIN_HOME},
        {"proxy_port": None, "home": "/Users/other"},
        {"proxy_port": None, "egress_socket": "/run/astral/egress.sock"},
        [("proxy_port", None)],
    ):
        with pytest.raises(ContextError):
            render(policy, context)  # type: ignore[arg-type]


def test_render_takes_home_from_the_loaded_record():
    policy = loaded(minimal_policy(), "darwin", DARWIN_DATA, "/Users/other")
    text = render(policy, {"proxy_port": None})
    assert '(deny file-read* (subpath "/Users/other/Library/Keychains"))' in text


def test_render_revalidates_for_darwin_and_needs_a_port_for_hosts():
    hosts = minimal_policy(hosts=[{"host": "api.example.com", "port": 443}])
    policy = loaded(hosts, "darwin", DARWIN_DATA, DARWIN_HOME)
    with pytest.raises(ContextError):
        render(policy, {"proxy_port": None})
    assert render(policy, {"proxy_port": 3128}).endswith('"localhost:3128"))\n')
