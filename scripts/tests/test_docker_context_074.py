"""Pre-build Docker-context sentinel checks for feature 074.

This memory-light gate implements only the documented Docker ignore pattern
forms used by this repository (root-relative paths, ``*``, ``?``, ``**``, and
last-match-wins negation). It deliberately does not invoke Docker and is not an
engine-equivalence proof; a real image build remains a later qualification
step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOCKERIGNORE = REPOSITORY_ROOT / ".dockerignore"


@dataclass(frozen=True)
class _Rule:
    exclude: bool
    expression: re.Pattern[str]


def _docker_glob_expression(pattern: str) -> re.Pattern[str]:
    """Compile the Docker glob subset present in this repository's rules."""
    if any(token in pattern for token in ("[", "]", "\\")):
        raise AssertionError(
            f"extend the sentinel matcher before using this pattern form: {pattern!r}"
        )

    expression: list[str] = ["^"]
    index = 0
    while index < len(pattern):
        if pattern.startswith("**", index):
            index += 2
            if index < len(pattern) and pattern[index] == "/":
                expression.append("(?:[^/]+/)*")
                index += 1
            else:
                expression.append(".*")
            continue

        character = pattern[index]
        if character == "*":
            expression.append("[^/]*")
        elif character == "?":
            expression.append("[^/]")
        else:
            expression.append(re.escape(character))
        index += 1

    expression.append("$")
    return re.compile("".join(expression))


def _load_rules(path: Path) -> tuple[_Rule, ...]:
    rules: list[_Rule] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or raw_line.startswith("#"):
            continue

        exclude = not line.startswith("!")
        if not exclude:
            line = line[1:]
        line = line.strip("/")
        if not line or line == ".":
            continue
        rules.append(_Rule(exclude, _docker_glob_expression(line)))
    return tuple(rules)


def _path_prefixes(relative_path: str) -> tuple[str, ...]:
    parts = PurePosixPath(relative_path).parts
    return tuple("/".join(parts[:end]) for end in range(1, len(parts) + 1))


def _is_excluded(relative_path: str, rules: tuple[_Rule, ...]) -> bool:
    excluded = False
    prefixes = _path_prefixes(relative_path)
    for rule in rules:
        # Excluding a directory excludes its descendants. The negated forms in
        # this file target the complete candidate path, so they cannot
        # accidentally reopen a descendant of an excluded directory.
        candidates = prefixes if rule.exclude else (relative_path,)
        if any(rule.expression.fullmatch(candidate) for candidate in candidates):
            excluded = rule.exclude
    return excluded


def _context_members(context_root: Path, rules: tuple[_Rule, ...]) -> set[str]:
    members: set[str] = set()
    for candidate in context_root.rglob("*"):
        if not candidate.is_file():
            continue
        relative_path = candidate.relative_to(context_root).as_posix()
        if not _is_excluded(relative_path, rules):
            members.add(relative_path)
    return members


def test_synthetic_component_context_excludes_nested_sensitive_state(
    tmp_path: Path,
) -> None:
    included = {
        "components/AstralPlane/src/astralplane/store.py",
        "components/AstralPrimitives/src/astralprims/core.py",
        "components/AstralProjection/src/astralprojection/render.py",
        "components/LETS/lets/client.py",
    }
    excluded = {
        "components/AstralProjection/config/private/.env.production",
        "components/AstralProjection/config/private/service.env",
        "components/AstralProjection/config/private/service.key",
        "components/AstralProjection/config/private/trust.pem",
        "components/AstralPlane/runtime/nested/state/app.db",
        "components/AstralPlane/runtime/nested/state/app.db-wal",
        "components/AstralPlane/runtime/nested/logs/app.log.1",
        "components/AstralProjection/android-client/app/signing/release.jks",
        "components/AstralProjection/android-client/app/signing/key.properties",
        "components/LETS/paper/submission/sections/method.tex",
        "components/LETS/paper/paper.pdf",
        "components/LETS/results/generated/nested/trial.json",
        "components/AstralProjection/backend/agents/example/data/session.json",
        "components/AstralPlane/backend/knowledge/tenants/test/index.json",
    }

    context_root = tmp_path / "synthetic-context"
    for relative_path in sorted(included | excluded):
        sentinel = context_root / Path(relative_path)
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("feature-074 sentinel\n", encoding="utf-8")

    members = _context_members(context_root, _load_rules(DOCKERIGNORE))

    assert members == included
