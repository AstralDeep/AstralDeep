"""Binds member possession to current Plane membership and server-custodied Keycloak authority.
mesh_api.py uses nonce-bound proofs and RFC 8693 exchange; orchestrator.py rechecks the
resulting attenuated identity at delegation and physical tool admission.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
import secrets
import time
import threading
from urllib.parse import urlsplit
import uuid

from astralplane.repositories.history import (
    SessionConsentObservation,
    SessionExecutionObservation,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jose import jwt

from orchestrator import auth, delegation, mesh_enrollment as me, web_auth
from orchestrator.plane_repository_context import plane_source_from_orchestrator
from orchestrator.session_store import WebSessionStore
from orchestrator.tool_permissions import VALID_SCOPES
from orchestrator.work_submit_authority import _signed_selection
from shared.auth_clients import allowed_azps

MEMBER_AUDIENCE = "astral-mesh"
MEMBER_ISSUER = "astral-internal-delegation"
MEMBER_CLAIM = "astral_mesh"
MAX_TOKEN_SECONDS = 300
NONCE_SECONDS = 60
SESSION_NAMESPACE = "astral-mesh-session"
_NONCE_HITS = {}
_NONCE_LOCK = threading.Lock()


class MeshAdmissionError(me.MeshEnrollmentError):
    status = 403
    code = "mesh_admission_refused"


class MeshRateLimited(me.MeshEnrollmentError):
    status = 429
    code = "mesh_nonce_rate_limited"


class MeshIAMUnavailable(me.MeshEnrollmentError):
    status = 503
    code = "mesh_iam_unavailable"


def _refuse():
    raise MeshAdmissionError("current member possession and IAM authority required")


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def thumbprint(key):
    return me._b64url_encode(
        hashlib.sha256(_canonical(me.normalize_device_key(key))).digest()
    )


def target_uri(path):
    base = (
        os.getenv("PUBLIC_BASE_URL") or os.getenv("BACKEND_PUBLIC_URL") or ""
    ).rstrip("/")
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or (parsed.scheme != "https" and os.getenv("ASTRAL_ENV") != "development")
        or not isinstance(path, str)
        or not path.startswith("/api/mesh/")
        or len(path) > 1024
        or any(character in path for character in "?#\\")
    ):
        _refuse()
    return base + path


def verify_member_token(token):
    try:
        claims = jwt.decode(
            token,
            delegation._child_signing_key(),
            algorithms=["HS256"],
            issuer=MEMBER_ISSUER,
            audience=MEMBER_AUDIENCE,
            options={"require_exp": True, "require_iat": True},
        )
        binding = claims.get(MEMBER_CLAIM)
        if (
            type(binding) is not dict
            or set(binding)
            != {
                "mesh_id",
                "member_id",
                "membership_epoch",
                "revocation_epoch",
                "member_version",
                "authority_revision",
                "agent_id",
                "session_incarnation",
            }
            or type(claims.get("sub")) is not str
            or not claims["sub"]
            or claims.get("delegation") is not True
            or claims.get("act")
            != {"sub": "mesh-member:" + binding.get("member_id", "")}
            or type(claims.get("scope")) is not str
            or not set(claims["scope"].split()).issubset(me.GRANTABLE_SCOPES)
            or type(claims.get("exp")) is not int
            or type(claims.get("iat")) is not int
            or type(claims.get("jti")) is not str
            or not claims["jti"]
            or claims["exp"] <= claims["iat"]
            or claims["exp"] - claims["iat"] > MAX_TOKEN_SECONDS
            or claims["iat"] > int(time.time())
            or type(claims.get("cnf")) is not dict
            or set(claims["cnf"]) != {"jkt"}
            or not isinstance(claims["cnf"]["jkt"], str)
            or any(
                type(binding[name]) is not int or binding[name] < 0
                for name in (
                    "membership_epoch",
                    "revocation_epoch",
                    "member_version",
                    "authority_revision",
                )
            )
            or any(
                type(binding[name]) is not str or not binding[name]
                for name in ("mesh_id", "member_id", "session_incarnation")
            )
            or (
                binding["agent_id"] is not None
                and (type(binding["agent_id"]) is not str or not binding["agent_id"])
            )
        ):
            _refuse()
        return claims
    except MeshAdmissionError:
        raise
    except Exception:
        _refuse()


def verify_possession(proof, key, *, method, target, nonce, access_token=None):
    try:
        if not isinstance(proof, str) or not 1 <= len(proof) <= 8192:
            _refuse()
        header_segment, payload_segment, signature_segment = proof.split(".")
        header = json.loads(me._b64url_decode(header_segment))
        payload = json.loads(me._b64url_decode(payload_segment))
        required = {"jti", "htm", "htu", "iat", "nonce"}
        if access_token is not None:
            required.add("ath")
        if (
            type(header) is not dict
            or set(header) != {"alg", "typ", "jwk"}
            or header["alg"] != "EdDSA"
            or header["typ"] != "dpop+jwt"
            or header["jwk"] != me.normalize_device_key(key)
            or type(payload) is not dict
            or set(payload) != required
            or payload["htm"] != method
            or payload["htu"] != target
            or payload["nonce"] != nonce
            or type(payload["iat"]) is not int
            or not int(time.time()) - NONCE_SECONDS
            <= payload["iat"]
            <= int(time.time())
            or type(payload["jti"]) is not str
            or not 1 <= len(payload["jti"]) <= 128
        ):
            _refuse()
        if access_token is not None and payload["ath"] != me._b64url_encode(
            hashlib.sha256(access_token.encode()).digest()
        ):
            _refuse()
        Ed25519PublicKey.from_public_bytes(me._b64url_decode(key["x"])).verify(
            me._b64url_decode(signature_segment),
            (header_segment + "." + payload_segment).encode(),
        )
        return payload
    except MeshAdmissionError:
        raise
    except Exception:
        _refuse()


@dataclass(frozen=True, slots=True)
class PreparedMemberIAM:
    owner_id: str
    custody: dict = field(repr=False)
    observation: SessionExecutionObservation = field(repr=False)
    subject_token: str = field(repr=False)
    scopes: tuple[str, ...]
    agent_id: str | None
    expires_at: int
    member_fence: tuple[int, int, int, str] | None = None


class MeshAdmission:
    def __init__(self, orchestrator, *, store=None):
        self.orchestrator = orchestrator
        self.source = plane_source_from_orchestrator(orchestrator)
        self.runtime = self.source.plane_runtime
        self.store = store or me.MeshEnrollmentStore(
            self.source, audit_repository=orchestrator.audit_repo
        )
        self.sessions = orchestrator.web_sessions
        if (
            type(self.sessions) is not WebSessionStore
            or self.sessions._sessions.plane_runtime is not self.runtime
        ):
            _refuse()

    async def owner_custody(self, request, claims):
        try:
            sid = _signed_selection(request)
            if sid is None:
                _refuse()
            reference = await asyncio.to_thread(
                self.sessions.capture_execution_reference,
                owner_id=claims["sub"],
                session_id=sid,
            )
            credential = reference.state.credential
            if (
                credential.issuing_issuer != claims.get("iss")
                or credential.issuing_client_id != claims.get("azp")
                or claims["iss"] != os.getenv("KEYCLOAK_AUTHORITY", "").rstrip("/")
            ):
                _refuse()
            expires = claims.get("exp")
            if type(expires) not in (int, float) or not math.isfinite(expires):
                _refuse()
            until = min(
                reference.state.observed_at + timedelta(seconds=15),
                datetime.fromtimestamp(
                    min(expires, credential.hard_expires_at), timezone.utc
                ),
            )
            custody = {
                "incarnation_id": credential.incarnation_id,
                "issuer": credential.issuing_issuer,
                "client_id": credential.issuing_client_id,
            }
            return custody, SessionConsentObservation(
                credential, reference.state.observed_at, until
            )
        except Exception:
            _refuse()

    def _session(self, transaction, owner_id, custody, *, expires_at):
        try:
            if (
                type(custody) is not dict
                or set(custody) != {"incarnation_id", "issuer", "client_id"}
                or custody["issuer"] != os.getenv("KEYCLOAK_AUTHORITY", "").rstrip("/")
                or custody["client_id"] not in allowed_azps()
            ):
                _refuse()
            repository = self.runtime.repositories.history.sessions
            session = repository.get_by_incarnation(
                transaction, owner_id=owner_id, incarnation_id=custody["incarnation_id"]
            )
            if (
                session is None
                or session.issuing_issuer != custody["issuer"]
                or session.issuing_client_id != custody["client_id"]
            ):
                _refuse()
            state = repository.get_execution_state(
                transaction, owner_id=owner_id, session_id=session.session_id
            )
            if state is None:
                _refuse()
            until = min(
                state.observed_at + timedelta(seconds=15),
                datetime.fromtimestamp(
                    min(expires_at, session.hard_expires_at), timezone.utc
                ),
            )
            observation = SessionExecutionObservation(
                state.credential, state.observed_at, until
            )
            repository.assert_current_execution(transaction, observation=observation)
            return observation
        except MeshAdmissionError:
            raise
        except Exception:
            _refuse()

    def current(self, transaction, claims, *, agent_id=None, required_scope=None):
        binding = claims.get(MEMBER_CLAIM)
        if type(binding) is not dict or claims.get("aud") != MEMBER_AUDIENCE:
            _refuse()
        mesh, member, record = self.store._current_member(
            transaction,
            claims["sub"],
            binding["member_id"],
            membership_epoch=binding["membership_epoch"],
            revocation_epoch=binding["revocation_epoch"],
            member_version=binding["member_version"],
        )
        if (
            binding["mesh_id"] != mesh.mesh_id
            or binding["authority_revision"] != record.get("authority_revision")
            or claims.get("cnf") != {"jkt": thumbprint(record["device_key"])}
            or (record.get("custody") or {}).get("incarnation_id")
            != binding["session_incarnation"]
            or not set(claims["scope"].split()).issubset(record["scopes"])
            or (agent_id is not None and binding["agent_id"] != agent_id)
            or (
                required_scope is not None
                and required_scope not in claims["scope"].split()
            )
            or claims["exp"] <= int(time.time())
        ):
            _refuse()
        self._session(
            transaction, claims["sub"], record.get("custody"), expires_at=claims["exp"]
        )
        session = self.store._get_in_transaction(
            transaction, claims["sub"], SESSION_NAMESPACE, member.member_id
        )
        selected = {
            key: value for key, value in claims.items() if not key.startswith("_")
        }
        if (
            session is None
            or session.get("claims_sha256")
            != hashlib.sha256(_canonical(selected)).hexdigest()
        ):
            _refuse()
        return mesh, member, record

    def nonce(self, owner_id, member_id):
        _member_ids(owner_id, member_id)
        with self.store._transaction() as transaction:
            _, _, record = self.store._current_member(transaction, owner_id, member_id)
            self._session(
                transaction,
                owner_id,
                record.get("custody"),
                expires_at=int(time.time()) + NONCE_SECONDS,
            )
            _nonce_budget(owner_id, member_id)
            challenge_id = uuid.uuid4().hex
            nonce = challenge_id + "." + secrets.token_urlsafe(32)
            now = me._now_ms()
            self.store._membership.issue_enrollment_challenge(
                transaction,
                owner_id=owner_id,
                mesh_id=record["mesh_id"],
                member_id=member_id,
                challenge_id=challenge_id,
                challenge_digest=hashlib.sha256(nonce.encode()).hexdigest(),
                issued_at=now,
                expires_at=now + NONCE_SECONDS * 1000,
            )
            self.store._audit_transition(
                transaction,
                owner_id,
                "mesh.member.nonce",
                actor_kind="anonymous",
                meta={"member_id": member_id},
            )
            return {"nonce": nonce, "expires_in": NONCE_SECONDS}

    def consume_proof(
        self,
        transaction,
        owner_id,
        member_id,
        proof,
        nonce,
        *,
        method,
        path,
        access_token=None,
        claims=None,
    ):
        _member_ids(owner_id, member_id)
        _, _, record = (
            self.current(transaction, claims)
            if claims is not None
            else self.store._current_member(transaction, owner_id, member_id)
        )
        verify_possession(
            proof,
            record["device_key"],
            method=method,
            target=target_uri(path),
            nonce=nonce,
            access_token=access_token,
        )
        if (
            not isinstance(nonce, str)
            or not 1 <= len(nonce) <= 256
            or nonce.count(".") != 1
        ):
            _refuse()
        try:
            proven = self.store._membership.prove_enrollment_challenge(
                transaction,
                owner_id=owner_id,
                challenge_id=nonce.split(".")[0],
                challenge_digest=hashlib.sha256(nonce.encode()).hexdigest(),
                as_of=me._now_ms(),
            )
            if proven.member_id != member_id or proven.mesh_id != record["mesh_id"]:
                _refuse()
        except Exception:
            _refuse()
        return record

    async def prepare_iam(self, owner_id, record, agent_id):
        try:
            if agent_id is not None and (
                type(agent_id) is not str or not 1 <= len(agent_id) <= 256
            ):
                _refuse()
            member_fence = None
            if record.get("member_id") is not None:

                def capture():
                    with self.store._transaction() as transaction:
                        mesh, member, current = self.store._current_member(
                            transaction, owner_id, record["member_id"]
                        )
                        if me._authority_digest(current) != me._authority_digest(
                            record
                        ):
                            _refuse()
                        return (
                            mesh.membership_epoch,
                            mesh.revocation_epoch,
                            member.record_version,
                            me._authority_digest(current),
                        )

                member_fence = await asyncio.to_thread(capture)
            custody = record.get("custody") or (record.get("confirmed_by") or {}).get(
                "custody"
            )
            reference = await asyncio.to_thread(
                self.sessions.capture_incarnation_execution_reference,
                owner_id=owner_id,
                incarnation_id=custody["incarnation_id"],
            )
            candidate = await self.sessions.refresh_for_execution(
                reference,
                exchange=web_auth._exchange_session_refresh,
                bound_exchange=web_auth._exchange_bound_session_refresh,
            )
            claims = await auth.verify_user(
                await auth.verify_production_token(candidate.access_token)
            )
            credential = candidate.credential
            if (
                claims.get("sub") != owner_id
                or claims.get("iss") != custody["issuer"]
                or claims.get("azp") != custody["client_id"]
                or credential.incarnation_id != custody["incarnation_id"]
                or credential.issuing_issuer != custody["issuer"]
                or credential.issuing_client_id != custody["client_id"]
            ):
                _refuse()
            requested = set(record.get("scopes", record.get("confirmed_scopes")) or [])
            effective = set()
            allowed_tools = []
            if agent_id is not None:
                card = self.orchestrator.agent_cards.get(agent_id)
                if card is None:
                    _refuse()
                permissions = self.orchestrator.tool_permissions
                current = await asyncio.to_thread(
                    permissions.get_enabled_scope_names, owner_id, agent_id
                )
                effective = requested & set(current) & set(VALID_SCOPES)
                for skill in card.skills:
                    if permissions.get_tool_scope(
                        agent_id, skill.id
                    ) in effective and permissions.is_tool_allowed(
                        owner_id, agent_id, skill.id
                    ):
                        allowed_tools.append(skill.id)
            exchange = await self.orchestrator.delegation.exchange_token_for_agent(
                candidate.access_token,
                agent_id or "mesh-control",
                allowed_tools,
                owner_id,
                (sorted(effective) or ["openid"])
                + ["tool:" + tool for tool in allowed_tools],
            )
            if "error" in exchange or not isinstance(exchange.get("access_token"), str):
                _refuse()
            authority = os.getenv("KEYCLOAK_AUTHORITY", "").rstrip("/")
            from shared.jwks_cache import get_jwks
            from shared.auth_clients import agent_service_client_id

            keys = await get_jwks(
                authority + "/protocol/openid-connect/certs",
                token=exchange["access_token"],
            )
            exchanged = jwt.decode(
                exchange["access_token"],
                keys,
                algorithms=["RS256"],
                issuer=authority,
                audience=agent_service_client_id(),
                options={"require_exp": True},
            )
            exchanged_tools = {
                scope[5:]
                for scope in exchanged.get("scope", "").split()
                if scope.startswith("tool:")
            }
            if (
                exchanged.get("sub") != owner_id
                or exchanged.get("aud")
                not in (agent_service_client_id(), [agent_service_client_id()])
                or exchanged.get("azp") != os.getenv("KEYCLOAK_CLIENT_ID", "").strip()
                or type(exchanged.get("scope")) is not str
                or not effective.issubset(set(exchanged["scope"].split()))
                or (set(exchanged["scope"].split()) & set(VALID_SCOPES)) - effective
                or exchanged_tools != set(allowed_tools)
            ):
                _refuse()
            from shared.a2a_security import token_roles

            roles = set(token_roles(exchanged))
            allowed_roles = (
                {"user", "offline_access", "uma_authorization"}
                | effective
                | {"tool:" + tool for tool in allowed_tools}
            )
            if "user" not in roles or roles - allowed_roles:
                _refuse()
            expires = min(
                int(claims["exp"]),
                int(exchanged["exp"]),
                credential.hard_expires_at,
                int(time.time()) + MAX_TOKEN_SECONDS,
            )
            observation = SessionExecutionObservation(
                credential,
                candidate.started_at,
                min(
                    candidate.started_at + timedelta(seconds=15),
                    datetime.fromtimestamp(expires, timezone.utc),
                ),
            )
            await asyncio.to_thread(
                self.sessions.assert_execution_observation, observation
            )
            scopes = tuple(sorted(effective | (requested & {me.CONFIRM_SCOPE})))
            return PreparedMemberIAM(
                owner_id,
                dict(custody),
                observation,
                exchange["access_token"],
                scopes,
                agent_id,
                expires,
                member_fence,
            )
        except MeshAdmissionError:
            raise
        except Exception:
            _refuse()

    def mint(self, transaction, prepared, member):
        fence = prepared.member_fence
        mesh, neutral, current = self.store._current_member(
            transaction,
            prepared.owner_id,
            member["member_id"],
            membership_epoch=fence[0] if fence else None,
            revocation_epoch=fence[1] if fence else None,
            member_version=fence[2] if fence else None,
        )
        self.runtime.repositories.history.sessions.assert_current_execution(
            transaction, observation=prepared.observation
        )
        if (
            current.get("custody") != prepared.custody
            or (fence is not None and me._authority_digest(current) != fence[3])
            or not set(prepared.scopes).issubset(current["scopes"])
            or prepared.expires_at <= int(time.time())
        ):
            _refuse()
        key = me.normalize_device_key(current["device_key"])
        if (
            self.store._active_key_holder(
                transaction,
                prepared.owner_id,
                me.device_key_fingerprint(key),
                exclude_member_id=neutral.member_id,
            )
            is not None
        ):
            _refuse()
        payload = {
            "sub": prepared.owner_id,
            "act": {"sub": "mesh-member:" + neutral.member_id},
            "scope": " ".join(prepared.scopes),
            "iss": MEMBER_ISSUER,
            "aud": MEMBER_AUDIENCE,
            "iat": int(time.time()),
            "exp": prepared.expires_at,
            "delegation": True,
            "jti": uuid.uuid4().hex,
            "cnf": {"jkt": thumbprint(current["device_key"])},
            MEMBER_CLAIM: {
                "mesh_id": mesh.mesh_id,
                "member_id": neutral.member_id,
                "membership_epoch": mesh.membership_epoch,
                "revocation_epoch": mesh.revocation_epoch,
                "member_version": neutral.record_version,
                "authority_revision": current["authority_revision"],
                "agent_id": prepared.agent_id,
                "session_incarnation": prepared.custody["incarnation_id"],
            },
        }
        token = delegation.encode_delegation_payload(payload)
        now = me._now_ms()
        self.store._credentials.repository.upsert_credential(
            transaction,
            owner_id=prepared.owner_id,
            agent_id=SESSION_NAMESPACE,
            credential_key=neutral.member_id,
            encrypted_value=me._encode_record(
                {
                    "updated_at": now,
                    "claims_sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
                    "delegation_token": prepared.subject_token,
                }
            ),
            updated_at=now,
        )
        self.store._audit_transition(
            transaction,
            prepared.owner_id,
            "mesh.member.session",
            actor_kind="member",
            actor_id=neutral.member_id,
            meta={
                "member_id": neutral.member_id,
                "agent_id": prepared.agent_id,
                "membership_epoch": mesh.membership_epoch,
                "revocation_epoch": mesh.revocation_epoch,
                "scopes": list(prepared.scopes),
            },
        )
        self.runtime.repositories.history.sessions.assert_current_execution(
            transaction, observation=prepared.observation
        )
        if prepared.expires_at <= int(time.time()):
            _refuse()
        return {
            "access_token": token,
            "token_type": "DPoP",
            "expires_in": prepared.expires_at - int(time.time()),
            "member": me.public_member(current),
        }

    def delegation_token(self, claims, agent_id):
        with self.store._transaction() as transaction:
            self.current(transaction, claims, agent_id=agent_id)
            record = self.store._get_in_transaction(
                transaction,
                claims["sub"],
                SESSION_NAMESPACE,
                claims[MEMBER_CLAIM]["member_id"],
            )
            selected = {
                key: value for key, value in claims.items() if not key.startswith("_")
            }
            if (
                record is None
                or record.get("claims_sha256")
                != hashlib.sha256(_canonical(selected)).hexdigest()
                or not isinstance(record.get("delegation_token"), str)
            ):
                _refuse()
            return record["delegation_token"]


def admission_from_orchestrator(orchestrator):
    return MeshAdmission(orchestrator)


def _member_ids(owner_id, member_id):
    if any(
        type(value) is not str or not 1 <= len(value) <= 256
        for value in (owner_id, member_id)
    ):
        _refuse()


def _nonce_budget(owner_id, member_id):
    now = time.monotonic()
    with _NONCE_LOCK:
        for stale in [
            key
            for key, hits in _NONCE_HITS.items()
            if not hits or now - hits[-1] >= NONCE_SECONDS
        ]:
            _NONCE_HITS.pop(stale, None)
        key = (owner_id, member_id)
        hits = [hit for hit in _NONCE_HITS.get(key, ()) if now - hit < NONCE_SECONDS]
        if len(hits) >= 30 or (key not in _NONCE_HITS and len(_NONCE_HITS) >= 10_000):
            raise MeshRateLimited()
        _NONCE_HITS[key] = hits + [now]


def assert_dispatch_current(orchestrator, claims, agent_id, required_scope):
    if MEMBER_CLAIM not in claims:
        return
    try:
        service = admission_from_orchestrator(orchestrator)
        with service.store._transaction() as transaction:
            service.current(
                transaction, claims, agent_id=agent_id, required_scope=required_scope
            )
            service.store._audit_transition(
                transaction,
                claims["sub"],
                "mesh.member.admission",
                actor_kind="member",
                actor_id=claims[MEMBER_CLAIM]["member_id"],
                meta={
                    "agent_id": agent_id,
                    "scope": required_scope,
                    "membership_epoch": claims[MEMBER_CLAIM]["membership_epoch"],
                    "revocation_epoch": claims[MEMBER_CLAIM]["revocation_epoch"],
                },
            )
            service.current(
                transaction, claims, agent_id=agent_id, required_scope=required_scope
            )
    except Exception:
        _refuse()
