"""Checks that each release tag prefix starts exactly the workflow that owns it.
It reads every tag-triggered workflow under .github/workflows and matches sample tags with GitHub's filter syntax."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
WINDOWS = "release-windows.yml"
VESSEL = "release-vessel.yml"


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = pattern.index("]", i)
            out.append(pattern[i : end + 1])
            i = end
        else:
            out.append(re.escape(char))
        i += 1
    return re.compile("".join(out))


def _tag_patterns() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        triggers = yaml.safe_load(path.read_text(encoding="utf-8")).get(True) or {}
        tags = (triggers.get("push") or {}).get("tags") if isinstance(triggers, dict) else None
        if tags:
            found[path.name] = list(tags)
    return found


def _starting(tag: str) -> set[str]:
    return {
        name
        for name, patterns in _tag_patterns().items()
        if any(_glob_to_regex(pattern).fullmatch(tag) for pattern in patterns)
    }


def test_a_plain_version_tag_starts_only_the_windows_release() -> None:
    assert _starting("v0.4.0") == {WINDOWS}
    assert _starting("v2.0.25") == {WINDOWS}


@pytest.mark.parametrize("tag", ["macos-v2.0.25-98", "release"])
def test_other_prefixes_start_no_tag_workflow(tag: str) -> None:
    assert _starting(tag) == set()


@pytest.mark.parametrize("tag", ["vessel-v0.8.0", "vessel-v0.8.0rc1"])
def test_a_vessel_tag_starts_only_the_vessel_release(tag: str) -> None:
    assert _starting(tag) == {VESSEL}


def test_the_matcher_follows_the_filter_syntax() -> None:
    assert _glob_to_regex("v[0-9]*").fullmatch("v1")
    assert not _glob_to_regex("v[0-9]*").fullmatch("vessel-v1")
    assert _glob_to_regex("v*").fullmatch("vessel-v1")
    assert not _glob_to_regex("v*").fullmatch("v1/2")
    assert _glob_to_regex("v**").fullmatch("v1/2")
    assert _glob_to_regex("v?").fullmatch("v1")
