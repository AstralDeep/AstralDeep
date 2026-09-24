"""Opt-in live-network driver (backend/verification/config.py, evidence.py,
isolation.py): drives a live deployment over REST upload and WebSocket chat against a
real Keycloak realm, degrading to a labelled mock run when unreachable.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional

from verification.config import RunConfig
from verification.evidence import CapturedEvidence, flatten_components
from verification.isolation import Principal

logger = logging.getLogger("verification.external")

_UI_TYPES = {"ui_render", "ui_upsert", "chat_status", "user_message_acked", "chat_created"}


def decide_auth_mode(config: RunConfig, reachable: Optional[bool] = None) -> tuple[str, List[str]]:
    flags: List[str] = []
    if not config.keycloak_available():
        flags.append("keycloak_credentials_absent")
        return "mock_inprocess", flags
    if reachable is False:
        flags.append("keycloak_unreachable_degraded")
        return "mock_inprocess", flags
    return "real_keycloak", flags


def parse_ws_messages(raw: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for frame in raw:
        if isinstance(frame, str):
            try:
                frame = json.loads(frame)
            except json.JSONDecodeError:
                continue
        if isinstance(frame, dict) and frame.get("type") in _UI_TYPES:
            out.append(frame)
    return out


class ExternalDriver:
    mode = "external"

    def __init__(
        self,
        config: RunConfig,
        *,
        http: Optional[Callable[..., Any]] = None,
        ws_exchange: Optional[Callable[..., Any]] = None,
        reachable: Optional[bool] = None,
    ) -> None:
        self.config = config
        self._http = http
        self._ws_exchange = ws_exchange
        self.auth_mode, self.flags = decide_auth_mode(config, reachable=reachable)
        self.base_url = config.base_url or os.environ.get("ASTRAL_VERIFY_BASE_URL", "")

    async def setup(self) -> None:
        if not self.base_url:
            raise ValueError("external mode requires --base-url or ASTRAL_VERIFY_BASE_URL")

    async def teardown(self) -> None:
        return None

    def _token_for(self, principal: Principal) -> str:
        if self.auth_mode == "real_keycloak" and self._http is not None:
            authority = os.environ.get("KEYCLOAK_AUTHORITY", "")
            realm = os.environ.get("KEYCLOAK_REALM", "astral")
            url = f"{authority.rstrip('/')}/realms/{realm}/protocol/openid-connect/token"
            resp = self._http(
                "POST", url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": os.environ.get("KEYCLOAK_CLIENT_ID", ""),
                    "client_secret": os.environ.get("KEYCLOAK_CLIENT_SECRET", ""),
                },
            )
            return (resp or {}).get("access_token", "")
        return "dev-token"

    async def run_scenario(self, scenario: Any) -> CapturedEvidence:
        principal = scenario.principal
        token = self._token_for(principal)
        persona = scenario.persona

        upload = self._http(
            "POST", f"{self.base_url.rstrip('/')}/api/upload", token=token,
            files={"file": (persona.fixture.filename, b"<fixture>")},
        ) if self._http else {}
        attachment_id = (upload or {}).get("attachment_id", "")

        register = {"type": "register_ui", "token": token, "device": {"device_type": "browser"}}
        chat = {
            "type": "chat_message",
            "payload": {
                "message": persona.query,
                "attachments": [
                    {"attachment_id": attachment_id, "filename": persona.fixture.filename,
                     "category": persona.fixture.category}
                ],
            },
        }
        raw: List[Any] = []
        if self._ws_exchange is not None:
            raw = await self._ws_exchange(
                f"{self.base_url.rstrip('/')}/ws", token, register, chat
            )
        messages = parse_ws_messages(raw)
        return CapturedEvidence(
            evidence_id=f"{scenario.scenario_id}:ext",
            scenario_id=scenario.scenario_id,
            run_mode=self.auth_mode,
            messages=messages,
            components=flatten_components(messages),
            extra={"attachment_id": attachment_id, "flags": list(self.flags),
                   "file_category": persona.fixture.category},
        )
