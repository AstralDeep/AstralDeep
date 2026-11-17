# The operator's commands: up writes a deployment when there is none, starts the personal node and its executor, waits
# and probes; check re-runs the probes and the node checks, down stops it, and pair, ask and chat talk to the node.

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import conformance, node
from .deployment import AGENT_ID, Deployment, load_deployment
from .manifest import load_package
from .probes import run_probes
from .stack import Stack

ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
KEY_VARIABLE = "VESSEL_UPSTREAM_MODEL_KEY"
SANDBOX_HEALTH = r'''
import json, urllib.request
print(urllib.request.urlopen("http://127.0.0.1:8710/health", timeout=5).read().decode())
'''
DATA_MODES = r'''
import json, os, stat
root = "/node-data/node"
out = {"directory": oct(stat.S_IMODE(os.stat(root).st_mode))}
for name in ("plane.sqlite3", "node-ed25519.pem"):
    path = os.path.join(root, name)
    out[name] = oct(stat.S_IMODE(os.stat(path).st_mode)) if os.path.exists(path) else None
print(json.dumps(out))
'''


def example_root() -> Path:
    # Installed, the example ships inside the package; in a checkout it is the repository's examples folder.
    packaged = Path(__file__).resolve().parent / "example"
    return packaged if packaged.is_dir() else Path(__file__).resolve().parent.parent / "examples"


@dataclass
class Answers:
    issuer: str = ""
    client_id: str = ""
    subject: str = ""
    image: str = ""
    port: int = 0
    upstream_url: str = ""
    api_key: str = ""
    agents: list[Path] = field(default_factory=list)
    mounts: dict[tuple[int, str], str] = field(default_factory=dict)  # (agent index, target) -> host folder


def _ask(prompt: str, secret: bool = False) -> str:
    try:
        return (getpass.getpass(prompt) if secret else input(prompt)).strip()
    except EOFError:
        raise SystemExit("\nstopped: no answer") from None


def _check_url(url: str, what: str = "URL") -> str | None:
    from .deployment import OwnerSection
    try:
        OwnerSection(issuer=url, client_id="x")
    except ValueError:
        return f"give an http(s) {what} with a host, such as https://keycloak.example.com/realms/astral"
    return None


def _check_key(key: str) -> str | None:
    if key.startswith("env:"):
        name = key[4:]
        if not ENV_NAME.fullmatch(name):
            return f"{key} is not a valid environment variable name"
        if not os.environ.get(name):
            return f"{name} is not set in this shell"
        return None
    if any(c in key for c in "'\n\r"):
        return "the API key cannot contain quotes or line breaks"
    return None if key else "the API key is empty"


def _check_agent(path: str) -> str | None:
    try:
        load_package(Path(path).expanduser())
    except FileNotFoundError:
        return f"{path} is not an agent folder (it needs vessel.toml)"
    except (OSError, ValueError) as exc:
        return f"{path}: {exc}".splitlines()[0][:300]
    return None


def gather(args: argparse.Namespace, interactive: bool) -> Answers:
    answers = Answers(issuer=(args.issuer or "").rstrip("/"), client_id=args.client_id or "",
                      subject=args.subject or "", image=args.image or "", port=args.port or 0,
                      upstream_url=args.upstream_url or "", api_key=args.api_key or "")
    if answers.api_key and not answers.api_key.startswith("env:"):
        raise SystemExit("--api-key takes env:NAME, so the key never appears in a command line; "
                         "or leave it out and type it at the prompt")
    for given, check in ((answers.issuer, _check_url), (answers.upstream_url, _check_url),
                         (answers.api_key, _check_key)):
        if given and (problem := check(given)):
            raise SystemExit(problem)
    if answers.api_key and not answers.upstream_url:
        raise SystemExit("--api-key belongs to the model gateway; give --upstream-url as well")
    if args.example:
        answers.agents = [example_root() / "field-guide"]
        answers.mounts[(0, "/data")] = str(example_root() / "sample-data")
    for path in args.agent or []:
        if problem := _check_agent(path):
            raise SystemExit(problem)
        answers.agents.append(Path(path).expanduser().resolve())
    for grant in args.mount or []:
        match = re.fullmatch(r"([^:]+):(/[^=]*)=(.+)", grant)
        if not match:
            raise SystemExit(f"--mount {grant}: use AGENT_FOLDER:/target=/host/folder")
        index = next((i for i, a in enumerate(answers.agents)
                      if a.name == match[1] or str(a) == str(Path(match[1]).expanduser().resolve())), None)
        if index is None:
            raise SystemExit(f"--mount {grant}: {match[1]} is not one of the agents")
        folder = Path(match[3]).expanduser().resolve()
        if not folder.is_dir():
            raise SystemExit(f"--mount {grant}: {folder} is not a folder")
        answers.mounts[(index, match[2])] = str(folder)
    if not interactive:
        missing = [flag for flag, have in (("--issuer URL", answers.issuer), ("--client-id ID", answers.client_id))
                   if not have]
        missing += [f"--mount {a.name}:{m.target}=FOLDER" for i, a in enumerate(answers.agents)
                    for m in load_package(a).manifest.sandbox.mounts if (i, m.target) not in answers.mounts]
        if missing:
            raise SystemExit("no deployment.toml here and no terminal to ask on; pass " + ", ".join(missing))
        return answers
    print("No deployment.toml here, so let's write one. Press Ctrl-C to stop.\n")
    while not answers.issuer:
        url = _ask("Your identity provider (OIDC issuer URL, such as https://keycloak.example.com/realms/astral): ")
        if problem := _check_url(url, "issuer URL"):
            print("  " + problem)
        else:
            answers.issuer = url.rstrip("/")
    while not answers.client_id:
        answers.client_id = _ask("The client id your Astral apps sign in with: ")
    for index, agent in enumerate(answers.agents):
        for mount in load_package(agent).manifest.sandbox.mounts:
            while (index, mount.target) not in answers.mounts:
                folder = _ask(f"Folder to mount read-only at {mount.target} for {agent.name}: ")
                if Path(folder).expanduser().is_dir():
                    answers.mounts[(index, mount.target)] = str(Path(folder).expanduser().resolve())
                else:
                    print(f"  {folder} is not a folder")
    return answers


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-_")
    return slug[:63] or "astralvessel"


def _docker(*args: str) -> str:
    try:
        return subprocess.run(["docker", *args], check=True, text=True, capture_output=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def pick_project(folder: Path) -> str:
    # Another running deployment may already use this folder's name; never join or replace it.
    base = _slug(folder.name)
    for n in range(1, 50):
        name = base if n == 1 else f"{base[:60]}-{n}"
        label = f"label=com.docker.compose.project={name}"
        dirs = set(_docker("ps", "-a", "--filter", label, "--format",
                           '{{.Label "com.docker.compose.project.working_dir"}}').split())
        # A stopped deployment keeps its volumes and networks; reusing its name would reuse its node data.
        leftovers = (_docker("volume", "ls", "-q", "--filter", label) + _docker("network", "ls", "-q", "--filter",
                                                                                   label)).split()
        if (not dirs and not leftovers) or dirs == {str(folder)}:
            return name
    raise SystemExit("could not find a free project name; set [node] project yourself")


def pick_subnet() -> ipaddress.IPv4Network:
    # Docker refuses a network that overlaps another, so take the first free /24 in 172.31.0.0/16.
    ids = _docker("network", "ls", "-q").split()
    taken = []
    if ids:
        for text in _docker("network", "inspect", "-f", "{{range .IPAM.Config}}{{.Subnet}} {{end}}", *ids).split():
            try:
                taken.append(ipaddress.ip_network(text, strict=False))
            except ValueError:
                continue
    for third in range(247, 0, -1):
        net = ipaddress.ip_network(f"172.31.{third}.0/24")
        if not any(net.overlaps(other) for other in taken if other.version == 4):
            return net
    raise SystemExit("no free 172.31.x.0/24 subnet; set [node] front_subnet yourself")


def _toml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _relative(path: Path, folder: Path) -> str:
    try:
        return str(path.resolve().relative_to(folder))
    except ValueError:
        return str(path.resolve())


def write_deployment(folder: Path, answers: Answers, *, example: bool) -> Path:
    deployment, env = folder / "deployment.toml", folder / ".env"
    if env.exists():
        raise SystemExit(f"{env} exists but deployment.toml does not; move the old .env away first")
    agents = list(answers.agents)
    mounts = dict(answers.mounts)
    if example:
        # Copy the example in, so the deployment folder holds everything it runs; never into folders already there.
        taken = [str(p) for p in (folder / "agents" / "field-guide", folder / "data") if p.exists()]
        if taken:
            raise SystemExit(f"{', '.join(taken)} already exist; run --example in an empty folder")
        shutil.copytree(agents[0], folder / "agents" / "field-guide")
        shutil.copytree(mounts[(0, "/data")], folder / "data")
        agents[0], mounts[(0, "/data")] = folder / "agents" / "field-guide", str(folder / "data")
    subnet = pick_subnet()
    lines = ["# Written by astralvessel up. Edit it and run astralvessel up again to apply a change.", "",
             "[node]", f"project = {_toml(pick_project(folder))}"]
    if answers.image:
        lines.append(f"image = {_toml(answers.image)}")
    if answers.port:
        lines.append(f"port = {answers.port}")
    lines += [f"front_subnet = {_toml(str(subnet))}", f"front_address = {_toml(str(subnet.network_address + 10))}", "",
              "[owner]", f"issuer = {_toml(answers.issuer)}", f"client_id = {_toml(answers.client_id)}"]
    if answers.subject:
        lines.append(f"subject = {_toml(answers.subject)}")
    lines.append("")
    if answers.upstream_url:
        key_ref = answers.api_key if answers.api_key.startswith("env:") else f"env:{KEY_VARIABLE}"
        lines += ["[egress]", f"upstream_url = {_toml(answers.upstream_url)}", f"upstream_key = {_toml(key_ref)}", ""]
    ids: list[str] = []
    for index, agent in enumerate(agents):
        package = load_package(agent)
        base = package.manifest.agent.name if re.fullmatch(AGENT_ID, package.manifest.agent.name) else "agent"
        agent_id, n = base, 1
        while agent_id in ids:
            n += 1
            agent_id = f"{base[:58]}-{n}"
        ids.append(agent_id)
        lines += ["[[agents]]", f"id = {_toml(agent_id)}", f"package = {_toml(_relative(agent, folder))}"]
        grants = []
        for mount in package.manifest.sandbox.mounts:
            source = _relative(Path(mounts[(index, mount.target)]), folder)
            grants.append(f"{{ target = {_toml(mount.target)}, source = {_toml(source)} }}")
        if grants:
            lines += ["[agents.sandbox]", f"mounts = [{', '.join(grants)}]"]
        lines.append("")
    values = {"VESSEL_SANDBOX_TOKEN": secrets.token_urlsafe(32)}
    if answers.upstream_url and not answers.api_key.startswith("env:"):
        values[KEY_VARIABLE] = answers.api_key
    # Single quotes keep a key with $, # or spaces literal for Compose's env file and for load_env_file.
    text = "".join(f"{k}='{v}'\n" for k, v in values.items())
    fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write("# Secrets for this deployment; keep this file private.\n" + text)
        deployment.write_text("\n".join(lines))
        load_deployment(deployment)
    except BaseException:
        # Leave nothing half written, so the next up starts over cleanly.
        deployment.unlink(missing_ok=True)
        env.unlink(missing_ok=True)
        raise
    gitignore = folder / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    needed = [line for line in (".env", ".generated/", "compose.generated.yml") if line not in existing.splitlines()]
    if needed:
        lead = "\n" if existing and not existing.endswith("\n") else ""
        with gitignore.open("a") as handle:
            handle.write(lead + "\n".join(needed) + "\n")
    print(f"wrote {deployment} and {env} (private)")
    return deployment


def write_compose(stack: Stack, out: Path | None = None) -> tuple[Deployment, Path]:
    from .compose import write
    stack.load_env()
    master = os.environ.get("VESSEL_SANDBOX_TOKEN", "")
    if len(master) < 16:
        raise SystemExit("set VESSEL_SANDBOX_TOKEN (16+ characters) in the environment or the env file")
    deployment = load_deployment(stack.deployment)
    repo = Path(__file__).resolve().parent.parent
    target = out or stack.compose_file
    for path in write(deployment, target, repo_root=repo if (repo / "Dockerfile").exists() else None,
                      master_token=master):
        print(f"wrote {path}")
    for agent_id, agent in deployment.agents.items():
        print(f"  agent {agent_id}: sandbox profile {agent.fingerprint}")
    return deployment, target


def _missing_env(deployment: Deployment) -> list[str]:
    refs = [deployment.egress.upstream_url or "", deployment.egress.upstream_key if deployment.egress.upstream_url
            else ""]
    return [r[4:] for r in refs if r.startswith("env:") and not os.environ.get(r[4:])]


def _require_env(stack: Stack, deployment: Deployment) -> None:
    # Compose fills every env: reference in the whole file for every command, so a missing one breaks them all.
    if missing := _missing_env(deployment):
        raise SystemExit(f"set {', '.join(missing)} in the environment or {stack.env_file}")


def require_image(deployment: Deployment) -> None:
    if _docker("image", "inspect", "-f", "{{.Id}}", deployment.node.image).strip():
        return
    raise SystemExit(f"the node image {deployment.node.image} is not on this machine. Build or pull an AstralDeep "
                     "node image and tag it with that name, or set image in the [node] section")


def wait_ready(stack: Stack, deployment: Deployment, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    agents: dict[str, bool] = {}
    state = ""
    print("waiting for the node to be ready and every agent's sandbox to report it is confined", end="", flush=True)
    while time.monotonic() < deadline:
        state = node.health(deployment.node)
        agents = {}
        for agent_id, agent in deployment.agents.items():
            done = stack.python(f"sandbox-{agent_id}", SANDBOX_HEALTH, timeout=20)
            try:
                body = json.loads(done.stdout)
                agents[agent_id] = bool(body.get("confined")) and body.get("profile") == agent.fingerprint
            except (json.JSONDecodeError, AttributeError):
                agents[agent_id] = False
        if state == "ok" and all(agents.values()):
            print(" ready")
            return True
        states = stack.compose("ps", "-a", "--format", "{{.Service}} {{.State}}", check=False, capture=True).stdout
        stopped = (["exited"], ["dead"], ["restarting"])
        dead = [line.split()[0] for line in states.splitlines() if line.split()[1:2] in stopped]
        if dead:
            print(f"\nstopped: {', '.join(dead)} exited or is restarting")
            for service in dead:
                print(f"--- {service} ---\n{stack.logs(service)}")
            return False
        print(".", end="", flush=True)
        time.sleep(3)
    late = [a for a, ok in agents.items() if not ok]
    print(f"\nnot ready after {timeout:.0f}s: " + ", ".join(([] if state == "ok" else ["node"]) + late))
    if state != "ok":
        print(f"--- node ---\n{stack.logs('node')}")
    for agent_id in late:
        print(f"--- sandbox-{agent_id} ---\n{stack.logs(f'sandbox-{agent_id}')}")
    return False


def node_checks(stack: Stack, deployment: Deployment) -> list[str]:
    # Prints one PASS or FAIL line per check and returns the names of the failures.
    failed: list[str] = []

    def report(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  node: {name}" + (f" ({detail})" if detail and not ok else ""))
        if not ok:
            failed.append(name)

    state = node.health(deployment.node)
    report("answers /readyz", state == "ok", state)
    problem = node.descriptor_problem(node.fetch_descriptor(deployment.node), deployment.owner)
    report("publishes a descriptor for this owner's issuer", problem is None, problem or "")
    done = stack.python("node", DATA_MODES, timeout=30)
    try:
        modes = json.loads(done.stdout)
    except json.JSONDecodeError:
        modes = {}
    report("keeps its data directory private", modes.get("directory") == "0o700", str(modes.get("directory")))
    report("keeps its database in a SQLite file readable only by the node", modes.get("plane.sqlite3") == "0o600",
           str(modes.get("plane.sqlite3")))
    report("keeps its signing key readable only by the node", modes.get("node-ed25519.pem") in (None, "0o600"),
           str(modes.get("node-ed25519.pem")))
    return failed


def up(args: argparse.Namespace) -> None:
    stack = Stack.at(args.deployment or "deployment.toml")
    if not stack.deployment.exists():
        if args.deployment:
            raise SystemExit(f"{stack.deployment} does not exist")
        answers = gather(args, interactive=sys.stdin.isatty() and sys.stdout.isatty())
        write_deployment(stack.deployment.parent, answers, example=args.example)
    elif any((args.example, args.agent, args.issuer, args.client_id, args.subject, args.image, args.port,
              args.upstream_url, args.api_key, args.mount)):
        raise SystemExit(f"{stack.deployment} exists; edit it instead of passing setup options")
    deployment, _ = write_compose(stack)
    _require_env(stack, deployment)
    require_image(deployment)
    stack.compose("up", "-d", "--build", "--remove-orphans")
    if not wait_ready(stack, deployment, args.timeout):
        raise SystemExit("the deployment is running but not ready; fix the problem above and run astralvessel up")
    report = run_probes(stack, deployment, os.environ["VESSEL_SANDBOX_TOKEN"])
    failed = node_checks(stack, deployment)
    print(f"{report.passed} probes passed, {len(report.failed)} failed, {report.skipped} skipped")
    if report.failed or failed:
        raise SystemExit("the deployment is running, but a check failed; it is not ready for use")
    print(f"\nready: node at {node.base_url(deployment.node)}\n  astralvessel pair\n"
          "  astralvessel ask \"your question\"")


def check(args: argparse.Namespace) -> None:
    stack = Stack.at(args.deployment or "deployment.toml")
    stack.require_compose()
    stack.load_env()
    deployment = load_deployment(stack.deployment)
    _require_env(stack, deployment)
    report = run_probes(stack, deployment, os.environ.get("VESSEL_SANDBOX_TOKEN", ""))
    failed = [] if args.isolation_only else node_checks(stack, deployment)
    print(f"{report.passed} probes passed, {len(report.failed)} failed, {report.skipped} skipped")
    if report.failed or failed:
        raise SystemExit(1)


def down(args: argparse.Namespace) -> None:
    stack = Stack.at(args.deployment or "deployment.toml")
    stack.require_compose()
    stack.load_env()
    _require_env(stack, load_deployment(stack.deployment))
    stack.compose("down", *(["-v"] if args.volumes else []))


def _node_of(args: argparse.Namespace) -> Deployment:
    stack = Stack.at(args.deployment or "deployment.toml")
    stack.require_compose()
    return load_deployment(stack.deployment)


EXECUTOR_OFFER = r'''
import json, os, urllib.error, urllib.request
request = urllib.request.Request("http://%s:%s/pairing" % (os.environ["VESSEL_HOST"], os.environ["VESSEL_PORT"]),
                                 headers={"Authorization": "Bearer " + os.environ["VESSEL_EXECUTOR_TOKEN"]})
try:
    print(json.dumps({"status": 200, **json.loads(urllib.request.urlopen(request, timeout=20).read())}))
except urllib.error.HTTPError as exc:
    print(json.dumps({"status": exc.code, **json.loads(exc.read() or b"{}")}))
'''
WAITING = {"executor.waiting_for_node": "the node has not joined the owner's mesh yet; pair the node first",
           "executor.enrolled": "the executor is already in the owner's mesh",
           "executor.no_kernel": "this image has no kernel binding, so the executor cannot join a mesh"}


def pair_executor(args: argparse.Namespace) -> None:
    stack = Stack.at(args.deployment or "deployment.toml")
    stack.require_compose()
    stack.load_env()
    done = stack.python("executor", EXECUTOR_OFFER, timeout=60)
    try:
        reply = json.loads(done.stdout)
    except json.JSONDecodeError:
        raise SystemExit("the executor did not answer; check that the deployment is running") from None
    if reply.get("status") != 200:
        raise SystemExit(WAITING.get(reply.get("code", ""), f"the executor refused: {reply.get('code', 'unknown')}"))
    print("\n".join(["Add the executor from the AstralDeep app the same way, so it can check the owner's tickets.",
                      "", f"  code     {reply['code']}", f"  payload  {reply['payload']}"]))


def pair(args: argparse.Namespace) -> None:
    if args.executor:
        pair_executor(args)
        return
    deployment = _node_of(args)
    try:
        offer = node.request_pairing(deployment.node, node.owner_token(args.token_file), args.label)
    except node.NodeError as exc:
        raise SystemExit(str(exc)) from None
    print(node.describe_offer(offer, deployment.node))


def ask(args: argparse.Namespace) -> None:
    deployment = _node_of(args)
    try:
        answer = node.ask(deployment.node, node.owner_token(args.token_file), args.question)
    except node.NodeError as exc:
        raise SystemExit(str(exc)) from None
    print(answer)


def chat(args: argparse.Namespace) -> None:
    deployment = _node_of(args)
    token = node.owner_token(args.token_file)
    context = None
    print("Chatting with your node. Press Ctrl-D to stop.")
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        try:
            answer, context = node.converse(deployment.node, token, line, context)
        except node.NodeError as exc:
            print(f"error: {exc}")
            continue
        print(f"node> {answer}")


def conformance_suite(args: argparse.Namespace) -> None:
    deployment = _node_of(args)
    failed = conformance.run(deployment, node.owner_token(args.token_file))
    print(f"{len(conformance.CHECKS) - len(failed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)
