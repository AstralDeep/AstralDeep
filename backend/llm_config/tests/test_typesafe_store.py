"""Feature 089 (T009): the TypeSafe credential facade.

The store's job is narrow and its failure modes matter more than its happy
path, so most of these tests are about what happens when something is wrong:
an undecryptable row, a durable read failure mid-turn, an outcome that arrives
after the user replaced their key.

Every key value here is synthetic.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from astralplane.repositories import RepositoryError
from cryptography.fernet import Fernet

from llm_config.typesafe_store import (
    MAX_KEY_CHARS,
    StoredTypeSafeKey,
    TypeSafeCredentialStore,
    TypeSafeKeyStatus,
    key_fingerprint,
)

KEY = "ts_live_CANARY0000NOTAREALKEY000000"
OTHER_KEY = "ts_live_CANARY1111NOTAREALKEY111111"
USER = "user-089"
OTHER_USER = "user-089-other"


def _run(coro):
    return asyncio.run(coro)


# -- fingerprint ---------------------------------------------------------


def test_fingerprint_is_twelve_lowercase_hex_characters() -> None:
    fingerprint = key_fingerprint(KEY)
    assert len(fingerprint) == 12
    assert fingerprint == fingerprint.lower()
    int(fingerprint, 16)


def test_fingerprint_is_stable_and_distinguishes_keys() -> None:
    assert key_fingerprint(KEY) == key_fingerprint(KEY)
    assert key_fingerprint(KEY) != key_fingerprint(OTHER_KEY)


def test_fingerprint_does_not_contain_the_key() -> None:
    assert KEY[:8] not in key_fingerprint(KEY)


# -- status --------------------------------------------------------------


def test_unset_status_for_a_user_with_no_row(typesafe_store) -> None:
    status = typesafe_store.status_sync(USER)
    assert status == TypeSafeKeyStatus()
    assert status.name == "not_set"
    assert status.is_set is False
    assert status.is_usable is False


def test_status_after_save_is_active(typesafe_store) -> None:
    typesafe_store.save_sync(USER, KEY)
    status = typesafe_store.status_sync(USER)
    assert status.name == "active"
    assert status.is_set is True
    assert status.is_usable is True
    assert status.last_verified_at is not None


def test_a_rejected_key_is_not_usable_but_an_unavailable_one_is(typesafe_store) -> None:
    typesafe_store.save_sync(USER, KEY)
    fingerprint = key_fingerprint(KEY)

    typesafe_store.record_outcome_sync(USER, "rejected", fingerprint)
    rejected = typesafe_store.status_sync(USER)
    assert rejected.name == "rejected"
    assert rejected.is_usable is False

    typesafe_store.record_outcome_sync(USER, "unavailable", fingerprint)
    unavailable = typesafe_store.status_sync(USER)
    assert unavailable.name == "unavailable"
    # Unavailable describes TypeSafe, not the credential: the circuit breaker
    # is what stops retries, not the status.
    assert unavailable.is_usable is True


def test_status_carries_no_key_material(typesafe_store) -> None:
    typesafe_store.save_sync(USER, KEY)
    rendered = repr(typesafe_store.status_sync(USER))
    assert KEY not in rendered
    assert key_fingerprint(KEY) not in rendered


# -- save / get ----------------------------------------------------------


def test_round_trip_returns_the_key_and_its_fingerprint(typesafe_store) -> None:
    typesafe_store.save_sync(USER, KEY)
    stored = typesafe_store.get_key_sync(USER)
    assert isinstance(stored, StoredTypeSafeKey)
    assert stored.api_key == KEY
    assert stored.fingerprint == key_fingerprint(KEY)


def test_the_key_is_encrypted_at_rest(typesafe_store, credential_plane) -> None:
    typesafe_store.save_sync(USER, KEY)
    row = credential_plane.typesafe[USER]
    assert KEY not in row["api_key_enc"]
    assert row["api_key_enc"].startswith("gAAAA")


def test_stored_key_repr_hides_the_key() -> None:
    stored = StoredTypeSafeKey(api_key=KEY, fingerprint="0123456789ab")
    assert KEY not in repr(stored)
    assert "0123456789ab" in repr(stored)


def test_save_is_owner_scoped(typesafe_store) -> None:
    typesafe_store.save_sync(USER, KEY)
    assert typesafe_store.get_key_sync(OTHER_USER) is None


def test_saving_again_replaces_the_key_and_clears_rejection(typesafe_store) -> None:
    typesafe_store.save_sync(USER, KEY)
    typesafe_store.record_outcome_sync(USER, "rejected", key_fingerprint(KEY))
    assert typesafe_store.status_sync(USER).name == "rejected"

    typesafe_store.save_sync(USER, OTHER_KEY)

    assert typesafe_store.get_key_sync(USER).api_key == OTHER_KEY
    assert typesafe_store.status_sync(USER).name == "active"


@pytest.mark.parametrize("bad", ["", "   ", None, 7, b"bytes"])
def test_an_empty_or_non_string_key_is_refused(typesafe_store, bad: object) -> None:
    with pytest.raises(ValueError):
        typesafe_store.save_sync(USER, bad)


def test_an_oversized_key_is_refused(typesafe_store) -> None:
    with pytest.raises(ValueError):
        typesafe_store.save_sync(USER, "t" * (MAX_KEY_CHARS + 1))


def test_surrounding_whitespace_is_stripped_before_storage(typesafe_store) -> None:
    typesafe_store.save_sync(USER, "  " + KEY + "\n")
    stored = typesafe_store.get_key_sync(USER)
    assert stored.api_key == KEY
    assert stored.fingerprint == key_fingerprint(KEY)


# -- clear ---------------------------------------------------------------


def test_clear_removes_the_key_and_is_idempotent(typesafe_store) -> None:
    typesafe_store.save_sync(USER, KEY)
    assert typesafe_store.clear_sync(USER) is True
    assert typesafe_store.get_key_sync(USER) is None
    assert typesafe_store.status_sync(USER).name == "not_set"
    # A second Remove on an already-clean page is a no-op, not an error.
    assert typesafe_store.clear_sync(USER) is False


def test_clear_is_owner_scoped(typesafe_store) -> None:
    typesafe_store.save_sync(USER, KEY)
    typesafe_store.save_sync(OTHER_USER, OTHER_KEY)
    typesafe_store.clear_sync(USER)
    assert typesafe_store.get_key_sync(OTHER_USER).api_key == OTHER_KEY


# -- outcome recording ---------------------------------------------------


def test_an_outcome_on_a_replaced_key_updates_nothing(typesafe_store) -> None:
    typesafe_store.save_sync(USER, KEY)
    stale_fingerprint = key_fingerprint(KEY)
    typesafe_store.save_sync(USER, OTHER_KEY)

    applied = typesafe_store.record_outcome_sync(USER, "rejected", stale_fingerprint)

    assert applied is False
    assert typesafe_store.status_sync(USER).name == "active"


def test_an_outcome_on_the_current_key_applies(typesafe_store) -> None:
    typesafe_store.save_sync(USER, KEY)
    applied = typesafe_store.record_outcome_sync(USER, "rejected", key_fingerprint(KEY))
    assert applied is True
    assert typesafe_store.status_sync(USER).name == "rejected"


def test_recording_an_outcome_for_a_user_with_no_row_is_harmless(typesafe_store) -> None:
    assert typesafe_store.record_outcome_sync(USER, "valid", key_fingerprint(KEY)) is False


def test_outcome_recording_never_raises(typesafe_store, monkeypatch) -> None:
    typesafe_store.save_sync(USER, KEY)

    def _explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("durable store is on fire")

    monkeypatch.setattr(typesafe_store._repository.repository, "record_outcome", _explode)

    # Bookkeeping must never be able to take a turn down.
    assert typesafe_store.record_outcome_sync(USER, "valid", key_fingerprint(KEY)) is False


# -- undecryptable rows --------------------------------------------------


def test_an_undecryptable_row_is_discarded_and_reads_as_absent(
    typesafe_store, credential_plane, monkeypatch
) -> None:
    typesafe_store.save_sync(USER, KEY)
    # Simulate a rotated encryption key: the ciphertext no longer opens.
    monkeypatch.setattr(typesafe_store, "_fernet", Fernet(Fernet.generate_key()))
    typesafe_store.invalidate(USER)

    assert typesafe_store.get_key_sync(USER) is None
    assert USER not in credential_plane.typesafe
    assert typesafe_store.pop_discard_note() == USER
    assert typesafe_store.pop_discard_note() is None


def test_a_discarded_row_leaves_other_users_alone(
    typesafe_store, credential_plane, monkeypatch
) -> None:
    typesafe_store.save_sync(USER, KEY)
    typesafe_store.save_sync(OTHER_USER, OTHER_KEY)
    monkeypatch.setattr(typesafe_store, "_fernet", Fernet(Fernet.generate_key()))
    typesafe_store.invalidate(USER)

    typesafe_store.get_key_sync(USER)

    assert USER not in credential_plane.typesafe
    assert OTHER_USER in credential_plane.typesafe


# -- durable failure -----------------------------------------------------


def test_a_repository_read_failure_is_treated_as_no_key(typesafe_store, monkeypatch) -> None:
    def _explode(*args: object, **kwargs: object) -> None:
        raise RepositoryError("plane is unreachable")

    monkeypatch.setattr(typesafe_store._repository.repository, "get_user", _explode)

    # A turn must fall back to standard routing, not fail.
    assert typesafe_store.get_key_sync(USER) is None
    assert typesafe_store.status_sync(USER).name == "not_set"


# -- cache ---------------------------------------------------------------


def test_reads_are_cached_and_writes_invalidate(typesafe_store, credential_plane) -> None:
    typesafe_store.save_sync(USER, KEY)
    assert typesafe_store.get_key_sync(USER).api_key == KEY

    # Mutate behind the store's back: a cached read still sees the old value.
    credential_plane.typesafe.pop(USER)
    assert typesafe_store.get_key_sync(USER) is not None

    typesafe_store.invalidate(USER)
    assert typesafe_store.get_key_sync(USER) is None


def test_save_invalidates_the_cache_immediately(typesafe_store) -> None:
    assert typesafe_store.status_sync(USER).name == "not_set"  # caches the miss
    typesafe_store.save_sync(USER, KEY)
    assert typesafe_store.status_sync(USER).name == "active"


def test_clear_invalidates_the_cache_immediately(typesafe_store) -> None:
    typesafe_store.save_sync(USER, KEY)
    assert typesafe_store.status_sync(USER).name == "active"
    typesafe_store.clear_sync(USER)
    assert typesafe_store.status_sync(USER).name == "not_set"


# -- async wrappers ------------------------------------------------------


def test_the_async_wrappers_mirror_the_sync_core(typesafe_store) -> None:
    async def _scenario() -> None:
        await typesafe_store.save(USER, KEY)
        stored = await typesafe_store.get_key(USER)
        assert stored.api_key == KEY
        status = await typesafe_store.status(USER)
        assert status.name == "active"
        assert await typesafe_store.record_outcome_async(
            USER, "rejected", stored.fingerprint
        )
        assert (await typesafe_store.status(USER)).name == "rejected"
        assert await typesafe_store.clear(USER) is True
        assert await typesafe_store.get_key(USER) is None

    _run(_scenario())


# -- isolation from the LLM gate ----------------------------------------


def test_the_typesafe_store_shares_the_llm_encryption_key(
    typesafe_store, store, fernet_key
) -> None:
    """One credential key covers both stores, so one rotation covers both."""
    assert typesafe_store._fernet._signing_key == store._fernet._signing_key


def test_saving_a_typesafe_key_creates_no_llm_configuration(
    typesafe_store, credential_plane
) -> None:
    typesafe_store.save_sync(USER, KEY)
    assert credential_plane.users == {}
    assert credential_plane.system is None


def test_clearing_an_llm_configuration_leaves_the_typesafe_key(
    typesafe_store, store, credential_plane
) -> None:
    store.set_sync(
        USER,
        provider="custom",
        base_url="https://models.invalid/v1",
        model="m",
        api_key="sk-000000000000000000000000",
    )
    typesafe_store.save_sync(USER, KEY)

    store.clear_sync(USER)

    assert credential_plane.users == {}
    assert typesafe_store.get_key_sync(USER).api_key == KEY


def test_clearing_the_typesafe_key_leaves_the_llm_configuration(
    typesafe_store, store, credential_plane
) -> None:
    store.set_sync(
        USER,
        provider="custom",
        base_url="https://models.invalid/v1",
        model="m",
        api_key="sk-000000000000000000000000",
    )
    typesafe_store.save_sync(USER, KEY)

    typesafe_store.clear_sync(USER)

    assert store.get_sync(USER) is not None
    assert typesafe_store.get_key_sync(USER) is None


def test_the_store_exposes_no_system_scope() -> None:
    """FR-005: a deployment-wide TypeSafe key must not be representable."""
    names = dir(TypeSafeCredentialStore)
    assert not [name for name in names if "system" in name.lower()]


def test_recent_outcome_timestamps_are_timezone_aware(typesafe_store) -> None:
    typesafe_store.save_sync(USER, KEY)
    status = typesafe_store.status_sync(USER)
    assert status.at is not None and status.at.tzinfo is not None
    assert datetime.now(UTC) - status.at < timedelta(minutes=5)
