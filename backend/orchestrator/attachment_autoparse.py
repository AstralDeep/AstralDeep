"""Eager, safe auto-creation of a backend parser for an uncovered file type.

Feature 031-attachment-upload-parsing (User Story 2). When an accepted file
type has no built-in or globally-promoted parser, uploading one *eagerly*
drafts a parser by reusing the feature-027 agentic-creation lifecycle
(draft -> security gate -> isolated VirtualWebSocket self-test -> ADMIN
approval -> global promotion). This module is the programmatic, format-seeded
entry point — it does NOT rely on the LLM deciding to call ``create_capability``
during a chat turn.

Lifecycle contract: specs/031-attachment-upload-parsing/contracts/parser-autocreate.md
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Optional, TypedDict

logger = logging.getLogger("attachment_autoparse")


def _plane(orch):
    composition = getattr(getattr(orch, "runtime_composition", None), "plane", None)
    if composition is None:
        raise RuntimeError("attachment autoparse requires the application Plane runtime")
    return composition


def _bind_attachment_provenance(
    plane,
    *,
    owner_id: str,
    draft_id: str,
    source_chat_id: str,
    gap_fingerprint: str,
    source_attachment_id: str,
) -> None:
    with plane.runtime.transaction() as transaction:
        current = plane.repositories.draft_agents.get_draft(
            transaction,
            owner_id=owner_id,
            draft_id=draft_id,
            for_update=True,
        )
        if current is None:
            raise LookupError("autoparse draft is unavailable")
        plane.repositories.draft_agents.bind_attachment_provenance(
            transaction,
            owner_id=owner_id,
            draft_id=draft_id,
            expected_revision=current.state_revision,
            source_chat_id=source_chat_id,
            gap_fingerprint=gap_fingerprint,
            source_attachment_id=source_attachment_id,
            updated_at=int(time.time() * 1000),
        )


def _store_self_test(
    plane,
    *,
    owner_id: str,
    draft_id: str,
    self_test: str,
) -> None:
    with plane.runtime.transaction() as transaction:
        current = plane.repositories.draft_agents.get_draft(
            transaction,
            owner_id=owner_id,
            draft_id=draft_id,
            for_update=True,
        )
        if current is None:
            raise LookupError("autoparse draft is unavailable")
        plane.repositories.draft_agents.compare_and_set_draft(
            transaction,
            owner_id=owner_id,
            draft_id=draft_id,
            expected_revision=current.state_revision,
            updates={"self_test": self_test},
            updated_at=int(time.time() * 1000),
        )


class CoverageStatus(TypedDict):
    status: str  # "covered" | "preparing" | "pending_admin_approval" | "unavailable"
    gap_fingerprint: Optional[str]


def _tool_name_for(extension: Optional[str]) -> str:
    """Deterministic, identifier-safe parser tool name for *extension*."""
    ext = re.sub(r"[^a-z0-9]+", "_", (extension or "file").lower()).strip("_") or "file"
    return f"parse_{ext}"


def coverage_status(orch, *, extension: Optional[str], category: str) -> CoverageStatus:
    """Resolve the parser-coverage status for a type without side effects.

    Used by the upload endpoint to decide the ``parser_status`` it returns and
    whether to enqueue a background ``start``. Honors the feature flag and the
    existing registry (dedup): a ``live`` row ⇒ covered, a ``pending`` row ⇒
    awaiting admin (no new draft).
    """
    from orchestrator import parser_registry
    from orchestrator.attachments.parser_repo import AttachmentParserRepository
    from shared.feature_flags import flags

    plane = _plane(orch)
    parser_repo = AttachmentParserRepository.from_plane_source(plane)
    fp = parser_registry.gap_fingerprint(category, extension)
    if parser_registry.coverage(extension, category, parser_repo=parser_repo)["covered"]:
        return {"status": "covered", "gap_fingerprint": fp}
    if not flags.is_enabled("attachment_autoparse"):
        return {"status": "unavailable", "gap_fingerprint": fp}
    existing = parser_repo.get_by_gap(fp)
    if existing:
        if existing["status"] == "live":
            return {"status": "covered", "gap_fingerprint": fp}
        if existing["status"] == "pending":
            return {"status": "pending_admin_approval", "gap_fingerprint": fp}
        # failed / discarded → a later upload may re-attempt.
    return {"status": "preparing", "gap_fingerprint": fp}


async def _notify_user(orch, user_id: str, message: str, chat_id: Optional[str] = None) -> None:
    """Best-effort status toast to all of *user_id*'s connected UI sockets.

    The upload is chat-agnostic, so absent a chat_id we notify every socket the
    user has open. Never raises.
    """
    try:
        clients = list(getattr(orch, "ui_clients", []) or [])
    except Exception:
        clients = []
    payload = json.dumps({"type": "chat_status", "status": "info", "message": message})
    for client in clients:
        try:
            if orch._get_user_id(client) != user_id:
                continue
            if chat_id is not None:
                active = orch._ws_active_chat.get(id(client)) if hasattr(orch, "_ws_active_chat") else None
                if active and active != chat_id:
                    continue
            await orch._safe_send(client, payload)
        except Exception:
            logger.debug("autoparse notify failed for one socket", exc_info=True)


async def auto_continue_after_go_live(orch, *, requested_by: Optional[str],
                                      source_chat_id: Optional[str],
                                      source_attachment_id: Optional[str],
                                      extension: Optional[str],
                                      category: Optional[str]) -> bool:
    """031 T031 — auto-continue the originating turn once the parser is live.

    Recovers the uploader's ORIGINAL request (history stores the un-augmented
    user text) for the turn that carried ``source_attachment_id`` and replays it
    in-process via a ``VirtualWebSocket`` so the parsed result persists into the
    original chat (seen on next open). Best-effort; returns True if a replay was
    dispatched, False otherwise. Never raises.
    """
    if not (requested_by and source_chat_id and source_attachment_id):
        return False
    try:
        plane = _plane(orch)

        def _load_original_turn():
            with plane.runtime.transaction() as transaction:
                links = plane.repositories.artifacts.message_attachments.list_for_conversation(
                    transaction,
                    owner_id=requested_by,
                    conversation_id=source_chat_id,
                )
                link = next(
                    (
                        candidate
                        for candidate in links
                        if candidate.attachment_id == source_attachment_id
                        and candidate.message_id is not None
                    ),
                    None,
                )
                if link is None:
                    return None
                try:
                    message_id = int(link.message_id)
                except (TypeError, ValueError):
                    return None
                message = plane.repositories.history.messages.get(
                    transaction,
                    owner_id=requested_by,
                    conversation_id=source_chat_id,
                    message_id=message_id,
                )
                attachment = plane.repositories.artifacts.attachments.get(
                    transaction,
                    owner_id=requested_by,
                    attachment_id=source_attachment_id,
                )
                if message is None or attachment is None:
                    return None
                return message.content, attachment

        loaded = await asyncio.to_thread(_load_original_turn)
        if loaded is None:
            return False
        original, attachment = loaded
        if not isinstance(original, str) or not original.strip():
            return False

        # Replay the original turn in-process; the parser is now live so the
        # attachment block resolves to ``covered`` and the reader tool runs.
        from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
        bg = BackgroundTask(task_id=f"autocont-{source_attachment_id[:8]}",
                            chat_id=source_chat_id, user_id=requested_by)
        vws = VirtualWebSocket(bg)
        # 056 US2 (FR-012): the replay is a machine turn — derive its authority
        # at the SAME shared seam scheduled runs use, so a real-agent reader
        # tool dispatches delegated under the uploader's standing consent in
        # production instead of being refused fail-closed. No derivable
        # authority is NOT fatal here: the replay still runs (dev posture and
        # non-agent paths are unchanged), and production simply refuses its
        # real-agent dispatches exactly as it does today.
        from orchestrator.chain_authority import AuthoritySkip
        authority = await orch.derive_machine_authority(
            user_id=requested_by, agent_id=None, turn_class="parser_replay")
        if not isinstance(authority, AuthoritySkip):
            orch._bind_machine_turn(vws, authority)
        try:
            await orch.handle_chat_message(
                vws, str(original), source_chat_id, user_id=requested_by,
                attachments=[{"attachment_id": source_attachment_id,
                              "filename": attachment.filename,
                              "category": attachment.category}],
            )
        finally:
            orch._unbind_machine_turn(vws)
            try:
                await vws.close()
            except Exception:  # pragma: no cover - close is best-effort
                pass
        logger.info("autoparse.auto_continued",
                    extra={"user_id": requested_by, "chat_id": source_chat_id,
                           "attachment_id": source_attachment_id, "extension": extension,
                           "delegated": not isinstance(authority, AuthoritySkip)})
        return True
    except Exception:
        logger.debug("autoparse: auto-continue failed (non-fatal)", exc_info=True)
        return False


async def start(orch, attachment, *, user_id: str, chat_id: Optional[str] = None) -> CoverageStatus:
    """Eagerly draft a parser for an uncovered uploaded *attachment*.

    Reuses the 027 lifecycle primitives directly (no LLM "should I" decision):
    register intent (dedup-safe), create+generate+self-test a draft parser
    against the uploaded file, leave it ``pending`` for ADMIN approval. Returns
    the resulting ``parser_status``. Never raises into the caller.
    """
    from orchestrator import agentic_creation, parser_registry
    from orchestrator.attachments.parser_repo import (
        AttachmentParserRepository, STATUS_FAILED,
    )

    extension = getattr(attachment, "extension", None)
    category = getattr(attachment, "category", "") or ""
    attachment_id = getattr(attachment, "attachment_id", None)
    filename = getattr(attachment, "filename", "") or ""
    fp = parser_registry.gap_fingerprint(category, extension)
    plane = _plane(orch)
    parser_repo = AttachmentParserRepository.from_plane_source(plane)

    # Dedup (FR-018): a pending/live row for this gap means no new draft.
    existing = parser_repo.get_by_gap(fp)
    if existing and existing["status"] in ("pending", "live"):
        return {"status": "pending_admin_approval" if existing["status"] == "pending" else "covered",
                "gap_fingerprint": fp}

    tool_name = _tool_name_for(extension)
    agent_name = f"{(extension or 'file').upper()} Parser"
    description = (
        f"Reads .{extension} ({category}) file attachments the user has uploaded and "
        f"extracts their text/structured content. Use only the Python standard library "
        f"and already-installed packages; if the format needs an unavailable library, do a "
        f"best-effort structural extraction (e.g. zip/XML, tarfile) and state the limitation."
    )
    tool_desc = (
        f"Read a .{extension} file attachment by attachment_id and return its extracted "
        f"text/structured content (best-effort, standard-library only)."
    )

    lifecycle = orch.lifecycle_manager
    try:
        draft = await lifecycle.create_draft(
            user_id=user_id, agent_name=agent_name, description=description,
            tools_spec=[{"name": tool_name, "description": tool_desc}],
        )
    except Exception:
        logger.exception("autoparse: create_draft failed for .%s", extension)
        return {"status": "unavailable", "gap_fingerprint": fp}
    draft_id = draft["id"]

    # Record provenance + register the (dedup-keyed) registry row.
    try:
        if not isinstance(attachment_id, str) or not attachment_id:
            raise ValueError("autoparse attachment identity is unavailable")
        await asyncio.to_thread(
            _bind_attachment_provenance,
            plane,
            owner_id=user_id,
            draft_id=draft_id,
            source_chat_id=chat_id or "",
            gap_fingerprint=fp,
            source_attachment_id=attachment_id,
        )
    except Exception:
        logger.debug("autoparse: draft provenance binding failed", exc_info=True)
    try:
        parser_repo.create_pending(
            gap_fingerprint=fp, category=category, extension=extension,
            draft_agent_id=draft_id, source_attachment_id=attachment_id,
            source_chat_id=chat_id, requested_by=user_id,
        )
    except Exception:
        logger.debug("autoparse: registry create_pending failed", exc_info=True)

    await agentic_creation._audit(
        user_id, "lifecycle.gap_detected",
        f"Unparseable upload .{extension} — auto-creating parser draft",
        correlation_id=draft_id, outcome="in_progress", chat_id=chat_id,
        inputs_meta={"extension": extension, "category": category,
                     "attachment_id": attachment_id, "gap_fingerprint": fp,
                     "trigger": "upload", "draft_id": draft_id},
    )

    # Generate → start → self-test against THE UPLOADED FILE (≤1 auto-refine).
    user_request = (
        f"Use the {tool_name} tool to read the attached .{extension} file and "
        f"summarize what it contains."
    )
    test_attachments = [{"attachment_id": attachment_id, "filename": filename, "category": category}]
    try:
        draft = await lifecycle.generate_code(draft_id)
        if (draft or {}).get("status") in ("error", "rejected"):
            parser_repo.mark_status(
                fp,
                STATUS_FAILED,
                owner_user_id=user_id,
            )
            await agentic_creation._audit(
                user_id, "lifecycle.auto_created", "Parser generation failed",
                correlation_id=draft_id, outcome="failure", chat_id=chat_id,
                inputs_meta={"draft_id": draft_id})
            await _notify_user(orch, user_id,
                               f"Couldn't prepare a reader for .{extension} files.", chat_id)
            return {"status": "unavailable", "gap_fingerprint": fp}

        draft = await lifecycle.start_draft_agent(draft_id)
        self_test = await agentic_creation._self_test_draft(
            orch, draft, user_request, user_id, attachments=test_attachments)
        refines = 0
        while self_test.get("status") != "passed" and refines < agentic_creation.SELF_TEST_MAX_AUTO_REFINES:
            refines += 1
            failure = "; ".join(self_test.get("errors") or [self_test.get("summary", "failed")])
            draft = await lifecycle.refine_agent(
                draft_id,
                f"The self-test failed: {failure}. Fix {tool_name} so it reads the "
                f".{extension} file and returns its content (standard library only).")
            if (draft or {}).get("status") == "error":
                break
            draft = await lifecycle.start_draft_agent(draft_id)
            self_test = await agentic_creation._self_test_draft(
                orch, draft, user_request, user_id, attachments=test_attachments)
        self_test["auto_refines"] = refines
        await asyncio.to_thread(
            _store_self_test,
            plane,
            owner_id=user_id,
            draft_id=draft_id,
            self_test=json.dumps(self_test),
        )
        await agentic_creation._audit(
            user_id, "lifecycle.auto_created",
            f"Auto-created parser draft '{agent_name}' ({draft_id})",
            correlation_id=draft_id, chat_id=chat_id,
            inputs_meta={"draft_id": draft_id, "gap_fingerprint": fp})
        await agentic_creation._audit(
            user_id, "lifecycle.self_test",
            f"Parser self-test {self_test.get('status')}: {self_test.get('summary', '')}",
            correlation_id=draft_id,
            outcome="success" if self_test.get("status") == "passed" else "failure",
            chat_id=chat_id, inputs_meta={"draft_id": draft_id})
    except Exception as exc:
        # Feature 054 (FR-020): codegen runs on the admin system credential;
        # its absence is an expected, honest degradation — log it by name so
        # operators can distinguish "configure the System LLM" from a bug.
        if "LLM not configured" in str(exc):
            logger.warning(
                "system_llm_unconfigured: autoparse skipped for .%s — "
                "configure the System LLM in admin settings", extension)
        else:
            logger.exception("autoparse: draft pipeline failed for .%s", extension)
        parser_repo.mark_status(
            fp,
            STATUS_FAILED,
            owner_user_id=user_id,
        )
        await _notify_user(orch, user_id,
                           f"Couldn't prepare a reader for .{extension} files.", chat_id)
        return {"status": "unavailable", "gap_fingerprint": fp}

    await _notify_user(
        orch, user_id,
        f"No reader exists for .{extension} files yet — a parser is being prepared "
        f"and is pending admin approval.", chat_id)
    return {"status": "pending_admin_approval", "gap_fingerprint": fp}


__all__ = ["CoverageStatus", "coverage_status", "start"]
