"""Prevents SQLite's split publication writes from losing a PostgreSQL eligibility predicate.
The live transition tests separately prove actual writes, replay, rollback and native SQL failures.
"""

import ast
import inspect
import re
import textwrap

import pytest

from astralplane.database.sqlite_sql import _segments
from astralplane.repositories import generated_agent_publication_sqlite as sqlite_publication
from astralplane.repositories.generated_agent_publications import (
    GeneratedAgentPublicationRepository,
)


def canonical(sql):
    return "".join(
        re.sub(r"\s+", " ", text) if code else text for code, text in _segments(sql)
    ).strip()


@pytest.mark.parametrize(
    "method,following",
    [
        ("rebind_recovery_attempt", "renewed"),
        ("mark_validated", "persisted_result"),
        ("fail", "failed_draft"),
        ("commit_published", "published_draft"),
    ],
)
def test_sqlite_eligibility_preserves_every_original_identity_and_authority_fence(
    method, following
):
    tree = ast.parse(
        textwrap.dedent(inspect.getsource(getattr(GeneratedAgentPublicationRepository, method)))
    )
    statement = next(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "WITH eligible AS (" in node.value
    )
    eligible = statement.split("WITH eligible AS (", 1)[1].split("), " + following + " AS (", 1)[0]
    actual = getattr(sqlite_publication, "_" + method.upper() + "_ELIGIBLE")
    assert canonical(actual) == canonical(eligible)
