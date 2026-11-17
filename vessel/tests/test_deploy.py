# Tests for deployment.toml, the generated Compose file and per-call Landlock confinement.

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
from importlib.metadata import version

import pytest

from astralvessel import confine
from astralvessel.compose import generate, inputs_hash, write
from astralvessel.deployment import egress_token, load_deployment, sandbox_token
from astralvessel.executor import agent_token, executor_token
from astralvessel.sandbox import LocalRunner, confinement_for

landlock = pytest.mark.skipif(confine.abi_version() < confine.REQUIRED_ABI, reason="kernel lacks Landlock ABI 6")

OWNER = '[owner]\nissuer = "https://keycloak.test/realms/astral"\nclient_id = "astral-app"\n'


def deployment_text(package, mounts='mounts = [{ target = "/data", source = "env:DATA_DIR" }]', head="",
                    egress=True) -> str:
    gateway = '[egress]\nupstream_url = "https://models.test/v1"\n' if egress else ""
    return f"""{head}
{OWNER}
{gateway}
[[agents]]
id = "guide"
package = "{package}"
[agents.sandbox]
{mounts}
"""


@pytest.fixture
def data_package(package_dir):
    manifest = package_dir / "vessel.toml"
    manifest.write_text(manifest.read_text().replace(
        "[sandbox]\n", '[sandbox]\nmounts = [{ source = "data", target = "/data", read_only = true }]\n'))
    return package_dir


def test_grants_must_match_what_the_agent_declares(data_package, tmp_path):
    path = tmp_path / "deployment.toml"
    path.write_text(deployment_text(data_package, mounts=""))
    with pytest.raises(ValueError, match="does not grant"):
        load_deployment(path)
    path.write_text(deployment_text(data_package, mounts=(
        'mounts = [{ target = "/data", source = "/v" }, { target = "/etc/x", source = "/x" }]')))
    with pytest.raises(ValueError, match="does not declare"):
        load_deployment(path)
    path.write_text(deployment_text(data_package, mounts=(
        'mounts = [{ target = "/data", source = "/v", read_only = false }]')))
    with pytest.raises(ValueError, match="writable mounts"):
        load_deployment(path)


def test_the_fingerprint_follows_the_sandbox_profile(data_package, tmp_path):
    path = tmp_path / "deployment.toml"
    path.write_text(deployment_text(data_package))
    before = load_deployment(path).agents["guide"].fingerprint
    path.write_text(deployment_text(data_package).replace('source = "env:DATA_DIR"', 'source = "/other"'))
    assert load_deployment(path).agents["guide"].fingerprint != before
    assert sandbox_token("m" * 16, "a") != sandbox_token("m" * 16, "b")


def test_an_edited_manifest_tool_or_deployment_recreates_what_loads_it(data_package, tmp_path):
    path = tmp_path / "deployment.toml"
    path.write_text(deployment_text(data_package))

    def services():
        spec = generate(load_deployment(path), repo_root=None)["services"]
        return spec["executor"]["labels"]["astralvessel.inputs"], spec["sandbox-guide"]["environment"][
            "VESSEL_PROFILE_HASH"]

    first = services()
    # The executor loads the package from its own mount; the same files must give the same fingerprint there.
    mount = tmp_path / "mount"
    shutil.copytree(data_package, mount / "guide")
    assert load_deployment(path, packages_root=str(mount)).agents["guide"].fingerprint == first[1]
    (data_package / "notes.md~").write_text("an editor backup")
    assert services() == first
    tool = data_package / "tools" / "echo.toml"
    tool.write_text(tool.read_text().replace("Echo the input", "Echo the input back"))
    edited = services()
    assert edited[0] != first[0] and edited[1] != first[1]
    (data_package / "tools" / "rank.toml").write_text(tool.read_text().replace('name = "echo"', 'name = "rank"'))
    manifest = data_package / "vessel.toml"
    manifest.write_text(manifest.read_text().replace('"tools/echo.toml"]', '"tools/echo.toml", "tools/rank.toml"]'))
    added = services()
    assert added[0] != edited[0] and added[1] != edited[1]
    path.write_text(path.read_text() + "\n# comment\n")
    redeployed = services()
    assert redeployed[0] != added[0] and redeployed[1] == added[1]
    assert inputs_hash(load_deployment(path)) == redeployed[0]


def test_compose_runs_a_personal_node_and_isolates_each_sandbox(data_package, tmp_path):
    path = tmp_path / "deployment.toml"
    path.write_text(deployment_text(data_package))
    deployment = load_deployment(path)
    spec = generate(deployment, repo_root=None)
    sandbox = spec["services"]["sandbox-guide"]
    assert sandbox["networks"] == ["tools-guide"] and spec["networks"]["tools-guide"]["internal"] is True
    assert sandbox["read_only"] and sandbox["cap_drop"] == ["ALL"]
    assert sandbox["environment"]["VESSEL_PROFILE_HASH"] == deployment.agents["guide"].fingerprint
    assert "${DATA_DIR:?" in sandbox["volumes"][1] and sandbox["volumes"][1].endswith(":/data:ro")
    node = spec["services"]["node"]
    assert node["image"] == "astralvessel-node:local" and "build" not in node
    assert node["ports"] == ["127.0.0.1:8001:8001"] and node["volumes"][0] == "node_data:/node-data"
    assert set(node["networks"]) == {"front", "outside", "egress"} and node["cap_drop"] == ["ALL"]
    assert not any(name.startswith("tools-") for name in node["networks"])
    env = node["environment"]
    assert env["ASTRAL_NODE_PROFILE"] == "personal" and env["ASTRAL_NODE_DATA_DIR"] == "/node-data/node"
    assert env["KEYCLOAK_AUTHORITY"] == "https://keycloak.test/realms/astral"
    assert env["KEYCLOAK_CLIENT_ID"] == "astral-app" and env["FF_A2A_SERVER"] == "true"
    assert env["ORCHESTRATOR_HOST"] == "0.0.0.0" and "DATABASE_URL" not in env and "ASTRAL_NODE_OWNER" not in env
    assert node["env_file"] == [".generated/node.env", ".generated/node-executor.env"]
    assert not any("postgres" in str(value).lower() for value in spec["services"].values())
    executor = spec["services"]["executor"]
    assert set(executor["networks"]) == {"front", "tools-guide"} and executor["read_only"]
    assert executor["networks"]["front"] == {"ipv4_address": "172.31.247.10"}
    assert executor["environment"]["VESSEL_HOST"] == "172.31.247.10"
    assert executor["environment"]["VESSEL_SANDBOX_TOKEN"].startswith("${VESSEL_SANDBOX_TOKEN:?")
    assert "outside" not in executor["networks"] and executor["env_file"] == [".generated/executor.env"]
    assert spec["services"]["egress"]["networks"] == ["egress", "outside"]
    released = version("astralvessel")
    assert executor["image"] == f"ghcr.io/astraldeep/astralvessel:{released}" and "build" not in executor
    assert spec["services"]["egress"]["image"] == f"ghcr.io/astraldeep/astralvessel-egress:{released}"
    with pytest.raises(SystemExit, match="needs the pinned kernel wheel"):
        generate(deployment, repo_root=tmp_path)
    artifacts = tmp_path / "kernel-artifacts"
    artifacts.mkdir()
    path.write_text(deployment_text(data_package, head='[executor]\nkernel_artifacts = "kernel-artifacts"\n'))
    with pytest.raises(SystemExit, match="holds no astral_kernel wheel"):
        generate(load_deployment(path), repo_root=tmp_path)
    (artifacts / "astral_kernel-0.1.0-py3-none-manylinux_2_36_aarch64.whl").write_bytes(b"w")
    built = generate(load_deployment(path), repo_root=tmp_path)["services"]
    assert built["executor"]["build"]["additional_contexts"] == {"kernel-artifacts": str(artifacts)}
    assert built["sandbox-guide"]["build"]["additional_contexts"] == {"kernel-artifacts": str(artifacts)}
    assert built["executor"]["image"] == "astralvessel:local"
    assert built["executor"]["build"]["context"] == str(tmp_path)
    assert built["egress"]["build"]["context"] == str(tmp_path / "egress")
    assert spec["services"]["egress"]["env_file"] == [".generated/egress.env"]
    shared = 'mounts = [{ target = "/data", source = "volume:shared-corpus" }]'
    path.write_text(deployment_text(data_package, mounts=shared))
    named = generate(load_deployment(path), repo_root=None)
    assert named["services"]["sandbox-guide"]["volumes"][1] == "shared-corpus:/data:ro"
    assert named["volumes"]["shared-corpus"] == {"external": True}


def test_the_node_runs_without_a_gateway_and_binds_the_owner_when_told_the_subject(data_package, tmp_path):
    path = tmp_path / "deployment.toml"
    path.write_text(deployment_text(data_package, egress=False).replace(
        'client_id = "astral-app"', 'client_id = "astral-app"\nsubject = "owner-1"\nallowed_azp = ["a", "b"]\n'
        'accepted_issuers = ["https://other.test/realms/x/"]') + "")
    deployment = load_deployment(path)
    spec = generate(deployment, repo_root=None)
    assert "egress" not in spec["services"] and "egress" not in spec["networks"]
    assert "egress" not in spec["services"]["node"]["networks"]
    env = spec["services"]["node"]["environment"]
    assert env["ASTRAL_NODE_OWNER"] == deployment.owner.owner_id
    assert env["ASTRAL_NODE_OWNER"].startswith("oidc.") and env["ASTRAL_NODE_OWNER"].endswith(".owner-1")
    assert env["KEYCLOAK_ALLOWED_AZP"] == "a,b" and env["ASTRAL_ACCEPTED_ISSUERS"] == "https://other.test/realms/x"
    assert env["PUBLIC_BASE_URL"] == "http://127.0.0.1:8001"


def test_the_node_section_validates_what_it_publishes_and_what_it_may_set(data_package, tmp_path):
    path = tmp_path / "deployment.toml"
    good = '[node]\nimage = "registry.test/node:1"\nport = 9001\npublic_url = "https://node.test"\n' \
           '[node.environment]\nASTRAL_ENV = "development"\n'
    path.write_text(good + deployment_text(data_package))
    spec = generate(load_deployment(path), repo_root=None)["services"]["node"]
    assert spec["image"] == "registry.test/node:1" and spec["ports"] == ["127.0.0.1:9001:8001"]
    assert spec["environment"]["ASTRAL_ENV"] == "development"
    assert spec["environment"]["PUBLIC_BASE_URL"] == "https://node.test"
    for bad in ('[node.environment]\nDATABASE_URL = "postgresql://x"\n', '[node.environment]\nMY_API_KEY = "x"\n',
                '[node.environment]\nlower = "x"\n', '[node.environment]\nFOO = "a\\nb"\n',
                '[node]\npublic_url = "https://node.test/path"\n', '[node]\nbind = "not-an-ip"\n',
                '[node]\nproject = "Bad Name"\n', '[node]\nimage = "has space"\n'):
        path.write_text(bad + deployment_text(data_package))
        with pytest.raises(ValueError):
            load_deployment(path)
    path.write_text(deployment_text(data_package).replace(OWNER, '[owner]\nissuer = "ftp://x"\nclient_id = "c"\n'))
    with pytest.raises(ValueError, match="issuer"):
        load_deployment(path)


def test_secrets_stay_out_of_the_compose_file_and_in_private_files(data_package, tmp_path):
    path = tmp_path / "deployment.toml"
    path.write_text(deployment_text(data_package))
    deployment = load_deployment(path)
    master = "master-secret-123456"
    out = tmp_path / "compose.generated.yml"
    written = write(deployment, out, repo_root=None, master_token=master)
    text = out.read_text()
    assert master not in text
    folder = tmp_path / ".generated"
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    for name in ("node", "executor", "egress", "sandbox-guide"):
        assert stat.S_IMODE(os.stat(folder / f"{name}.env").st_mode) == 0o600
    assert sandbox_token(master, "guide") in (folder / "sandbox-guide.env").read_text()
    assert f"VESSEL_EXECUTOR_TOKEN={executor_token(master)}\n" == (folder / "executor.env").read_text()
    assert f"EGRESS_TOKEN={egress_token(master)}\n" == (folder / "egress.env").read_text()
    handed = json.loads((folder / "executor-agents.json").read_text())
    assert handed == {"guide": agent_token(master, "guide")}
    assert (folder / "executor-agents.json").stat().st_mode & 0o777 == 0o644 and folder.stat().st_mode & 0o77 == 0
    assert (folder / "node-executor.env").read_text() == f"ASTRAL_VESSEL_EXECUTOR_TOKEN={executor_token(master)}\n"
    assert (folder / "node-executor.env").stat().st_mode & 0o77 == 0
    tokens = {master, sandbox_token(master, "guide"), executor_token(master), egress_token(master),
              agent_token(master, "guide"), agent_token(master, "other")}
    assert len(tokens) == 6
    held = (folder / "node.env").read_text()
    names = [line.split("=")[0] for line in held.splitlines()]
    assert names == ["WEB_SESSION_ENC_KEY", "CREDENTIAL_ENCRYPTION_KEY", "AUDIT_HMAC_SECRET", "AGENT_API_KEY"]
    for line in held.splitlines():
        assert line.split("=", 1)[1] not in text
    write(deployment, out, repo_root=None, master_token=master)
    assert (folder / "node.env").read_text() == held  # the node's secrets survive a redeploy
    assert len(written) == 7


def test_a_gatewayless_deployment_writes_no_egress_secret(data_package, tmp_path):
    path = tmp_path / "deployment.toml"
    path.write_text(deployment_text(data_package, egress=False))
    write(load_deployment(path), tmp_path / "compose.generated.yml", repo_root=None, master_token="m" * 16)
    assert not (tmp_path / ".generated" / "egress.env").exists()


@landlock
async def test_concurrent_calls_cannot_see_each_other(package_dir, tmp_path):
    scratch = tmp_path / "calls"
    scratch.mkdir()
    from astralvessel.manifest import load_package
    package = load_package(package_dir)
    runner = LocalRunner(package, confinement_for(package, str(scratch)))
    holder = asyncio.create_task(runner.run("bash", {"command": "echo private > $TMPDIR/note; sleep 3"}))
    await asyncio.sleep(0.5)
    peek, _ = await runner.run("bash", {"command": f"cat {scratch}/*/note; ls {scratch}; ls /proc | head -1"})
    assert "private" not in peek and peek.count("Permission denied") >= 2
    own, failed = await runner.run("bash", {"command": "echo mine > $TMPDIR/x && cat $TMPDIR/x"})
    assert own.startswith("mine") and not failed
    data, _ = await runner.run("bash", {"command": "cat notes/ada-lovelace.md"})
    assert "Analytical engine" in data
    home, _ = await runner.run("bash", {"command": f"cat {os.path.expanduser('~')}/.bashrc"})
    assert "Permission denied" in home or "No such file" in home
    tcp, failed = await runner.run("bash", {"command": "python3 -c \"import socket; socket.create_connection(("
                                                       "'127.0.0.1', 22), 2)\""})
    assert failed and "PermissionError" in tcp
    outside, failed = await runner.run("read", {"path": "/etc/hostname"})
    assert failed and "outside" in outside
    climb, failed = await runner.run("glob", {"pattern": "../*"})
    assert failed and ".." in climb
    sock, failed = await runner.run("bash", {"command": "python3 -c \"import socket,os; socket.socket("
                                                        "socket.AF_UNIX).bind(os.environ['TMPDIR'] + '/s')\""})
    assert failed and "PermissionError" in sock
    shm, failed = await runner.run("bash", {"command": "python3 -c \"import ctypes; l = ctypes.CDLL(None, "
                                                       "use_errno=True); print(l.shmget(0, 4096, 0o1600), "
                                                       "ctypes.get_errno())\""})
    assert shm.split()[:2] == ["-1", "1"]
    udp, failed = await runner.run("bash", {"command": "python3 -c \"import socket; socket.socket(socket.AF_INET, "
                                                       "socket.SOCK_DGRAM)\""})
    assert failed and "PermissionError" in udp
    pair, failed = await runner.run("bash", {"command": "python3 -c \"import socket; socket.socketpair(); "
                                                        "print('ok')\""})
    assert pair.startswith("ok") and not failed
    await holder
    assert list(scratch.iterdir()) == []


@landlock
async def test_calls_in_one_turn_share_a_workspace_that_other_turns_cannot_see(package_dir, tmp_path):
    scratch = tmp_path / "calls"
    scratch.mkdir()
    from astralvessel.manifest import load_package
    package = load_package(package_dir)
    runner = LocalRunner(package, confinement_for(package, str(scratch)))
    turn_a, turn_b = "a" * 32, "b" * 32
    _, failed = await runner.run("bash", {"command": "echo kept > $TMPDIR/table.rds"}, turn_a)
    assert not failed
    later, failed = await runner.run("bash", {"command": "cat $TMPDIR/table.rds"}, turn_a)
    assert later.startswith("kept") and not failed
    read, failed = await runner.run("read", {"path": str(scratch / f"turn-{turn_a}" / "table.rds")}, turn_a)
    assert "kept" in read and not failed
    other, _ = await runner.run("bash", {"command": f"cat {scratch}/turn-{turn_a}/table.rds; ls {scratch}"}, turn_b)
    assert "kept" not in other and other.count("Permission denied") >= 2
    other, failed = await runner.run("read", {"path": str(scratch / f"turn-{turn_a}" / "table.rds")}, turn_b)
    assert failed and "outside" in other
    unkeyed, _ = await runner.run("bash", {"command": f"cat {scratch}/turn-{turn_a}/table.rds"})
    assert "kept" not in unkeyed
    await runner.end_turn(turn_a)
    assert sorted(p.name for p in scratch.iterdir()) == [f"turn-{turn_b}"]
    await runner.end_turn(turn_b)
    assert list(scratch.iterdir()) == []
    _, failed = await runner.run("bash", {"command": "true"}, "../escape" + "0" * 23)
    assert failed and list(scratch.iterdir()) == []


def test_stale_workspaces_are_swept(tmp_path):
    from astralvessel.tools import Confinement, sweep_workspaces
    confinement = Confinement(readable=(), scratch_root=str(tmp_path))
    old, fresh, other = tmp_path / ("turn-" + "a" * 32), tmp_path / ("turn-" + "b" * 32), tmp_path / "call-x"
    for path in (old, fresh, other):
        path.mkdir()
    os.utime(old, (4102444800, 4102444800))  # a call can set its workspace's times; the sweep ignores them
    sweep_workspaces(confinement, {"b" * 32})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["call-x", fresh.name]


@pytest.mark.parametrize("tool", ["read", "list", "glob"])
def test_in_server_tools_check_where_the_opened_path_really_points(tmp_path, tool):
    # A call can swap a link after the path check; the tools check the opened descriptor too.
    from astralvessel.tools import Confinement, _glob, _list, _read
    data, outside = tmp_path / "data", tmp_path / "outside"
    data.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (data / "link").symlink_to(outside / "secret.txt" if tool == "read" else outside)
    confinement = Confinement(readable=(str(data),), scratch_root=str(tmp_path / "calls"))
    call = {"read": lambda: _read({"path": "link"}, str(data), confinement),
            "list": lambda: _list({"path": "link"}, str(data), confinement),
            "glob": lambda: _glob({"pattern": "*", "path": "link"}, str(data), 5, confinement)}[tool]
    with pytest.raises(PermissionError):
        asyncio.run(call())


def test_two_deployments_on_one_host_can_differ_in_project_subnet_and_port(package_dir, tmp_path):
    path = tmp_path / "deployment.toml"
    path.write_text(deployment_text(package_dir, mounts=""))
    assert generate(load_deployment(path), repo_root=None)["name"] == "astralvessel"
    second = '[node]\nproject = "second"\nport = 8002\nfront_subnet = "172.31.248.0/24"\n' \
             'front_address = "172.31.248.10"\n'
    path.write_text(second + deployment_text(package_dir, mounts=""))
    spec = generate(load_deployment(path), repo_root=None)
    assert spec["name"] == "second" and spec["networks"]["front"]["ipam"]["config"] == [
        {"subnet": "172.31.248.0/24", "ip_range": "172.31.248.128/25"}]
    assert spec["services"]["node"]["ports"] == ["127.0.0.1:8002:8001"]
    for bad in ('[node]\nfront_address = "10.0.0.5"\n', '[node]\nfront_address = "172.31.247.200"\n',
                '[node]\nfront_subnet = "172.31.248.0/30"\nfront_address = "172.31.248.1"\n'):
        path.write_text(bad + deployment_text(package_dir, mounts=""))
        with pytest.raises(ValueError):
            load_deployment(path)


def test_a_second_project_keeps_its_own_images_and_folder(package_dir, tmp_path):
    path = tmp_path / "deployment.toml"
    second = '[node]\nproject = "second"\nfront_subnet = "172.31.248.0/24"\nfront_address = "172.31.248.10"\n'
    kernel = '[executor]\nkernel_artifacts = "kernel"\n'
    path.write_text(second + kernel + deployment_text(package_dir, mounts=""))
    repo = tmp_path / "repo"
    (repo / "egress").mkdir(parents=True)
    (tmp_path / "kernel").mkdir()
    (tmp_path / "kernel" / "astral_kernel-0.1.0-py3-none-manylinux_2_36_aarch64.whl").write_bytes(b"w")
    services = generate(load_deployment(path), repo_root=repo)["services"]
    assert {services[s]["image"] for s in ("executor", "egress", "sandbox-guide")} == {
        "second:local", "second-egress:local"}
    out = tmp_path / "compose.generated.yml"
    write(load_deployment(path), out, repo_root=None, master_token="m" * 16)
    path.write_text(deployment_text(package_dir, mounts=""))
    with pytest.raises(SystemExit, match="second"):
        write(load_deployment(path), out, repo_root=None, master_token="m" * 16)


def test_the_gateway_endpoint_is_checked_and_may_come_from_the_environment(package_dir, tmp_path):
    path = tmp_path / "deployment.toml"
    for bad in ('"ftp://x"', '"env:"', '"http://"', '"https://x y"'):
        path.write_text(deployment_text(package_dir, mounts="").replace('"https://models.test/v1"', bad))
        with pytest.raises(ValueError, match="http"):
            load_deployment(path)
    path.write_text(deployment_text(package_dir, mounts="").replace('"https://models.test/v1"', '"HTTPS://m.test/v1"'))
    load_deployment(path)
    path.write_text(deployment_text(package_dir, mounts="").replace('"https://models.test/v1"', '"env:MODEL_URL"'))
    egress = generate(load_deployment(path), repo_root=None)["services"]["egress"]["environment"]
    assert egress["LLM_BASE_URL"].startswith("${MODEL_URL:?")


def test_the_node_is_told_where_the_executor_is_and_given_its_tokens(data_package, tmp_path):
    path = tmp_path / "deployment.toml"
    path.write_text(deployment_text(data_package))
    node = generate(load_deployment(path), repo_root=None)["services"]["node"]
    assert node["environment"]["ASTRAL_VESSEL_EXECUTOR_URL"] == "http://172.31.247.10:8700"
    assert node["environment"]["ASTRAL_VESSEL_EXECUTOR_AGENT_TOKENS_FILE"] == "/run/secrets/executor-agents.json"
    assert "ASTRAL_VESSEL_EXECUTOR_TOKEN" not in node["environment"]
    assert node["env_file"] == [".generated/node.env", ".generated/node-executor.env"]
    assert "./.generated/executor-agents.json:/run/secrets/executor-agents.json:ro" in node["volumes"]
    assert "front" in node["networks"]


def test_a_node_without_agents_is_not_pointed_at_an_executor(tmp_path):
    path = tmp_path / "deployment.toml"
    path.write_text(OWNER)
    node = generate(load_deployment(path), repo_root=None)["services"]["node"]
    assert "ASTRAL_VESSEL_EXECUTOR_URL" not in node["environment"] and node["env_file"] == [".generated/node.env"]
    assert node["volumes"] == ["node_data:/node-data"]


def test_the_executor_token_file_is_rewritten_on_every_deploy_but_the_node_secrets_are_kept(data_package, tmp_path):
    path = tmp_path / "deployment.toml"
    path.write_text(deployment_text(data_package))
    out = tmp_path / "compose.generated.yml"
    write(load_deployment(path), out, repo_root=None, master_token="a" * 20)
    held = (tmp_path / ".generated" / "node.env").read_text()
    write(load_deployment(path), out, repo_root=None, master_token="b" * 20)
    assert (tmp_path / ".generated" / "node.env").read_text() == held
    assert (tmp_path / ".generated" / "node-executor.env").read_text() == (
        f"ASTRAL_VESSEL_EXECUTOR_TOKEN={executor_token('b' * 20)}\n")
    assert json.loads((tmp_path / ".generated" / "executor-agents.json").read_text()) == {
        "guide": agent_token("b" * 20, "guide")}


def test_the_node_owner_must_be_the_one_the_deployment_names(data_package, tmp_path):
    path = tmp_path / "deployment.toml"
    path.write_text(deployment_text(data_package).replace(
        'client_id = "astral-app"', 'client_id = "astral-app"\nsubject = "owner-1"'))
    deployment = load_deployment(path)
    env = generate(deployment, repo_root=None)["services"]["node"]["environment"]
    assert env["ASTRAL_NODE_OWNER"] == deployment.owner.owner_id
    path.write_text(deployment_text(data_package))
    unbound = load_deployment(path)
    unbound.node.environment["ASTRAL_NODE_OWNER"] = "oidc.0000000000000000.other"
    with pytest.raises(SystemExit, match="ASTRAL_NODE_OWNER"):
        generate(unbound, repo_root=None)
