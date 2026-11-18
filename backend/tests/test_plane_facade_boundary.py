"""Checks that no Python file outside plane/ imports an AstralPlane module that plane/facade.json leaves internal.
Existing test files that still do are a frozen list that may shrink and never grow, and the checker also runs as a command against any checkout.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "astralplane"
FROZEN_INTERNAL_IMPORTERS: frozenset[str] = frozenset(
    {
        "backend/onboarding/tests/test_schema_init.py",
        "backend/persistent_agents/tests/test_plane_clone_postgres.py",
        "backend/tests/attachments/test_purge_coordinator_074.py",
        "backend/tests/helpers/plane_template.py",
        "backend/tests/helpers/voice_plane_runtime.py",
        "backend/tests/test_mesh_availability_topology.py",
        "backend/tests/test_mesh_owner_storage_sweep.py",
        "backend/tests/test_plane_cutover_boundary_074.py",
        "backend/tests/test_plane_integration.py",
        "backend/tests/test_plane_sqlite_selection.py",
        "backend/tests/test_voice_migration_065.py",
        "backend/tests/test_voice_plane_clone_postgres.py",
        "backend/tests/test_voice_transcript_proof_065.py",
        "backend/tests/test_voice_zero_retention_065.py",
    }
)
FROZEN_LIMIT = 15


class Facade:
    def __init__(self, document: dict[str, object], root_exports: frozenset[str]) -> None:
        self.modules = frozenset(document["modules"])  # type: ignore[arg-type]
        self.packages = frozenset(document["packages"])  # type: ignore[arg-type]
        self.root_exports = root_exports

    def is_public_module(self, name: str) -> bool:
        return name in self.modules or any(
            name == package or name.startswith(package + ".") for package in self.packages
        )

    def is_public_root_name(self, name: str) -> bool:
        return name in self.root_exports or self.is_public_module(f"{PACKAGE}.{name}")


def load_facade(root: Path) -> Facade:
    document = json.loads((root / "plane" / "facade.json").read_text(encoding="utf-8"))
    tree = ast.parse((root / "plane/src/astralplane/__init__.py").read_text(encoding="utf-8"))
    exports: frozenset[str] = frozenset()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            exports = frozenset(ast.literal_eval(node.value))
    assert exports, "plane root package declares no __all__"
    return Facade(document, exports)


def _belongs(name: str) -> bool:
    return name == PACKAGE or name.startswith(PACKAGE + ".")


def find_violations(source: str, facade: Facade, filename: str = "<source>") -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Import):
            found.extend(
                alias.name
                for alias in node.names
                if _belongs(alias.name) and not facade.is_public_module(alias.name)
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == PACKAGE:
                found.extend(
                    f"{PACKAGE}.{alias.name}"
                    for alias in node.names
                    if alias.name != "*" and not facade.is_public_root_name(alias.name)
                )
                found.extend(f"{PACKAGE}.*" for alias in node.names if alias.name == "*")
            elif _belongs(node.module) and not facade.is_public_module(node.module):
                found.append(node.module)
        elif isinstance(node, ast.Call) and node.args:
            function = node.func
            dynamic = (
                isinstance(function, ast.Attribute)
                and function.attr == "import_module"
                or isinstance(function, ast.Name)
                and function.id in {"__import__", "import_module"}
            )
            target = node.args[0]
            if (
                dynamic
                and isinstance(target, ast.Constant)
                and isinstance(target.value, str)
                and _belongs(target.value)
                and not facade.is_public_module(target.value)
            ):
                found.append(target.value)
    return found


def tracked_python_files(root: Path) -> list[str]:
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard",
         "--", "*.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(path for path in listing.split("\0") if path and not path.startswith("plane/"))


def scan(root: Path) -> dict[str, list[str]]:
    facade = load_facade(root)
    result: dict[str, list[str]] = {}
    for relative in tracked_python_files(root):
        path = root / relative
        if not path.is_file():
            continue
        try:
            violations = find_violations(path.read_text(encoding="utf-8"), facade, relative)
        except (SyntaxError, UnicodeDecodeError):
            continue
        if violations:
            result[relative] = sorted(set(violations))
    return result


def unexpected(found: dict[str, list[str]], frozen: Iterable[str] = FROZEN_INTERNAL_IMPORTERS) -> dict[str, list[str]]:
    allowed = set(frozen)
    return {path: names for path, names in found.items() if path not in allowed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    arguments = parser.parse_args(argv)
    problems = unexpected(scan(arguments.root))
    for path, names in problems.items():
        print(f"{path}: imports internal {', '.join(names)}", file=sys.stderr)
    return 1 if problems else 0


FACADE_FIXTURE = Facade(
    {"modules": ["astralplane", "astralplane.api", "astralplane.errors"],
     "packages": ["astralplane.repositories", "astralplane.authority"]},
    frozenset({"SCHEMA_REVISION", "create_runtime"}),
)


def test_internal_imports_are_reported() -> None:
    for source, expected in (
        ("from astralplane.database.pool import ConnectionPool\n", "astralplane.database.pool"),
        ("import astralplane.database.migrations as m\n", "astralplane.database.migrations"),
        ("from astralplane import database\n", "astralplane.database"),
        ('import importlib\nimportlib.import_module("astralplane.recovery")\n', "astralplane.recovery"),
        ('__import__("astralplane.outbox")\n', "astralplane.outbox"),
        ("from astralplane.database import *\n", "astralplane.database"),
        ("from astralplane import *\n", "astralplane.*"),
        ('from importlib import import_module\nimport_module("astralplane.purge")\n', "astralplane.purge"),
    ):
        assert find_violations(source, FACADE_FIXTURE) == [expected], source


def test_public_imports_are_accepted() -> None:
    for source in (
        "from astralplane import SCHEMA_REVISION\n",
        "from astralplane.repositories.history import X\n",
        "from astralplane.authority import AuthorityPopulation\n",
        "from astralplane import api, errors\n",
        "import astralplane\nimport astralplane.api\n",
        "from astralplane.repositories import agents\n",
        "import os\nfrom collections import abc\nimport importlib\nimportlib.import_module('json')\n",
        "from . import database\nfrom .database import pool\n",
        "importlib.import_module(name)\nprint(1)\n",
    ):
        assert find_violations(source, FACADE_FIXTURE) == [], source


def test_a_lookalike_package_name_is_not_astralplane() -> None:
    assert find_violations("import astralplane_extras.database\n", FACADE_FIXTURE) == []


def test_the_real_tree_only_imports_the_facade_apart_from_the_frozen_list() -> None:
    found = scan(ROOT)
    assert unexpected(found) == {}
    assert set(found) == set(FROZEN_INTERNAL_IMPORTERS), "remove files that no longer import internals"
    assert len(FROZEN_INTERNAL_IMPORTERS) <= FROZEN_LIMIT


def test_one_added_product_import_of_an_internal_fails_the_check(tmp_path: Path) -> None:
    copy = tmp_path / "scratch"
    subprocess.run(["git", "init", "-q", str(copy)], check=True)
    (copy / "plane/src/astralplane").mkdir(parents=True)
    for relative in ("plane/facade.json", "plane/src/astralplane/__init__.py"):
        (copy / relative).write_bytes((ROOT / relative).read_bytes())
    (copy / "backend").mkdir()
    (copy / "backend/ok.py").write_text("from astralplane import SCHEMA_REVISION\n", encoding="utf-8")
    assert main(["--root", str(copy)]) == 0
    (copy / "backend/product.py").write_text(
        "from astralplane.database.pool import ConnectionPool\n", encoding="utf-8"
    )
    assert main(["--root", str(copy)]) == 1
    command = subprocess.run(
        [sys.executable, str(Path(__file__)), "--root", str(copy)], capture_output=True, text=True
    )
    assert command.returncode == 1
    assert "backend/product.py" in command.stderr


def test_files_under_plane_are_not_scanned(tmp_path: Path) -> None:
    copy = tmp_path / "scratch"
    subprocess.run(["git", "init", "-q", str(copy)], check=True)
    (copy / "plane/src/astralplane").mkdir(parents=True)
    for relative in ("plane/facade.json", "plane/src/astralplane/__init__.py"):
        (copy / relative).write_bytes((ROOT / relative).read_bytes())
    (copy / "plane/tests").mkdir()
    (copy / "plane/tests/test_internal.py").write_text(
        "from astralplane.database.pool import ConnectionPool\n", encoding="utf-8"
    )
    (copy / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    assert scan(copy) == {}


if __name__ == "__main__":
    raise SystemExit(main())
