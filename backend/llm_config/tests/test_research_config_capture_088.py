"""Tests for llm_config/research_profile.py's select_config: capture bypasses the
mutable cache, the resulting selection binds exact ciphertext and timestamps, and a
corrupt or foreign row refuses without mutating state.
"""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from types import SimpleNamespace
from contextlib import contextmanager

import pytest
from cryptography.fernet import Fernet

from audit.pii import private_binding_key
from llm_config.research_profile import (
    BASE_URL,
    MODEL,
    ResearchProfileUnavailable,
    select_config,
)
from llm_config.user_store import (
    CapturedUserLLMConfig,
    UserConfigCaptureUnavailable,
    _capture_user_row,
)


@pytest.fixture
def configured(store, monkeypatch):
    monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "research_test")
    monkeypatch.setenv(
        "AUDIT_HMAC_SECRET", "synthetic-config-binding-" + "1234567890abcdef" * 2
    )
    monkeypatch.delenv("AUDIT_HMAC_SECRET_RESEARCH_TEST", raising=False)
    store._repository.plane_runtime.repositories.history = SimpleNamespace(
        sessions=SimpleNamespace(bound_request_execution_waits=Mock(return_value=None))
    )
    store.set_sync(
        "alice",
        provider="openai",
        base_url=BASE_URL,
        model=MODEL,
        api_key="synthetic-provider-key-not-used",
    )
    return store


def select(store, capture=None):
    return select_config(
        capture or store.capture_user_sync("alice"),
        store=store,
        binding_key=private_binding_key(),
    )


def test_capture_bypasses_mutable_cache_without_decrypt_or_cache_write(
    configured, fake_db, monkeypatch
):
    cached = configured.get_sync("alice")
    cached.model = "cache-tampered"
    fake_db.users["alice"]["model"] = MODEL
    original_cache = dict(configured._cache)
    monkeypatch.setattr(
        configured, "_decrypt_key", Mock(side_effect=AssertionError("must not decrypt"))
    )
    capture = configured.capture_user_sync("alice")
    assert capture._record.model == MODEL
    assert capture.owner_id == "alice"
    assert configured._cache == original_cache
    assert configured.get_sync("alice") is cached
    assert "synthetic-provider" not in repr(capture)
    assert fake_db.users["alice"]["api_key_enc"] not in repr(capture)
    with pytest.raises(FrozenInstanceError):
        capture._record = None
    assert configured.capture_user_sync("__system__") is None
    assert configured.capture_user_sync("other-owner") is None


@pytest.mark.asyncio
async def test_async_capture_stays_uncached(configured, fake_db):
    before = await configured.capture_user("alice")
    fake_db.users["alice"]["updated_at"] += timedelta(microseconds=1)
    after = await configured.capture_user("alice")
    assert not before.matches(after._record)
    assert before.matches(before._record)
    assert not before.matches(None)
    assert not before.matches(object())


def test_selection_exact_revision_and_private_values(configured):
    capture = configured.capture_user_sync("alice")
    selection = select(configured, capture)
    assert selection.owner_id == "alice"
    assert selection.matches(capture._record)
    assert selection == select(configured, capture)
    assert len(selection.revision) == 64
    assert "synthetic-provider-key" not in repr(selection)
    assert capture._record.api_key_ciphertext not in repr(selection)
    assert selection._api_key == "synthetic-provider-key-not-used"


@pytest.mark.parametrize(
    "field",
    [
        "owner_id",
        "provider",
        "base_url",
        "model",
        "api_key_ciphertext",
        "created_at",
        "updated_at",
        "updated_by",
        "scope",
    ],
)
def test_every_raw_field_changes_row_comparison(configured, field):
    capture = configured.capture_user_sync("alice")
    value = getattr(capture._record, field)
    replacement = (
        value + timedelta(microseconds=1)
        if isinstance(value, datetime)
        else str(value) + "changed"
    )
    assert not capture.matches(replace(capture._record, **{field: replacement}))


@pytest.mark.parametrize(
    "field", ["api_key_ciphertext", "created_at", "updated_at", "owner_id"]
)
def test_revision_mac_binds_raw_ciphertext_and_exact_microseconds(configured, field):
    before = select(configured)
    row = before._capture._record
    if field == "api_key_ciphertext":
        value = configured._encrypt_key(
            before._api_key
        )
    elif field == "owner_id":
        value = "bob"
    else:
        value = (
            getattr(row, field) - timedelta(microseconds=1)
            if field == "created_at"
            else getattr(row, field) + timedelta(microseconds=1)
        )
    changed = _capture_user_row(
        replace(row, **{field: value}), value if field == "owner_id" else "alice"
    )
    assert select(configured, changed).revision != before.revision


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "OPENAI"),
        ("provider", "custom"),
        ("base_url", BASE_URL + "/"),
        ("base_url", "http://127.0.0.1/v1"),
        ("model", "gpt-4o-mini"),
        ("model", MODEL + " "),
    ],
)
def test_exact_profile_refuses_alias_or_fallback(configured, fake_db, field, value):
    fake_db.users["alice"][field] = value
    with pytest.raises(ResearchProfileUnavailable):
        select(configured)


@pytest.mark.parametrize(
    "key", ["", "not-needed", " key", "key ", "line\nbreak", "é", "x" * 8193]
)
def test_keyless_or_unusable_provider_secret_refuses(configured, fake_db, key):
    fake_db.users["alice"]["api_key_enc"] = configured._encrypt_key(key)
    with pytest.raises(ResearchProfileUnavailable):
        select(configured)


def test_corrupt_or_rotated_ciphertext_never_deletes_or_changes_cache(
    configured, fake_db, monkeypatch
):
    original_cache = dict(configured._cache)
    for ciphertext in (
        "not-fernet",
        Fernet(Fernet.generate_key()).encrypt(b"synthetic").decode(),
    ):
        fake_db.users["alice"]["api_key_enc"] = ciphertext
        with pytest.raises(
            UserConfigCaptureUnavailable, match="^user_config_capture_unavailable$"
        ):
            select(configured)
        assert fake_db.users["alice"]["api_key_enc"] == ciphertext
        assert configured._cache == original_cache
    with pytest.raises(UserConfigCaptureUnavailable):
        configured.open_captured_user_key(object())
    with pytest.raises(ResearchProfileUnavailable):
        select_config(None, store=configured, binding_key=private_binding_key())
    with pytest.raises(ResearchProfileUnavailable):
        select_config(
            configured.capture_user_sync("alice"), store=configured, binding_key=None
        )


@pytest.mark.parametrize("owner", [None, True, "", " ", "a" * 2049, "\ud800"])
def test_invalid_owner_refuses_before_repository_read(configured, monkeypatch, owner):
    read = Mock(side_effect=AssertionError("repository must not run"))
    monkeypatch.setattr(configured._repository.repository, "get_user", read)
    with pytest.raises(UserConfigCaptureUnavailable):
        configured.capture_user_sync(owner)
    read.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": "system"},
        {"owner_id": "other"},
        {"updated_by": "admin"},
        {"created_at": datetime(2026, 1, 1)},
        {"updated_at": datetime(2020, 1, 1, tzinfo=UTC)},
        {"provider": None},
        {"base_url": True},
        {"model": "x" * 32769},
        {"api_key_ciphertext": "\ud800"},
    ],
)
def test_malformed_repository_row_refuses_data_free(configured, monkeypatch, changes):
    row = configured.capture_user_sync("alice")._record
    monkeypatch.setattr(
        configured._repository.repository,
        "get_user",
        lambda *a, **k: replace(row, **changes),
    )
    with pytest.raises(
        UserConfigCaptureUnavailable, match="^user_config_capture_unavailable$"
    ):
        configured.capture_user_sync("alice")


def test_no_duck_typed_foreign_row(configured, monkeypatch):
    monkeypatch.setattr(
        configured._repository.repository, "get_user", lambda *a, **k: object()
    )
    with pytest.raises(UserConfigCaptureUnavailable):
        configured.capture_user_sync("alice")
    assert isinstance(CapturedUserLLMConfig, type)


def test_cap_applies_before_read_in_same_transaction_and_failure_closes(
    configured, monkeypatch
):
    events, transaction = [], object()
    runtime = configured._repository.plane_runtime
    row = configured.capture_user_sync("alice")._record

    @contextmanager
    def scope():
        events.append("enter")
        try:
            yield transaction
        finally:
            events.append("exit")

    def bound(actual):
        assert actual is transaction
        events.append("cap")

    def read(actual, *, owner_id):
        assert actual is transaction and owner_id == "alice"
        events.append("read")
        return row

    monkeypatch.setattr(runtime, "transaction", scope)
    monkeypatch.setattr(
        runtime.repositories.history.sessions, "bound_request_execution_waits", bound
    )
    monkeypatch.setattr(configured._repository.repository, "get_user", read)
    assert configured.capture_user_sync("alice").matches(row)
    assert events == ["enter", "cap", "read", "exit"]


@pytest.mark.parametrize(
    "mode", ["missing", "invalid", "raises", "read_raises", "entry_raises"]
)
def test_cap_or_repository_failure_is_closed_and_never_uses_fallback(
    configured, monkeypatch, mode
):
    runtime = configured._repository.plane_runtime
    sessions = runtime.repositories.history.sessions
    original = dict(configured._cache)
    read = Mock(side_effect=RuntimeError("synthetic-private-row-detail"))
    monkeypatch.setattr(configured._repository.repository, "get_user", read)
    if mode == "missing":
        monkeypatch.delattr(sessions, "bound_request_execution_waits")
    elif mode == "invalid":
        monkeypatch.setattr(sessions, "bound_request_execution_waits", None)
    elif mode == "raises":
        monkeypatch.setattr(
            sessions,
            "bound_request_execution_waits",
            Mock(side_effect=RuntimeError("synthetic-private-cap")),
        )
    elif mode == "entry_raises":
        monkeypatch.setattr(
            runtime,
            "transaction",
            Mock(side_effect=RuntimeError("synthetic-private-entry")),
        )
    with pytest.raises(
        UserConfigCaptureUnavailable, match="^user_config_capture_unavailable$"
    ) as error:
        configured.capture_user_sync("alice")
    assert error.value.__suppress_context__
    assert configured._cache == original
    if mode != "read_raises":
        read.assert_not_called()
