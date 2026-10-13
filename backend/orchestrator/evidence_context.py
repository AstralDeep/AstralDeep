"""Binds bounded evidence and context usage to the host's current owner, dispatch, and provider authority.
The conversation loop uses this adapter without granting retained text command authority or durable UI storage.
"""

from __future__ import annotations

import asyncio
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from enum import Enum
import hashlib
import json
import logging
import os
import re
from types import SimpleNamespace
import traceback
from uuid import uuid4

from audit.schemas import AuditEventCreate
from orchestrator.context_presentation import evidence_components, usage_components
from orchestrator.context_budget import load_budget
from orchestrator.context_usage import ContextUsage
from orchestrator.context_views import ContextViewStore, SourceDependency, ViewDenied, ViewUnavailable
from orchestrator.evidence_archive import (
    EvidenceArchive, EvidenceCaptureError, EvidenceDenied, EvidenceError,
    EvidencePolicyError, EvidenceUnavailable, load_grants, match_grant,
)
from orchestrator.safe_compaction import CompactionResult, compact_context, estimate_context_tokens
from shared.feature_flags import flags
from shared.protocol import AgentCard, AgentSkill, MCPResponse

_REQUESTER = ContextVar("evidence_host_requester", default=None)
_PRIVACY_PROOFS = ContextVar("evidence_delivery_privacy_proofs", default=None)
_REFERENCE = re.compile(r"obs_[A-Za-z0-9_-]{43}")
_REFERENCE_TOKENS = re.compile(r"(?<![A-Za-z0-9_])obs_[A-Za-z0-9_-]*")
_SECRET = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+[A-Za-z0-9._~-]+|\b(?:api[_-]?key|password|access[_-]?token|secret)\s*[:=]\s*[^\s,;]{4,}", re.I)
_SERVER_ARGUMENTS = {"user_id", "session_id"}
_PACK_BYTES = 4096
_PRIVACY_BYTES = 65536
_PREVIEW_BYTES = 1024
_DELIVERY_TOKEN = object()
logger = logging.getLogger("EvidenceContext")


@dataclass(frozen=True, slots=True, repr=False)
class _EvidenceDelivery:
    token: object
    authority: object
    owner: str
    chat: str
    websocket: object
    result_digest: str
    component_digest: str
    state_digest: str
    adapter_binding: tuple | None = None


@dataclass(slots=True, repr=False)
class _PrivacyProofMemo:
    service: object
    archive: object
    websocket: object
    owner: str
    chat: str
    authority: object
    request_generation: str
    navigation: object
    operation_context: dict
    proofs: set = field(default_factory=set)
    closed: bool = False

    def current(self, service, observation=None):
        from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
        from orchestrator.context_authority import current_context_authority

        context = _CONNECTION_OPERATION_CONTEXT.get()
        if (self.closed or _PRIVACY_PROOFS.get() is not self or service is not self.service
                or service.archive is not self.archive
                or getattr(service.orchestrator, "_evidence_context", None) is not service
                or context is not self.operation_context
                or str(context.get("request_generation")) != self.request_generation
                or context.get("evidence_navigation") is not self.navigation
                or getattr(self.navigation, "closed", True)
                or getattr(service.orchestrator, "_evidence_navigation", {}).get(id(self.websocket)) is not self.navigation
                or current_context_authority(orchestrator=service.orchestrator, websocket=self.websocket,
                                             chat_id=self.chat) is not self.authority
                or self.authority.owner_id != self.owner):
            raise EvidenceDenied()
        self.authority.assert_current(orchestrator=service.orchestrator, websocket=self.websocket, chat_id=self.chat)
        if observation is not None and (observation.owner_id, observation.conversation_id,
                                       observation.audience_id) != (self.owner, self.chat, f"user:{self.owner}"):
            raise EvidenceDenied()

    def close(self):
        self.closed = True
        self.proofs.clear()


def enabled() -> bool:
    return flags.is_enabled("observation_packing") or flags.is_enabled("safe_compaction")


def needs_authority(orchestrator) -> bool:
    return enabled() or getattr(orchestrator, "_evidence_context", None) is not None


def get_context(orchestrator):
    service = getattr(orchestrator, "_evidence_context", None)
    if service is None and enabled():
        service = EvidenceContext(orchestrator)
        orchestrator._evidence_context = service
    return service


@contextmanager
def dispatch_requester(orchestrator, parent_token, initiating_agent_id, owner, chat, websocket):
    token = _REQUESTER.set((orchestrator, copy.deepcopy(parent_token), initiating_agent_id, owner, chat, websocket))
    try:
        yield
    finally:
        _REQUESTER.reset(token)


def requester_binding(orchestrator) -> dict:
    value = _REQUESTER.get()
    if value is None or value[0] is not orchestrator:
        return {"requester_verified": False}
    return {"requester_verified": True, "requester_parent": copy.deepcopy(value[1]), "requester_agent": value[2],
            "requester_owner": value[3], "requester_chat": value[4], "requester_websocket": value[5]}


def transient_result(result, agent_id) -> bool:
    return result is not None and (agent_id == "evidence-1" or getattr(result, "_evidence_transient", False) is True)


def _literal_read_tool(result):
    data = result.result
    if type(data) is not dict:
        return None
    if data.get("view") == "generated_summary":
        return "inspect_context_view"
    return "recall_observation" if data.get("reference") is not None else None


def has_references(messages) -> bool:
    return bool(_REFERENCE_TOKENS.search(_json(messages)))


def command(message: str):
    if not message.startswith("/evidence"):
        return None
    pieces = message.split()
    if pieces == ["/evidence", "usage"]:
        return "context_usage", {}
    if len(pieces) == 3 and pieces[1] == "delete" and _REFERENCE.fullmatch(pieces[2]):
        return "delete_observation", {"reference": pieces[2]}
    if (len(pieces) in {3, 4} and pieces[1] == "recall" and _REFERENCE.fullmatch(pieces[2])
            and (len(pieces) == 3 or pieces[3].isascii() and pieces[3].isdigit() and len(pieces[3]) <= 8)):
        return "recall_observation", {"reference": pieces[2], "offset": int(pieces[3]) if len(pieces) == 4 else 0}
    return "", {}


def _encode(value):
    from pydantic import BaseModel

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return {"byte_digest": hashlib.sha256(value).hexdigest()}
    from uuid import UUID
    if isinstance(value, UUID):
        return str(value)
    raise TypeError("context_snapshot_invalid")


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"), default=_encode)


def _digest(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _privacy_method(value):
    return id(getattr(value, "__self__", None)), id(getattr(value, "__func__", value))


def _privacy_fields(value, names):
    fields = {}
    for name in names:
        item = getattr(value, name, None)
        if isinstance(item, (set, frozenset)):
            if len(item) > 256 or any(type(part) is not str for part in item):
                raise EvidenceDenied()
            item = sorted(item)
        fields[name] = item
    return fields


def _privacy_snapshot():
    from personalization import phi_gate
    from shared.phi_redactor import PHI_VALUE_PATTERNS

    gate = phi_gate.get_phi_gate()
    analyzer, redact = getattr(gate, "_analyzer", None), getattr(gate, "redact_for_storage", None)
    analyze = getattr(analyzer, "analyze", None)
    if getattr(gate, "available", None) is not True or not callable(redact) or not callable(analyze):
        raise EvidenceDenied()
    registry = getattr(analyzer, "registry", None)
    recognizers = getattr(registry, "recognizers", [])
    if type(recognizers) not in (list, tuple) or len(recognizers) > 128:
        raise EvidenceDenied()
    records = []
    for recognizer in recognizers:
        patterns = getattr(recognizer, "patterns", [])
        if type(patterns) not in (list, tuple) or len(patterns) > 256:
            raise EvidenceDenied()
        records.append({"identity": id(recognizer), "analyze": _privacy_method(getattr(recognizer, "analyze", None)),
            "validate": _privacy_method(getattr(recognizer, "validate_result", None)),
            "invalidate": _privacy_method(getattr(recognizer, "invalidate_result", None)),
            "config": _privacy_fields(recognizer, ("name", "version", "supported_entities", "supported_language",
                "context", "deny_list", "deny_list_score", "global_regex_flags", "score_thresholds", "_country_code")),
            "patterns": [{"identity": id(pattern), **_privacy_fields(pattern, ("name", "regex", "score"))}
                         for pattern in patterns]})
    enhancer, engine = getattr(analyzer, "context_aware_enhancer", None), getattr(analyzer, "nlp_engine", None)
    loaded = getattr(engine, "nlp", {})
    if type(loaded) is not dict or len(loaded) > 128:
        raise EvidenceDenied()
    snapshot = {"gate": id(gate), "analyzer": id(analyzer), "available": gate.available,
        "threshold": getattr(gate, "_score_threshold", None), "analyze": _privacy_method(analyze),
        "redact": _privacy_method(redact), "contains": _privacy_method(getattr(gate, "contains_phi", None)),
        "entities": phi_gate.PHI_ENTITIES,
        "patterns": [(pattern.pattern, pattern.flags, replacement) for pattern, replacement in PHI_VALUE_PATTERNS],
        "prefilter": [(pattern.pattern, pattern.flags) for pattern in phi_gate._PREFILTER_PATTERNS],
        "registry": id(registry), "registry_config": _privacy_fields(registry, ("supported_languages", "global_regex_flags")),
        "recognizers": records, "analyzer_config": _privacy_fields(analyzer, ("supported_languages", "default_score_threshold")),
        "enhancer": id(enhancer), "enhance": _privacy_method(getattr(enhancer, "enhance_using_context", None)),
        "enhancer_config": _privacy_fields(enhancer, ("context_similarity_factor", "min_score_with_context_similarity",
                                                     "context_prefix_count", "context_suffix_count")),
        "engine": id(engine), "process": _privacy_method(getattr(engine, "process_text", None)),
        "engine_models": getattr(engine, "models", None),
        "engine_loaded": {language: id(model) for language, model in loaded.items()},
        "ner_config": _privacy_fields(getattr(engine, "ner_model_configuration", None),
            ("labels_to_ignore", "aggregation_strategy", "alignment_mode", "model_to_presidio_entity_mapping",
             "low_score_entity_names", "low_confidence_score_multiplier", "default_score"))}
    pending, nodes = [(snapshot, 0)], 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > 8192 or depth > 8:
            raise EvidenceDenied()
        if isinstance(value, dict):
            if len(value) > 256 or any(type(key) is not str or len(key) > 256 for key in value):
                raise EvidenceDenied()
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, (list, tuple)):
            if len(value) > 256:
                raise EvidenceDenied()
            pending.extend((item, depth + 1) for item in value)
        elif isinstance(value, str):
            if len(value) > 16384:
                raise EvidenceDenied()
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise EvidenceDenied()
    serialized = _json(snapshot)
    if len(serialized.encode("utf-8")) > 65536:
        raise EvidenceDenied()
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest(), redact


def _public_arguments(args) -> dict:
    return {key: copy.deepcopy(value) for key, value in args.items()
            if type(key) is str and not key.startswith("_") and key not in _SERVER_ARGUMENTS}


def _outcome(value) -> str:
    if not isinstance(value, dict):
        return "success"
    status = value.get("status")
    if status in {"error", "denied", "denial", "failure", "failed", "cancelled", "interrupted", "incomplete"}:
        return status
    if value.get("complete") is False or value.get("partial") is True:
        return "incomplete"
    return "success"


class EvidenceContext:
    def __init__(self, orchestrator, *, archive=None, usage=None, clock=None):
        self.orchestrator = orchestrator
        self.clock = clock or (lambda: datetime.now(UTC))
        self.archive = archive or EvidenceArchive(clock=self.clock)
        self.views = ContextViewStore(clock=self.clock, view_limit_bytes=16384)
        self.usage = usage or ContextUsage(self._persist, self._recover)
        self._sources = {}
        self._retained_views = {}
        self._sweep_task = None

    @contextmanager
    def privacy_delivery(self, *, websocket, owner, chat, request_generation):
        from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
        from orchestrator.context_authority import current_context_authority

        context = _CONNECTION_OPERATION_CONTEXT.get()
        if type(context) is not dict or type(request_generation) is not str:
            raise EvidenceDenied()
        authority = current_context_authority(orchestrator=self.orchestrator, websocket=websocket, chat_id=chat)
        memo = _PrivacyProofMemo(self, self.archive, websocket, owner, chat, authority,
                                request_generation, context.get("evidence_navigation"), context)
        token = _PRIVACY_PROOFS.set(memo)
        try:
            memo.current(self)
            yield
        finally:
            memo.close()
            _PRIVACY_PROOFS.reset(token)

    def _source_states(self, owner, chat):
        states = {}
        for reference, observation in tuple(self._sources.items()):
            if observation.owner_id != owner or observation.conversation_id != chat:
                continue
            grant = self._grant(owner, chat, observation.source_agent, observation.source_tool)
            current = self.archive.inspect(reference, owner_id=owner, conversation_id=chat,
                                           audience_id=f"user:{owner}")
            if current.grant_fingerprint != grant.fingerprint:
                raise EvidenceDenied()
            self.archive.read(reference, grant=grant, owner_id=owner,
                              conversation_id=chat, audience_id=f"user:{owner}")
            states[reference] = {"integrity": current.integrity_identity, "grant": grant.fingerprint,
                                 "expires_at": current.expires_at}
        return states

    def _check_sources(self, messages, owner, chat):
        text = json.dumps(messages, ensure_ascii=False, default=lambda item: item.model_dump(mode="json"))
        for reference in set(_REFERENCE_TOKENS.findall(text)):
            if not _REFERENCE.fullmatch(reference):
                raise EvidenceDenied()
            current = self.archive.inspect(reference, owner_id=owner, conversation_id=chat,
                                           audience_id=f"user:{owner}")
            grant = self._grant(owner, chat, current.source_agent, current.source_tool)
            self.archive.read(reference, grant=grant, owner_id=owner,
                              conversation_id=chat, audience_id=f"user:{owner}")

    def _source_stamp(self, websocket, owner, chat, observation, parent):
        from orchestrator import delegation, policy
        from orchestrator.tool_permissions import turn_permission_memo
        from personalization.phi_gate import get_phi_gate

        session = self.orchestrator.ui_sessions.get(websocket)
        if (not isinstance(session, dict) or session.get("sub") != owner
                or self.orchestrator.cancelled_sessions.get(id(websocket))):
            raise EvidenceDenied()
        with turn_permission_memo():
            if not self.orchestrator.tool_permissions.is_tool_allowed(owner, observation.source_agent, observation.source_tool):
                raise EvidenceDenied()
        if parent is not None:
            scope = self.orchestrator.tool_permissions.get_tool_scope(observation.source_agent, observation.source_tool)
            allowed, _reason = delegation.authorize_chained_tool_call(parent, observation.source_tool, scope)
            if parent.get("sub") != owner or not allowed:
                raise EvidenceDenied()
        blocked = self.orchestrator.security_flags.get(observation.source_agent, {}).get(observation.source_tool, {})
        if blocked.get("blocked"):
            raise EvidenceDenied()
        card = self.orchestrator.agent_cards.get(observation.source_agent)
        if card is None:
            raise EvidenceDenied()
        raw = (os.getenv("POLICY_RULES") or "[]") if policy.policy_enabled() else "[]"
        try:
            rules = json.loads(raw)
            if type(rules) is not list or any(type(rule) is not dict or rule.get("effect") not in {
                    policy.ALLOW, policy.DENY, policy.CONFIRM, policy.REWRITE, policy.REQUIRE_TOKEN} for rule in rules):
                raise ValueError
            decision = policy.evaluate_policy(rules, {"agent": observation.source_agent, "tool": observation.source_tool,
                                                      "user_id": owner, "roles": self.orchestrator._policy_roles(websocket),
                                                      "args": observation.source_args})
            if decision.effect != policy.ALLOW or decision.args is not None and decision.args != observation.source_args:
                raise ValueError
        except (TypeError, ValueError):
            raise EvidenceDenied() from None
        pending = {key: asdict(call) for key, call in getattr(self.orchestrator, "_hitl_pending_calls", {}).items()
                   if call.owner == owner and call.chat == chat}
        privacy_gate = get_phi_gate()
        analyzer = getattr(privacy_gate, "_analyzer", None)
        analyze = getattr(analyzer, "analyze", None)
        redact = getattr(privacy_gate, "redact_for_storage", None)
        privacy = {"gate": id(privacy_gate), "available": getattr(privacy_gate, "available", None) is True,
                   "analyzer": id(analyzer), "analyze": id(getattr(analyze, "__func__", analyze)),
                   "redact": id(getattr(redact, "__func__", redact)),
                   "threshold": getattr(privacy_gate, "_score_threshold", None)}
        memo = _PRIVACY_PROOFS.get()
        if memo is not None:
            memo.current(self, observation)
            privacy["delivery_policy"] = _privacy_snapshot()[0]
        return _digest({"card": card.to_dict(), "security": blocked, "policy": raw,
                        "pending": pending, "privacy": privacy,
                        "claims": {key: value for key, value in session.items() if not key.startswith("_")}})

    async def _persist(self, event):
        receipt = await asyncio.to_thread(self.orchestrator.audit_repo.insert, event)
        if receipt is None:
            raise EvidenceDenied()
        return receipt

    async def _recover(self, owner_id, conversation_id):
        result, cursor, scanned = [], None, 0
        while True:
            page, cursor = await asyncio.to_thread(
                self.orchestrator.audit_repo.list_for_user, owner_id,
                limit=200, cursor=cursor, event_classes=["llm_call"],
            )
            scanned += len(page)
            result.extend({**item.model_dump(), "actor_user_id": owner_id}
                          for item in page if item.conversation_id in {None, conversation_id})
            if cursor is None:
                return result
            if scanned >= 8192:
                raise EvidenceDenied()

    async def _audit(self, owner, chat, action, *, principal=None, observation=None,
                     outcome="success", reason=None, extra=None, retained=None):
        if retained is not None and retained[0] is not None:
            return await self._persist(retained[0])
        now = self.clock()
        meta = dict(extra or {})
        if observation is not None:
            meta.update(reference_digest=_digest(observation.reference),
                        integrity_identity=observation.integrity_identity,
                        grant_fingerprint=observation.grant_fingerprint,
                        source_agent=observation.source_agent,
                        source_tool=observation.source_tool,
                        operation_id=observation.operation_id,
                        source_outcome=observation.outcome)
        if reason:
            meta["reason"] = reason
        event = AuditEventCreate(event_id=str(uuid4()) if retained is not None else None,
            actor_user_id=owner, auth_principal=principal or owner,
            event_class="agent_tool_call", action_type=f"evidence.{action}",
            description=f"Evidence {action}", conversation_id=chat,
            correlation_id=str(uuid4()), outcome=outcome,
            inputs_meta=meta, started_at=now, completed_at=now,
        )
        if retained is not None:
            retained[0] = event
        return await self._persist(event)

    async def _owner(self, websocket, owner, chat):
        from orchestrator import auth
        from orchestrator.context_authority import current_context_authority

        authority = current_context_authority(orchestrator=self.orchestrator, websocket=websocket, chat_id=chat)
        proof = await authority.verify(orchestrator=self.orchestrator, websocket=websocket, chat_id=chat)
        if proof.owner_id != owner:
            raise EvidenceDenied()

        session = self.orchestrator.ui_sessions.get(websocket)
        if (not isinstance(session, dict) or session.get("sub") != owner
                or not owner or not chat or self.orchestrator.cancelled_sessions.get(id(websocket))):
            raise EvidenceDenied()
        raw = session.get("_raw_token")
        if not raw:
            raise EvidenceDenied()
        claims = await auth.verify_user(await auth.verify_production_token(raw))
        if claims.get("sub") != owner:
            raise EvidenceDenied()
        record = await asyncio.to_thread(self.orchestrator.history.get_conversation_record, chat, owner)
        if record is None:
            self.archive.revoke(owner_id=owner, conversation_id=chat)
            self.views.revoke(owner_id=owner, conversation_id=chat)
            raise EvidenceDenied()
        await authority.verify(orchestrator=self.orchestrator, websocket=websocket, chat_id=chat)
        authority.assert_current(orchestrator=self.orchestrator, websocket=websocket, chat_id=chat)
        return record, session

    def _grant(self, owner, chat, agent, tool):
        path = os.getenv("ASTRAL_OBSERVATION_POLICY", "")
        if not path:
            raise EvidencePolicyError()
        return match_grant(load_grants(path), owner_id=owner, conversation_id=chat,
                           audience_id=f"user:{owner}", source_agent=agent,
                           source_tool=tool, now=self.clock())

    async def _permitted_text(self, text):
        from personalization.phi_gate import get_phi_gate

        return await self._redact_text(text, get_phi_gate().redact_for_storage)

    async def _redact_text(self, text, redact):
        if type(text) is not str or len(text.encode("utf-8")) > _PRIVACY_BYTES or _SECRET.search(text):
            raise EvidenceCaptureError("evidence_privacy_refused")
        result = await asyncio.to_thread(redact, text)
        if (type(result) is not tuple or len(result) != 2 or type(result[0]) is not str
                or type(result[1]) is not bool or _SECRET.search(result[0])):
            raise EvidenceCaptureError("evidence_privacy_refused")
        return result[0]

    async def _verify_retained_text(self, text, observation):
        try:
            memo = _PRIVACY_PROOFS.get()
            if memo is None:
                if await self._permitted_text(text) != text:
                    raise EvidenceDenied()
                return
            memo.current(self, observation)
            grant = self._grant(observation.owner_id, observation.conversation_id,
                                observation.source_agent, observation.source_tool)
            if self.archive.inspect(observation.reference, owner_id=observation.owner_id,
                    conversation_id=observation.conversation_id, audience_id=observation.audience_id) != observation:
                raise EvidenceDenied()
            self.archive.read(observation.reference, grant=grant, owner_id=observation.owner_id,
                              conversation_id=observation.conversation_id, audience_id=observation.audience_id)
            content = text.encode("utf-8")
            if len(content) != observation.size_bytes or hashlib.sha256(content).hexdigest() != observation.digest:
                raise EvidenceDenied()
            stamp, redact = _privacy_snapshot()
            key = (observation.owner_id, observation.conversation_id, observation.audience_id,
                   observation.reference, observation.digest, observation.size_bytes, observation.integrity_identity,
                   observation.grant_fingerprint, grant.fingerprint, observation.expires_at.isoformat(), stamp)
            if key in memo.proofs:
                return
            if len(memo.proofs) >= 32 or await self._redact_text(text, redact) != text:
                raise EvidenceDenied()
            memo.current(self, observation)
            if _privacy_snapshot()[0] != stamp:
                raise EvidenceDenied()
            if self.archive.inspect(observation.reference, owner_id=observation.owner_id,
                    conversation_id=observation.conversation_id, audience_id=observation.audience_id) != observation:
                raise EvidenceDenied()
            self.archive.read(observation.reference, grant=self._grant(observation.owner_id,
                observation.conversation_id, observation.source_agent, observation.source_tool),
                owner_id=observation.owner_id, conversation_id=observation.conversation_id,
                audience_id=observation.audience_id)
            memo.proofs.add(key)
        except Exception:
            removed = self.archive.revoke(owner_id=observation.owner_id, conversation_id=observation.conversation_id,
                                          grant_fingerprint=observation.grant_fingerprint)
            for item in removed:
                self._sources.pop(item.reference, None)
                self.views.revoke(owner_id=observation.owner_id, conversation_id=observation.conversation_id,
                                  source_reference=item.reference)
            raise EvidenceDenied() from None

    async def _verify_observation_text(self, observation, grant):
        parts, offset = [], 0
        while True:
            page = self.archive.read(observation.reference, offset=offset, grant=grant,
                                     owner_id=observation.owner_id, conversation_id=observation.conversation_id,
                                     audience_id=observation.audience_id)
            parts.append(page.text)
            if page.at_end:
                break
            offset = page.next_offset
        await self._verify_retained_text("".join(parts), observation)

    async def _probe_source(self, websocket, owner, chat, agent, tool, args, parent, initiator):
        from orchestrator.tool_permissions import turn_permission_memo

        with turn_permission_memo():
            gate = await self.orchestrator._authorize_and_prepare(
                websocket, agent, tool, copy.deepcopy(args), chat, owner,
                stream_params={}, parent_token=parent, initiating_agent_id=initiator,
                auto_subscribe_stream=False,
            )
        if hasattr(gate, "response"):
            raise EvidenceDenied()
        try:
            if _public_arguments(gate.args) != args:
                raise EvidenceDenied()
        finally:
            cap_id = gate.cap_job_id
            if cap_id:
                try:
                    await self.orchestrator.concurrency_cap.release(owner, agent, cap_id)
                finally:
                    self.orchestrator._pending_cap_entries.pop(cap_id, None)
                    self.orchestrator._job_context.pop(cap_id, None)
                    await self.orchestrator._release_hop_cap_slot(cap_id)

    async def _source_authorized(self, websocket, owner, chat, observation, *, parent=None, initiator=None):
        from orchestrator import policy

        await self._owner(websocket, owner, chat)
        grant = self._grant(owner, chat, observation.source_agent, observation.source_tool)
        if grant.fingerprint != observation.grant_fingerprint:
            self.archive.revoke(owner_id=owner, conversation_id=chat,
                                grant_fingerprint=observation.grant_fingerprint)
            self.views.revoke(owner_id=owner, conversation_id=chat, source_reference=observation.reference)
            raise EvidenceDenied()
        args = observation.source_args
        if policy.policy_enabled():
            raw = os.getenv("POLICY_RULES") or "[]"
            try:
                rules = json.loads(raw)
                if type(rules) is not list or any(type(rule) is not dict or rule.get("effect") not in {
                        policy.ALLOW, policy.DENY, policy.CONFIRM, policy.REWRITE, policy.REQUIRE_TOKEN} for rule in rules):
                    raise ValueError
                decision = policy.evaluate_policy(rules, {
                    "tool": observation.source_tool, "agent": observation.source_agent,
                    "user_id": owner, "roles": self.orchestrator._policy_roles(websocket), "args": args,
                })
                if decision.effect != policy.ALLOW or (decision.args is not None and decision.args != args):
                    raise EvidenceDenied()
            except (TypeError, ValueError):
                raise EvidenceDenied() from None
        await self._probe_source(websocket, owner, chat, observation.source_agent, observation.source_tool,
                                 args, parent, initiator)
        stamp = self._source_stamp(websocket, owner, chat, observation, parent)
        await self._verify_observation_text(observation, grant)
        await self._owner(websocket, owner, chat)
        if self._source_stamp(websocket, owner, chat, observation, parent) != stamp:
            raise EvidenceDenied()
        current = self._grant(owner, chat, observation.source_agent, observation.source_tool)
        if current.fingerprint != grant.fingerprint:
            raise EvidenceDenied()
        return grant

    def _view_adapter(self, expected=None, *, tool="inspect_context_view"):
        from agents.evidence.evidence_agent import EvidenceAgent
        from agents.evidence.mcp_server import MCPServer

        local = getattr(self.orchestrator, "local_agents", None)
        cards = getattr(self.orchestrator, "agent_cards", None)
        adapter = local.get("evidence-1") if type(local) is dict else None
        server = getattr(adapter, "mcp_server", None)
        card = getattr(adapter, "card", None)
        tools = getattr(server, "tools", None)
        inspect = tools.get(tool) if type(tools) is dict else None
        if (tool not in {"inspect_context_view", "recall_observation"}
                or not flags.is_enabled("inprocess_agents") or getattr(self.orchestrator, "_evidence_context", None) is not self
                or type(adapter) is not EvidenceAgent or type(server) is not MCPServer
                or server._orchestrator is not self.orchestrator or type(card) is not AgentCard
                or type(cards) is not dict or cards.get("evidence-1") is not card or card.agent_id != "evidence-1"
                or type(inspect) is not dict or inspect.get("scope") != "tools:read"
                or type(card.skills) is not list or not any(
                    type(skill) is AgentSkill and skill.id == tool and skill.scope == "tools:read"
                    for skill in card.skills)):
            raise EvidenceDenied()
        try:
            identity = _digest({"card": card.to_dict(), "tools": tools})
        except (AttributeError, TypeError, ValueError, UnicodeError):
            raise EvidenceDenied() from None
        if expected is not None and (type(expected) is not tuple or len(expected) != 4
                or adapter is not expected[0] or server is not expected[1] or card is not expected[2]
                or identity != expected[3]):
            raise EvidenceDenied()
        return adapter, server, card, identity

    def _packing_adapter(self, expected=None):
        from agents.evidence.evidence_agent import EvidenceAgent
        from agents.evidence.mcp_server import MCPServer

        local = getattr(self.orchestrator, "local_agents", None)
        cards = getattr(self.orchestrator, "agent_cards", None)
        adapter = local.get("evidence-1") if type(local) is dict else None
        server = getattr(adapter, "mcp_server", None)
        card = getattr(adapter, "card", None)
        tools = getattr(server, "tools", None)
        recall = tools.get("recall_observation") if type(tools) is dict else None
        if (not flags.is_enabled("observation_packing") or not flags.is_enabled("inprocess_agents")
                or getattr(self.orchestrator, "_evidence_context", None) is not self
                or type(adapter) is not EvidenceAgent or type(server) is not MCPServer
                or server._orchestrator is not self.orchestrator or type(card) is not AgentCard
                or type(cards) is not dict or cards.get("evidence-1") is not card or card.agent_id != "evidence-1"
                or type(recall) is not dict or recall.get("scope") != "tools:read"
                or type(card.skills) is not list or not any(
                    getattr(skill, "id", None) == "recall_observation" and getattr(skill, "scope", None) == "tools:read"
                    for skill in card.skills)):
            raise EvidenceCaptureError("evidence_recall_unavailable")
        try:
            identity = _digest({"card": card.to_dict(), "tools": tools})
        except (AttributeError, TypeError, ValueError, UnicodeError):
            raise EvidenceCaptureError("evidence_recall_unavailable") from None
        if expected is not None and (adapter is not expected[0] or server is not expected[1] or identity != expected[2]):
            raise EvidenceCaptureError("evidence_recall_unavailable")
        return adapter, server, identity

    async def pack_result(self, result, *, websocket, owner, chat, agent, tool, args,
                          operation_id, parent=None, initiator=None):
        if (not flags.is_enabled("observation_packing") or result is None or result.error
                or agent == "evidence-1" or result.result is None):
            return result
        value = result.result
        try:
            text = value if type(value) is str else json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
            if len(text.encode("utf-8")) < _PACK_BYTES:
                return result
        except (TypeError, ValueError, UnicodeError):
            return result
        observations = []
        try:
            await self._owner(websocket, owner, chat)
            adapter_binding = self._packing_adapter()
            try:
                read_adapter = self._view_adapter(tool="recall_observation")
            except EvidenceDenied:
                raise EvidenceCaptureError("evidence_recall_unavailable") from None
            grant = self._grant(owner, chat, agent, tool)
            permitted = await self._permitted_text(text)
            source_args = _public_arguments(args)
            argument_text = json.dumps(source_args, sort_keys=True, ensure_ascii=False, allow_nan=False)
            if await self._permitted_text(argument_text) != argument_text:
                raise EvidenceCaptureError("evidence_privacy_refused")
            observation = self.archive.capture(
                permitted, grant=grant, operation_id=operation_id,
                source_args=source_args, outcome=_outcome(value),
            )
            observations.append(observation)
            await self._source_authorized(websocket, owner, chat, observation, parent=parent, initiator=initiator)
            await self._audit(owner, chat, "capture", principal=f"agent:{initiator}" if initiator else owner,
                              observation=observation)
            grant = await self._source_authorized(websocket, owner, chat, observation, parent=parent, initiator=initiator)
            page = self.archive.read(observation.reference, grant=grant, owner_id=owner,
                                     conversation_id=chat, audience_id=f"user:{owner}")
            stamp = self._source_stamp(websocket, owner, chat, observation, parent)
            await self._verify_retained_text(permitted, observation)
            from orchestrator.context_authority import current_context_authority
            current_context_authority(orchestrator=self.orchestrator, websocket=websocket, chat_id=chat)
            if self._source_stamp(websocket, owner, chat, observation, parent) != stamp:
                raise EvidenceDenied()
            grant = self._grant(owner, chat, agent, tool)
            page = self.archive.read(observation.reference, grant=grant, owner_id=owner,
                                     conversation_id=chat, audience_id=f"user:{owner}")
            self._packing_adapter(adapter_binding)
            try:
                self._view_adapter(read_adapter, tool="recall_observation")
            except EvidenceDenied:
                raise EvidenceCaptureError("evidence_recall_unavailable") from None
            preview = page.text.encode("utf-8")[:_PREVIEW_BYTES].decode("utf-8", errors="ignore")
            view = {"view": "partial_preview", "untrusted": True,
                    "reference": observation.reference, "digest": observation.digest,
                    "outcome": observation.outcome, "preview": preview,
                    "total_bytes": observation.size_bytes, "omitted": True,
                    "recall": {"tool": "recall_observation", "reference": observation.reference, "offset": 0}}
            packed = copy.copy(result)
            packed.result = view
            packed.ui_components = evidence_components(
                state="preview", text=preview, reference=observation.reference,
                digest=observation.digest, end=len(preview.encode("utf-8")),
                total=observation.size_bytes, next_offset=0, outcome=observation.outcome,
            )
            packed._evidence_transient = True
            packed._evidence_view_adapter = read_adapter
            self._sources[observation.reference] = observation
            return packed
        except BaseException as exc:
            for observation in observations:
                try:
                    self.archive.delete(observation.reference, owner_id=owner,
                                        conversation_id=chat, audience_id=f"user:{owner}")
                except (EvidenceDenied, EvidenceUnavailable):
                    pass
                self._sources.pop(observation.reference, None)
            if not isinstance(exc, Exception):
                raise
            code = exc.code if isinstance(exc, EvidenceError) else "evidence_capture_unavailable"
            failure = traceback.extract_tb(exc.__traceback__)[-1]
            logger.warning("Evidence capture refused: %s at %s:%d", code, failure.name, failure.lineno)
            try:
                await self._audit(owner, chat, "capture_refused", outcome="failure", reason=code)
            except Exception as audit_error:
                logger.warning("Evidence capture refusal receipt unavailable: %s", type(audit_error).__name__)
            if not isinstance(exc, (EvidencePolicyError, EvidenceCaptureError)):
                return self._denied(result.request_id)
            try:
                await self.orchestrator.send_ui_render(websocket, evidence_components(
                    state="missing", text="Observation shortening was refused; the authorized result remains intact.",
                ), target="chat", speak=False)
            except Exception as delivery_error:
                logger.warning("Evidence capture limit delivery unavailable: %s", type(delivery_error).__name__)
            try:
                await self._owner(websocket, owner, chat)
                source = SimpleNamespace(source_agent=agent, source_tool=tool, source_args=_public_arguments(args))
                await self._probe_source(websocket, owner, chat, agent, tool, source.source_args, parent, initiator)
                stamp = self._source_stamp(websocket, owner, chat, source, parent)
                await self._owner(websocket, owner, chat)
                if self._source_stamp(websocket, owner, chat, source, parent) != stamp:
                    raise EvidenceDenied()
            except Exception:
                return self._denied(result.request_id)
            from astralprims import Text
            result.ui_components = [*(result.ui_components or []), Text(content=(
                "Observation shortening was refused; the authorized result and history remain intact."
            )).to_dict()]
            return result

    async def tool(self, request_id, name, arguments):
        context = self.orchestrator._dispatch_context.get(request_id)
        if (not isinstance(context, dict) or context.get("agent_id") != "evidence-1"
                or context.get("requester_verified") is not True):
            return self._denied(request_id)
        owner, chat, socket = context.get("requester_owner"), context.get("requester_chat"), context.get("requester_websocket")
        observation = None
        principal = f"agent:{context['requester_agent']}" if context.get("requester_agent") else owner
        try:
            view_adapter = self._view_adapter(tool=name) if name in {"inspect_context_view", "recall_observation"} else None
            await self._owner(socket, owner, chat)
            if view_adapter is not None:
                self._view_adapter(view_adapter, tool=name)
            if name == "context_usage" and not arguments:
                totals = await self.usage.totals(owner, chat, refresh=True)
                return MCPResponse(request_id=request_id, result=totals, ui_components=usage_components(totals))
            if name == "inspect_context_view" and set(arguments) == {"view_id"}:
                data = await self._view_content(arguments["view_id"], websocket=socket, owner=owner, chat=chat,
                    parent=context.get("requester_parent"), initiator=context.get("requester_agent"), adapter_binding=view_adapter)
                self._view_adapter(view_adapter)
                await self._audit(owner, chat, "view_read", principal=principal,
                    extra={"view_digest": _digest(arguments["view_id"]), "bytes": len(data["text"].encode())})
                self._view_adapter(view_adapter)
                await self.usage.record_recall(owner, chat, len(data["text"].encode()))
                self._view_adapter(view_adapter)
                data = await self._view_content(arguments["view_id"], websocket=socket, owner=owner, chat=chat,
                    parent=context.get("requester_parent"), initiator=context.get("requester_agent"), adapter_binding=view_adapter)
                self._view_adapter(view_adapter)
                response = MCPResponse(request_id=request_id, result=data,
                    ui_components=evidence_components(state="summary", text=data["text"]))
                response._evidence_view_adapter = view_adapter
                return response
            allowed = {"reference", "offset"} if name == "recall_observation" else {"reference"}
            if name not in {"recall_observation", "delete_observation"} or not arguments.keys() <= allowed:
                raise EvidenceDenied()
            reference = arguments.get("reference")
            observation = self.archive.inspect(reference, owner_id=owner,
                                               conversation_id=chat, audience_id=f"user:{owner}")
            if name == "delete_observation":
                self.archive.delete(reference, owner_id=owner, conversation_id=chat, audience_id=f"user:{owner}")
                self._sources.pop(reference, None)
                self.views.revoke(owner_id=owner, conversation_id=chat, source_reference=reference)
                await self._audit(owner, chat, "delete", principal=principal, observation=observation)
                return MCPResponse(request_id=request_id, result={"status": "deleted"},
                                   ui_components=evidence_components(state="missing"))
            grant = await self._source_authorized(
                socket, owner, chat, observation, parent=context.get("requester_parent"),
                initiator=context.get("requester_agent"),
            )
            self._view_adapter(view_adapter, tool="recall_observation")
            page = self.archive.read(reference, offset=arguments.get("offset", 0), grant=grant,
                                     owner_id=owner, conversation_id=chat, audience_id=f"user:{owner}")
            await self._verify_observation_text(observation, grant)
            self._view_adapter(view_adapter, tool="recall_observation")
            await self._audit(owner, chat, "recall", principal=principal, observation=observation,
                              extra={"start": page.start, "end": page.end})
            self._view_adapter(view_adapter, tool="recall_observation")
            await self.usage.record_recall(owner, chat, page.end - page.start)
            self._view_adapter(view_adapter, tool="recall_observation")
            current = await self._source_authorized(
                socket, owner, chat, observation, parent=context.get("requester_parent"),
                initiator=context.get("requester_agent"),
            )
            self._view_adapter(view_adapter, tool="recall_observation")
            page = self.archive.read(reference, offset=page.start, grant=current,
                                     owner_id=owner, conversation_id=chat, audience_id=f"user:{owner}")
            stamp = self._source_stamp(socket, owner, chat, observation, context.get("requester_parent"))
            await self._verify_observation_text(observation, current)
            self._view_adapter(view_adapter, tool="recall_observation")
            from orchestrator.context_authority import current_context_authority
            current_context_authority(orchestrator=self.orchestrator, websocket=socket, chat_id=chat)
            if self._source_stamp(socket, owner, chat, observation, context.get("requester_parent")) != stamp:
                raise EvidenceDenied()
            current = self._grant(owner, chat, observation.source_agent, observation.source_tool)
            page = self.archive.read(reference, offset=page.start, grant=current,
                                     owner_id=owner, conversation_id=chat, audience_id=f"user:{owner}")
            response = MCPResponse(request_id=request_id, result={**page.to_dict(), "untrusted": True,
                                                              "outcome": observation.outcome},
                               ui_components=evidence_components(
                                   state="source", text=page.text, reference=page.reference, digest=page.digest,
                                   start=page.start, end=page.end, total=page.total,
                                   next_offset=page.next_offset, outcome=observation.outcome,
                               ))
            response._evidence_view_adapter = view_adapter
            return response
        except Exception as exc:
            reason = exc.code if isinstance(exc, EvidenceError) else "evidence_unavailable_or_not_authorized"
            refusal_recorded = False
            if owner and chat:
                try:
                    await self._audit(owner, chat, "recall_refused", principal=principal,
                                      observation=observation, outcome="failure", reason=reason)
                    refusal_recorded = True
                except Exception as audit_error:
                    logger.warning("Evidence denial receipt unavailable: %s", type(audit_error).__name__)
            if refusal_recorded and isinstance(exc, EvidenceUnavailable) and exc.reason != "revoked":
                return await self._unavailable(request_id, exc, websocket=socket, owner=owner, chat=chat,
                    reference=arguments.get("reference"), observation=observation,
                    parent=context.get("requester_parent"), initiator=context.get("requester_agent"))
            return self._denied(request_id)

    async def _unavailable(self, request_id, error, *, websocket, owner, chat, reference,
                           observation=None, parent=None, initiator=None):
        observation = self._sources.get(reference) if isinstance(reference, str) else None
        if observation is None or (observation.owner_id, observation.conversation_id,
                                   observation.audience_id) != (owner, chat, f"user:{owner}"):
            return self._denied(request_id)
        try:
            def metadata_policy():
                bindings = (owner, chat, f"user:{owner}", observation.source_agent, observation.source_tool)
                matches = [grant for grant in load_grants(os.getenv("ASTRAL_OBSERVATION_POLICY", ""))
                           if (grant.owner_id, grant.conversation_id, grant.audience_id,
                               grant.source_agent, grant.source_tool) == bindings]
                if len(matches) != 1 or matches[0].fingerprint != observation.grant_fingerprint:
                    raise EvidenceDenied()
                try:
                    self.archive.inspect(reference, owner_id=owner, conversation_id=chat, audience_id=f"user:{owner}")
                except EvidenceUnavailable as current:
                    if current.reason == error.reason and current.reason != "revoked":
                        return
                raise EvidenceDenied()

            metadata_policy()
            await self._owner(websocket, owner, chat)
            await self._probe_source(websocket, owner, chat, observation.source_agent, observation.source_tool,
                                     observation.source_args, parent, initiator)
            stamp = self._source_stamp(websocket, owner, chat, observation, parent)
            await self._owner(websocket, owner, chat)
            if self._source_stamp(websocket, owner, chat, observation, parent) != stamp:
                raise EvidenceDenied()
            metadata_policy()
            response = MCPResponse(request_id=request_id, result={"status": "unavailable", "reason": error.reason},
                                   ui_components=evidence_components(state="missing"))
            response._evidence_unavailable_reference = observation.reference
            response._evidence_delivery_scope = (owner, chat, websocket, copy.deepcopy(parent), initiator)
            self._seal_delivery(response, websocket=websocket, owner=owner, chat=chat, parent=parent)
            return response
        except Exception:
            return self._denied(request_id)

    @staticmethod
    def _denied(request_id):
        return MCPResponse(request_id=request_id, error={
            "code": "evidence_unavailable_or_not_authorized", "retryable": False,
            "message": "Evidence is unavailable or not authorized.",
        })

    async def verify_delivery(self, result, *, websocket, owner, chat, parent=None, initiator=None):
        if result is None or result.error:
            return result
        observation = None
        try:
            read_tool = _literal_read_tool(result)
            view_adapter = (self._view_adapter(getattr(result, "_evidence_view_adapter", None), tool=read_tool)
                            if read_tool is not None else None)
            scope = getattr(result, "_evidence_delivery_scope", None)
            if scope is not None:
                if (type(scope) is not tuple or len(scope) != 5 or scope[:2] != (owner, chat)
                        or scope[2] is not websocket):
                    raise EvidenceDenied()
                parent, initiator = scope[3:]
            await self._owner(websocket, owner, chat)
            if view_adapter is not None:
                self._view_adapter(view_adapter, tool=read_tool)
            data = result.result
            if type(data) is not dict:
                raise EvidenceDenied()
            reference = data.get("reference")
            if data.get("view") == "generated_summary":
                expected = await self._view_content(data.get("view_id"), websocket=websocket, owner=owner, chat=chat,
                                                    parent=parent, initiator=initiator, adapter_binding=view_adapter)
                self._view_adapter(view_adapter)
                if _json(data) != _json(expected):
                    raise EvidenceDenied()
                components = evidence_components(state="summary", text=expected["text"])
            elif reference is not None:
                observation = self.archive.inspect(reference, owner_id=owner,
                    conversation_id=chat, audience_id=f"user:{owner}")
                grant = await self._source_authorized(websocket, owner, chat, observation,
                                                       parent=parent, initiator=initiator)
                self._view_adapter(view_adapter, tool=read_tool)
                preview = data.get("view") == "partial_preview"
                offset = 0 if preview else data.get("start")
                page = self.archive.read(reference, offset=offset, grant=grant, owner_id=owner,
                                         conversation_id=chat, audience_id=f"user:{owner}")
                stamp = self._source_stamp(websocket, owner, chat, observation, parent)
                await self._verify_observation_text(observation, grant)
                self._view_adapter(view_adapter, tool=read_tool)
                from orchestrator.context_authority import current_context_authority
                current_context_authority(orchestrator=self.orchestrator, websocket=websocket, chat_id=chat)
                if self._source_stamp(websocket, owner, chat, observation, parent) != stamp:
                    raise EvidenceDenied()
                grant = self._grant(owner, chat, observation.source_agent, observation.source_tool)
                page = self.archive.read(reference, offset=offset, grant=grant, owner_id=owner,
                                         conversation_id=chat, audience_id=f"user:{owner}")
                if preview:
                    text = page.text.encode("utf-8")[:_PREVIEW_BYTES].decode("utf-8", errors="ignore")
                    expected = {"view": "partial_preview", "untrusted": True,
                        "reference": reference, "digest": observation.digest, "outcome": observation.outcome,
                        "preview": text, "total_bytes": observation.size_bytes, "omitted": True,
                        "recall": {"tool": "recall_observation", "reference": reference, "offset": 0}}
                    components = evidence_components(state="preview", text=text, reference=reference,
                        digest=observation.digest, end=len(text.encode()), total=observation.size_bytes,
                        next_offset=0, outcome=observation.outcome)
                else:
                    expected = {**page.to_dict(), "untrusted": True, "outcome": observation.outcome}
                    components = evidence_components(state="source", text=page.text, reference=reference,
                        digest=page.digest, start=page.start, end=page.end, total=page.total,
                        next_offset=page.next_offset, outcome=observation.outcome)
                if _json(data) != _json(expected):
                    raise EvidenceDenied()
            elif data == {"status": "deleted"}:
                expected = copy.deepcopy(data)
                components = evidence_components(state="missing")
            elif (set(data) == {"status", "reason"} and data["status"] == "unavailable"
                  and data["reason"] in {"expired", "deleted", "integrity_failed", "clock_unavailable"}):
                reference = getattr(result, "_evidence_unavailable_reference", None)
                observation = self._sources.get(reference) if isinstance(reference, str) else None
                if observation is None:
                    raise EvidenceDenied()
                checked = await self._unavailable(result.request_id, EvidenceUnavailable(data["reason"]),
                    websocket=websocket, owner=owner, chat=chat, reference=observation.reference,
                    observation=observation, parent=parent, initiator=initiator)
                if checked.error:
                    raise EvidenceDenied()
                expected, components = checked.result, checked.ui_components
            else:
                totals = await self.usage.totals(owner, chat, refresh=True)
                if _json(data) != _json(totals):
                    raise EvidenceDenied()
                await self._owner(websocket, owner, chat)
                expected = totals
                components = usage_components(totals)
            result = copy.copy(result)
            result.result = copy.deepcopy(expected)
            result.ui_components = components
            result._evidence_delivery_scope = (owner, chat, websocket, copy.deepcopy(parent), initiator)
            if view_adapter is not None:
                self._view_adapter(view_adapter, tool=read_tool)
                result._evidence_view_adapter = view_adapter
            self._seal_delivery(result, websocket=websocket, owner=owner, chat=chat, parent=parent)
            return result
        except Exception as exc:
            try:
                await self._audit(owner, chat, "delivery_refused", principal=f"agent:{initiator}" if initiator else owner,
                    observation=observation, outcome="failure",
                    reason=exc.code if isinstance(exc, EvidenceError) else "evidence_unavailable_or_not_authorized")
            except Exception as audit_error:
                logger.warning("Evidence delivery refusal receipt unavailable: %s", type(audit_error).__name__)
                return self._denied(result.request_id)
            if isinstance(exc, EvidenceUnavailable) and exc.reason != "revoked":
                return await self._unavailable(result.request_id, exc, websocket=websocket, owner=owner, chat=chat,
                    reference=result.result.get("reference") if isinstance(result.result, dict) else None,
                    observation=observation, parent=parent, initiator=initiator)
            return self._denied(result.request_id)

    @staticmethod
    def _dependency(observation):
        return SourceDependency(observation.reference, observation.integrity_identity,
                                observation.grant_fingerprint, observation.expires_at)

    def _view_sources_current(self, dependencies, *, websocket, owner, chat, parent=None, stamps=None):
        current = {}
        for dependency in dependencies:
            observation = self.archive.inspect(dependency.reference, owner_id=owner,
                conversation_id=chat, audience_id=f"user:{owner}")
            if self._dependency(observation) != dependency:
                raise EvidenceDenied()
            grant = self._grant(owner, chat, observation.source_agent, observation.source_tool)
            self.archive.read(observation.reference, grant=grant, owner_id=owner,
                              conversation_id=chat, audience_id=f"user:{owner}")
            current[dependency.reference] = self._source_stamp(websocket, owner, chat, observation, parent)
        if stamps is not None and current != stamps:
            raise EvidenceDenied()
        return current

    async def summary_view(self, text, messages, *, websocket, owner, chat):
        captured = None
        try:
            adapter_binding = self._view_adapter()
            await self._owner(websocket, owner, chat)
            self._view_adapter(adapter_binding)
            references = sorted(set(_REFERENCE_TOKENS.findall(_json(messages))))
            if not references or any(_REFERENCE.fullmatch(reference) is None for reference in references):
                raise EvidenceDenied()
            dependencies, stamps = [], {}
            for reference in references:
                observation = self.archive.inspect(reference, owner_id=owner,
                    conversation_id=chat, audience_id=f"user:{owner}")
                await self._source_authorized(websocket, owner, chat, observation)
                self._view_adapter(adapter_binding)
                dependencies.append(self._dependency(observation))
                stamps[reference] = self._source_stamp(websocket, owner, chat, observation, None)
            permitted = await self._permitted_text(text)
            self._view_adapter(adapter_binding)
            if not permitted.strip():
                raise EvidenceDenied()
            await self._owner(websocket, owner, chat)
            self._view_adapter(adapter_binding)
            self._view_sources_current(tuple(dependencies), websocket=websocket, owner=owner, chat=chat, stamps=stamps)
            if len(self._retained_views) >= self.views.max_records:
                raise EvidenceCaptureError("context_view_cleanup_limit")
            captured = self.views.capture(permitted, owner_id=owner, conversation_id=chat,
                                          audience_id=f"user:{owner}", dependencies=tuple(dependencies))
            self._retained_views[captured.reference] = (copy.deepcopy(captured), None, None, [None])
            self._view_adapter(adapter_binding)
            await self._audit(owner, chat, "view_capture", extra={
                "view_digest": _digest(captured.reference), "bytes": captured.size_bytes,
                "source_count": len(dependencies),
            })
            self._view_adapter(adapter_binding)
            await self._owner(websocket, owner, chat)
            self._view_adapter(adapter_binding)
            self._view_sources_current(tuple(dependencies), websocket=websocket, owner=owner, chat=chat, stamps=stamps)
            self.views.inspect(captured.reference, owner_id=owner, conversation_id=chat,
                               audience_id=f"user:{owner}", dependencies=tuple(dependencies))
            return captured.reference
        except BaseException as exc:
            if captured is not None:
                try:
                    self.views.delete(captured.reference, owner_id=owner, conversation_id=chat, audience_id=f"user:{owner}")
                except Exception as cleanup_error:
                    logger.warning("Temporary context view cleanup refused: %s", type(cleanup_error).__name__)
            if not isinstance(exc, Exception):
                raise
            logger.warning("Temporary context view refused: %s", type(exc).__name__)
            return None

    async def _view_content(self, view_id, *, websocket, owner, chat, parent=None, initiator=None, adapter_binding=None):
        adapter_binding = self._view_adapter(adapter_binding)
        await self._owner(websocket, owner, chat)
        self._view_adapter(adapter_binding)
        view = self.views.inspect(view_id, owner_id=owner, conversation_id=chat, audience_id=f"user:{owner}")
        if not view.dependencies:
            raise EvidenceDenied()
        stamps = {}
        try:
            for dependency in view.dependencies:
                observation = self.archive.inspect(dependency.reference, owner_id=owner,
                    conversation_id=chat, audience_id=f"user:{owner}")
                if self._dependency(observation) != dependency:
                    raise EvidenceDenied()
                await self._source_authorized(websocket, owner, chat, observation, parent=parent, initiator=initiator)
                self._view_adapter(adapter_binding)
                stamps[dependency.reference] = self._source_stamp(websocket, owner, chat, observation, parent)
            content = self.views.read(view_id, owner_id=owner, conversation_id=chat,
                                     audience_id=f"user:{owner}", dependencies=view.dependencies)
            if await self._permitted_text(content.text) != content.text:
                raise EvidenceDenied()
            self._view_adapter(adapter_binding)
            await self._owner(websocket, owner, chat)
            self._view_adapter(adapter_binding)
            self._view_sources_current(view.dependencies, websocket=websocket, owner=owner, chat=chat,
                                       parent=parent, stamps=stamps)
            content = self.views.read(view_id, owner_id=owner, conversation_id=chat,
                                     audience_id=f"user:{owner}", dependencies=view.dependencies)
            return {"view": "generated_summary", "view_id": view_id, "text": content.text,
                    "source_refs": [dependency.reference for dependency in view.dependencies],
                    "expires_at": content.expires_at.isoformat(), "untrusted": True}
        except Exception:
            self.views.delete(view_id, owner_id=owner, conversation_id=chat, audience_id=f"user:{owner}")
            raise

    def _delivery_state(self, result, *, websocket, owner, chat, parent, adapter_binding=None):
        data = result.result
        read_tool = _literal_read_tool(result)
        adapter_stamp = None
        if read_tool is not None:
            adapter_binding = self._view_adapter(adapter_binding, tool=read_tool)
            adapter_stamp = (tuple(id(value) for value in adapter_binding[:3]), adapter_binding[3])
        if data.get("view") == "generated_summary":
            view = self.views.inspect(data.get("view_id"), owner_id=owner,
                                      conversation_id=chat, audience_id=f"user:{owner}")
            content = self.views.read(view.reference, owner_id=owner, conversation_id=chat,
                                     audience_id=f"user:{owner}", dependencies=view.dependencies)
            if content.text != data.get("text"):
                raise EvidenceDenied()
            return {"view": view.integrity_identity,
                    "adapter": adapter_stamp,
                    "sources": self._view_sources_current(view.dependencies, websocket=websocket,
                                                           owner=owner, chat=chat, parent=parent)}
        reference = data.get("reference") or getattr(result, "_evidence_unavailable_reference", None)
        if reference is None:
            return {}
        observation = self._sources.get(reference)
        if observation is None or (observation.owner_id, observation.conversation_id,
                                   observation.audience_id) != (owner, chat, f"user:{owner}"):
            raise EvidenceDenied()
        if data.get("status") == "unavailable":
            matching = [grant for grant in load_grants(os.getenv("ASTRAL_OBSERVATION_POLICY", ""))
                        if grant.fingerprint == observation.grant_fingerprint]
            if len(matching) != 1:
                raise EvidenceDenied()
            try:
                self.archive.inspect(reference, owner_id=owner, conversation_id=chat, audience_id=f"user:{owner}")
            except EvidenceUnavailable as unavailable:
                if unavailable.reason != data.get("reason") or unavailable.reason == "revoked":
                    raise EvidenceDenied() from None
            else:
                raise EvidenceDenied()
            return {"state": data, "source": self._source_stamp(websocket, owner, chat, observation, parent)}
        return {"adapter": adapter_stamp, "sources": self._view_sources_current(
            (self._dependency(observation),), websocket=websocket, owner=owner, chat=chat, parent=parent)}

    def _seal_delivery(self, result, *, websocket, owner, chat, parent):
        from orchestrator.context_authority import current_context_authority

        authority = current_context_authority(orchestrator=self.orchestrator, websocket=websocket, chat_id=chat)
        authority.assert_current(orchestrator=self.orchestrator, websocket=websocket, chat_id=chat)
        if authority.owner_id != owner:
            raise EvidenceDenied()
        read_tool = _literal_read_tool(result)
        adapter_binding = (self._view_adapter(getattr(result, "_evidence_view_adapter", None), tool=read_tool)
                           if read_tool is not None else None)
        result._evidence_delivery_proof = _EvidenceDelivery(
            _DELIVERY_TOKEN, authority, owner, chat, websocket, _digest(result.result), _digest(result.ui_components),
            _digest(self._delivery_state(result, websocket=websocket, owner=owner, chat=chat,
                                        parent=parent, adapter_binding=adapter_binding)), adapter_binding,
        )

    def assert_delivery_current(self, result, *, websocket, owner, chat):
        from orchestrator.context_authority import current_context_authority

        proof = getattr(result, "_evidence_delivery_proof", None)
        scope = getattr(result, "_evidence_delivery_scope", None)
        authority = current_context_authority(orchestrator=self.orchestrator, websocket=websocket, chat_id=chat)
        if (type(proof) is not _EvidenceDelivery or proof.token is not _DELIVERY_TOKEN
                or proof.authority is not authority or (proof.owner, proof.chat) != (owner, chat)
                or proof.websocket is not websocket or type(scope) is not tuple or len(scope) != 5
                or scope[:2] != (owner, chat) or scope[2] is not websocket or result.error
                or proof.result_digest != _digest(result.result) or proof.component_digest != _digest(result.ui_components)):
            raise EvidenceDenied()
        authority.assert_current(orchestrator=self.orchestrator, websocket=websocket, chat_id=chat)
        if _literal_read_tool(result) is not None and type(proof.adapter_binding) is not tuple:
            raise EvidenceDenied()
        if proof.state_digest != _digest(self._delivery_state(result, websocket=websocket, owner=owner, chat=chat,
                                                             parent=scope[3], adapter_binding=proof.adapter_binding)):
            raise EvidenceDenied()

    async def budget(self, owner, capture=None):
        capture = capture or await self.orchestrator._llm_store.capture_user(owner)
        if capture is None or capture.owner_id != owner:
            raise EvidenceDenied()
        record = capture._record
        return load_budget(os.getenv("ASTRAL_CONTEXT_BUDGET_POLICY", ""), owner_id=owner,
                           provider=record.provider, base_url=record.base_url, model=record.model)

    async def prepare_model(self, *, websocket, owner, chat, model, base_url, request, provider_capture=None):
        from orchestrator.context_authority import current_context_authority
        from persistent_agents.dispatch_context import current_dispatch

        origin = asyncio.current_task()
        reservation = current_dispatch()
        authority = None
        if reservation is None:
            authority = current_context_authority(orchestrator=self.orchestrator, websocket=websocket, chat_id=chat)
        elif (reservation.kind != "model" or reservation.owner_id != owner
              or reservation.conversation_id != chat or not reservation.consumed):
            raise EvidenceDenied()
        adapter_binding = (self._view_adapter(tool="recall_observation")
                           if type(request) is dict and has_references(request.get("messages", [])) else None)
        capture = await self.orchestrator._llm_store.capture_user(owner)
        budget = await self.budget(owner, capture)
        if (type(request) is not dict or request.get("model") != model or capture is None or provider_capture is None
                or provider_capture.owner_id != owner or not provider_capture.matches(capture._record)
                or model != capture._record.model or base_url != capture._record.base_url):
            raise EvidenceDenied()
        output = budget.max_output_tokens
        for parameter in ("max_tokens", "max_completion_tokens"):
            if parameter in request:
                value = request.pop(parameter)
                if type(value) is not int or value < 1:
                    raise EvidenceDenied()
                output = min(output, value)
        request[budget.output_parameter] = output
        if (estimate_context_tokens(request.get("messages", [])) + output
                + len(_json({key: value for key, value in request.items() if key != "messages"}).encode())
                > budget.context_tokens):
            raise EvidenceDenied()
        request_identity = _digest(request)

        async def guard():
            if adapter_binding is not None:
                self._view_adapter(adapter_binding, tool="recall_observation")
            current = await self.orchestrator._llm_store.capture_user(owner)
            ack = await self.orchestrator._data_sharing_store.state(owner)
            if (current is None or not capture.matches(current._record) or not ack.acknowledged
                    or await self.budget(owner, current) != budget or _digest(request) != request_identity):
                raise EvidenceDenied()
            source_stamps = []
            for reference in set(_REFERENCE_TOKENS.findall(_json(request.get("messages", [])))):
                if not _REFERENCE.fullmatch(reference):
                    raise EvidenceDenied()
                observation = self.archive.inspect(reference, owner_id=owner, conversation_id=chat, audience_id=f"user:{owner}")
                grant = await self._source_authorized(websocket, owner, chat, observation)
                await self._verify_observation_text(observation, grant)
                source_stamps.append((observation, self._source_stamp(websocket, owner, chat, observation, None)))
            self._check_sources(request.get("messages", []), owner, chat)
            if reservation is not None:
                if origin.done() or origin.cancelling() or current_dispatch() is not reservation:
                    raise EvidenceDenied()
                await reservation.authorize()
                if origin.done() or origin.cancelling():
                    raise EvidenceDenied()
                if reservation.research_input is not None:
                    reservation.research_input.assert_body(owner, request)
            else:
                proof = await authority.verify(orchestrator=self.orchestrator, websocket=websocket, chat_id=chat)
                if proof.owner_id != owner:
                    raise EvidenceDenied()
            current = await self.orchestrator._llm_store.capture_user(owner)
            ack = await self.orchestrator._data_sharing_store.state(owner)
            if (current is None or not capture.matches(current._record) or not ack.acknowledged
                    or not enabled() and not has_references(request.get("messages", []))):
                raise EvidenceDenied()
            if reservation is not None:
                if origin.done() or origin.cancelling():
                    raise EvidenceDenied()
            else:
                authority.assert_current(orchestrator=self.orchestrator, websocket=websocket, chat_id=chat)
            if _digest(request) != request_identity or load_budget(
                    os.getenv("ASTRAL_CONTEXT_BUDGET_POLICY", ""), owner_id=owner,
                    provider=capture._record.provider, base_url=base_url, model=model) != budget:
                raise EvidenceDenied()
            for observation, stamp in source_stamps:
                if self._source_stamp(websocket, owner, chat, observation, None) != stamp:
                    raise EvidenceDenied()
                grant = self._grant(owner, chat, observation.source_agent, observation.source_tool)
                self.archive.read(observation.reference, grant=grant, owner_id=owner,
                                  conversation_id=chat, audience_id=f"user:{owner}")
            self._check_sources(request.get("messages", []), owner, chat)
            if adapter_binding is not None:
                self._view_adapter(adapter_binding, tool="recall_observation")

        return guard

    async def _protected(self, websocket, owner, chat, capture):
        from orchestrator.context_authority import current_context_authority

        authority = current_context_authority(orchestrator=self.orchestrator, websocket=websocket, chat_id=chat)
        if authority.owner_id != owner:
            raise EvidenceDenied()
        async with asyncio.timeout(15):
            record, session = await self._owner(websocket, owner, chat)
            current = await self.orchestrator._llm_store.capture_user(owner)
            if current is None or not capture.matches(current._record):
                raise EvidenceDenied()
            ack = await self.orchestrator._data_sharing_store.state(owner)
            if not ack.acknowledged:
                raise EvidenceDenied()
            tasks = await self.orchestrator.async_task_manager.list_for_user(owner, limit=257)
            if len(tasks) > 256:
                raise EvidenceDenied()
            task_projections = []
            for task in tasks:
                if task.chat_id == chat:
                    if task._operation is None:
                        raise EvidenceDenied()
                    task_projections.append({"operation": task._operation, "fence": task._execution_fence,
                                             "title": task.title, "kind": task.kind, "outputs": task.outputs,
                                             "errors": task.errors})
            foreground_tasks = self.orchestrator.task_manager.get_chat_tasks(chat)
            if len(foreground_tasks) > 256:
                raise EvidenceDenied()
            for task in foreground_tasks:
                current_task = await self.orchestrator.task_manager.refresh_task(task.task_id)
                if current_task is None or current_task._operation is None:
                    raise EvidenceDenied()
                task_projections.append({"operation": current_task._operation, "fence": current_task._execution_fence,
                                         "message": current_task.message, "tools": current_task.tool_calls_made,
                                         "current_tool": current_task.current_tool})
            pending = {key: asdict(call) for key, call in getattr(self.orchestrator, "_hitl_pending_calls", {}).items()
                       if call.owner == owner and call.chat == chat}
            jobs = {key: value for key, value in self.orchestrator._job_context.items()
                    if value.get("user_id") == owner and value.get("chat_id") == chat}
            from orchestrator.tool_permissions import turn_permission_memo
            with turn_permission_memo():
                permissions = await asyncio.to_thread(lambda: {
                    agent: {skill.id: self.orchestrator.tool_permissions.is_tool_allowed(owner, agent, skill.id)
                            for skill in card.skills}
                    for agent, card in self.orchestrator.agent_cards.items()
                })
            current_budget = await self.budget(owner, capture)
            proof = await authority.verify(orchestrator=self.orchestrator, websocket=websocket, chat_id=chat)
            authority.assert_current(orchestrator=self.orchestrator, websocket=websocket, chat_id=chat)
            from orchestrator.conversation_publication import current_conversation_publication
            stage = current_conversation_publication()
            publication = None if stage is None else {
                name: getattr(stage, name) for name in (
                    "commit_id", "chat_id", "user_id", "base_render_revision", "next_render_revision",
                    "execution_base_render_revision", "operation_fence", "publication_role", "summary_text",
                    "summary_source", "sealed", "committed", "dirty", "layouts",
                )
            }
            return {"authorized": True, "authority": _digest({
                "conversation": record, "pending": pending, "jobs": jobs, "tasks": task_projections,
                "publication": publication, "manifests": {key: card.to_dict() for key, card in self.orchestrator.agent_cards.items()},
                "permissions": permissions, "security": self.orchestrator.security_flags,
                "policy": os.getenv("POLICY_RULES"), "retention": self._source_states(owner, chat),
                "budget": current_budget, "operations": proof.stable_identity,
                "session": {key: value for key, value in session.items() if not key.startswith("_")},
                "provider": capture._record, "consent": ack,
            })}

    async def prepare(self, messages, *, websocket, owner, chat, overhead_tokens=0):
        try:
            before = estimate_context_tokens(messages)
            source_identity = _digest([item if isinstance(item, dict) else item.model_dump(mode="json") for item in messages])
        except (TypeError, ValueError, UnicodeError):
            return CompactionResult(messages=messages, status="context_limit", reason="unsupported_history",
                                    before_tokens=0, after_tokens=0)
        try:
            capture = await self.orchestrator._llm_store.capture_user(owner)
            budget = await self.budget(owner, capture)
            context_tokens = budget.context_tokens
            overhead_tokens += budget.max_output_tokens
        except Exception:
            return CompactionResult(messages=messages, status="context_limit", reason="context_budget_unconfigured",
                                    before_tokens=before, after_tokens=before)
        if before + overhead_tokens <= context_tokens:
            return CompactionResult(messages=messages, status="unchanged", reason="within_budget",
                                    before_tokens=before, after_tokens=before)
        if not flags.is_enabled("safe_compaction"):
            return CompactionResult(messages=messages, status="context_limit", reason="protected_context_limit",
                                    before_tokens=before, after_tokens=before)
        from persistent_agents.dispatch_context import current_dispatch

        if current_dispatch() is not None:
            return CompactionResult(messages=messages, status="context_limit", reason="auxiliary_reservation_unavailable",
                                    before_tokens=before, after_tokens=before)
        try:
            if capture is None or capture.owner_id != owner:
                raise EvidenceDenied()
            protected = await self._protected(websocket, owner, chat, capture)
            await self._audit(owner, chat, "compaction_attempt", extra={"source_identity": _digest(messages)})
            record = capture._record
            from llm_config.user_store import PersistedLLMConfig
            config = PersistedLLMConfig(record.provider, record.base_url, record.model,
                                        self.orchestrator._llm_store.open_captured_user_key(capture))
            client, source, resolved = self.orchestrator._build_llm_client(config, self.orchestrator._CredentialSource.USER)
            if (source != self.orchestrator._CredentialSource.USER
                    or resolved.model != record.model or resolved.base_url != record.base_url):
                raise EvidenceDenied()
            from llm_config.evidence_transport import guard_model_client
            client = guard_model_client(client, base_url=record.base_url)

            async def propose(prompt):
                await self._protected(websocket, owner, chat, capture)
                if estimate_context_tokens(prompt) + budget.max_output_tokens > context_tokens:
                    raise EvidenceDenied()

                async def physical():
                    request = {"model": record.model, "messages": prompt,
                               budget.output_parameter: budget.max_output_tokens}
                    guard = await self.prepare_model(websocket=websocket, owner=owner, chat=chat,
                                                     model=record.model, base_url=record.base_url, request=request,
                                                     provider_capture=capture)
                    await guard()
                    if not flags.is_enabled("safe_compaction"):
                        raise EvidenceDenied()
                    return await asyncio.to_thread(client.chat.completions.create, **request)

                response = await self.usage.model_call(owner, chat, "safe_compaction", record.model, physical)
                choices = getattr(response, "choices", None)
                if not choices or getattr(choices[0], "message", None) is None:
                    raise EvidenceDenied()
                return choices[0].message.content

            async def references(text):
                source_text = json.dumps([item if isinstance(item, dict) else item.model_dump(mode="json")
                                          for item in messages], ensure_ascii=False)
                for reference in set(_REFERENCE_TOKENS.findall(text + source_text)):
                    if not _REFERENCE.fullmatch(reference):
                        raise EvidenceDenied()
                    observation = self.archive.inspect(reference, owner_id=owner,
                                                       conversation_id=chat, audience_id=f"user:{owner}")
                    grant = await self._source_authorized(websocket, owner, chat, observation)
                    self.archive.read(reference, grant=grant, owner_id=owner,
                                      conversation_id=chat, audience_id=f"user:{owner}")
                return True

            await references("")
            result = await compact_context(messages, llm_call=propose, context_tokens=context_tokens,
                overhead_tokens=overhead_tokens, read_protected_state=lambda: self._protected(websocket, owner, chat, capture),
                validate_references=references)
            await self._audit(owner, chat, "compaction_proposal_validated" if result.status == "accepted"
                              else f"compaction_{result.status}",
                              outcome="success" if result.status == "accepted" else "failure",
                              reason=result.reason, extra={"before_tokens": result.before_tokens,
                                                          "after_tokens": result.after_tokens})
            if result.status == "accepted":
                await references(json.dumps(result.messages, ensure_ascii=False, default=lambda item: item.model_dump(mode="json")))
                if await self._protected(websocket, owner, chat, capture) != protected:
                    raise EvidenceDenied()
                if _digest([item if isinstance(item, dict) else item.model_dump(mode="json") for item in messages]) != source_identity:
                    raise EvidenceDenied()
                self._check_sources(result.messages, owner, chat)
            return result
        except Exception:
            try:
                await self._audit(owner, chat, "compaction_refused", outcome="failure", reason="authority_unavailable")
            except Exception as audit_error:
                logger.warning("Context refusal receipt unavailable: %s", type(audit_error).__name__)
            return CompactionResult(messages=messages, status="context_limit", reason="authority_unavailable",
                                    before_tokens=before, after_tokens=before)

    def start(self):
        if self._sweep_task is None:
            self._sweep_task = asyncio.create_task(self._sweep(), name="evidence-retention-sweep")

    def _queue_view_cleanup(self, removed, action, reason):
        for view in removed:
            retained = self._retained_views.get(view.reference)
            if retained is not None and retained[1] is None:
                self._retained_views[view.reference] = (retained[0], action, reason, retained[3])

    def _reconcile_view_cleanup(self):
        for reference, retained in tuple(self._retained_views.items()):
            view, action, _reason, audit = retained
            if action is not None:
                continue
            try:
                self.views.inspect(reference, owner_id=view.owner_id,
                    conversation_id=view.conversation_id, audience_id=view.audience_id)
            except ViewUnavailable as exc:
                action = "view_expiry_cleanup" if exc.reason == "expired" else "view_revocation_cleanup"
                reason = "expiry" if exc.reason == "expired" else exc.reason
                self._retained_views[reference] = (view, action, reason, audit)
            except ViewDenied:
                self._retained_views[reference] = (view, "view_revocation_cleanup", "unavailable", audit)

    async def _drain_view_cleanup(self):
        self._reconcile_view_cleanup()
        for reference, retained in tuple(self._retained_views.items()):
            view, action, reason, audit = retained
            if action is None:
                continue
            await self._audit(view.owner_id, view.conversation_id, action, reason=reason, retained=audit, extra={
                "view_digest": _digest(view.reference), "size_bytes": view.size_bytes,
                "integrity_identity": view.integrity_identity, "source_count": len(view.dependencies),
            })
            if self._retained_views.get(reference) is retained:
                self._retained_views.pop(reference)

    async def sweep(self):
        try:
            removed_views = self.views.cleanup()
        except ViewUnavailable as exc:
            if exc.reason != "clock_unavailable":
                raise
            removed_views = ()
        self._queue_view_cleanup(removed_views, "view_expiry_cleanup", "expiry")
        expired = self.archive.cleanup()
        for observation in expired:
            self._sources.pop(observation.reference, None)
            self._queue_view_cleanup(self.views.revoke(
                owner_id=observation.owner_id, conversation_id=observation.conversation_id,
                source_reference=observation.reference), "view_revocation_cleanup", "source_expired")
        receipts = [("expiry_cleanup", observation) for observation in expired]
        for reference, observation in tuple(self._sources.items()):
            try:
                self.archive.inspect(reference, owner_id=observation.owner_id,
                                     conversation_id=observation.conversation_id, audience_id=observation.audience_id)
                grant = self._grant(observation.owner_id, observation.conversation_id,
                                    observation.source_agent, observation.source_tool)
                record = await asyncio.to_thread(self.orchestrator.history.get_conversation_record,
                                                 observation.conversation_id, observation.owner_id)
                if grant.fingerprint != observation.grant_fingerprint or record is None:
                    raise EvidenceDenied()
                from orchestrator.tool_permissions import turn_permission_memo
                with turn_permission_memo():
                    allowed = await asyncio.to_thread(self.orchestrator.tool_permissions.is_tool_allowed,
                        observation.owner_id, observation.source_agent, observation.source_tool)
                if (not allowed or self.orchestrator.security_flags.get(observation.source_agent, {}).get(
                        observation.source_tool, {}).get("blocked")):
                    raise EvidenceDenied()
            except Exception:
                self._sources.pop(reference, None)
                removed = self.archive.revoke(owner_id=observation.owner_id,
                    conversation_id=observation.conversation_id, grant_fingerprint=observation.grant_fingerprint)
                for item in removed:
                    self._sources.pop(item.reference, None)
                    self._queue_view_cleanup(self.views.revoke(owner_id=item.owner_id,
                        conversation_id=item.conversation_id, source_reference=item.reference),
                        "view_revocation_cleanup", "source_authority_revoked")
                receipts.extend(("revocation_cleanup", item) for item in removed)
        for action, observation in receipts:
            await self._audit(observation.owner_id, observation.conversation_id, action, observation=observation)
        await self._drain_view_cleanup()

    async def _sweep(self):
        while True:
            try:
                await self.sweep()
            except Exception as exc:
                logger.warning("Evidence cleanup audit unavailable: %s", type(exc).__name__)
            await asyncio.sleep(60)

    async def close(self):
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            await asyncio.gather(self._sweep_task, return_exceptions=True)
            self._sweep_task = None
        removed = tuple(self._sources.values())
        for observation in removed:
            try:
                self.archive.delete(observation.reference, owner_id=observation.owner_id,
                                    conversation_id=observation.conversation_id, audience_id=observation.audience_id)
            except (EvidenceDenied, EvidenceUnavailable):
                pass
        self._sources.clear()
        try:
            self._reconcile_view_cleanup()
        finally:
            self.views.clear()
            for reference, retained in tuple(self._retained_views.items()):
                if retained[1] is None:
                    self._retained_views[reference] = (retained[0], "view_shutdown_cleanup", "shutdown", retained[3])
        await self.usage.drain()
        for observation in removed:
            await self._audit(observation.owner_id, observation.conversation_id, "shutdown_cleanup", observation=observation)
        await self._drain_view_cleanup()
