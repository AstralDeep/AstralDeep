# Disk limits: the deployment settings, the per-file limit on confined calls, and the turn and user limits.

from __future__ import annotations

import hashlib
import os

import httpx
import pytest
from test_deploy import deployment_text, landlock

from astralvessel import sandbox
from astralvessel.compose import generate
from astralvessel.deployment import load_deployment, size_bytes
from astralvessel.manifest import load_package

TOKEN = "t" * 16
AUTH = {"Authorization": "Bearer " + TOKEN}
ALICE, BOB = "a" * 32, "b" * 32


def test_sizes_and_defaults_follow_scratch(package_dir, tmp_path):
    assert (size_bytes("512k"), size_bytes("64m"), size_bytes("2g"), size_bytes("100")) == (
        512 * 1024, 64 * 1024 ** 2, 2 * 1024 ** 3, 100)
    with pytest.raises(ValueError):
        size_bytes("lots")
    path = tmp_path / "deployment.toml"
    path.write_text(deployment_text(package_dir, mounts='scratch = "100m"'))
    env = generate(load_deployment(path), repo_root=None)["services"]["sandbox-guide"]["environment"]
    assert (env["VESSEL_TURN_DISK"], env["VESSEL_USER_DISK"]) == (str(25 * 1024 ** 2), str(50 * 1024 ** 2))
    path.write_text(deployment_text(package_dir, mounts='scratch = "100m"\nturn_disk = "10m"\nuser_disk = "30m"'))
    env = generate(load_deployment(path), repo_root=None)["services"]["sandbox-guide"]["environment"]
    assert (env["VESSEL_TURN_DISK"], env["VESSEL_USER_DISK"]) == (str(10 * 1024 ** 2), str(30 * 1024 ** 2))
    for bad in ('turn_disk = "40m"\nuser_disk = "20m"', 'user_disk = "200m"', 'turn_disk = "0"'):
        path.write_text(deployment_text(package_dir, mounts=f'scratch = "100m"\n{bad}'))
        with pytest.raises(ValueError, match="turn_disk"):
            load_deployment(path)


async def test_the_executor_sends_a_digest_of_the_user_never_the_name():
    sent = []

    async def handler(request):
        sent.append(request.read())
        return httpx.Response(200, json={"output": "ok", "error": False})

    runner = sandbox.RemoteRunner("http://sb", TOKEN, 10)
    runner._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://sb")
    await runner.run("bash", {"command": "true"}, "c" * 32, owner="alice@example.org")
    body = sent[0].decode()
    assert "alice" not in body and hashlib.sha256(b"alice@example.org").hexdigest()[:32] in body


@landlock
async def test_no_single_file_may_outgrow_the_turn_limit(package_dir, tmp_path):
    package = load_package(package_dir)
    scratch = tmp_path / "calls"
    scratch.mkdir()
    confinement = sandbox.confinement_for(package, str(scratch))
    runner = sandbox.LocalRunner(package, sandbox.replace(confinement, max_file_bytes=100_000))
    command = "head -c 200000 /dev/zero > $TMPDIR/big && echo wrote; wc -c < $TMPDIR/big"
    out, _ = await runner.run("bash", {"command": command}, "c" * 32)
    assert "wrote" not in out and "100000" in out
    await runner.end_turn("c" * 32)


def disk_app(package_dir, tmp_path, monkeypatch, turn, user):
    monkeypatch.setenv("VESSEL_TURN_DISK", str(turn))
    monkeypatch.setenv("VESSEL_USER_DISK", str(user))
    scratch = tmp_path / "calls"
    app = sandbox.sandbox_app(str(package_dir), token=TOKEN, confine=True, scratch_root=str(scratch))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sb"), scratch


def write(kb, key, owner, name="f"):
    return {"name": "bash", "args": {"command": f"head -c {kb * 1024} /dev/urandom > $TMPDIR/{name}; echo ok"},
            "workspace": key, "owner": owner}


@landlock
async def test_a_turn_over_its_disk_limit_is_stopped_and_its_workspace_emptied(package_dir, tmp_path, monkeypatch):
    client, scratch = disk_app(package_dir, tmp_path, monkeypatch, turn=1024 * 1024, user=4 * 1024 * 1024)
    async with client:
        first = (await client.post("/run", json=write(600, "c" * 32, ALICE, "one"), headers=AUTH)).json()
        assert not first["error"]
        second = (await client.post("/run", json=write(600, "c" * 32, ALICE, "two"), headers=AUTH)).json()
        assert second["error"] and "1 MB disk limit" in second["output"]
        assert not (scratch / f"turn-{'c' * 32}").exists()
        again = (await client.post("/run", json=write(100, "c" * 32, ALICE), headers=AUTH)).json()
        assert not again["error"]  # the turn goes on with an empty workspace


@landlock
async def test_a_users_turns_together_are_held_to_the_user_limit(package_dir, tmp_path, monkeypatch):
    client, _ = disk_app(package_dir, tmp_path, monkeypatch, turn=1024 * 1024, user=1024 * 1024)
    async with client:
        assert not (await client.post("/run", json=write(600, "c" * 32, ALICE), headers=AUTH)).json()["error"]
        other = (await client.post("/run", json=write(600, "e" * 32, BOB), headers=AUTH)).json()
        assert not other["error"]  # another user's files do not count against Alice
        second = (await client.post("/run", json=write(600, "d" * 32, ALICE), headers=AUTH)).json()
        assert second["error"] and "across turns" in second["output"]


async def test_a_turn_stays_with_the_user_who_started_it(package_dir):
    app = sandbox.sandbox_app(str(package_dir), token=TOKEN, confine=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sb") as client:
        call = {"name": "bash", "args": {"command": "true"}, "workspace": "c" * 32}
        assert (await client.post("/run", json={**call, "owner": ALICE}, headers=AUTH)).status_code == 200
        assert (await client.post("/run", json={**call, "owner": BOB}, headers=AUTH)).status_code == 403
        assert (await client.post("/run", json={**call, "owner": "../x"}, headers=AUTH)).status_code == 400
        assert (await client.post("/run", json=call, headers=AUTH)).status_code == 403


def test_folders_shut_with_chmod_are_counted_and_removed(tmp_path):
    from astralvessel.tools import disk_usage, remove_tree
    shut = tmp_path / "turn" / "d"
    shut.mkdir(parents=True)
    (shut / "big").write_bytes(b"x" * 300_000)
    shut.chmod(0)
    assert disk_usage(str(tmp_path / "turn")) >= 300_000
    shut.chmod(0)
    remove_tree(str(tmp_path / "turn"))
    assert not (tmp_path / "turn").exists()


def test_empty_entries_are_charged_and_deep_trees_count_as_over(tmp_path):
    from astralvessel.tools import ENTRY_BYTES, MAX_DEPTH, disk_usage, remove_tree
    flat = tmp_path / "flat"
    flat.mkdir()
    for index in range(100):
        (flat / f"e{index}").touch()
    assert disk_usage(str(flat)) == 100 * ENTRY_BYTES
    deep = tmp_path / "deep"
    deep.mkdir()
    fd = os.open(deep, os.O_RDONLY)
    for _ in range(MAX_DEPTH + 40):  # deeper than PATH_MAX can name in one string
        os.mkdir("n" * 100, dir_fd=fd)
        child = os.open("n" * 100, os.O_RDONLY, dir_fd=fd)
        os.close(fd)
        fd = child
    os.close(fd)
    assert disk_usage(str(deep), limit=10 ** 9) > 10 ** 9
    remove_tree(str(deep))
    assert not deep.exists()


@landlock
async def test_a_deleted_file_held_open_still_counts(package_dir, tmp_path, monkeypatch):
    client, _ = disk_app(package_dir, tmp_path, monkeypatch, turn=1024 * 1024, user=4 * 1024 * 1024)
    hold = "exec 3>$TMPDIR/f; head -c 1500000 /dev/urandom >&3; rm $TMPDIR/f; sleep 5; echo done"
    async with client:
        out = (await client.post("/run", json={"name": "bash", "args": {"command": hold}, "workspace": "c" * 32,
                                               "owner": ALICE}, headers=AUTH)).json()
        assert out["error"] and "disk limit" in out["output"]


def test_a_name_too_long_to_reach_counts_as_over_instead_of_failing(tmp_path):
    from astralvessel.tools import disk_usage
    fd = os.open(tmp_path, os.O_RDONLY)
    for _ in range(17):  # under the depth limit, but the full path is longer than PATH_MAX
        os.mkdir("n" * 250, dir_fd=fd)
        child = os.open("n" * 250, os.O_RDONLY, dir_fd=fd)
        os.close(fd)
        fd = child
    os.close(fd)
    assert disk_usage(str(tmp_path), limit=10 ** 9) > 10 ** 9


@landlock
async def test_a_call_cannot_leave_its_session_and_nothing_it_starts_outlives_it(package_dir, tmp_path):
    package = load_package(package_dir)
    scratch = tmp_path / "calls"
    scratch.mkdir()
    runner = sandbox.LocalRunner(package, sandbox.confinement_for(package, str(scratch)))
    out, _ = await runner.run("bash", {"command": "python3 -c 'import os; os.setsid()' 2>&1 | tail -1; "
                                                  "ulimit -n; (sleep 377 >/dev/null 2>&1 &); echo started"}, "c" * 32)
    assert "PermissionError" in out and "4096" in out and "started" in out
    def sleeping(pid: str) -> bool:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                return handle.read() == b"sleep\x00377\x00"
        except OSError:
            return False
    assert not any(sleeping(p) for p in os.listdir("/proc") if p.isdigit())
    await runner.end_turn("c" * 32)


@landlock
async def test_no_call_of_a_turn_starts_while_its_workspace_is_being_emptied(package_dir, tmp_path, monkeypatch):
    import asyncio
    import threading
    import time
    removing, removed_at = threading.Event(), []
    original = sandbox.end_workspace

    def slow_end(confinement, workspace):
        removing.set()
        time.sleep(0.5)
        original(confinement, workspace)
        removed_at.append(time.time())

    monkeypatch.setattr(sandbox, "end_workspace", slow_end)
    client, _ = disk_app(package_dir, tmp_path, monkeypatch, turn=1024 * 1024, user=4 * 1024 * 1024)
    async with client:
        first = asyncio.create_task(client.post("/run", json=write(1500, "c" * 32, ALICE), headers=AUTH))
        await asyncio.to_thread(removing.wait, 10)
        clock = {"name": "bash", "args": {"command": "python3 -c 'import time; print(time.time())'"},
                 "workspace": "c" * 32, "owner": ALICE}
        second = (await client.post("/run", json=clock, headers=AUTH)).json()
        assert "disk limit" in (await first).json()["output"]
        assert float(second["output"].split()[0]) >= removed_at[0]
