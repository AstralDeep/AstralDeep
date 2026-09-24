"""Tests for scripts/rehearse_backend_web_state.py: synthetic state seed/verify/append,
owner-denial handling, and target-identity validation.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

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
