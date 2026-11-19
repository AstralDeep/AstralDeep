"""Implements the orchestrator meta-tools create_capability/extend_agent: drafts an
agent, security-gates it, and returns an approve/refine/discard chat card gated by
explicit user approval. Also holds the draft decision handlers used by chrome_events.py.
"""

import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import time
from typing import Any, Dict, List, Optional

from astralprims import Alert, Button, Card, Text
from orchestrator.bounded_work import run_generation
from orchestrator.code_security import blocks_execution
from shared.protocol import MCPResponse

logger = logging.getLogger("Orchestrator.AgenticCreation")

def _draft_store(orch):
    lifecycle = getattr(orch, "lifecycle_manager", None)
    store = vars(lifecycle).get("draft_store") if hasattr(lifecycle, "__dict__") else None
    if store is None:
        raise RuntimeError("Plane draft persistence is unavailable")
    return store


def _parser_repository(orch):
    from orchestrator.attachments.parser_repo import AttachmentParserRepository
    from orchestrator.plane_repository_context import plane_source_from_orchestrator

    return AttachmentParserRepository.from_plane_source(
        plane_source_from_orchestrator(orch)
    )


def gap_fingerprint(agent_name: str, tools_spec: Optional[List[Dict]] = None,
                    extra: str = "") -> str:
    names = sorted((t.get("name") or "").strip().lower() for t in (tools_spec or []))
    basis = "|".join([(agent_name or "").strip().lower(), *names, (extra or "").strip().lower()])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


async def _audit(user_id: str, action_type: str, description: str,
                 correlation_id: str, outcome: str = "success",
                 chat_id: Optional[str] = None, agent_id: Optional[str] = None,
                 inputs_meta: Optional[Dict] = None) -> None:
    try:
        from datetime import datetime, timezone

        from audit.recorder import get_recorder
        from audit.schemas import AuditEventCreate
        rec = get_recorder()
        if rec is None:
            return
        await rec.record(AuditEventCreate(
            actor_user_id=user_id or "unknown",
            auth_principal=user_id or "unknown",
            agent_id=agent_id,
            event_class="agent_lifecycle",
            action_type=action_type,
            description=description[:1024],
            conversation_id=chat_id,
            correlation_id=correlation_id,
            outcome=outcome,
            inputs_meta=inputs_meta or {},
            started_at=datetime.now(timezone.utc),
        ))
    except Exception:
        logger.debug("agentic: audit record failed (%s)", action_type, exc_info=True)


def _read_draft_code(orch, draft: Dict[str, Any]) -> str:
    try:
        agents_dir = orch.lifecycle_manager._agents_dir
        path = os.path.join(agents_dir, draft["agent_slug"], "mcp_tools.py")
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        logger.debug("archive: could not read draft code for %s", draft.get("id"),
                     exc_info=True)
        return ""


def _archive_on_success(orch, draft: Dict[str, Any], self_test: Dict[str, Any]) -> None:
    try:
        from orchestrator import draft_archive
        if not draft_archive.archive_enabled():
            return
        if (self_test or {}).get("status") != "passed":
            return
        code = _read_draft_code(orch, draft)
        if not code:
            return
        draft_archive.record_archived_draft(
            draft_archive.draft_fingerprint(draft),
            code,
            1.0,
            owner_user_id=str(draft.get("user_id") or ""),
            draft_uuid=str(draft.get("draft_uuid") or draft.get("id") or ""),
            source_state_revision=int(draft.get("state_revision") or 0),
        )
    except Exception:  # pragma: no cover
        logger.debug("archive: record-on-success failed", exc_info=True)


def _decision_buttons(draft_id: str, revision: bool = False) -> List[Dict[str, Any]]:
    approve_action = "revision_apply" if revision else "draft_approve"
    discard_action = "revision_discard" if revision else "draft_discard"
    approve_label = "Apply to live agent" if revision else "Approve"
    return [
        Button(label=approve_label, action=approve_action, payload={"draft_id": draft_id}).to_dict(),
        Button(label="Refine", action="draft_refine", payload={"draft_id": draft_id}).to_dict(),
        Button(label="Discard", action=discard_action, payload={"draft_id": draft_id}).to_dict(),
    ]


def creation_card(draft: Dict[str, Any], self_test: Dict[str, Any],
                  revision: bool = False, note: str = "") -> Dict[str, Any]:
    verdict = {"passed": "✓ Gate checks passed", "failed": "✗ Gate checks failed"}.get(self_test.get("status"))
    lines = [Text(content=draft.get("description", ""), variant="caption").to_dict()]
    if verdict:
        lines.append(Text(content=f"**{verdict}** — {self_test.get('summary', '')}", variant="markdown").to_dict())
    if self_test.get("evidence"):
        lines.append(Text(content=f"Preview: {self_test['evidence']}", variant="caption").to_dict())
    if note:
        lines.append(Text(content=note, variant="caption").to_dict())
    what = "Draft revision" if revision else "Draft agent"
    return Card(
        id=f"draft-card-{draft['id']}",
        title=f"{what}: {draft.get('agent_name', 'unnamed')}",
        content=lines + _decision_buttons(draft["id"], revision=revision),
    ).to_dict()


def _error_card(message: str) -> Dict[str, Any]:
    return Alert(message=message, variant="error").to_dict()


async def handle_meta_tool(orch, tool_name: str, args: Dict[str, Any], *,
                           user_id: str, chat_id: Optional[str],
                           websocket=None) -> MCPResponse:
    try:
        if tool_name == "create_capability":
            return await _create_capability(orch, args, user_id=user_id,
                                            chat_id=chat_id, websocket=websocket)
        if tool_name == "extend_agent":
            return await _extend_agent(orch, args, user_id=user_id,
                                       chat_id=chat_id, websocket=websocket)
        return MCPResponse(error={"message": f"Unknown meta-tool: {tool_name}", "retryable": False})
    except Exception as exc:
        logger.exception("agentic: meta-tool %s failed", tool_name)
        card = _error_card(
            "Creating the capability failed unexpectedly. You can retry, rephrase the "
            "request, or create the agent manually under Settings → Agents & permissions."
        )
        return MCPResponse(
            result={"status": "error", "detail": str(exc)[:300]},
            ui_components=[card],
        )


async def _create_capability(orch, args: Dict[str, Any], *, user_id: str,
                             chat_id: Optional[str], websocket=None) -> MCPResponse:
    agent_name = (args.get("agent_name") or "").strip()
    description = (args.get("description") or "").strip()
    tools_spec = args.get("tools_spec") or []
    if not agent_name or len(description) < 10 or not tools_spec:
        return MCPResponse(error={
            "message": "create_capability needs agent_name, a description (≥10 chars) and tools_spec",
            "retryable": False})
    tools_spec = tools_spec[:4]

    fingerprint = gap_fingerprint(agent_name, tools_spec)
    draft_store = _draft_store(orch)
    existing = await run_generation(
        draft_store.find_gap_draft,
        user_id,
        chat_id or "",
        fingerprint,
    )
    if existing:
        self_test = json.loads(existing.get("self_test") or "{}")
        card = creation_card(existing, self_test,
                             note="This capability is already staged — decide on the existing draft.")
        return MCPResponse(
            result={"status": "duplicate", "draft_id": existing["id"],
                    "draft_status": existing.get("status")},
            ui_components=[card],
        )

    lifecycle = orch.lifecycle_manager
    draft = await lifecycle.create_draft(
        user_id=user_id, agent_name=agent_name, description=description,
        tools_spec=[{"name": t.get("name", ""), "description": t.get("description", "")}
                    for t in tools_spec],
        origin="auto_chat",
        source_chat_id=chat_id or "",
        gap_fingerprint=fingerprint,
    )
    draft_id = draft["id"]
    await _audit(user_id, "lifecycle.gap_detected",
                 f"Capability gap: {agent_name} — auto-creating draft",
                 correlation_id=draft_id, outcome="in_progress", chat_id=chat_id,
                 inputs_meta={"gap_fingerprint": fingerprint, "draft_id": draft_id})

    draft = await lifecycle.generate_code(draft_id, websocket=websocket)
    if draft.get("status") in ("error", "rejected"):
        await _audit(user_id, "lifecycle.auto_created", "Generation failed",
                     correlation_id=draft_id, outcome="failure", chat_id=chat_id,
                     agent_id=None, inputs_meta={"draft_id": draft_id})
        card = creation_card(draft, {"status": "failed",
                                     "summary": draft.get("error_message") or "generation failed"},
                             note="Generation failed — Refine to retry with guidance, or Discard.")
        return MCPResponse(result={"status": "generation_failed", "draft_id": draft_id},
                           ui_components=[card])

    draft = await lifecycle.start_draft_agent(draft_id, websocket=websocket)
    launch_notice = (draft.get("confinement") or {}).get("notice", "")
    await _audit(user_id, "lifecycle.auto_created",
                 f"Auto-created draft '{agent_name}' ({draft_id})",
                 correlation_id=draft_id, chat_id=chat_id,
                 inputs_meta={"draft_id": draft_id, "gap_fingerprint": fingerprint})

    card = creation_card(
        await run_generation(draft_store.get_draft_agent, draft_id) or draft,
        {},
        note=launch_notice,
    )
    return MCPResponse(
        result={"status": "created", "draft_id": draft_id,
                "next": "user must approve, refine, or discard via the buttons"},
        ui_components=[card],
    )


def _live_agent_dir_and_draft(orch, agent_id: str):
    lifecycle = orch.lifecycle_manager
    row = lifecycle._get_draft_by_agent_id(agent_id)
    if not row or row.get("status") != "live":
        return None, None
    agent_dir = os.path.join(lifecycle._agents_dir, row["agent_slug"])
    return row, agent_dir


def _read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as stream:
        return stream.read()


def _stage_revision_code(lifecycle, rev, live_row, rev_dir, new_code):
    compile(new_code, "mcp_tools.py", "exec")
    report = lifecycle.security.analyze(
        new_code, filename=f"{rev['agent_slug']}/mcp_tools.py"
    )
    if blocks_execution(report):
        return report, None
    os.makedirs(rev_dir, exist_ok=True)
    with open(os.path.join(rev_dir, "mcp_tools.py"), "w", encoding="utf-8") as stream:
        stream.write(new_code)
    validation = lifecycle.validator.validate(
        new_code, rev["agent_slug"], lifecycle._agents_dir
    )
    return report, validation


def _gate_revision_code(lifecycle, rev, live_row, new_code):
    report = lifecycle.security.analyze(
        new_code, filename=f"{live_row['agent_slug']}/mcp_tools.py"
    )
    if blocks_execution(report):
        return report, None
    validation = lifecycle.validator.validate(
        new_code, rev["agent_slug"], lifecycle._agents_dir
    )
    return report, validation


async def _extend_agent(orch, args: Dict[str, Any], *, user_id: str,
                        chat_id: Optional[str], websocket=None) -> MCPResponse:
    agent_id = (args.get("agent_id") or "").strip()
    instruction = (args.get("instruction") or "").strip()
    if not agent_id or not instruction:
        return MCPResponse(error={"message": "extend_agent needs agent_id and instruction",
                                  "retryable": False})

    draft_store = _draft_store(orch)
    ownership = await run_generation(draft_store.get_agent_ownership, agent_id)
    user = await run_generation(draft_store.get_user, user_id) or {}
    owner_email = user.get("email", user_id)
    if not ownership or ownership.get("owner_email") not in (owner_email, user_id):
        return MCPResponse(
            result={"status": "not_owned", "agent_id": agent_id},
            ui_components=[_error_card(
                f"You don't own '{agent_id}', so it can't be extended. You can create a "
                f"new agent with this capability instead.")],
        )

    live_row, live_dir = await run_generation(
        _live_agent_dir_and_draft, orch, agent_id
    )
    if live_row is None or not await run_generation(os.path.isdir, live_dir):
        return MCPResponse(
            result={"status": "not_revisable", "agent_id": agent_id},
            ui_components=[_error_card(
                f"'{agent_id}' is not a lifecycle-managed agent, so it can't be revised "
                f"in place. Ask me to create a new agent with this capability instead.")],
        )

    fingerprint = gap_fingerprint(agent_id, extra=instruction)
    existing = await run_generation(
        draft_store.find_gap_draft, user_id, chat_id or "", fingerprint
    )
    if existing:
        self_test = json.loads(existing.get("self_test") or "{}")
        card = creation_card(existing, self_test, revision=True,
                             note="This revision is already staged — decide on it below.")
        return MCPResponse(result={"status": "duplicate", "draft_id": existing["id"]},
                           ui_components=[card])

    lifecycle = orch.lifecycle_manager
    rev = await lifecycle.create_draft(
        user_id=user_id,
        agent_name=f"{live_row['agent_name']} (revision)",
        description=f"Revision of {agent_id}: {instruction}",
        origin="revision",
        source_chat_id=chat_id or "",
        gap_fingerprint=fingerprint,
        revises_agent_id=agent_id,
    )
    rev_id = rev["id"]
    await _audit(user_id, "lifecycle.gap_detected",
                 f"Revision requested for {agent_id}: {instruction[:120]}",
                 correlation_id=rev_id, outcome="in_progress", chat_id=chat_id,
                 agent_id=agent_id, inputs_meta={"draft_id": rev_id, "revises_agent_id": agent_id})

    rev_dir = os.path.join(lifecycle._agents_dir, rev["agent_slug"])
    try:
        live_tools = os.path.join(live_dir, "mcp_tools.py")
        current_code = await run_generation(_read_text_file, live_tools)
        new_code = await lifecycle.generator.refine_tools_file(
            current_code=current_code, user_message=instruction,
            agent_name=live_row["agent_name"], description=live_row["description"])
        report, validation = await run_generation(
            _stage_revision_code,
            lifecycle,
            rev,
            live_row,
            rev_dir,
            new_code,
        )
        sec_blocker = getattr(report, "max_severity", None)
        sec_name = getattr(sec_blocker, "name", str(sec_blocker or "")).upper()
        passed = validation is not None and validation.passed
        validator_summary = (
            f"validator: {validation.tools_passed}/{validation.tools_tested} tools passed"
            if validation is not None
            else "validator: skipped (security gate refused execution)"
        )
        self_test = {
            "status": "passed" if passed else "failed",
            "summary": (f"{validator_summary}; "
                        f"security max severity: {sec_name or 'NONE'}"),
            "tools_called": [], "errors": [] if passed else ["gate checks failed"],
            "evidence": "", "tested_at": int(time.time() * 1000),
        }
        await run_generation(
            draft_store.update_draft_agent,
            rev_id,
            status="error" if validation is None else "generated",
            self_test=json.dumps(self_test),
            security_report=json.dumps(report.to_dict()),
            validation_report=json.dumps(
                validation.to_dict() if validation is not None else {"passed": False}
            ),
        )
    except Exception as exc:
        logger.exception("agentic: revision staging failed for %s", agent_id)
        await run_generation(
            draft_store.update_draft_agent,
            rev_id,
            status="error",
            error_message=str(exc)[:500],
        )
        await _audit(user_id, "lifecycle.auto_created", f"Revision staging failed: {exc}",
                     correlation_id=rev_id, outcome="failure", chat_id=chat_id, agent_id=agent_id)
        return MCPResponse(result={"status": "staging_failed", "draft_id": rev_id},
                           ui_components=[_error_card(
                               "Staging the revision failed. Refine with more detail or discard it.")])

    await _audit(user_id, "lifecycle.auto_created",
                 f"Staged revision {rev_id} for {agent_id}",
                 correlation_id=rev_id, chat_id=chat_id, agent_id=agent_id)
    await _audit(user_id, "lifecycle.self_test",
                 f"Revision gate-check {self_test['status']}: {self_test['summary']}",
                 correlation_id=rev_id,
                 outcome="success" if self_test["status"] == "passed" else "failure",
                 chat_id=chat_id, agent_id=agent_id)

    if validation is None:
        return MCPResponse(
            result={"status": "revision_refused", "draft_id": rev_id,
                    "security": sec_name or "NONE"},
            ui_components=[_error_card(
                f"The revised code was refused by the security gate "
                f"(max severity {sec_name or 'NONE'}) and nothing was staged. "
                "Re-run the change with a different instruction."
            )],
        )

    card = creation_card(
        await run_generation(draft_store.get_draft_agent, rev_id),
        self_test,
        revision=True,
    )
    return MCPResponse(
        result={"status": "revision_staged", "draft_id": rev_id,
                "self_test": self_test["status"],
                "next": "user must apply, refine, or discard via the buttons"},
        ui_components=[card],
    )


async def apply_revision(orch, rev: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    lifecycle = orch.lifecycle_manager
    draft_store = _draft_store(orch)
    agent_id = rev.get("revises_agent_id") or ""
    live_row, live_dir = await run_generation(
        _live_agent_dir_and_draft, orch, agent_id
    )
    if live_row is None:
        return {"applied": False, "detail": f"Live agent {agent_id} not found"}

    rev_dir = os.path.join(lifecycle._agents_dir, rev["agent_slug"])
    staged = os.path.join(rev_dir, "mcp_tools.py")
    if not await run_generation(os.path.exists, staged):
        return {"applied": False, "detail": "Staged revision file missing"}
    new_code = await run_generation(_read_text_file, staged)

    report, validation = await run_generation(
        _gate_revision_code, lifecycle, rev, live_row, new_code
    )
    sec_name = getattr(getattr(report, "max_severity", None), "name", "").upper()
    if validation is None or not validation.passed:
        validator_note = (
            f"{validation.tools_passed}/{validation.tools_tested}"
            if validation is not None
            else "skipped (security gate refused execution)"
        )
        await run_generation(
            draft_store.update_draft_agent,
            rev["id"],
            status="rejected",
            error_message=(
                f"Gate failed: security={sec_name or 'NONE'}, validator "
                f"{validator_note}"
            ),
        )
        await _audit(user_id, "lifecycle.rejected",
                     f"Revision {rev['id']} failed the gate — live agent unchanged",
                     correlation_id=rev["id"], outcome="failure", agent_id=agent_id)
        return {"applied": False,
                "detail": "Security/validation gate failed — the live agent is unchanged. "
                          "The revision stays editable (Refine) or can be discarded."}

    live_tools = os.path.join(live_dir, "mcp_tools.py")
    backup = live_tools + ".bak027"
    scopes_snapshot = {}
    try:
        scopes_snapshot = dict(
            await run_generation(
                orch.tool_permissions.get_agent_scopes, user_id, agent_id
            ) or {}
        )
    except Exception:
        logger.debug("agentic: scope snapshot failed", exc_info=True)

    try:
        await lifecycle.stop_draft_agent(live_row["id"])
        await run_generation(shutil.copy2, live_tools, backup)
        await run_generation(Path(live_tools).write_text, new_code, encoding="utf-8")
        await lifecycle.start_draft_agent(live_row["id"], align_scopes=False)
        await run_generation(
            draft_store.update_draft_agent,
            live_row["id"],
            status="live",
        )
    except Exception as exc:
        logger.exception("agentic: revision swap failed for %s — rolling back", agent_id)
        try:
            if await run_generation(os.path.exists, backup):
                await run_generation(shutil.copy2, backup, live_tools)
            await lifecycle.start_draft_agent(live_row["id"], align_scopes=False)
            await run_generation(
                draft_store.update_draft_agent,
                live_row["id"],
                status="live",
            )
        except Exception:
            logger.exception("agentic: rollback restart failed for %s", agent_id)
        await run_generation(
            draft_store.update_draft_agent,
            rev["id"],
            status="rejected",
            error_message=str(exc)[:500],
        )
        await _audit(user_id, "lifecycle.revision_rolled_back",
                     f"Revision {rev['id']} swap failed; backup restored",
                     correlation_id=rev["id"], outcome="failure", agent_id=agent_id)
        return {"applied": False, "detail": "Swap failed — the previous version was restored."}
    finally:
        try:
            if scopes_snapshot:
                await run_generation(
                    orch.tool_permissions.set_agent_scopes,
                    user_id,
                    agent_id,
                    scopes_snapshot,
                )
        except Exception:
            logger.debug("agentic: scope restore failed", exc_info=True)
        try:
            if await run_generation(os.path.exists, backup):
                await run_generation(os.remove, backup)
        except OSError:
            pass

    try:
        await lifecycle.delete_draft(rev["id"])
    except Exception:
        logger.warning("agentic: revision cleanup failed for %s", rev["id"], exc_info=True)
    try:
        from orchestrator import agent_trust
        await agent_trust.reset_on_revision(
            draft_store,
            agent_id,
            actor_user=user_id,
        )
    except Exception:
        logger.debug("agentic: safe-marker reset failed for %s", agent_id, exc_info=True)
    await _audit(user_id, "lifecycle.revision_applied",
                 f"Revision applied to {agent_id}",
                 correlation_id=rev["id"], agent_id=agent_id)
    return {"applied": True, "detail": f"Revision applied — {agent_id} restarted with the new tools."}


async def _owned_draft(
    orch, user_id: str, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    draft_id = str(payload.get("draft_id") or "")
    draft = (
        await run_generation(
            _draft_store(orch).get_owned_draft_agent,
            user_id,
            draft_id,
        )
        if draft_id else None
    )
    return draft


async def _decidable_draft(
    orch, user_id: str, roles, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    draft_id = str(payload.get("draft_id") or "")
    draft = (
        await run_generation(_draft_store(orch).get_draft_agent, draft_id)
        if draft_id else None
    )
    if not draft:
        return None
    if draft.get("user_id") == user_id:
        return draft
    if draft.get("origin") == "auto_attachment" and "admin" in (roles or []):
        return draft
    return None


async def _send_chat_card(orch, websocket, component: Dict[str, Any]):
    await orch.send_ui_render(websocket, [component], target="chat")


async def _replace_card_state(orch, websocket, user_id: str, draft_id: str,
                              card: Dict[str, Any]) -> None:
    try:
        chat_id = orch._ws_active_chat.get(id(websocket)) if websocket is not None else None
        if not chat_id:
            return
        card = dict(card)
        card["id"] = f"draft-card-{draft_id}"
        await orch._send_or_replace_components(websocket, [card], chat_id, user_id=user_id)
    except Exception:
        logger.debug("decision card replacement failed (non-fatal)", exc_info=True)


def _terminal_card(draft_id: str, title: str, message: str) -> Dict[str, Any]:
    return Card(id=f"draft-card-{draft_id}", title=title, content=[
        Text(content=message, variant="caption").to_dict(),
    ]).to_dict()


async def _promote_parser_global(orch, draft, agent_id, *, approved_by):
    try:
        from orchestrator import attachment_autoparse
        parser_repo = _parser_repository(orch)
        row = await run_generation(
            parser_repo.get_by_draft,
            draft["id"],
            for_administration=True,
        ) or {}
        gap = row.get("gap_fingerprint")
        extension = row.get("extension")
        requested_by = row.get("requested_by")
        tool_name = attachment_autoparse._tool_name_for(extension)

        try:
            await run_generation(
                _draft_store(orch).set_agent_visibility,
                agent_id,
                True,
            )
        except Exception:
            logger.debug("autoparse: set_agent_visibility failed", exc_info=True)
        if gap:
            await run_generation(
                parser_repo.mark_live,
                gap,
                live_agent_id=agent_id,
                tool_name=tool_name,
                approved_by=approved_by,
            )

        try:
            scopes = await run_generation(
                orch.tool_permissions.scopes_required_by_tools, agent_id
            ) or []
            grant = {s: True for s in scopes if s != "tools:write"}
            if grant and requested_by:
                await run_generation(
                    orch.tool_permissions.set_agent_scopes,
                    requested_by,
                    agent_id,
                    grant,
                )
        except Exception:
            logger.debug("autoparse: scope grant failed", exc_info=True)

        if requested_by:
            await attachment_autoparse._notify_user(
                orch, requested_by,
                f"The .{extension} reader is live — ask again to read your file.")
    except Exception:
        logger.exception("autoparse: global promotion failed for draft %s", draft.get("id"))


async def _h_draft_approve(orch, websocket, user_id, roles, payload):
    draft_id = str(payload.get("draft_id") or "")
    raw_draft = (
        await run_generation(_draft_store(orch).get_draft_agent, draft_id)
        if draft_id else None
    )
    is_autoparse = bool(raw_draft and raw_draft.get("origin") == "auto_attachment")

    if is_autoparse:
        if "admin" not in (roles or []):
            await _audit(user_id, "lifecycle.rejected",
                         f"Non-admin approval attempt on parser draft {draft_id}",
                         correlation_id=draft_id, outcome="failure")
            await _send_chat_card(orch, websocket, _error_card(
                "Approving an auto-created file parser requires the admin role."))
            return None
        draft = raw_draft
    else:
        draft = await _owned_draft(orch, user_id, payload)
        if draft is None:
            await _send_chat_card(orch, websocket, _error_card("Draft not found (it may have been discarded)."))
            return None

    from orchestrator import redteam
    if redteam.redteam_enabled():
        await _audit(user_id, "lifecycle.rejected",
                     f"Draft {draft['id']} approval refused: {redteam.UNAVAILABLE_CODE}",
                     correlation_id=draft["id"], outcome="failure")
        await _send_chat_card(orch, websocket, _error_card(
            "Approval is unavailable while the red-team gate is enabled, because the gate "
            f"can no longer run ({redteam.UNAVAILABLE_CODE}). Nothing went live."))
        return None

    result = await orch.lifecycle_manager.approve_agent(draft["id"], websocket=websocket)
    status = (result or {}).get("status")
    corr = draft["id"]
    if status == "live":
        agent_id = f"{draft['agent_slug'].replace('_', '-')}-1"
        if is_autoparse:
            await _promote_parser_global(orch, draft, agent_id, approved_by=user_id)
        try:
            approved_draft = await run_generation(
                _draft_store(orch).get_draft_agent,
                draft["id"],
            ) or draft
            approved_self_test = json.loads(approved_draft.get("self_test") or "{}")
            if approved_self_test.get("status") != "passed":
                approved_self_test = {"status": "passed"}
            await run_generation(
                _archive_on_success, orch, approved_draft, approved_self_test
            )
        except Exception:
            logger.debug("archive: approval-time record failed", exc_info=True)
        await _audit(user_id, "lifecycle.approved", f"Draft {draft['id']} approved → live",
                     correlation_id=corr, agent_id=agent_id)
        live_msg = ("Security checks passed. The parser is live and available to everyone — "
                    "re-upload or ask again to read that file type."
                    if is_autoparse else
                    "Security checks passed. The agent joined your fleet and is usable "
                    "right now — just ask again.")
        await _send_chat_card(orch, websocket, Card(title=f"{draft['agent_name']} is live", content=[
            Text(content=live_msg, variant="default").to_dict(),
        ]).to_dict())
        await _replace_card_state(orch, websocket, user_id, draft["id"], _terminal_card(
            draft["id"], f"✓ Approved: {draft['agent_name']}",
            "Approved and live — ask again to use it."))
    else:
        detail = (result or {}).get("error_message") or f"status: {status}"
        await _audit(user_id, "lifecycle.rejected", f"Draft {draft['id']} not promoted ({status})",
                     correlation_id=corr, outcome="failure")
        not_promoted = Card(title=f"{draft['agent_name']}: not promoted", content=[
            Text(content=f"The approval gate did not pass — {detail}. The draft stays "
                         f"editable: Refine it or Discard it.", variant="default").to_dict(),
        ] + _decision_buttons(draft["id"])).to_dict()
        await _send_chat_card(orch, websocket, not_promoted)
        await _replace_card_state(orch, websocket, user_id, draft["id"], not_promoted)
    return None


async def _h_draft_refine(orch, websocket, user_id, roles, payload):
    draft = await _decidable_draft(orch, user_id, roles, payload)
    if draft is None:
        await _send_chat_card(orch, websocket, _error_card("Draft not found (it may have been discarded)."))
        return None
    message = str(payload.get("message") or (payload.get("fields") or {}).get("message") or "").strip()
    if not message:
        await _send_chat_card(orch, websocket, Card(title=f"Refine {draft['agent_name']}", content=[
            {"type": "param_picker", "title": "What should change?",
             "fields": [{"name": "message", "kind": "text", "label": "Describe the fix/change"}],
             "submit_label": "Refine",
             "submit_message_template": f"Refine draft {draft['id']}: {{message}}"},
        ]).to_dict())
        return None
    result = await orch.lifecycle_manager.refine_agent(draft["id"], message, websocket=websocket)
    await _audit(user_id, "lifecycle.refined", f"Draft {draft['id']} refined",
                 correlation_id=draft["id"])
    note = ("Refined. Review it, then Approve / Discard."
            if result.get("status") != "error"
            else f"Refine failed: {result.get('error_message', 'unknown error')}")
    latest = await run_generation(
        _draft_store(orch).get_draft_agent,
        draft["id"],
    ) or {}
    self_test = json.loads(latest.get("self_test") or "{}")
    refreshed = creation_card(
        latest or draft, self_test,
        revision=bool(draft.get("revises_agent_id")), note=note)
    await _send_chat_card(orch, websocket, refreshed)
    await _replace_card_state(orch, websocket, user_id, draft["id"], refreshed)
    return None


async def _h_draft_discard(orch, websocket, user_id, roles, payload):
    draft = await _decidable_draft(orch, user_id, roles, payload)
    if draft is None:
        await _send_chat_card(orch, websocket, _error_card("Draft not found (already discarded?)."))
        return None
    if draft.get("origin") == "auto_attachment":
        try:
            from orchestrator.attachments.parser_repo import STATUS_DISCARDED

            _pr = _parser_repository(orch)
            _row = await run_generation(
                _pr.get_by_draft,
                draft["id"],
                owner_user_id=user_id,
            )
            if _row:
                await run_generation(
                    _pr.mark_status,
                    _row["gap_fingerprint"],
                    STATUS_DISCARDED,
                    owner_user_id=user_id,
                )
        except Exception:
            logger.debug("autoparse: discard registry update failed", exc_info=True)
    await orch.lifecycle_manager.delete_draft(draft["id"])
    await _audit(user_id, "lifecycle.discarded", f"Draft {draft['id']} discarded",
                 correlation_id=draft["id"])
    await _send_chat_card(orch, websocket, Card(title="Draft discarded", content=[
        Text(content=f"'{draft['agent_name']}' was removed. I'll answer with existing "
                     f"capabilities where I can.", variant="default").to_dict(),
    ]).to_dict())
    await _replace_card_state(orch, websocket, user_id, draft["id"], _terminal_card(
        draft["id"], f"Discarded: {draft['agent_name']}",
        "This draft was removed — nothing went live."))
    return None


async def _h_revision_apply(orch, websocket, user_id, roles, payload):
    rev = await _owned_draft(orch, user_id, payload)
    if rev is None or not rev.get("revises_agent_id"):
        await _send_chat_card(orch, websocket, _error_card("Revision not found."))
        return None
    outcome = await apply_revision(orch, rev, user_id)
    if outcome["applied"]:
        await _send_chat_card(orch, websocket, Card(title="Revision applied", content=[
            Text(content=outcome["detail"], variant="default").to_dict()]).to_dict())
        await _replace_card_state(orch, websocket, user_id, rev["id"], _terminal_card(
            rev["id"], "✓ Revision applied", outcome["detail"]))
    else:
        not_applied = Card(title="Revision not applied", content=[
            Text(content=outcome["detail"], variant="default").to_dict(),
        ] + _decision_buttons(rev["id"], revision=True)).to_dict()
        await _send_chat_card(orch, websocket, not_applied)
        await _replace_card_state(orch, websocket, user_id, rev["id"], not_applied)
    return None


async def _h_revision_discard(orch, websocket, user_id, roles, payload):
    return await _h_draft_discard(orch, websocket, user_id, roles, payload)


HANDLERS = {
    "draft_approve": _h_draft_approve,
    "draft_refine": _h_draft_refine,
    "draft_discard": _h_draft_discard,
    "revision_apply": _h_revision_apply,
    "revision_discard": _h_revision_discard,
}
