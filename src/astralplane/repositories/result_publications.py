"""Commits a one-time owner canvas Save through the existing action and publication
ledgers, with prepare() as a locked read-only observation before commit(). Creates no
worker claim or dispatch permit; used by orchestrator/work_publication.py.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import fields
from datetime import timedelta

from astralplane.database.dialect import dialect_of
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories import assignments as a
from astralplane.repositories.history import SessionCredentialFence, SessionExecutionObservation
from astralplane.repositories.result_publication_models import (
    ResultPublicationContent,
    ResultPublicationPreparation,
    ResultPublicationProposal,
    ResultPublicationReceipt,
)
from astralplane.repositories.selected_input_models import AssignmentSelectedInput
from astralplane.repositories.workspaces import (
    CanvasComponentRecord,
    CanvasRepository,
    LayoutRecord,
    LayoutRepository,
    PublicationRebaseComponent,
    PublicationRebaseLayout,
    PublicationRepository,
)

KIND = "result_publication"
_ACTION_KEYS = {
    "action_id",
    "assignment_id",
    "owner_id",
    "intent",
    "intent_digest",
    "instruction_revision",
    "control_epoch",
    "state",
    "result",
    "attempts",
    "decision",
    "foreground_admission",
    "reconciliation",
    "publication_receipt",
    "approval_consumed_at",
}


def _require(condition):
    if not condition:
        a._conflict("assignment_publication_conflict")


def _typed(value, model):
    if type(value) is not model:
        raise RepositoryValidationError("typed result publication metadata required")
    return {entry.name: getattr(value, entry.name) for entry in fields(model)}


def _proposal(value):
    copied = _typed(value, ResultPublicationProposal)
    copied["expires_at"] = a._guidance_cutoff(copied["expires_at"])
    result = ResultPublicationProposal(**copied)
    for name in ("action_id", "result_action_id", "publication_id"):
        a._uuid(getattr(result, name))
    for name in ("conversation_id", "component_id"):
        if type(getattr(result, name)) is not str:
            raise RepositoryValidationError("invalid publication destination")
        a._text(getattr(result, name))
    for name in (
        "result_digest",
        "content_digest",
        "stage_digest",
        "permission_digest",
        "precondition_digest",
    ):
        a._digest(getattr(result, name))
    a._integer(result.base_render_revision)
    if result.base_render_revision == 0:
        _require(result.base_publication_id is None)
    else:
        a._text(result.base_publication_id)
    if type(result.version) is not int or result.version != 1 or result.expires_at is None:
        raise RepositoryValidationError("unsupported result publication version")
    _require(result.action_id != result.result_action_id)
    return result


def _content(value):
    _typed(value, ResultPublicationContent)
    if type(value.components) is not tuple or type(value.layouts) is not tuple:
        raise RepositoryValidationError("typed publication sequence required")
    if not 1 <= len(value.components) <= 10000 or len(value.layouts) > 10000:
        raise RepositoryValidationError("publication stage exceeds bounds")
    components, layouts = [], []
    maximum_bytes = 8 * 1024 * 1024
    accumulated_bytes = len(a.canonical(ResultPublicationContent((), ())).encode())

    def append_bounded(destination, entry):
        nonlocal accumulated_bytes
        accumulated_bytes += len(a.canonical(entry, maximum_bytes).encode()) + bool(destination)
        if accumulated_bytes > maximum_bytes:
            raise RepositoryValidationError("assignment JSON exceeds its bound")
        destination.append(entry)

    for entry in value.components:
        copied = _typed(entry, PublicationRebaseComponent)
        for key in ("row_id", "component_id", "component_type"):
            if type(copied[key]) is not str:
                raise RepositoryValidationError("invalid component identity")
            a._text(copied[key], 128 if key == "component_type" else 512)
        a._integer(copied["position"])
        if copied["title"] is not None and (
            type(copied["title"]) is not str or len(copied["title"].encode()) > 512
        ):
            raise RepositoryValidationError("invalid component title")
        copied["payload"] = json.loads(a.canonical(copied["payload"]))
        append_bounded(components, PublicationRebaseComponent(**copied))
    for entry in value.layouts:
        copied = _typed(entry, PublicationRebaseLayout)
        if type(copied["layout_key"]) is not str:
            raise RepositoryValidationError("invalid layout identity")
        a._text(copied["layout_key"])
        a._integer(copied["position"])
        copied["tree"] = json.loads(a.canonical(copied["tree"]))
        append_bounded(layouts, PublicationRebaseLayout(**copied))
    for entries, identity in ((components, "component_id"), (layouts, "layout_key")):
        if len({getattr(entry, identity) for entry in entries}) != len(entries) or {
            entry.position for entry in entries
        } != set(range(len(entries))):
            raise RepositoryValidationError("publication stage identity or position conflicts")
    if len({entry.row_id for entry in components}) != len(components):
        raise RepositoryValidationError("publication component rows conflict")
    result = ResultPublicationContent(
        tuple(sorted(components, key=lambda item: item.position)),
        tuple(sorted(layouts, key=lambda item: item.position)),
    )
    a.canonical(result, 8 * 1024 * 1024)
    return result


def _stage(value):
    return {
        "version": 1,
        "components": a.plain(value.components),
        "layouts": a.plain(value.layouts),
    }


def result_publication_stage_digest(content: ResultPublicationContent) -> str:
    import hashlib

    return hashlib.sha256(
        a.canonical(_stage(_content(content)), 8 * 1024 * 1024).encode()
    ).hexdigest()


def _check_content(proposal, content):
    _require(result_publication_stage_digest(content) == proposal.stage_digest)
    selected = [
        entry for entry in content.components if entry.component_id == proposal.component_id
    ]
    _require(len(selected) == 1 and a.digest(selected[0].payload) == proposal.content_digest)


def _selection(value):
    if value is None:
        return None
    return AssignmentSelectedInput(**_typed(value, AssignmentSelectedInput))


def _authority(value):
    if value is None:
        return None
    copied = _typed(value, SessionExecutionObservation)
    copied["credential"] = SessionCredentialFence(
        **_typed(value.credential, SessionCredentialFence)
    )
    for key in ("started_at", "valid_until"):
        copied[key] = a._guidance_cutoff(copied[key])
    return SessionExecutionObservation(**copied)


def _decision(value):
    result = a.AssignmentActionDecision(**_typed(value, a.AssignmentActionDecision))
    a._uuid(result.submission_id)
    for name in (
        "submission_digest",
        "proposal_digest",
        "permission_digest",
        "precondition_digest",
    ):
        a._digest(getattr(result, name))
    if type(result.decision) is not str or result.decision != "approve":
        raise RepositoryValidationError("result Save requires an exact approval")
    return result


def _ids(owner_id, assignment_id, action_id, expected_state_version):
    if type(owner_id) is not str:
        raise RepositoryValidationError("invalid owner")
    a._text(owner_id)
    a._uuid(assignment_id)
    a._uuid(action_id)
    a._integer(expected_state_version, 1)


def _request(proposal, selected):
    return {
        "kind": KIND,
        "version": 1,
        "proposal": a.plain(proposal),
        "selected_digest": a.digest(selected),
    }


def _intent(proposal, selected):
    request = _request(proposal, selected)
    return a.AssignmentActionIntent(
        action_key="result-publication-v1:" + proposal.action_id,
        request=request,
        request_digest=a.digest(request),
        maximum=a.AssignmentResourceAmount(),
        permission_digest=proposal.permission_digest,
        precondition_digest=proposal.precondition_digest,
        sensitivity="sensitive",
        interactive_only=True,
        boundary="internal_transaction",
        approval_expires_at=proposal.expires_at,
    )


def _decode_proposal(action):
    raw = action["intent"]["request"]
    _require(
        set(raw) == {"kind", "version", "proposal", "selected_digest"}
        and raw["kind"] == KIND
        and type(raw["version"]) is int
        and raw["version"] == 1
    )
    values = dict(raw["proposal"])
    values["expires_at"] = a._time(values["expires_at"])
    result = _proposal(ResultPublicationProposal(**values))
    a._digest(raw["selected_digest"])
    _require(result.action_id == action["action_id"])
    return result


def _receipt(action):
    values = dict(action["publication_receipt"])
    values["committed_at"] = a._time(values["committed_at"])
    result = ResultPublicationReceipt(**values)
    proposal = _decode_proposal(action)
    decision = _decision(a.AssignmentActionDecision(**action["decision"]))
    expected = ResultPublicationReceipt(
        action["owner_id"],
        action["assignment_id"],
        action["action_id"],
        action["intent"]["request_digest"],
        decision.submission_id,
        decision.submission_digest,
        proposal.publication_id,
        proposal.conversation_id,
        proposal.component_id,
        proposal.content_digest,
        proposal.stage_digest,
        proposal.result_action_id,
        proposal.result_digest,
        action["intent"]["request"]["selected_digest"],
        action["instruction_revision"],
        action["control_epoch"],
        proposal.base_render_revision + 1,
        result.committed_at,
    )
    _require(result.committed_at is not None and a.canonical(result) == a.canonical(expected))
    _require(
        decision.proposal_digest == action["intent"]["request_digest"]
        and decision.permission_digest == proposal.permission_digest
        and decision.precondition_digest == proposal.precondition_digest
    )
    return result


def known_publication_action(action):
    try:
        _require(set(action) == _ACTION_KEYS)
        proposal = _decode_proposal(action)
        request = action["intent"]["request"]
        intent = a.plain(_intent(proposal, None))
        intent["request"] = request
        intent["request_digest"] = a.digest(request)
        _require(
            a.canonical(intent) == a.canonical(action["intent"])
            and action["intent_digest"] == a.digest(intent)
        )
        a._integer(action["instruction_revision"], 1)
        a._integer(action["control_epoch"], 1)
        _require(
            action["attempts"] == []
            and action["result"] is None
            and action["foreground_admission"] is None
            and action["reconciliation"] is None
        )
        if action["state"] == "succeeded":
            receipt = _receipt(action)
            _require(action["approval_consumed_at"] == a.plain(receipt.committed_at))
        else:
            _require(
                action["state"] in {"proposed", "invalidated"}
                and action["decision"] is None
                and action["publication_receipt"] is None
                and action["approval_consumed_at"] is None
            )
        return action
    except (RepositoryConflictError, RepositoryValidationError, TypeError, KeyError, ValueError):
        return None


def _completed(repo, tx, data, proposal):
    operation = data.get("operation") or {}
    _require(
        a._executable(data)
        and type(operation.get("version")) is int
        and operation["version"] == 2
        and operation["kind"] == "research"
        and operation["source_retention"] == "operation"
        and data["lifecycle"] == "completed"
        and data["phase"] not in {"waiting_authorization", "reconciliation", "failed"}
        and operation.get("terminal_outcome") == "completed"
        and operation.get("result_reference") == proposal.result_action_id
    )
    actions, _, held = repo._purge_blockers(tx, data)
    _require(not held)
    model = next(
        (entry for entry in actions if entry["action_id"] == proposal.result_action_id), None
    )
    _require(model is not None)
    _settled(data, model)
    _require(
        model["intent"]["request"].get("kind") == "model"
        and model["result"]["result_digest"] == proposal.result_digest
    )
    transient = a._transient_input(
        model["intent"].get("transient_input"),
        model["intent"]["request"],
        model["intent"]["request_digest"],
    )
    _require(transient.source_retention == "operation")
    sources = [ref for ref in transient.references if ref.kind == "source"]
    _require(len(sources) == 1 and type(sources[0].revision) is int and sources[0].revision == 1)
    source = next(
        (entry for entry in actions if entry["action_id"] == sources[0].resource_id), None
    )
    _require(source is not None and source["action_id"] != model["action_id"])
    _settled(data, source)
    _require(
        source["intent"]["request"].get("kind") == "tool"
        and source["intent"]["boundary"] == "read_only"
        and source["intent"]["request_digest"] == a.digest(source["intent"]["request"])
    )
    definition_source = data["definition"]["source"]
    _require({"agent_id", "tool_name", "arguments"} <= set(definition_source))
    expected_request = {
        "kind": "tool",
        **{key: definition_source[key] for key in ("agent_id", "tool_name", "arguments")},
    }
    _require(a.canonical(source["intent"]["request"]) == a.canonical(expected_request))
    selected = repo.get_selected_input(
        tx, owner_id=data["owner_id"], assignment_id=data["assignment_id"]
    )
    expected_refs = (sources[0],) + (
        ()
        if selected is None
        else tuple(
            a.AssignmentInputReference(ref.kind, ref.resource_id, ref.revision)
            for ref in selected.references
        )
    )
    _require(a.canonical(transient.references) == a.canonical(expected_refs))
    if selected is not None and selected.envelope is not None:
        _require(transient.binding_key_id == selected.envelope.binding_key_id)
    disposition = model["result"].get("result_disposition") or {}
    _require(
        type(disposition.get("version")) is int
        and disposition["version"] == 1
        and disposition.get("available") is True
        and disposition.get("binding_key_id") == transient.binding_key_id
    )


def _settled(data, action):
    a._version(action, data["instruction_revision"], data["control_epoch"])
    result = action["result"] or {}
    _require(
        action["state"] == "succeeded"
        and action["attempts"]
        and action["attempts"][-1]["dispatch_token"] is not None
        and action["attempts"][-1]["state"] == "succeeded"
        and action["reconciliation"] is None
        and result.get("outcome") == "succeeded"
        and result.get("result_available", True) is True
        and result.get("result") is not None
    )


def _bounds(repo, tx, data, proposal, authority, caller_cutoff):
    _require(authority is not None and caller_cutoff is not None)
    _require(data.get("execution_profile") == "one_shot" and a._executable(data))
    if not repo._lock_execution_authority(tx, data, authority):
        a._conflict("assignment_authorization_unavailable")
    selected = data["operation"]["authority"]
    return min(
        proposal.expires_at,
        caller_cutoff,
        authority.valid_until,
        a._time(selected["expires_at"]),
        a._time(data["operation"]["deadline_at"]),
    )


def _final(repo, tx, data, proposal, authority, caller_cutoff, selected_digest):
    cutoff = _bounds(repo, tx, data, proposal, authority, caller_cutoff)
    selected = repo.get_selected_input(
        tx, owner_id=data["owner_id"], assignment_id=data["assignment_id"]
    )
    _require(a.digest(selected) == selected_digest)
    repo.assert_selected_input_current(
        tx,
        owner_id=data["owner_id"],
        assignment_id=data["assignment_id"],
        expected_instruction_revision=data["instruction_revision"],
        expected_control_epoch=data["control_epoch"],
        expected_state_version=data["state_version"],
        expected=selected,
        authority_valid_until=cutoff,
    )


def _head(tx, owner_id, proposal):
    try:
        with tx.savepoint("result_publication_head"):
            existing = tx.fetch_one(
                "SELECT commit_id FROM conversation_commit WHERE commit_id=%s FOR UPDATE NOWAIT",
                (proposal.publication_id,),
            )
            _require(existing is None)
            row = tx.fetch_one(
                "SELECT render_revision,conversation_commit_id FROM chats "
                "WHERE user_id=%s AND id=%s FOR UPDATE NOWAIT",
                (owner_id, proposal.conversation_id),
            )
            if row is None:
                raise RepositoryNotFoundError("publication destination unavailable")
            _require(
                (row["render_revision"] or 0) == proposal.base_render_revision
                and row["conversation_commit_id"] == proposal.base_publication_id
            )
    except Exception as exc:
        if getattr(exc, "pgcode", None) == "55P03":
            a._conflict("assignment_publication_busy")
        raise


def _rows(tx, owner_id, proposal):
    try:
        with tx.savepoint("result_publication_content_read"):
            for table in ("saved_components", "workspace_layout"):
                rows = tx.fetch_all(
                    "SELECT id FROM " + table + " WHERE conversation_commit_id=%s "
                    "ORDER BY id LIMIT 10001 FOR UPDATE NOWAIT",
                    (proposal.publication_id,),
                )
                _require(len(rows) <= 10000)
    except Exception as exc:
        if getattr(exc, "pgcode", None) == "55P03":
            a._conflict("assignment_publication_busy")
        raise
    canvas = CanvasRepository().list_for_publication(
        tx,
        owner_id=owner_id,
        conversation_id=proposal.conversation_id,
        publication_id=proposal.publication_id,
        committed_render_revision=proposal.base_render_revision + 1,
    )
    layouts = LayoutRepository().list_for_publication(
        tx,
        owner_id=owner_id,
        conversation_id=proposal.conversation_id,
        publication_id=proposal.publication_id,
        committed_render_revision=proposal.base_render_revision + 1,
    )
    _require(
        not tx.fetch_one(
            "SELECT id FROM messages WHERE conversation_commit_id=%s LIMIT 1",
            (proposal.publication_id,),
        )
    )
    return _content(
        ResultPublicationContent(
            tuple(
                PublicationRebaseComponent(
                    row.row_id,
                    row.component_id,
                    row.payload,
                    row.component_type,
                    row.title,
                    row.position,
                )
                for row in canvas
            ),
            tuple(
                PublicationRebaseLayout(row.layout_key, row.position, row.tree) for row in layouts
            ),
        )
    )


def _replay(tx, action, decision):
    _require(a.canonical(action["decision"]) == a.canonical(decision))
    receipt = _receipt(action)
    proposal = _decode_proposal(action)
    try:
        with tx.savepoint("result_publication_receipt_read"):
            tx.fetch_one(
                "SELECT commit_id FROM conversation_commit WHERE commit_id=%s FOR UPDATE NOWAIT",
                (proposal.publication_id,),
            )
    except Exception as exc:
        if getattr(exc, "pgcode", None) == "55P03":
            a._conflict("assignment_publication_busy")
        raise
    publication = PublicationRepository().get_for_owner(
        tx, owner_id=receipt.owner_id, publication_id=receipt.publication_id
    )
    _require(
        publication is not None
        and publication.state == "committed"
        and publication.conversation_id == receipt.conversation_id
        and publication.committed_render_revision == receipt.committed_render_revision
        and publication.request_generation == receipt.action_id
        and publication.committed_at == receipt.committed_at
    )
    _check_content(proposal, _rows(tx, receipt.owner_id, proposal))
    return receipt


def put(
    repo,
    tx,
    *,
    owner_id,
    assignment_id,
    expected_instruction_revision,
    expected_control_epoch,
    expected_state_version,
    proposal,
    content,
    expected_selected,
    authority,
    caller_valid_until,
):
    proposal = _proposal(proposal)
    content = _content(content)
    _check_content(proposal, content)
    selected = _selection(expected_selected)
    authority, cutoff = _authority(authority), a._guidance_cutoff(caller_valid_until)
    _ids(owner_id, assignment_id, proposal.action_id, expected_state_version)
    a._integer(expected_instruction_revision, 1)
    a._integer(expected_control_epoch, 1)
    intent = _intent(proposal, selected)
    with tx.savepoint("result_publication_propose"):
        _require(repo._lock_operation_owner(tx, owner_id))
        peek = repo._load(tx, owner_id, assignment_id)
        _bounds(repo, tx, peek, proposal, authority, cutoff)
        data = repo._load(tx, owner_id, assignment_id, lock=True)
        _completed(repo, tx, data, proposal)
        old = repo._action(tx, owner_id, assignment_id, proposal.action_id, required=False)
        if old is not None:
            _require(
                known_publication_action(old) is not None
                and old["intent_digest"] == a.digest(intent)
            )
            _final(repo, tx, data, proposal, authority, cutoff, a.digest(selected))
            return a._action_record(old)
        a._version(data, expected_instruction_revision, expected_control_epoch)
        a._state_version(data, expected_state_version)
        counts = tx.fetch_one(
            "SELECT count(*) AS total,count(*) FILTER(WHERE state='proposed') "
            "AS pending FROM persistent_assignment_action WHERE assignment_id=%s",
            (assignment_id,),
        )
        _require(counts["total"] < 10000 and counts["pending"] < 100)
        _require(proposal.expires_at <= a._now(tx) + timedelta(hours=24))
        _head(tx, owner_id, proposal)
        action = dict(
            action_id=proposal.action_id,
            assignment_id=assignment_id,
            owner_id=owner_id,
            intent=a.plain(intent),
            intent_digest=a.digest(intent),
            instruction_revision=data["instruction_revision"],
            control_epoch=data["control_epoch"],
            state="proposed",
            result=None,
            attempts=[],
            decision=None,
            foreground_admission=None,
            reconciliation=None,
            publication_receipt=None,
            approval_consumed_at=None,
        )
        tx.execute(
            "INSERT INTO persistent_assignment_action "
            "(id,assignment_id,owner_user_id,action_key,state,data) "
            "VALUES(%s,%s,%s,%s,'proposed',%s::jsonb)",
            (proposal.action_id, assignment_id, owner_id, intent.action_key, a.canonical(action)),
        )
        repo._save(tx, data)
        _final(repo, tx, data, proposal, authority, cutoff, a.digest(selected))
        return a._action_record(action)


@contextmanager
def _prepare_rows(tx):
    previous = None
    try:
        if dialect_of(tx).is_sqlite:
            previous = tx.fetch_one("SELECT current_setting('lock_timeout') AS value")["value"]
            tx.fetch_one("SELECT set_config('lock_timeout','1ms',true)")
        with tx.savepoint("result_publication_prepare"):
            yield
    except Exception as exc:
        if dialect_of(tx).is_sqlite and getattr(exc, "pgcode", None) == "55P03":
            a._conflict("assignment_publication_busy")
        raise
    finally:
        if previous is not None:
            tx.fetch_one("SELECT set_config('lock_timeout',%s,true)", (previous,))


def prepare(
    repo,
    tx,
    *,
    owner_id,
    assignment_id,
    action_id,
    decision,
    expected_state_version,
    authority=None,
    caller_valid_until=None,
):
    decision = _decision(decision)
    authority, cutoff = _authority(authority), a._guidance_cutoff(caller_valid_until)
    _ids(owner_id, assignment_id, action_id, expected_state_version)
    with _prepare_rows(tx):
        _require(repo._lock_operation_owner(tx, owner_id))
        peek = repo._load(tx, owner_id, assignment_id)
        row = tx.fetch_one(
            "SELECT data FROM persistent_assignment_action "
            "WHERE owner_user_id=%s AND assignment_id=%s AND id=%s",
            (owner_id, assignment_id, action_id),
        )
        if row is None:
            raise RepositoryNotFoundError("result publication proposal unavailable")
        initial = a.plain(row["data"])
        _require(known_publication_action(initial) is not None)
        proposal = _decode_proposal(initial)
        replay = initial["publication_receipt"] is not None
        if not replay:
            _bounds(repo, tx, peek, proposal, authority, cutoff)
        data = repo._load(tx, owner_id, assignment_id, lock=True)
        if not replay:
            _completed(repo, tx, data, proposal)
        action = repo._action(tx, owner_id, assignment_id, action_id)
        _require(a.canonical(action) == a.canonical(initial))
        if replay:
            receipt = _replay(tx, action, decision)
        else:
            _require(
                action["state"] == "proposed"
                and decision.proposal_digest == action["intent"]["request_digest"]
                and decision.permission_digest == proposal.permission_digest
                and decision.precondition_digest == proposal.precondition_digest
            )
            a._version(data, action["instruction_revision"], action["control_epoch"])
            a._state_version(data, expected_state_version)
            _head(tx, owner_id, proposal)
            _final(
                repo,
                tx,
                data,
                proposal,
                authority,
                cutoff,
                action["intent"]["request"]["selected_digest"],
            )
            receipt = None
        return ResultPublicationPreparation(
            a._record(data), a._action_record(action), proposal, receipt, replay
        )


@contextmanager
def _fresh_writes(tx):
    before = tx.fetch_one("SELECT current_setting('lock_timeout') AS value")["value"]
    try:
        tx.fetch_one("SELECT set_config('lock_timeout','1ms',true)")
        with tx.savepoint("result_publication_fresh_rows"):
            yield
    except Exception as exc:
        if getattr(exc, "pgcode", None) == "55P03":
            a._conflict("assignment_publication_busy")
        raise
    finally:
        tx.fetch_one("SELECT set_config('lock_timeout',%s,true)", (before,))


def commit(
    repo,
    tx,
    *,
    owner_id,
    assignment_id,
    action_id,
    decision,
    expected_state_version,
    content,
    authority=None,
    caller_valid_until=None,
):
    # Validate/copy inputs before the savepoint can wait
    content, decision = _content(content), _decision(decision)
    authority, cutoff = _authority(authority), a._guidance_cutoff(caller_valid_until)
    with tx.savepoint("result_publication_commit"):
        preparation = prepare(
            repo,
            tx,
            owner_id=owner_id,
            assignment_id=assignment_id,
            action_id=action_id,
            decision=decision,
            expected_state_version=expected_state_version,
            authority=authority,
            caller_valid_until=cutoff,
        )
        proposal = preparation.proposal
        _check_content(proposal, content)
        if preparation.replayed:
            return preparation.receipt
        with _fresh_writes(tx):
            publications = PublicationRepository()
            now = a._now(tx)
            publications.stage(
                tx,
                publication_id=proposal.publication_id,
                owner_id=owner_id,
                conversation_id=proposal.conversation_id,
                request_generation=action_id,
                base_render_revision=proposal.base_render_revision,
                started_at=now,
            )
            timestamp = int(now.timestamp())
            for entry in content.components:
                CanvasRepository().create(
                    tx,
                    CanvasComponentRecord(
                        entry.row_id,
                        proposal.conversation_id,
                        owner_id,
                        entry.component_id,
                        entry.payload,
                        entry.component_type,
                        entry.title,
                        entry.position,
                        timestamp,
                        timestamp,
                        proposal.publication_id,
                        proposal.base_render_revision + 1,
                    ),
                )
            for entry in content.layouts:
                LayoutRepository().create(
                    tx,
                    LayoutRecord(
                        0,
                        proposal.conversation_id,
                        owner_id,
                        entry.layout_key,
                        entry.position,
                        entry.tree,
                        timestamp,
                        timestamp,
                        proposal.publication_id,
                        proposal.base_render_revision + 1,
                    ),
                )
            publications.validate_stage(
                tx,
                owner_id=owner_id,
                conversation_id=proposal.conversation_id,
                publication_id=proposal.publication_id,
            )
            _check_content(proposal, _rows(tx, owner_id, proposal))
            published = publications.commit_at_head(
                tx,
                owner_id=owner_id,
                conversation_id=proposal.conversation_id,
                publication_id=proposal.publication_id,
                expected_staged_base_render_revision=proposal.base_render_revision,
                expected_head_render_revision=proposal.base_render_revision,
                expected_head_publication_id=proposal.base_publication_id,
                committed_at=now,
                updated_at=timestamp,
            )
        data = repo._load(tx, owner_id, assignment_id)
        action = repo._action(tx, owner_id, assignment_id, action_id)
        receipt = ResultPublicationReceipt(
            owner_id,
            assignment_id,
            action_id,
            action["intent"]["request_digest"],
            decision.submission_id,
            decision.submission_digest,
            proposal.publication_id,
            proposal.conversation_id,
            proposal.component_id,
            proposal.content_digest,
            proposal.stage_digest,
            proposal.result_action_id,
            proposal.result_digest,
            action["intent"]["request"]["selected_digest"],
            data["instruction_revision"],
            data["control_epoch"],
            published.committed_render_revision,
            now,
        )
        action.update(
            state="succeeded",
            decision=a.plain(decision),
            publication_receipt=a.plain(receipt),
            approval_consumed_at=a.plain(now),
        )
        _require(known_publication_action(action) is not None)
        repo._save_action(tx, action)
        repo._save(tx, data)
        _final(repo, tx, data, proposal, authority, cutoff, receipt.selected_digest)
        return receipt
