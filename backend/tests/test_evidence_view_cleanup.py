"""Verifies temporary-view cleanup removes plaintext and records content-free public audit receipts.
Signed owner fixtures exercise lazy removal, bounded audit retries, source revocation, and shutdown.
"""

import asyncio
from datetime import timedelta
import hashlib
import json
import re

import pytest

from orchestrator.context_views import ContextViewStore, ViewDenied, ViewUnavailable
from tests.test_evidence_service import (
    AGENT, TOOL, bound as bound, events, evidence_turn, fixture as fixture, human as human,
    invoke, pack, runtime as runtime, service as service, signing_key as signing_key,
)

pytestmark = pytest.mark.asyncio
_VIEW_ACTIONS = {"evidence.view_expiry_cleanup", "evidence.view_revocation_cleanup",
                 "evidence.view_shutdown_cleanup"}


async def retained_views(state, count=2):
    packed = await pack(state)
    messages = [{"role": "tool", "content": json.dumps(packed.result)}]
    views = []
    for index in range(count):
        text = f"Synthetic temporary cleanup interpretation {index}; omitted detail remains possible."
        reference = await state.evidence.summary_view(text, messages, websocket=state.socket,
            owner=state.owner, chat=state.chat)
        assert reference is not None
        views.append((reference, text))
    records = await events(state)
    captures = {}
    for reference, _ in views:
        view = state.evidence.views.inspect(reference, owner_id=state.owner,
            conversation_id=state.chat, audience_id=f"user:{state.owner}")
        digest = hashlib.sha256(json.dumps(reference, separators=(",", ":")).encode()).hexdigest()
        captures[digest] = (view.size_bytes, view.integrity_identity)
    assert {record.inputs_meta["view_digest"] for record in records
            if record.action_type == "evidence.view_capture"} == set(captures)
    assert len(captures) == count and state.evidence.views.view_count == count
    assert state.evidence.views.retained_bytes == sum(len(text.encode()) for _, text in views)
    return views, captures


def view_receipts(records):
    return [record for record in records if record.action_type in _VIEW_ACTIONS]


def assert_plaintext_unavailable(state, views):
    assert state.evidence.views.retained_bytes == 0 and state.evidence.views.view_count == 0
    for reference, _ in views:
        with pytest.raises(ViewUnavailable):
            state.evidence.views.inspect(reference, owner_id=state.owner,
                conversation_id=state.chat, audience_id=f"user:{state.owner}")


def assert_content_free_receipts(state, records, views, captures, action, reason):
    receipts = view_receipts(records)
    assert len(receipts) == len(views)
    assert len({record.event_id for record in receipts}) == len(views)
    assert {record.action_type for record in receipts} == {f"evidence.{action}"}
    assert {record.inputs_meta["view_digest"]: (
        record.inputs_meta["size_bytes"], record.inputs_meta["integrity_identity"],
    ) for record in receipts} == captures
    for record in receipts:
        assert record.conversation_id == state.chat and record.outcome == "success"
        assert set(record.inputs_meta) == {"view_digest", "size_bytes", "integrity_identity", "source_count", "reason"}
        assert re.fullmatch(r"[a-f0-9]{64}", record.inputs_meta["view_digest"])
        assert re.fullmatch(r"[a-f0-9]{64}", record.inputs_meta["integrity_identity"])
        assert record.inputs_meta["source_count"] == 1 and record.inputs_meta["reason"] == reason
    encoded = "\n".join(record.model_dump_json() for record in records)
    for reference, text in views:
        assert reference not in encoded and text not in encoded


def source_messages(state, views):
    view = state.evidence.views.inspect(views[0][0], owner_id=state.owner,
        conversation_id=state.chat, audience_id=f"user:{state.owner}")
    return [{"role": "tool", "content": json.dumps({"reference": view.dependencies[0].reference})}]


async def capture_next(state, messages):
    return await state.evidence.summary_view("Another synthetic temporary interpretation.", messages,
        websocket=state.socket, owner=state.owner, chat=state.chat)


async def revoke(state, loss):
    if loss == "permission":
        await asyncio.to_thread(state.host.tool_permissions.set_tool_overrides, state.owner, AGENT, {TOOL: False})
    elif loss == "security":
        state.host.security_flags = {AGENT: {TOOL: {"blocked": True}}}
    else:
        state.policy.write_text("[]")


async def cleanup(state, mode):
    if mode == "expiry":
        state.clock.advance(timedelta(minutes=11))
        return await state.evidence.sweep()
    if mode == "revocation":
        await revoke(state, "permission")
        return await state.evidence.sweep()
    return await state.evidence.close()


async def test_expired_views_emit_once_without_removing_the_live_source(human, bound, fixture, tmp_path, monkeypatch):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        views, captures = await retained_views(state)
        await cleanup(state, "expiry")
        assert_plaintext_unavailable(state, views)
        assert state.archive.retained_bytes > 0
        records = await events(state)
        assert_content_free_receipts(state, records, views, captures, "view_expiry_cleanup", "expiry")
        assert not any(record.action_type == "evidence.expiry_cleanup" for record in records)
        identities = {record.event_id for record in view_receipts(records)}
        await state.evidence.sweep()
        assert {record.event_id for record in view_receipts(await events(state))} == identities


@pytest.mark.parametrize("loss", ["permission", "security", "retention"])
async def test_revoked_source_removes_all_dependent_views_and_keeps_source_receipt(
    human, bound, fixture, tmp_path, monkeypatch, loss,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        views, captures = await retained_views(state)
        await revoke(state, loss)
        await state.evidence.sweep()
        assert_plaintext_unavailable(state, views)
        assert state.archive.retained_bytes == 0
        records = await events(state)
        assert_content_free_receipts(state, records, views, captures, "view_revocation_cleanup", "source_authority_revoked")
        assert sum(record.action_type == "evidence.revocation_cleanup" for record in records) == 1
        identities = {record.event_id for record in view_receipts(records)}
        await state.evidence.sweep()
        assert {record.event_id for record in view_receipts(await events(state))} == identities


async def test_shutdown_emits_once_and_preserves_the_ordinary_source_cleanup(human, bound, fixture, tmp_path, monkeypatch):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        views, captures = await retained_views(state)
        await state.evidence.close()
        assert_plaintext_unavailable(state, views)
        assert state.archive.retained_bytes == 0
        records = await events(state)
        assert_content_free_receipts(state, records, views, captures, "view_shutdown_cleanup", "shutdown")
        assert sum(record.action_type == "evidence.shutdown_cleanup" for record in records) == 1
        identities = {record.event_id for record in view_receipts(records)}
        await state.evidence.close()
        assert {record.event_id for record in view_receipts(await events(state))} == identities


@pytest.mark.parametrize("mode", ["sweep", "close"])
async def test_reset_views_remain_denied_and_emit_content_free_revocation_once(
    human, bound, fixture, tmp_path, monkeypatch, mode,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        views, captures = await retained_views(state)
        state.evidence.views = ContextViewStore(clock=state.clock)
        for reference, _ in views:
            with pytest.raises(ViewDenied):
                state.evidence.views.inspect(reference, owner_id=state.owner,
                    conversation_id=state.chat, audience_id=f"user:{state.owner}")
            response = await invoke(state, "inspect_context_view", {"view_id": reference})
            assert response.error["code"] == "evidence_unavailable_or_not_authorized"
            assert response.result is None and not response.ui_components
        assert view_receipts(await events(state)) == []
        await getattr(state.evidence, mode)()
        assert state.evidence.views.retained_bytes == 0 and state.evidence.views.view_count == 0
        records = await events(state)
        assert_content_free_receipts(state, records, views, captures, "view_revocation_cleanup", "unavailable")
        assert (state.archive.retained_bytes > 0) is (mode == "sweep")
        identities = {record.event_id for record in view_receipts(records)}
        await getattr(state.evidence, mode)()
        assert {record.event_id for record in view_receipts(await events(state))} == identities


@pytest.mark.parametrize("mode, failed_action", [
    ("expiry", "evidence.view_expiry_cleanup"),
    ("revocation", "evidence.view_revocation_cleanup"),
    ("revocation", "evidence.revocation_cleanup"),
    ("shutdown", "evidence.view_shutdown_cleanup"),
    ("shutdown", "evidence.shutdown_cleanup"),
])
async def test_failed_cleanup_audit_cannot_retain_plaintext_or_fabricate_success(
    human, bound, fixture, tmp_path, monkeypatch, mode, failed_action,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        views, captures = await retained_views(state)
        insert = state.host.audit_repo.insert
        attempted = []

        def unavailable(event):
            if event.action_type == failed_action:
                assert state.evidence.views.retained_bytes == 0
                attempted.append(event.model_copy(deep=True))
                raise RuntimeError("synthetic cleanup audit unavailable")
            return insert(event)

        monkeypatch.setattr(state.host.audit_repo, "insert", unavailable)
        with pytest.raises(RuntimeError, match="synthetic cleanup audit unavailable"):
            await cleanup(state, mode)
        assert_plaintext_unavailable(state, views)
        assert len(attempted) == 1
        if mode != "expiry":
            assert state.archive.retained_bytes == 0
        records = await events(state)
        assert view_receipts(records) == []
        assert not any(record.action_type == failed_action for record in records)
        encoded = "\n".join(record.model_dump_json() for record in records)
        for reference, text in views:
            assert reference not in encoded and text not in encoded
        monkeypatch.setattr(state.host.audit_repo, "insert", insert)
        await state.evidence.close() if mode == "shutdown" else await state.evidence.sweep()
        records = await events(state)
        action, reason = {
            "expiry": ("view_expiry_cleanup", "expiry"),
            "revocation": ("view_revocation_cleanup", "source_authority_revoked"),
            "shutdown": ("view_shutdown_cleanup", "shutdown"),
        }[mode]
        assert_content_free_receipts(state, records, views, captures, action, reason)
        identities = {record.event_id for record in view_receipts(records)}
        await state.evidence.close() if mode == "shutdown" else await state.evidence.sweep()
        assert {record.event_id for record in view_receipts(await events(state))} == identities
        assert len(attempted) == 1


async def test_later_cleanup_receipt_failure_leaves_only_the_actual_earlier_receipt(
    human, bound, fixture, tmp_path, monkeypatch,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        views, captures = await retained_views(state)
        insert = state.host.audit_repo.insert
        attempted = []

        def unavailable(event):
            if event.action_type == "evidence.view_expiry_cleanup":
                attempted.append(event.inputs_meta["view_digest"])
                if len(attempted) == 2:
                    raise RuntimeError("synthetic second receipt unavailable")
            return insert(event)

        monkeypatch.setattr(state.host.audit_repo, "insert", unavailable)
        with pytest.raises(RuntimeError, match="synthetic second receipt unavailable"):
            await cleanup(state, "expiry")
        assert_plaintext_unavailable(state, views)
        receipts = view_receipts(await events(state))
        assert len(receipts) == 1 and receipts[0].inputs_meta["view_digest"] == attempted[0]
        assert receipts[0].inputs_meta["size_bytes"] == captures[attempted[0]][0]
        await state.evidence.sweep()
        assert len(attempted) == 3 and attempted[1] == attempted[2]
        records = await events(state)
        assert_content_free_receipts(state, records, views, captures, "view_expiry_cleanup", "expiry")
        assert receipts[0].event_id in {record.event_id for record in view_receipts(records)}
        identities = {record.event_id for record in view_receipts(records)}
        await state.evidence.sweep()
        assert len(attempted) == 3 and {record.event_id for record in view_receipts(await events(state))} == identities


async def test_expired_read_before_sweep_still_records_every_removed_view(
    human, bound, fixture, tmp_path, monkeypatch,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        views, captures = await retained_views(state)
        state.clock.advance(timedelta(minutes=11))
        response = await invoke(state, "inspect_context_view", {"view_id": views[0][0]})
        assert response.error["code"] == "evidence_unavailable_or_not_authorized"
        await state.evidence.sweep()
        assert_plaintext_unavailable(state, views)
        assert_content_free_receipts(state, await events(state), views, captures, "view_expiry_cleanup", "expiry")


async def test_capture_expiring_older_views_keeps_their_cleanup_receipts(
    human, bound, fixture, tmp_path, monkeypatch,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        views, captures = await retained_views(state)
        messages = source_messages(state, views)
        state.clock.advance(timedelta(minutes=11))
        current = await capture_next(state, messages)
        assert current is not None and current not in {reference for reference, _ in views}
        await state.evidence.sweep()
        assert state.evidence.views.view_count == 1
        assert state.evidence.views.inspect(current, owner_id=state.owner,
            conversation_id=state.chat, audience_id=f"user:{state.owner}").reference == current
        for reference, _ in views:
            with pytest.raises(ViewUnavailable):
                state.evidence.views.inspect(reference, owner_id=state.owner,
                    conversation_id=state.chat, audience_id=f"user:{state.owner}")
        assert_content_free_receipts(state, await events(state), views, captures, "view_expiry_cleanup", "expiry")


@pytest.mark.parametrize("loss", ["integrity_failed", "clock_unavailable"])
async def test_store_integrity_or_clock_loss_retains_trusted_cleanup_metadata(
    human, bound, fixture, tmp_path, monkeypatch, loss,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        views, captures = await retained_views(state, count=1 if loss == "integrity_failed" else 2)
        if loss == "integrity_failed":
            with monkeypatch.context() as patch:
                patch.setattr("orchestrator.context_views._identity", lambda _: "0" * 64)
                response = await invoke(state, "inspect_context_view", {"view_id": views[0][0]})
        else:
            state.clock.advance(timedelta(seconds=-1))
            response = await invoke(state, "inspect_context_view", {"view_id": views[0][0]})
            state.clock.advance(timedelta(seconds=2))
        assert response.error["code"] == "evidence_unavailable_or_not_authorized"
        await state.evidence.sweep()
        assert_plaintext_unavailable(state, views)
        assert_content_free_receipts(state, await events(state), views, captures, "view_revocation_cleanup", loss)


@pytest.mark.parametrize("removal, reason", [("delete", "revoked"), ("read_denied", "deleted")])
async def test_explicit_source_removal_keeps_a_receipt_for_each_generated_view(
    human, bound, fixture, tmp_path, monkeypatch, removal, reason,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        views, captures = await retained_views(state, count=2 if removal == "delete" else 1)
        if removal == "delete":
            reference = json.loads(source_messages(state, views)[0]["content"])["reference"]
            response = await invoke(state, "delete_observation", {"reference": reference})
            assert response.result == {"status": "deleted"}
        else:
            await revoke(state, "permission")
            response = await invoke(state, "inspect_context_view", {"view_id": views[0][0]})
            assert response.error["code"] == "evidence_unavailable_or_not_authorized"
        await state.evidence.sweep()
        assert_plaintext_unavailable(state, views)
        assert_content_free_receipts(state, await events(state), views, captures, "view_revocation_cleanup", reason)


async def test_unwritten_cleanup_receipts_bound_new_captures_until_a_successful_retry(
    human, bound, fixture, tmp_path, monkeypatch,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        state.evidence.views.max_records = 2
        views, captures = await retained_views(state)
        messages = source_messages(state, views)
        insert = state.host.audit_repo.insert
        attempts = []

        def unavailable(event):
            if event.action_type == "evidence.view_expiry_cleanup":
                assert state.evidence.views.retained_bytes == 0
                attempts.append(event.model_copy(deep=True))
                raise RuntimeError("synthetic pending receipt unavailable")
            return insert(event)

        state.clock.advance(timedelta(minutes=11))
        response = await invoke(state, "inspect_context_view", {"view_id": views[0][0]})
        assert response.error["code"] == "evidence_unavailable_or_not_authorized"
        monkeypatch.setattr(state.host.audit_repo, "insert", unavailable)
        with pytest.raises(RuntimeError, match="synthetic pending receipt unavailable"):
            await state.evidence.sweep()
        assert_plaintext_unavailable(state, views)
        assert await capture_next(state, messages) is None
        assert state.evidence.views.view_count == 0 and state.evidence.views.retained_bytes == 0
        assert view_receipts(await events(state)) == []
        assert len([record for record in await events(state) if record.action_type == "evidence.view_capture"]) == 2
        for event in attempts:
            for reference, text in views:
                assert reference not in event.model_dump_json() and text not in event.model_dump_json()
        monkeypatch.setattr(state.host.audit_repo, "insert", insert)
        await state.evidence.sweep()
        records = await events(state)
        assert_content_free_receipts(state, records, views, captures, "view_expiry_cleanup", "expiry")
        identities = {record.event_id for record in view_receipts(records)}
        await state.evidence.sweep()
        assert {record.event_id for record in view_receipts(await events(state))} == identities
        current = await capture_next(state, messages)
        assert current is not None and state.evidence.views.view_count == 1


async def test_committed_cleanup_with_lost_acknowledgement_retries_the_same_receipt(
    human, bound, fixture, tmp_path, monkeypatch,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        views, captures = await retained_views(state, count=1)
        insert = state.host.audit_repo.insert
        attempted = []

        def acknowledgement_lost(event):
            result = insert(event)
            if event.action_type == "evidence.view_expiry_cleanup":
                attempted.append(event.model_copy(deep=True))
                raise RuntimeError("synthetic cleanup acknowledgement lost")
            return result

        monkeypatch.setattr(state.host.audit_repo, "insert", acknowledgement_lost)
        with pytest.raises(RuntimeError, match="synthetic cleanup acknowledgement lost"):
            await cleanup(state, "expiry")
        assert_plaintext_unavailable(state, views)
        records = await events(state)
        assert_content_free_receipts(state, records, views, captures, "view_expiry_cleanup", "expiry")
        assert len(attempted) == 1
        identities = {record.event_id for record in view_receipts(records)}
        monkeypatch.setattr(state.host.audit_repo, "insert", insert)
        state.clock.advance(timedelta(seconds=1))
        await state.evidence.sweep()
        assert {record.event_id for record in view_receipts(await events(state))} == identities
        assert len(view_receipts(await events(state))) == 1
        await state.evidence.sweep()
        assert {record.event_id for record in view_receipts(await events(state))} == identities
