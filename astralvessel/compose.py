# Turns deployment.toml into a Docker Compose file: the personal node, the egress, and one confined sandbox per agent.
# Nothing in the running system touches Docker; the operator runs `docker compose up` on what this writes.

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets as tokens
from importlib.metadata import version
from pathlib import Path
from typing import Any

from .deployment import Deployment, egress_token, sandbox_token, size_bytes
from .executor import agent_token, executor_token

NODE_DATA = "/node-data"
NODE_INTERNAL_PORT = 8001
NODE_SECRETS = ("WEB_SESSION_ENC_KEY", "CREDENTIAL_ENCRYPTION_KEY", "AUDIT_HMAC_SECRET", "AGENT_API_KEY")
REGISTRY = "ghcr.io/astraldeep"
SANDBOX_PORT = 8710
EXECUTOR_PORT = 8700
AGENT_TOKENS_PATH = "/run/secrets/executor-agents.json"
HARDENED: dict[str, Any] = {
    "read_only": True,
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges:true"],
    "restart": "unless-stopped",
    "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
}


def _env_ref(value: str) -> str:
    # "env:NAME" stays a reference that Compose fills from the operator's environment, never a copied secret.
    if value.startswith("env:"):
        name = value[4:]
        return f"${{{name}:?set {name} in the environment or .env}}"
    return value


def _source(deployment: Deployment, source: str) -> str:
    # "volume:NAME" is an existing Docker volume (for data another stack writes); a relative path is relative to
    # the deployment file, like an agent's package path.
    if source.startswith("volume:"):
        return source[7:]
    if source.startswith("env:") or os.path.isabs(source):
        return _env_ref(source)
    return str((deployment.path.parent / source).resolve())


def _image(repo_root: Path | None, name: str = "astralvessel", project: str = "astralvessel",
           contexts: dict[str, str] | None = None) -> dict[str, Any]:
    # From a checkout, build the images locally, tagged per project; from an installed package, pull the released ones.
    if repo_root is None:
        return {"image": published(name)}
    local = project + name.removeprefix("astralvessel")
    build: dict[str, Any] = {"context": str(repo_root), "dockerfile": "Dockerfile"}
    if contexts:
        build["additional_contexts"] = contexts
    return {"image": f"{local}:local", "build": build}


def _artifacts(deployment: Deployment, repo_root: Path | None) -> dict[str, str]:
    if repo_root is None:
        return {}
    folder = deployment.executor.kernel_artifacts
    if not folder:
        raise SystemExit("building the executor image needs the pinned kernel wheel: set [executor] kernel_artifacts "
                         "in the deployment file to the folder that holds it")
    path = (deployment.path.parent / folder).resolve()
    if not any(path.glob("astral_kernel-*.whl")):
        raise SystemExit(f"[executor] kernel_artifacts names {path}, which holds no astral_kernel wheel")
    return {"kernel-artifacts": str(path)}


def published(name: str) -> str:
    return f"{REGISTRY}/{name}:{version('astralvessel')}"


def node_environment(deployment: Deployment) -> dict[str, str]:
    owner, node = deployment.owner, deployment.node
    public = (node.public_url or f"http://127.0.0.1:{node.port}").rstrip("/")
    env = {
        "ASTRAL_NODE_PROFILE": "personal",
        "ASTRAL_NODE_DATA_DIR": f"{NODE_DATA}/node",
        "KEYCLOAK_AUTHORITY": owner.issuer.rstrip("/"),
        "KEYCLOAK_CLIENT_ID": owner.client_id,
        "ATTACHMENT_UPLOAD_ROOT": f"{NODE_DATA}/node/blobs",
        "PERSONAL_AGENT_ARTIFACT_ROOT": f"{NODE_DATA}/node/agents",
        "ORCHESTRATOR_HOST": "0.0.0.0",
        "ORCHESTRATOR_PORT": str(NODE_INTERNAL_PORT),
        "PUBLIC_BASE_URL": public,
        "BACKEND_PUBLIC_URL": public,
        "FF_A2A_SERVER": "true",
        "PYTHON_DOTENV_DISABLED": "1",
    }
    if owner.allowed_azp:
        env["KEYCLOAK_ALLOWED_AZP"] = ",".join(owner.allowed_azp)
    if owner.accepted_issuers:
        env["ASTRAL_ACCEPTED_ISSUERS"] = ",".join(i.rstrip("/") for i in owner.accepted_issuers)
    if owner.owner_id:
        env["ASTRAL_NODE_OWNER"] = owner.owner_id
    merged = {**node.environment, **env}
    if merged.get("ASTRAL_NODE_OWNER") != owner.owner_id:
        raise SystemExit("ASTRAL_NODE_OWNER in [node.environment] must equal the owner the deployment's subject names; "
                         "set [owner] subject instead")
    if deployment.agents:
        merged["ASTRAL_VESSEL_EXECUTOR_URL"] = f"http://{node.front_address}:{EXECUTOR_PORT}"
        merged["ASTRAL_VESSEL_EXECUTOR_AGENT_TOKENS_FILE"] = AGENT_TOKENS_PATH
    return merged


def generate(deployment: Deployment, *, repo_root: Path | None) -> dict[str, Any]:
    project = deployment.node.project
    gateway = deployment.egress.upstream_url is not None
    networks: dict[str, Any] = {
        "front": {"internal": True, "ipam": {"config": [{"subnet": deployment.node.front_subnet,
                                                         "ip_range": str(deployment.node.front_dynamic_range)}]}},
        "outside": {},
    }
    node_networks: dict[str, Any] = {"front": {}, "outside": {}}
    services: dict[str, Any] = {}
    if gateway:
        networks["egress"] = {"internal": True}
        node_networks["egress"] = {}
        services["egress"] = {
            **HARDENED, **_image(repo_root and repo_root / "egress", "astralvessel-egress", project),
            "environment": {"LLM_BASE_URL": _env_ref(deployment.egress.upstream_url or ""),
                            "LLM_API_KEY": _env_ref(deployment.egress.upstream_key)},
            "env_file": [".generated/egress.env"],
            "networks": ["egress", "outside"], "mem_limit": "512m", "pids_limit": 128}
    services["node"] = {
        "image": deployment.node.image,
        "restart": HARDENED["restart"], "logging": HARDENED["logging"],
        "cap_drop": HARDENED["cap_drop"], "security_opt": HARDENED["security_opt"],
        "environment": node_environment(deployment),
        "env_file": [".generated/node.env", *([".generated/node-executor.env"] if deployment.agents else [])],
        "volumes": [f"node_data:{NODE_DATA}",
                    *([f"./.generated/executor-agents.json:{AGENT_TOKENS_PATH}:ro"] if deployment.agents else [])],
        "ports": [f"{deployment.node.bind}:{deployment.node.port}:{NODE_INTERNAL_PORT}"],
        "networks": node_networks,
        "pids_limit": 2048,
        "healthcheck": {"test": ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen("
                                f"'http://127.0.0.1:{NODE_INTERNAL_PORT}/readyz', timeout=4)"],
                        "interval": "10s", "timeout": "6s", "retries": 18, "start_period": "60s"},
        "labels": {"astralvessel.role": "node"},
    }
    for agent_id, agent in deployment.agents.items():
        tools = f"tools-{agent_id}"
        networks[tools] = {"internal": True}
        mounts = [f"{agent.host_package}:/agent:ro"] + [
            f"{_source(deployment, m.source)}:{m.target}:{'ro' if m.read_only else 'rw'}" for m in agent.profile.mounts]
        dockerfile = agent.package.manifest.sandbox.dockerfile
        if dockerfile:
            # The agent's own image, built FROM the AstralVessel image (named "astralvessel" in its Dockerfile).
            base = "service:executor" if repo_root else f"docker-image://{published('astralvessel')}"
            image = {"image": f"{project}-sandbox-{agent_id}:local",
                     "build": {"context": str(agent.host_package), "dockerfile": dockerfile,
                               "additional_contexts": {"astralvessel": base}}}
        else:
            image = _image(repo_root, project=project, contexts=_artifacts(deployment, repo_root))
        services[f"sandbox-{agent_id}"] = {
            **HARDENED, **image,
            "command": ["astralvessel", "sandbox"],
            "environment": {"VESSEL_PACKAGE": "/agent", "VESSEL_PORT": str(SANDBOX_PORT),
                            "VESSEL_PROFILE_HASH": agent.fingerprint,
                            "VESSEL_TURN_DISK": str(agent.profile.turn_bytes),
                            "VESSEL_USER_DISK": str(agent.profile.user_bytes)},
            "env_file": [f".generated/sandbox-{agent_id}.env"],
            "volumes": mounts,
            # One inode per KB of scratch, matching the charge per entry, so empty files cannot exhaust memory.
            "tmpfs": [f"/tmp:size={agent.profile.scratch},nr_inodes={size_bytes(agent.profile.scratch) // 1024},"
                      "noexec,nosuid,nodev,mode=1777"],
            "networks": [tools],
            "mem_limit": agent.profile.memory, "cpus": agent.profile.cpus, "pids_limit": agent.profile.pids,
            "labels": {"astralvessel.agent": agent_id, "astralvessel.profile": agent.fingerprint},
        }
    executor_networks: dict[str, Any] = {"front": {"ipv4_address": deployment.node.front_address}}
    executor_volumes = [f"{deployment.path}:/deploy/deployment.toml:ro", "executor_data:/executor"]
    for agent_id, agent in deployment.agents.items():
        executor_networks[f"tools-{agent_id}"] = {}
        executor_volumes.append(f"{agent.host_package}:/agents/{agent_id}:ro")
    services["executor"] = {
        **HARDENED, **_image(repo_root, project=project, contexts=_artifacts(deployment, repo_root)),
        "command": ["astralvessel", "executor"],
        "environment": {
            "VESSEL_DEPLOYMENT": "/deploy/deployment.toml", "VESSEL_PACKAGES_ROOT": "/agents",
            "VESSEL_SANDBOX_URL_TEMPLATE": f"http://sandbox-{{id}}:{SANDBOX_PORT}",
            "VESSEL_SANDBOX_TOKEN": _env_ref("env:VESSEL_SANDBOX_TOKEN"),
            # Listen only on the front network, so no sandbox (each shares a tools network with the executor) can
            # reach it.
            "VESSEL_HOST": deployment.node.front_address, "VESSEL_PORT": str(EXECUTOR_PORT),
            "VESSEL_NODE_URL": f"http://node:{NODE_INTERNAL_PORT}", "VESSEL_EXECUTOR_DIR": "/executor"},
        "env_file": [".generated/executor.env"],
        "volumes": executor_volumes,
        "tmpfs": ["/tmp:size=64m,noexec,nosuid,nodev,mode=1777"],
        "networks": executor_networks,
        # The executor reads the deployment file and every package at start, so a change to either must recreate it.
        "labels": {"astralvessel.inputs": inputs_hash(deployment)},
        "mem_limit": "1g", "pids_limit": 256,
        "depends_on": [f"sandbox-{a}" for a in deployment.agents],
    }
    volumes: dict[str, Any] = {"node_data": {}, "executor_data": {}}
    for agent in deployment.agents.values():
        for mount in agent.profile.mounts:
            if mount.source.startswith("volume:"):
                volumes[mount.source[7:]] = {"external": True}
    return {"name": project, "services": services, "networks": networks, "volumes": volumes}


def inputs_hash(deployment: Deployment) -> str:
    body = {"deployment": hashlib.sha256(deployment.path.read_bytes()).hexdigest(),
            "agents": {agent_id: agent.fingerprint for agent_id, agent in deployment.agents.items()}}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def _private_file(path: Path, text: str, *, keep: bool = False, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if keep else os.O_TRUNC)
    fd = os.open(path, flags, mode)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    path.chmod(mode)


def write(deployment: Deployment, out: Path, *, repo_root: Path | None, master_token: str) -> list[Path]:
    # The secrets folder sits beside the Compose file, so two deployments must not share a folder.
    if out.exists():
        lines = out.read_text().splitlines()
        named = next((line[6:].strip().strip("\"'") for line in lines if line.startswith("name: ")), None)
        if named and named != deployment.node.project:
            raise SystemExit(f"{out} belongs to project {named!r}; give each deployment its own folder, or --out")
    secrets = out.parent / ".generated"
    secrets.mkdir(mode=0o700, exist_ok=True)
    secrets.chmod(0o700)
    (secrets / ".gitignore").write_text("*\n")
    written = [out]
    node_env = secrets / "node.env"
    if not node_env.exists():  # keep the node's secrets across redeploys: they encrypt what the node has stored
        generated = {
            "WEB_SESSION_ENC_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
            "CREDENTIAL_ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
            "AUDIT_HMAC_SECRET": tokens.token_hex(32),
            "AGENT_API_KEY": tokens.token_hex(32),
        }
        _private_file(node_env, "".join(f"{name}={generated[name]}\n" for name in NODE_SECRETS), keep=True)
    written.append(node_env)
    if deployment.egress.upstream_url is not None:
        path = secrets / "egress.env"
        _private_file(path, f"EGRESS_TOKEN={egress_token(master_token)}\n")
        written.append(path)
    path = secrets / "executor.env"
    _private_file(path, f"VESSEL_EXECUTOR_TOKEN={executor_token(master_token)}\n")
    written.append(path)
    path = secrets / "executor-agents.json"
    # The node's user reads this file through the bind mount, so it is world-readable inside the private folder.
    _private_file(path, json.dumps({agent_id: agent_token(master_token, agent_id) for agent_id in deployment.agents},
                                   indent=2) + "\n", mode=0o644)
    written.append(path)
    path = secrets / "node-executor.env"
    _private_file(path, f"ASTRAL_VESSEL_EXECUTOR_TOKEN={executor_token(master_token)}\n")
    written.append(path)
    for agent_id in deployment.agents:
        path = secrets / f"sandbox-{agent_id}.env"
        _private_file(path, f"VESSEL_SANDBOX_TOKEN={sandbox_token(master_token, agent_id)}\n")
        written.append(path)
    header = ("# Generated by `astralvessel deploy` from " + str(deployment.path) + ". Do not edit; edit the\n"
              "# deployment file and run deploy again. Each sandbox is labeled with its profile fingerprint.\n")
    out.write_text(header + to_yaml(generate(deployment, repo_root=repo_root)))
    return written


def to_yaml(value: Any, indent: int = 0) -> str:
    # A small emitter for the plain dicts, lists and scalars above; every scalar is JSON-quoted, which YAML accepts.
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return "{}\n"
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{key}:\n{to_yaml(item, indent + 1)}")
            else:
                lines.append(f"{pad}{key}: {_scalar(item)}\n")
        return "".join(lines)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, (dict, list)) and item:
                body = to_yaml(item, indent + 1)
                lines.append(f"{pad}- {body.lstrip()}")
            else:
                lines.append(f"{pad}- {_scalar(item)}\n")
        return "".join(lines)
    return f"{pad}{_scalar(value)}\n"


def _scalar(value: Any) -> str:
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    return json.dumps(value)
