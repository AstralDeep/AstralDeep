"""Records immutable-range test and CI changes that need maintainer scrutiny.
Warnings are review diagnostics and never replace behavioral qualification or authorize merging.
"""

import argparse
import ast
import json
import re
import subprocess
from pathlib import Path


def test_inventory(source):
    tree = ast.parse(source or "")
    tests = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    }
    skips = sum(
        isinstance(node, ast.Attribute)
        and node.attr in {"skip", "skipif", "xfail", "skipTest"}
        for node in ast.walk(tree)
    )
    swallowed = sum(
        isinstance(node, ast.ExceptHandler)
        and any(isinstance(item, ast.Pass) for item in node.body)
        for node in ast.walk(tree)
    )
    return tests, skips, swallowed


def inspect_change(path, before, after):
    flags = []
    if path.endswith(".py") and (
        "tests/" in path or Path(path).name.startswith("test_")
    ):
        old_tests, old_skips, old_swallowed = test_inventory(before)
        tests, skips, swallowed = test_inventory(after)
        if old_tests - tests:
            flags.append({"kind": "removed-test", "tests": sorted(old_tests - tests)})
        if skips > old_skips:
            flags.append({"kind": "added-skip-or-xfail", "count": skips - old_skips})
        if swallowed > old_swallowed:
            flags.append(
                {
                    "kind": "added-swallowed-test-error",
                    "count": swallowed - old_swallowed,
                }
            )
    if (
        path.startswith((".github/", "scripts/", "tooling/"))
        or path == ".specify/memory/constitution.md"
        or Path(path).name in {"pyproject.toml", ".coveragerc", "pytest.ini", "uv.lock"}
    ):
        flags.append({"kind": "qualification-policy-change"})
    return [{"path": path, **flag} for flag in flags]


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True
    ).stdout


def collect(root, base, head):
    for sha in (base, head):
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("review range requires immutable commit SHAs")
        git(root, "cat-file", "-e", sha + "^{commit}")
    paths = git(
        root, "diff", "--no-renames", "--name-only", "-z", base, head, "--"
    ).split(b"\0")
    flags = []
    for raw in filter(None, paths):
        path = raw.decode("utf-8")
        versions = []
        for sha in (base, head):
            exists = (
                subprocess.run(
                    ["git", "cat-file", "-e", sha + ":" + path],
                    cwd=root,
                    capture_output=True,
                ).returncode
                == 0
            )
            versions.append(
                git(root, "show", sha + ":" + path).decode("utf-8")
                if exists and path.endswith(".py")
                else ""
            )
        flags.extend(inspect_change(path, *versions))
    return {
        "base": base,
        "head": head,
        "review_required": bool(flags),
        "findings": flags,
        "diagnostic_only": True,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Record immutable qualification review findings."
    )
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    receipt = collect(args.root, args.base, args.head)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2))
    if receipt["review_required"]:
        print(
            "::warning::Tests or qualification policy changed; inspect the immutable review receipt before merge."
        )


if __name__ == "__main__":
    main()
