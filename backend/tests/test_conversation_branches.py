"""Tests for Mac chat conversation branches: fail-closed branch operations on the pinned
AstralPlane revision, and the edit-and-resend / regenerate / fork / branch-switch semantics
plus snapshot branch fields and recovery replay. Branch behavior runs over a
contract-faithful fake of the AstralPlane #113 repository contract, unmerged at the
current pin.
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from astralplane.repositories import RepositoryConflictError, RepositoryNotFoundError
from orchestrator.history import (
    BRANCH_SUMMARY_PREFIX,
    ConversationBranchUnavailable,
    ConversationNotFound,
    ConversationSnapshotInvalid,
    HistoryManager,
    _branch_sibling_fields,
)
from tests.helpers.voice_plane_runtime import history_manager, isolated_voice_plane_runtime


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_voice_plane_runtime("conversation_branches_gate") as runtime:
        yield runtime


@pytest.fixture(scope="module")
def pinned_history(plane_runtime):
    return history_manager(plane_runtime)


@pytest.fixture
def owner():
    return f"branch-{uuid.uuid4().hex[:12]}"


def _linear_chat(hm, owner_id, turns=("hello", "hi there")):
    chat_id = hm.create_chat(user_id=owner_id)
    for role, text in zip(("user", "assistant"), turns):
        hm.add_message(chat_id, role, text, user_id=owner_id)
    return chat_id


def test_branch_operations_fail_closed_on_the_pinned_plane(pinned_history, owner):
    print("DBG SUPPORT:", pinned_history._branch_support, type(pinned_history._history.repository))
    chat_id = _linear_chat(pinned_history, owner)
    assert pinned_history.conversation_branches_supported is False
    with pytest.raises(ConversationBranchUnavailable):
        pinned_history.edit_and_resend(chat_id, owner, 1, "revised")
    with pytest.raises(ConversationBranchUnavailable):
        pinned_history.prepare_regenerate(chat_id, owner, 2)
    with pytest.raises(ConversationBranchUnavailable):
        pinned_history.fork_chat(chat_id, owner, 1)
    with pytest.raises(ConversationBranchUnavailable):
        pinned_history.switch_branch(chat_id, owner, 1)
    assert pinned_history.branch_active_view(chat_id, owner) is None
    chat = pinned_history.get_chat(chat_id, user_id=owner)
    assert [message["content"] for message in chat["messages"]] == ["hello", "hi there"]


def test_pinned_plane_reads_and_writes_stay_linear(pinned_history, owner):
    chat_id = _linear_chat(pinned_history, owner)
    pinned_history.add_message(chat_id, "user", "again", user_id=owner)
    chat = pinned_history.get_chat(chat_id, user_id=owner)
    assert [message["content"] for message in chat["messages"]] == [
        "hello", "hi there", "again",
    ]
    assert pinned_history.get_chat(chat_id, user_id="someone-else") is None


class _FakeBranchConversations:
    def __init__(self):
        self.chats = {}
        self.heads = {}

    def create(self, transaction, *, conversation_id, owner_id, title, agent_id, created_at):
        self.chats[conversation_id] = SimpleNamespace(
            conversation_id=conversation_id,
            owner_id=owner_id,
            title=title,
            agent_id=agent_id,
            created_at=created_at,
            updated_at=created_at,
            render_revision=0,
            publication_id=None,
            has_saved_components=False,
            snapshot_committed_at=None,
            fork_source_conversation_id=None,
            fork_source_message_id=None,
        )
        self.heads[conversation_id] = SimpleNamespace(
            conversation_id=conversation_id,
            owner_id=owner_id,
            head_message_id=None,
            branch_snapshot_id=None,
            version=0,
        )

    def get(self, transaction, *, owner_id, conversation_id, for_update=False):
        chat = self.chats.get(conversation_id)
        if chat is None or chat.owner_id != owner_id:
            return None
        return chat

    def rename(self, transaction, *, owner_id, conversation_id, title,
               expected_updated_at, updated_at):
        chat = self.get(transaction, owner_id=owner_id, conversation_id=conversation_id)
        if chat is None:
            raise RepositoryNotFoundError("conversation was not found")
        if chat.updated_at != expected_updated_at:
            raise RepositoryConflictError("conversation title changed concurrently")
        chat.title = title
        chat.updated_at = updated_at

    def fork(self, transaction, *, owner_id, conversation_id, source_conversation_id,
             source_message_id, title, agent_id, created_at):
        source = self.chats.get(source_conversation_id)
        if source is None or source.owner_id != owner_id:
            raise RepositoryNotFoundError("fork source message was not found")
        if conversation_id == source_conversation_id:
            raise RepositoryConflictError("fork conversation must differ from its source")
        self.chats[conversation_id] = SimpleNamespace(
            conversation_id=conversation_id,
            owner_id=owner_id,
            title=title,
            agent_id=agent_id,
            created_at=created_at,
            updated_at=created_at,
            render_revision=0,
            publication_id=None,
            has_saved_components=False,
            snapshot_committed_at=None,
            fork_source_conversation_id=source_conversation_id,
            fork_source_message_id=source_message_id,
        )
        self.heads.setdefault(conversation_id, SimpleNamespace(
            conversation_id=conversation_id,
            owner_id=owner_id,
            head_message_id=None,
            branch_snapshot_id=None,
            version=0,
        ))

    def branch_head(self, transaction, *, owner_id, conversation_id):
        head = self.heads.get(conversation_id)
        if head is None or head.owner_id != owner_id:
            return None
        return head

    def move_branch_head(self, transaction, *, owner_id, conversation_id, head_message_id,
                         expected_version, branch_snapshot_id=None):
        head = self.heads.get(conversation_id)
        if head is None or head.owner_id != owner_id:
            raise RepositoryNotFoundError("conversation was not found")
        if head.version != expected_version:
            raise RepositoryConflictError("branch head changed since it was read")
        moved = SimpleNamespace(
            conversation_id=head.conversation_id,
            owner_id=head.owner_id,
            head_message_id=head_message_id,
            branch_snapshot_id=branch_snapshot_id,
            version=head.version + 1,
        )
        self.heads[conversation_id] = moved
        return moved


class _FakeBranchMessages:
    def __init__(self, conversations):
        self._conversations = conversations
        self.records = {}
        self._next_id = 1

    def append(self, transaction, *, owner_id, conversation_id, role, content, timestamp,
               parent_message_id=None, **_publication):
        chat = self._conversations.chats.get(conversation_id)
        if chat is None or chat.owner_id != owner_id or chat.render_revision != 0:
            raise RepositoryNotFoundError(
                "conversation is unavailable for legacy message publication"
            )
        if parent_message_id is not None:
            parent = self.records.get(parent_message_id)
            if (
                parent is None
                or parent.owner_id != owner_id
                or parent.conversation_id != conversation_id
            ):
                raise RepositoryNotFoundError("branch parent message was not found")
        record = SimpleNamespace(
            message_id=self._next_id,
            conversation_id=conversation_id,
            owner_id=owner_id,
            role=role,
            content=content,
            timestamp=timestamp,
            parent_message_id=parent_message_id,
        )
        self._next_id += 1
        self.records[record.message_id] = record
        return record

    def get(self, transaction, *, owner_id, conversation_id, message_id):
        record = self.records.get(message_id)
        if (
            record is None
            or record.owner_id != owner_id
            or record.conversation_id != conversation_id
        ):
            return None
        return record

    def list_visible(self, transaction, *, owner_id, conversation_id,
                     through_render_revision=None, limit=200):
        records = [
            record for record in self.records.values()
            if record.conversation_id == conversation_id and record.owner_id == owner_id
        ]
        records.sort(key=lambda record: (record.timestamp, record.message_id))
        return tuple(records[:limit])

    def latest_visible_id(self, transaction, *, owner_id, conversation_id):
        records = self.list_visible(
            transaction, owner_id=owner_id, conversation_id=conversation_id
        )
        return None if not records else records[-1].message_id

    def list_branch_path(self, transaction, *, owner_id, conversation_id, limit=200):
        head = self._conversations.branch_head(
            transaction, owner_id=owner_id, conversation_id=conversation_id
        )
        if head is None or head.head_message_id is None:
            return ()
        chain = []
        cursor = head.head_message_id
        while cursor is not None:
            record = self.records.get(cursor)
            if record is None or record.owner_id != owner_id:
                break
            chain.append(record)
            cursor = record.parent_message_id
        chain.reverse()
        return tuple(chain[:limit])

    def count_message_siblings(self, transaction, *, owner_id, conversation_id, message_id):
        record = self.get(
            transaction, owner_id=owner_id, conversation_id=conversation_id,
            message_id=message_id,
        )
        if record is None:
            raise RepositoryNotFoundError("message was not found")
        return sum(
            1 for other in self.records.values()
            if other.conversation_id == conversation_id
            and other.owner_id == owner_id
            and other.parent_message_id == record.parent_message_id
        )


class _FakeBranchCatalog:
    def __init__(self):
        self.conversations = _FakeBranchConversations()
        self.messages = _FakeBranchMessages(self.conversations)
        self.history = SimpleNamespace(
            conversations=self.conversations, messages=self.messages,
        )
        for name in (
            "workspaces", "artifacts", "conversation_files", "chat_steps", "preferences",
            "projects",
        ):
            setattr(self, name, SimpleNamespace())
        self.projects.project_for_chat = lambda *args, **kwargs: None


class _FakeBranchRuntime:
    def __init__(self):
        self.repositories = _FakeBranchCatalog()
        self.plane_repositories = self.repositories

    @contextmanager
    def transaction(self, isolation=None):
        yield SimpleNamespace()


def branch_history():
    runtime = _FakeBranchRuntime()
    return HistoryManager(plane_runtime=runtime, plane_repositories=runtime.repositories)


def _turn(hm, chat_id, owner_id, role, text):
    hm.add_message(chat_id, role, text, user_id=owner_id)
    return hm.get_latest_message_id(chat_id, owner_id)


def _branched_chat(owner_id):
    hm = branch_history()
    chat_id = str(uuid.uuid4())
    hm.create_chat(chat_id, user_id=owner_id)
    first = _turn(hm, chat_id, owner_id, "user", "What is the launch window?")
    reply = _turn(hm, chat_id, owner_id, "assistant", "The window opens at dusk.")
    return hm, chat_id, first, reply


def test_edit_and_resend_creates_a_sibling_and_keeps_the_original(owner):
    hm, chat_id, first, reply = _branched_chat(owner)
    edited = _turn(hm, chat_id, owner, "user", "What about the backup window?")

    result = hm.edit_and_resend(chat_id, owner, edited, "What is the backup window?")

    replacement = result["message_id"]
    assert replacement != edited
    assert result["revised_message_id"] == edited
    chat = hm.get_chat(chat_id, user_id=owner)
    assert [message["content"] for message in chat["messages"]] == [
        "What is the launch window?",
        "The window opens at dusk.",
        "What is the backup window?",
    ]
    visible = hm._history.call(
        hm._history.repository.messages.list_visible, owner_id=owner, conversation_id=chat_id
    )
    assert {record.content for record in visible} >= {
        "What about the backup window?", "What is the backup window?",
    }
    assert reply in [record.message_id for record in visible]
    head = hm._history.call(
        hm._history.repository.conversations.branch_head, owner_id=owner, conversation_id=chat_id
    )
    assert head.head_message_id == replacement
    assert head.version >= 1


def test_edit_and_resend_refuses_foreign_or_assistant_turns(owner):
    hm, chat_id, first, reply = _branched_chat(owner)
    edited = _turn(hm, chat_id, owner, "user", "What about the backup window?")
    with pytest.raises(ConversationSnapshotInvalid):
        hm.edit_and_resend(chat_id, owner, reply, "not a user turn")
    with pytest.raises(ConversationSnapshotInvalid):
        hm.edit_and_resend(chat_id, owner, 99999, "missing")
    with pytest.raises(ConversationNotFound):
        hm.edit_and_resend(chat_id, "someone-else", edited, "foreign")
    hm.edit_and_resend(chat_id, owner, edited, "still fine")


def test_regenerate_repositions_the_head_to_the_parent(owner):
    hm, chat_id, first, reply = _branched_chat(owner)

    prepared = hm.prepare_regenerate(chat_id, owner, reply)

    assert prepared["head_message_id"] == first
    chat = hm.get_chat(chat_id, user_id=owner)
    assert [message["content"] for message in chat["messages"]] == [
        "What is the launch window?",
    ]
    regenerated = _turn(hm, chat_id, owner, "assistant", "Regenerated answer.")
    assert regenerated != reply
    view = hm.branch_active_view(chat_id, owner)
    assert [record.message_id for record in view["path"]] == [first, regenerated]
    assert len(view["visible"]) == 3


def test_regenerate_refuses_user_turns_missing_replies_and_foreign_owners(owner):
    hm, chat_id, first, reply = _branched_chat(owner)
    with pytest.raises(ConversationSnapshotInvalid):
        hm.prepare_regenerate(chat_id, owner, first)
    with pytest.raises(ConversationSnapshotInvalid):
        hm.prepare_regenerate(chat_id, owner, 99999)
    with pytest.raises(ConversationNotFound):
        hm.prepare_regenerate(chat_id, "someone-else", reply)


def test_fork_copies_the_prefix_records_lineage_and_keeps_the_source(owner):
    hm, chat_id, first, reply = _branched_chat(owner)
    _turn(hm, chat_id, owner, "user", "Never mind, changing course.")

    new_chat_id = hm.fork_chat(chat_id, owner, reply)

    assert new_chat_id != chat_id
    forked = hm.get_chat(new_chat_id, user_id=owner)
    assert [message["content"] for message in forked["messages"]] == [
        "What is the launch window?", "The window opens at dusk.",
    ]
    record = hm.get_conversation_record(new_chat_id, owner)
    assert record.fork_source_conversation_id == chat_id
    assert record.fork_source_message_id == reply
    source = hm.get_chat(chat_id, user_id=owner)
    assert [message["content"] for message in source["messages"]] == [
        "What is the launch window?", "The window opens at dusk.", "Never mind, changing course.",
    ]
    with pytest.raises(ConversationNotFound):
        hm.fork_chat(chat_id, "someone-else", reply)
    with pytest.raises(ConversationSnapshotInvalid):
        hm.fork_chat(chat_id, owner, 99999)


def test_fork_carries_a_marked_branch_summary(owner):
    hm, chat_id, first, reply = _branched_chat(owner)
    _turn(hm, chat_id, owner, "user", "Never mind, changing course.")

    new_chat_id = hm.fork_chat(
        chat_id, owner, reply, summary="You left a plan to change course after dusk.",
    )

    forked = hm.get_chat(new_chat_id, user_id=owner)
    summary_message = forked["messages"][-1]
    assert summary_message["content"].startswith(BRANCH_SUMMARY_PREFIX)
    assert "change course after dusk" in summary_message["content"]


def test_switch_branch_moves_the_head_and_appends_the_summary(owner):
    hm, chat_id, first, reply = _branched_chat(owner)
    parked = _turn(hm, chat_id, owner, "user", "Second question")

    result = hm.switch_branch(
        chat_id, owner, reply,
        summary="The second question is parked on the other branch.",
    )

    assert result["switched_from"] == parked
    chat = hm.get_chat(chat_id, user_id=owner)
    contents = [message["content"] for message in chat["messages"]]
    assert contents[-1].startswith(BRANCH_SUMMARY_PREFIX)
    assert "Second question" not in contents
    head = hm._history.call(
        hm._history.repository.conversations.branch_head, owner_id=owner, conversation_id=chat_id
    )
    assert head.head_message_id == result["head_message_id"]
    with pytest.raises(ConversationNotFound):
        hm.switch_branch(chat_id, "someone-else", first)


def test_switch_branch_replays_exactly_after_a_restart(owner):
    hm, chat_id, first, reply = _branched_chat(owner)
    hm.switch_branch(chat_id, owner, reply)
    expected = [
        message["content"] for message in hm.get_chat(chat_id, user_id=owner)["messages"]
    ]

    revived = HistoryManager(
        plane_runtime=hm._history.plane_runtime,
        plane_repositories=hm._history.plane_runtime.repositories,
    )
    replayed = revived.get_chat(chat_id, user_id=owner)
    assert [message["content"] for message in replayed["messages"]] == expected
    assert revived.get_chat(chat_id, user_id="someone-else") is None


def test_branch_head_conflict_is_refused(owner):
    hm, chat_id, first, reply = _branched_chat(owner)
    head = hm._history.call(
        hm._history.repository.conversations.branch_head, owner_id=owner, conversation_id=chat_id
    )
    with pytest.raises(RepositoryConflictError):
        hm._history.repository.conversations.move_branch_head(
            None, owner_id=owner, conversation_id=chat_id,
            head_message_id=reply, expected_version=head.version + 7,
        )


def test_sibling_fields_follow_the_client_contract(owner):
    records = [
        SimpleNamespace(message_id=1, parent_message_id=None),
        SimpleNamespace(message_id=2, parent_message_id=1),
        SimpleNamespace(message_id=3, parent_message_id=1),
        SimpleNamespace(message_id=4, parent_message_id=3),
    ]
    fields = _branch_sibling_fields("chat-a", records)
    assert set(fields) == {2, 3}
    assert fields[2]["count"] == 2 and fields[2]["index"] == 1
    assert fields[3]["index"] == 2
    assert fields[2]["siblings"] == ["2", "3"]
    assert fields[2]["group_id"] == fields[3]["group_id"]
    assert uuid.UUID(fields[2]["group_id"]).version == 5
    assert 4 not in fields and 1 not in fields


def test_sibling_fields_drop_oversized_groups():
    records = [
        SimpleNamespace(message_id=index, parent_message_id=0)
        for index in range(1, 60)
    ]
    assert _branch_sibling_fields("chat-a", records) == {}


def test_message_ids_are_validated(owner):
    hm, chat_id, first, reply = _branched_chat(owner)
    from orchestrator.history import _positive_message_id

    assert _positive_message_id("42") == 42
    assert _positive_message_id(7) == 7
    for bad in ("0", "-3", "abc", "042", "", None, True, 1.5):
        with pytest.raises(ValueError):
            hm.edit_and_resend(chat_id, owner, bad, "x")


def test_revisioned_conversations_refuse_branch_operations(owner):
    hm = branch_history()
    chat_id = str(uuid.uuid4())
    hm.create_chat(chat_id, user_id=owner)
    hm._history.repository.conversations.chats[chat_id].render_revision = 3
    hm._history.repository.conversations.heads[chat_id].head_message_id = 1
    with pytest.raises(ConversationBranchUnavailable):
        hm.edit_and_resend(chat_id, owner, 1, "x")
    with pytest.raises(ConversationBranchUnavailable):
        hm.prepare_regenerate(chat_id, owner, 1)
    with pytest.raises(ConversationBranchUnavailable):
        hm.fork_chat(chat_id, owner, 1)
    with pytest.raises(ConversationBranchUnavailable):
        hm.switch_branch(chat_id, owner, 1)


def test_chats_without_a_branch_head_refuse_operations(owner):
    hm, chat_id, first, reply = _branched_chat(owner)
    del hm._history.repository.conversations.heads[chat_id]
    with pytest.raises(ConversationBranchUnavailable):
        hm.edit_and_resend(chat_id, owner, first, "x")
    view = hm.branch_active_view(chat_id, owner)
    assert view["head"] is None and view["path"] == ()
    chat = hm.get_chat(chat_id, user_id=owner)
    assert len(chat["messages"]) == 2


def test_switch_branch_refuses_unknown_targets(owner):
    hm, chat_id, first, reply = _branched_chat(owner)
    with pytest.raises(ConversationSnapshotInvalid):
        hm.switch_branch(chat_id, owner, 99999)


class _SnapshotWorkspaces:
    def __init__(self):
        self.canvas = SimpleNamespace(
            list_current=lambda *a, **k: (), list_scoped=lambda *a, **k: (),
        )
        self.layouts = SimpleNamespace(list_current=lambda *a, **k: ())
        self.publications = SimpleNamespace(get=lambda *a, **k: None)


class _SnapshotArtifacts:
    def __init__(self):
        self.message_attachments = SimpleNamespace(
            list_for_message=lambda *a, **k: (),
        )
        self.attachments = SimpleNamespace(get=lambda *a, **k: None)


def snapshot_repository(fake_runtime, uuid_factory=None):
    from orchestrator.history import ConversationCommitRepository

    fake_runtime.repositories.workspaces = _SnapshotWorkspaces()
    fake_runtime.repositories.artifacts = _SnapshotArtifacts()
    return ConversationCommitRepository(
        plane_runtime=fake_runtime,
        plane_repositories=fake_runtime.repositories,
        uuid_factory=uuid_factory or uuid.uuid4,
    )


def test_snapshot_carries_branch_fields_for_capable_clients(owner):
    hm, chat_id, first, reply = _branched_chat(owner)
    edited = _turn(hm, chat_id, owner, "user", "What about the backup window?")
    result = hm.edit_and_resend(chat_id, owner, edited, "What is the backup window?")
    replacement = result["message_id"]

    repository = snapshot_repository(hm._history.plane_runtime)
    snapshot = repository.build_snapshot(
        chat_id=chat_id,
        owner_user_id=owner,
        connection_generation=uuid.uuid4(),
        request_generation=uuid.uuid4(),
        snapshot_purpose="hydration",
        include_branch_fields=True,
    )
    branched = [
        entry for entry in snapshot["transcript"]
        if entry["message_id"] == str(replacement)
    ]
    assert branched and branched[0]["branch"]["count"] == 2
    assert branched[0]["branch"]["siblings"] == [str(edited), str(replacement)]
    assert branched[0]["branch"]["index"] == 2
    head = hm._history.call(
        hm._history.repository.conversations.branch_head, owner_id=owner,
        conversation_id=chat_id,
    )
    head_id = str(head.head_message_id)
    transcript_ids = [entry["message_id"] for entry in snapshot["transcript"]]
    assert transcript_ids[-1] == head_id
    assert str(edited) not in transcript_ids, "the departed turn stays out of the snapshot"


def test_snapshot_without_branch_capability_keeps_the_linear_transcript(owner):
    hm, chat_id, first, reply = _branched_chat(owner)
    repository = snapshot_repository(hm._history.plane_runtime)
    repository._branch_support = False
    snapshot = repository.build_snapshot(
        chat_id=chat_id,
        owner_user_id=owner,
        connection_generation=uuid.uuid4(),
        request_generation=uuid.uuid4(),
        snapshot_purpose="hydration",
    )
    assert all("branch" not in entry for entry in snapshot["transcript"])
    assert [entry["message_id"] for entry in snapshot["transcript"]] == [
        str(first), str(reply),
    ]


def test_snapshot_rejects_non_boolean_branch_flag(owner):
    hm, chat_id, first, reply = _branched_chat(owner)
    repository = snapshot_repository(hm._history.plane_runtime)
    with pytest.raises(ValueError):
        repository.build_snapshot(
            chat_id=chat_id,
            owner_user_id=owner,
            connection_generation=uuid.uuid4(),
            request_generation=uuid.uuid4(),
            snapshot_purpose="hydration",
            include_branch_fields="yes",
        )


def test_snapshot_branch_walk_is_bounded(owner):
    hm, chat_id, first, reply = _branched_chat(owner)
    previous = reply
    timestamps = int(time.time() * 1000)
    for index in range(1000):
        record = hm._history.repository.messages.append(
            None, owner_id=owner, conversation_id=chat_id, role="assistant",
            content=f"spam {index}", timestamp=timestamps + index,
            parent_message_id=previous,
        )
        previous = record.message_id
    head = hm._history.call(
        hm._history.repository.conversations.branch_head, owner_id=owner,
        conversation_id=chat_id,
    )
    hm._history.repository.conversations.move_branch_head(
        None, owner_id=owner, conversation_id=chat_id,
        head_message_id=previous, expected_version=head.version,
    )
    with pytest.raises(ConversationSnapshotInvalid):
        hm._active_head_and_path(None, owner, chat_id)


def test_probe_survives_signature_failures():
    from orchestrator.history import _plane_branch_support

    class _Messages:
        append = len
        list_branch_path = len
        count_message_siblings = len

    repository = SimpleNamespace(
        conversations=SimpleNamespace(
            fork=lambda: None, branch_head=lambda: None, move_branch_head=lambda: None,
        ),
        messages=_Messages(),
    )
    assert _plane_branch_support(repository) is False


def test_commit_repository_advances_the_branch_head_at_publication(owner):
    hm, chat_id, first, reply = _branched_chat(owner)
    repository = snapshot_repository(hm._history.plane_runtime)
    assert repository._branch_support is True

    head = hm._history.call(
        hm._history.repository.conversations.branch_head, owner_id=owner,
        conversation_id=chat_id,
    )
    stale_version = head.version
    repository._advance_branch_head_to_latest(None, owner, chat_id)
    moved = hm._history.call(
        hm._history.repository.conversations.branch_head, owner_id=owner,
        conversation_id=chat_id,
    )
    assert moved.head_message_id == head.head_message_id
    assert moved.version == stale_version

    fresh = _turn(hm, chat_id, owner, "user", "Follow-up turn")
    repository._advance_branch_head_to_latest(None, owner, chat_id)
    moved = hm._history.call(
        hm._history.repository.conversations.branch_head, owner_id=owner,
        conversation_id=chat_id,
    )
    assert moved.head_message_id == fresh
    assert moved.version == stale_version + 1


def test_branch_view_of_a_foreign_chat_is_none(owner):
    hm, chat_id, first, reply = _branched_chat(owner)
    assert hm.branch_active_view(chat_id, "someone-else") is None
