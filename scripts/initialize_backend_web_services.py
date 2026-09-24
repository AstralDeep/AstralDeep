"""Starts fresh, private synthetic Keycloak/Postgres/LiveKit/proxy services on an
isolated Docker host for backend/web qualification, using only names scoped to the
caller's fresh ID; exercised by test_backend_web_services.py.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

KEYCLOAK_IMAGE = "quay.io/keycloak/keycloak@sha256:4883630ef9db14031cde3e60700c9a9a8eaf1b5c24db1589d6a2d43de38ba2a9"
POSTGRES_IMAGE = (
    "postgres@sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73"
)
PROXY_IMAGE = (
    "nginx@sha256:ef8676b33d681f272ba429b27658bdd7e640963279714c96bddf1dc76307f7b6"
)
LIVEKIT_IMAGE = "livekit/livekit-server:v1.13.5@sha256:3497163e15c48fef6e7830c78716f9e9d5edc28abf7aa90b61c86e93bbc306b1"
LETS_COMMIT = "6245189920c686353c4ced7a208d56ec266f745c"
LABEL = "com.astraldeep.backend-web-services"
VOLUMES = (
    "iam-pg",
    "iam-data",
    "warden-state",
    "warden-config",
    "warden-authority",
    "warden-audit",
)


def command(
    arguments: list[str], *, missing_ok: bool = False
) -> subprocess.CompletedProcess:
    result = subprocess.run(arguments, capture_output=True, timeout=180, check=False)
    if result.returncode and not missing_ok:
        raise RuntimeError(
            f"isolated service operation failed (exit {result.returncode})"
        )
    return result


def immutable_image(value: str) -> str:
    if not re.fullmatch(
        r"(?:[a-zA-Z0-9][a-zA-Z0-9._:/-]*@)?sha256:[0-9a-f]{64}", value
    ):
        raise ValueError("every image must use an immutable SHA-256 identity")
    return value


def private_directory(path: Path, *, empty: bool = False) -> None:
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_dir()
        or path.resolve() != path
    ):
        raise ValueError("directory must be canonical, absolute and real")
    if path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid():
        raise ValueError("directory must be private and owned by the operator")
    if empty and any(path.iterdir()):
        raise ValueError("refusing to reuse a material directory")


def _require_posix() -> None:
    if os.name != "posix":
        raise ValueError("service initialization requires a POSIX Docker host")


def livekit_address(details: dict, qualification_id: str) -> str:
    if (
        details.get("Name") != "ad-bwq-services-" + qualification_id
        or details.get("Labels", {}).get(LABEL) != qualification_id
        or details.get("Driver") != "bridge"
    ):
        raise ValueError("LiveKit requires the exact owned bridge network")
    networks = [
        ipaddress.ip_network(item["Subnet"])
        for item in details.get("IPAM", {}).get("Config", [])
    ]
    ipv4 = [network for network in networks if network.version == 4]
    if (
        len(ipv4) != 1
        or ipv4[0].num_addresses < 16
        or not any(
            ipv4[0].subnet_of(ipaddress.ip_network(block))
            for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        )
    ):
        raise ValueError("LiveKit requires one bounded private IPv4 bridge")
    candidate = ipv4[0].broadcast_address - 1
    reserved = {str(item.get("Gateway", "")) for item in details["IPAM"]["Config"]}
    reserved.update(
        value["IPv4Address"].split("/")[0]
        for value in details.get("Containers", {}).values()
    )
    if str(candidate) in reserved:
        raise ValueError("reserved LiveKit address is already allocated")
    return str(candidate)


def livekit_profile(address: str) -> dict:
    ip = ipaddress.ip_address(address)
    if ip.version != 4 or not any(
        ip in ipaddress.ip_network(block)
        for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ):
        raise ValueError("LiveKit address must be private IPv4")
    return {
        "port": 7880,
        "bind_addresses": ["0.0.0.0"],
        "development": False,
        "rtc": {
            "node_ip": address,
            "tcp_port": 7881,
            "port_range_start": 50000,
            "port_range_end": 50099,
            "use_external_ip": False,
            "advertise_internal_ip": True,
            "enable_loopback_candidate": False,
            "allow_tcp_fallback": True,
            "data_channel_max_buffered_amount": 1048576,
        },
        "room": {
            "auto_create": False,
            "empty_timeout": 300,
            "departure_timeout": 20,
            "max_participants": 16,
            "enabled_codecs": [{"mime": "audio/opus"}],
        },
        "turn": {
            "enabled": True,
            "domain": "ad-bwq-turn",
            "udp_port": 3478,
            "tls_port": 443,
            "external_tls": False,
            "cert_file": "/materials/turn-cert.pem",
            "key_file": "/materials/turn-key.pem",
            "relay_range_start": 51000,
            "relay_range_end": 51099,
            "ttl_seconds": 300,
            "allow_restricted_peer_cidrs": [address + "/32"],
        },
        "logging": {
            "level": "warn",
            "pion_level": "error",
            "json": True,
            "sample": True,
        },
        "enable_data_tracks": True,
    }


def wait_for_iam_postgres(container: str) -> None:
    for _ in range(60):
        if (
            command(
                [
                    "docker",
                    "exec",
                    container,
                    "pg_isready",
                    "-U",
                    "qualification",
                    "-d",
                    "keycloak",
                ],
                missing_ok=True,
            ).returncode
            == 0
        ):
            return
        time.sleep(1)
    raise RuntimeError("isolated IAM PostgreSQL did not become ready")


def read_speech_environment(path: Path) -> dict[str, str]:
    if (
        not path.is_absolute()
        or path.resolve() != path
        or path.is_symlink()
        or not path.is_file()
    ):
        raise ValueError("speech input must be a canonical private file")
    if path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid():
        raise ValueError("speech input must be private and owned by the operator")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if (
            not separator
            or key in values
            or key not in {"VOICE_SPEECH_BASE_URL", "VOICE_SPEECH_API_KEY"}
            or not value
            or value != value.strip()
            or "\x00" in value
            or value.startswith(("'", '"'))
        ):
            raise ValueError(
                "speech input must contain only two literal reviewed settings"
            )
        values[key] = value
    if set(values) != {"VOICE_SPEECH_BASE_URL", "VOICE_SPEECH_API_KEY"}:
        raise ValueError("both speech settings are required")
    url = urlsplit(values["VOICE_SPEECH_BASE_URL"])
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.fragment
    ):
        raise ValueError("speech endpoint requires HTTPS without embedded credentials")
    if len(values["VOICE_SPEECH_API_KEY"].encode()) > 8192:
        raise ValueError("speech credential exceeds the worker bound")
    return values


def initialize(
    *,
    qualification_id: str,
    source: Path,
    material_root: Path,
    candidate_commit: str,
    candidate_image: str,
    lets_image: str,
    policy_root: Path,
    policy_commit: str,
    materializer_image: str,
    voice_image: str | None = None,
    voice_closure_sha256: str | None = None,
    speech_runtime_env: Path | None = None,
) -> dict:
    _require_posix()
    if not re.fullmatch(r"[0-9a-f]{32}", qualification_id):
        raise ValueError("invalid qualification ID")
    if not re.fullmatch(r"[0-9a-f]{40}", candidate_commit):
        raise ValueError("invalid candidate commit")
    if not re.fullmatch(r"[0-9a-f]{40}", policy_commit):
        raise ValueError("invalid policy commit")
    voice_inputs = (voice_image, voice_closure_sha256, speech_runtime_env)
    if any(voice_inputs) and not all(voice_inputs):
        raise ValueError(
            "voice image, closure and private speech inputs must be supplied together"
        )
    speech = None
    if all(voice_inputs):
        if (
            not re.fullmatch(r"[0-9a-f]{64}", voice_closure_sha256)
            or voice_closure_sha256 == "0" * 64
        ):
            raise ValueError("voice requires the exact nonzero worker closure digest")
        speech = read_speech_environment(speech_runtime_env)
    private_directory(material_root, empty=True)
    for root in (source, policy_root):
        if not root.is_absolute() or root.resolve() != root or not root.is_dir():
            raise ValueError("source must be an absolute canonical directory")
        if material_root.is_relative_to(root):
            raise ValueError("private materials must be outside both source checkouts")
    for checkout, expected in (
        (source, candidate_commit),
        (source / "components/LETS", LETS_COMMIT),
        (policy_root, policy_commit),
        (policy_root / "components/LETS", LETS_COMMIT),
    ):
        actual = (
            command(["git", "-C", str(checkout), "rev-parse", "HEAD"])
            .stdout.decode()
            .strip()
        )
        changed = command(
            [
                "git",
                "-C",
                str(checkout),
                "status",
                "--porcelain",
                "--untracked-files=no",
            ]
        ).stdout
        if actual != expected or changed:
            raise ValueError("source checkout is dirty or does not match the candidate")
    images = [
        immutable_image(value)
        for value in (
            candidate_image,
            lets_image,
            materializer_image,
            KEYCLOAK_IMAGE,
            POSTGRES_IMAGE,
            PROXY_IMAGE,
            LIVEKIT_IMAGE,
        )
    ]
    if voice_image:
        images.append(immutable_image(voice_image))
    image_ids = {
        value: json.loads(command(["docker", "image", "inspect", value]).stdout)[0][
            "Id"
        ]
        for value in images
    }
    prefix = "ad-bwq-services-" + qualification_id
    network = prefix
    label = LABEL + "=" + qualification_id
    resources = [("network", network)]
    resources += [("volume", prefix + "-" + suffix) for suffix in VOLUMES]
    resources += [
        ("container", prefix + "-" + suffix)
        for suffix in ("iam-pg", "keycloak", "warden", "proxy", "livekit")
    ]
    if voice_image:
        resources.append(("container", prefix + "-voice"))
    # All names checked before any resource is created
    for kind, name in resources:
        if command(["docker", kind, "inspect", name], missing_ok=True).returncode == 0:
            raise ValueError("refusing to reuse an existing service namespace")
    command(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--cpus",
            "1",
            "--memory",
            "512m",
            "--pids-limit",
            "128",
            "--mount",
            f"type=bind,src={policy_root},dst=/policy,readonly",
            "--mount",
            f"type=bind,src={material_root},dst=/materials",
            "-e",
            "PYTHONPATH=/policy/backend:/policy/components/LETS/src",
            materializer_image,
            "python",
            "/policy/scripts/backend_web_service_materials.py",
            "--output",
            "/materials",
            "--qualification-id",
            qualification_id,
            "--lets-source",
            "/policy/components/LETS",
            "--operator-uid",
            str(os.getuid()),
        ]
    )
    public = json.loads((material_root / "public-summary.json").read_text())
    if (
        public["qualification_id"] != qualification_id
        or public["classification"] != "synthetic"
    ):
        raise ValueError("material marker mismatch")
    command(["docker", "network", "create", "--label", label, network])
    address = livekit_address(
        json.loads(command(["docker", "network", "inspect", network]).stdout)[0],
        qualification_id,
    )
    profile = livekit_profile(address)
    profile_path = material_root / "livekit/config.json"
    with profile_path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(profile, sort_keys=True, indent=2) + "\n")
    profile_path.chmod(0o600)
    for suffix in VOLUMES:
        command(["docker", "volume", "create", "--label", label, prefix + "-" + suffix])

    def bind(relative: str, target: str) -> list[str]:
        return [
            "--mount",
            f"type=bind,src={material_root / relative},dst={target},readonly",
        ]

    def start(suffix: str, alias: str) -> list[str]:
        return [
            "docker",
            "run",
            "-d",
            "--name",
            prefix + "-" + suffix,
            "--label",
            label,
            "--network",
            network,
            "--network-alias",
            alias,
            "--cpus",
            "1.5",
            "--memory",
            "1g",
            "--pids-limit",
            "256",
            "--security-opt",
            "no-new-privileges:true",
        ]

    command(
        [
            *start("iam-pg", "ad-bwq-iam-pg"),
            "--env-file",
            str(material_root / "keycloak/postgres.env"),
            "-v",
            prefix + "-iam-pg:/var/lib/postgresql/data",
            POSTGRES_IMAGE,
        ]
    )
    wait_for_iam_postgres(prefix + "-iam-pg")
    command(
        [
            *start("keycloak", "ad-bwq-keycloak"),
            "--env-file",
            str(material_root / "keycloak/runtime.env"),
            "-v",
            prefix + "-iam-data:/opt/keycloak/data",
            *bind("keycloak", "/materials"),
            *bind("keycloak/import", "/opt/keycloak/data/import"),
            KEYCLOAK_IMAGE,
            "start",
            "--import-realm",
        ]
    )
    state = [
        "-v",
        prefix + "-warden-state:/var/lib/lets",
        "-v",
        prefix + "-warden-config:/var/lib/lets-config",
        "-v",
        prefix + "-warden-authority:/var/lib/lets-authority",
        "-v",
        prefix + "-warden-audit:/var/lib/lets-audit",
    ]
    command(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            *state,
            lets_image,
            "python",
            "-c",
            "import os; paths=['/var/lib/lets','/var/lib/lets-config','/var/lib/lets-authority','/var/lib/lets-audit']; [(os.chown(p,10001,10001),os.chmod(p,0o700)) for p in paths]",
        ]
    )
    trust = [
        *bind("warden/trust", "/etc/lets/trust"),
        *bind("warden/signer", "/run/lets-signer"),
    ]
    command(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-e",
            "LETS_ACCEPTANCE_WARDEN_ID=ad-bwq-warden",
            *state,
            *trust,
            *bind("init_warden.py", "/init_warden.py"),
            lets_image,
            "python",
            "/init_warden.py",
        ]
    )
    command(
        [
            *start("warden", "ad-bwq-warden"),
            "--read-only",
            "--cap-drop",
            "ALL",
            "--init",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
            *state,
            "--mount",
            "type=volume,src="
            + prefix
            + "-warden-config,dst=/var/lib/lets/config.json,volume-subpath=config.json,readonly",
            *trust,
            *bind("warden/tls", "/run/lets-pki"),
            lets_image,
            "lets",
            "--config",
            "/var/lib/lets/config.json",
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            "8443",
            "--production",
            "--tls-cert",
            "/run/lets-pki/server-cert.pem",
            "--tls-key",
            "/run/lets-pki/server-key.pem",
            "--client-ca",
            "/run/lets-pki/ca.pem",
            "--peer-ca",
            "/run/lets-pki/ca.pem",
            "--peer-cert",
            "/run/lets-pki/peer-cert.pem",
            "--peer-key",
            "/run/lets-pki/peer-key.pem",
            "--log-level",
            "warning",
            "--limit-concurrency",
            "64",
            "--backlog",
            "128",
            "--peer-request-timeout-seconds",
            "60",
            "--timeout-keep-alive",
            "5",
            "--timeout-graceful-shutdown",
            "30",
        ]
    )
    command(
        [
            *start("proxy", "ad-bwq-web"),
            "--network-alias",
            "ad-bwq-livekit",
            "--read-only",
            "--user",
            f"{os.getuid()}:{os.getuid()}",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
            "--tmpfs",
            "/var/cache/nginx:rw,noexec,nosuid,nodev,size=32m,mode=1777",
            *bind("proxy", "/materials"),
            "--entrypoint",
            "nginx",
            PROXY_IMAGE,
            "-c",
            "/materials/nginx.conf",
            "-g",
            "daemon off;",
        ]
    )
    command(
        [
            *start("livekit", "ad-bwq-livekit-node"),
            "--network-alias",
            "ad-bwq-turn",
            "--ip",
            address,
            "--read-only",
            "--user",
            f"{os.getuid()}:{os.getuid()}",
            "--cap-drop",
            "ALL",
            "--sysctl",
            "net.ipv4.ip_unprivileged_port_start=0",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
            "--env-file",
            str(material_root / "livekit/runtime.env"),
            *bind("livekit", "/materials"),
            LIVEKIT_IMAGE,
            "--config",
            "/materials/config.json",
        ]
    )
    if speech is not None:
        fragment = dict(
            line.split("=", 1)
            for line in (material_root / "runtime-fragment.env")
            .read_text()
            .splitlines()
        )
        fragment["VOICE_WORKER_CLOSURE_SHA256"] = voice_closure_sha256
        (material_root / "runtime-fragment.env").write_text(
            "".join(key + "=" + value + "\n" for key, value in fragment.items()),
            encoding="utf-8",
        )
        worker = {
            "ASTRAL_ENV": "production",
            "ASTRAL_VOICE_CONTROL_URL": "wss://ad-bwq-web:9443/api/voice/worker-control",
            "VOICE_CONTROL_SECRET": fragment["VOICE_CONTROL_SECRET"],
            "VOICE_WORKER_IDENTITY": "bwq-voice-" + qualification_id,
            "VOICE_WORKER_MAX_SESSIONS": "2",
            "VOICE_WORKER_CLOSURE_SHA256": voice_closure_sha256,
            "SSL_CERT_FILE": "/run/astral-bwq/ca-bundle.pem",
            **speech,
        }
        private = material_root / "voice-runtime.env"
        with private.open("x", encoding="utf-8") as stream:
            stream.write(
                "".join(key + "=" + value + "\n" for key, value in worker.items())
            )
        private.chmod(0o600)
        command(
            [
                *start("voice", "ad-bwq-voice"),
                "--read-only",
                "--cap-drop",
                "ALL",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777",
                "--env-file",
                str(private),
                *bind("voice", "/run/astral-bwq"),
                *bind("voice/ca-bundle.pem", "/etc/ssl/certs/ca-certificates.crt"),
                voice_image,
            ]
        )
    readiness_result = command(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            "--label",
            label,
            "--user",
            f"{os.getuid()}:{os.getuid()}",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--cpus",
            "1",
            "--memory",
            "512m",
            "--pids-limit",
            "128",
            "--entrypoint",
            "python3",
            "--mount",
            f"type=bind,src={policy_root / 'scripts/wait_backend_web_services.py'},dst=/wait.py,readonly",
            *bind("app", "/materials"),
            materializer_image,
            "/wait.py",
            "--material-root",
            "/materials",
            "--qualification-id",
            qualification_id,
        ]
    )
    readiness = json.loads(readiness_result.stdout)
    if (
        readiness.get("qualification_id") != qualification_id
        or readiness.get("status") != "infrastructure-ready"
        or readiness.get("application_acceptance") is not False
    ):
        raise ValueError("isolated infrastructure observation mismatch")
    report = {
        "schema_version": 1,
        "qualification_id": qualification_id,
        "classification": "synthetic",
        "candidate_commit": candidate_commit,
        "candidate_image": candidate_image,
        "policy_commit": policy_commit,
        "materializer_image": materializer_image,
        "image_ids": image_ids,
        "service_network": network,
        "material_root": str(material_root / "app"),
        "runtime_fragment": str(material_root / "runtime-fragment.env"),
        "status": "started-acceptance-pending",
        "infrastructure": readiness,
        "web_origin": "https://ad-bwq-web:9443",
        "proxy_upstream": "ad-bwq-" + qualification_id + "-app:8001",
        "proxy_status": "started; TLS and upstream acceptance pending",
        "livekit": {
            "image": LIVEKIT_IMAGE,
            "status": "started-media-acceptance-pending",
            "signaling_url": "wss://ad-bwq-livekit:9444",
            "turn_tls": "ad-bwq-turn:443",
            "turn_udp": address + ":3478",
            "node_ipv4": address,
            "profile_sha256": hashlib.sha256(profile_path.read_bytes()).hexdigest(),
            "scope": "private bridge; external ingress and device network reachability are not established",
        },
        "voice": {
            "enabled": True,
            "backend": "llm_factory",
            "image": voice_image,
            "closure_sha256": voice_closure_sha256,
            "status": "worker-started-acceptance-pending"
            if voice_image
            else "operator-input-required",
        },
        "materials": public,
        "source_sha256": {
            name: hashlib.sha256(
                (policy_root / "scripts" / name).read_bytes()
            ).hexdigest()
            for name in (
                "initialize_backend_web_services.py",
                "backend_web_service_materials.py",
                "wait_backend_web_services.py",
            )
        },
    }
    output = material_root / "services-evidence.json"
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, sort_keys=True, indent=2) + "\n")
    output.chmod(0o600)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualification-id", required=True)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--material-root", required=True, type=Path)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--lets-image", required=True)
    parser.add_argument("--policy-root", required=True, type=Path)
    parser.add_argument("--policy-commit", required=True)
    parser.add_argument("--materializer-image", required=True)
    parser.add_argument("--voice-image")
    parser.add_argument("--voice-closure-sha256")
    parser.add_argument("--speech-runtime-env", type=Path)
    print(json.dumps(initialize(**vars(parser.parse_args())), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
