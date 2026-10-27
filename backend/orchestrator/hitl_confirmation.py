"""Holds attended human-in-the-loop approvals bound to one server-held pending call for
fifteen minutes, never client-supplied arguments; handle_decision() re-enters evaluate() to
release the approved dispatch, and computer task grants from computer_sessions stand in for a card.
Owner settings surfaces confirm destructive controls through a single-use confirmation bound to
the same signed-in socket, owner, action and target.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, replace
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from astralprims import Alert, Button, Card, CodeBlock, Image, Text

from orchestrator.hitl import confirmation_request
from shared.protocol import MCPResponse

logger = logging.getLogger(__name__)
TTL_SECONDS = 900
SURFACE_CONFIRMATION_TTL_S = 120
SURFACE_CONFIRMATIONS_PER_OWNER = 16
_SURFACE_CONFIRMATIONS_TOTAL = 128
POINTER_VERBS = frozenset({"click", "double_click", "right_click", "move", "drag", "scroll"})
POINTER_REFUSAL = "Call list_windows and pass hwnd for the target window."
CONTROL_VERBS = frozenset({"interrupt_command"})
CONTINUATION_TEXT = ("The exact approved action completed. Continue the remaining original task using current "
                     "observations; any further effects still require their ordinary authorization and review.")
REMOTE_REVIEWED_VERBS = frozenset({"run_command"})
_BUNDLE_ID = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


@dataclass(frozen=True)
class GrantOffer:
    label: str | None
    scope: dict
    verb_classes: tuple
    command: dict | None = None
    on_approve: bool = False


@dataclass(frozen=True)
class GrantRequest:
    target: dict
    offers: tuple = ()

    @property
    def offer(self) -> GrantOffer | None:
        return self.offers[0] if self.offers else None


@dataclass(frozen=True)
class PendingCall:
    request_id: str
    owner: str
    chat: str
    agent: str
    tool: str
    arguments: str
    risks: tuple[str, ...]
    expires: float
    task: object = None
    computer_authority: object = None
    remote_proposal: str | None = None
    websocket: object = None
    cancellation: object = None
    grant_offers: tuple = ()
    grant_id: str | None = None


@dataclass(frozen=True)
class SurfaceConfirmation:
    confirmation_id: str
    owner: str
    websocket: object
    action: str
    target: str
    expires: float


@dataclass
class Approval:
    orchestrator: object
    call: PendingCall
    consumed: bool = False
    effect_started: bool = False
    terminal_input_used: bool = False


_APPROVAL = contextvars.ContextVar("hitl_approval", default=None)
_SERVER_ARGUMENTS = frozenset({"_credentials", "_credentials_encrypted", "_session_llm_credentials",
                               "_delegation_token", "_cap_job_id"})
_DESTINATION_FIELDS = frozenset({"to", "cc", "bcc", "recipient", "recipients", "email", "to_email",
                                 "phone", "destination"})
_SECRET_MARKERS = re.compile(r"(?i)(?:api[_ -]?key|access[_ -]?token|secret|password|authorization|"
                             r"bearer |credential|-----BEGIN|\bsk-[\w-]{12,}|\beyJ[\w-]+\.[\w-]+\.[\w-]+)")
_EMAIL_DESTINATION = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_DESTINATION = re.compile(r"\+?[0-9(). -]{10,24}")


def _routing_address(key, value) -> bool:
    if key.lower() not in _DESTINATION_FIELDS or not isinstance(value, str) or len(value) > 320:
        return False
    return bool(_EMAIL_DESTINATION.fullmatch(value) or (
        _PHONE_DESTINATION.fullmatch(value)
        and 10 <= sum(character.isdigit() for character in value) <= 15))


def _arguments(args) -> str:
    if not isinstance(args, dict):
        raise ValueError("confirmation arguments must be an object")
    encoded = json.dumps(args, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("confirmation arguments exceed limit")
    return encoded


def _pending(orch) -> dict[str, PendingCall]:
    current = getattr(orch, "_hitl_pending_calls", {})
    current = {key: call for key, call in current.items() if call.expires > time.monotonic()}
    orch._hitl_pending_calls = current
    return current


def _attended(orch, websocket, owner: str) -> bool:
    from orchestrator.async_tasks import VirtualWebSocket

    if websocket is None or isinstance(websocket, VirtualWebSocket):
        return False
    claims = getattr(orch, "ui_sessions", {}).get(websocket)
    return (isinstance(claims, dict) and claims.get("sub") == owner
            and not claims.get("machine_class") and claims.get("_invocation_channel") != "mcp")


def _current_session(orch, websocket, call: PendingCall, *, allow_local_pause=False) -> bool:
    from orchestrator.remote_confirmation import computer_approval_current

    return (_attended(orch, websocket, call.owner)
            and (call.websocket is None or call.websocket is websocket)
            and call.cancellation is not None
            and cancellation_witness(orch, websocket, call.owner, call.chat) is call.cancellation
            and getattr(orch, "_ws_active_chat", {}).get(id(websocket)) == call.chat
            and call.expires > time.monotonic()
            and not getattr(orch, "cancelled_sessions", {}).get(id(websocket), False)
            and (call.task is None or _task(orch, websocket, call.owner, call.chat) is call.task)
            and (call.computer_authority is None
                 or computer_approval_current(orch, websocket, call.computer_authority,
                                               allow_local_pause=allow_local_pause)))


def begin_task(orch, websocket, owner, chat) -> None:
    if not owner or not chat or not _attended(orch, websocket, owner):
        return
    tasks = getattr(orch, "_hitl_task_origins", {})
    tasks.pop((owner, chat), None)
    tasks[(owner, chat)] = (websocket, SimpleNamespace(task_id=uuid.uuid4().hex))
    while len(tasks) > 256:
        tasks.pop(next(iter(tasks)))
    orch._hitl_task_origins = tasks


def _task(orch, websocket, owner, chat):
    task = getattr(orch, "_hitl_task_origins", {}).get((owner, chat))
    return task if task is not None and task[0] is websocket else None


def cancel_pending(orch, websocket, owner, chat) -> None:
    getattr(orch, "_hitl_cancellation_witnesses", {}).pop((websocket, owner, chat), None)
    pending = getattr(orch, "_hitl_pending_calls", {})
    for request_id, call in list(pending.items()):
        if (call.owner, call.chat, call.websocket) == (owner, chat, websocket):
            pending.pop(request_id, None)


def cancellation_witness(orch, websocket, owner, chat, *, create=False):
    now = time.monotonic()
    witnesses = getattr(orch, "_hitl_cancellation_witnesses", {})
    for key, (_witness, expires) in list(witnesses.items()):
        if expires <= now:
            witnesses.pop(key, None)
    key = (websocket, owner, chat)
    entry = witnesses.get(key)
    if create:
        witness = entry[0] if entry is not None else object()
        witnesses.pop(key, None)
        witnesses[key] = (witness, now + TTL_SECONDS)
        while len(witnesses) > 256:
            witnesses.pop(next(iter(witnesses)))
        orch._hitl_cancellation_witnesses = witnesses
        return witness
    return entry[0] if entry is not None else None


def matching_approval(orch, owner, chat, agent, tool, args) -> bool:
    approval = _APPROVAL.get()
    if not isinstance(approval, Approval) or approval.orchestrator is not orch or approval.consumed:
        return False
    call = approval.call
    return (call.expires > time.monotonic()
            and (call.owner, call.chat, call.agent, call.tool) == (owner, chat, agent, tool)
            and call.arguments == _arguments(args))


def reviewed_remote_proposal(orch, owner, chat, agent, tool, args):
    return _APPROVAL.get().call.remote_proposal if matching_approval(orch, owner, chat, agent, tool, args) else None


def current_approval(orch, owner, chat, agent, tool, args) -> Approval | None:
    approval = _APPROVAL.get()
    if not isinstance(approval, Approval) or approval.orchestrator is not orch:
        return None
    call = approval.call
    if (not approval.consumed or approval.effect_started or call.expires <= time.monotonic()
            or (call.owner, call.chat, call.agent, call.tool) != (owner, chat, agent, tool)
            or not _current_session(orch, call.websocket, call)):
        return None
    if not isinstance(args, dict):
        return None
    original = json.loads(call.arguments)
    compared = {key: value for key, value in args.items()
                if key in original or key not in _SERVER_ARGUMENTS}
    for key, expected in (("user_id", owner), ("session_id", chat)):
        if key not in original and compared.get(key) == expected:
            compared.pop(key)
    try:
        return approval if _arguments(compared) == call.arguments else None
    except (TypeError, ValueError):
        return None


def approved_call(orch, owner, chat, agent, tool) -> bool:
    approval = _APPROVAL.get()
    return (isinstance(approval, Approval) and approval.orchestrator is orch
            and (approval.call.owner, approval.call.chat, approval.call.agent, approval.call.tool)
            == (owner, chat, agent, tool))


def consume_terminal_input(orch, session, tool, args) -> bool:
    approval = _APPROVAL.get()
    if (not isinstance(approval, Approval) or approval.orchestrator is not orch or not approval.consumed
            or not approval.effect_started or approval.terminal_input_used):
        return False
    call = approval.call
    authority = call.computer_authority
    if (call.agent != "computer-use-1" or call.tool != tool or tool not in {"type_text", "press_keys"}
            or authority is None or authority.session is not session
            or "terminal_input" not in call.risks or not _current_session(orch, authority.websocket, call)):
        return False
    original = json.loads(call.arguments)
    compared = {key: value for key, value in args.items()
                if key in original or key not in _SERVER_ARGUMENTS | {"_runtime"}}
    for key, expected in (("user_id", call.owner), ("session_id", call.chat)):
        if key not in original and compared.get(key) == expected:
            compared.pop(key)
    if _arguments(compared) != call.arguments:
        return False
    approval.terminal_input_used = True
    return True


def computer_effect_generation(orch, session):
    approval = _APPROVAL.get()
    if not isinstance(approval, Approval) or approval.orchestrator is not orch or approval.call.agent != "computer-use-1":
        return None
    authority = approval.call.computer_authority
    return authority.generation if authority is not None and authority.session is session else -1


def computer_effect_current(orch, session, tool) -> bool:
    approval = _APPROVAL.get()
    if approval is None:
        return True
    if not isinstance(approval, Approval) or approval.orchestrator is not orch:
        return False
    call = approval.call
    authority = call.computer_authority
    return (call.agent == "computer-use-1" and call.tool == tool and approval.consumed
            and approval.effect_started and authority is not None and authority.session is session
            and _current_session(orch, authority.websocket, call))


def effect_refusal(orch, websocket, owner, chat, agent, tool, args, *, start=False) -> MCPResponse | None:
    if not approved_call(orch, owner, chat, agent, tool):
        return None
    approval = _APPROVAL.get()
    if (not approval.consumed or approval.effect_started
            or not _current_session(orch, websocket, approval.call)):
        return _refused("Approval is no longer valid for this session or was already used. Reissue the action.")
    original = json.loads(approval.call.arguments)
    compared = {key: value for key, value in args.items() if key in original or key not in _SERVER_ARGUMENTS}
    for key, expected in (("user_id", owner), ("session_id", chat)):
        if key not in original and compared.get(key) == expected:
            compared.pop(key)
    if _arguments(compared) != approval.call.arguments:
        return _refused("The action arguments changed after review. Reissue the action for a new approval.")
    if start:
        approval.effect_started = True
    return None


async def _audit(orch, call: PendingCall, transition: str) -> None:
    from audit.recorder import now_utc
    from audit.schemas import AuditEventCreate

    try:
        correlation = str(uuid.UUID(call.request_id))
    except (ValueError, TypeError, AttributeError):
        correlation = str(uuid.uuid5(uuid.NAMESPACE_URL, f"hitl:{call.request_id}"))
    try:
        receipt = await orch.audit_recorder.record(AuditEventCreate(
            actor_user_id=call.owner, auth_principal=call.owner,
            event_class="agent_tool_call", action_type=f"hitl.{transition}",
            description=f"{transition}: {call.tool}", correlation_id=correlation,
            conversation_id=call.chat,
            outcome="interrupted" if transition == "effect_uncertain" else "success",
            inputs_meta={"agent_id": call.agent, "tool": call.tool,
                         "risks": list(call.risks),
                         "arguments_sha256": hashlib.sha256(
                             call.arguments.encode("utf-8")).hexdigest()},
            started_at=now_utc(), completed_at=now_utc(),
        ))
        if "source_bound" in call.risks and receipt is None:
            raise RuntimeError("audit receipt unavailable")
    except Exception:
        if "source_bound" in call.risks:
            raise
        logger.debug("HITL audit unavailable", exc_info=True)


async def audit_pre_effect(orch, call: PendingCall) -> None:
    if "source_bound" not in call.risks:
        raise ValueError("source-bound approval required")
    await _audit(orch, call, "effect_started")


async def audit_effect_outcome(orch, call: PendingCall, *, uncertain: bool) -> None:
    if "source_bound" not in call.risks:
        raise ValueError("source-bound approval required")
    await _audit(orch, call, "effect_uncertain" if uncertain else "effect_response_success")


def _surface_confirmations(orch) -> dict[str, SurfaceConfirmation]:
    now = time.monotonic()
    current = {key: item for key, item in getattr(orch, "_hitl_surface_confirmations", {}).items()
               if item.expires > now}
    orch._hitl_surface_confirmations = current
    return current


def _surface_call(orch, entry: SurfaceConfirmation) -> PendingCall:
    from orchestrator.hitl import IRREVERSIBLE

    chat = getattr(orch, "_ws_active_chat", {}).get(id(entry.websocket))
    return PendingCall(entry.confirmation_id, entry.owner, chat, "owner-surface", entry.action, entry.target,
                       (IRREVERSIBLE,), entry.expires)


async def request_surface_confirmation(orch, websocket, owner, action, target) -> str | None:
    try:
        encoded = _arguments(target)
    except (TypeError, ValueError):
        return None
    if not _attended(orch, websocket, owner):
        return None
    confirmations = _surface_confirmations(orch)
    for item in confirmations.values():
        if (item.websocket is websocket and (item.owner, item.action, item.target) == (owner, action, encoded)):
            return item.confirmation_id
    if (len(confirmations) >= _SURFACE_CONFIRMATIONS_TOTAL
            or sum(item.owner == owner for item in confirmations.values()) >= SURFACE_CONFIRMATIONS_PER_OWNER):
        return None
    entry = SurfaceConfirmation(uuid.uuid4().hex, owner, websocket, action, encoded,
                                time.monotonic() + SURFACE_CONFIRMATION_TTL_S)
    confirmations[entry.confirmation_id] = entry
    await _audit(orch, _surface_call(orch, entry), "proposed")
    return entry.confirmation_id


async def consume_surface_confirmation(orch, websocket, owner, action, target, confirmation_id) -> bool:
    try:
        encoded = _arguments(target)
    except (TypeError, ValueError):
        return False
    confirmations = _surface_confirmations(orch)
    entry = confirmations.get(confirmation_id) if isinstance(confirmation_id, str) else None
    if (entry is None or entry.websocket is not websocket or (entry.owner, entry.action, entry.target)
            != (owner, action, encoded) or not _attended(orch, websocket, owner)):
        return False
    confirmations.pop(confirmation_id)
    await _audit(orch, _surface_call(orch, entry), "consumed")
    return True


def _refused(message: str) -> MCPResponse:
    return MCPResponse(error={"message": message, "retryable": False})


def _input_request(authority, tool, args) -> GrantRequest | None:
    from orchestrator import computer_use_policy as policy
    from orchestrator.computer_grants import is_grantable_application

    if "terminal_app" in args:
        return None
    if tool == "open_app":
        try:
            bundle = policy.mac_application(args.get("app"), args.get("args", []))
        except ValueError:
            return None
        if _BUNDLE_ID.fullmatch(bundle) is None or bundle.lower().endswith(".app"):
            return None
        target, offers = {"bundle_id": bundle}, []
    else:
        hwnd = args.get("hwnd")
        entry = authority.session.window_cache.get(hwnd) if type(hwnd) is int else None
        if entry is None or not entry["bundle_id"]:
            return None
        bundle = entry["bundle_id"]
        if not is_grantable_application(bundle, "window"):
            return None
        target = {"bundle_id": bundle, "hwnd": hwnd}
        offers = [GrantOffer(policy.window_grant_label(policy.application_name(bundle), entry["title"]),
                             {"kind": "window", "bundle_id": bundle, "hwnd": hwnd}, ("input",))]
    if is_grantable_application(bundle, "app"):
        offers.append(GrantOffer(policy.input_grant_label(policy.application_name(bundle)),
                                 {"kind": "app", "bundle_id": bundle}, ("input",)))
    return GrantRequest(target, tuple(offers)) if offers else None


def _command_request(authority, args) -> GrantRequest:
    from orchestrator import computer_use_policy as policy

    grant = policy.command_grant(args, authority.host.workspace_roots)
    argv = args.get("argv")
    first = argv[0] if isinstance(argv, list) and argv and isinstance(argv[0], str) else ""
    target = {"argv0": grant["argv0"] if grant else first, "cwd": args.get("cwd"), "destructive": grant is None}
    if grant is None:
        return GrantRequest(target)
    return GrantRequest(target, (GrantOffer(policy.command_grant_label(grant["argv0"], grant["folder"]),
                                            {"kind": "command"}, ("command",),
                                            {"argv0": grant["argv0"], "root": grant["root"]}),))


def _browser_request(authority, tool, args) -> GrantRequest:
    from agents.computer_use import browser_tools

    target = dict(browser_tools.browser_target(tool, args, browser_tools.page_url(authority.session.session_id, args)))
    origin = browser_tools.origin_of(args.get("url")) if tool == "browser_navigate" else None
    if origin is None or target.get("destructive") or target.get("origin") != origin:
        return GrantRequest(target)
    return GrantRequest(target, (GrantOffer(None, {"kind": "origin", "origin": origin}, ("browser",),
                                            on_approve=True),))


def grant_request(authority, tool, args) -> GrantRequest | None:
    from orchestrator.computer_grants import INPUT_GRANT_VERBS, ORIGIN_GRANT_VERBS

    if authority is None or authority.host.platform != "macos" or not isinstance(args, dict):
        return None
    if tool in INPUT_GRANT_VERBS:
        return _input_request(authority, tool, args)
    if tool == "run_command":
        return _command_request(authority, args)
    if tool in ORIGIN_GRANT_VERBS:
        return _browser_request(authority, tool, args)
    return None


def _match_grant(orch, authority, owner, chat, tool, request):
    return orch.computer_sessions.grants.match(
        authority.session.session_id, tool, request.target, now=time.time(), owner_id=owner,
        host_id=authority.host.host_id, chat_id=chat)


def grant_covers(orch, websocket, owner, chat, tool, args) -> bool:
    from orchestrator.remote_confirmation import capture_computer_approval

    authority = capture_computer_approval(orch, websocket, owner, chat, args)
    request = grant_request(authority, tool, args)
    return request is not None and _match_grant(orch, authority, owner, chat, tool, request) is not None


def _tainted_sink(orch, chat, agent, tool, args) -> bool:
    from orchestrator import taint

    if not taint.taint_enabled() or not taint.is_sink(agent, tool):
        return False
    try:
        trust = orch._taint_tracker(chat).effective_trust_of_args(args, user_text=orch._current_request_text(chat))
    except Exception:
        logger.warning("Taint state unavailable; the grant does not apply", exc_info=True)
        return True
    return taint.check_flow(trust) == "deny"


async def _dispatch_approved(orch, websocket, call) -> MCPResponse:
    token = _APPROVAL.set(Approval(orch, call))
    try:
        tool_call = SimpleNamespace(id=f"grant-{call.request_id}", function=SimpleNamespace(
            name=call.tool, arguments=call.arguments))
        response = await orch.execute_single_tool(websocket, tool_call, {call.tool: call.agent}, call.chat,
                                                  user_id=call.owner)
    finally:
        _APPROVAL.reset(token)
    return response if response is not None else MCPResponse(error={
        "code": "outcome_uncertain", "message": "outcome_uncertain: inspect current state before retrying",
        "retryable": False})


async def granted_dispatch(orch, websocket, owner, chat, agent, tool, args, risks, *, authority=None):
    from orchestrator.hitl import CROSS_PRINCIPAL, IRREVERSIBLE, UNTRUSTED_TAINTED
    from orchestrator.remote_confirmation import capture_computer_approval, source_review_proposal

    if agent != "computer-use-1" or approved_call(orch, owner, chat, agent, tool):
        return None
    authority = authority or capture_computer_approval(orch, websocket, owner, chat, args)
    if authority is None:
        return None
    grant = request = None
    if tool in CONTROL_VERBS:
        job = args.get("job_id")
        if not isinstance(job, str) or job not in authority.session.command_jobs:
            return None
    else:
        if ({"terminal_input", CROSS_PRINCIPAL, IRREVERSIBLE, UNTRUSTED_TAINTED} & set(risks)
                or _tainted_sink(orch, chat, agent, tool, args)):
            return None
        request = grant_request(authority, tool, args)
        grant = _match_grant(orch, authority, owner, chat, tool, request) if request is not None else None
        if grant is None:
            return None
    call = PendingCall(str(uuid.uuid4()), owner, chat, agent, tool, _arguments(args), tuple(sorted(risks)),
                       time.monotonic() + TTL_SECONDS, _task(orch, websocket, owner, chat), authority,
                       source_review_proposal(orch, owner, chat, agent, tool, args), websocket,
                       cancellation_witness(orch, websocket, owner, chat, create=True),
                       grant_id=grant.grant_id if grant is not None else None)
    if not _current_session(orch, websocket, call):
        return None
    try:
        await _audit(orch, call, "granted" if grant is not None else "control")
    except Exception:
        return _refused("Action audit is unavailable. The action was not run.")
    if grant is not None and orch.computer_sessions.grants.touch(
            grant.grant_id, now=time.time(), verb=tool, target=request.target) is None:
        return None
    return await _dispatch_approved(orch, websocket, call)


def _consumed_command_review(orch, owner, chat, tool, args, proposal_id) -> bool:
    from orchestrator.remote_confirmation import _fingerprint, _proposal_context

    context = _proposal_context(orch)
    row = context.call(context.repository.get, owner_id=owner, proposal_id=proposal_id)
    return (row is not None and row.status == "consumed" and row.expires_at > time.time()
            and (row.owner_id, row.conversation_id, row.agent_id, row.tool_name)
            == (owner, chat, "computer-use-1", tool) and row.args_fingerprint == _fingerprint(args))


async def remote_reviewed_dispatch(orch, websocket, owner, chat, agent, tool, args, risks, remote_proposal, authority):
    if (agent != "computer-use-1" or tool not in REMOTE_REVIEWED_VERBS or not isinstance(remote_proposal, str)
            or authority is None or approved_call(orch, owner, chat, agent, tool)):
        return None
    now = time.monotonic()
    used = {key: expiry for key, expiry in getattr(orch, "_hitl_remote_reviews_used", {}).items() if expiry > now}
    orch._hitl_remote_reviews_used = used
    entry = getattr(orch, "_computer_approval_authorities", {}).get(remote_proposal)
    reviewed = entry[0] if entry is not None and entry[1] > now else None
    if remote_proposal in used or reviewed is None or reviewed.session is not authority.session:
        return None
    try:
        consumed = await asyncio.to_thread(_consumed_command_review, orch, owner, chat, tool, args, remote_proposal)
    except Exception:
        logger.warning("Remote command review could not be verified", exc_info=True)
        return None
    if not consumed or remote_proposal in used or len(used) >= 1024:
        return None
    used[remote_proposal] = now + TTL_SECONDS
    call = PendingCall(str(uuid.uuid4()), owner, chat, agent, tool, _arguments(args), tuple(sorted(risks)),
                       now + TTL_SECONDS, _task(orch, websocket, owner, chat), authority, remote_proposal, websocket,
                       cancellation_witness(orch, websocket, owner, chat, create=True))
    if not _current_session(orch, websocket, call):
        return None
    try:
        await _audit(orch, call, "remote_reviewed")
    except Exception:
        return _refused("Action audit is unavailable. The action was not run.")
    return await _dispatch_approved(orch, websocket, call)


def _computer_points(tool, args):
    names = (("x1", "y1"), ("x2", "y2")) if tool == "drag" else (("x", "y"),)
    points = [(args.get(x), args.get(y)) for x, y in names]
    return points if all(type(v) is int and 0 <= v <= 32767 for point in points for v in point) else []


async def _computer_context(authority, tool, args) -> list:
    from orchestrator import computer_use_policy as policy

    hwnd = args.get("hwnd")
    entry = authority.session.window_cache.get(hwnd) if type(hwnd) is int else None
    parts = []
    if entry is not None:
        title = policy.input_grant_label("", entry["title"]).partition("window ")[2] if entry["title"] else ""
        parts.append(Text(content=f"Application: {policy.application_name(entry['bundle_id'])}"
                                  + (f" · Window: {title}" if title else ""), variant="body"))
    points = _computer_points(tool, args) if tool in POINTER_VERBS else []
    crop = await asyncio.to_thread(authority.session.pointer_crop, points) if points else None
    if crop:
        parts.append(Image(url=f"data:image/jpeg;base64,{crop}", width="100%",
                           alt="Untrusted screen crop around the marked target from the latest screenshot"))
    return parts


def _review_summary(args: dict) -> str:
    fields = ", ".join(str(key)[:64] for key in args if not str(key).startswith("_"))[:512]
    description = f"Fields supplied: {fields or 'none'}."
    url = args.get("url")
    if isinstance(url, str):
        try:
            host = urlsplit(url).hostname
            if host:
                description = f"Destination host: {host[:253]}. {description}"
        except ValueError:
            description = f"Destination URL needs review. {description}"
    return description


def _review_arguments(args: dict) -> tuple[str, bool]:
    from orchestrator.hitl import _SENSITIVE_INPUT, sensitive_url
    from personalization.phi_gate import get_phi_gate
    from shared.phi_redactor import PHI_FIELD_PATTERNS, redact

    reviewable = True
    visible_strings = []

    def walk(value, key=""):
        nonlocal reviewable
        destination = _routing_address(key, value)
        sensitive_key = _SECRET_MARKERS.search(key) or any(
            marker in key.lower() for marker in ("password", "credential", "authorization", "cookie", "private_key"))
        if sensitive_key or (not destination and key not in {"url", "filename", "file_name", "hostname"}
                             and any(marker in key.lower() for marker in PHI_FIELD_PATTERNS)):
            reviewable = False
            return "[REDACTED — review unavailable]"
        if isinstance(value, dict):
            return {name: walk(child, name) for name, child in value.items()}
        if isinstance(value, list):
            return [walk(child, key) for child in value]
        if isinstance(value, str):
            decoded = unquote(unquote(value))
            if _SECRET_MARKERS.search(decoded) or (not destination and _SENSITIVE_INPUT.search(decoded)):
                reviewable = False
                return "[REDACTED — review unavailable]"
            if key == "url":
                try:
                    url = urlsplit(decoded)
                    if sensitive_url(url):
                        reviewable = False
                        return "[REDACTED URL credentials]"
                except ValueError:
                    reviewable = False
                    return "[Invalid URL — review unavailable]"
            cleaned, truncated = (value, False) if destination else redact(value)
            if truncated or cleaned != value:
                reviewable = False
            if not destination:
                visible_strings.append(value)
            return cleaned
        return value

    preview = json.dumps(walk(args), indent=2, ensure_ascii=False)
    if len(preview) > 4096:
        return "[Arguments exceed the complete review limit. Use a smaller action.]", False
    if reviewable and visible_strings:
        try:
            if get_phi_gate().contains_phi("\n".join(visible_strings)):
                return "[Private identifiers cannot be shown in an approval preview.]", False
        except Exception:
            return "[Privacy review unavailable. Reissue the action when it is available.]", False
    return preview, reviewable


async def evaluate(orch, websocket, owner, chat, agent, tool, args, risks) -> MCPResponse | None:
    try:
        encoded = _arguments(args)
        witness = cancellation_witness(orch, websocket, owner, chat, create=True)
        task = _task(orch, websocket, owner, chat)
        if matching_approval(orch, owner, chat, agent, tool, args):
            approval = _APPROVAL.get()
            if not _current_session(orch, websocket, approval.call):
                return _refused("This confirmation is no longer valid for the active session.")
            if approval.call.risks == tuple(sorted(risks)):
                approval.consumed = True
                try:
                    await _audit(orch, approval.call, "consumed")
                except Exception:
                    return _refused("Action audit is unavailable. The action was not run.")
                if not _current_session(orch, websocket, approval.call):
                    return _refused("This confirmation expired or the active session changed. Reissue the action.")
                return None
        if not chat or not _attended(orch, websocket, owner):
            return _refused("Confirmation requires an interactive signed-in conversation. Reissue the action there.")
        try:
            args = json.loads(encoded)
            if agent != "terminal-shell-1":
                args = await asyncio.to_thread(orch._map_file_paths, chat, args, user_id=owner)
            encoded = _arguments(args)
        except Exception:
            return _refused("Review unavailable: action targets could not be resolved. Reissue the action when they are available.")
        shown = {key: value for key, value in args.items()
                 if not (key == "computer" and agent == "computer-use-1") and not (key == "user_id" and value == owner)}
        preview, reviewable = await asyncio.to_thread(_review_arguments, shown)
        if not reviewable:
            return _refused("Review unavailable: this action includes sensitive or oversized arguments that cannot be "
                            "shown completely. Remove private identifiers or credentials, or use a smaller action.")
        if (not _attended(orch, websocket, owner)
                or getattr(orch, "_ws_active_chat", {}).get(id(websocket)) != chat
                or cancellation_witness(orch, websocket, owner, chat) is not witness):
            return _refused("This confirmation is no longer valid for the active session.")
        pending = _pending(orch)
        from orchestrator.computer_use_policy import HOST_VERBS
        from orchestrator.remote_confirmation import capture_computer_approval, source_review_proposal

        remote_proposal = source_review_proposal(orch, owner, chat, agent, tool, args)
        computer = card_host = None
        if agent == "computer-use-1":
            from orchestrator.computer_hosts import ComputerHostError
            from orchestrator.remote_confirmation import computer_card_host

            try:
                card_host = computer_card_host(orch, owner, chat, args)
            except ComputerHostError as exc:
                return _refused(f"{exc.code}: {exc.message}")
        if agent == "computer-use-1" and tool in HOST_VERBS | {"confirm_action"}:
            computer = capture_computer_approval(orch, websocket, owner, chat, args)
            if computer is None:
                return _refused("The computer session is no longer current. Reissue the action in its active conversation.")
            hwnd = args.get("hwnd")
            if (tool in POINTER_VERBS and computer.host.platform == "macos"
                    and (type(hwnd) is not int or hwnd not in computer.session.window_cache)):
                return _refused(POINTER_REFUSAL)
            granted = (await remote_reviewed_dispatch(orch, websocket, owner, chat, agent, tool, args, risks,
                                                      remote_proposal, computer)
                       or await granted_dispatch(orch, websocket, owner, chat, agent, tool, args, risks,
                                                 authority=computer))
            if granted is not None:
                return granted
        call = next((item for item in pending.values() if
                     (item.owner, item.chat, item.agent, item.tool, item.arguments, item.risks, item.remote_proposal)
                     == (owner, chat, agent, tool, encoded, tuple(sorted(risks)), remote_proposal)
                     and item.task is task and item.websocket is websocket and item.cancellation is witness), None)
        if call is None:
            if len(pending) >= 128 or sum(item.owner == owner for item in pending.values()) >= 32:
                return _refused("Too many pending confirmations. Resolve an existing request and retry.")
            request = grant_request(computer, tool, args)
            offers = tuple(offer for offer in request.offers if offer.label) if request is not None else ()
            call = PendingCall(str(uuid.uuid4()), owner, chat, agent, tool,
                               encoded, tuple(sorted(risks)), time.monotonic() + TTL_SECONDS,
                               task, computer, remote_proposal,
                               websocket, witness, grant_offers=offers)
            pending[call.request_id] = call
            try:
                await _audit(orch, call, "proposed")
            except Exception:
                pending.pop(call.request_id, None)
                return _refused("Action audit is unavailable. The action was not proposed.")
        if not _current_session(orch, websocket, call):
            getattr(orch, "_hitl_pending_calls", {}).pop(call.request_id, None)
            return _refused("This confirmation is no longer valid for the active conversation.")
        summary = confirmation_request(tool, list(call.risks)).summary
        context = await _computer_context(call.computer_authority, tool, args) if call.computer_authority else []
        grant = [Button(label=offer.label, action="authorize_action", variant="secondary",
                        payload={"hitl_request_id": call.request_id, "decision": "grant_task",
                                 "grant_scope": offer.scope["kind"]})
                 for offer in call.grant_offers]
        on_host = [Text(content=f"On {card_host.name}", variant="body")] if card_host is not None else []
        card = Card(title="Review action", id=f"hitl_{call.request_id}", content=[
            Text(content=f"Tool: {tool} · Agent: {agent}", variant="body"),
            *on_host,
            Text(content=summary, variant="body"),
            Text(content=_review_summary(shown), variant="body"),
            *context,
            CodeBlock(code=preview, language="json"),
            Text(content="Approve this exact action once, or decline. This request expires in 15 minutes. "
                         "Privacy rules and current permissions still apply.", variant="caption"),
            Button(label="Approve", action="authorize_action",
                   payload={"hitl_request_id": call.request_id, "decision": "approve"}),
            *grant,
            Button(label="Decline", action="authorize_action", variant="secondary",
                   payload={"hitl_request_id": call.request_id, "decision": "decline"}),
        ]).to_dict()
        if agent == "computer-use-1" and tool == "write_file":
            from orchestrator.write_review import review_components

            index = next(index for index, item in enumerate(card["content"]) if item.get("type") == "code") + 1
            card["content"][index:index] = await review_components(orch, websocket, owner, chat, args,
                                                                   f"hitl_{call.request_id}_diff")
        from orchestrator.remote_confirmation import computer_resume_target

        target = computer_resume_target(websocket, call.computer_authority, tool, args)
        if target is not None:
            index = next(index for index, item in enumerate(card["content"]) if item.get("action") == "authorize_action")
            card["content"][index] = Button(
                label=f"Approve and resume on {call.computer_authority.host.name} / {target}",
                action="authorize_action", payload={"hitl_request_id": call.request_id,
                                                     "decision": "approve_and_resume"}).to_dict()
            card["content"].insert(index, Text(content="This decision explicitly resumes this Mac session. Any focus change "
                "is limited to the reviewed target. Current policy and privacy checks still apply before the exact action.",
                variant="caption").to_dict())
        return MCPResponse(result={"_data": {"status": "confirmation_required",
                           "message": "An approval card is available. End this turn and wait for the user's decision; "
                                      "do not retry the action or ask again in text."}}, ui_components=[card])
    except (TypeError, ValueError):
        return _refused("Confirmation could not be prepared. Correct the action arguments and retry.")


async def continuation_guidance(orch, websocket):
    from orchestrator import evidence_context, user_skills
    from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
    from orchestrator.human_request_authority import current_socket_human_read, retire_socket_human_read
    from orchestrator.turn_guidance_authority import capture_turn_guidance_from_human
    from persistent_agents.models import AssignmentError

    if not user_skills.enabled() and not evidence_context.needs_authority(orch):
        return None, True
    context = _CONNECTION_OPERATION_CONTEXT.get() or {}
    try:
        pending = context.get("human_request")
        if getattr(pending, "purpose", None) == "remote_effect":
            return await capture_turn_guidance_from_human(await pending.authenticate(),
                                                          expected_orchestrator=orch), True
        caller = await current_socket_human_read(expected_orchestrator=orch, websocket=websocket,
                                                 operation_context=context)
        try:
            return await capture_turn_guidance_from_human(caller, expected_orchestrator=orch), True
        finally:
            retire_socket_human_read(caller)
    except AssignmentError as exc:
        logger.warning("Approval continuation guidance unavailable: %s (%s)", exc.code, exc.status_code)
        return None, False


def continue_after_approval(orch, websocket, owner, chat, message, display, origin, *, guard=None, name):
    from orchestrator.detached_context import detached_context
    from persistent_agents.models import AssignmentError

    async def run():
        from orchestrator.turn_guidance_authority import bind_http_guidance, use_turn_guidance

        try:
            if guard is not None and not guard():
                return
            if origin is None:
                await orch._serialized_chat(websocket, message, chat, display, user_id=owner, continuation_guard=guard)
                return
            binding = bind_http_guidance(origin, expected_orchestrator=orch, websocket=websocket, chat_id=chat)
            with use_turn_guidance(binding, expected_orchestrator=orch):
                await orch._serialized_chat(websocket, message, chat, display, user_id=owner, continuation_guard=guard)
        except AssignmentError as exc:
            logger.warning("Approval continuation refused: %s (%s)", exc.code, exc.status_code)
        finally:
            if origin is not None:
                origin.close()

    return asyncio.create_task(run(), context=detached_context(orch), name=name)


async def _finish_card(orch, websocket, call, title, message, components=None):
    try:
        component = Card(id=f"hitl_{call.request_id}", title=title, content=[Text(content=message)]).to_dict()
        cid = orch.workspace.resolve_identity(component)

        async def mutation():
            ops = await orch.workspace.aupsert(call.chat, call.owner, [component], force_component_id=cid)
            if components:
                ops += await orch.workspace.aupsert(call.chat, call.owner, components)
            return ops

        ops = await orch.run_detached_conversation_mutation(chat_id=call.chat, user_id=call.owner, mutation=mutation)
        if ops:
            await orch.send_ui_upsert(None, call.chat, call.owner, ops)
    except Exception:
        logger.debug("HITL card replacement unavailable", exc_info=True)
    await orch.send_ui_render(websocket, [Alert(message=message, variant="info").to_dict()], target="chat")


async def handle_decision(orch, websocket, owner, payload, *, admission_check=None) -> None:
    held = []
    try:
        await _handle_decision(orch, websocket, owner, payload, admission_check, held)
    finally:
        for origin in held:
            origin.close()


def _chosen_offer(call, payload) -> GrantOffer | None:
    if not call.grant_offers:
        return None
    wanted = payload.get("grant_scope", call.grant_offers[0].scope["kind"])
    return next((offer for offer in call.grant_offers if offer.scope["kind"] == wanted), None)


async def _handle_decision(orch, websocket, owner, payload, admission_check, held) -> None:
    pending = _pending(orch)
    request_id = payload.get("hitl_request_id") if isinstance(payload, dict) else None
    call = pending.get(request_id) if isinstance(request_id, str) else None
    resume_requested = isinstance(payload, dict) and payload.get("decision") == "approve_and_resume"
    grant_requested = isinstance(payload, dict) and payload.get("decision") == "grant_task"
    offer = _chosen_offer(call, payload) if grant_requested and call is not None else None
    valid = not (call is None or call.owner != owner or not _attended(orch, websocket, owner)
            or getattr(orch, "_ws_active_chat", {}).get(id(websocket)) != call.chat
            or not {"hitl_request_id", "decision"}.issubset(payload)
            or set(payload) - {"hitl_request_id", "decision", "chat_id", "component_id", "grant_scope"}
            or payload.get("decision") not in ("approve", "approve_and_resume", "grant_task", "decline")
            or "grant_scope" in payload and not grant_requested
            or grant_requested and (offer is None or call.computer_authority is None)
            or "chat_id" in payload and payload["chat_id"] != call.chat
            or admission_check is not None and not admission_check())
    if valid and "component_id" in payload:
        try:
            expected = orch.workspace.resolve_identity({"id": f"hitl_{call.request_id}"})
            if not isinstance(payload["component_id"], str) or payload["component_id"] != expected:
                valid = False
            else:
                row = await orch.workspace.aget_by_component_id(call.chat, owner, expected)
                component = (row or {}).get("component_data", {})
                pressed = {key: payload[key] for key in ("hitl_request_id", "decision", "grant_scope") if key in payload}
                valid = (isinstance(component, dict) and component.get("type") == "card"
                    and component.get("id") == f"hitl_{call.request_id}"
                    and any(isinstance(item, dict) and item.get("type") == "button"
                        and item.get("action") == "authorize_action" and item.get("disabled") is not True
                        and item.get("payload") == pressed
                        for item in component.get("content", [])))
        except Exception:
            valid = False
    pending = _pending(orch)
    if (not valid or pending.get(request_id) is not call
            or not _current_session(orch, websocket, call,
                allow_local_pause=resume_requested or payload["decision"] == "decline")
            or admission_check is not None and not admission_check()):
        await orch.send_ui_render(websocket, [Alert(message="This confirmation is unavailable in this conversation. "
            "It may have expired or already been handled.", variant="warning").to_dict()], target="chat")
        return
    from orchestrator.remote_confirmation import (
        computer_resume_target, pause_failed_computer_approval, resume_computer_approval,
    )

    if resume_requested and computer_resume_target(
            websocket, call.computer_authority, call.tool, json.loads(call.arguments)) is None:
        await orch.send_ui_render(websocket, [Alert(message="This action has no reviewed local resume target.",
                                                  variant="warning").to_dict()], target="chat")
        return
    origin, guided = (await continuation_guidance(orch, websocket) if payload["decision"] != "decline"
                      else (None, False))
    if origin is not None:
        held.append(origin)
    pending = _pending(orch)
    if pending.get(request_id) is not call:
        await orch.send_ui_render(websocket, [Alert(message="This confirmation is unavailable in this conversation. "
            "It may have expired or already been handled.", variant="warning").to_dict()], target="chat")
        return
    # Pop before the await — blocks a concurrent double-click
    pending.pop(request_id)
    try:
        owned_chat = await asyncio.to_thread(orch.history.get_chat, call.chat, user_id=owner)
    except Exception:
        owned_chat = None
    if not owned_chat:
        await _finish_card(orch, websocket, call, "Unavailable", "Conversation access could not be verified. Reissue the action.")
        return
    if admission_check is not None and not admission_check():
        await _finish_card(orch, websocket, call, "Unavailable", "The connection changed. Reissue the action.")
        return
    if not _current_session(orch, websocket, call, allow_local_pause=resume_requested or payload["decision"] == "decline"):
        await _finish_card(orch, websocket, call, "Unavailable", "The session changed or this confirmation expired. Reissue the action.")
        return
    if payload["decision"] == "decline":
        await _audit(orch, call, "declined")
        await _finish_card(orch, websocket, call, "Declined", "Declined — the action was not run.")
        return
    try:
        await _audit(orch, call, "approved")
    except Exception:
        await _finish_card(orch, websocket, call, "Unavailable", "Action audit is unavailable. The action was not run.")
        return
    if (not _current_session(orch, websocket, call, allow_local_pause=resume_requested)
            or admission_check is not None and not admission_check()):
        await _finish_card(orch, websocket, call, "Unavailable", "The session changed or this confirmation expired. Reissue the action.")
        return
    if grant_requested:
        authority = call.computer_authority
        task_id = getattr(call.task[1], "task_id", None) if call.task is not None else None
        try:
            orch.computer_sessions.grants.create(
                call.owner, authority.host.host_id, authority.session.session_id, call.chat,
                task_id or call.request_id, offer.scope, offer.verb_classes, offer.command, now=time.time())
        except ValueError:
            await _finish_card(orch, websocket, call, "Unavailable",
                               "This target stays on exact review. Approve the single action instead.")
            return
    if resume_requested:
        authority = await resume_computer_approval(orch, websocket, call.computer_authority,
                                                   call.tool, json.loads(call.arguments))
        if authority is None:
            await _finish_card(orch, websocket, call, "Unavailable", "The local resume authority changed. Reissue the action.")
            return
        call = replace(call, computer_authority=authority)
    token = _APPROVAL.set(Approval(orch, call))
    try:
        tool_call = SimpleNamespace(id=f"hitl-{call.request_id}", function=SimpleNamespace(
            name=call.tool, arguments=call.arguments))
        response = await orch.execute_single_tool(websocket, tool_call, {call.tool: call.agent}, call.chat, user_id=owner)
    except asyncio.CancelledError:
        if resume_requested:
            await pause_failed_computer_approval(orch, websocket, call.computer_authority)
        await _finish_card(orch, websocket, call, "Outcome uncertain",
                           "The action was interrupted; inspect current state before retrying.")
        raise
    except Exception:
        if resume_requested:
            await pause_failed_computer_approval(orch, websocket, call.computer_authority)
        logger.exception("Approved HITL dispatch failed")
        await _finish_card(orch, websocket, call, "Failed", "The approved action could not complete. Reissue it to retry.")
        return
    finally:
        _APPROVAL.reset(token)
    from orchestrator.remote_confirmation import _computer_use_on_approved, successful_result

    failed = not successful_result(response)
    if failed and resume_requested:
        await pause_failed_computer_approval(orch, websocket, call.computer_authority)
    data = response.result.get("_data", {}) if response and isinstance(response.result, dict) else {}
    uncertain = (response is None or isinstance(data, dict) and (
        data.get("code") == "outcome_uncertain" or data.get("status") in {"uncertain", "outcome_uncertain"})
        or response and isinstance(response.error, dict) and (
            response.error.get("code") == "outcome_uncertain"
            or str(response.error.get("message", "")).startswith("outcome_uncertain:")))
    waiting = isinstance(data, dict) and data.get("status") == "confirmation_required"
    if waiting:
        if resume_requested:
            for request_id, item in list(_pending(orch).items()):
                if item.computer_authority is not None and item.computer_authority.session is call.computer_authority.session:
                    _pending(orch).pop(request_id, None)
            await _finish_card(orch, websocket, call, "Review changed",
                               "The review requirements changed and this Mac remains paused. Resume the session "
                               "explicitly, then reissue the action for a fresh review.")
            return
        await _finish_card(orch, websocket, call, "Further review needed",
                           "The action still needs approval. Review the new card before it can run.",
                           response.ui_components)
        return
    await _finish_card(orch, websocket, call, "Outcome uncertain" if uncertain else "Blocked" if failed else "Approved",
                       "The action outcome is uncertain. Inspect current state before issuing another action." if uncertain
                       else "The approved action was blocked or failed. See the tool message for details." if failed
                       else "Approval used — the action was sent through the normal permission and privacy checks.",
                       response.ui_components if response and not failed else None)
    if failed or not _current_session(orch, websocket, call):
        return
    if call.computer_authority is not None:
        row = SimpleNamespace(owner_id=call.owner, conversation_id=call.chat,
            machine_id=call.computer_authority.host.host_id, tool_name=call.tool,
            arguments=json.loads(call.arguments))
        _computer_use_on_approved(orch, row, websocket, call.computer_authority)
    if call.task is not None and guided:
        if origin is not None:
            held.remove(origin)
        continue_after_approval(orch, websocket, call.owner, call.chat, CONTINUATION_TEXT, "Approved — continuing",
                                origin, guard=lambda: _current_session(orch, websocket, call),
                                name=f"hitl-continue-{call.request_id}")
