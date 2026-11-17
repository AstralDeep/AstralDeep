"""Tests canonical JSON key validation, structural bounds, and byte stability in
repository helpers. Public repository rejection before SQL is covered in
test_remote_proposals.py.
"""

from types import MappingProxyType

import pytest

from astralplane.repositories import (
    CANONICAL_JSON_MAX_BYTES,
    CANONICAL_JSON_MAX_DEPTH,
    CANONICAL_JSON_MAX_NODES,
    RepositoryValidationError,
    _canonical_json,
)


def _nested_chain(levels: int) -> object:
    value: object = "leaf"
    for _ in range(levels):
        value = {"nested": (value,)}
    return value


@pytest.mark.parametrize(
    "value, expected",
    [
        ({}, "{}"),
        ({"b": 2, "a": 1}, '{"a":1,"b":2}'),
        ({"1": "string", "": "empty"}, '{"":"empty","1":"string"}'),
        (
            MappingProxyType({"b": True, "a": (MappingProxyType({"z": None}), "é")}),
            '{"a":[{"z":null},"é"],"b":true}',
        ),
    ],
)
def test_string_keys_preserve_canonical_json_bytes(value: object, expected: str) -> None:
    assert _canonical_json(value, "arguments") == expected


@pytest.mark.parametrize(
    "key", [1, None, True, 1.5, object()], ids=["int", "none", "bool", "float", "custom"]
)
@pytest.mark.parametrize("nested", ["mapping", "list", "tuple"])
def test_non_string_keys_are_rejected_at_every_mapping_depth(key: object, nested: str) -> None:
    mapping = {key: "rejected", str(key): "retained"}
    value = (
        mapping
        if nested == "mapping"
        else {"nested": [mapping] if nested == "list" else (mapping,)}
    )
    with pytest.raises(RepositoryValidationError) as failure:
        _canonical_json(value, "arguments")
    assert failure.value.code == "repository_validation"


def test_deeper_mapping_rejection_preserves_valid_bytes() -> None:
    valid: object = {"1": "retained"}
    invalid: object = {1: "rejected", "1": "retained"}
    for _ in range(32):
        valid = {"nested": (valid,)}
        invalid = {"nested": (invalid,)}
    assert (
        _canonical_json(valid, "arguments") == '{"nested":[' * 32 + '{"1":"retained"}' + "]}" * 32
    )
    with pytest.raises(RepositoryValidationError):
        _canonical_json(invalid, "arguments")


def test_cyclic_mapping_and_list_are_typed_rejections() -> None:
    cyclic: dict[str, object] = {"a": 1}
    cyclic["self"] = cyclic
    with pytest.raises(RepositoryValidationError, match="cyclic") as failure:
        _canonical_json(cyclic, "arguments")
    assert failure.value.code == "repository_validation"
    assert failure.value.metadata == (("field", "arguments"),)
    loop: list[object] = [1]
    loop.append(loop)
    with pytest.raises(RepositoryValidationError, match="cyclic") as failure:
        _canonical_json({"nested": (loop,)}, "arguments")
    assert failure.value.code == "repository_validation"


def test_depth_limit_admits_just_below_and_rejects_just_above() -> None:
    just_below = _nested_chain(CANONICAL_JSON_MAX_DEPTH // 2)
    wrappers = CANONICAL_JSON_MAX_DEPTH // 2
    expected = '{"nested":[' * wrappers + '"leaf"' + "]}" * wrappers
    assert _canonical_json(just_below, "arguments") == expected
    with pytest.raises(RepositoryValidationError, match="structural bounds") as failure:
        _canonical_json(_nested_chain(wrappers + 1), "arguments")
    assert failure.value.code == "repository_validation"
    assert int(dict(failure.value.metadata)["depth"]) > CANONICAL_JSON_MAX_DEPTH


def test_node_limit_admits_just_below_and_rejects_just_above() -> None:
    just_below = {f"key-{index}": index for index in range(CANONICAL_JSON_MAX_NODES - 1)}
    assert len(_canonical_json(just_below, "arguments")) > 0
    just_above = {f"key-{index}": index for index in range(CANONICAL_JSON_MAX_NODES)}
    with pytest.raises(RepositoryValidationError, match="structural bounds") as failure:
        _canonical_json(just_above, "arguments")
    assert failure.value.code == "repository_validation"
    assert int(dict(failure.value.metadata)["nodes"]) > CANONICAL_JSON_MAX_NODES


def test_shared_non_cyclic_subtrees_stay_valid_with_stable_bytes() -> None:
    shared = {"token_count": 3}
    value = {"a": shared, "b": [shared, (shared,)], "c": {"d": shared}}
    assert _canonical_json(value, "arguments") == (
        '{"a":{"token_count":3},'
        '"b":[{"token_count":3},[{"token_count":3}]],'
        '"c":{"d":{"token_count":3}}}'
    )


def test_unpaired_surrogates_are_typed_rejections() -> None:
    for value in ({"x": "\ud800"}, {"nested": ["ok", "\udfff"]}, "\ud800"):
        with pytest.raises(RepositoryValidationError, match="surrogate") as failure:
            _canonical_json(value, "arguments")
        assert failure.value.code == "repository_validation"
    assert _canonical_json({"emoji": "\U0001f600"}, "arguments") == '{"emoji":"\U0001f600"}'


def test_surrogate_keys_and_hostile_sort_order_are_typed_rejections() -> None:
    with pytest.raises(RepositoryValidationError, match="surrogate") as failure:
        _canonical_json({"x\ud800": 1}, "arguments")
    assert failure.value.code == "repository_validation"

    class HostileOrder(str):
        def __lt__(self, other: object) -> bool:
            raise ValueError("unorderable key")

        def __gt__(self, other: object) -> bool:
            raise ValueError("unorderable key")

    with pytest.raises(RepositoryValidationError, match="canonical JSON-compatible"):
        _canonical_json({HostileOrder("a"): 1, "b": 2}, "arguments")


def test_byte_bound_rejects_oversized_canonical_output() -> None:
    within = {"blob": "x" * (CANONICAL_JSON_MAX_BYTES - 12)}
    assert len(_canonical_json(within, "arguments")) == CANONICAL_JSON_MAX_BYTES - 1
    with pytest.raises(RepositoryValidationError, match="byte bound") as failure:
        _canonical_json({"blob": "x" * CANONICAL_JSON_MAX_BYTES}, "arguments")
    assert failure.value.code == "repository_validation"
    assert failure.value.metadata == (
        ("field", "arguments"),
        ("maximum", str(CANONICAL_JSON_MAX_BYTES)),
    )
