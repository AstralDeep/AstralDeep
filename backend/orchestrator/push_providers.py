"""Bounded APNs/FCM provider adapters for owner-bound mobile push: provider endpoints
and sender credentials come only from runtime configuration, outbound requests use the
approved shared.external_http egress path, and every failure is classified as transient
or permanent for the delivery pipeline's outcome state machine.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from jose import jwt
from shared.external_http import (
    AuthFailedError,
    BadRequestError,
    ExternalHttpError,
    request,
    request_http2,
)

logger = logging.getLogger("Orchestrator.PushProviders")

FCM_TOKEN_URL = "https://oauth2.googleapis.com/token"
FCM_SEND_URL_TEMPLATE = (
    "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
)
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
APNS_DEFAULT_HOST = "https://api.push.apple.com"
APNS_TOKEN_LIFETIME_SECONDS = 1800
FCM_TOKEN_LIFETIME_SECONDS = 3300
_DETAIL_BODY = re.compile(r": (\{.*\})\s*$", re.DOTALL)


class PushProviderError(Exception):
    def __init__(self, *, transient: bool, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.transient = transient
        self.code = code
        self.detail = (detail or code)[:200].replace("\n", " ").replace("\r", " ")


def _detail_body(detail: str) -> dict[str, Any]:
    match = _DETAIL_BODY.search(detail or "")
    if match is None:
        return {}
    try:
        parsed = json.loads(match.group(1))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class FcmPushAdapter:
    provider = "fcm"

    def __init__(
        self,
        *,
        project_id: str,
        client_email: str,
        private_key: str,
        token_url: str = FCM_TOKEN_URL,
        send_url_template: str = FCM_SEND_URL_TEMPLATE,
        transport: Callable[..., Any] = request,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._project_id = (project_id or "").strip()
        self._client_email = (client_email or "").strip()
        self._private_key = (private_key or "").strip()
        self._token_url = token_url
        self._send_url = send_url_template.format(project_id=self._project_id)
        self._transport = transport
        self._clock = clock
        self._cached_token: str | None = None
        self._cached_token_expiry = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._project_id and self._client_email and self._private_key)

    def _transport_failure(self, exc: ExternalHttpError) -> PushProviderError:
        if isinstance(exc, AuthFailedError):
            self._cached_token = None
            return PushProviderError(
                transient=True,
                code="provider_credentials_rejected",
                detail="fcm rejected the sender credential",
            )
        return PushProviderError(
            transient=True, code="provider_unavailable", detail=str(exc)
        )

    def _access_token(self) -> str:
        now = self._clock()
        if self._cached_token and now < self._cached_token_expiry:
            return self._cached_token
        assertion = jwt.encode(
            {
                "iss": self._client_email,
                "scope": FCM_SCOPE,
                "aud": self._token_url,
                "iat": int(now),
                "exp": int(now) + FCM_TOKEN_LIFETIME_SECONDS,
            },
            self._private_key,
            algorithm="RS256",
        )
        try:
            response = self._transport(
                "POST",
                self._token_url,
                api_key="",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
                timeout=15,
                max_response_bytes=64 * 1024,
            )
        except ExternalHttpError as exc:
            raise self._transport_failure(exc) from None
        try:
            parsed = json.loads(getattr(response, "text", "") or "")
        except Exception:
            parsed = {}
        granted = parsed.get("access_token") if isinstance(parsed, dict) else None
        if not isinstance(granted, str) or not granted:
            raise PushProviderError(
                transient=True,
                code="provider_unavailable",
                detail="fcm oauth token exchange returned no access token",
            )
        self._cached_token = granted
        self._cached_token_expiry = now + FCM_TOKEN_LIFETIME_SECONDS
        return granted

    def send(
        self,
        *,
        device_token: str,
        title: str,
        body: str,
        notification_id: str,
    ) -> None:
        if not self.configured:
            raise PushProviderError(
                transient=True,
                code="provider_unconfigured",
                detail="fcm sender credentials are not configured",
            )
        access_token = self._access_token()
        payload = {
            "message": {
                "token": device_token,
                "notification": {"title": title, "body": body},
                "data": {"notification_id": notification_id},
            }
        }
        try:
            self._transport(
                "POST",
                self._send_url,
                api_key=access_token,
                json_body=payload,
                timeout=15,
                max_response_bytes=64 * 1024,
            )
        except ExternalHttpError as exc:
            raise self._send_failure(exc) from None
        logger.info(
            "push_provider.fcm_accepted",
            extra={"notification_id": notification_id, "provider": self.provider},
        )

    def _send_failure(self, exc: ExternalHttpError) -> PushProviderError:
        if isinstance(exc, AuthFailedError):
            self._cached_token = None
            return PushProviderError(
                transient=True,
                code="provider_credentials_rejected",
                detail="fcm rejected the sender credential",
            )
        if isinstance(exc, BadRequestError):
            error = _detail_body(str(exc)).get("error")
            status = error.get("status") if isinstance(error, dict) else None
            if status == "UNREGISTERED":
                return PushProviderError(
                    transient=False,
                    code="token_unregistered",
                    detail="fcm reports the device token as unregistered",
                )
            if status == "SENDER_ID_MISMATCH":
                return PushProviderError(
                    transient=False,
                    code="token_sender_mismatch",
                    detail="device token is registered to a different sender",
                )
            if status in {"THIRD_PARTY_AUTH_ERROR", "INVALID_ARGUMENT"}:
                return PushProviderError(
                    transient=False,
                    code="request_rejected",
                    detail=str(status),
                )
            return PushProviderError(
                transient=True, code="provider_unavailable", detail=str(exc)
            )
        return PushProviderError(
            transient=True, code="provider_unavailable", detail=str(exc)
        )


class ApnsPushAdapter:
    provider = "apns"

    def __init__(
        self,
        *,
        key_id: str,
        team_id: str,
        private_key: str,
        topic: str,
        host: str = APNS_DEFAULT_HOST,
        transport: Callable[..., Any] = request_http2,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._key_id = (key_id or "").strip()
        self._team_id = (team_id or "").strip()
        self._private_key = (private_key or "").strip()
        self._topic = (topic or "").strip()
        self._host = (host or APNS_DEFAULT_HOST).strip().rstrip("/")
        self._transport = transport
        self._clock = clock
        self._cached_token: str | None = None
        self._cached_token_expiry = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._key_id and self._team_id and self._private_key and self._topic)

    def _provider_token(self) -> str:
        now = self._clock()
        if self._cached_token and now < self._cached_token_expiry:
            return self._cached_token
        self._cached_token = jwt.encode(
            {"iss": self._team_id, "iat": int(now)},
            self._private_key,
            algorithm="ES256",
            headers={"kid": self._key_id},
        )
        self._cached_token_expiry = now + APNS_TOKEN_LIFETIME_SECONDS
        return self._cached_token

    def send(
        self,
        *,
        device_token: str,
        title: str,
        body: str,
        notification_id: str,
    ) -> None:
        if not self.configured:
            raise PushProviderError(
                transient=True,
                code="provider_unconfigured",
                detail="apns sender credentials are not configured",
            )
        provider_token = self._provider_token()
        url = f"{self._host}/3/device/{device_token}"
        payload = {
            "aps": {
                "alert": {"title": title, "body": body},
                "sound": "default",
            },
            "notification_id": notification_id,
        }
        headers = {
            "apns-topic": self._topic,
            "apns-push-type": "alert",
            "apns-priority": "10",
        }
        try:
            self._transport(
                "POST",
                url,
                api_key=provider_token,
                json_body=payload,
                extra_headers=headers,
                timeout=15,
                max_response_bytes=64 * 1024,
            )
        except ExternalHttpError as exc:
            raise self._send_failure(exc) from None
        logger.info(
            "push_provider.apns_accepted",
            extra={"notification_id": notification_id, "provider": self.provider},
        )

    def _send_failure(self, exc: ExternalHttpError) -> PushProviderError:
        if isinstance(exc, AuthFailedError):
            return PushProviderError(
                transient=True,
                code="provider_credentials_rejected",
                detail="apns rejected the provider token or topic",
            )
        if isinstance(exc, BadRequestError):
            reason = str(_detail_body(str(exc)).get("reason") or "")
            status = _status_from_detail(str(exc))
            if status == 410 or reason == "Unregistered":
                return PushProviderError(
                    transient=False,
                    code="token_unregistered",
                    detail=reason or "device token is unregistered",
                )
            if status == 400 and reason == "BadDeviceToken":
                return PushProviderError(
                    transient=False,
                    code="token_unregistered",
                    detail=reason,
                )
            if status == 403 or reason in {"InvalidProviderToken", "InvalidTopic"}:
                return PushProviderError(
                    transient=True,
                    code="provider_credentials_rejected",
                    detail=reason or "apns rejected the provider credential",
                )
            if reason in {"BadTopic", "TopicDisallowed", "PayloadEmpty", "PayloadTooLarge"}:
                return PushProviderError(
                    transient=False,
                    code="request_rejected",
                    detail=reason,
                )
            return PushProviderError(
                transient=False, code="request_rejected", detail=reason or str(exc)
            )
        return PushProviderError(
            transient=True, code="provider_unavailable", detail=str(exc)
        )


def _status_from_detail(detail: str) -> int | None:
    match = re.search(r"returned (\d{3}):", detail or "")
    if match is None:
        return None
    return int(match.group(1))


@dataclass(frozen=True)
class PushProviderSettings:
    fcm_project_id: str
    fcm_client_email: str
    fcm_private_key: str
    apns_key_id: str
    apns_team_id: str
    apns_private_key: str
    apns_topic: str
    apns_host: str


def settings_from_env(environ: Any = None) -> PushProviderSettings:
    env = os.environ if environ is None else environ
    return PushProviderSettings(
        fcm_project_id=env.get("PUSH_FCM_PROJECT_ID", ""),
        fcm_client_email=env.get("PUSH_FCM_CLIENT_EMAIL", ""),
        fcm_private_key=env.get("PUSH_FCM_PRIVATE_KEY", ""),
        apns_key_id=env.get("PUSH_APNS_KEY_ID", ""),
        apns_team_id=env.get("PUSH_APNS_TEAM_ID", ""),
        apns_private_key=env.get("PUSH_APNS_PRIVATE_KEY", ""),
        apns_topic=env.get("PUSH_APNS_TOPIC", ""),
        apns_host=env.get("PUSH_APNS_HOST", APNS_DEFAULT_HOST),
    )


def adapters_from_settings(settings: PushProviderSettings) -> dict[str, Any]:
    return {
        "fcm": FcmPushAdapter(
            project_id=settings.fcm_project_id,
            client_email=settings.fcm_client_email,
            private_key=settings.fcm_private_key,
        ),
        "apns": ApnsPushAdapter(
            key_id=settings.apns_key_id,
            team_id=settings.apns_team_id,
            private_key=settings.apns_private_key,
            topic=settings.apns_topic,
            host=settings.apns_host,
        ),
    }


def adapters_from_env(environ: Any = None) -> dict[str, Any]:
    return adapters_from_settings(settings_from_env(environ))
