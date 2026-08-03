#!/usr/bin/env python3
"""Run the locked Feature 065 RTC integration lane without persistent secrets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.voice-integration.yml"
SERVICE = "voice-worker-livekit-integration"


def _b64url(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def _join_grant(
    *,
    api_key: str,
    api_secret: str,
    room_name: str,
    identity: str,
    can_publish_data: bool,
    issued_at: int,
) -> str:
    claims: dict[str, Any] = {
        "video": {
            "roomCreate": False,
            "roomList": False,
            "roomRecord": False,
            "roomAdmin": False,
            "roomJoin": True,
            "room": room_name,
            "canPublish": True,
            "canSubscribe": True,
            "canPublishData": can_publish_data,
            "canPublishSources": ["microphone"],
            "canUpdateOwnMetadata": False,
            "ingressAdmin": False,
        },
        "sub": identity,
        "iss": api_key,
        "nbf": issued_at,
        "exp": issued_at + 90,
    }
    header = {"alg": "HS256", "typ": "JWT"}
    encoded = b".".join(
        _b64url(
            json.dumps(
                value,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        for value in (header, claims)
    )
    signature = hmac.new(api_secret.encode("utf-8"), encoded, hashlib.sha256).digest()
    return (encoded + b"." + _b64url(signature)).decode("ascii")


def _compose(project: str, *args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--ansi",
        "never",
        "--project-name",
        project,
        "--file",
        str(COMPOSE_FILE),
        *args,
    ]


def main() -> int:
    """Build, run, and tear down one isolated credential-ephemeral project."""

    if shutil.which("docker") is None or not COMPOSE_FILE.is_file():
        print("voice_livekit_integration_unavailable", file=sys.stderr)
        return 2

    environment = os.environ.copy()
    # Compose resolves required interpolation variables even for a build-only
    # invocation. Use inert placeholders until the potentially slow image build
    # is complete so the real 90-second room grants cannot expire in a cold cache.
    environment.update(
        {
            "VOICE_INTEGRATION_LIVEKIT_API_KEY": "build-placeholder-key",
            "VOICE_INTEGRATION_LIVEKIT_API_SECRET": "build-placeholder-secret",
            "VOICE_INTEGRATION_ROOM_NAME": "build-placeholder-room",
            "VOICE_INTEGRATION_WORKER_TOKEN": "build-placeholder-worker-token",
            "VOICE_INTEGRATION_CLIENT_TOKEN": "build-placeholder-client-token",
        }
    )
    project = "astraldeep-voice-int-" + secrets.token_hex(6)
    result = 1
    try:
        pull = subprocess.run(
            _compose(project, "pull", "livekit-integration"),
            cwd=REPO_ROOT,
            env=environment,
            check=False,
        )
        if pull.returncode != 0:
            return pull.returncode
        build = subprocess.run(
            _compose(project, "build", SERVICE),
            cwd=REPO_ROOT,
            env=environment,
            check=False,
        )
        if build.returncode != 0:
            return build.returncode
        api_key = "voice_int_" + secrets.token_hex(12)
        api_secret = secrets.token_hex(32)
        room_name = "voice-integration-" + secrets.token_hex(12)
        issued_at = int(time.time()) - 1
        environment.update(
            {
                "VOICE_INTEGRATION_LIVEKIT_API_KEY": api_key,
                "VOICE_INTEGRATION_LIVEKIT_API_SECRET": api_secret,
                "VOICE_INTEGRATION_ROOM_NAME": room_name,
                "VOICE_INTEGRATION_WORKER_TOKEN": _join_grant(
                    api_key=api_key,
                    api_secret=api_secret,
                    room_name=room_name,
                    identity="voice-worker-integration",
                    can_publish_data=True,
                    issued_at=issued_at,
                ),
                "VOICE_INTEGRATION_CLIENT_TOKEN": _join_grant(
                    api_key=api_key,
                    api_secret=api_secret,
                    room_name=room_name,
                    identity="client-integration",
                    can_publish_data=False,
                    issued_at=issued_at,
                ),
            }
        )
        api_key = ""
        api_secret = ""
        start = subprocess.run(
            _compose(
                project,
                "up",
                "--detach",
                "--pull",
                "never",
                "livekit-integration",
            ),
            cwd=REPO_ROOT,
            env=environment,
            check=False,
        )
        if start.returncode != 0:
            return start.returncode
        run = subprocess.run(
            _compose(project, "run", "--rm", "--no-deps", "-T", SERVICE),
            cwd=REPO_ROOT,
            env=environment,
            check=False,
        )
        result = run.returncode
    finally:
        subprocess.run(
            _compose(project, "down", "--volumes", "--remove-orphans"),
            cwd=REPO_ROOT,
            env=environment,
            check=False,
        )
        for name in (
            "VOICE_INTEGRATION_LIVEKIT_API_KEY",
            "VOICE_INTEGRATION_LIVEKIT_API_SECRET",
            "VOICE_INTEGRATION_ROOM_NAME",
            "VOICE_INTEGRATION_WORKER_TOKEN",
            "VOICE_INTEGRATION_CLIENT_TOKEN",
        ):
            environment[name] = ""
    return result


if __name__ == "__main__":
    raise SystemExit(main())
