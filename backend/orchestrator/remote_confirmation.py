"""Classifies remote tools and enforces ownership, host policy and unattended-operation limits before dispatch.
The orchestrator and MCP projection share the same classification.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from orchestrator.computer_hosts import ComputerHostError
from orchestrator.plane_repository_context import plane_source_from_orchestrator

logger = logging.getLogger("RemoteConfirmation")


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


@dataclass(frozen=True)
class RemoteToolPolicy:
    classification: dict
    machine_key: str
    gate_unclassified_unattended: bool
    unattended_allowed: frozenset
    is_destructive: Callable | None = None


def _computer_policy():
    from orchestrator import computer_use_policy

    return computer_use_policy


def _resolve_command_folder(orch, user_id, chat_id, args):
    from astralprims import Alert

    try:
        host = computer_host(orch, user_id, chat_id, args)
    except ComputerHostError:
        return None
    policy = _computer_policy()
    from orchestrator.project_authority import project_roots
    try:
        roots = project_roots(orch, user_id, chat_id, host, required=True)
        args["cwd"] = policy.resolve_cwd(args.get("cwd", roots[0]["path"] if roots and getattr(host, "project_mode", False) else None), roots, host.name)
    except ComputerHostError as exc:
        return f"{exc.code}: {exc.message}", [Alert(message=exc.message, variant="error").to_dict()]
    except policy.CwdRefusal as exc:
        _audit_sync(user_id, "remote_op.refused_cwd", "refused run_command outside the shared folders",
                    machine_id=host.host_id, verb="run_command", outcome="failure", chat_id=chat_id)
        return (f"{exc.code}: {exc} Nothing was run.",
                        [Alert(message=f"That folder is not shared from {host.name}; nothing was run.",
                               variant="error").to_dict()])
    return None


def _resolve_file_folder(orch, user_id, chat_id, tool_name, args):
    from astralprims import Alert

    try:
        host = computer_host(orch, user_id, chat_id, args)
    except ComputerHostError:
        return None
    if host.platform != "macos":
        return None
    policy = _computer_policy()
    from orchestrator.project_authority import project_roots
    try:
        roots = project_roots(orch, user_id, chat_id, host, required=True)
        primary = roots[0]["path"] if roots and getattr(host, "project_mode", False) else None
        args["path"] = policy.resolve_workspace_path(args.get("path"), roots, host.name, primary=primary)
    except ComputerHostError as exc:
        return f"{exc.code}: {exc.message}", [Alert(message=exc.message, variant="error").to_dict()]
    except policy.CwdRefusal as exc:
        _audit_sync(user_id, "remote_op.refused_path", "refused file operation outside the shared folders",
                    machine_id=host.host_id, verb=tool_name, outcome="failure", chat_id=chat_id)
        return (f"{exc.code}: {exc} Nothing was run.",
                        [Alert(message=str(exc), variant="error").to_dict()])
    return None


def _bind_ax_target(orch, user_id, chat_id, tool_name, args):
    from astralprims import Alert

    if tool_name not in _computer_policy().AX_REF_POINTER_VERBS | _computer_policy().AX_INPUT_VERBS:
        return None
    try:
        host = computer_host(orch, user_id, chat_id, args)
    except ComputerHostError:
        return None
    session = orch.computer_sessions.live_for_host(user_id, host.host_id)
    refusal = session.bind_ax(tool_name, args) if session is not None and host.platform == "macos" else None
    if refusal is None:
        return None
    return (f"{refusal.code}: {refusal} Nothing was run.", [Alert(message=str(refusal), variant="error").to_dict()])


def computer_host(orch, owner, chat, args):
    sessions = getattr(orch, "computer_sessions", None)
    if sessions is None:
        raise ComputerHostError("computer_unavailable", "remote control is not enabled on this server")
    return sessions.resolve_host(owner, chat, args.get("computer"))


def _policies() -> Dict[str, RemoteToolPolicy]:
    from agents.gaiakeep.catalog import READ_TOOLS, TOOLS, is_mutation
    from orchestrator import computer_use_policy
    return {
        "gaiakeep-1": RemoteToolPolicy(
            classification={name: "always" if is_mutation(name) else "never" for name in TOOLS},
            machine_key="machine_id",
            gate_unclassified_unattended=True,
            unattended_allowed=READ_TOOLS,
        ),
        MUTATING_AGENT_ID: RemoteToolPolicy(
            classification=DESTRUCTIVE_CLASSIFICATION,
            machine_key="machine_id",
            gate_unclassified_unattended=False,
            unattended_allowed=frozenset(),
        ),
        computer_use_policy.AGENT_ID: RemoteToolPolicy(
            classification=computer_use_policy.DESTRUCTIVE_CLASSIFICATION,
            machine_key="computer",
            gate_unclassified_unattended=True,
            unattended_allowed=computer_use_policy.UNATTENDED_ALLOWED,
            is_destructive=computer_use_policy.is_destructive,
        ),
        "cresco-1": RemoteToolPolicy(
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
        ),
        "terminal-shell-1": RemoteToolPolicy(
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
        ),
    }


def policy_for(agent_id: Optional[str]) -> Optional[RemoteToolPolicy]:
    if not agent_id:
        return None
    return _policies().get(agent_id)


def _audit_event(user_id: Optional[str], action_type: str, description: str, *,
                 proposal_id: Optional[str] = None, machine_id: Optional[str] = None,
                 verb: Optional[str] = None, outcome: str = "success",
                 chat_id: Optional[str] = None,
                 origin: Optional[str] = None, label_hashes: Optional[List[str]] = None,
                 reason: Optional[str] = None, event_class: str = "agent_lifecycle"):
    from datetime import datetime, timezone

    from audit.schemas import AuditEventCreate
    meta: Dict[str, Any] = {}
    if machine_id:
        meta["machine_id"] = str(machine_id)
    if verb:
        meta["verb"] = str(verb)
    if proposal_id:
        meta["proposal_id"] = str(proposal_id)
    if origin:
        meta["origin"] = str(origin)
    if label_hashes:
        meta["item_labels_sha256"] = [str(item) for item in label_hashes[:32]]
    if reason:
        meta["reason"] = str(reason)
    return AuditEventCreate(
        actor_user_id=user_id or "unknown",
        auth_principal=user_id or "unknown",
        event_class=event_class,
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


def managed_policy_refusal(orch, owner: str, chat: Optional[str], tool_name: str,
                           args: Dict[str, Any]) -> Optional[str]:
    from orchestrator.computer_use_policy import (
        BYPASS_VERBS,
        BROWSER_COMPOSITE_VERBS,
        BROWSER_VERBS,
        HOST_VERBS,
    )

    sessions = getattr(orch, "computer_sessions", None)
    if sessions is None or not isinstance(args, dict):
        return None
    try:
        host = computer_host(orch, owner, chat, args)
    except ComputerHostError:
        return None
    if tool_name in BROWSER_VERBS or tool_name in BROWSER_COMPOSITE_VERBS:
        switch = "browser_pane"
    elif tool_name in HOST_VERBS:
        switch = "computer_use"
    elif (tool_name in BYPASS_VERBS and tool_name not in {"list_computers", "end_session"}
          and sessions.bypass_on(owner, host.host_id)):
        switch = "bypass"
    else:
        return None
    ceilings, sources = sessions.effective_ceilings(host, ui_sessions=getattr(orch, "ui_sessions", None))
    if getattr(ceilings, switch):
        return None
    from orchestrator.computer_managed_policy import refusal_message

    return refusal_message(switch, host.name, sources.get(switch, ""))


def _tainted_sink(orch, chat, agent, tool, args) -> bool:
    from orchestrator import taint

    if not taint.taint_enabled() or not taint.is_sink(agent, tool):
        return False
    try:
        trust = orch._taint_tracker(chat).effective_trust_of_args(
            taint.flow_args(agent, args), user_text=orch._current_request_text(chat))
    except Exception:
        logger.warning("Taint state unavailable; the grant does not apply", exc_info=True)
        return True
    return taint.check_flow(trust) == "deny"


def _computer_refusal(orch, websocket, owner, chat, tool, args) -> str | None:
    from orchestrator import computer_use_policy as policy
    from orchestrator.hitl_confirmation import attended
    from orchestrator.project_authority import FILE_VERBS, PROJECT_CONSENT_VERBS, current_origin

    if tool == "list_computers":
        return None
    try:
        host = computer_host(orch, owner, chat, args)
    except ComputerHostError as exc:
        return f"{exc.code}: {exc.message}"
    sessions = orch.computer_sessions
    session = sessions.live_for_host(owner, host.host_id)
    if session is not None and session.state == "paused" and tool not in {"end_session", "wait", "list_computers"}:
        return "This Mac is paused. Resume it on the Mac before continuing."
    if tool == "run_command" and policy.sandbox_exact_review_required(
            args, getattr(session, "approved_domains", None)):
        return None
    project_standing = False
    if getattr(host, "project_mode", False):
        origin = current_origin(orch, owner, chat, host)
        if origin is None:
            return "The origin project's authority is unavailable. Reconnect this Mac."
        if tool in PROJECT_CONSENT_VERBS:
            if not origin.roots and tool in FILE_VERBS:
                return "Choose folders for this project on this Mac before using file or command tools."
            project_standing = bool(origin.roots)
    if not project_standing and sessions.bypass_on(owner, host.host_id):
        from orchestrator.computer_managed_policy import refusal_message

        ceilings, sources = sessions.effective_ceilings(host, ui_sessions=getattr(orch, "ui_sessions", None))
        if not ceilings.bypass:
            return refusal_message("bypass", host.name, sources.get("bypass", ""))
    if not project_standing and not sessions.bypass_on(owner, host.host_id):
        return None
    if (tool not in policy.BYPASS_VERBS or not attended(orch, websocket, owner)
            or _tainted_sink(orch, chat, policy.AGENT_ID, tool, args)):
        return "This action is outside this Mac's current authority. Choose an allowed target."
    hwnd = args.get("hwnd")
    entry = session.window_cache.get(hwnd) if session is not None and type(hwnd) is int else None
    if policy.bypass_floor(tool, args, entry["bundle_id"] if entry else None,
                           getattr(session, "approved_domains", None)):
        return "This action requires an allowed app or a current window target. List windows and choose an allowed target."
    return None


def evaluate(orch, websocket, agent_id: Optional[str], tool_name: str,
             args: Dict[str, Any], chat_id: Optional[str], user_id: Optional[str]):
    from astralprims import Alert
    from orchestrator.hitl_confirmation import attended

    policy = policy_for(agent_id)
    if policy is None:
        return None
    if "_remote_op_proposal_id" in args:
        message = "This node does not accept a remote approval proposal."
        _audit_sync(user_id, "remote_op.approval_invalid", message, verb=tool_name,
                    outcome="failure", chat_id=chat_id)
        return message, [Alert(message=message, variant="error").to_dict()]
    if agent_id == "computer-use-1" and tool_name == "browser_fill_login":
        from orchestrator.computer_use_policy import fill_login_preflight

        shape_refusal = fill_login_preflight(args)
        if shape_refusal:
            _audit_sync(user_id, "remote_op.refused_fill_login_arguments",
                        "refused browser_fill_login with values or extra arguments",
                        machine_id=args.get("computer"), verb=tool_name, outcome="failure", chat_id=chat_id)
            return (shape_refusal, [Alert(message=shape_refusal, variant="error").to_dict()])
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
    machine_ref = args.get(policy.machine_key)
    classification = classification_for(tool_name, agent_id)
    if classification is None and not policy.gate_unclassified_unattended:
        return None

    if not attended(orch, websocket, user_id) and tool_name not in policy.unattended_allowed:
        logger.info("remote_op refused (unattended): verb=%s owner=%s", tool_name, user_id)
        _audit_sync(user_id, "remote_op.refused_unattended",
                    f"refused unattended {tool_name}", machine_id=machine_ref,
                    verb=tool_name, outcome="failure", chat_id=chat_id)
        message = "unattended_refused: this remote operation requires a live person."
        return message, [Alert(message=message, variant="error").to_dict()]

    if agent_id == "computer-use-1":
        from orchestrator.computer_managed_policy import MANAGED_POLICY_REFUSAL_REASON

        refusal = managed_policy_refusal(orch, user_id, chat_id, tool_name, args)
        if refusal is not None:
            _audit_sync(user_id, "remote_op.refused", f"refused {tool_name} by managed policy ceilings",
                        verb=tool_name, outcome="failure", chat_id=chat_id,
                        reason=MANAGED_POLICY_REFUSAL_REASON)
            return (refusal, [Alert(message=refusal, variant="error").to_dict()])
        if tool_name in {"read_file", "list_dir", "write_file", "delete_path"}:
            refusal = _resolve_file_folder(orch, user_id, chat_id, tool_name, args)
            if refusal is not None:
                return refusal
        refusal = _bind_ax_target(orch, user_id, chat_id, tool_name, args)
        if refusal is not None:
            return refusal
        refusal = _computer_refusal(orch, websocket, user_id, chat_id, tool_name, args)
        if refusal is not None:
            return (refusal, [Alert(message=refusal, variant="error").to_dict()])
    if classification is None:
        return None

    if agent_id == "computer-use-1" and tool_name == "run_command":
        refusal = _resolve_command_folder(orch, user_id, chat_id, args)
        if refusal is not None:
            return refusal

    opens = (tool_name == "browser_navigate" and "url" in args
             or tool_name == "browser_tabs" and args.get("url") is not None)
    if agent_id == "computer-use-1" and opens and _computer_policy().canonical_browser_url(args["url"]) is None:
        _audit_sync(user_id, "remote_op.refused_url", f"refused {tool_name} to a rejected address",
                    machine_id=machine_ref, verb=tool_name, outcome="failure", chat_id=chat_id)
        return ("url_rejected: the browser pane only opens http and https addresses with a plain host name.",
                        [Alert(message="That address cannot be opened in the browser pane.", variant="error").to_dict()])

    if policy.is_destructive is not None:
        destructive = bool(policy.is_destructive(tool_name, args))
    else:
        destructive = _is_destructive(orch, user_id, tool_name, args, classification)
    if not destructive:
        return None

    message = "This remote operation requires attended authorization, which this node does not provide."
    _audit_sync(user_id, "remote_op.refused", "attended authorization unavailable",
                machine_id=machine_ref, verb=tool_name, outcome="failure", chat_id=chat_id)
    return message, [Alert(message=message, variant="error").to_dict()]
