"""Feature 054 — UserLLMConfigStore / PersistedLLMConfig unit tests.

Successor to the retired feature-006 ``test_session_creds.py`` (the
per-WebSocket in-memory ``SessionCredentialStore`` no longer exists).
Covers the persisted per-user + system store: encrypted-at-rest round
trips, clear semantics, keyless saves, TTL cache behaviour, and the
FR-010 undecryptable-row discard path.
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

import llm_config.user_store as user_store_mod
from llm_config.user_store import (
    _CACHE_TTL_SECONDS,
    _SYSTEM_CACHE_KEY,
    PersistedLLMConfig,
    UserLLMConfigStore,
)


class _Clock:
    """Fake ``time`` module for the store (monotonic + time)."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now


class TestPersistedLLMConfigRepr:
    """__repr__ / __str__ MUST elide the api_key (FR-006)."""

    def test_repr_omits_api_key(self):
        cfg = PersistedLLMConfig(
            provider="openai",
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key="sk-super-secret-key-abc123",
        )
        assert "sk-super-secret-key-abc123" not in repr(cfg)
        assert "<redacted>" in repr(cfg)
        assert "gpt-4o-mini" in repr(cfg)
        assert "api.openai.com" in repr(cfg)

    def test_str_also_omits_api_key(self):
        cfg = PersistedLLMConfig(
            provider="custom", base_url="x", model="y", api_key="sk-leaky")
        assert "sk-leaky" not in str(cfg)

    def test_has_key_property(self):
        assert PersistedLLMConfig("p", "u", "m", "sk-x").has_key is True
        assert PersistedLLMConfig("p", "u", "m", "").has_key is False


class TestSetGetRoundTrip:
    def test_get_missing_returns_none(self, store):
        assert store.get_sync("nobody") is None

    def test_set_get_round_trip_with_encryption_at_rest(
            self, store, fake_db, fernet_key):
        plaintext = "sk-roundtrip-secret-1234567890abcdef"
        store.set_sync(
            "alice",
            provider="openai",
            base_url="https://api.openai.com/v1/",
            model="gpt-4o-mini",
            api_key=plaintext,
        )
        # At rest: ciphertext only, never the plaintext key.
        enc = fake_db.users["alice"]["api_key_enc"]
        assert enc is not None
        assert enc != plaintext
        assert plaintext not in enc
        # ...and it is genuinely Fernet ciphertext under the env key.
        assert Fernet(fernet_key.encode()).decrypt(enc.encode()).decode() == plaintext

        # Force a DB read (bypass the write-through cache) — full round trip.
        store.invalidate("alice")
        got = store.get_sync("alice")
        assert got is not None
        assert got.provider == "openai"
        assert got.base_url == "https://api.openai.com/v1"  # trailing slash stripped
        assert got.model == "gpt-4o-mini"
        assert got.api_key == plaintext
        assert got.updated_at is not None

    def test_set_replaces_prior_entry(self, store, fake_db):
        store.set_sync("u", provider="openai",
                       base_url="https://api.openai.com/v1", model="m1",
                       api_key="sk-first-1234567890")
        store.set_sync("u", provider="custom",
                       base_url="https://other.example/v1", model="m2",
                       api_key="sk-second-1234567890")
        store.invalidate("u")
        got = store.get_sync("u")
        assert got.model == "m2"
        assert got.api_key == "sk-second-1234567890"
        assert len(fake_db.users) == 1

    def test_empty_provider_defaults_to_custom(self, store):
        cfg = store.set_sync("u", provider="",
                             base_url="https://x.example/v1", model="m",
                             api_key="k")
        assert cfg.provider == "custom"


class TestKeylessSave:
    def test_keyless_save_stores_null_ciphertext(self, store, fake_db):
        store.set_sync("u", provider="ollama",
                       base_url="http://localhost:11434/v1", model="llama3",
                       api_key="")
        assert fake_db.users["u"]["api_key_enc"] is None
        store.invalidate("u")
        got = store.get_sync("u")
        assert got.api_key == ""
        assert got.has_key is False


class TestPartialSubmissions:
    """FR: partial records must never be stored (empty base_url/model)."""

    def test_empty_base_url_raises(self, store, fake_db):
        with pytest.raises(ValueError, match="base_url"):
            store.set_sync("u", provider="custom", base_url="",
                           model="m", api_key="k")
        assert fake_db.users == {}

    def test_empty_model_raises(self, store, fake_db):
        with pytest.raises(ValueError, match="model"):
            store.set_sync("u", provider="custom",
                           base_url="https://x/v1", model="  ", api_key="k")
        assert fake_db.users == {}

    def test_system_partial_raises_too(self, store, fake_db):
        with pytest.raises(ValueError):
            store.set_system_sync(provider="custom", base_url="",
                                  model="m", api_key="k", updated_by="admin")
        with pytest.raises(ValueError):
            store.set_system_sync(provider="custom", base_url="https://x/v1",
                                  model="", api_key="k", updated_by="admin")
        assert fake_db.system is None


class TestClear:
    def test_clear_returns_true_when_row_existed(self, store, fake_db):
        store.set_sync("u", provider="openai",
                       base_url="https://api.openai.com/v1", model="m",
                       api_key="k")
        assert store.clear_sync("u") is True
        assert store.get_sync("u") is None
        assert "u" not in fake_db.users

    def test_clear_returns_false_when_absent(self, store):
        assert store.clear_sync("nobody") is False

    def test_second_clear_returns_false(self, store):
        store.set_sync("u", provider="openai",
                       base_url="https://api.openai.com/v1", model="m",
                       api_key="k")
        assert store.clear_sync("u") is True
        assert store.clear_sync("u") is False


class TestCacheInvalidation:
    def test_get_is_cached_between_calls(self, store, fake_db):
        store.set_sync("u", provider="openai",
                       base_url="https://api.openai.com/v1", model="m1",
                       api_key="k")
        # Mutate the DB behind the store's back — a cached read won't see it.
        fake_db.users["u"]["model"] = "sneaky-change"
        assert store.get_sync("u").model == "m1"

    def test_set_invalidates_cache(self, store, fake_db):
        store.set_sync("u", provider="openai",
                       base_url="https://api.openai.com/v1", model="m1",
                       api_key="k")
        fake_db.users["u"]["model"] = "sneaky-change"
        assert store.get_sync("u").model == "m1"  # still cached
        store.set_sync("u", provider="openai",
                       base_url="https://api.openai.com/v1", model="m2",
                       api_key="k")
        # The write-through replaced the stale entry, not left "m1".
        assert store.get_sync("u").model == "m2"

    def test_clear_takes_effect_immediately(self, store, fake_db):
        store.set_sync("u", provider="openai",
                       base_url="https://api.openai.com/v1", model="m",
                       api_key="k")
        store.clear_sync("u")
        # Absence is observed at once (gate transitions are immediate),
        # even if a row sneaks back into the DB out-of-band.
        fake_db.users["u"] = {
            "provider": "openai", "base_url": "https://api.openai.com/v1",
            "model": "m", "api_key_enc": None, "updated_at": 1.0,
        }
        assert store.get_sync("u") is None

    def test_explicit_invalidate_forces_db_read(self, store, fake_db):
        store.set_sync("u", provider="openai",
                       base_url="https://api.openai.com/v1", model="m1",
                       api_key="")
        fake_db.users["u"]["model"] = "fresh-from-db"
        store.invalidate("u")
        assert store.get_sync("u").model == "fresh-from-db"


class TestCacheTTL:
    def test_cached_value_expires_after_ttl(self, monkeypatch, store, fake_db):
        clock = _Clock(1000.0)
        monkeypatch.setattr(user_store_mod, "time", clock)
        store.set_sync("u", provider="openai",
                       base_url="https://api.openai.com/v1", model="m1",
                       api_key="")
        fake_db.users["u"]["model"] = "changed-in-db"
        # Within the TTL: still the cached value.
        clock.now += _CACHE_TTL_SECONDS - 0.5
        assert store.get_sync("u").model == "m1"
        # Past the TTL: the store re-reads the DB.
        clock.now += 1.0
        assert store.get_sync("u").model == "changed-in-db"

    def test_absence_is_cached_with_ttl_too(self, monkeypatch, store, fake_db):
        clock = _Clock(1000.0)
        monkeypatch.setattr(user_store_mod, "time", clock)
        assert store.get_sync("u") is None  # caches the miss
        fake_db.users["u"] = {
            "provider": "openai", "base_url": "https://api.openai.com/v1",
            "model": "m", "api_key_enc": None, "updated_at": 1.0,
        }
        assert store.get_sync("u") is None  # still the cached miss
        clock.now += _CACHE_TTL_SECONDS + 1.0
        assert store.get_sync("u") is not None  # TTL elapsed — fresh read


class TestUndecryptableRow:
    """FR-010: an undecryptable row is discarded, treated as absent,
    and queued for the discarded_undecryptable audit."""

    def _plant_garbage_row(self, fake_db, user_id):
        wrong_key_fernet = Fernet(Fernet.generate_key())
        fake_db.users[user_id] = {
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "api_key_enc": wrong_key_fernet.encrypt(b"sk-old-secret").decode(),
            "updated_at": 123.0,
        }

    def test_undecryptable_user_row_discarded_and_absent(self, store, fake_db):
        self._plant_garbage_row(fake_db, "victim")
        assert store.get_sync("victim") is None
        # The unusable row was deleted (re-gate, no partial state)...
        assert "victim" not in fake_db.users
        # ...and the audit note queued for the orchestrator's drain.
        assert store.pop_discard_note() == ("user", "victim")
        assert store.pop_discard_note() is None

    def test_non_fernet_garbage_also_discarded(self, store, fake_db):
        fake_db.users["victim"] = {
            "provider": "openai", "base_url": "https://api.openai.com/v1",
            "model": "m", "api_key_enc": "not-even-base64!!", "updated_at": 1.0,
        }
        assert store.get_sync("victim") is None
        assert "victim" not in fake_db.users
        assert store.pop_discard_note() == ("user", "victim")

    def test_undecryptable_system_row_discarded(self, store, fake_db):
        wrong = Fernet(Fernet.generate_key())
        fake_db.system = {
            "provider": "openai", "base_url": "https://api.openai.com/v1",
            "model": "m", "api_key_enc": wrong.encrypt(b"sk-sys").decode(),
            "updated_by": "admin", "updated_at": 1.0,
        }
        assert store.get_system_sync() is None
        assert fake_db.system is None
        assert store.pop_discard_note() == ("system", _SYSTEM_CACHE_KEY)

    def test_pop_discard_note_empty_by_default(self, store):
        assert store.pop_discard_note() is None


class TestSystemRecord:
    def test_system_round_trip_including_updated_by(
            self, store, fake_db, fernet_key):
        plaintext = "sk-system-secret-1234567890abcdef"
        store.set_system_sync(
            provider="openai",
            base_url="https://api.openai.com/v1/",
            model="gpt-4o",
            api_key=plaintext,
            updated_by="admin-1",
        )
        assert fake_db.system["updated_by"] == "admin-1"
        enc = fake_db.system["api_key_enc"]
        assert enc != plaintext and plaintext not in enc
        assert Fernet(fernet_key.encode()).decrypt(enc.encode()).decode() == plaintext

        store.invalidate(_SYSTEM_CACHE_KEY)
        got = store.get_system_sync()
        assert got is not None
        assert got.provider == "openai"
        assert got.base_url == "https://api.openai.com/v1"
        assert got.model == "gpt-4o"
        assert got.api_key == plaintext

    def test_system_absent_returns_none(self, store):
        assert store.get_system_sync() is None

    def test_clear_system_true_false(self, store):
        assert store.clear_system_sync() is False
        store.set_system_sync(provider="openai",
                              base_url="https://api.openai.com/v1",
                              model="m", api_key="k", updated_by="admin")
        assert store.clear_system_sync() is True
        assert store.get_system_sync() is None
        assert store.clear_system_sync() is False


class TestAsyncWrappers:
    async def test_async_set_get_clear(self, store):
        cfg = await store.set(
            "u", provider="openai", base_url="https://api.openai.com/v1",
            model="m", api_key="sk-async-1234567890")
        assert cfg.model == "m"
        got = await store.get("u")
        assert got is not None and got.api_key == "sk-async-1234567890"
        assert await store.clear("u") is True
        assert await store.get("u") is None

    async def test_async_system(self, store):
        assert await store.get_system() is None
        await store.set_system(provider="openai",
                               base_url="https://api.openai.com/v1",
                               model="m", api_key="k", updated_by="admin")
        got = await store.get_system()
        assert got is not None
        assert await store.clear_system() is True


class TestNoKeyFileWritten:
    def test_env_key_prevents_key_file_creation(self, fernet_key, fake_db,
                                                tmp_path):
        # With CREDENTIAL_ENCRYPTION_KEY set, the dev key-file fallback
        # must not be touched even when a data_dir is supplied.
        UserLLMConfigStore(
            data_dir=str(tmp_path),
            plane_runtime=fake_db,
            plane_repositories=fake_db.repositories,
        )
        assert not (tmp_path / ".credential_key").exists()
