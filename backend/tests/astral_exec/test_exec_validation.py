"""Runs every committed validation vector through astral_exec.Policy and pins the parsing edge cases the vectors do not name.
The vector file is a copy of the contract vectors, so a failure here means the product package disagrees with the reference.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from astral_exec import Policy, PolicyError
from astral_exec.constants import VALIDATION_CODES
from exec_vectors import VALIDATION, minimal_policy


def _input(case):
    if "bytes_hex" in case:
        return bytes.fromhex(case["bytes_hex"])
    return case["raw"] if "raw" in case else case["policy"]


@pytest.mark.parametrize("case", VALIDATION, ids=[c["name"] for c in VALIDATION])
def test_validation_vector(case):
    if case["code"] == "ok":
        policy = Policy.parse(_input(case), case["platform"])
        assert policy.digest() == case["digest"]
        return
    with pytest.raises(PolicyError) as caught:
        Policy.parse(_input(case), case["platform"])
    assert caught.value.code == case["code"]


def test_every_validation_code_has_a_vector():
    seen = {c["code"] for c in VALIDATION} - {"ok"}
    assert seen == set(VALIDATION_CODES)


def test_platform_is_a_required_argument():
    with pytest.raises(TypeError):
        Policy.parse(minimal_policy())  # type: ignore[call-arg]


def test_bytes_text_and_mapping_share_one_digest():
    doc = minimal_policy()
    text = json.dumps(doc, indent=3)
    digests = {
        Policy.parse(text, "linux").digest(),
        Policy.parse(text.encode("utf-8"), "linux").digest(),
        Policy.parse(doc, "linux").digest(),
        Policy.parse(json.dumps(doc, separators=(",", ":")), "darwin").digest(),
    }
    assert len(digests) == 1


def test_digest_is_sha256_over_the_canonical_bytes():
    policy = Policy.parse(minimal_policy(), "linux")
    expected = hashlib.sha256(policy.canonical_bytes()).hexdigest()
    assert policy.digest() == f"sha256:{expected}"
    assert policy.canonical_bytes() == json.dumps(
        minimal_policy(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def test_document_is_a_fresh_copy_each_time():
    policy = Policy.parse(minimal_policy(), "linux")
    first = policy.document
    first["workspace"] = "/etc"
    first["limits"]["processes"] = 4096
    assert policy.document == minimal_policy()


def test_the_caller_mapping_is_not_aliased():
    doc = minimal_policy()
    policy = Policy.parse(doc, "linux")
    doc["workspace"] = "/etc"
    assert policy.document["workspace"] == "/private/tmp/astral/ws"


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff\xfe{\x00}\x00",
        b"\xef\xbb\xbf" + json.dumps(minimal_policy()).encode(),
        json.dumps(minimal_policy()).encode("utf-16"),
        b'{"v":1,"workspace":"/w/\xff"}',
        '{"v":1,"workspace":"/w/\\ud800"}',
        "[" * 100000,
        '{"v":' + "9" * 5000 + "}",
        '{"v":NaN}',
        '{"v":Infinity}',
        '{"v":1e0}',
        "",
        b"",
        "null",
        "1",
    ],
    ids=[
        "utf16_without_bom",
        "utf8_bom",
        "utf16_with_bom",
        "invalid_utf8",
        "lone_surrogate_escape",
        "deep_nesting",
        "huge_integer",
        "nan",
        "infinity",
        "exponent_float",
        "empty_text",
        "empty_bytes",
        "null_root",
        "number_root",
    ],
)
def test_hostile_text_is_malformed(raw):
    with pytest.raises(PolicyError) as caught:
        Policy.parse(raw, "linux")
    assert caught.value.code == "policy.malformed"


def test_non_json_types_are_malformed():
    for value in (None, 7, 1.5, [], ("v",)):
        with pytest.raises(PolicyError) as caught:
            Policy.parse(value, "linux")  # type: ignore[arg-type]
        assert caught.value.code == "policy.malformed"


def test_a_mapping_with_non_string_keys_is_refused():
    doc = minimal_policy()
    doc[1] = "x"  # type: ignore[index]
    with pytest.raises(PolicyError) as caught:
        Policy.parse(doc, "linux")
    assert caught.value.code == "policy.unknown_field"


def test_a_mapping_holding_a_float_is_malformed():
    doc = minimal_policy(time={"wall_ms": 30000.0})
    with pytest.raises(PolicyError) as caught:
        Policy.parse(doc, "linux")
    assert caught.value.code == "policy.malformed"


def test_policy_error_carries_a_code_and_a_readable_detail():
    with pytest.raises(PolicyError) as caught:
        Policy.parse(minimal_policy(v=2), "linux")
    error = caught.value
    assert error.code == "policy.malformed"
    assert str(error).startswith("policy.malformed")
    assert error.detail


def test_a_policy_is_hashable_and_comparable_by_digest():
    one = Policy.parse(minimal_policy(), "linux")
    two = Policy.parse(json.dumps(minimal_policy()), "darwin")
    other = Policy.parse(minimal_policy(workspace="/private/tmp/astral/other"), "linux")
    assert one == two and hash(one) == hash(two)
    assert one != other
    assert one != "not a policy"


def test_repr_names_the_digest_and_not_the_paths():
    policy = Policy.parse(minimal_policy(), "linux")
    assert repr(policy) == f"Policy({policy.digest()})"
    assert "/private" not in repr(policy)
