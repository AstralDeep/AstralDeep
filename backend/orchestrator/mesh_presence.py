"""Resolves enrolled mesh members to their current advertised fabric endpoints and
presence. mesh_api.py exposes possession-proof publication and owner topology reads;
the shared mesh surface renders this same resolver output so every client shows
identical, honest presence.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from orchestrator import mesh_admission as ma
from orchestrator import mesh_enrollment as me
from orchestrator.mesh_enrollment import MeshEnrollmentError

PRESENCE_NAMESPACE = "astral-mesh-presence"
RECORD_VERSION = 1
PRESENCE_ONLINE = "online"
PRESENCE_OFFLINE = "offline"
PRESENCE_UNAVAILABLE = "unavailable"

REASON_UNPUBLISHED = "unpublished"
REASON_IDENTITY_REPLACED = "identity_replaced"
REASON_MEMBERSHIP_CHANGED = "membership_changed"
REASON_STALE_OBSERVATION = "stale_observation"
REASON_AMBIGUOUS_ENDPOINT = "ambiguous_endpoint"
REASON_MALFORMED = "malformed"

DEFAULT_TTL_SECONDS = 300
MIN_TTL_SECONDS = 30
MAX_TTL_SECONDS = 3600
MAX_CAPABILITIES = 8
MAX_ENDPOINT_CHARS = 255
_CAPABILITY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")


class PresenceRefused(MeshEnrollmentError):
    status = 403
    code = "mesh_presence_refused"


class PresenceConflict(MeshEnrollmentError):
    status = 409
    code = "mesh_presence_conflict"


class PresenceStoreUnavailable(MeshEnrollmentError):
    status = 503
    code = "mesh_presence_unavailable"


def ttl_seconds() -> int:
    raw = os.getenv("MESH_PRESENCE_TTL_SECONDS", "")
    try:
        value = int(raw) if raw else DEFAULT_TTL_SECONDS
    except ValueError:
        return DEFAULT_TTL_SECONDS
    return max(MIN_TTL_SECONDS, min(MAX_TTL_SECONDS, value))


def normalize_endpoint(raw) -> dict[str, int | str]:
    try:
        if not isinstance(raw, str) or not raw or len(raw) > MAX_ENDPOINT_CHARS:
            raise ValueError
        parsed = urlsplit(raw)
        if (
            parsed.scheme not in {"wss", "https", "ws", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or parsed.scheme in {"ws", "http"}
            and os.getenv("ASTRAL_ENV") != "development"
        ):
            raise ValueError
        port = parsed.port
    except (ValueError, TypeError):
        raise PresenceRefused("endpoint must be a bare fabric service origin") from None
    return {
        "scheme": parsed.scheme,
        "hostname": parsed.hostname,
        "port": port
        if port is not None
        else (443 if parsed.scheme in {"wss", "https"} else 80),
    }


def endpoint_origin(endpoint: dict) -> str:
    scheme, hostname, port = endpoint["scheme"], endpoint["hostname"], endpoint["port"]
    default = 443 if scheme in {"wss", "https"} else 80
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{scheme}://{host}{'' if port == default else f':{port}'}"


def normalize_capabilities(raw) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_CAPABILITIES:
        raise PresenceRefused("capabilities must be a short list of labels")
    labels: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not _CAPABILITY.fullmatch(item):
            raise PresenceRefused("capability labels are bounded lowercase identifiers")
        if item not in labels:
            labels.append(item)
    return sorted(labels)


def _endpoint_of(record) -> dict | None:
    endpoint = record.get("endpoint") if isinstance(record, dict) else None
    if (
        type(endpoint) is not dict
        or type(endpoint.get("scheme")) is not str
        or not endpoint.get("scheme")
        or type(endpoint.get("hostname")) is not str
        or not endpoint["hostname"]
        or type(endpoint.get("port")) is not int
        or not 0 <= endpoint["port"] <= 65535
    ):
        return None
    return endpoint


class MeshPresence:
    def __init__(self, source=None, *, store: me.MeshEnrollmentStore | None = None) -> None:
        if store is None:
            store = me.MeshEnrollmentStore(source)
        self.store = store

    def _fresh_reason(self, record: dict, member: dict, *, now: int) -> str | None:
        if record.get("device_key_fingerprint") != member.get("device_key_fingerprint"):
            return REASON_IDENTITY_REPLACED
        if (
            record.get("membership_epoch") != member.get("membership_epoch")
            or record.get("authority_revision") != member.get("authority_revision")
        ):
            return REASON_MEMBERSHIP_CHANGED
        observed_at = record.get("observed_at")
        if type(observed_at) is not int or now > observed_at + ttl_seconds() * 1000:
            return REASON_STALE_OBSERVATION
        if _endpoint_of(record) is None:
            return REASON_MALFORMED
        return None

    def publish(
        self,
        *,
        admission: ma.MeshAdmission,
        claims: dict,
        endpoint,
        capabilities,
    ) -> dict:
        owner_id = claims["sub"]
        member_id = claims[ma.MEMBER_CLAIM]["member_id"]
        observed_endpoint = normalize_endpoint(endpoint)
        labels = normalize_capabilities(capabilities)
        now = me._now_ms()
        with self.store._transaction() as transaction:
            _, member, record = admission.current(transaction, claims)
            existing = self.store._get_in_transaction(
                transaction, owner_id, PRESENCE_NAMESPACE, member_id
            )
            payload = {
                "updated_at": now,
                "record_version": RECORD_VERSION,
                "member_id": member_id,
                "mesh_id": record["mesh_id"],
                "device_key_fingerprint": record["device_key_fingerprint"],
                "membership_epoch": member.membership_epoch,
                "authority_revision": record["authority_revision"],
                "endpoint": observed_endpoint,
                "capabilities": labels,
                "observed_at": now,
            }
            try:
                if existing is None:
                    self.store._credentials.repository.upsert_credential(
                        transaction,
                        owner_id=owner_id,
                        agent_id=PRESENCE_NAMESPACE,
                        credential_key=member_id,
                        encrypted_value=me._encode_record(payload),
                        updated_at=payload["updated_at"],
                    )
                else:
                    self.store._transition(
                        owner_id,
                        PRESENCE_NAMESPACE,
                        member_id,
                        expected_updated_at=existing["updated_at"],
                        record=payload,
                        transaction=transaction,
                    )
            except me.InvitationStateInvalid as exc:
                raise PresenceConflict("presence changed concurrently") from exc
            self.store._audit_transition(
                transaction,
                owner_id,
                "mesh.member.presence",
                actor_kind="member",
                actor_id=member_id,
                meta={
                    "member_id": member_id,
                    "endpoint": endpoint_origin(observed_endpoint),
                    "capabilities": labels,
                },
            )
        targets = self.resolve(owner_id)
        for target in targets:
            if target["member_id"] == member_id:
                return target
        raise me.MemberNotFound(member_id)

    def resolve(self, owner_id: str, *, member_id: str | None = None) -> list[dict]:
        now = me._now_ms()
        try:
            members = [
                member
                for member in self.store._list(owner_id, me.MEMBER_NAMESPACE)
                if member.get("status") == me.MEMBER_ACTIVE_STATUS
            ]
            records = {
                record["member_id"]: record
                for record in self.store._list(owner_id, PRESENCE_NAMESPACE)
                if isinstance(record.get("member_id"), str)
            }
        except MeshEnrollmentError as exc:
            raise PresenceStoreUnavailable(
                "mesh presence inventory is unavailable"
            ) from exc
        members.sort(key=lambda item: item.get("enrolled_at", 0), reverse=True)
        fresh: dict[str, dict] = {}
        reasons: dict[str, str] = {}
        observed: dict[str, int | None] = {}
        for member in members:
            key = member["member_id"]
            record = records.get(key)
            if record is None:
                reasons[key] = REASON_UNPUBLISHED
                observed[key] = None
                continue
            seen = record.get("observed_at")
            observed[key] = seen if type(seen) is int else None
            reason = self._fresh_reason(record, member, now=now)
            if reason is None:
                fresh[key] = record
            else:
                reasons[key] = reason
        holders: dict[str, list[str]] = {}
        for key, record in fresh.items():
            holders.setdefault(endpoint_origin(_endpoint_of(record)), []).append(key)
        ambiguous = {
            key
            for origin in holders
            if len(holders[origin]) > 1
            for key in holders[origin]
        }
        targets = []
        for member in members:
            key = member["member_id"]
            if member_id is not None and key != member_id:
                continue
            record = fresh.get(key)
            disclosed = None if key in ambiguous else record
            if key in ambiguous:
                presence, reason = PRESENCE_UNAVAILABLE, REASON_AMBIGUOUS_ENDPOINT
            elif record is not None:
                presence, reason = PRESENCE_ONLINE, None
            else:
                presence, reason = PRESENCE_OFFLINE, reasons.get(
                    key, REASON_UNPUBLISHED
                )
            targets.append(
                {
                    "member_id": key,
                    "label": member.get("label"),
                    "presence": presence,
                    "reason": reason,
                    "last_seen": observed.get(key),
                    "endpoint": endpoint_origin(_endpoint_of(disclosed))
                    if disclosed
                    else None,
                    "capabilities": list(disclosed.get("capabilities") or [])
                    if disclosed
                    else None,
                }
            )
        return targets

    def self_presence(self, *, admission: ma.MeshAdmission, claims: dict) -> dict:
        with self.store._transaction() as transaction:
            admission.current(transaction, claims)
        targets = self.resolve(
            claims["sub"], member_id=claims[ma.MEMBER_CLAIM]["member_id"]
        )
        if not targets:
            raise me.MemberNotFound(claims[ma.MEMBER_CLAIM]["member_id"])
        return targets[0]
