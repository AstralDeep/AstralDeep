"""Manual script exchanging a real user token for a delegation token against a live
Keycloak instance, reading credentials from the environment; not run in CI.
"""

import os
import json
import base64
import asyncio
import aiohttp
import argparse


def load_env(path: str):
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k] = v
    else:
        print(f"Warning: .env file not found at {path}")


def decode(tok):
    p = tok.split(".")[1]
    p += "=" * ((4 - len(p) % 4) % 4)
    return json.loads(base64.urlsafe_b64decode(p))


async def main(token: str | None = None):
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
    load_env(env_path)

    AUTHORITY = os.getenv("KEYCLOAK_AUTHORITY")
    CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID")
    CLIENT_SECRET = os.getenv("KEYCLOAK_CLIENT_SECRET", "")
    AGENT_CLIENT = os.getenv("AGENT_SERVICE_CLIENT_ID", "astral-agent-service")
    TOKEN_URL = f"{AUTHORITY}/protocol/openid-connect/token"

    USER_TOKEN = token
    if not USER_TOKEN:
        print("No token provided. Set KEYCLOAK_USER_TOKEN env var or pass --token.")
        print("Example: python test_real_exchange.py --token eyJhbG...")
        return

    print("User token claims:")
    user_payload = decode(USER_TOKEN)
    print(f"  sub: {user_payload['sub']}")
    print(f"  name: {user_payload.get('name')}")
    print(f"  azp: {user_payload.get('azp')}")
    print()

    print("Exchanging user token for delegation token...")
    async with aiohttp.ClientSession() as session:
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "subject_token": USER_TOKEN,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": AGENT_CLIENT,
        }
        async with session.post(TOKEN_URL, data=data) as resp:
            body = await resp.json()
            if resp.status != 200:
                print(f"FAIL ({resp.status}): {body.get('error')}")
                print(f"  {body.get('error_description')}")
                return

            delegation_token = body["access_token"]
            delegation_payload = decode(delegation_token)

            print("Delegation token claims:")
            print(f"  sub: {delegation_payload['sub']}")
            print(f"  azp: {delegation_payload.get('azp')}")
            print(f"  audience: {delegation_payload.get('aud')}")
            act = delegation_payload.get("act")
            if act:
                print(f"  act.sub: {act.get('sub')}")
                print(f"  act.act: {json.dumps(act.get('act', {}))}")
            print()
            print("SUCCESS: Token exchange completed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Keycloak RFC 8693 token exchange")
    parser.add_argument(
        "--token",
        default=os.getenv("KEYCLOAK_USER_TOKEN", ""),
        help="User access token to exchange (or set KEYCLOAK_USER_TOKEN env var)",
    )
    args = parser.parse_args()
    asyncio.run(main(token=args.token))