#!/usr/bin/env python3
"""Installs a node pack into a temporary root, runs the confinement conformance, starts the node from the pack, pairs a development owner and runs a caller-ticketed tool call, with Docker absent from every child's PATH.
It is stdlib only and drives the pack's own interpreter through node_bundle/verify.py, the pack's install script, launcher and astral_exec; the owner half re-runs this file inside the pack with the development mesh of backend/devtools/dev_mesh.py."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

HERE = Path(__file__).resolve().parent
VERIFY = HERE / "node_bundle" / "verify.py"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
TOOL = "generate_dynamic_chart"
CHART_TITLE = "No Docker check chart"
CHART_ARGUMENTS = {
    "data": [{"name": "a", "value": 1}, {"name": "b", "value": 2}],
    "x_key": "name",
    "y_key": "value",
    "chart_type": "bar",
    "title": CHART_TITLE,
}
PROTOCOL = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": PROTOCOL,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "no-docker-check", "version": "1"},
}
REFUSED_STATUSES = {401, 403}
PAUSED_STATUSES = {401, 403, 503}
NODE_ADDITIONS = {
    "ASTRAL_ENV": "development",
    "FF_MCP_SERVER": "true",
    "ASTRAL_NODE_MESH_LOOPBACK": "1",
    "ASTRAL_NODE_MAILBOX": "0",
    "ASTRAL_NODE_MESH_RELAY": "none",
    "ASTRAL_NODE_PUBLISHED_TOOLS": f"general-1__{TOOL},dice-roller-1__roll_dice",
}
CATALOG_WAIT_S = 240.0
PAUSE_RETRY_S = 90.0
READY_WAIT_S = 240.0


class CheckError(Exception):
    pass


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, bool(ok), detail))
        return bool(ok)

    @property
    def failed(self) -> list[Check]:
        return [check for check in self.checks if not check.ok]

    def lines(self) -> list[str]:
        rows = [f"{'PASS' if c.ok else 'FAIL'} {c.name}" + (f" ({c.detail})" if c.detail else "") for c in self.checks]
        rows.append(f"RESULT {len(self.checks) - len(self.failed)} passed, {len(self.failed)} failed of {len(self.checks)}")
        return rows

    @property
    def exit_code(self) -> int:
        return 1 if self.failed or not self.checks else 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def clean_path(extra: Sequence[str] = ()) -> str:
    return os.pathsep.join([*extra, *SYSTEM_PATH.split(os.pathsep)])


def docker_on_path(path: str, which: Callable[..., Optional[str]] = shutil.which) -> Optional[str]:
    return which("docker", path=path)


def parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return values


def merge_env(text: str, extra: dict[str, str]) -> str:
    present = parse_env_text(text)
    body = text if not text or text.endswith("\n") else text + "\n"
    return body + "".join(f"{key}={value}\n" for key, value in extra.items() if key not in present)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def pack_python(root: Path) -> Path:
    return root / "python" / "bin" / "python3.11"


def child_env(home: Path) -> dict[str, str]:
    return {"PATH": clean_path(), "HOME": str(home), "TMPDIR": str(home / "tmp") + "/", "LANG": "en_US.UTF-8"}


def conformance_verdict(returncode: int, output: str) -> tuple[bool, str]:
    rows = [line.split(" ", 1)[0] for line in output.splitlines() if line.split(" ", 1)[0] in {"PASS", "FAIL", "REFUSED"}]
    passed = rows.count("PASS")
    detail = f"{passed} of {len(rows)} cases passed"
    return returncode == 0 and bool(rows) and passed == len(rows), detail


def mcp_body(method: str, params: Optional[dict] = None) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {**(params or {}), "_meta": dict(META)}}).encode()


def mcp_headers(method: str, name: Optional[str] = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "MCP-Protocol-Version": PROTOCOL, "Mcp-Method": method}
    if name:
        headers["Mcp-Name"] = name
    return headers


def result_text(text: str) -> str:
    try:
        payload = json.loads(text)
    except ValueError:
        return text
    return json.dumps(payload.get("result", payload)) if isinstance(payload, dict) else text


def covered_names(text: str) -> list[str]:
    try:
        tools = json.loads(text)["result"]["tools"]
    except (ValueError, KeyError, TypeError):
        return []
    return sorted(tool.get("name", "") for tool in tools if isinstance(tool, dict))


def refused(status: Optional[int], text: str = "") -> bool:
    if status is None:
        return False
    if status in REFUSED_STATUSES or status >= 400:
        return True
    try:
        return "error" in json.loads(text)
    except (ValueError, TypeError):
        return False


def evaluate(observed: dict, tool_name: str) -> Report:
    report = Report()
    if not report.add("development owner paired and the node published its tools", observed.get("paired"), observed.get("paired_detail", "")):
        return report
    status = observed.get("anonymous", {}).get("status")
    report.add("a call with no credential is refused", status in REFUSED_STATUSES, f"status {status}")
    status = observed.get("bearer", {}).get("status")
    report.add("a call with a bearer token is refused", status in REFUSED_STATUSES, f"status {status}")
    listing = observed.get("listing", {})
    report.add("the ticket lists only the tool it covers", listing.get("status") == 200 and covered_names(listing.get("text", "")) == [tool_name],
               f"status {listing.get('status')}")
    call = observed.get("call", {})
    answered = call.get("status") == 200 and CHART_TITLE in call.get("text", "") and not refused(200, call.get("text", ""))
    report.add(f"{TOOL} ran for the ticketed caller and returned its chart", answered, f"status {call.get('status')}")
    other = observed.get("uncovered")
    if other is None:
        report.add("a tool outside the ticket is refused", False, "the node published no second tool to try")
    else:
        report.add("a tool outside the ticket is refused", refused(other.get("status"), other.get("text", "")), f"status {other.get('status')}")
    replay = observed.get("replay", {})
    report.add("a replayed proof is refused", refused(replay.get("status"), replay.get("text", "")), f"status {replay.get('status')}")
    return report


def post(base: str, path: str, body: bytes, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(base + path, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return {"status": response.status, "text": response.read().decode(errors="replace")}
    except urllib.error.HTTPError as error:
        return {"status": error.code, "text": error.read().decode(errors="replace")}
    except OSError as error:
        return {"status": None, "text": str(error)}


async def owner_turn(pack: Path, directory: Path, port: int, offer: str) -> dict:
    sys.path.insert(0, str(pack / "app" / "backend"))
    from devtools import dev_mesh

    base = f"http://127.0.0.1:{port}"
    observed: dict = {}
    owner = dev_mesh.DevOwner("owner", directory / "owner")
    owner.create_mesh()
    owner.confirm_recovery()
    await owner.start()
    try:
        await owner.approve_offer(offer)
        deadline = time.monotonic() + CATALOG_WAIT_S
        entries: list = []
        while time.monotonic() < deadline:
            entries = list(owner.kernel.peer_tool_catalog())
            if any(entry.tool.name.endswith(TOOL) for entry in entries):
                break
            await asyncio.sleep(0.5)
        chosen = next((entry for entry in entries if entry.tool.name.endswith(TOOL)), None)
        if chosen is None:
            observed["paired"] = False
            observed["paired_detail"] = f"{len(entries)} tools published, none ends with {TOOL}"
            return observed
        observed["paired"] = True
        observed["tool_name"] = chosen.tool.name
        other = next((e.tool.name for e in entries if e.device == chosen.device and e.tool.name != chosen.tool.name), None)
        holder = dev_mesh.CallerHolder.generate()
        ticket = owner.mint_caller_ticket(chosen.device, holder, [chosen.tool.name], label="No Docker check")

        def signed(body: bytes) -> dict[str, str]:
            return owner.caller_headers(holder, ticket, "POST", "/mcp", body)

        def call(name: str) -> tuple[bytes, dict[str, str]]:
            body = mcp_body("tools/call", {"name": name, "arguments": CHART_ARGUMENTS})
            return body, {**mcp_headers("tools/call", name), **signed(body)}

        observed["anonymous"] = await asyncio.to_thread(post, base, "/mcp", *_pair(mcp_body("tools/list"), mcp_headers("tools/list")))
        observed["bearer"] = await asyncio.to_thread(
            post, base, "/mcp", *_pair(mcp_body("tools/list"), {**mcp_headers("tools/list"), "Authorization": "Bearer not-a-real-token"}))
        end = time.monotonic() + PAUSE_RETRY_S
        while True:
            body = mcp_body("tools/list")
            observed["listing"] = await asyncio.to_thread(post, base, "/mcp", body, {**mcp_headers("tools/list"), **signed(body)})
            if observed["listing"]["status"] not in PAUSED_STATUSES or time.monotonic() > end:
                break
            await asyncio.sleep(1.0)
        body, headers = call(chosen.tool.name)
        observed["call"] = await asyncio.to_thread(post, base, "/mcp", body, headers)
        observed["replay"] = await asyncio.to_thread(post, base, "/mcp", body, headers)
        if other is not None:
            body, headers = call(other)
            observed["uncovered"] = await asyncio.to_thread(post, base, "/mcp", body, headers)
    finally:
        await owner.stop()
    return observed


def _pair(body: bytes, headers: dict[str, str]) -> tuple[bytes, dict[str, str]]:
    return body, headers


def wait_ready(port: int, seconds: float, process: subprocess.Popen) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if process.poll() is not None:
            raise CheckError(f"the node exited with {process.returncode}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=3) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.5)
    raise CheckError("the node did not become ready")


def wait_for_file(path: Path, seconds: float) -> str:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.is_file() and path.stat().st_size:
            return path.read_text().strip()
        time.sleep(0.2)
    raise CheckError(f"{path.name} did not appear")


def fetch_offer(port: int, secret: str) -> str:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/node/mesh/offer", data=b"", method="POST",
                                     headers={"Astral-Pairing-Secret": secret})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except (OSError, ValueError) as error:
        raise CheckError(f"the node gave no pairing offer: {error}") from error
    if payload.get("state") != "offer" or not payload.get("payload"):
        raise CheckError(f"the node answered {payload.get('state')} instead of an offer")
    return payload["payload"]


def group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_group(process: Optional[subprocess.Popen]) -> None:
    if process is None:
        return
    for sent in (signal.SIGTERM, signal.SIGKILL):
        if not group_alive(process.pid):
            break
        try:
            os.killpg(process.pid, sent)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            continue
        deadline = time.monotonic() + 5
        while group_alive(process.pid) and time.monotonic() < deadline:
            time.sleep(0.1)


def listed_commands() -> list[tuple[int, str]]:
    proc = Path("/proc")
    if proc.is_dir():
        found = []
        for entry in proc.iterdir():
            if entry.name.isdigit():
                try:
                    found.append((int(entry.name), (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")))
                except OSError:
                    continue
        return found
    listing = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True)
    rows = []
    for line in listing.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit():
            rows.append((int(pid), command))
    return rows


def leftover_processes(marker: str, commands: Callable[[], list[tuple[int, str]]] = listed_commands) -> list[int]:
    return [pid for pid, command in commands() if marker in command and pid != os.getpid()]


def installed_pack_python(root: Path) -> Path:
    python = pack_python(root)
    if not python.is_file():
        raise CheckError(f"{root} holds no node pack interpreter")
    return python


def step_docker(report: Report) -> None:
    found = docker_on_path(clean_path())
    report.add("docker is absent from the PATH every step runs with", found is None, f"found {found}" if found else "")


def step_install(report: Report, pack_dir: Path, root: Path) -> Path:
    descriptor = pack_dir / "node-pack.json"
    if not descriptor.is_file():
        raise CheckError(f"{pack_dir} holds no node-pack.json")
    done = subprocess.run([sys.executable, str(VERIFY), str(descriptor), "--sha256", sha256_file(descriptor), "--into", str(root)],
                          capture_output=True, text=True, env={"PATH": clean_path()})
    report.add("pack folder verifies and installs", done.returncode == 0, (done.stderr or done.stdout).strip()[-200:] if done.returncode else "")
    if done.returncode:
        raise CheckError("the pack did not install")
    return root


def step_conformance(report: Report, root: Path, home: Path) -> None:
    python = installed_pack_python(root)
    done = subprocess.run([str(python), "-I", "-B", str(root / "app/backend/astral_exec/run.py"), "conformance"],
                          capture_output=True, text=True, env=child_env(home), cwd=home, timeout=900)
    ok, detail = conformance_verdict(done.returncode, done.stdout)
    report.add("confinement conformance passes on the host backend", ok, detail if ok else f"exit {done.returncode}, {detail}")


def step_install_home(root: Path, home: Path, port: int) -> Path:
    done = subprocess.run(["/bin/bash", str(root / "app/scripts/node/install-personal-node.sh"), "--home", str(home)],
                          capture_output=True, text=True, env=child_env(home))
    if done.returncode:
        raise CheckError("install-personal-node.sh failed: " + (done.stdout + done.stderr)[-300:])
    env_file = home / "node.env"
    additions = {**NODE_ADDITIONS, "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}"}
    env_file.write_text(merge_env(env_file.read_text(), additions))
    env_file.chmod(0o600)
    return env_file


def run_check(pack_dir: Path, keep: bool, log: Callable[[str], None]) -> Report:
    report = Report()
    scratch = Path(tempfile.mkdtemp(prefix="nodocker-"))
    scratch.chmod(0o700)
    node: Optional[subprocess.Popen] = None
    try:
        log(f"workdir {scratch.name}")
        step_docker(report)
        if report.failed:
            return report
        root = step_install(report, pack_dir, scratch / "pack")
        home = scratch / "home"
        (home / "tmp").mkdir(parents=True)
        step_conformance(report, root, home)
        port = free_port()
        env_file = step_install_home(root, home, port)
        python = installed_pack_python(root)
        node = subprocess.Popen(
            [str(python), "-I", str(root / "launcher/astral_node_launch.py"), "--env-file", str(env_file), "--port", str(port),
             "--log-dir", str(home / "logs")],
            cwd=root, start_new_session=True, stdout=subprocess.DEVNULL, stderr=(scratch / "node-stderr.log").open("w"),
            env=child_env(home))
        wait_ready(port, READY_WAIT_S, node)
        report.add("node starts from the pack with ASTRAL_ENV=development", True)
        secret = wait_for_file(home / "data" / "pairing-secret", 30)
        offer = fetch_offer(port, secret)
        offer_file = scratch / "offer.txt"
        offer_file.write_text(offer)
        done = subprocess.run(
            [str(python), "-I", str(Path(__file__).resolve()), "owner", str(root), str(scratch), str(port), str(offer_file)],
            capture_output=True, text=True, timeout=900, env={**child_env(home), "ASTRAL_ENV": "development"})
        if done.returncode:
            raise CheckError("owner step failed: " + done.stderr[-400:])
        observed = json.loads(done.stdout.strip().splitlines()[-1])
        report.checks.extend(evaluate(observed, observed.get("tool_name", TOOL)).checks)
    except (CheckError, subprocess.TimeoutExpired, ValueError, OSError) as error:
        report.add("run completed", False, f"{type(error).__name__}: {error}")
    finally:
        stop_group(node)
        left = leftover_processes(scratch.name)
        gone = node is None or not group_alive(node.pid)
        report.add("no process the node started remains", gone and not left, f"{len(left)} left")
        if not keep:
            shutil.rmtree(scratch, ignore_errors=True)
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["owner"]:
        pack, directory, port, offer_file = arguments[1:5]
        print(json.dumps(asyncio.run(owner_turn(Path(pack), Path(directory), int(port), Path(offer_file).read_text().strip()))))
        return 0
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pack", type=Path, required=True, help="a folder holding node-pack.json and its archive")
    parser.add_argument("--keep", action="store_true", help="keep the temporary folder")
    options = parser.parse_args(arguments)
    started = time.monotonic()
    report = run_check(options.pack.expanduser().resolve(), options.keep, lambda line: print(line, flush=True))
    for line in report.lines():
        print(line)
    print(f"DURATION {time.monotonic() - started:.1f}s")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
