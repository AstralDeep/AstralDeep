"""Durable single-use approval cards for destructive GaiaKeep, remote-compute and computer-use
operations. The dispatch gate calls evaluate(); approvals re-enter it via
handle_decision(), stored through astralplane's remote_proposals.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextvars
from contextlib import contextmanager
import copy
from dataclasses import dataclass, replace
import hashlib
import json
import logging
import time
import uuid
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple

from astralplane.repositories.remote_proposals import RemoteOperationProposalRecord
from orchestrator.computer_hosts import ComputerHostError
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    plane_source_from_orchestrator,
    repository_from,
)

logger = logging.getLogger("RemoteConfirmation")

PROPOSAL_TTL_S = 900

MUTATING_AGENT_ID = "remote-compute-1"

DESTRUCTIVE_CLASSIFICATION: Dict[str, Any] = {
    "make_directory": "never",
    "upload_file": "if_exists",
    "remove_path": "always",
    "cancel_job": "always",
    "signal_process": "always",
    "control_service": {"by_action": ["stop", "disable", "restart"]},
    "manage_package": {"by_action": ["remove"]},
    "submit_job": "never",
    "run_job": "never",
}

_MARKER = "_remote_op_proposal_id"

_PENDING_CARDS: Dict[tuple, tuple] = {}


_RETRY_GRACE: Dict[Tuple[Any, ...], Tuple[str, float]] = {}


_SOURCE_PROPOSAL = contextvars.ContextVar("source_review_remote_proposal", default=None)


class Refusal(tuple):
    __slots__ = ()


@contextmanager
def source_review_scope(orch, row):
    token = _SOURCE_PROPOSAL.set((orch, row.proposal_id, row.owner_id, row.conversation_id,
                                  row.agent_id, row.tool_name, row.args_fingerprint))
    try:
        yield
    finally:
        _SOURCE_PROPOSAL.reset(token)


def source_review_proposal(orch, owner, chat, agent, tool, args):
    from orchestrator.hitl_confirmation import reviewed_remote_proposal

    value = _SOURCE_PROPOSAL.get()
    if (value is not None and value[0] is orch
            and value[2:] == (owner, chat, agent, tool, _fingerprint(args))):
        return value[1]
    return reviewed_remote_proposal(orch, owner, chat, agent, tool, args)


def _forget_pending(proposal_id: str) -> None:
    for key, (pid, _exp) in list(_PENDING_CARDS.items()):
        if pid == proposal_id:
            _PENDING_CARDS.pop(key, None)


class AgentConfirmationPolicy:
    def __init__(self, *, agent_id: str, classification: Dict[str, Any], machine_key: str,
                 gate_unclassified_unattended: bool, unattended_allowed: frozenset,
                 card_title: str, card_caption: str, summary, machine_label,
                 is_destructive=None, refusal_text: Optional[str] = None,
                 auto_continue: bool = False, dedupe_pending: bool = False,
                 machine_id=None, card_as_result: bool = False, on_approved=None,
                 retry_grace_s: float = 0.0):
        self.agent_id = agent_id
        self.classification = classification
        self.machine_key = machine_key
        self.gate_unclassified_unattended = gate_unclassified_unattended
        self.unattended_allowed = unattended_allowed
        self.card_title = card_title
        self.card_caption = card_caption
        self.summary = summary
        self.machine_label = machine_label
        self.is_destructive = is_destructive
        self.refusal_text = refusal_text or "confirmation_required: approve the operation to proceed."
        self.auto_continue = auto_continue
        self.dedupe_pending = dedupe_pending
        self.machine_id = machine_id
        self.card_as_result = card_as_result
        self.on_approved = on_approved
        self.retry_grace_s = float(retry_grace_s or 0.0)


def _computer_policy():
    from orchestrator import computer_use_policy

    return computer_use_policy


def _computer_use_label(orch, user_id: str, ref) -> str:
    try:
        registry = getattr(orch, "computer_hosts", None)
        if registry is not None:
            return registry.resolve(user_id, ref).name
    except Exception:  # noqa: BLE001
        pass
    return "your computer"


def _computer_use_machine_id(orch, user_id: str, args: Dict[str, Any]) -> str:
    try:
        registry = getattr(orch, "computer_hosts", None)
        if registry is not None:
            return registry.resolve(user_id, args.get("computer")).host_id
    except Exception:  # noqa: BLE001
        pass
    return str(args.get("computer") or "unresolved")


@dataclass(frozen=True)
class ComputerApproval:
    owner: str
    chat: str
    websocket: object
    host: object
    session: object
    generation: int
    task: object = None
    controller_context: object = None
    connection_generation: object = None


def _resolve_command_folder(orch, user_id, chat_id, args):
    from astralprims import Alert

    try:
        host = computer_card_host(orch, user_id, chat_id, args)
    except ComputerHostError:
        return None
    policy = _computer_policy()
    try:
        args["cwd"] = policy.resolve_cwd(args.get("cwd"), host.workspace_roots, host.name)
    except policy.CwdRefusal as exc:
        _audit_sync(user_id, "remote_op.refused_cwd", "refused run_command outside the shared folders",
                    machine_id=host.host_id, verb="run_command", outcome="failure", chat_id=chat_id)
        return Refusal((f"{exc.code}: {exc} Nothing was run.",
                        [Alert(message=f"That folder is not shared from {host.name}; nothing was run.",
                               variant="error").to_dict()]))
    return None


def _resolve_file_folder(orch, user_id, chat_id, tool_name, args):
    from astralprims import Alert

    try:
        host = computer_card_host(orch, user_id, chat_id, args)
    except ComputerHostError:
        return None
    if host.platform != "macos":
        return None
    policy = _computer_policy()
    try:
        args["path"] = policy.resolve_workspace_path(args.get("path"), host.workspace_roots, host.name)
    except policy.CwdRefusal as exc:
        _audit_sync(user_id, "remote_op.refused_path", "refused file operation outside the shared folders",
                    machine_id=host.host_id, verb=tool_name, outcome="failure", chat_id=chat_id)
        return Refusal((f"{exc.code}: {exc} Nothing was run.",
                        [Alert(message=str(exc), variant="error").to_dict()]))
    return None


def computer_card_host(orch, owner, chat, args):
    sessions = getattr(orch, "computer_sessions", None)
    if sessions is None:
        raise ComputerHostError("computer_unavailable", "remote control is not enabled on this server")
    return sessions.resolve_host(owner, chat, args.get("computer"))


def capture_computer_approval(orch, websocket, owner, chat, args):
    try:
        host = computer_card_host(orch, owner, chat, args)
        session = orch.computer_sessions.live_for_host(owner, host.host_id)
        if session is None:
            return None
        contexts = getattr(orch, "_connection_contexts", None)
        context = contexts.get(id(websocket)) if isinstance(contexts, dict) else None
        if context is None:
            return None
        authority = ComputerApproval(owner, chat, websocket, host, session, session.authority_generation,
            getattr(orch, "_hitl_task_origins", {}).get((owner, chat)), context,
            getattr(context, "connection_generation", None))
        if session.state == "paused" and session.pause_reason == "approval_not_completed":
            authority = getattr(session, "approval_pause_authority", None)
            return authority if computer_approval_current(orch, websocket, authority, allow_local_pause=True) else None
        return authority if computer_approval_current(orch, websocket, authority) else None
    except (AttributeError, ComputerHostError):
        return None


def computer_approval_current(orch, websocket, authority, *, allow_local_pause=False) -> bool:
    if not isinstance(authority, ComputerApproval):
        return False
    claims = orch.ui_sessions.get(websocket) or {}
    contexts = getattr(orch, "_connection_contexts", None)
    context = authority.controller_context
    if (not isinstance(contexts, dict) or context is None or contexts.get(id(websocket)) is not context
            or context.websocket is not websocket
            or not context.registered or context.closing or context.connection_generation != authority.connection_generation):
        return False
    session = authority.session
    state_current = (session.state == "active" and session.authority_generation == authority.generation)
    if allow_local_pause:
        recoverable_pause = (session.pause_reason == "local_input"
                             or session.pause_reason == "approval_not_completed" and authority.task is not None
                             and getattr(session, "approval_pause_authority", None) is authority)
        state_current = state_current or (session.state == "paused" and recoverable_pause
                                         and session.authority_generation == authority.generation + 1)
    return (authority.websocket is websocket and claims.get("sub") == authority.owner
            and not _no_live_human(orch, websocket)
            and getattr(orch, "_ws_active_chat", {}).get(id(websocket)) == authority.chat
            and not getattr(orch, "cancelled_sessions", {}).get(id(websocket), False)
            and (authority.task is None
                 or getattr(orch, "_hitl_task_origins", {}).get((authority.owner, authority.chat)) is authority.task)
            and orch.computer_hosts.get(authority.owner, authority.host.host_id) is authority.host
            and orch.computer_sessions.get(session.session_id) is session
            and session.owner_sub == authority.owner and session.chat_id == authority.chat
            and session.controller_ws_id == id(websocket) and state_current)


def computer_resume_target(websocket, authority, tool, args) -> str | None:
    from orchestrator import computer_use_policy

    if (not isinstance(authority, ComputerApproval) or authority.host.platform != "macos"
            or authority.host.websocket is not websocket):
        return None
    hwnd = args.get("hwnd")
    if tool in computer_use_policy.INPUT_VERBS - {"open_app", "set_clipboard", "focus_window"}:
        if type(hwnd) is not int or not 1 <= hwnd <= 2**53:
            return None
        label = authority.session.window_labels.get(hwnd)
        return f"{label} (window {hwnd})" if label else f"window {hwnd}"
    if (tool == "run_command" and authority.session.pause_reason == "approval_not_completed"
            and isinstance(args.get("cwd"), str)):
        return args["cwd"][:200]
    if tool == "open_app":
        try:
            return computer_use_policy.mac_application(args.get("app"), args.get("args", []))
        except ValueError:
            return None
    if tool == "confirm_action":
        try:
            return computer_use_policy.mac_terminal_application(args.get("terminal_app"))
        except ValueError:
            return None
    path = args.get("path")
    if tool in {"write_file", "delete_path"} and isinstance(path, str) and path.startswith("/") and "\x00" not in path:
        return path[:200]
    return None


async def resume_computer_approval(orch, websocket, authority, tool, args):
    if (computer_resume_target(websocket, authority, tool, args) is None
            or not computer_approval_current(orch, websocket, authority, allow_local_pause=True)):
        return None
    generation = authority.generation + (2 if authority.session.state == "paused" else 0)
    await orch.computer_sessions.resume(authority.session, local=True)
    resumed = replace(authority, generation=generation)
    return resumed if computer_approval_current(orch, websocket, resumed) else None


async def pause_failed_computer_approval(orch, websocket, authority):
    if computer_approval_current(orch, websocket, authority):
        await orch.computer_sessions.pause(authority.session, "approval_not_completed")
        if authority.session.authority_generation == authority.generation + 1:
            authority.session.approval_pause_authority = authority


def _computer_use_on_approved(orch, row, websocket=None, authority=None) -> bool:
    from orchestrator import computer_use_policy
    if not computer_approval_current(orch, websocket, authority):
        return False
    if (row.owner_id != authority.owner or row.conversation_id != authority.chat
            or row.machine_id != authority.host.host_id):
        return False
    args = row.arguments
    app = args.get("app") if row.tool_name == "open_app" else args.get("terminal_app")
    if row.tool_name not in {"open_app", "confirm_action"} or not computer_use_policy.is_shell_app(app):
        return False
    if row.tool_name == "confirm_action" and (
            not isinstance(args.get("summary"), str) or not 0 < len(args["summary"]) <= 400):
        return False
    if authority.host.platform == "macos":
        try:
            computer_use_policy.mac_application(app, args.get("args", []))
            computer_use_policy.mac_terminal_application(app, allow_path=row.tool_name == "open_app")
        except ValueError:
            return False
    authority.session.grant_terminal(computer_use_policy.TERMINAL_GRANT_S)
    return True


def _computer_use_summary(orch, user_id: str, tool_name: str, args: Dict[str, Any]) -> str:
    from orchestrator import computer_use_policy
    return computer_use_policy.summary_for(
        tool_name, args, _computer_use_label(orch, user_id, args.get("computer")))


def _cresco_label(orch, user_id: str, ref) -> str:
    return str(ref or "the approved fabric target")[:80]


def _cresco_summary(orch, user_id: str, tool_name: str, args: dict[str, Any]) -> str:
    from shared.phi_redactor import redact

    public = {k: v for k, v in args.items() if not str(k).startswith("_") and k != "user_id"}
    preview, _ = redact(public, kind="args")
    return f"{tool_name} on {_cresco_label(orch, user_id, args.get('target'))}: {preview}"


def _terminal_label(orch, user_id: str, ref) -> str:
    return "terminal session"


def _terminal_summary(orch, user_id: str, tool_name: str, args: dict[str, Any]) -> str:
    from shared.phi_redactor import redact

    public = {key: value for key, value in args.items()
              if not str(key).startswith("_") and key not in {"user_id", "runtime_binding"}}
    target = "a terminal session"
    if args.get("runtime_binding"):
        target = orch.terminal_sessions.runtime_preview(
            user_id, runtime_binding=args["runtime_binding"], session_id=args.get("session_id"))
    if "data_b64" in public:
        try:
            encoded = public.pop("data_b64")
            if not isinstance(encoded, str) or len(encoded) > 21848:
                raise ValueError()
            raw = base64.b64decode(encoded, validate=True)
            if not 1 <= len(raw) <= 16384:
                raise ValueError()
            public.update(input_bytes=len(raw), input_sha256=hashlib.sha256(raw).hexdigest())
            try:
                public["input_text"] = raw.decode("utf-8")
            except UnicodeError:
                public["input_base64"] = base64.b64encode(raw).decode("ascii")
        except (ValueError, TypeError, binascii.Error):
            from orchestrator.terminal_security import TerminalError

            raise TerminalError("terminal_input_invalid", 422) from None
    if "data" in public and (not isinstance(public["data"], str)
            or not 1 <= len(public["data"].encode("utf-8")) <= 16384):
        from orchestrator.terminal_security import TerminalError

        raise TerminalError("terminal_input_invalid", 422)
    preview, _ = redact(public, kind="args")
    return f"{tool_name} on {target}: {preview}"


def _terminal_machine_id(orch, user_id: str, args: dict[str, Any]) -> str:
    return orch.terminal_sessions.runtime_machine_id(
        user_id, runtime_binding=args.get("runtime_binding"), session_id=args.get("session_id"))


def _policies() -> Dict[str, AgentConfirmationPolicy]:
    from agents.gaiakeep.catalog import READ_TOOLS, TOOLS, is_mutation
    from orchestrator import computer_use_policy
    return {
        "gaiakeep-1": AgentConfirmationPolicy(
            agent_id="gaiakeep-1",
            classification={name: "always" if is_mutation(name) else "never" for name in TOOLS},
            machine_key="machine_id",
            gate_unclassified_unattended=True,
            unattended_allowed=READ_TOOLS,
            card_title="Confirm a GaiaKeep change",
            card_caption="Approve this exact operation after checking its parameters and native Gaia role requirements.",
            summary=_gaiakeep_summary,
            machine_label=_machine_label,
            auto_continue=True,
            card_as_result=True,
        ),
        MUTATING_AGENT_ID: AgentConfirmationPolicy(
            agent_id=MUTATING_AGENT_ID,
            classification=DESTRUCTIVE_CLASSIFICATION,
            machine_key="machine_id",
            gate_unclassified_unattended=False,
            unattended_allowed=frozenset(),
            card_title="Confirm a destructive operation",
            card_caption=("This changes the remote machine and cannot be undone by me. "
                          "Approve to run it exactly as shown, or decline."),
            summary=_summary,
            machine_label=_machine_label,
        ),
        computer_use_policy.AGENT_ID: AgentConfirmationPolicy(
            agent_id=computer_use_policy.AGENT_ID,
            classification=computer_use_policy.DESTRUCTIVE_CLASSIFICATION,
            machine_key="computer",
            gate_unclassified_unattended=True,
            unattended_allowed=computer_use_policy.UNATTENDED_ALLOWED,
            card_title=computer_use_policy.CARD_TITLE,
            card_caption=computer_use_policy.CARD_CAPTION,
            summary=_computer_use_summary,
            machine_label=_computer_use_label,
            is_destructive=computer_use_policy.is_destructive,
            refusal_text=computer_use_policy.REFUSAL_TEXT,
            auto_continue=False,
            dedupe_pending=True,
            machine_id=_computer_use_machine_id,
            card_as_result=True,
        ),
        "cresco-1": AgentConfirmationPolicy(
            agent_id="cresco-1",
            classification={
                "cresco_list_regions": "never",
                "cresco_list_agents": "never",
                "cresco_agent_info": "never",
                "cresco_get_sysinfo": "never",
                "cresco_dispatch_work": "always",
                "cresco_run_command": "always",
            },
            machine_key="target",
            gate_unclassified_unattended=True,
            unattended_allowed=frozenset({"cresco_list_regions", "cresco_list_agents", "cresco_agent_info", "cresco_get_sysinfo"}),
            card_title="Confirm a Cresco dispatch",
            card_caption=("This sends work to the approved Cresco fabric target and cannot be "
                          "recalled once the fabric accepts it. Approve this exact operation "
                          "after checking its target and payload."),
            summary=_cresco_summary,
            machine_label=_cresco_label,
            auto_continue=True,
            card_as_result=True,
        ),
        "terminal-shell-1": AgentConfirmationPolicy(
            agent_id="terminal-shell-1",
            classification={
                "start_terminal": "always", "terminal_write": "always",
                "list_terminals": "never", "terminal_read": "never",
                "terminal_resize": "never", "terminal_interrupt": "never",
                "close_terminal": "never",
            },
            machine_key="session_id",
            gate_unclassified_unattended=True,
            unattended_allowed=frozenset({
                "list_terminals", "terminal_read", "terminal_resize",
                "terminal_interrupt", "close_terminal",
            }),
            card_title="Confirm terminal execution",
            card_caption="Approve only this exact terminal operation after checking its input.",
            summary=_terminal_summary,
            machine_label=_terminal_label,
            machine_id=_terminal_machine_id,
            card_as_result=True,
        ),
    }


GATED_AGENT_IDS = frozenset({MUTATING_AGENT_ID, "computer-use-1", "gaiakeep-1", "cresco-1", "terminal-shell-1"})


def policy_for(agent_id: Optional[str]) -> Optional[AgentConfirmationPolicy]:
    if not agent_id:
        return None
    return _policies().get(agent_id)


def _audit_event(user_id: Optional[str], action_type: str, description: str, *,
                 proposal_id: Optional[str] = None, machine_id: Optional[str] = None,
                 verb: Optional[str] = None, outcome: str = "success",
                 chat_id: Optional[str] = None):
    from datetime import datetime, timezone

    from audit.schemas import AuditEventCreate
    meta: Dict[str, Any] = {}
    if machine_id:
        meta["machine_id"] = str(machine_id)
    if verb:
        meta["verb"] = str(verb)
    if proposal_id:
        meta["proposal_id"] = str(proposal_id)
    return AuditEventCreate(
        actor_user_id=user_id or "unknown",
        auth_principal=user_id or "unknown",
        event_class="agent_lifecycle",
        action_type=action_type,
        description=description[:1024],
        conversation_id=chat_id,
        correlation_id=proposal_id or uuid.uuid4().hex,
        outcome=outcome,
        inputs_meta=meta,
        started_at=datetime.now(timezone.utc),
    )


def _audit_sync(user_id: Optional[str], action_type: str, description: str, **kw) -> None:
    try:
        from audit.recorder import get_recorder
        rec = get_recorder()
        if rec is None:
            return
        rec.record_blocking(_audit_event(user_id, action_type, description, **kw))
    except Exception:  # noqa: BLE001
        logger.debug("remote_op audit failed (%s)", action_type, exc_info=True)


async def _audit_async(user_id: Optional[str], action_type: str, description: str, **kw) -> None:
    try:
        from audit.recorder import get_recorder
        rec = get_recorder()
        if rec is None:
            return
        await rec.record(_audit_event(user_id, action_type, description, **kw))
    except Exception:  # noqa: BLE001
        logger.debug("remote_op audit failed (%s)", action_type, exc_info=True)


def _canonical_args(args: Dict[str, Any]) -> str:
    clean = {k: v for k, v in args.items() if not str(k).startswith("_")}
    return json.dumps(clean, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(args: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_args(args).encode("utf-8")).hexdigest()


def _machine_label(orch, user_id: str, machine_id: Optional[str]) -> str:
    if not machine_id:
        return "?"
    try:
        from orchestrator import remote_machines
        row = remote_machines.get_machine(
            plane_source_from_orchestrator(orch),
            user_id,
            machine_id,
        )
        if row:
            return str(row.get("label") or machine_id)
    except Exception:  # noqa: BLE001
        pass
    return str(machine_id)


def _summary(orch, user_id: str, tool_name: str, args: Dict[str, Any]) -> str:
    m = _machine_label(orch, user_id, args.get("machine_id"))
    if tool_name == "remove_path":
        return f"Delete {args.get('path')} on {m}" + (" (recursive)" if args.get("recursive") else "")
    if tool_name == "cancel_job":
        return f"Cancel job {args.get('job_id')} on {m}"
    if tool_name == "signal_process":
        return f"Send {args.get('signal')} to process {args.get('pid')} on {m}"
    if tool_name == "control_service":
        return f"{str(args.get('action') or '').capitalize()} service {args.get('service_name')} on {m}"
    if tool_name == "manage_package":
        return f"Remove package {args.get('package_name')} on {m}"
    if tool_name == "upload_file":
        return f"Overwrite {args.get('remote_path')} on {m}"
    return f"{tool_name} on {m}"


def _gaiakeep_summary(orch, user_id: str, tool_name: str, args: dict[str, Any]) -> str:
    from agents.gaiakeep.client import clean_result
    public = {k: v for k, v in args.items() if not str(k).startswith("_") and k != "data_base64"}
    if "data_base64" in args:
        public["upload_base64_sha256"] = hashlib.sha256(str(args["data_base64"]).encode()).hexdigest()
    preview = json.dumps(clean_result(public), ensure_ascii=False, sort_keys=True)
    return f"{tool_name} on {_machine_label(orch, user_id, args.get('machine_id'))}: {preview}"


def classification_for(tool_name: str, agent_id: Optional[str] = None) -> Any:
    policy = policy_for(agent_id) if agent_id else None
    table = policy.classification if policy is not None else DESTRUCTIVE_CLASSIFICATION
    return table.get(tool_name, "always" if agent_id == "gaiakeep-1" else None)


def is_destructive_unattended(tool_name: str, args: Dict[str, Any],
                              agent_id: Optional[str] = None) -> bool:
    policy = policy_for(agent_id) if agent_id else None
    if policy is not None and policy.gate_unclassified_unattended \
            and tool_name not in policy.unattended_allowed:
        return True
    classification = classification_for(tool_name, agent_id)
    if classification in (None, "never"):
        return False
    if classification == "always" or classification == "if_exists":
        return True
    if isinstance(classification, dict) and "by_action" in classification:
        return args.get("action") in set(classification["by_action"])
    if policy is not None and policy.is_destructive is not None:
        return bool(policy.is_destructive(tool_name, args))
    return True


def _is_destructive(orch, user_id: str, tool_name: str, args: Dict[str, Any], classification: Any) -> bool:
    if classification == "never":
        return False
    if classification == "always":
        return True
    if isinstance(classification, dict) and "by_action" in classification:
        return args.get("action") in set(classification["by_action"])
    if classification == "if_exists":
        from orchestrator import remote_machines
        from orchestrator.remote_transport import get_transport
        try:
            target = remote_machines.build_target(
                plane_source_from_orchestrator(orch),
                orch.credential_manager,
                user_id,
                args.get("machine_id"),
            )
            res = get_transport().stat(target, str(args.get("remote_path") or ""), timeout=15.0)
            if not res.ok:
                return True
            return bool((res.data or {}).get("exists"))
        except Exception:  # noqa: BLE001
            return True
    return True


def _no_live_human(orch, websocket) -> bool:
    if websocket is None:
        return True
    try:
        from orchestrator.async_tasks import VirtualWebSocket
        if isinstance(websocket, VirtualWebSocket):
            return True
    except Exception:  # noqa: BLE001
        pass
    claims = orch.ui_sessions.get(websocket) or getattr(websocket, "machine_claims", None)
    if isinstance(claims, dict):
        if claims.get("machine_class") or claims.get("_invocation_channel") in {
                "rest", "mcp", "a2a", "background", "scheduled", "chained", "stream"}:
            return True
    return False


def _proposal_context(orch) -> PlaneRepositoryContext:
    plane = orch.runtime_composition.plane
    repository, runtime = repository_from(
        "remote_operation_proposals",
        plane_runtime=plane.runtime,
        repositories=plane.repositories,
        legacy_database=None,
    )
    return PlaneRepositoryContext(repository=repository, plane_runtime=runtime)


def _consume_if_valid(
    orch,
    proposal_id: str,
    user_id: str,
    tool_name: str,
    args: Dict[str, Any],
) -> bool:
    context = _proposal_context(orch)
    with context.transaction() as transaction:
        consumed = context.repository.consume_if_valid(
            transaction,
            owner_id=user_id,
            proposal_id=proposal_id,
            expected_tool_name=tool_name,
            expected_args_fingerprint=_fingerprint(args),
            consumed_at=int(time.time()),
        )
    return consumed is not None


def consumed_review(orch, proposal_id, owner, chat, agent, tool, args, *, transaction=None):
    from orchestrator.remote_effect_execution import qualified_tool

    if not qualified_tool(agent, tool) or not isinstance(proposal_id, str):
        return None
    context = _proposal_context(orch)
    row = (context.call(context.repository.get, owner_id=owner, proposal_id=proposal_id)
        if transaction is None else context.repository.get(transaction, owner_id=owner, proposal_id=proposal_id))
    return row if (row is not None and row.status == "consumed" and row.expires_at > time.time()
        and (row.owner_id, row.conversation_id, row.agent_id, row.tool_name, row.machine_id)
            == (owner, chat, agent, tool, args.get("machine_id"))
        and row.args_fingerprint == _fingerprint(args)) else None


def _create_proposal(orch, user_id: str, chat_id: str | None, agent_id: str,
                     tool_name: str, args: dict[str, Any], *, on_created=None, websocket=None, review_parts=()) -> tuple[str, dict[str, Any]]:
    from astralprims import Button, Card, Text
    policy = policy_for(agent_id) or policy_for(MUTATING_AGENT_ID)
    proposal_id = uuid.uuid4().hex
    now = int(time.time())
    card_host = computer_card_host(orch, user_id, chat_id, args) if agent_id == "computer-use-1" else None
    summary = (_computer_policy().summary_for(tool_name, args, card_host.name) if card_host is not None
               else policy.summary(orch, user_id, tool_name, args))
    display_summary = summary
    if agent_id in {"terminal-shell-1", "computer-use-1"} and len(summary) > 1024:
        summary = summary[:1000] + "… (see approval card preview)"
    machine_ref = args.get(policy.machine_key)
    if card_host is not None:
        machine_ref = card_host.host_id
    elif policy.machine_id is not None:
        machine_ref = policy.machine_id(orch, user_id, args)
    context = _proposal_context(orch)
    with context.transaction() as transaction:
        context.repository.create(
            transaction,
            RemoteOperationProposalRecord(
                proposal_id=proposal_id,
                owner_id=user_id,
                conversation_id=chat_id,
                machine_id=str(machine_ref or ""),
                agent_id=agent_id,
                tool_name=tool_name,
                args_fingerprint=_fingerprint(args),
                arguments={
                    key: value
                    for key, value in args.items()
                    if not str(key).startswith("_")
                },
                summary=summary,
                status="pending",
                created_at=now,
                expires_at=now + PROPOSAL_TTL_S,
            ),
        )
        # Must run in this same transaction — no orphaned confirmation
        if on_created is not None:
            on_created(transaction, proposal_id)
    logger.info("remote_op proposal created: %s verb=%s owner=%s", proposal_id, tool_name, user_id)
    offer = None
    if agent_id == "computer-use-1":
        from orchestrator.hitl_confirmation import grant_request

        authorities = getattr(orch, "_computer_approval_authorities", {})
        authorities = {key: value for key, value in authorities.items() if value[1] > time.monotonic()}
        authority = capture_computer_approval(orch, websocket, user_id, chat_id, args)
        request = grant_request(authority, tool_name, args)
        offer = request.offer if request is not None else None
        if authority is not None and len(authorities) < 128:
            authorities[proposal_id] = (authority, time.monotonic() + PROPOSAL_TTL_S, offer)
        orch._computer_approval_authorities = authorities
    _audit_sync(user_id, "remote_op.proposed", f"proposed {tool_name}: {summary}",
                proposal_id=proposal_id, machine_id=machine_ref, verb=tool_name,
                outcome="in_progress", chat_id=chat_id)
    title = policy.card_title
    if "{host}" in title:
        title = title.replace("{host}", card_host.name if card_host is not None
                              else policy.machine_label(orch, user_id, machine_ref))
    card = Card(title=title, content=[
        Text(content=display_summary, variant="body"),
        Text(content=policy.card_caption, variant="caption"),
        Button(label="Approve", action="remote_op_decision",
               payload={"proposal_id": proposal_id, "decision": "approve"}),
        Button(label="Decline", action="remote_op_decision", variant="secondary",
               payload={"proposal_id": proposal_id, "decision": "decline"}),
    ], id=card_component_id(proposal_id) if policy.card_as_result or agent_id == "gaiakeep-1" else None).to_dict()
    if offer is not None and offer.label and authority is not None:
        card["content"].insert(len(card["content"]) - 1, Button(
            label=offer.label, action="remote_op_decision", variant="secondary",
            payload={"proposal_id": proposal_id, "decision": "grant_task"}).to_dict())
    if agent_id == "computer-use-1":
        target = computer_resume_target(websocket, authority, tool_name, args)
        if target is not None:
            card["content"][2] = Button(
                label=f"Approve and resume on {authority.host.name} / {target}",
                action="remote_op_decision", payload={"proposal_id": proposal_id,
                                                     "decision": "approve_and_resume"}).to_dict()
            card["content"].insert(2, Text(content="This decision explicitly resumes this Mac session. Any focus change is "
                "limited to the reviewed target. The exact action still passes permission, privacy and source review.",
                variant="caption").to_dict())
    card["content"][2:2] = review_parts
    return proposal_id, card


def card_component_id(proposal_id: str) -> str:
    return f"au_approval_{proposal_id}"


def is_approval_card_result(components: Any) -> bool:
    if not isinstance(components, list) or len(components) != 1:
        return False
    card = components[0]
    if not isinstance(card, dict) or card.get("type") != "card":
        return False
    ident = card.get("id")
    if not isinstance(ident, str) or not ident.startswith("au_approval_"):
        return False
    proposal_id = ident.removeprefix("au_approval_")
    if len(proposal_id) != 32 or any(char not in "0123456789abcdef" for char in proposal_id):
        return False
    content = card.get("content")
    if not isinstance(content, list):
        return False
    buttons = [item for item in content if isinstance(item, dict) and item.get("type") == "button"]
    return len(buttons) == 2 and all(
        button.get("action") == "remote_op_decision"
        and button.get("payload") == {"proposal_id": proposal_id, "decision": decision}
        for button, decision in zip(buttons, ("approve", "decline"))
    )


async def _replace_card(orch, row, title: str, body: str, variant: str = "default") -> None:
    if not row.conversation_id or str(row.conversation_id).startswith("terminal-api:"):
        return
    try:
        from astralprims import Card, Text
        policy = policy_for(row.agent_id)
        if policy is None or (not policy.card_as_result and row.agent_id != "gaiakeep-1"):
            return
        comp = Card(title=title, content=[Text(content=body, variant="body")],
                    id=card_component_id(str(row.proposal_id))).to_dict()
        cid = card_component_id(str(row.proposal_id))

        async def _mutation():
            return await orch.workspace.aupsert(row.conversation_id, row.owner_id, [comp],
                                                force_component_id=cid)
        ops = await orch.run_detached_conversation_mutation(
            chat_id=row.conversation_id, user_id=row.owner_id, mutation=_mutation)
        if ops:
            await orch.send_ui_upsert(None, row.conversation_id, row.owner_id, ops)
    except Exception:  # noqa: BLE001
        logger.debug("remote_op card replacement failed", exc_info=True)


def evaluate(orch, websocket, agent_id: Optional[str], tool_name: str,
             args: Dict[str, Any], chat_id: Optional[str], user_id: Optional[str]):
    from astralprims import Alert
    policy = policy_for(agent_id)
    if policy is None:
        return None
    if agent_id == "terminal-shell-1" and tool_name in {"start_terminal", "terminal_write"}:
        from orchestrator import terminal_approvals

        request = terminal_approvals.current_request(orch, user_id, tool_name)
        if not args.get(_MARKER) and (request is not None or not _no_live_human(orch, websocket)):
            args["runtime_binding"] = orch.terminal_sessions.runtime_binding(
                user_id, session_id=args.get("session_id") if tool_name == "terminal_write" else None)
        if request is not None and _no_live_human(orch, websocket) and not args.get(_MARKER):
            pid, card = request.create_proposal(lambda: _create_proposal(
                orch, user_id, request.conversation_id, agent_id, tool_name, args))
            return (policy.refusal_text, [card])
    if agent_id == "gaiakeep-1":
        from agents.gaiakeep.catalog import validate
        public = {k: v for k, v in args.items() if not str(k).startswith("_") and k not in {"user_id", "session_id"}}
        try:
            validate(tool_name, public)
        except ValueError:
            return ("Invalid GaiaKeep arguments; no operation was sent.",
                    [Alert(message="Invalid GaiaKeep arguments; no operation was sent.", variant="error").to_dict()])
        from orchestrator import remote_machines
        try:
            owned = bool(user_id and remote_machines.resolve_machine(
                plane_source_from_orchestrator(orch), user_id, args.get("machine_id")))
        except Exception:  # noqa: BLE001
            owned = False
        if not owned:
            _audit_sync(user_id, "remote_op.owner_denied", "Gaia machine is not available to this owner",
                        machine_id=args.get("machine_id"), verb=tool_name, outcome="failure", chat_id=chat_id)
            return ("GaiaKeep requires a registered machine owned by the signed-in user; no operation was sent.",
                    [Alert(message="This GaiaKeep machine is not available to your account.", variant="error").to_dict()])
        from agents.gaiakeep.catalog import TOOLS, UPLOAD_ACTIONS
        from orchestrator.gaiakeep_dispatch import prepare_reconciliation

        reconciliation = prepare_reconciliation(tool_name, public)
        if "request_id" in reconciliation:
            if TOOLS[tool_name]["action"] in UPLOAD_ACTIONS:
                args["request_id"] = reconciliation["request_id"]
            else:
                args["params"] = dict(public["params"], request_id=reconciliation["request_id"])
    if agent_id == "computer-use-1":
        from orchestrator.hitl_confirmation import bypass_refusal

        if tool_name in {"read_file", "list_dir", "write_file", "delete_path"}:
            refusal = _resolve_file_folder(orch, user_id, chat_id, tool_name, args)
            if refusal is not None:
                return refusal
        refusal = bypass_refusal(orch, websocket, user_id, chat_id, agent_id, tool_name, args, ())
        if refusal is not None:
            return Refusal((refusal, [Alert(message=refusal, variant="error").to_dict()]))
    machine_ref = args.get(policy.machine_key)
    classification = classification_for(tool_name, agent_id)
    if classification is None and not policy.gate_unclassified_unattended:
        return None

    # Must run before the destructiveness and marker checks below
    if _no_live_human(orch, websocket) and tool_name not in policy.unattended_allowed:
        logger.info("remote_op refused (unattended): verb=%s owner=%s", tool_name, user_id)
        _audit_sync(user_id, "remote_op.refused_unattended",
                    f"refused unattended {tool_name}", machine_id=machine_ref,
                    verb=tool_name, outcome="failure", chat_id=chat_id)
        return ("unattended_refused: remote-control operations need a live person; "
                "re-issue it interactively.",
                [Alert(message="Remote-control operations can't run unattended — "
                               "re-issue this interactively.", variant="error").to_dict()])

    if classification is None:
        return None

    if agent_id == "computer-use-1" and tool_name == "run_command":
        refusal = _resolve_command_folder(orch, user_id, chat_id, args)
        if refusal is not None:
            return refusal

    if (agent_id == "computer-use-1" and tool_name == "browser_navigate" and "url" in args
            and _computer_policy().canonical_browser_url(args["url"]) is None):
        _audit_sync(user_id, "remote_op.refused_url", "refused browser_navigate to a rejected address",
                    machine_id=machine_ref, verb=tool_name, outcome="failure", chat_id=chat_id)
        return ("url_rejected: the browser pane only opens http and https addresses with a plain host name; "
                "no approval was requested.",
                [Alert(message="That address cannot be opened in the browser pane.", variant="error").to_dict()])

    from persistent_agents.dispatch_context import current_dispatch
    if current_dispatch() is not None and classification == "if_exists":
        destructive = True
    elif policy.is_destructive is not None:
        destructive = bool(policy.is_destructive(tool_name, args))
    else:
        destructive = _is_destructive(orch, user_id, tool_name, args, classification)
    if not destructive:
        return None

    from orchestrator.hitl_confirmation import reviewed_remote_proposal

    reviewed = reviewed_remote_proposal(orch, user_id, chat_id, agent_id, tool_name, args)
    if reviewed is not None:
        context = _proposal_context(orch)
        row = context.call(context.repository.get, owner_id=user_id, proposal_id=reviewed)
        if (row is not None and row.status == "consumed" and row.owner_id == user_id
                and row.conversation_id == chat_id and row.agent_id == agent_id and row.tool_name == tool_name
                and row.args_fingerprint == _fingerprint(args) and row.expires_at > time.time()):
            return None
        return ("The previously reviewed operation expired or changed. Re-request the action.",
                [Alert(message="Previous operation review is no longer current.", variant="warning").to_dict()])

    marker = args.get(_MARKER)
    if marker:
        ok = _consume_if_valid(orch, str(marker), user_id, tool_name, args)
        args.pop(_MARKER, None)
        if ok:
            _audit_sync(user_id, "remote_op.consumed",
                        f"approved & consumed {tool_name}", proposal_id=str(marker),
                        machine_id=machine_ref, verb=tool_name, chat_id=chat_id)
            return None
        _audit_sync(user_id, "remote_op.approval_invalid",
                    f"invalid approval for {tool_name}", proposal_id=str(marker),
                    machine_id=machine_ref, verb=tool_name, outcome="failure", chat_id=chat_id)
        return ("This approval is no longer valid (already used, expired, or the "
                "arguments changed). Re-request the operation.",
                [Alert(message="Approval no longer valid — re-request the operation.",
                       variant="error").to_dict()])

    if agent_id == "computer-use-1":
        from orchestrator.hitl_confirmation import bypass_covers, grant_covers

        try:
            card_host = computer_card_host(orch, user_id, chat_id, args)
        except ComputerHostError as exc:
            return Refusal((f"{exc.code}: {exc.message}", [Alert(message=exc.message, variant="error").to_dict()]))
        if (grant_covers(orch, websocket, user_id, chat_id, tool_name, args)
                or bypass_covers(orch, websocket, user_id, chat_id, agent_id, tool_name, args, ())):
            return None
    review_parts = []
    if agent_id == "computer-use-1" and card_host.platform == "macos":
        authority = capture_computer_approval(orch, websocket, user_id, chat_id, args)
        if authority is None or (authority.session.state == "paused"
                and computer_resume_target(websocket, authority, tool_name, args) is None):
            message = "The computer session cannot authorize this action. Resume it on the Mac and re-request it."
            return Refusal((message, [Alert(message=message, variant="error").to_dict()]))
    if agent_id == "computer-use-1" and card_host.platform == "macos" and tool_name in {"write_file", "delete_path"}:
        from astralprims import CodeBlock, Text
        from orchestrator import hitl, source_bound
        from orchestrator.hitl_confirmation import _review_arguments

        shown = {key: value for key, value in args.items() if key != "computer"}
        preview, reviewable = _review_arguments(shown)
        if not reviewable:
            message = "Review unavailable: remove sensitive or oversized arguments before requesting this action."
            return Refusal((message, [Alert(message=message, variant="error").to_dict()]))
        action = source_bound.declaration(orch.tool_permissions.get_tool_scope_map(agent_id), agent_id, tool_name)
        risks = {*source_bound.review_risks(action), *hitl.assess_risk(tool_name, args, actor_principal=user_id,
                                                                    agent_id=agent_id)}
        origin = orch._source_ledger(user_id, chat_id).origin_of(args).value
        review_parts = [Text(content=f"Risks: {', '.join(sorted(risks))}. Source: {origin}.",
                             variant="body").to_dict(), CodeBlock(code=preview, language="json").to_dict()]
    pending_key = (user_id, agent_id, tool_name, _fingerprint(args))
    if policy.retry_grace_s > 0:
        grace = _RETRY_GRACE.pop(pending_key, None)
        if grace is not None and grace[1] > time.time():
            _audit_sync(user_id, "remote_op.retry_under_grace",
                        f"retry of approved {tool_name} admitted", proposal_id=grace[0],
                        machine_id=machine_ref, verb=tool_name, chat_id=chat_id)
            return None
    if policy.dedupe_pending:
        pending = _PENDING_CARDS.get(pending_key)
        if pending is not None and pending[1] > time.time():
            return (policy.refusal_text, [Alert(message="Still waiting for your approval of the "
                                                "action above.", variant="warning").to_dict()])
    pid, card = _create_proposal(orch, user_id, chat_id, agent_id, tool_name, args, websocket=websocket,
                                 review_parts=review_parts)
    if policy.dedupe_pending:
        _PENDING_CARDS[pending_key] = (pid, time.time() + PROPOSAL_TTL_S)
    return (policy.refusal_text, [card])


async def handle_decision(orch, websocket, user_id: str, payload: Dict[str, Any]) -> None:
    held: list = []
    try:
        await _handle_decision(orch, websocket, user_id, payload, held)
    finally:
        for origin in held:
            origin.close()


async def _handle_decision(orch, websocket, user_id: str, payload: Dict[str, Any], held: list) -> None:
    from astralprims import Alert
    proposal_id = (payload or {}).get("proposal_id")
    decision = (payload or {}).get("decision")
    resume_requested = decision == "approve_and_resume"
    grant_requested = decision == "grant_task"

    async def _say(message: str, variant: str = "warning") -> None:
        await orch.send_ui_render(websocket, [Alert(message=message, variant=variant).to_dict()], target="chat")

    context = _proposal_context(orch)
    row = None
    if proposal_id:
        row = await asyncio.to_thread(
            context.call,
            context.repository.get,
            owner_id=user_id,
            proposal_id=str(proposal_id),
        )
    if row is None:
        logger.warning("remote_op_decision refused (not owner/found): id=%s actor=%s", proposal_id, user_id)
        await _audit_async(user_id, "remote_op.decision_refused",
                           "decision refused (not owner or not found)",
                           proposal_id=proposal_id, outcome="failure")
        await _say("That confirmation is not available.")
        return
    computer_authority = offer = None
    if row.agent_id == "computer-use-1":
        entry = getattr(orch, "_computer_approval_authorities", {}).get(str(proposal_id))
        computer_authority = entry[0] if entry is not None and entry[1] > time.monotonic() else None
        offer = entry[2] if computer_authority is not None and len(entry) > 2 else None
        if grant_requested and (offer is None or not offer.label):
            await _say("This action cannot be allowed for the whole task. Approve it once or decline.")
            return
        if (decision not in {"approve", "approve_and_resume", "grant_task", "decline"}
                or (resume_requested and computer_resume_target(
                    websocket, computer_authority, row.tool_name, row.arguments) is None)
                or not computer_approval_current(orch, websocket, computer_authority,
                                                 allow_local_pause=resume_requested or decision == "decline")):
            _forget_pending(str(proposal_id))
            await _audit_async(user_id, "remote_op.decision_refused", "computer decision lost its authority",
                               proposal_id=proposal_id, machine_id=row.machine_id, verb=row.tool_name,
                               outcome="failure", chat_id=row.conversation_id)
            await _replace_card(orch, row, "Expired", "The computer session changed. Re-request this action.")
            await _say("This computer session changed. Re-request the action in its current conversation.")
            return
    elif resume_requested:
        await _say("This action cannot resume a local computer session.")
        return
    elif grant_requested:
        await _say("This action cannot be allowed for the whole task. Approve it once or decline.")
        return
    terminal_request = None
    if str(row.conversation_id or "").startswith("terminal-api:"):
        from orchestrator import terminal_approvals
        from orchestrator.terminal_security import TerminalError

        try:
            terminal_request = await asyncio.to_thread(
                terminal_approvals.for_decision, orch, websocket, user_id, row)
        except TerminalError:
            await _say("This terminal request is no longer available. Re-request it from the original client.")
            return
    from persistent_agents.approvals import handle_linked_remote_decision
    if await handle_linked_remote_decision(orch, websocket, user_id, row, decision):
        return
    now = int(time.time())
    _mid, _verb, _cid = row.machine_id, row.tool_name, row.conversation_id
    _forget_pending(str(proposal_id))
    if row.status != "pending":
        await _say("This request was already handled.")
        return
    if now > row.expires_at:
        def _expire():
            with context.transaction() as transaction:
                return context.repository.expire_if_pending(
                    transaction,
                    owner_id=user_id,
                    proposal_id=str(proposal_id),
                    observed_at=now,
                )

        expired = await asyncio.to_thread(_expire)
        if expired is None:
            await _say("This request was already handled.")
            return
        await _audit_async(user_id, "remote_op.expired", f"approval expired for {_verb}",
                           proposal_id=proposal_id, machine_id=_mid, verb=_verb,
                           outcome="failure", chat_id=_cid)
        await _say("This request expired — please re-request the operation.")
        return
    if decision not in {"approve", "approve_and_resume", "grant_task"}:
        def _decline():
            with context.transaction() as transaction:
                return context.repository.decide_if_pending(
                    transaction,
                    owner_id=user_id,
                    proposal_id=str(proposal_id),
                    decision="declined",
                    decided_at=now,
                )

        declined = await asyncio.to_thread(_decline)
        if declined is None:
            await _say("This request was already handled.")
            return
        await _audit_async(user_id, "remote_op.declined", f"declined {_verb}",
                           proposal_id=proposal_id, machine_id=_mid, verb=_verb,
                           outcome="failure", chat_id=_cid)
        await _say("Declined — nothing was changed.", variant="info")
        await _replace_card(orch, row, "Declined", f"{row.summary} — not done.")
        if terminal_request is not None:
            terminal_request.status = "declined"
        return

    def _approve():
        with context.transaction() as transaction:
            return context.repository.decide_if_pending(
                transaction,
                owner_id=user_id,
                proposal_id=str(proposal_id),
                decision="approved",
                decided_at=now,
            )

    from orchestrator.hitl_confirmation import continuation_guidance

    origin, guided = await continuation_guidance(orch, websocket)
    if origin is not None:
        held.append(origin)
    approved = await asyncio.to_thread(_approve)
    if approved is None:
        await _say("This request was already handled.")
        return
    await _audit_async(user_id, "remote_op.approved", f"approved {_verb}",
                       proposal_id=proposal_id, machine_id=_mid, verb=_verb, chat_id=_cid)
    if offer is not None and (grant_requested or offer.on_approve):
        if not computer_approval_current(orch, websocket, computer_authority):
            await _say("The computer authority changed during review. No new action was dispatched.")
            return
        task = computer_authority.task
        try:
            orch.computer_sessions.grants.create(
                user_id, computer_authority.host.host_id, computer_authority.session.session_id, row.conversation_id,
                getattr(task[1], "task_id", None) if task is not None else str(proposal_id),
                offer.scope, offer.verb_classes, offer.command, now=time.time())
        except ValueError:
            await _say("This action stays on exact review and was not run. Approve it once instead.")
            return
    if resume_requested:
        computer_authority = await resume_computer_approval(
            orch, websocket, computer_authority, row.tool_name, row.arguments)
        if computer_authority is None:
            await _say("The local resume authority changed. Re-request the action.")
            return
    await _replace_card(orch, row, "Approved ✓", f"{row.summary} — running now.")
    # Goes back through the gate to re-validate — not a direct call
    if row.agent_id == "gaiakeep-1":
        from persistent_agents.runtime_values import thaw
        stored_args = thaw(row.arguments)
    else:
        stored_args = dict(row.arguments)
    stored_args[_MARKER] = proposal_id
    tc = SimpleNamespace(id="remote-op", function=SimpleNamespace(
        name=row.tool_name, arguments=json.dumps(stored_args)))
    logger.info("remote_op approved -> re-dispatch: id=%s verb=%s", proposal_id, row.tool_name)
    from orchestrator.hitl_confirmation import (
        announce_turn, claim_turn_effect, release_turn_effect, turn_effect_cancelled,
    )

    announced = False
    if terminal_request is not None:
        terminal_request.status = "running"
        try:
            with terminal_approvals.effect_scope(terminal_request):
                with source_review_scope(orch, row):
                    result = await orch.execute_single_tool(
                        websocket, tc, {row.tool_name: row.agent_id}, None, user_id=user_id)
            terminal_request.complete(result)
        except Exception as exc:
            terminal_request.complete(error=exc)
            await _say("The terminal request could not complete. Check its status before issuing another request.")
            return
    else:
        if row.agent_id == "computer-use-1" and not computer_approval_current(orch, websocket, computer_authority):
            await _say("The computer authority changed during review. No new action was dispatched.")
            return
        announced = websocket is not None and bool(row.conversation_id)
        if announced:
            await announce_turn(orch, websocket, "executing", f"Running: {row.tool_name}...")
        effect_key = claim_turn_effect(orch, websocket, row.conversation_id) if announced else None
        try:
            try:
                with source_review_scope(orch, row):
                    result = await orch.execute_single_tool(
                        websocket, tc, {row.tool_name: row.agent_id}, row.conversation_id, user_id=user_id)
                cancelled = False
            except asyncio.CancelledError:
                if effect_key is None or not turn_effect_cancelled(orch):
                    raise
                result, cancelled = None, True
            finally:
                if effect_key is not None:
                    release_turn_effect(orch, effect_key)
        except (Exception, asyncio.CancelledError):
            if row.agent_id == "computer-use-1":
                if resume_requested:
                    await pause_failed_computer_approval(orch, websocket, computer_authority)
                getattr(orch, "_computer_approval_authorities", {}).pop(str(proposal_id), None)
            raise
        if cancelled:
            if row.agent_id == "computer-use-1":
                if resume_requested:
                    await pause_failed_computer_approval(orch, websocket, computer_authority)
                getattr(orch, "_computer_approval_authorities", {}).pop(str(proposal_id), None)
            await _replace_card(orch, row, "Cancelled",
                                f"{row.summary} — cancelled; inspect current state before retrying.")
            await announce_turn(orch, websocket, "done")
            return
    continued = False
    try:
        continued = await _settle_decision(orch, websocket, user_id, row, result, stored_args, proposal_id,
                                           computer_authority, resume_requested, guided, origin, held, _say)
    finally:
        if announced and not continued:
            await announce_turn(orch, websocket, "done")


async def _settle_decision(orch, websocket, user_id, row, result, stored_args, proposal_id, computer_authority,
                           resume_requested, guided, origin, held, _say) -> bool:
    from orchestrator.remote_effect_ingress import publish_review
    await publish_review(orch, websocket, row.agent_id, row.tool_name, row.conversation_id, user_id, result)
    if row.agent_id == "computer-use-1" and successful_result(result):
        _computer_use_on_approved(orch, row, websocket, computer_authority)
    elif row.agent_id == "computer-use-1":
        data, _error = _result_data(result)
        if not isinstance(data, dict) or data.get("status") != "confirmation_required":
            if resume_requested:
                await pause_failed_computer_approval(orch, websocket, computer_authority)
            reason = _failure_text(result)
            await _replace_card(orch, row, "Not completed", f"{row.summary} — not completed: {reason}")
            await _say(f"{row.tool_name} did not complete: {reason}", variant="error")
    if row.agent_id == "computer-use-1":
        getattr(orch, "_computer_approval_authorities", {}).pop(str(proposal_id), None)
    if (row.agent_id in {"gaiakeep-1", "computer-use-1"} and websocket is not None and row.conversation_id
            and result is not None and result.error is None and result.ui_components):
        components = copy.deepcopy(result.ui_components)
        public_arguments = {key: value for key, value in stored_args.items()
                            if not str(key).startswith("_") and key not in {"user_id", "session_id"}}
        from orchestrator.orchestrator import _tag_tool_result_source
        for component in components:
            _tag_tool_result_source(component, result, row.agent_id, row.tool_name,
                                    public_arguments, getattr(result, "correlation_id", None))
        async def _publish_result():
            return await orch._send_or_replace_components(
                websocket, components, row.conversation_id, user_id=user_id)
        await orch.run_detached_conversation_mutation(
            chat_id=row.conversation_id, user_id=user_id, mutation=_publish_result)
    policy = policy_for(row.agent_id)
    not_attempted = _not_attempted_code(result)
    if policy is not None and policy.retry_grace_s > 0 and not_attempted:
        _RETRY_GRACE[(user_id, row.agent_id, row.tool_name, _fingerprint(dict(row.arguments)))] = (
            str(proposal_id), time.time() + policy.retry_grace_s)
        logger.info("remote_op approved but not attempted (%s): retry grace %.0fs id=%s",
                    not_attempted, policy.retry_grace_s, proposal_id)
    from orchestrator.hitl_confirmation import CONTINUATION_TEXT, continue_after_approval

    def computer_current():
        return computer_approval_current(orch, websocket, computer_authority)

    guard = None
    if policy is not None and policy.auto_continue:
        outcome = _continuation_text(row, result, retry_grace_s=policy.retry_grace_s if not_attempted else 0)
    elif row.agent_id == "computer-use-1" and successful_result(result):
        outcome, guard = CONTINUATION_TEXT, computer_current
    else:
        outcome = None
    if outcome is None or not guided or not row.conversation_id or websocket is None:
        return False
    if origin is not None:
        held.remove(origin)
    continue_after_approval(orch, websocket, user_id, row.conversation_id, outcome, "✓ Approved — continuing",
                            origin, guard=guard, name=f"remote-op-continue-{row.proposal_id}")
    return True


def _result_data(result) -> Tuple[Any, Any]:
    data: Any = None
    error: Any = None
    if result is not None:
        raw = getattr(result, "result", None)
        error = getattr(result, "error", None)
        data = raw.get("_data") if isinstance(raw, dict) and "_data" in raw else raw
    return data, error


def successful_result(result) -> bool:
    data, error = _result_data(result)
    if result is None or error:
        return False
    return not isinstance(data, dict) or (not data.get("code") and data.get("status") not in {
        "confirmation_required", "failed", "error", "uncertain", "outcome_uncertain", "cancelled"}
        and data.get("ok") is not False and data.get("launched") is not False)


def _failure_text(result) -> str:
    data, error = _result_data(result)
    for source in (error, data):
        if isinstance(source, dict) and (source.get("message") or source.get("code")):
            return " ".join(str(source.get("message") or source.get("code")).split())[:300]
    return "the computer returned no result"


def _not_attempted_code(result) -> Optional[str]:
    data, error = _result_data(result)
    code = data.get("code") if isinstance(data, dict) else None
    if code is None and isinstance(error, dict):
        message = str(error.get("message") or "")
        code = "paused" if message.startswith("paused") else None
    return "paused" if code == "paused" else None


def _continuation_text(row, result, retry_grace_s: float = 0) -> str:
    data, error = _result_data(result)
    try:
        rendered = json.dumps(data, default=str)[:4000] if data is not None else "(no data)"
    except Exception:  # noqa: BLE001
        rendered = str(data)[:4000]
    if error:
        rendered = f"ERROR: {str((error or {}).get('message') if isinstance(error, dict) else error)[:1000]}"
    if retry_grace_s > 0:
        return (f"[The user tapped Approve.] `{row.tool_name}` was approved but could NOT run yet: "
                f"the computer is paused because someone is using it. Result: {rendered}\n\n"
                f"Call wait (seconds=10), then resume_session; once it is active, call `{row.tool_name}` "
                f"again with EXACTLY the same arguments {json.dumps(dict(row.arguments), default=str)[:1500]} — "
                f"the approval is kept for {int(retry_grace_s)} seconds, so that one retry needs no new "
                "confirmation. Then continue the task and report the outcome in plain language.")
    return (f"[The user tapped Approve.] `{row.tool_name}` has now been carried out on the computer "
            f"(do not call it again for this step). Result: {rendered}\n\n"
            "Continue the task from here — take any further actions on the computer that are "
            "needed, then report the outcome to the user in plain language.")
