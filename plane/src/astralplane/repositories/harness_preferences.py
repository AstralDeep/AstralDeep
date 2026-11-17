"""Owner-locked harness preferences use an independent CAS revision in the existing
preferences document.
Admission retains the same lock while its immutable capture and ordinary authority checks commit.
"""

from __future__ import annotations

import json

from astralplane.repositories import RepositoryConflictError, RepositoryValidationError
from astralplane.repositories.harness_bindings import HarnessProfileIdentity
from astralplane.repositories.harness_capture import (
    HarnessCapture,
    HarnessSelectionRecord,
    _integer,
    _text,
    harness_capture_from_value,
)

_KEY = "harness_selection"


def lock_harness_owner(transaction, owner_id):
    _text(owner_id, 512)
    transaction.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (owner_id,))
    row = transaction.fetch_one(
        "SELECT state FROM astralplane_blob_owner_state WHERE owner_id=%s FOR UPDATE", (owner_id,)
    )
    if row is not None and row["state"] != "active":
        raise RepositoryConflictError("harness owner is retired")


def _document(row):
    value = {} if row is None else row["preferences"]
    if type(value) is str:

        def pairs(items):
            result = {}
            for key, item in items:
                if key in result:
                    raise ValueError
                result[key] = item
            return result

        try:
            value = json.loads(value, object_pairs_hook=pairs)
        except (ValueError, TypeError, RecursionError):
            raise RepositoryValidationError("harness preferences document is invalid") from None
    if type(value) is not dict:
        raise RepositoryValidationError("harness preferences document is invalid")
    return dict(value)


def _record(document, owner_id):
    if _KEY not in document:
        return HarnessSelectionRecord(owner_id, 0, None, None)
    value = document[_KEY]
    try:
        if type(value) is not dict or set(value) != {
            "revision",
            "identity",
            "qualification_digest",
        }:
            raise ValueError
        identity = value["identity"]
        if identity is not None:
            if type(identity) is not dict:
                raise ValueError
            identity = HarnessProfileIdentity(**identity)
        _integer(value["revision"], 1)
        return HarnessSelectionRecord(
            owner_id, value["revision"], identity, value["qualification_digest"]
        )
    except (KeyError, ValueError, TypeError):
        raise RepositoryValidationError("stored harness selection is invalid") from None


class HarnessPreferenceRepository:
    _SELECT = "SELECT preferences FROM user_preferences WHERE user_id=%s"

    def lock_owner(self, transaction, *, owner_id):
        lock_harness_owner(transaction, owner_id)

    def get(self, query, *, owner_id):
        _text(owner_id, 512)
        return _record(_document(query.fetch_one(self._SELECT, (owner_id,))), owner_id)

    def assert_current(self, transaction, *, owner_id, expected):
        if type(expected) is not HarnessSelectionRecord or expected.owner_id != owner_id:
            raise RepositoryConflictError("harness selection owner changed")
        lock_harness_owner(transaction, owner_id)
        current = _record(
            _document(transaction.fetch_one(self._SELECT + " FOR UPDATE", (owner_id,))), owner_id
        )
        if current != expected:
            raise RepositoryConflictError("harness selection revision changed")

    def compare_and_set(
        self, transaction, *, owner_id, expected_revision, identity, qualification_digest
    ):
        _integer(expected_revision)
        desired = HarnessSelectionRecord(
            owner_id, expected_revision + 1, identity, qualification_digest
        )
        lock_harness_owner(transaction, owner_id)
        document = _document(transaction.fetch_one(self._SELECT + " FOR UPDATE", (owner_id,)))
        current = _record(document, owner_id)
        if current.revision != expected_revision:
            raise RepositoryConflictError("harness selection revision changed")
        transaction.execute(
            "INSERT INTO user_preferences(user_id,preferences,updated_at) VALUES(%s,'{}',0) "
            "ON CONFLICT(user_id) DO NOTHING",
            (owner_id,),
        )
        locked = transaction.fetch_one(self._SELECT + " FOR UPDATE", (owner_id,))
        document = _document(locked)
        if _record(document, owner_id) != current:
            raise RepositoryConflictError("harness selection revision changed")
        document[_KEY] = {
            "revision": desired.revision,
            "identity": None
            if identity is None
            else {
                "profile": identity.profile,
                "version": identity.version,
                "artifact_digest": identity.artifact_digest,
            },
            "qualification_digest": qualification_digest,
        }
        encoded = json.dumps(document, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode()) > 65536:
            raise RepositoryValidationError("harness preferences document exceeds its bound")
        result = transaction.execute(
            "UPDATE user_preferences SET preferences=%s, updated_at=GREATEST("
            "COALESCE(updated_at,0)+1,FLOOR(EXTRACT(EPOCH FROM clock_timestamp())*1000)::BIGINT) "
            "WHERE user_id=%s AND preferences=%s",
            (encoded, owner_id, locked["preferences"]),
        )
        if result.rowcount != 1:
            raise RepositoryConflictError("harness preferences update lost its owner")
        return desired


def assert_harness_admission(transaction, *, owner_id, selection, capture, assignment_id=None):
    if capture is not None and (
        type(capture) is not HarnessCapture or capture.owner_id != owner_id
    ):
        raise RepositoryValidationError("harness capture owner is invalid")
    if selection is None:
        if capture is None and assignment_id is None:
            return
        if assignment_id is None:
            raise RepositoryValidationError("harness capture requires its admission selection")
        lock_harness_owner(transaction, owner_id)
        row = transaction.fetch_one(
            "SELECT data FROM persistent_assignment WHERE owner_user_id=%s AND id=%s FOR UPDATE",
            (owner_id, assignment_id),
        )
        if row is None or row["data"].get("harness_capture") is None:
            raise RepositoryConflictError("assignment harness capture is unavailable")
        if harness_capture_from_value(row["data"]["harness_capture"]) != capture:
            raise RepositoryConflictError("assignment harness capture changed")
        return
    if type(selection) is not HarnessSelectionRecord or selection.owner_id != owner_id:
        raise RepositoryValidationError("harness selection owner is invalid")
    if assignment_id is not None:
        raise RepositoryValidationError("harness selection and inherited capture are exclusive")
    if selection.identity is None:
        if capture is not None:
            raise RepositoryValidationError("unselected work cannot carry a harness capture")
    elif (
        capture is None
        or capture.selection_revision != selection.revision
        or capture.identity != selection.identity
        or capture.qualification_digest != selection.qualification_digest
    ):
        raise RepositoryValidationError("harness capture does not match admission selection")
    HarnessPreferenceRepository().assert_current(transaction, owner_id=owner_id, expected=selection)
