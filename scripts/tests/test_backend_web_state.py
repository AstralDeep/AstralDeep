"""Tests for scripts/rehearse_backend_web_state.py: synthetic state seed/verify/append,
owner-denial handling, and target-identity validation.
"""

from __future__ import annotations

import json
import sys
import hashlib
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet, InvalidToken

from scripts import rehearse_backend_web_state as state

QUALIFICATION = "a" * 32
OWNER = f"__verif__{QUALIFICATION}_recovery"


def environment():
    return {"DATABASE_URL": f"postgresql://qualification:synthetic@ad-bwq-{QUALIFICATION}-pg/astralplane_qualification_{QUALIFICATION}?options=-csearch_path%3Dastralplane_fixture_{QUALIFICATION}%2Cpg_catalog",
            "ASTRAL_ENV": "production", "USE_MOCK_AUTH": "false",
            "CREDENTIAL_ENCRYPTION_KEY": "synthetic-key", "AUDIT_HMAC_SECRET": "synthetic-audit"}


class Stores:
    def __init__(self):
        self.configs = {}
        self.chats = {}
        self.events = {}
        self.broken_chain = False
        self.audit_leak = False
        self.chat_leak = False
        self.cursor = None
        self._llm_store = SimpleNamespace(get_sync=self.configs.get, set_sync=self.set_config)
        self.history = SimpleNamespace(create_chat=self.create_chat, add_message=self.message,
                                       get_chat=self.get_chat)
        self.audit_repo = SimpleNamespace(insert=self.insert, list_for_user=self.list_events,
                                          get_for_user=self.get_event,
                                          verify_chain=lambda _: "broken" if self.broken_chain else None)
        self._close_started_services = AsyncMock()
        self.attachments = {}
        self.blob_bytes = {}
        self.blob_leak = False
        self.blob_read_leak = False
        self.foreign_blob_error = False
        self.runtime_composition = SimpleNamespace(plane=SimpleNamespace(
            runtime=SimpleNamespace(transaction=nullcontext,
                inspect_baseline_compatibility=lambda: SimpleNamespace(compatible=True, empty=False, observed_revision="089.004"),
                inspect_compatibility=lambda: SimpleNamespace(migration_digest="08b7d824a62284351d1ce178708bb185d7c81f77c96ab14e890f7e5675a008fc")),
            repositories=SimpleNamespace(artifacts=SimpleNamespace(attachments=SimpleNamespace(get=self.get_attachment))),
            attachment_materializations=SimpleNamespace(
                begin_pending_materialization=self.begin_attachment,
                open_pending_materialization_staging=self.open_staging,
                publish_pending_materialization=self.publish_attachment),
            blobs=SimpleNamespace(iter_chunks=self.read_blob),
        ))

    def begin_attachment(self, **values):
        self.pending = values
        return SimpleNamespace(pending=SimpleNamespace(lease_version=0))

    def open_staging(self, **values):
        def write(chunks):
            return SimpleNamespace(content=b"".join(chunks), abort=lambda: None)
        return SimpleNamespace(write_chunks=write)

    def publish_attachment(self, *, staged, **values):
        from astralplane.repositories.artifacts import AttachmentRecord
        pending = self.pending
        row = AttachmentRecord(pending["attachment_id"], pending["owner_id"], pending["filename"],
            values["content_type"], pending["category"], pending["extension"], len(staged.content),
            hashlib.sha256(staged.content).hexdigest(), pending["storage_locator"], pending["created_at"])
        self.attachments[row.attachment_id] = row
        self.blob_bytes[(row.owner_id, pending["storage_key"])] = staged.content
        return row

    def get_attachment(self, _, *, owner_id, attachment_id):
        row = self.attachments.get(attachment_id)
        return row if row and (row.owner_id == owner_id or self.blob_leak) else None

    def read_blob(self, *, owner_id, key, max_bytes, expected_size_bytes, expected_sha256):
        from astralplane.errors import PlaneError
        if self.foreign_blob_error and owner_id != OWNER:
            raise PlaneError("unsafe blob path", code="blob_path_unsafe")
        if self.blob_read_leak:
            owner_id = OWNER
        if (owner_id, key) not in self.blob_bytes:
            raise PlaneError("blob does not exist", code="blob_not_found")
        content = self.blob_bytes[(owner_id, key)]
        if len(content) > max_bytes or len(content) != expected_size_bytes or hashlib.sha256(content).hexdigest() != expected_sha256:
            raise ValueError("blob integrity mismatch")
        return iter((content,))

    def set_config(self, owner, **values):
        self.configs[owner] = SimpleNamespace(**values)

    def create_chat(self, *, user_id):
        chat = str(uuid4())
        self.chats[chat] = (user_id, {"messages": []})
        return chat

    def message(self, chat, role, content, *, user_id):
        assert self.chats[chat][0] == user_id
        self.chats[chat][1]["messages"].append({"role": role, "content": content})

    def get_chat(self, chat, *, user_id):
        owner, value = self.chats.get(chat, (None, None))
        return value if owner == user_id or self.chat_leak else None

    def insert(self, event):
        row = SimpleNamespace(event_id=str(uuid4()))
        self.events.setdefault(event.actor_user_id, []).append(row)
        return row

    def list_events(self, owner, **kwargs):
        return self.events.get(owner, []), self.cursor

    def get_event(self, owner, event_id):
        if self.audit_leak:
            return object()
        return next((row for row in self.events.get(owner, []) if row.event_id == event_id), None)



def test_rehearsal_events_use_distinct_canonical_database_correlations():
    from uuid import UUID

    first = state._event(OWNER, "baseline_created")
    second = state._event(OWNER, "baseline_retained")
    assert str(UUID(first.correlation_id)) == first.correlation_id
    assert str(UUID(second.correlation_id)) == second.correlation_id
    assert first.correlation_id != second.correlation_id
    assert first.action_type == "qualification.baseline_created"
    assert second.action_type == "qualification.baseline_retained"


def test_seed_verify_and_append_keep_keys_out_of_report():
    stores = Stores()
    result = state.exercise(stores, owner=OWNER, checkpoint=None)
    assert result["synthetic_only"] and not result["real_provider_called"]
    assert not result["release_authorized"]
    assert stores.configs[OWNER].api_key not in json.dumps(result)
    verified = state.exercise(stores, owner=OWNER, checkpoint=result["checkpoint"], append=True)
    assert verified["appended_event_id"]
    assert len(stores.events[OWNER]) == 3
    assert state.exercise(stores, owner=OWNER, checkpoint=result["checkpoint"])["credential_decryption"] == "pass"
    with pytest.raises(ValueError, match="already contains"):
        state.exercise(stores, owner=OWNER, checkpoint=None)


@pytest.mark.parametrize("fault,expected", [
    ("key", "decrypted"), ("foreign_key", "foreign credential"),
    ("chat", "conversation changed"), ("foreign_chat", "foreign conversation"),
    ("chain", "authenticated audit"), ("missing_audit", "authenticated audit"),
    ("cursor", "authenticated audit"), ("foreign_audit", "foreign audit"),
    ("owner", "exact synthetic owner"), ("shape", "exact synthetic owner"),
])
def test_loss_and_owner_denials_fail_closed(fault, expected):
    stores = Stores()
    checkpoint = state.exercise(stores, owner=OWNER, checkpoint=None)["checkpoint"]
    if fault == "key":
        stores.configs[OWNER].api_key = "replaced"
    elif fault == "foreign_key":
        stores.configs[OWNER + "_foreign"] = stores.configs[OWNER]
    elif fault == "chat":
        stores.chats.clear()
    elif fault == "foreign_chat":
        stores.chat_leak = True
    elif fault == "chain":
        stores.broken_chain = True
    elif fault == "missing_audit":
        stores.events[OWNER].pop()
    elif fault == "cursor":
        stores.cursor = "another-page"
    elif fault == "foreign_audit":
        stores.audit_leak = True
    elif fault == "owner":
        checkpoint["owner"] = "different"
    else:
        checkpoint["unexpected"] = True
    with pytest.raises(ValueError, match=expected):
        state.exercise(stores, owner=OWNER, checkpoint=checkpoint)


def test_append_must_extend_and_authenticate_same_chain():
    stores = Stores()
    checkpoint = state.exercise(stores, owner=OWNER, checkpoint=None)["checkpoint"]
    stores.audit_repo.insert = lambda event: SimpleNamespace(event_id="not-persisted")
    with pytest.raises(ValueError, match="append"):
        state.exercise(stores, owner=OWNER, checkpoint=checkpoint, append=True)


@pytest.mark.parametrize("fault", ["missing", "corrupt", "metadata", "owner", "foreign", "blob-owner", "foreign-error"])
def test_retained_blob_missing_corrupt_or_cross_owner_fails_closed(fault):
    stores = Stores()
    checkpoint = state.exercise(stores, owner=OWNER, checkpoint=None)["checkpoint"]
    blob = checkpoint["blob"]
    row = stores.attachments[blob["attachment_id"]]
    if fault == "missing":
        stores.blob_bytes.clear()
    elif fault == "corrupt":
        stores.blob_bytes[(OWNER, blob["storage_key"])] = b"corrupt retained bytes"
    elif fault == "metadata":
        stores.attachments.clear()
    elif fault == "owner":
        stores.blob_leak = True
        stores.attachments[row.attachment_id] = replace(row, owner_id=OWNER + "_foreign")
    elif fault == "foreign":
        stores.blob_leak = True
    elif fault == "blob-owner":
        stores.blob_read_leak = True
    else:
        stores.foreign_blob_error = True
    with pytest.raises(ValueError, match="blob"):
        state.exercise(stores, owner=OWNER, checkpoint=checkpoint)


def test_blob_and_pre_append_audit_observations_are_checkpoint_bound():
    stores = Stores()
    seeded = state.exercise(stores, owner=OWNER, checkpoint=None)
    assert seeded["blob_retention"] == "pass"
    blob = seeded["checkpoint"]["blob"]
    assert blob["owner_id"] == OWNER and blob["size_bytes"] > 0
    assert len(blob["sha256"]) == 64
    observed = state.exercise(stores, owner=OWNER, checkpoint=seeded["checkpoint"], append=True)
    assert set(observed["pre_append_audit_event_ids"]) == set(seeded["checkpoint"]["audit_event_ids"])
    assert observed["appended_event_id"] not in observed["pre_append_audit_event_ids"]


def test_candidate_only_event_refuses_recovered_append_before_effect():
    stores = Stores()
    checkpoint = state.exercise(stores, owner=OWNER, checkpoint=None)["checkpoint"]
    appended = state.exercise(stores, owner=OWNER, checkpoint=checkpoint, append=True)["appended_event_id"]
    with pytest.raises(ValueError, match="candidate-only"):
        state.exercise(stores, owner=OWNER, checkpoint=checkpoint, append=True, forbidden_audit_event=appended)
    assert len(stores.events[OWNER]) == 3
    del stores.events[OWNER][-1]
    recovered = state.exercise(stores, owner=OWNER, checkpoint=checkpoint, append=True, forbidden_audit_event=appended)
    assert appended not in recovered["pre_append_audit_event_ids"]


@pytest.mark.parametrize("fault", ["encryption-key", "audit-key", "audit-bytes"])
def test_real_crypto_key_or_authenticated_chain_loss_denies_recovery(monkeypatch, fault):
    from audit.pii import chain_hmac

    stores = Stores()
    cipher = Fernet(Fernet.generate_key())
    ciphertexts = {}
    chain = []
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-recovery-audit-secret")

    def set_config(owner, **values):
        ciphertexts[owner] = (values, cipher.encrypt(values["api_key"].encode()))

    def get_config(owner):
        if owner not in ciphertexts:
            return None
        values, ciphertext = ciphertexts[owner]
        try:
            return SimpleNamespace(**(values | {"api_key": cipher.decrypt(ciphertext).decode()}))
        except InvalidToken:
            return None

    def insert(event):
        row = stores.insert(event)
        previous = chain[-1][2] if chain else bytes(32)
        digest = chain_hmac(previous, row.event_id.encode())[0]
        chain.append((row.event_id, previous, digest))
        return row

    def verify(_):
        previous = bytes(32)
        for identity, observed_previous, digest in chain:
            if observed_previous != previous or chain_hmac(previous, identity.encode())[0] != digest:
                return "authenticated-chain-mismatch"
            previous = digest
        return None

    stores._llm_store = SimpleNamespace(set_sync=set_config, get_sync=get_config)
    stores.audit_repo.insert = insert
    stores.audit_repo.verify_chain = verify
    checkpoint = state.exercise(stores, owner=OWNER, checkpoint=None)["checkpoint"]
    assert ciphertexts[OWNER][1] != ciphertexts[OWNER][0]["api_key"].encode()
    if fault == "encryption-key":
        cipher = Fernet(Fernet.generate_key())
    elif fault == "audit-key":
        monkeypatch.setenv("AUDIT_HMAC_SECRET", "different-synthetic-audit-secret")
    else:
        identity, previous, _ = chain[0]
        chain[0] = (identity, previous, bytes(32))
    with pytest.raises(ValueError, match="decrypted|authenticated audit"):
        state.exercise(stores, owner=OWNER, checkpoint=checkpoint, append=True)
    assert len(stores.events[OWNER]) == 2


@pytest.mark.parametrize("fault", ["not-pending", "publication"])
def test_failed_blob_publication_cannot_emit_successful_checkpoint(fault):
    stores = Stores()
    coordinator = stores.runtime_composition.plane.attachment_materializations
    if fault == "not-pending":
        coordinator.begin_pending_materialization = lambda **values: SimpleNamespace(pending=None)
    else:
        coordinator.publish_pending_materialization = lambda **values: (_ for _ in ()).throw(ValueError("publication failed"))
    with pytest.raises(ValueError):
        state.exercise(stores, owner=OWNER, checkpoint=None)
    assert not stores.attachments and not stores.blob_bytes


@pytest.mark.parametrize("field,value", [("owner_id", "foreign"), ("attachment_id", "bad"), ("storage_key", "../escape"),
    ("size_bytes", True), ("sha256", "f" * 64), ("unexpected", "extra")])
def test_blob_checkpoint_cannot_redirect_owner_identity_path_or_digest(field, value):
    stores = Stores()
    checkpoint = state.exercise(stores, owner=OWNER, checkpoint=None)["checkpoint"]
    checkpoint["blob"][field] = value
    with pytest.raises(ValueError, match="blob"):
        state.exercise(stores, owner=OWNER, checkpoint=checkpoint)


def test_uninitialized_plane_cannot_seed_recovery_state():
    stores = Stores()
    stores.runtime_composition.plane.runtime.inspect_baseline_compatibility = lambda: SimpleNamespace(compatible=False, empty=False)
    with pytest.raises(ValueError, match="not initialized"):
        state.exercise(stores, owner=OWNER, checkpoint=None)
    assert not stores.configs and not stores.events and not stores.blob_bytes


@pytest.mark.parametrize("change", [
    {"DATABASE_URL": "postgresql://live/astral"}, {"ASTRAL_ENV": "development"},
    {"USE_MOCK_AUTH": "true"}, {"CREDENTIAL_ENCRYPTION_KEY": ""}, {"AUDIT_HMAC_SECRET": ""},
])
def test_target_refuses_live_or_mock_or_unkeyed_state(change):
    with pytest.raises(ValueError, match="isolated"):
        state.require_isolated_target(QUALIFICATION, environment() | change)


def test_canonical_target_identity():
    assert state.require_isolated_target(QUALIFICATION, environment()) == OWNER
    with pytest.raises(ValueError, match="ID"):
        state.require_isolated_target("not-canonical", environment())


@pytest.mark.parametrize("query", ["host=live", "options=-csearch_path%3Dpublic", "options=one&options=two", "service=live"])
def test_target_rejects_libpq_redirection_and_changed_search_path(query):
    values = environment()
    values["DATABASE_URL"] = values["DATABASE_URL"].split("?", 1)[0] + "?" + query
    with pytest.raises(ValueError):
        state.require_isolated_target(QUALIFICATION, values)


@pytest.mark.parametrize("content", ['', '{}', '{"checkpoint":{},"checkpoint":{}}', '[]'])
def test_checkpoint_refuses_empty_ambiguous_or_invalid_documents(tmp_path, content):
    path = tmp_path / "checkpoint.json"
    path.write_text(content)
    path.chmod(0o600)
    with pytest.raises(ValueError):
        state._load_checkpoint(path)


def test_cli_cleanup_and_closed_evidence(monkeypatch, tmp_path, capsys):
    for key, value in environment().items():
        monkeypatch.setenv(key, value)
    stores = Stores()
    monkeypatch.setitem(sys.modules, "orchestrator.orchestrator", SimpleNamespace(Orchestrator=lambda: stores))
    output = tmp_path / "seed.json"
    arguments = ["--qualification-id", QUALIFICATION, "--output", str(output)]
    assert state.main(arguments) == 0
    assert stores._close_started_services.await_count == 1
    assert "passed" in capsys.readouterr().out
    verify = tmp_path / "verify.json"
    assert state.main(["--qualification-id", QUALIFICATION, "--checkpoint", str(output),
                       "--output", str(verify), "--append"]) == 0
    assert state.main(arguments) == 1
    stores.broken_chain = True
    assert state.main(["--qualification-id", QUALIFICATION, "--checkpoint", str(output),
                       "--output", str(tmp_path / "failed.json")]) == 1
    assert stores._close_started_services.await_count == 3
    assert not (tmp_path / "failed.json").exists()
