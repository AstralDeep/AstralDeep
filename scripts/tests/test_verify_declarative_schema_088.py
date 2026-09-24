"""Tests that the schema declaration is read statically as data only, never executed,
and that its grammar cannot expand beyond the reviewed declarative module.
"""

from pathlib import Path

import pytest
from test_verify_composition import composition

SYMBOL = "DECLARATIVE_AGENT_SCHEMA_STATEMENTS"
IMPORT = f"from astralplane.database.declarative_agent_schema import {SYMBOL}\n"
FINAL = "from typing import Final\n"


def read(tmp_path: Path, source: str):
    path = tmp_path / "src/astralplane/database/declarative_agent_schema.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    tree = composition.ast.parse(IMPORT)
    return composition._plane_schema_literal_import(
        tmp_path, tree, stem="declarative_agent", symbol=SYMBOL,
    )


@pytest.mark.parametrize("prefix,annotation", [("", ""), (FINAL, ": Final")])
def test_exact_literal_and_final_declarations_are_data_only(tmp_path, prefix, annotation):
    assert read(tmp_path, f'{prefix}{SYMBOL}{annotation} = ("SQL β",)\n') == {
        SYMBOL: ("SQL β",),
    }


@pytest.mark.parametrize("source", [
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
])
def test_annotation_cannot_expand_the_reviewed_grammar(tmp_path, source):
    with pytest.raises(composition.CompositionError):
        read(tmp_path, source)


def test_executable_annotation_is_not_evaluated(tmp_path):
    marker = tmp_path / "candidate-executed"
    source = (f"{FINAL}{SYMBOL}: Final = ('SQL',)\n"
              f"__import__('pathlib').Path({str(marker)!r}).touch()\n")
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
            tmp_path, tree, stem="operation", symbol="OPERATION_SCHEMA_STATEMENTS",
        )
