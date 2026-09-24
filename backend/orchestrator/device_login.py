"""RFC 8628 device-authorization broker backing watch QR sign-in: issues an opaque
encrypted poll handle from Keycloak's device grant and polls/refreshes it; used by
auth.py and native_session_custody.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger("orchestrator.device_login")

__all__ = [
    "DeviceLoginError", "DeviceLoginUnavailable", "UnknownClient",
    "InvalidHandle", "RateLimited", "start", "poll", "refresh",
    "flag_on", "reset_state",
]

_SCOPE = "openid profile email offline_access"
_DISCOVERY_TTL_SECONDS = 300
_START_WINDOW_SECONDS = 60
_START_MAX_PER_WINDOW = int(os.getenv("DEVICE_LOGIN_START_RATE", "10"))
_DEFAULT_INTERVAL = 5
_SLOW_DOWN_BUMP = 5

_TOKEN_KEYS = (
    "access_token", "refresh_token", "expires_in", "refresh_expires_in",
    "token_type", "id_token", "scope",
)

HttpPostForm = Callable[[str, Dict[str, str]], Awaitable[Tuple[int, Dict[str, Any]]]]
HttpGetJson = Callable[[str], Awaitable[Tuple[int, Dict[str, Any]]]]


class DeviceLoginError(Exception):
    status = 500
    code = "device_login_error"


class DeviceLoginUnavailable(DeviceLoginError):
    status = 503
    code = "device_login_unavailable"


class UnknownClient(DeviceLoginError):
    status = 400
    code = "unknown_client"


class InvalidHandle(DeviceLoginError):
    status = 400
    code = "invalid_handle"


class RateLimited(DeviceLoginError):
    status = 429
    code = "rate_limited"


class RefreshRejected(DeviceLoginError):
    status = 401
    code = "invalid_grant"


_START_HITS: Dict[str, list] = {}
_POLL_STATE: Dict[str, Dict[str, Any]] = {}
_DISCOVERY: Dict[str, Any] = {"at": 0.0, "data": None}


def reset_state() -> None:
    _START_HITS.clear()
    _POLL_STATE.clear()
    _DISCOVERY["at"] = 0.0
    _DISCOVERY["data"] = None


def flag_on() -> bool:
    return os.getenv("FF_DEVICE_LOGIN", "1").strip().lower() not in ("0", "false", "no", "off")


def _authority() -> str:
    authority = (
        os.getenv("KEYCLOAK_AUTHORITY", "") or os.getenv("KEYCLOAK_AUTHORITY", "")
    ).rstrip("/")
    if not authority:
        raise DeviceLoginUnavailable(
            "KEYCLOAK_AUTHORITY is not configured; device login requires the IdP realm URL"
        )
    return authority


def device_grant_clients() -> set:
    raw = os.getenv("KEYCLOAK_DEVICE_CLIENTS", "astral-watch")
    return {c.strip() for c in raw.split(",") if c.strip()}


def _validate_client(client: str) -> str:
    client = (client or "").strip()
    from shared.auth_clients import _primary_client_id, allowed_azps
    if (
        not client
        or client not in device_grant_clients()
        or client == _primary_client_id()
        or client not in allowed_azps()
    ):
        raise UnknownClient(
            "client must be an allow-listed public device-grant client "
            "(KEYCLOAK_DEVICE_CLIENTS ∩ KEYCLOAK_ALLOWED_AZP)"
        )
    return client


def _fernet():
    key = os.getenv("WEB_SESSION_ENC_KEY") or os.getenv("OFFLINE_GRANT_ENC_KEY")
    if not key:
        raise DeviceLoginUnavailable(
            "WEB_SESSION_ENC_KEY (or OFFLINE_GRANT_ENC_KEY) is unset — the poll "
            "handle cannot be protected; device login is disabled"
        )
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode() if isinstance(key, str) else key)
    # Key errors mean unavailable — never expose plaintext
    except Exception as exc:
        raise DeviceLoginUnavailable(f"session encryption key unusable: {exc}") from None


async def _bounded_json(method, url, *, data=None):
    import httpx
    async with httpx.AsyncClient(timeout=10.0, trust_env=False, follow_redirects=False) as client:
        async with client.stream(method, url, data=data) as response:
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 65_536:
                    raise DeviceLoginUnavailable("IdP response exceeded the bounded envelope")
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                body = {}
            return response.status_code, body if isinstance(body, dict) else {}


async def _default_post_form(url: str, data: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    return await _bounded_json("POST", url, data=data)


async def _default_get_json(url: str) -> Tuple[int, Dict[str, Any]]:
    return await _bounded_json("GET", url)


async def _discover(http_get: Optional[HttpGetJson]) -> Dict[str, str]:
    now = time.time()
    if _DISCOVERY["data"] and now - _DISCOVERY["at"] < _DISCOVERY_TTL_SECONDS:
        return _DISCOVERY["data"]
    url = f"{_authority()}/.well-known/openid-configuration"
    getter = http_get or _default_get_json
    try:
        status, body = await getter(url)
    except DeviceLoginError:
        raise
    except Exception as exc:
        raise DeviceLoginUnavailable(f"IdP discovery failed: {exc}") from None
    if status != 200 or not isinstance(body, dict) or not body.get("token_endpoint"):
        raise DeviceLoginUnavailable(f"IdP discovery failed (HTTP {status})")
    if not body.get("device_authorization_endpoint"):
        raise DeviceLoginUnavailable(
            "the realm does not advertise device_authorization_endpoint — enable "
            "the OAuth 2.0 Device Authorization Grant on the device client "
            "(docs/keycloak-realm-settings.md)"
        )
    data = {
        "device_authorization_endpoint": body["device_authorization_endpoint"],
        "token_endpoint": body["token_endpoint"],
    }
    _DISCOVERY.update(at=now, data=data)
    return data


def _handle_digest(handle: str) -> str:
    return hashlib.sha256(handle.encode()).hexdigest()[:32]


def _jwt_claims(token: str) -> Dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _has_entry_role(claims: Dict[str, Any]) -> bool:
    roles = set(claims.get("realm_access", {}).get("roles", []) or [])
    for client_roles in (claims.get("resource_access", {}) or {}).values():
        roles.update(client_roles.get("roles", []) or [])
    return bool(roles & {"user", "admin"})


async def _audit(action: str, sub: str, description: str, *, outcome: str = "success") -> None:
    try:
        from audit.hooks import record_auth_event
        await record_auth_event(
            claims={"sub": sub or "anonymous"},
            action=action,
            description=description,
            outcome=outcome,
        )
    except Exception:
        logger.debug("device_login: audit hook unavailable for %s", action, exc_info=True)


async def _revoke_refresh(refresh_token: str, client_id: str) -> None:
    try:
        from orchestrator import web_auth
        await web_auth._revoke_refresh_token(refresh_token, client_id=client_id)
    except Exception:
        logger.warning("device_login: refresh-token revocation failed", exc_info=True)


def _check_start_rate(ip: str) -> None:
    now = time.time()
    hits = [t for t in _START_HITS.get(ip, []) if now - t < _START_WINDOW_SECONDS]
    if len(hits) >= _START_MAX_PER_WINDOW:
        _START_HITS[ip] = hits
        raise RateLimited("too many device-login starts; retry later")
    hits.append(now)
    _START_HITS[ip] = hits


async def start(
    client: str,
    ip: str,
    *,
    http_post: Optional[HttpPostForm] = None,
    http_get: Optional[HttpGetJson] = None,
    custody=None,
) -> Dict[str, Any]:
    if not flag_on():
        raise DeviceLoginUnavailable("FF_DEVICE_LOGIN is off")
    fernet = _fernet()
    client = _validate_client(client)
    if custody is None:
        _check_start_rate(ip or "unknown")
    disco = await _discover(http_get)
    if custody is not None:
        from orchestrator.native_session_custody import assert_discovery
        assert_discovery(custody, disco)

    poster = http_post or _default_post_form
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    try:
        status, body = await poster(
            disco["device_authorization_endpoint"],
            {"client_id": client, "scope": _SCOPE,
             "code_challenge": code_challenge,
             "code_challenge_method": "S256"},
        )
    except DeviceLoginError:
        raise
    except Exception as exc:
        raise DeviceLoginUnavailable(f"IdP device authorization failed: {exc}") from None
    if status != 200 or not body.get("device_code") or not body.get("user_code"):
        idp_error = str(body.get("error", "")) if isinstance(body, dict) else ""
        if idp_error == "unauthorized_client":
            raise DeviceLoginUnavailable(
                "the realm has not enabled the device grant for this client — "
                "ask an admin to turn on 'OAuth 2.0 Device Authorization Grant' "
                f"for '{client}' (docs/keycloak-realm-settings.md §051)")
        detail = f" ({idp_error})" if idp_error else ""
        raise DeviceLoginUnavailable(
            f"IdP refused device authorization (HTTP {status}){detail}")

    user_code = str(body["user_code"])
    verification_uri = str(body.get("verification_uri", ""))
    verification_uri_complete = str(
        body.get("verification_uri_complete", "")
        or (f"{verification_uri}?user_code={user_code}" if verification_uri else "")
    )
    if not verification_uri_complete:
        raise DeviceLoginUnavailable("IdP response lacked a verification URI")
    expires_in = int(body.get("expires_in", 600))
    interval = max(int(body.get("interval", _DEFAULT_INTERVAL)), 1)

    now = time.time()
    extra = {}
    if custody is not None:
        if (type(body.get("expires_in")) is not int or not 1 <= expires_in <= 600
                or type(body.get("interval", _DEFAULT_INTERVAL)) is not int
                or not 1 <= interval <= 600
                or not verification_uri_complete.startswith(custody.issuer + "/")):
            raise DeviceLoginUnavailable("native device grant unavailable")
        await custody.current()
        custody.expires = min(custody.expires, now + expires_in)
        extra = {"custody": {"version": 1, "issuer": custody.issuer,
                             "client": client, "owner": custody.owner,
                             "expires": custody.expires}}
    handle = fernet.encrypt(json.dumps({
        "dc": str(body["device_code"]),
        "cv": code_verifier,
        "client": client,
        "iat": now,
        "exp": now + expires_in,
        "interval": interval,
        **extra,
    }).encode()).decode("ascii")
    _POLL_STATE[_handle_digest(handle)] = {
        "next_ok": now + interval, "interval": interval, "used": False,
        **({"custody": custody, "in_flight": False} if custody is not None else {}),
    }

    from shared.qr import encode_matrix, qr_png_base64
    await _audit(
        "device_login_started", "anonymous",
        (f"Device sign-in started for {client}; user_code {user_code}" if custody is None
         else "Native server-custody device sign-in started"),
    )
    return {
        "handle": handle,
        "user_code": user_code,
        "verification_uri": verification_uri,
        "verification_uri_complete": verification_uri_complete,
        "expires_in": expires_in,
        "interval": interval,
        "qr_png_base64": qr_png_base64(verification_uri_complete, scale=6, border=2),
        "qr_matrix": encode_matrix(verification_uri_complete),
    }


async def poll(
    handle: str,
    ip: str,
    *,
    http_post: Optional[HttpPostForm] = None,
    http_get: Optional[HttpGetJson] = None,
    custody=None,
) -> Dict[str, Any]:
    if not flag_on():
        raise DeviceLoginUnavailable("FF_DEVICE_LOGIN is off")
    fernet = _fernet()
    try:
        blob = json.loads(fernet.decrypt((handle or "").encode()))
        device_code = blob["dc"]
        code_verifier = blob.get("cv", "")
        client = blob["client"]
        exp = float(blob["exp"])
    except Exception:
        raise InvalidHandle("poll handle is invalid") from None

    info = blob.get("custody")
    if (info is not None) != (custody is not None):
        raise InvalidHandle("poll handle mode mismatch")
    if custody is not None:
        binding, _finish = custody
        if info != {"version": 1, "issuer": binding.issuer, "client": binding.client,
                    "owner": binding.owner, "expires": binding.expires}:
            raise InvalidHandle("poll handle binding mismatch")

    digest = _handle_digest(handle)
    now = time.time()
    state = _POLL_STATE.setdefault(digest, {
        "next_ok": 0.0, "interval": int(blob.get("interval", _DEFAULT_INTERVAL)),
        "used": False,
    })
    if state["used"]:
        raise InvalidHandle("poll handle already completed")
    if now >= exp:
        state["used"] = True
        await _audit("device_login_expired", "anonymous",
                     f"Device sign-in expired for {client}", outcome="failure")
        return {"status": "expired"}
    if now < state["next_ok"]:
        return {"status": "slow_down", "interval": state["interval"]}

    disco = await _discover(http_get)
    if custody is not None:
        from orchestrator.native_session_custody import assert_discovery
        await custody[0].current()
        assert_discovery(custody[0], disco)
    poster = http_post or _default_post_form
    try:
        token_request = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": client,
        }
        if code_verifier:
            token_request["code_verifier"] = code_verifier
        status, body = await poster(disco["token_endpoint"], token_request)
    except DeviceLoginError:
        raise
    except Exception as exc:
        raise DeviceLoginUnavailable(f"IdP token poll failed: {exc}") from None

    if status == 200 and body.get("access_token"):
        if custody is not None:
            state["used"] = True
            return await custody[1](body)
        claims = _jwt_claims(str(body["access_token"]))
        sub = str(claims.get("sub", "") or "anonymous")
        state["used"] = True
        if not _has_entry_role(claims):
            await _revoke_refresh(str(body.get("refresh_token", "")), client)
            await _audit("device_login_denied", sub,
                         f"Device sign-in refused for {client}: token has no "
                         "user/admin role", outcome="failure")
            return {"status": "denied", "reason": "denied_no_access"}
        await _audit("device_login_approved", sub,
                     f"Device sign-in approved for {client}")
        return {"status": "approved",
                "tokens": {k: body[k] for k in _TOKEN_KEYS if k in body}}

    error = str(body.get("error", "") or "")
    if error == "authorization_pending":
        state["next_ok"] = now + state["interval"]
        return {"status": "pending", "interval": state["interval"]}
    if error == "slow_down":
        state["interval"] += _SLOW_DOWN_BUMP
        state["next_ok"] = now + state["interval"]
        return {"status": "slow_down", "interval": state["interval"]}
    if error in ("expired_token", "invalid_grant"):
        state["used"] = True
        await _audit("device_login_expired", "anonymous",
                     f"Device sign-in expired for {client}", outcome="failure")
        return {"status": "expired"}
    if error == "access_denied":
        state["used"] = True
        await _audit("device_login_denied", "anonymous",
                     f"Device sign-in denied by the user for {client}",
                     outcome="failure")
        return {"status": "denied", "reason": "access_denied"}
    raise DeviceLoginUnavailable(f"IdP token poll failed (HTTP {status}, {error or 'no error code'})")


async def refresh(
    client: str,
    refresh_token: str,
    *,
    http_post: Optional[HttpPostForm] = None,
    http_get: Optional[HttpGetJson] = None,
) -> Dict[str, Any]:
    if not flag_on():
        raise DeviceLoginUnavailable("FF_DEVICE_LOGIN is off")
    client = _validate_client(client)
    if not (refresh_token or "").strip():
        raise RefreshRejected("refresh_token is required")
    disco = await _discover(http_get)
    poster = http_post or _default_post_form
    try:
        status, body = await poster(disco["token_endpoint"], {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client,
        })
    except DeviceLoginError:
        raise
    except Exception as exc:
        raise DeviceLoginUnavailable(f"IdP refresh failed: {exc}") from None
    if status == 200 and body.get("access_token"):
        return {k: body[k] for k in _TOKEN_KEYS if k in body}
    raise RefreshRejected("the IdP rejected the refresh token")
