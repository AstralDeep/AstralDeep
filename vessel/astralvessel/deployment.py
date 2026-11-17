# Loads deployment.toml: the personal node to run, its owner's issuer, and the agents whose tools the executor runs.
# The agent package says what it needs; this file is what the operator grants, and the only place grants come from.

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .manifest import AgentPackage, load_package

AGENT_ID = r"^[a-z][a-z0-9-]{0,62}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


NODE_IMAGE = "astralvessel-node:local"
NODE_PORT = 8001
# Names the generated Compose file sets itself; the operator's [node.environment] may not override them.
OWNED_ENVIRONMENT = frozenset({
    "ASTRAL_NODE_PROFILE", "ASTRAL_NODE_DATA_DIR", "DATABASE_URL", "KEYCLOAK_AUTHORITY", "KEYCLOAK_CLIENT_ID",
    "KEYCLOAK_ALLOWED_AZP", "ASTRAL_NODE_OWNER", "ORCHESTRATOR_PORT", "ATTACHMENT_UPLOAD_ROOT",
    "PERSONAL_AGENT_ARTIFACT_ROOT", "ASTRAL_MAILBOX_BIN", "ASTRAL_NODE_MAILBOX_BIND", "PYTHON_DOTENV_DISABLED",
    "FF_A2A_SERVER", "PUBLIC_BASE_URL", "BACKEND_PUBLIC_URL"})
SECRET_SHAPED = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL", re.IGNORECASE)


class NodeSection(Strict):
    # Two deployments on one host need different project names, front subnets and published ports.
    project: str = Field(default="astralvessel", pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    # Any AstralDeep node image built with the kernel binding; the default is a tag built on this machine.
    image: str = Field(default=NODE_IMAGE, pattern=r"^[^\s]+$")
    bind: str = "127.0.0.1"
    port: int = Field(default=NODE_PORT, ge=1, le=65535)
    public_url: str | None = None
    environment: dict[str, str] = {}
    front_subnet: str = "172.31.247.0/24"
    front_address: str = "172.31.247.10"

    @model_validator(mode="after")
    def valid(self) -> NodeSection:
        subnet = ipaddress.ip_network(self.front_subnet, strict=True)
        if ipaddress.ip_address(self.front_address) not in subnet:
            raise ValueError(f"front_address {self.front_address} is not in front_subnet {self.front_subnet}")
        if subnet.prefixlen > 28:
            raise ValueError(f"front_subnet {self.front_subnet} is too small; use a /28 or larger")
        if ipaddress.ip_address(self.front_address) in self.front_dynamic_range:
            raise ValueError(f"front_address {self.front_address} must be in the lower half of {self.front_subnet}; "
                             f"Docker hands out the upper half ({self.front_dynamic_range}) to other containers")
        ipaddress.ip_address(self.bind)
        if self.public_url is not None:
            parts = urlsplit(self.public_url)
            if parts.scheme not in ("http", "https") or not parts.hostname or parts.path not in ("", "/"):
                raise ValueError("public_url must be an http(s) origin such as https://node.example.com")
        for name, value in self.environment.items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name in OWNED_ENVIRONMENT or SECRET_SHAPED.search(name):
                raise ValueError(f"node environment {name} is set by the deployment or looks like a secret")
            if "\n" in value or "\r" in value:
                raise ValueError(f"node environment {name} must be one line")
        return self

    @property
    def front_dynamic_range(self) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
        # Containers that join front get addresses only from the upper half, so none can take the executor's
        # fixed address while it is stopped.
        return list(ipaddress.ip_network(self.front_subnet, strict=True).subnets(new_prefix=None))[-1]


class OwnerSection(Strict):
    # The identity provider the node trusts, and the one owner it serves. The subject binds the node to its owner
    # now; without it the first owner whose token the node verifies binds it.
    issuer: str
    client_id: str = Field(min_length=1)
    subject: str | None = Field(default=None, min_length=1)
    allowed_azp: list[str] = []
    accepted_issuers: list[str] = []

    @model_validator(mode="after")
    def urls(self) -> OwnerSection:
        for url in (self.issuer, *self.accepted_issuers):
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname or re.search(r"[\s,]", url):
                raise ValueError(f"issuer {url!r} must be an http(s) URL with a host and no commas")
        return self

    @property
    def owner_id(self) -> str | None:
        if self.subject is None:
            return None
        return f"oidc.{hashlib.sha256(self.issuer.rstrip('/').encode()).hexdigest()[:16]}.{self.subject}"


class ExecutorSection(Strict):
    # A folder holding the pinned kernel wheel, handed to the image build so the executor can verify tickets.
    kernel_artifacts: str | None = None


class EgressSection(Strict):
    # The optional model gateway: an OpenAI-compatible endpoint the egress container reaches with a key only it holds.
    # Without an upstream the egress still serves its filtered web proxy and refuses model calls.
    upstream_url: str | None = None
    upstream_key: str = "env:VESSEL_UPSTREAM_MODEL_KEY"

    @model_validator(mode="after")
    def endpoint(self) -> EgressSection:
        url = self.upstream_url
        if url is None:
            return self
        if url.startswith("env:"):
            ok = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", url[4:]) is not None
        else:
            parts = urlsplit(url)
            ok = parts.scheme.lower() in ("http", "https") and bool(parts.hostname) and not re.search(r"\s", url)
        if not ok:
            raise ValueError("egress upstream_url must be an http(s) URL with a host, or env:NAME")
        return self


class MountGrant(Strict):
    target: str
    source: str
    read_only: bool = True


class SandboxProfile(Strict):
    mounts: list[MountGrant] = []
    memory: str = "2g"
    cpus: float = Field(default=2, gt=0)
    pids: int = Field(default=512, ge=32)
    scratch: str = "256m"
    # Disk a turn's workspace, and all of one user's live turns together, may hold inside scratch.
    turn_disk: str | None = None  # default: a quarter of scratch
    user_disk: str | None = None  # default: half of scratch, so one user cannot fill it

    @model_validator(mode="after")
    def disk_fits(self) -> SandboxProfile:
        scratch, turn, user = size_bytes(self.scratch), self.turn_bytes, self.user_bytes
        if not 0 < turn <= user <= scratch:
            raise ValueError("need 0 < turn_disk <= user_disk <= scratch")
        return self

    @property
    def turn_bytes(self) -> int:
        return size_bytes(self.turn_disk) if self.turn_disk else size_bytes(self.scratch) // 4

    @property
    def user_bytes(self) -> int:
        return size_bytes(self.user_disk) if self.user_disk else size_bytes(self.scratch) // 2


def size_bytes(text: str) -> int:
    # Docker-style sizes: "512k", "64m", "2g", or plain bytes.
    match = re.fullmatch(r"(\d+)([bkmg]?)", text.strip().lower())
    if not match:
        raise ValueError(f"size {text!r} must look like 64m or 2g")
    return int(match[1]) * 1024 ** "bkmg".index(match[2] or "b")


class AgentEntry(Strict):
    id: str = Field(pattern=AGENT_ID)
    package: str
    sandbox: SandboxProfile = SandboxProfile()


class DeploymentFile(Strict):
    node: NodeSection = NodeSection()
    owner: OwnerSection
    egress: EgressSection = EgressSection()
    executor: ExecutorSection = ExecutorSection()
    agents: list[AgentEntry] = []

    @model_validator(mode="after")
    def consistent(self) -> DeploymentFile:
        ids = [a.id for a in self.agents]
        if len(set(ids)) != len(ids):
            raise ValueError("agent ids must be unique")
        return self


class Agent(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    id: str
    package: AgentPackage
    host_package: Path
    profile: SandboxProfile

    @property
    def fingerprint(self) -> str:
        # Everything that shapes the sandbox container, plus the manifest and declared tools that the executor and
        # the sandbox load once at start; a change means a redeploy.
        sandbox = self.package.manifest.sandbox.model_dump()
        dockerfile = self.package.manifest.sandbox.dockerfile
        image = build_inputs_hash(self.package.root, dockerfile) if dockerfile else None
        body = {"id": self.id, "profile": self.profile.model_dump(), "needs": sandbox, "image": image,
                "loaded": self.package.loaded_digest(), "schema": 3}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


class Deployment(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    path: Path
    node: NodeSection
    owner: OwnerSection
    egress: EgressSection
    executor: ExecutorSection
    agents: dict[str, Agent]


def load_deployment(path: str | Path, packages_root: str | None = None) -> Deployment:
    # packages_root: inside the executor container, packages are mounted at <root>/<agent id>, not their host paths.
    path = Path(path).resolve()
    raw = DeploymentFile.model_validate(tomllib.loads(path.read_text()))
    agents: dict[str, Agent] = {}
    for entry in raw.agents:
        host = (path.parent / entry.package).resolve()
        package = load_package(Path(packages_root) / entry.id if packages_root else host)
        _check_grants(entry, package)
        agents[entry.id] = Agent(id=entry.id, package=package, host_package=host, profile=entry.sandbox)
    return Deployment(path=path, node=raw.node, owner=raw.owner, egress=raw.egress, executor=raw.executor,
                      agents=agents)


def _check_grants(entry: AgentEntry, package: AgentPackage) -> None:
    needs = {m.target: m for m in package.manifest.sandbox.mounts}
    grants = {m.target: m for m in entry.sandbox.mounts}
    missing = sorted(set(needs) - set(grants))
    extra = sorted(set(grants) - set(needs))
    if missing:
        raise ValueError(f"agent {entry.id} needs mounts the deployment does not grant: {', '.join(missing)}")
    if extra:
        raise ValueError(f"agent {entry.id} is granted mounts it does not declare: {', '.join(extra)}")
    for target, need in needs.items():
        # Every sandbox can serve many users at once, and a writable mount would be shared between them.
        if not need.read_only or not grants[target].read_only:
            raise ValueError(f"agent {entry.id}: writable mounts ({target}) are not supported yet; mounts are "
                             "read-only and each turn writes only its own scratch directory")


def build_inputs_hash(root: Path, dockerfile: str) -> str:
    # The Dockerfile plus every package file its COPY and ADD lines bring in, so editing one marks the sandbox stale.
    digest = hashlib.sha256((root / dockerfile).read_bytes())
    for line in (root / dockerfile).read_text().splitlines():
        words = line.split()
        if not words or words[0].upper() not in ("COPY", "ADD"):
            continue
        sources = [w for w in words[1:-1] if not w.startswith("--")]
        for source in sources:
            for path in sorted(root.glob(source.lstrip("/"))):
                files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
                for file in files:
                    if root in file.resolve().parents:
                        digest.update(str(file.relative_to(root)).encode() + b"\0" + file.read_bytes())
    return digest.hexdigest()


def sandbox_token(master: str, agent_id: str) -> str:
    # Each sandbox gets its own token, derived from one master secret the executor holds.
    return hmac.new(master.encode(), f"sandbox:{agent_id}".encode(), hashlib.sha256).hexdigest()


def egress_token(master: str) -> str:
    # The token for the egress model passthrough, so nothing else on the egress network can spend the key.
    return hmac.new(master.encode(), b"egress:model", hashlib.sha256).hexdigest()
