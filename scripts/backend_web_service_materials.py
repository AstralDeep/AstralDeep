"""Generates private synthetic TLS/identity materials for isolated backend/web
rehearsal, using LETS's canonical/crypto/manifest/policy modules; runs with no
network access and is exercised by test_backend_web_services.py.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import certifi
from cryptography.fernet import Fernet
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from lets.canonical import b64url_encode, canonical_json
from lets.crypto import Ed25519Signer
from lets.manifest import (
    ClusterManifest,
    ManifestPublicKey,
    ManifestSignature,
    WardenManifest,
)
from lets.policy import MachineSpec, PolicySpec, ResourceDimension, TransitionSpec
from nacl.signing import SigningKey
from orchestrator.lets_scope_profile import SCOPE_BINDINGS


def _require_posix() -> None:
    if os.name != "posix":
        raise ValueError("service material ownership requires POSIX")


def generate_materials(
    destination: Path,
    qualification_id: str,
    lets_source: Path,
    *,
    operator_uid: int = 0,
) -> dict:
    if not re.fullmatch("[0-9a-f]{32}", qualification_id):
        raise ValueError("invalid qualification ID")
    if (
        not destination.is_absolute()
        or destination.is_symlink()
        or not destination.is_dir()
    ):
        raise ValueError("material root must be an absolute real directory")
    if destination.resolve() != destination or any(destination.iterdir()):
        raise ValueError("material root must be canonical and empty")
    if type(operator_uid) is not int or operator_uid < 0:
        raise ValueError("invalid operator UID")
    _require_posix()
    ROOT, ID = destination, qualification_id
    ROOT.chmod(0o700)

    def output(path, value, owner=None, executable=False):
        path = ROOT / path
        owner = operator_uid if owner is None else owner
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = value if isinstance(value, bytes) else value.encode()
        with path.open("xb") as stream:
            stream.write(payload)
        path.chmod(0o700 if executable else 0o600)
        os.chown(path, owner, owner)

    def document(path, value, owner=None):
        output(path, json.dumps(value, sort_keys=True, indent=2) + "\n", owner)

    def certificate(common_name, issuer=None, usage=None):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        now = datetime.now(timezone.utc)
        builder = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(issuer[1].subject if issuer else name)
            .public_key(private.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=2))
            .add_extension(
                x509.BasicConstraints(
                    ca=issuer is None, path_length=0 if issuer is None else None
                ),
                critical=True,
            )
        )
        if usage:
            builder = builder.add_extension(
                x509.ExtendedKeyUsage([usage]), critical=False
            )
        if usage == ExtendedKeyUsageOID.SERVER_AUTH:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False
            )
        cert = builder.sign(issuer[0] if issuer else private, hashes.SHA256())
        return private, cert

    def save_cert(prefix, pair, owner=None):
        output(
            prefix + "-cert.pem",
            pair[1].public_bytes(serialization.Encoding.PEM),
            owner,
        )
        output(
            prefix + "-key.pem",
            pair[0].private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
            owner,
        )

    iam_ca = certificate("Astral synthetic IAM CA " + ID)
    lets_ca = certificate("Astral synthetic LETS CA " + ID)
    save_cert(
        "keycloak/server",
        certificate("ad-bwq-keycloak", iam_ca, ExtendedKeyUsageOID.SERVER_AUTH),
        1000,
    )
    save_cert(
        "proxy/server",
        certificate("ad-bwq-web", iam_ca, ExtendedKeyUsageOID.SERVER_AUTH),
    )
    save_cert(
        "proxy/livekit",
        certificate("ad-bwq-livekit", iam_ca, ExtendedKeyUsageOID.SERVER_AUTH),
    )
    save_cert(
        "livekit/turn",
        certificate("ad-bwq-turn", iam_ca, ExtendedKeyUsageOID.SERVER_AUTH),
    )
    save_cert(
        "warden/tls/server",
        certificate("ad-bwq-warden", lets_ca, ExtendedKeyUsageOID.SERVER_AUTH),
        10001,
    )
    save_cert(
        "warden/tls/peer",
        certificate("ad-bwq-peer", lets_ca, ExtendedKeyUsageOID.CLIENT_AUTH),
        10001,
    )
    save_cert(
        "app/lets-client",
        certificate("ad-bwq-app", lets_ca, ExtendedKeyUsageOID.CLIENT_AUTH),
    )
    output(
        "warden/tls/ca.pem", lets_ca[1].public_bytes(serialization.Encoding.PEM), 10001
    )
    output("app/lets-ca.pem", lets_ca[1].public_bytes(serialization.Encoding.PEM))
    output("app/iam-ca.pem", iam_ca[1].public_bytes(serialization.Encoding.PEM))
    output(
        "app/ca-bundle.pem",
        Path(certifi.where()).read_bytes()
        + iam_ca[1].public_bytes(serialization.Encoding.PEM),
    )
    output("voice/ca-bundle.pem", (ROOT / "app/ca-bundle.pem").read_bytes(), 10001)
    document(
        "app/qualification.json",
        {"schema_version": 1, "qualification_id": ID, "classification": "synthetic"},
    )

    client_secret, agent_secret, pg_password = (
        secrets.token_urlsafe(40) for _ in range(3)
    )
    users = {
        name: secrets.token_urlsafe(32)
        for name in ("owner-a", "owner-b", "administrator")
    }
    tool_scopes = [entry.scope for entry in SCOPE_BINDINGS]

    def client(name, secret, web=False):
        return {
            "clientId": name,
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": False,
            "secret": secret,
            "standardFlowEnabled": web,
            "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": not web,
            "fullScopeAllowed": False,
            "redirectUris": ["https://ad-bwq-web:9443/auth/callback"] if web else [],
            "webOrigins": ["https://ad-bwq-web:9443"] if web else [],
            "attributes": {
                "standard.token.exchange.enabled": "true",
                "pkce.code.challenge.method": "S256",
            }
            if web
            else {"standard.token.exchange.enabled": "true"},
            "defaultClientScopes": ["basic", "profile", "email", "roles"],
            "optionalClientScopes": ["offline_access"] + tool_scopes,
            "protocolMappers": [
                {
                    "name": "agent-service-audience",
                    "protocol": "openid-connect",
                    "protocolMapper": "oidc-audience-mapper",
                    "consentRequired": False,
                    "config": {
                        "included.client.audience": "astral-agent-service",
                        "access.token.claim": "true",
                        "id.token.claim": "false",
                    },
                }
            ]
            if web
            else [],
        }

    realm = {
        "realm": "astral-bwq-" + ID,
        "enabled": True,
        "sslRequired": "all",
        "registrationAllowed": False,
        "resetPasswordAllowed": False,
        "accessTokenLifespan": 90,
        "roles": {"realm": [{"name": "user"}, {"name": "admin"}]},
        "users": [
            {
                "id": "fixture-" + name,
                "username": name,
                "enabled": True,
                "emailVerified": True,
                "email": name + "@qualification.invalid",
                "firstName": "Synthetic",
                "lastName": name,
                "realmRoles": ["user", "admin", "offline_access"]
                if name == "administrator"
                else ["user", "offline_access"],
                "credentials": [
                    {"type": "password", "value": password, "temporary": False}
                ],
            }
            for name, password in users.items()
        ],
        "clients": [
            client("astral-frontend", client_secret, True),
            client("astral-agent-service", agent_secret),
        ],
        "scopeMappings": [
            {"client": "astral-frontend", "roles": ["user", "admin", "offline_access"]},
            {"client": "astral-agent-service", "roles": ["user", "admin"]},
        ],
        "clientScopes": [
            {
                "name": scope,
                "protocol": "openid-connect",
                "attributes": {"include.in.token.scope": "true"},
            }
            for scope in tool_scopes + ["profile", "email", "offline_access"]
        ]
        + [
            {
                "name": "basic",
                "protocol": "openid-connect",
                "attributes": {"include.in.token.scope": "false"},
                "protocolMappers": [
                    {
                        "name": "sub",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-sub-mapper",
                        "config": {
                            "access.token.claim": "true",
                            "introspection.token.claim": "true",
                        },
                    }
                ],
            },
            {
                "name": "roles",
                "protocol": "openid-connect",
                "attributes": {"include.in.token.scope": "true"},
                "protocolMappers": [
                    {
                        "name": "realm-roles",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-usermodel-realm-role-mapper",
                        "config": {
                            "claim.name": "realm_access.roles",
                            "jsonType.label": "String",
                            "multivalued": "true",
                            "access.token.claim": "true",
                            "id.token.claim": "false",
                        },
                    }
                ],
            },
        ],
    }
    document("keycloak/import/" + realm["realm"] + "-realm.json", realm, 1000)
    output(
        "keycloak/runtime.env",
        "\n".join(
            [
                "KC_DB=postgres",
                "KC_DB_URL=jdbc:postgresql://ad-bwq-iam-pg:5432/keycloak",
                "KC_DB_USERNAME=qualification",
                "KC_DB_PASSWORD=" + pg_password,
                "KC_HOSTNAME=https://ad-bwq-keycloak:8443",
                "KC_HTTPS_CERTIFICATE_FILE=/materials/server-cert.pem",
                "KC_HTTPS_CERTIFICATE_KEY_FILE=/materials/server-key.pem",
                "KC_HTTP_ENABLED=false",
            ]
        )
        + "\n",
    )
    output(
        "keycloak/postgres.env",
        "POSTGRES_USER=qualification\nPOSTGRES_DB=keycloak\nPOSTGRES_PASSWORD="
        + pg_password
        + "\n",
    )
    document("test-identities.json", {"classification": "synthetic", "users": users})

    dimensions = tuple(
        ResourceDimension(
            item.scope.removeprefix("tools:"),
            "count",
            "Synthetic bounded " + item.scope,
        )
        for item in SCOPE_BINDINGS
    )
    policy = PolicySpec(
        "astral-bwq-policy",
        "v1",
        dimensions,
        MachineSpec(
            "astral-bwq-worker",
            "ready",
            tuple(
                TransitionSpec(
                    item.transition, "ready", "ready", item.unit_cost(), item.capability
                )
                for item in SCOPE_BINDINGS
            ),
        ),
        max_lease_ttl_ns=600_000_000_000,
        receipt_ttl_ns=60_000_000_000,
        max_clock_uncertainty_ns=1_000_000_000,
        transfer_gap_window=8,
    )
    signer = Ed25519Signer.generate("ad-bwq-warden")
    operator = Ed25519Signer.generate("ad-bwq-operator")
    tenant, envelope = "bwq-" + ID, "envelope-" + ID
    manifest = ClusterManifest(
        tenant_id=tenant,
        envelope_id=envelope,
        config_epoch=1,
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        resources=dimensions,
        initial_budget=(10000,) * 6,
        wardens=(
            WardenManifest(
                warden_id="ad-bwq-warden",
                initial_share=(10000,) * 6,
                peer_endpoint="https://ad-bwq-warden:8443",
                client_endpoint="https://ad-bwq-warden:8443",
                keys=(ManifestPublicKey(signer.key_id, signer.public_key_bytes),),
                extensions={},
            ),
        ),
        policies=(policy,),
        extensions={
            "org.astraldeep.lets/purpose": "isolated synthetic backend-web qualification"
        },
    )
    manifest = replace(
        manifest,
        signatures=(
            ManifestSignature(
                operator.key_id, operator.sign(canonical_json(manifest.unsigned_dict()))
            ),
        ),
    )
    identity = SigningKey.generate()
    kid = "identity-" + bytes(identity.verify_key).hex()[:24]
    trust = {
        "key_id": operator.key_id,
        "public_key": b64url_encode(operator.public_key_bytes),
    }
    document("warden/trust/manifest.json", manifest.to_dict(), 10001)
    document("warden/trust/operator.json", trust, 10001)
    document(
        "warden/trust/identity-keys.json",
        {
            "keys": [
                {"kid": kid, "public_key": b64url_encode(bytes(identity.verify_key))}
            ]
        },
        10001,
    )
    document(
        "warden/signer/signer.json",
        {"key_id": signer.key_id, "public_key": b64url_encode(signer.public_key_bytes)},
        10001,
    )
    output("warden/signer/warden.seed", signer.seed_bytes, 10001)
    output(
        "warden/signer/signer_helper.py",
        (lets_source / "deploy/production/acceptance/signer_helper.py").read_bytes(),
        10001,
        True,
    )
    document("app/lets-manifest.json", manifest.to_dict())
    document(
        "app/lets-operator-trust.json",
        {
            "api_version": "astraldeep.lets-operator-trust/v1",
            "threshold": 1,
            "keys": [{"algorithm": "Ed25519", **trust}],
        },
    )
    output("app/identity.seed", bytes(identity))
    livekit_key, livekit_secret = (
        "bwq_" + secrets.token_hex(12),
        secrets.token_urlsafe(48),
    )
    output(
        "livekit/runtime.env",
        "LIVEKIT_KEYS=" + livekit_key + ": " + livekit_secret + "\n",
    )
    initializer = (
        lets_source / "deploy/production/acceptance/init_node.py"
    ).read_text()
    replacements = {
        "https://identity.production-acceptance": "https://identity.astral-bwq.invalid",
        "lets-production-acceptance": "astral-bwq",
        "staged = _object(CONFIG)": 'staged = _object(CONFIG)\n    staged.setdefault("peer_endpoints", {})',
    }
    for before, after in replacements.items():
        if initializer.count(before) != 1:
            raise ValueError("pinned LETS initializer contract changed")
        initializer = initializer.replace(before, after)
    output("init_warden.py", initializer, 10001)
    runtime = {
        "ASTRAL_ENV": "production",
        "USE_MOCK_AUTH": "false",
        "LETS_MODE": "enforce",
        "FF_LETS_EXTERNAL_WARDEN": "true",
        "KEYCLOAK_AUTHORITY": "https://ad-bwq-keycloak:8443/realms/" + realm["realm"],
        "KEYCLOAK_CLIENT_ID": "astral-frontend",
        "KEYCLOAK_CLIENT_SECRET": client_secret,
        "AGENT_SERVICE_CLIENT_ID": "astral-agent-service",
        "AGENT_SERVICE_CLIENT_SECRET": agent_secret,
        "PUBLIC_BASE_URL": "https://ad-bwq-web:9443",
        "BACKEND_PUBLIC_URL": "https://ad-bwq-web:9443",
        "SSL_CERT_FILE": "/run/astral-bwq/ca-bundle.pem",
        "REQUESTS_CA_BUNDLE": "/run/astral-bwq/ca-bundle.pem",
        "LETS_WARDEN_URL": "https://ad-bwq-warden:8443",
        "LETS_TENANT_ID": tenant,
        "LETS_ENVELOPE_ID": envelope,
        "LETS_CA_BUNDLE": "/run/astral-bwq/lets-ca.pem",
        "LETS_CLIENT_CERT_FILE": "/run/astral-bwq/lets-client-cert.pem",
        "LETS_CLIENT_KEY_FILE": "/run/astral-bwq/lets-client-key.pem",
        "LETS_SIGNED_TRUST_MANIFEST": "/run/astral-bwq/lets-manifest.json",
        "LETS_MANIFEST_OPERATOR_KEYS_FILE": "/run/astral-bwq/lets-operator-trust.json",
        "LETS_IDENTITY_SEED_FILE": "/run/astral-bwq/identity.seed",
        "LETS_IDENTITY_KID": kid,
        "LETS_IDENTITY_ISSUER": "https://identity.astral-bwq.invalid",
        "LETS_IDENTITY_AUDIENCE": "astral-bwq",
        "LETS_IDENTITY_SUBJECT": "astral-bwq-orchestrator",
        "LETS_IDENTITY_SCOPES": "lets.lease.issue lets.lease.manage lets.branch.revoke lets.metrics.read lets.audit.read",
        "LETS_POLICY_DIGEST": policy.digest,
        "LETS_MACHINE_DIGEST": policy.machine.digest,
        "LETS_DEFAULT_ALLOCATION": "100,100,100,100,100,100",
        "LETS_DEFAULT_TTL_SECONDS": "300",
        "LETS_REQUEST_TIMEOUT_SECONDS": "10",
        "LETS_REQUEST_ATTEMPTS": "2",
        "LETS_EXECUTOR_INSTANCE_ID": "bwq-executor-" + ID,
        "LETS_EXECUTOR_DB_ROOT": "/var/lib/astral-lets-replay",
        "LETS_EXECUTOR_AUTHORITY_ROOT": "/var/lib/astral-lets-authority",
        "FF_CONVERSATIONAL_VOICE": "true",
        "VOICE_COORDINATOR_REPLICA_ID": "bwq-coordinator-" + ID,
        "VOICE_WATCH_BRIDGE_PUBLIC_URL": "wss://ad-bwq-web:9443/api/voice/watch-bridge",
        "LIVEKIT_INTERNAL_URL": "https://ad-bwq-livekit:9444",
        "LIVEKIT_PUBLIC_URL": "wss://ad-bwq-livekit:9444",
        "LIVEKIT_API_KEY": livekit_key,
        "LIVEKIT_API_SECRET": livekit_secret,
    }
    for key in (
        "CREDENTIAL_ENCRYPTION_KEY",
        "WEB_SESSION_ENC_KEY",
        "OFFLINE_GRANT_ENC_KEY",
    ):
        runtime[key] = Fernet.generate_key().decode("ascii")
    for key in (
        "AUDIT_HMAC_SECRET",
        "MEMORY_HMAC_KEY",
        "TXN_TOKEN_KEY",
        "VOICE_CONTROL_SECRET",
        "VOICE_UI_BINDING_SECRET",
        "WEB_SESSION_SECRET",
    ):
        runtime[key] = secrets.token_urlsafe(48)
    runtime["VOICE_SPEECH_BACKEND"] = "llm_factory"
    output(
        "runtime-fragment.env",
        "".join(key + "=" + value + "\n" for key, value in runtime.items()),
    )
    output(
        "proxy/nginx.conf",
        """pid /tmp/nginx.pid;
events {}
http {
  access_log off;
  error_log /dev/stderr warn;
  resolver 127.0.0.11 valid=10s ipv6=off;
  map $http_upgrade $connection_upgrade { default upgrade; '' close; }
  server {
    listen 9443 ssl;
    server_name ad-bwq-web;
    ssl_certificate /materials/server-cert.pem;
    ssl_certificate_key /materials/server-key.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    location = /api/voice/watch-bridge {
      set $worker http://ad-bwq-voice:7890;
      proxy_pass $worker;
      proxy_http_version 1.1;
      proxy_set_header Host $http_host;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $connection_upgrade;
      proxy_set_header Sec-WebSocket-Extensions "";
      proxy_buffering off;
      proxy_read_timeout 3600s;
    }
    location / {
      set $backend http://ad-bwq-"""
        + ID
        + """-app:8001;
      proxy_pass $backend;
      proxy_http_version 1.1;
      # Application category limits remain authoritative for streamed uploads.
      client_max_body_size 0;
      proxy_request_buffering off;
      proxy_buffering off;
      proxy_set_header Host $http_host;
      proxy_set_header X-Forwarded-Proto https;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $connection_upgrade;
      proxy_read_timeout 3600s;
    }
  }
  server {
    listen 9444 ssl;
    server_name ad-bwq-livekit;
    ssl_certificate /materials/livekit-cert.pem;
    ssl_certificate_key /materials/livekit-key.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    location / {
      set $livekit http://ad-bwq-livekit-node:7880;
      proxy_pass $livekit;
      proxy_http_version 1.1;
      proxy_set_header Host $http_host;
      proxy_set_header X-Forwarded-Proto https;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $connection_upgrade;
      proxy_buffering off;
      proxy_read_timeout 3600s;
    }
  }
}
""",
    )
    document(
        "voice-profile.json",
        {
            "schema_version": 1,
            "status": "operator-input-required",
            "qualification_id": ID,
            "speech_backend": "llm_factory",
            "worker_environment": {
                "ASTRAL_ENV": "production",
                "ASTRAL_VOICE_CONTROL_URL": "wss://ad-bwq-web:9443/api/voice/worker-control",
                "VOICE_WORKER_IDENTITY": "bwq-voice-" + ID,
                "VOICE_WORKER_MAX_SESSIONS": "2",
            },
            "private_control_secret_source": "runtime-fragment.env:VOICE_CONTROL_SECRET",
            "required_operator_inputs": [
                "immutable voice image built from candidate Dockerfile.voice",
                "exact nonzero worker closure digest",
                "approved remote speech endpoint and credential",
                "browser trusted synthetic IAM CA",
            ],
            "qualification_claim": "none: worker/media/browser acceptance remains required",
        },
    )
    for path in ROOT.rglob("*"):
        if path.is_dir():
            path.chmod(0o700)
            owner = (
                10001
                if path.relative_to(ROOT).parts[0] in {"warden", "voice"}
                else 1000
                if path.relative_to(ROOT).parts[0] == "keycloak"
                else operator_uid
            )
            os.chown(path, owner, owner)
    document(
        "public-summary.json",
        {
            "schema_version": 1,
            "qualification_id": ID,
            "classification": "synthetic",
            "manifest_digest": manifest.digest,
            "policy_digest": policy.digest,
            "machine_digest": policy.machine.digest,
            "keycloak_realm": realm["realm"],
            "keycloak_image": "quay.io/keycloak/keycloak@sha256:4883630ef9db14031cde3e60700c9a9a8eaf1b5c24db1589d6a2d43de38ba2a9",
            "lets_commit": "6245189920c686353c4ced7a208d56ec266f745c",
            "expires_within_hours": 48,
            "public_certificate_sha256": {
                name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                for name in (
                    "app/iam-ca.pem",
                    "app/lets-ca.pem",
                    "proxy/server-cert.pem",
                    "proxy/livekit-cert.pem",
                    "livekit/turn-cert.pem",
                )
            },
            "failure_domain": "separate logical stores only; physical independence is not established",
            "voice_status": "operator-input-required",
        },
    )
    return json.loads((ROOT / "public-summary.json").read_text())


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--qualification-id", required=True)
    parser.add_argument("--lets-source", required=True, type=Path)
    parser.add_argument("--operator-uid", type=int, default=0)
    args = parser.parse_args()
    result = generate_materials(
        args.output,
        args.qualification_id,
        args.lets_source,
        operator_uid=args.operator_uid,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
