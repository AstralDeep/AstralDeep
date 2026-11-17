"""Boots the real orchestrator in a subprocess through the pack launcher with the personal profile and no PostgreSQL setting, then checks readiness, the SQLite file and a ticketed development-mesh request.
Uses scripts/node_bundle/launcher/astral_node_launch.py laid out as a pack, tests/helpers/dev_mesh.py for pairing and devtools/dev_mesh.py for the owner's ticket."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from devtools import dev_mesh
from shared import node_identity
from tests.helpers.dev_mesh import CALLER_PAUSE_MS, dev_node, now_ms, publish_catalog, until

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "node_bundle" / "launcher"
BOOT_SECONDS = 120.0
READ_VALUE = {
    "name": "read_value", "description": "reads a value on the node", "effect": "mesh_read", "annotations": {},
    "input_schema": {"type": "object", "properties": {}},
}
DATABASE_SETTINGS = ("DATABASE_URL", "ASTRALPLANE_DATABASE_URL", "ASTRALPLANE_DSN")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def lay_out_pack(root: Path) -> Path:
    shutil.copytree(LAUNCHER, root / "launcher")
    app = root / "app"
    shutil.copytree(ROOT / "backend", app / "backend", ignore=shutil.ignore_patterns("tests", "__pycache__", "data", "tmp", "*.pyc"))
    shutil.copytree(ROOT / "config", app / "config")
    return root / "launcher" / "astral_node_launch.py"


def node_environment(directory: Path, port: int) -> dict[str, str]:
    return {
        "ASTRAL_NODE_PROFILE": "personal",
        "ASTRAL_ENV": "development",
        "FF_MCP_SERVER": "true",
        "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
        "ASTRAL_NODE_DATA_DIR": str(directory / "node"),
        "ATTACHMENT_UPLOAD_ROOT": str(directory / "blobs"),
        "PERSONAL_AGENT_ARTIFACT_ROOT": str(directory / "agents"),
        "ASTRAL_NODE_MESH_LOOPBACK": "1",
        "ASTRAL_NODE_MAILBOX": "0",
        "ASTRAL_NODE_MESH_RELAY": "none",
    }


def write_env_file(path: Path, values: dict[str, str]) -> None:
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    path.chmod(0o600)


@contextmanager
def booted(directory: Path, port: int):
    pack = directory / "pack"
    pack.mkdir(exist_ok=True)
    script = lay_out_pack(pack) if not (pack / "launcher").exists() else pack / "launcher" / "astral_node_launch.py"
    env_file = directory / "node.env"
    write_env_file(env_file, node_environment(directory, port))
    inherited = {
        key: value for key, value in os.environ.items()
        if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"} or key.startswith("PYTEST_")
    }
    inherited.pop("PYTEST_CURRENT_TEST", None)
    for name in DATABASE_SETTINGS:
        assert name not in inherited
    assert not any(name.startswith("DB_") for name in inherited)
    log = (directory / "node.log").open("wb")
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", str(script), "--env-file", str(env_file), "--port", str(port)],
        env=inherited, stdout=log, stderr=subprocess.STDOUT, cwd=directory,
    )
    try:
        ready = wait_ready(process, port, directory / "node.log")
        yield ready
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log.close()


def wait_ready(process: subprocess.Popen, port: int, log: Path) -> httpx.Response:
    deadline = time.monotonic() + BOOT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"the node exited with {process.returncode}:\n{log.read_text(errors='replace')[-4000:]}")
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/readyz", timeout=2)
        except httpx.HTTPError:
            time.sleep(0.5)
            continue
        if response.status_code == 200:
            return response
        time.sleep(0.5)
    raise AssertionError(f"the node was not ready in time:\n{log.read_text(errors='replace')[-4000:]}")


def sqlite_file(directory: Path) -> Path:
    found = sorted((directory / "node").rglob("plane.sqlite3"))
    assert len(found) == 1, found
    return found[0]


META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "boot-check", "version": "1"},
}
MCP_HEADERS = {"Content-Type": "application/json", "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"}


def listing_body() -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": META}}).encode()


async def ticketed_listing(owner: dev_mesh.DevOwner, node_device: str, port: int) -> httpx.Response:
    holder = dev_mesh.CallerHolder.generate()
    ticket = owner.mint_caller_ticket(node_device, holder, ["read_value"])
    body = listing_body()
    headers = owner.caller_headers(holder, ticket, "POST", "/mcp", body)
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        return await client.post("/mcp", content=body, headers={**MCP_HEADERS, **headers})


async def bare_listing(port: int) -> httpx.Response:
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        return await client.post("/mcp", content=listing_body(), headers=MCP_HEADERS)


async def test_a_personal_node_boots_on_sqlite_and_serves_a_ticketed_request(tmp_path, monkeypatch):
    for name in DATABASE_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    async with dev_node(tmp_path, monkeypatch) as mesh:
        node_device = mesh.node_device
        await publish_catalog(mesh, [READ_VALUE])
        await until(lambda: now_ms() > mesh.opened_ms + CALLER_PAUSE_MS, "the replay pause to pass")
    node_identity.reset_cache()
    owner = dev_mesh.DevOwner("owner", tmp_path / "owner")
    port = free_port()
    with booted(tmp_path, port) as ready:
        assert ready.json()["db"] == "ok"
        database = sqlite_file(tmp_path)
        assert stat.S_IMODE(database.stat().st_mode) == 0o600
        await asyncio.sleep(CALLER_PAUSE_MS / 1000 + 1)
        listing = await ticketed_listing(owner, node_device, port)
        assert listing.status_code == 200, (listing.text, listing.headers.get("www-authenticate"))
        assert isinstance(listing.json()["result"]["tools"], list)
        refused = await bare_listing(port)
        assert refused.status_code in {401, 403}


async def test_a_second_boot_reads_the_sqlite_file_of_the_first_unchanged(tmp_path):
    port = free_port()
    with booted(tmp_path, port):
        database = sqlite_file(tmp_path)
    marker = database.parent / "marker.txt"
    marker.write_text("kept")
    before = database.stat().st_mode, database.read_bytes()[:16]
    with booted(tmp_path, free_port()):
        assert sqlite_file(tmp_path) == database
        assert (database.stat().st_mode, database.read_bytes()[:16]) == before
        assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert marker.read_text() == "kept"
