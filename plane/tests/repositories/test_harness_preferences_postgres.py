"""Real PostgreSQL harness preferences preserve independent fields and serialize owner CAS.
Admission checks retain the captured revision while unrelated preference edits remain compatible.
"""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import psycopg2
import pytest

from astralplane.repositories import RepositoryConflictError, RepositoryValidationError
from astralplane.repositories.harness_bindings import HarnessProfileIdentity
from astralplane.repositories.harness_capture import HarnessSelectionRecord
from astralplane.repositories.preferences import PreferencesRepository
from tests.integration.test_lifecycle_acceptance import concurrent_database

IDENTITY = HarnessProfileIdentity("standard_local", "1.0.0", "a" * 64)


def test_harness_selection_is_owner_scoped_cas_and_preserves_theme(migrated_clone):
    db = migrated_clone.database
    prefs = PreferencesRepository()
    with db.transaction() as tx:
        assert prefs.harness.get(tx, owner_id="a") == HarnessSelectionRecord("a", 0, None, None)
        prefs.theme.put(tx, owner_id="a", theme={"accent": "blue"})
        chosen = prefs.harness.compare_and_set(
            tx, owner_id="a", expected_revision=0, identity=IDENTITY, qualification_digest="b" * 64
        )
        assert chosen.revision == 1 and chosen.identity == IDENTITY
        prefs.theme.put(tx, owner_id="a", theme={"accent": "green"})
        assert prefs.harness.get(tx, owner_id="a") == chosen
        assert prefs.harness.get(tx, owner_id="b").revision == 0
        assert prefs.harness.assert_current(tx, owner_id="a", expected=chosen) is None
        for expected in (replace(chosen, owner_id="b"), replace(chosen, revision=2)):
            with pytest.raises(RepositoryConflictError):
                prefs.harness.assert_current(tx, owner_id="a", expected=expected)
        with pytest.raises(RepositoryConflictError):
            prefs.harness.compare_and_set(
                tx, owner_id="a", expected_revision=0, identity=None, qualification_digest=None
            )
        reset = prefs.harness.compare_and_set(
            tx, owner_id="a", expected_revision=1, identity=None, qualification_digest=None
        )
        assert reset.revision == 2 and reset.identity is None
        assert json.loads(
            tx.fetch_one("SELECT preferences FROM user_preferences WHERE user_id=%s", ("a",))[
                "preferences"
            ]
        )["theme"] == {"accent": "green"}


@pytest.mark.parametrize("revision", [True, -1, 1.0, "0", 2**53])
def test_harness_selection_refuses_noninteger_revision_without_mutation(migrated_clone, revision):
    with migrated_clone.database.transaction() as tx:
        with pytest.raises(RepositoryValidationError):
            PreferencesRepository().harness.compare_and_set(
                tx,
                owner_id="a",
                expected_revision=revision,
                identity=IDENTITY,
                qualification_digest="b" * 64,
            )
        assert tx.fetch_one("SELECT 1 AS found FROM user_preferences WHERE user_id='a'") is None


@pytest.mark.parametrize(
    "document",
    [
        "[]",
        '{"harness_selection":[]}',
        '{"harness_selection":{"revision":0,"identity":null,"qualification_digest":null,"extra":1}}',
        '{"harness_selection":{"revision":true,"identity":null,"qualification_digest":null}}',
        '{"harness_selection":{"revision":1,"identity":{},"qualification_digest":null}}',
    ],
)
def test_malformed_persisted_preference_never_becomes_default(migrated_clone, document):
    with migrated_clone.database.transaction() as tx:
        tx.execute(
            "INSERT INTO user_preferences(user_id,preferences,updated_at) VALUES('a',%s,0)",
            (document,),
        )
        with pytest.raises(RepositoryValidationError):
            PreferencesRepository().harness.get(tx, owner_id="a")


def test_absent_selection_compare_and_set_has_one_winner(migrated_clone):
    db = migrated_clone.database
    barrier = Barrier(2)

    def claim(database):
        with database.transaction() as tx:
            barrier.wait(timeout=30)
            try:
                return PreferencesRepository().harness.compare_and_set(
                    tx,
                    owner_id="a",
                    expected_revision=0,
                    identity=IDENTITY,
                    qualification_digest="b" * 64,
                )
            except RepositoryConflictError:
                return None

    with (
        concurrent_database(migrated_clone) as database,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        results = list(executor.map(lambda _: claim(database), range(2)))
    assert sum(value is not None for value in results) == 1
    with db.transaction() as tx:
        assert PreferencesRepository().harness.get(tx, owner_id="a").revision == 1


def test_retired_owner_and_repository_outage_never_become_default(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        tx.execute(
            "INSERT INTO astralplane_blob_owner_state(owner_id,state,retired_at) "
            "VALUES('retired','retired',clock_timestamp())"
        )
        with pytest.raises(RepositoryConflictError, match="retired"):
            PreferencesRepository().harness.compare_and_set(
                tx,
                owner_id="retired",
                expected_revision=0,
                identity=IDENTITY,
                qualification_digest="b" * 64,
            )
        with (
            pytest.raises(
                (psycopg2.errors.UndefinedTable, sqlite3.OperationalError), match="user_preferences"
            ),
            tx.savepoint("outage"),
        ):
            tx.execute("ALTER TABLE user_preferences RENAME TO unavailable_user_preferences")
            PreferencesRepository().harness.get(tx, owner_id="a")
        assert PreferencesRepository().harness.get(tx, owner_id="a").revision == 0


@pytest.mark.parametrize(
    "document",
    [
        '{"harness_selection":null,"harness_selection":null}',
        "{",
        '{"harness_selection":{"revision":1,"identity":[],"qualification_digest":null}}',
    ],
)
def test_ambiguous_persisted_preference_is_denied(migrated_clone, document):
    with migrated_clone.database.transaction() as tx:
        tx.execute(
            "INSERT INTO user_preferences(user_id,preferences,updated_at) VALUES('a',%s,0)",
            (document,),
        )
        with pytest.raises(RepositoryValidationError):
            PreferencesRepository().harness.get(tx, owner_id="a")


def test_existing_oversized_preferences_are_not_silently_discarded(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        document = json.dumps({"preserved": "x" * 65536})
        tx.execute(
            "INSERT INTO user_preferences(user_id,preferences,updated_at) VALUES('a',%s,0)",
            (document,),
        )
        with pytest.raises(RepositoryValidationError):
            PreferencesRepository().harness.compare_and_set(
                tx,
                owner_id="a",
                expected_revision=0,
                identity=IDENTITY,
                qualification_digest="b" * 64,
            )
        assert (
            tx.fetch_one("SELECT preferences FROM user_preferences WHERE user_id='a'")[
                "preferences"
            ]
            == document
        )
