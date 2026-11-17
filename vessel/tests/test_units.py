# Unit tests for the manifest loader, the sandbox tools and the sandbox server.

from __future__ import annotations

import asyncio

import httpx
import pytest

from astralvessel import sandbox
from astralvessel.manifest import load_package
from astralvessel.sandbox import LocalRunner
from astralvessel.tools import clip


def test_example_field_guide_package_loads():
    package = load_package("examples/field-guide")
    assert package.manifest.agent.name == "field-guide"
    assert {t.name for t in package.declared} == {"species"}
    assert package.manifest.output.files == ["png", "csv"]


def test_unknown_builtin_is_rejected(package_dir):
    text = (package_dir / "vessel.toml").read_text().replace('"bash"]', '"bash", "teleport"]')
    (package_dir / "vessel.toml").write_text(text)
    with pytest.raises(ValueError, match="teleport"):
        load_package(package_dir)


def test_declared_tool_outside_package_is_rejected(package_dir, tmp_path):
    (tmp_path / "evil.toml").write_text('name = "evil"\ndescription = "x"\ncommand = ["true"]\n')
    text = (package_dir / "vessel.toml").read_text().replace('"tools/echo.toml"', '"../evil.toml"')
    (package_dir / "vessel.toml").write_text(text)
    with pytest.raises(ValueError, match="outside the package"):
        load_package(package_dir)


def test_declared_tool_cannot_shadow_a_builtin(package_dir):
    (package_dir / "tools" / "echo.toml").write_text('name = "bash"\ndescription = "x"\ncommand = ["true"]\n')
    with pytest.raises(ValueError, match="collide"):
        load_package(package_dir)


async def test_sandbox_tools_run_with_limits(package_dir):
    runner = LocalRunner(load_package(package_dir))
    out, err = await runner.run("read", {"path": "notes/ada-lovelace.md"})
    assert not err and "Analytical engine" in out
    out, err = await runner.run("grep", {"pattern": "Analytical", "glob": "*.md"})
    assert not err and "ada-lovelace.md" in out
    out, err = await runner.run("grep", {"pattern": "nothing-matches-this"})
    assert out == "no matches" and not err
    out, err = await runner.run("glob", {"pattern": "notes/*.md"})
    assert "ada-lovelace.md" in out
    out, err = await runner.run("glob", {"pattern": "*.md"})
    assert "ada-lovelace.md" in out
    out, err = await runner.run("glob", {"pattern": "/**/*.sqlite"})
    assert err and "relative" in out
    out, err = await runner.run("list", {"path": "notes"})
    assert out.strip() == "ada-lovelace.md"
    out, err = await runner.run("bash", {"command": "echo hi && exit 3"})
    assert err and "hi" in out and "exit code 3" in out
    out, err = await runner.run("bash", {"command": "sleep 30"})
    assert err and "timed out" in out
    out, err = await runner.run("echo", {"word": "vessel"})
    assert not err and '"word": "vessel"' in out
    out, err = await runner.run("read", {"path": "missing.md"})
    assert err


async def test_bash_does_not_inherit_the_harness_environment(package_dir, monkeypatch):
    monkeypatch.setenv("VESSEL_SECRET_PROBE", "should-not-leak")
    runner = LocalRunner(load_package(package_dir))
    out, _ = await runner.run("bash", {"command": "env"})
    assert "should-not-leak" not in out


def test_clip_marks_truncation():
    text, truncated = clip("x" * 5000, 1000)
    assert truncated and len(text) < 1100 and "truncated" in text
    assert clip("short", 1000) == ("short", False)










async def test_glob_stops_at_its_time_budget(package_dir):
    from astralvessel.tools import run_sandbox_tool
    out, err = await run_sandbox_tool("glob", {"pattern": "**/*"}, workdir=str(package_dir), timeout=0,
                                      declared={}, package_root=str(package_dir))
    assert not err and "stopped after 0 s" in out


async def test_reaper_kills_strays_but_spares_running_calls(monkeypatch):
    from astralvessel.tools import ACTIVE_SESSIONS
    stray = await asyncio.create_subprocess_exec("sleep", "30", start_new_session=True)
    running = await asyncio.create_subprocess_exec("sleep", "30", start_new_session=True)
    ACTIVE_SESSIONS.add(running.pid)
    try:
        # Only these two children are visible, so the test can never touch another process.
        monkeypatch.setattr(sandbox.os, "listdir", lambda _p: [str(stray.pid), str(running.pid), "self"])
        sandbox.reap_strays()
        assert running.returncode is None and stray.returncode is None  # not the container's init: no-op
        monkeypatch.setattr(sandbox.os, "getpid", lambda: 1)
        sandbox.reap_strays()
        assert await asyncio.wait_for(stray.wait(), 5) == -9
        await asyncio.sleep(0.1)
        assert running.returncode is None
    finally:
        ACTIVE_SESSIONS.discard(running.pid)
        running.kill()
        await running.wait()


async def test_tool_output_and_long_lines_are_bounded(package_dir):
    from astralvessel.tools import MAX_OUTPUT_BYTES, run_sandbox_tool
    out, err = await run_sandbox_tool("bash", {"command": "head -c 5000000 /dev/zero | tr '\\0' a"},
                                      workdir=str(package_dir), timeout=20, declared={}, package_root="")
    assert err and "[output stopped" in out and len(out) < MAX_OUTPUT_BYTES + 70_000
    (package_dir / "long.txt").write_text("a" * 50_000 + "\nsecond\n")
    out, err = await run_sandbox_tool("read", {"path": "long.txt"}, workdir=str(package_dir), timeout=5,
                                      declared={}, package_root="")
    assert not err and "[line truncated]" in out and "second" in out and len(out) < 5000


async def test_declared_flag_tools_accept_only_declared_parameters(package_dir):
    from astralvessel.manifest import DeclaredTool
    from astralvessel.tools import run_sandbox_tool
    tool = DeclaredTool(name="say", description="x", command=["echo"], input="args",
                        parameters={"type": "object", "properties": {"word": {"type": "string"}}})
    out, err = await run_sandbox_tool("say", {"word": "hi"}, workdir=str(package_dir), timeout=5,
                                      declared={"say": tool}, package_root="")
    assert not err and out == "--word=hi"
    out, err = await run_sandbox_tool("say", {"word": "hi", "exec": "sh"}, workdir=str(package_dir), timeout=5,
                                      declared={"say": tool}, package_root="")
    assert err and "unknown arguments" in out and "exec" in out


def test_sandbox_server_refuses_a_short_token(package_dir):
    with pytest.raises(ValueError, match="16"):
        sandbox.sandbox_app(str(package_dir), token="short")


async def test_sandbox_server_checks_workspace_keys(package_dir):
    app = sandbox.sandbox_app(str(package_dir), token="t" * 16, confine=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sb") as client:
        auth = {"Authorization": "Bearer " + "t" * 16}
        bad = await client.post("/run", json={"name": "bash", "args": {"command": "true"}, "workspace": "../x"},
                                headers=auth)
        assert bad.status_code == 400
        good = await client.post("/run", json={"name": "bash", "args": {"command": "true"}, "workspace": "c" * 32},
                                 headers=auth)
        assert good.status_code == 200 and not good.json()["error"]
        assert (await client.post("/end", json={"workspace": "c" * 32}, headers=auth)).status_code == 200
        assert (await client.post("/end", json={"workspace": 5}, headers=auth)).status_code == 400
        assert (await client.post("/end", json=["x"], headers=auth)).status_code == 400
        assert (await client.post("/end", json={"workspace": "c" * 32})).status_code == 401
        late = await client.post("/run", json={"name": "bash", "args": {"command": "true"}, "workspace": "c" * 32},
                                 headers=auth)
        assert late.status_code == 410


async def test_ending_a_turn_stops_its_calls_still_running(package_dir):
    app = sandbox.sandbox_app(str(package_dir), token="t" * 16, confine=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sb") as client:
        auth = {"Authorization": "Bearer " + "t" * 16}
        call = asyncio.create_task(client.post(
            "/run", json={"name": "bash", "args": {"command": "sleep 30"}, "workspace": "e" * 32}, headers=auth))
        await asyncio.sleep(0.5)
        started = asyncio.get_running_loop().time()
        assert (await client.post("/end", json={"workspace": "e" * 32}, headers=auth)).status_code == 200
        response = await asyncio.wait_for(call, timeout=5)
        assert response.status_code == 410 and asyncio.get_running_loop().time() - started < 5
