"""Polls the isolated TLS listener until it answers, without treating that response as
proof the application behind it has accepted traffic.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import ssl
import time
from pathlib import Path

import httpx


def turn_tls(context: ssl.SSLContext) -> None:
    with socket.create_connection(("ad-bwq-turn", 443), timeout=3) as connection:
        with context.wrap_socket(connection, server_hostname="ad-bwq-turn"):
            pass


def observe(root: Path, qualification_id: str) -> None:
    context = ssl.create_default_context(cafile=str(root / "ca-bundle.pem"))
    issuer = "https://ad-bwq-keycloak:8443/realms/astral-bwq-" + qualification_id
    with httpx.Client(
        verify=context, trust_env=False, timeout=3, follow_redirects=False
    ) as client:
        discovery = client.get(issuer + "/.well-known/openid-configuration")
        discovery.raise_for_status()
        if discovery.json().get("issuer") != issuer:
            raise ValueError("isolated IAM issuer mismatch")
        livekit = client.get("https://ad-bwq-livekit:9444/")
        livekit.raise_for_status()
        proxy = client.get("https://ad-bwq-web:9443/")
        if not 200 <= proxy.status_code < 600:
            raise ValueError("web TLS listener unavailable")
    warden_context = ssl.create_default_context(cafile=str(root / "lets-ca.pem"))
    warden_context.load_cert_chain(
        str(root / "lets-client-cert.pem"), str(root / "lets-client-key.pem")
    )
    with httpx.Client(verify=warden_context, trust_env=False, timeout=3) as client:
        ready = client.get("https://ad-bwq-warden:8443/health/ready")
        ready.raise_for_status()
        if ready.json() != {"status": "ready"}:
            raise ValueError("warden not ready")
    turn_tls(context)


def wait(root: Path, qualification_id: str, timeout: int = 120) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", qualification_id) or not 1 <= timeout <= 150:
        raise ValueError("invalid isolated infrastructure observation bounds")
    if (
        not root.is_absolute()
        or root.resolve() != root
        or root.is_symlink()
        or not root.is_dir()
    ):
        raise ValueError("invalid material root")
    marker = json.loads((root / "qualification.json").read_text(encoding="utf-8"))
    if marker != {
        "schema_version": 1,
        "qualification_id": qualification_id,
        "classification": "synthetic",
    }:
        raise ValueError("synthetic material marker mismatch")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            observe(root, qualification_id)
        except (OSError, ValueError, httpx.HTTPError):
            time.sleep(1)
        else:
            return {
                "schema_version": 1,
                "qualification_id": qualification_id,
                "status": "infrastructure-ready",
                "application_acceptance": False,
                "observations": [
                    "iam-discovery-tls",
                    "warden-ready-mtls",
                    "livekit-signaling-tls",
                    "turn-tls-handshake",
                    "web-tls-listener",
                ],
            }
    raise RuntimeError("isolated TLS infrastructure readiness timed out")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--material-root", required=True, type=Path)
    parser.add_argument("--qualification-id", required=True)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    try:
        result = wait(args.material_root, args.qualification_id, args.timeout)
    except (RuntimeError, ValueError, OSError):
        print("isolated infrastructure is not ready")
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
