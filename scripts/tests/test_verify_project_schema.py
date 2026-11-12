"""Tests that the schema declaration is read statically as data only, never executed,
and that its grammar cannot expand beyond the reviewed declarative module.
"""

from pathlib import Path

import pytest
from test_verify_composition import composition

SYMBOL = "PROJECT_SCHEMA_STATEMENTS"
IMPORT = f"from astralplane.database.project_schema import {SYMBOL}\n"
FINAL = "from typing import Final\n"


def read(tmp_path: Path, source: str):
    path = tmp_path / "src/astralplane/database/project_schema.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    tree = composition.ast.parse(IMPORT)
    return composition._plane_schema_literal_import(
        tmp_path,
        tree,
        stem="project",
        symbol=SYMBOL,
    )


@pytest.mark.parametrize("prefix,annotation", [("", ""), (FINAL, ": Final")])
def test_exact_literal_and_final_declarations_are_data_only(
    tmp_path, prefix, annotation
):
    assert read(tmp_path, f'{prefix}{SYMBOL}{annotation} = ("SQL β",)\n') == {
        SYMBOL: ("SQL β",),
    }


@pytest.mark.parametrize(
    "source",
    [
        f"from arbitrary import Final\n{SYMBOL}: Final = ('SQL',)\n",
        f"from typing import Final as Other\n{SYMBOL}: Other = ('SQL',)\n",
        f"from typing import Final, Any\n{SYMBOL}: Final = ('SQL',)\n",
        f"from .typing import Final\n{SYMBOL}: Final = ('SQL',)\n",
        f"import typing\n{SYMBOL}: typing.Final = ('SQL',)\n",
        f"{SYMBOL}: Final = ('SQL',)\n",
        f"{FINAL}{SYMBOL}: Final[str] = ('SQL',)\n",
        f"{FINAL}{SYMBOL}: str = ('SQL',)\n",
        f"{FINAL}{SYMBOL} = ('SQL',)\n",
        f"{FINAL}{FINAL}{SYMBOL}: Final = ('SQL',)\n",
        f"{FINAL}Final = str\n{SYMBOL}: Final = ('SQL',)\n",
        f"{FINAL}{SYMBOL}: Final\n",
        f"{FINAL}OTHER: Final = ('SQL',)\n",
        f"{FINAL}{SYMBOL}: Final = ('SQL'.strip(),)\n",
        f"{FINAL}{SYMBOL}: Final = tuple(['SQL'])\n",
        f"{FINAL}{SYMBOL}: Final = ('SQL',) + ('SECOND',)\n",
        f"{FINAL}{SYMBOL}: Final = ('',)\n",
        f"{FINAL}{SYMBOL}: Final = (1,)\n",
    ],
)
def test_annotation_cannot_expand_the_reviewed_grammar(tmp_path, source):
    with pytest.raises(composition.CompositionError):
        read(tmp_path, source)


def test_executable_annotation_is_not_evaluated(tmp_path):
    marker = tmp_path / "candidate-executed"
    source = (
        f"{FINAL}{SYMBOL}: Final = ('SQL',)\n"
        f"__import__('pathlib').Path({str(marker)!r}).touch()\n"
    )
    with pytest.raises(composition.CompositionError):
        read(tmp_path, source)
    assert not marker.exists()


def test_final_grammar_is_limited_to_reviewed_declarative_module(tmp_path):
    path = tmp_path / "src/astralplane/database/operation_schema.py"
    path.parent.mkdir(parents=True)
    path.write_text(f"{FINAL}OPERATION_SCHEMA_STATEMENTS: Final = ('SQL',)\n")
    tree = composition.ast.parse(
        "from astralplane.database.operation_schema import OPERATION_SCHEMA_STATEMENTS\n"
    )
    with pytest.raises(composition.CompositionError):
        composition._plane_schema_literal_import(
            tmp_path,
            tree,
            stem="operation",
            symbol="OPERATION_SCHEMA_STATEMENTS",
        )


@pytest.mark.parametrize(
    "mutation", ["foreign", "duplicate", "nested", "alias", "rebound"]
)
def test_project_import_identity_cannot_be_changed(tmp_path, mutation):
    imports = {
        "foreign": IMPORT.replace("astralplane.database", "arbitrary"),
        "duplicate": IMPORT + IMPORT,
        "nested": "if True:\n    " + IMPORT,
        "alias": IMPORT.replace(SYMBOL, f"{SYMBOL} as ALIAS"),
        "rebound": IMPORT + f"{SYMBOL} = ('REBOUND',)\n",
    }
    path = tmp_path / "src/astralplane/database/project_schema.py"
    path.parent.mkdir(parents=True)
    path.write_text(f"{FINAL}{SYMBOL}: Final = ('SQL',)\n")
    with pytest.raises(composition.CompositionError):
        composition._plane_schema_literal_import(
            tmp_path,
            composition.ast.parse(imports[mutation]),
            stem="project",
            symbol=SYMBOL,
        )


@pytest.mark.parametrize(
    "source",
    [
        f"{FINAL}{SYMBOL}: Final = " + repr(("SQL",) * 4097),
        " " * 1_048_577,
    ],
)
def test_project_source_and_tuple_bounds_are_enforced(tmp_path, source):
    with pytest.raises(composition.CompositionError):
        read(tmp_path, source)


def test_project_schema_symlink_is_refused(tmp_path):
    source = tmp_path / "outside.py"
    source.write_text(f"{FINAL}{SYMBOL}: Final = ('SQL',)\n")
    path = tmp_path / "src/astralplane/database/project_schema.py"
    path.parent.mkdir(parents=True)
    path.symlink_to(source)
    with pytest.raises(composition.CompositionError, match="regular local source"):
        composition._plane_schema_literal_import(
            tmp_path, composition.ast.parse(IMPORT), stem="project", symbol=SYMBOL
        )


def test_pinned_project_registry_matches_qualified_digest():
    root = Path(__file__).resolve().parents[2] / "components/AstralPlane"
    assert (
        composition._plane_migration_digest(root)
        == "dde302967743cf8f6f5d55e82f8355ef405823dfee638909c3a2f11b85f5cb27"
    )


MESH_SYMBOL = "MESH_EVENT_SCHEMA_STATEMENTS"
MESH_IMPORT = f"from astralplane.database.mesh_event_schema import {MESH_SYMBOL}\n"
CONSTANTS = "_HASH = \"'^sha256:'\"\n_BOUND = \"9007\"\n"


def read_formatted(tmp_path: Path, source: str, stem: str = "mesh_event", symbol: str = MESH_SYMBOL):
    path = tmp_path / f"src/astralplane/database/{stem}_schema.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    tree = composition.ast.parse(f"from astralplane.database.{stem}_schema import {symbol}\n")
    return composition._plane_schema_literal_import(tmp_path, tree, stem=stem, symbol=symbol)


@pytest.mark.parametrize(
    "stem,symbol",
    [("mesh_event", MESH_SYMBOL), ("run_ledger", "RUN_LEDGER_SCHEMA_STATEMENTS")],
)
def test_formatted_schema_tuples_resolve_only_module_string_constants(tmp_path, stem, symbol):
    source = (
        '"""Doc."""\n' + CONSTANTS
        + f'{symbol} = (\n    f"""\nCREATE TABLE t (h TEXT CHECK(h ~ {{_HASH}}), n BIGINT CHECK(n < {{_BOUND}}))\n""".strip(),\n'
        '    "SELECT 1",\n)\n'
    )
    assert read_formatted(tmp_path, source, stem, symbol) == {
        symbol: ("CREATE TABLE t (h TEXT CHECK(h ~ '^sha256:'), n BIGINT CHECK(n < 9007))", "SELECT 1"),
    }


@pytest.mark.parametrize(
    "source",
    [
        f'{CONSTANTS}{MESH_SYMBOL} = (f"{{UNKNOWN}}".strip(),)\n',
        f'{CONSTANTS}{MESH_SYMBOL} = (f"{{_HASH!r}}".strip(),)\n',
        f'{CONSTANTS}{MESH_SYMBOL} = (f"{{_HASH:>4}}".strip(),)\n',
        f'{CONSTANTS}{MESH_SYMBOL} = (f"{{_HASH.upper()}}".strip(),)\n',
        f'{CONSTANTS}_HASH = "again"\n{MESH_SYMBOL} = ("SQL",)\n',
        f'_HASH = f"{{_BOUND}}"\n_BOUND = "1"\n{MESH_SYMBOL} = ("SQL",)\n',
        f'_HASH = 1\n{MESH_SYMBOL} = ("SQL",)\n',
        f'import os\n{MESH_SYMBOL} = ("SQL",)\n',
        f'{CONSTANTS}{MESH_SYMBOL} = (f"{{_HASH}}",)\n',
        f'{CONSTANTS}{MESH_SYMBOL} = (f"{{_HASH}}".lower(),)\n',
    ],
)
def test_formatted_schema_grammar_cannot_expand(tmp_path, source):
    with pytest.raises(composition.CompositionError):
        read_formatted(tmp_path, source)


def test_formatted_strings_stay_refused_for_other_schemas(tmp_path):
    source = f'{CONSTANTS}{SYMBOL} = (f"{{_HASH}}".strip(),)\n'
    with pytest.raises(composition.CompositionError):
        read_formatted(tmp_path, source, "project", SYMBOL)
