"""RFC 8693 token-exchange client that asks the identity server for a scoped agent token on behalf of a user token.
It is used by orchestrator.py until the server profile goes."""

import os
import logging
from typing import Optional, Dict, List

import aiohttp


logger = logging.getLogger("DelegationService")


GRANT_TYPE_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
TOKEN_TYPE_ACCESS = "urn:ietf:params:oauth:token-type:access_token"


class DelegationService:
    def __init__(self):
        self.authority = os.getenv("KEYCLOAK_AUTHORITY", "")
        self.client_id = os.getenv("KEYCLOAK_CLIENT_ID", "")
        self.client_secret = os.getenv("KEYCLOAK_CLIENT_SECRET", "")
        self.agent_service_client_id = os.getenv(
            "AGENT_SERVICE_CLIENT_ID", "astral-agent-service"
        )
        self.agent_service_client_secret = os.getenv(
            "AGENT_SERVICE_CLIENT_SECRET", ""
        )
        logger.info(
            f"DelegationService configured for Keycloak: {self.authority}"
        )

    async def exchange_token_for_agent(
        self,
        user_token: str,
        agent_id: str,
        allowed_tools: List[str],
        user_id: Optional[str] = None,
        enabled_scopes: Optional[List[str]] = None,
    ) -> Dict:
        if not enabled_scopes:
            logger.error(
                f"Refusing token exchange for agent '{agent_id}': the user has "
                f"no effective tool scopes for this agent"
            )
            return {
                "error": "no_enabled_scopes",
                "error_description": (
                    "no enabled tool scopes for this user/agent pair — "
                    "delegation would carry no authority"
                ),
            }

        return await self._exchange_via_keycloak(
            user_token, agent_id, allowed_tools, enabled_scopes
        )

    async def _exchange_via_keycloak(
        self,
        user_token: str,
        agent_id: str,
        allowed_tools: List[str],
        enabled_scopes: Optional[List[str]] = None,
    ) -> Dict:
        if not self.authority or not self.client_id or not self.client_secret:
            return {
                "error": "server_error",
                "error_description": "Keycloak not configured for delegation",
            }

        token_url = f"{self.authority}/protocol/openid-connect/token"

        combined_scopes = " ".join(enabled_scopes or [])

        form_data = {
            "grant_type": GRANT_TYPE_TOKEN_EXCHANGE,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "subject_token": user_token,
            "subject_token_type": TOKEN_TYPE_ACCESS,
            "requested_token_type": TOKEN_TYPE_ACCESS,
            "audience": self.agent_service_client_id,
            "scope": combined_scopes,
        }

        logger.info(
            f"Exchanging token for agent '{agent_id}' with "
            f"{len(allowed_tools)} allowed tool(s) (scope='{combined_scopes}')"
        )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    token_url, data=form_data
                ) as resp:
                    body = await resp.json()
                    if resp.status != 200:
                        logger.error(
                            f"Token exchange failed: {resp.status} {body}"
                        )
                        return {
                            "error": body.get("error", "exchange_failed"),
                            "error_description": body.get(
                                "error_description",
                                f"Keycloak returned {resp.status}",
                            ),
                        }

                    logger.info(
                        f"Token exchange successful for agent '{agent_id}'"
                    )
                    return {
                        "access_token": body["access_token"],
                        "token_type": body.get("token_type", "Bearer"),
                        "expires_in": body.get("expires_in", 300),
                        "scope": body.get("scope", combined_scopes),
                        "issued_token_type": body.get(
                            "issued_token_type", TOKEN_TYPE_ACCESS
                        ),
                        "agent_id": agent_id,
                    }
        except Exception as e:
            logger.error(f"Token exchange error: {e}")
            return {
                "error": "exchange_error",
                "error_description": str(e),
            }

    @staticmethod
    def extract_delegation_info(token_payload: dict) -> Optional[Dict]:
        act_claim = token_payload.get("act")
        if not act_claim:
            return None

        return {
            "user_id": token_payload.get("sub"),
            "actor": act_claim.get("sub"),
            "scopes": (token_payload.get("scope", "")).split(),
            "is_delegation": True,
        }
