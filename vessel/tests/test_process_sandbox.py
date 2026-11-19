# Process-mode tests: agent calls run under the host's confinement backend after ticket verification, with no Docker.
# The file runs on its own (no conftest, no pytest-asyncio) so the Linux harness runs it exactly as this Mac does.

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

from astralvessel import sandbox, tools
from astralvessel.deployment import load_deployment
from astralvessel.execmode import ProcessExec, ProcessRefused, refusal_text
from astralvessel.executor import (
    Replays,
    agent_token,
    args_digest,
    catalog,
    executor_app,
    workspace_for,
)
from astralvessel.manifest import load_package
from astralvessel.mesh import Refused, Verified

# The harness runs this file on its own, so the skip is local rather than in conftest.
_host_backend = unittest.skipIf(sys.platform.startswith("win32"),
                                "the process exec backend confines with Landlock or Seatbelt; Windows has neither")

MASTER, SANDBOX_TOKEN, OPERATOR = "m" * 20, "s" * 16, "e" * 40
AUTH = {"Authorization": f"Bearer {SANDBOX_TOKEN}"}
ALICE, BOB = "a" * 32, "b" * 32
ABI5 = {"platform": "linux", "mode": "enforce", "landlock_abi": 5, "bwrap": True, "userns": True, "euid": 1000,
        "sandbox_exec": False, "app_sandboxed": False}


def real(path) -> str:
    return os.path.realpath(path)


def build_world(root: Path) -> dict:
    root = Path(real(root))
    package = root / "agent"
    (package / "tools").mkdir(parents=True)
    work = root / "work"
    (work / "notes").mkdir(parents=True)
    (work / "notes" / "ada.md").write_text("# Ada\nAnalytical engine.\n")
    (package / "tools" / "echo.toml").write_text(
        'name = "echo"\ndescription = "Echo the input"\ncommand = ["python3", "-c", '
        '"import sys,json; print(json.dumps(json.load(sys.stdin)))"]\n'
        '[parameters]\ntype = "object"\n[parameters.properties.word]\ntype = "string"\n')
    (package / "vessel.toml").write_text(f"""
[agent]
name = "tester"
description = "A test agent."

[tools]
builtin = ["read", "grep", "glob", "list", "bash"]
declared = ["tools/echo.toml"]

[sandbox]
workdir = "{work.as_posix()}"
deadline_seconds = 20
tool_timeout_seconds = 10
""")
    node = root / "node-data"
    node.mkdir()
    (node / "secret.db").write_text("node secret canary")
    data = root / "exec-data"
    (data / "exec").mkdir(parents=True)
    (root / "deployment.toml").write_text(f'[[agents]]\nid = "guide"\npackage = "{package.as_posix()}"\n')
    return {"root": root, "package": package, "work": work, "node": node, "data": data}


def process_exec(world: dict, facts=None, **over) -> ProcessExec:
    import astral_exec
    interpreter = Path(real(os.sys.executable))
    values = dict(data_dir=str(world["data"]), node_data=str(world["node"]), home=real(Path.home()),
                  python=os.sys.executable, run_py=str(Path(astral_exec.__file__).resolve().parent / "run.py"),
                  reads=(real(os.sys.base_prefix),), path_dirs=(str(interpreter.parent),))
    if facts is not None:
        values["facts"] = lambda: facts
    return ProcessExec(**{**values, **over})


def app_for(world: dict, **over):
    package = load_package(world["package"])
    return sandbox.sandbox_app(str(world["package"]), token=SANDBOX_TOKEN, process=process_exec(world, **over)), package


async def post(app, path: str, body: dict, headers=AUTH):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sb") as http:
        return await http.post(path, json=body, headers=headers)


def sandbox_call(app, name: str, args: dict, workspace: str | None = "c" * 32, owner: str | None = ALICE):
    response = asyncio.run(post(app, "/run", {"name": name, "args": args, "workspace": workspace, "owner": owner}))
    return response.status_code, response.json()


@_host_backend
def test_builtins_and_confined_tools_run_under_the_host_backend():
    with tempfile.TemporaryDirectory() as tmp:
        world = build_world(Path(tmp))
        app, _ = app_for(world)
        assert sandbox_call(app, "read", {"path": "notes/ada.md"})[1]["output"].endswith("Analytical engine.")
        assert "ada.md" in sandbox_call(app, "glob", {"pattern": "*.md"})[1]["output"]
        assert "notes/" in sandbox_call(app, "list", {})[1]["output"]
        assert "ada.md:2:Analytical engine." in sandbox_call(app, "grep", {"pattern": "engine"})[1]["output"]
        code, body = sandbox_call(app, "bash", {"command": "echo hi; pwd"})
        assert code == 200 and "hi" in body["output"] and not body["error"], body
        assert "turn-" in body["output"]
        echoed = sandbox_call(app, "echo", {"word": "x"})[1]
        assert json.loads(echoed["output"]) == {"word": "x"}, echoed


@_host_backend
def test_a_confined_call_cannot_read_node_data_or_write_outside_its_workspace():
    with tempfile.TemporaryDirectory() as tmp:
        world = build_world(Path(tmp))
        app, _ = app_for(world)
        outside = world["root"] / "outside.txt"
        out = sandbox_call(app, "bash", {"command": f"cat {world['node']}/secret.db; echo x > {outside}; "
                                                    f"cat {world['data']}/exec/../secret 2>&1; echo done"})[1]
        assert "canary" not in out["output"] and not outside.exists() and "done" in out["output"]


@_host_backend
def test_health_reports_the_backend_or_why_there_is_none():
    with tempfile.TemporaryDirectory() as tmp:
        world = build_world(Path(tmp))
        app, package = app_for(world)
        body = asyncio.run(_get(app, "/health"))
        assert body["confined"] is True and body["backend"] in ("seatbelt", "linux") and body["unavailable"] is None
        weak, _ = app_for(world, facts=ABI5)
        body = asyncio.run(_get(weak, "/health"))
        assert body["confined"] is True and body["backend"] is None and "landlock_abi" in body["unavailable"]


async def _get(app, path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sb") as http:
        return (await http.get(path)).json()


@_host_backend
def test_with_landlock_abi_5_builtins_answer_grep_searches_in_python_and_bash_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        world = build_world(Path(tmp))
        app, _ = app_for(world, facts=ABI5, rg=None)
        spawned = []
        original = tools._spawn

        async def counting(*args, **kwargs):
            spawned.append(args)
            return await original(*args, **kwargs)

        tools._spawn = counting
        try:
            assert "Analytical" in sandbox_call(app, "read", {"path": "notes/ada.md"})[1]["output"]
            assert "ada.md" in sandbox_call(app, "glob", {"pattern": "*.md"})[1]["output"]
            assert "notes/" in sandbox_call(app, "list", {})[1]["output"]
            found = sandbox_call(app, "grep", {"pattern": "ENGINE", "ignore_case": True, "glob": "*.md"})[1]
            assert "ada.md:2:Analytical engine." in found["output"] and not found["error"]
            assert sandbox_call(app, "grep", {"pattern": "nothing-here"})[1]["output"] == "no matches"
            code, refused = sandbox_call(app, "bash", {"command": "echo hi"})
            assert code == 200 and refused["error"] is True
            assert "exec.backend_unavailable" in refused["output"] and "landlock_abi" in refused["output"]
            assert sandbox_call(app, "echo", {"word": "x"})[1]["error"] is True
        finally:
            tools._spawn = original
        assert spawned == []


@_host_backend
def test_a_bad_python_pattern_is_an_error_not_a_crash():
    with tempfile.TemporaryDirectory() as tmp:
        world = build_world(Path(tmp))
        app, _ = app_for(world, facts=ABI5)
        assert "invalid pattern" in sandbox_call(app, "grep", {"pattern": "("})[1]["output"]


class Verifier:
    def __init__(self):
        self.fail, self.calls = None, 0

    async def start(self): ...

    async def stop(self): ...

    async def state(self):
        return "enrolled"

    async def mesh_id(self):
        return "mesh-1"

    async def pairing_offer(self):
        raise Refused("executor.enrolled")

    async def verify(self, ticket_json, action_json, effect):
        self.calls += 1
        if self.fail:
            raise Refused(self.fail)
        return Verified(mesh="mesh-1", device="device-1", turn="turn-1", agents=("kernel",),
                        action_digest=f"sha256:{self.calls:064d}")


def executor_stack(world: dict, process: bool = True):
    deployment = load_deployment(world["root"] / "deployment.toml")
    px = process_exec(world) if process else None
    app = sandbox.sandbox_app(str(world["package"]), token=SANDBOX_TOKEN, confine=False, process=px)
    runner = sandbox.RemoteRunner("http://sb", SANDBOX_TOKEN, 10, [])
    runner._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sb", headers=AUTH)
    verifier = Verifier()
    replays = Replays(world["root"] / ("spent-p.db" if process else "spent-c.db"))
    return deployment, verifier, executor_app(deployment, verifier, {"guide": runner}, OPERATOR,
                                              {"guide": agent_token(MASTER, "guide")}, replays)


def run_body(deployment, tool="bash", arguments=None, **over):
    arguments_json = json.dumps(arguments or {"command": "echo ran"})
    entry = next(t for t in catalog(deployment)["guide"]["tools"] if t["name"] == tool)
    action = {"v": 1, "mesh": "mesh-1", "device": "device-1", "turn": "turn-1", "agent": "kernel", "tool": tool,
              "tool_def": entry["definition"], "args_digest": args_digest(arguments_json), "influence": []}
    return {**{"agent": "guide", "tool": tool, "arguments_json": arguments_json, "action_json": json.dumps(action),
               "ticket_json": "{}", "workspace": None, "owner": "mesh.mesh-1"}, **over}


def with_executor(app, scenario):
    # One lifespan for the whole scenario, because the replay database closes when it ends.
    async def main():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ex") as http:
                return await scenario(http)
    return asyncio.run(main())


AGENT = {"Authorization": f"Bearer {agent_token(MASTER, 'guide')}"}


def counting_spawns():
    spawned = []
    original = tools._spawn

    async def counting(*args, **kwargs):
        spawned.append(1)
        return await original(*args, **kwargs)

    tools._spawn = counting
    return spawned, lambda: setattr(tools, "_spawn", original)


@_host_backend
def test_no_child_spawns_before_the_ticket_verifies_and_refusals_run_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        world = build_world(Path(tmp))
        deployment, verifier, app = executor_stack(world)
        spawned, restore = counting_spawns()
        changed = run_body(deployment)
        action = json.loads(changed["action_json"])
        action["tool_def"] = "sha256:" + "0" * 64
        changed["action_json"] = json.dumps(action)

        async def scenario(http):
            verifier.fail = "ticket.expired"
            refused = await http.post("/run", json=run_body(deployment), headers=AGENT)
            assert refused.status_code == 403 and refused.json()["code"] == "ticket.expired" and spawned == []
            verifier.fail = None
            assert (await http.post("/run", json=changed, headers=AGENT)).json()["code"] == "gate.tool_def_changed"
            assert spawned == []
            ok = await http.post("/run", json=run_body(deployment), headers=AGENT)
            assert ok.status_code == 200 and "ran" in ok.json()["output"] and spawned

        try:
            with_executor(app, scenario)
        finally:
            restore()


@_host_backend
def test_a_replayed_ticket_is_refused_in_process_mode():
    with tempfile.TemporaryDirectory() as tmp:
        world = build_world(Path(tmp))
        deployment, verifier, app = executor_stack(world)

        async def same(ticket_json, action_json, effect):
            return Verified(mesh="mesh-1", device="device-1", turn="turn-1", agents=("kernel",),
                            action_digest="sha256:" + "1" * 64)

        verifier.verify = same

        async def scenario(http):
            first = await http.post("/run", json=run_body(deployment), headers=AGENT)
            second = await http.post("/run", json=run_body(deployment), headers=AGENT)
            assert first.status_code == 200 and second.json()["code"] == "ticket.replayed"

        with_executor(app, scenario)


@_host_backend
def test_the_catalog_is_byte_identical_in_both_modes():
    with tempfile.TemporaryDirectory() as tmp:
        world = build_world(Path(tmp))
        listings = []
        for process in (False, True):
            _, _, app = executor_stack(world, process)
            async def scenario(http):
                return (await http.get("/tools", headers={"Authorization": f"Bearer {OPERATOR}"})).content

            listings.append(with_executor(app, scenario))
        assert listings[0] == listings[1] and b"echo" in listings[0]


def disk_world(tmp, turn, user):
    world = build_world(Path(tmp))
    os.environ["VESSEL_TURN_DISK"], os.environ["VESSEL_USER_DISK"] = str(turn), str(user)
    return world


@_host_backend
def test_the_turn_disk_limit_empties_the_workspace_and_the_end_sweep_removes_it():
    with tempfile.TemporaryDirectory() as tmp:
        world = disk_world(tmp, 300_000, 400_000)
        try:
            app, _ = app_for(world)
            key = "d" * 32
            command = "for i in 1 2 3 4 5; do head -c 100000 /dev/zero > f$i; done; echo wrote"
            out = sandbox_call(app, "bash", {"command": command}, key)[1]
            assert "stopped: the files went over" in out["output"] or out["error"], out
            folder = world["data"] / "exec" / f"turn-{key}"
            assert not folder.exists() or not any(folder.glob("f*"))
            sandbox_call(app, "bash", {"command": "echo a > keep"}, "e" * 32)
            kept = world["data"] / "exec" / f"turn-{'e' * 32}"
            assert (kept / "keep").exists()
            assert asyncio.run(post(app, "/end", {"workspace": "e" * 32})).status_code == 200
            assert not kept.exists()
        finally:
            os.environ.pop("VESSEL_TURN_DISK"), os.environ.pop("VESSEL_USER_DISK")


@_host_backend
def test_a_turn_belongs_to_the_user_whose_call_began_it():
    with tempfile.TemporaryDirectory() as tmp:
        world = build_world(Path(tmp))
        app, _ = app_for(world)
        key = "f" * 32
        assert sandbox_call(app, "bash", {"command": "true"}, key, ALICE)[0] == 200
        code, body = sandbox_call(app, "bash", {"command": "true"}, key, BOB)
        assert code == 403 and "another user" in body["output"]
        assert sandbox_call(app, "bash", {"command": "true"}, key, ALICE)[0] == 200


@_host_backend
def test_a_read_root_that_holds_the_node_data_is_refused_before_anything_starts():
    with tempfile.TemporaryDirectory() as tmp:
        world = build_world(Path(tmp))
        px = process_exec(world, reads=(real(world["root"]),))
        try:
            px.command(["/bin/echo"], {"PATH": "/usr/bin"}, (), str(world["data"] / "exec"), 5, 0)
        except ProcessRefused as exc:
            assert exc.code == "exec.policy_overlaps_data"
        else:
            raise AssertionError("the overlap was accepted")


def test_the_launcher_reports_refusals_as_one_json_line():
    line = json.dumps({"code": "exec.memory_limit", "message": "too much"})
    assert refusal_text(125, "noise\n" + line + "\n") == "exec.memory_limit: too much"
    assert refusal_text(0, line) is None and refusal_text(125, "no json") is None


@_host_backend
def test_a_call_past_its_time_limit_reports_a_timeout():
    with tempfile.TemporaryDirectory() as tmp:
        world = build_world(Path(tmp))
        manifest = world["package"] / "vessel.toml"
        manifest.write_text(manifest.read_text().replace("tool_timeout_seconds = 10", "tool_timeout_seconds = 2"))
        app, _ = app_for(world)
        out = sandbox_call(app, "bash", {"command": "sleep 20"})[1]
        assert "timed out" in out["output"] and out["error"] is True


@_host_backend
def test_environment_and_paths_come_from_the_variables_the_runtime_writes():
    with tempfile.TemporaryDirectory() as tmp:
        world = build_world(Path(tmp))
        env = {"VESSEL_EXEC_DATA": str(world["data"]), "VESSEL_NODE_DATA_DIR": str(world["node"]),
               "VESSEL_PACK_ROOT": str(world["root"] / "pack"), "VESSEL_MEMORY_BYTES": "123456789",
               "VESSEL_PIDS": "64", "VESSEL_RG_BIN": "/x/rg"}
        px = ProcessExec.from_environment(env)
        assert px.memory_bytes == 123456789 and px.pids == 64 and px.rg == "/x/rg"
        assert px.path_dirs[0].endswith("pack/python/bin") and px.run_py.endswith("astral_exec/run.py")
        assert px.resolve("/bin/sh") == "/bin/sh" and workspace_for("m", "t")


def grep_world(tmp):
    world = build_world(Path(tmp))
    outside = world["root"] / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside canary token\n")
    notes = world["work"] / "notes"
    (notes / "link.txt").symlink_to(outside / "secret.txt")
    (notes / "linkdir").symlink_to(outside, target_is_directory=True)
    (notes / "inside.txt").write_text("inside canary token\n")
    return world, notes


@_host_backend
def test_python_search_does_not_read_through_links_that_leave_the_root():
    with tempfile.TemporaryDirectory() as tmp:
        world, notes = grep_world(tmp)
        app, _ = app_for(world, facts=ABI5)
        found = sandbox_call(app, "grep", {"pattern": "canary"})[1]["output"]
        assert "inside.txt" in found and "outside" not in found and "secret.txt" not in found
        for link in ("link.txt", "linkdir"):
            assert sandbox_call(app, "grep", {"pattern": "canary", "path": str(notes / link)})[1][
                "output"] == "no matches"


@_host_backend
def test_a_file_swapped_for_a_link_after_the_walk_is_refused_at_open():
    with tempfile.TemporaryDirectory() as tmp:
        world, notes = grep_world(tmp)
        app, _ = app_for(world, facts=ABI5)
        target = notes / "swap.txt"
        target.write_text("harmless\n")
        original = tools.fnmatch.fnmatch

        def swap_then_match(name, pattern):
            if name == "swap.txt" and target.exists() and not target.is_symlink():
                target.unlink()
                target.symlink_to(world["root"] / "outside" / "secret.txt")
            return original(name, pattern)

        tools.fnmatch.fnmatch = swap_then_match
        try:
            out = sandbox_call(app, "grep", {"pattern": "canary", "glob": "*.txt"})[1]["output"]
        finally:
            tools.fnmatch.fnmatch = original
        assert target.is_symlink()
        assert "outside canary" not in out and "swap.txt" not in out


@_host_backend
def test_the_agent_probe_reports_the_backend_the_node_data_rule_and_the_limits():
    from astralvessel import execmode
    with tempfile.TemporaryDirectory() as tmp:
        world = build_world(Path(tmp))
        env = {"VESSEL_EXEC_DATA": str(world["data"]), "VESSEL_NODE_DATA_DIR": str(world["node"]),
               "VESSEL_PACKAGE": str(world["package"]), "VESSEL_PIDS": "64", "VESSEL_MEMORY_BYTES": str(1 << 30)}
        lines = execmode.agent_probe(env)
        names = {line["name"] for line in lines}
        assert "no read root holds the node data" in names and "the call limits come from the agent's profile" in names
        assert all(line.get("ok") or line.get("skip") for line in lines), lines
        env["VESSEL_READS"] = str(world["root"])
        lines = execmode.agent_probe(env)
        assert {"name": "no read root holds the node data", "ok": False} in lines


@_host_backend
def test_the_executor_checks_run_over_loopback_against_a_real_executor_app():
    from astralvessel import conformance
    with tempfile.TemporaryDirectory() as tmp:
        world = build_world(Path(tmp))
        deployment, verifier, app = executor_stack(world)
        verifier.fail = "ticket.forged"
        gen = world["root"] / "gen"
        gen.mkdir()
        (gen / "executor.env").write_text(f"VESSEL_EXECUTOR_TOKEN={OPERATOR}\n")
        (gen / "executor-agents.json").write_text(json.dumps({"guide": agent_token(MASTER, "guide")}))
        keys = world["root"] / "executor" / "keys"
        keys.mkdir(parents=True)
        (keys / "k").write_text("x")
        (keys / "k").chmod(0o600)

        class Layout:
            generated, executor_data = gen, world["root"] / "executor"

        class Runtime:
            ports = {"executor": 1}

        Runtime.deployment, Runtime.layout = deployment, Layout()

        def through_app(method, url, json=None, headers=None, timeout=None):
            async def go():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ex") as http:
                    return await http.request(method, url.removeprefix("http://127.0.0.1:1"), json=json,
                                              headers=headers)
            return asyncio.run(go())

        original = conformance.httpx.request
        conformance.httpx.request = through_app
        try:
            lines = conformance.executor_lines(Runtime())
        finally:
            conformance.httpx.request = original
        assert [line["name"] for line in lines] == [
            "refuses a caller without its token", "lists what its agents may run", "refuses a forged ticket",
            "refuses an unknown agent", "operator token cannot run a call", "keeps its keys private"]
        assert all(line["ok"] for line in lines), lines
        (keys / "k").chmod(0o644)
        assert conformance.executor_lines(Runtime())[-1]["ok"] is False
