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
            _case("h-other"),
            _case("h-other-2"),
            _case("h-other-3"),
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
