"""Tests for the frozen fixture: disjoint partitions, independent labels, canonical
digests, and fail-closed rejection of empty, missing, overlapping, or mislabeled
partitions.
"""

from __future__ import annotations

import pytest

from prompt_optimization.fixture import (
    PlannerCase,
    build_fixture,
    render_case_block,
)


def _case(case_id: str, **overrides) -> PlannerCase:
    defaults = dict(
        case_id=case_id,
        request="List the projects for team Atlas.",
        available_tools=("list_projects", "fetch_record"),
        min_subtasks=1,
        max_subtasks=1,
        expected_tools=frozenset({"list_projects"}),
        simple=True,
    )
    defaults.update(overrides)
    return PlannerCase(**defaults)


def test_default_fixture_is_valid_and_disjoint():
    fixture = build_fixture()
    ids = {
        partition: {case.case_id for case in cases}
        for partition, cases in fixture.partitions.items()
    }
    assert ids["train"].isdisjoint(ids["validation"])
    assert ids["train"].isdisjoint(ids["holdout"])
    assert ids["validation"].isdisjoint(ids["holdout"])
    assert len(ids["train"]) >= 2
    assert len(ids["validation"]) >= 2
    assert len(ids["holdout"]) >= 2


def test_fixture_digests_are_deterministic_and_partition_bound():
    fixture = build_fixture()
    again = build_fixture()
    assert fixture.digest == again.digest
    assert fixture.partition_digests == again.partition_digests
    assert set(fixture.partition_digests) == {"train", "validation", "holdout"}
    assert all(len(digest) == 64 for digest in fixture.partition_digests.values())


def test_changing_holdout_changes_fixture_and_partition_digest():
    default = build_fixture()
    replaced = build_fixture(
        holdout=(
            _case("h-other", request="List synthetic final projects one."),
            _case("h-other-2", request="List synthetic final projects two."),
            _case("h-other-3", request="List synthetic final projects three."),
        )
    )
    assert replaced.digest != default.digest
    assert replaced.partition_digests["holdout"] != default.partition_digests["holdout"]
    assert (
        replaced.partition_digests["train"]
        == default.partition_digests["train"]
    )


def test_empty_validation_fails_configuration():
    with pytest.raises(Exception) as excinfo:
        build_fixture(validation=())
    assert "validation" in str(excinfo.value)


def test_missing_partition_fails_configuration():
    with pytest.raises(Exception) as excinfo:
        build_fixture(holdout=())
    assert "holdout" in str(excinfo.value)


def test_duplicate_case_across_partitions_fails():
    with pytest.raises(Exception) as excinfo:
        build_fixture(holdout=(_case("t-simple-projects"), _case("h-x")))
    assert "disjoint" in str(excinfo.value)


def test_duplicate_case_within_partition_fails():
    with pytest.raises(Exception) as excinfo:
        build_fixture(train=(_case("t-dup"), _case("t-dup")))
    assert "disjoint" in str(excinfo.value)


def test_invalid_subtask_range_fails():
    with pytest.raises(Exception) as excinfo:
        build_fixture(
            train=(
                _case("t-a", min_subtasks=3, max_subtasks=2),
                _case("t-b"),
            )
        )
    assert "subtask range" in str(excinfo.value)


def test_expected_tool_outside_available_fails():
    with pytest.raises(Exception) as excinfo:
        build_fixture(
            train=(
                _case("t-a", expected_tools=frozenset({"ghost_tool"})),
                _case("t-b"),
            )
        )
    assert "outside its available list" in str(excinfo.value)


def test_tiny_partition_fails_configuration():
    with pytest.raises(Exception) as excinfo:
        build_fixture(train=(_case("t-only"),))
    assert "at least" in str(excinfo.value)


def test_unlabeled_case_fails():
    with pytest.raises(Exception) as excinfo:
        build_fixture(
            train=(
                _case("t-a", request="   "),
                _case("t-b"),
            )
        )
    assert "unlabeled case" in str(excinfo.value)


def test_case_ids_helper():
    from prompt_optimization.fixture import case_ids

    assert case_ids([_case("t-one"), _case("t-two")]) == ["t-one", "t-two"]


def test_render_case_block_shape():
    case = _case("t-x", available_tools=("list_projects", "fetch_record"))
    block = render_case_block(case)
    assert block.startswith("[available tools]\nlist_projects\nfetch_record")
    assert "[request]\nList the projects for team Atlas." in block


def test_renamed_hidden_duplicate_fails_partition_validation():
    from dataclasses import replace
    from prompt_optimization.fixture import FixtureError

    fixture = build_fixture()
    duplicate = replace(fixture.cases("train")[0], case_id="hidden-duplicate")
    with pytest.raises(FixtureError, match="disjoint"):
        build_fixture(holdout=(duplicate,) + fixture.cases("holdout")[1:])


def test_case_ids_and_labels_do_not_reopen_exposed_inputs():
    from dataclasses import replace

    fixture = build_fixture()
    relabeled = build_fixture(holdout=tuple(
        replace(case, case_id="renamed-" + case.case_id, simple=not case.simple)
        for case in fixture.cases("holdout")
    ))
    assert relabeled.holdout_input_digest == fixture.holdout_input_digest
    assert relabeled.digest != fixture.digest


def test_explicit_new_holdout_file_is_loaded_and_validation_stays_frozen(tmp_path):
    import json
    from prompt_optimization.fixture import load_holdout

    original = build_fixture()
    cases = [case.to_dict() for case in original.cases("holdout")]
    for case in cases:
        case["request"] = "New synthetic scenario: " + case["request"]
    path = tmp_path / "holdout.json"
    path.write_text(json.dumps({"schema_version": 1, "holdout": cases}))
    fixture = load_holdout(path)
    assert fixture.cases("train") == original.cases("train")
    assert fixture.cases("validation") == original.cases("validation")
    assert fixture.partition_digests["holdout"] != original.partition_digests["holdout"]


def test_holdout_loader_rejects_non_utf8_input(tmp_path):
    import json
    from prompt_optimization.fixture import FixtureError, load_holdout

    cases = [case.to_dict() for case in build_fixture().cases("holdout")]
    path = tmp_path / "utf16.json"
    path.write_bytes(json.dumps({"schema_version": 1, "holdout": cases}).encode("utf-16"))
    with pytest.raises(FixtureError):
        load_holdout(path)


@pytest.mark.parametrize("field,value", [
    ("case_id", ""), ("request", 1), ("min_subtasks", True), ("max_subtasks", 33),
    ("simple", 1), ("expect_dependency", None), ("available_tools", [42]),
    ("expected_tools", "list_projects"), ("forbidden_tools", ["x" * 129]),
    ("unknown", "field"),
])
def test_holdout_loader_rejects_invalid_case_schema(tmp_path, field, value):
    import json
    from prompt_optimization.fixture import FixtureError, load_holdout

    cases = [case.to_dict() for case in build_fixture().cases("holdout")]
    cases[0][field] = value
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({"schema_version": 1, "holdout": cases}))
    with pytest.raises(FixtureError):
        load_holdout(path)


def test_holdout_loader_bounds_file_before_decoding(tmp_path):
    from prompt_optimization.fixture import FixtureError, load_holdout

    path = tmp_path / "oversized.json"
    path.write_bytes(b"x" * 65537)
    with pytest.raises(FixtureError, match="exceeds"):
        load_holdout(path)
