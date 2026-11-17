# The operator commands without Docker: writing a deployment from prompts or flags, waiting for the node and the
# sandboxes, the node checks, the node image check and pairing the executor.

from __future__ import annotations

import argparse
import json
import subprocess
import tomllib

import pytest

from astralvessel import operate
from astralvessel.deployment import load_deployment

ISSUER = "https://keycloak.test/realms/astral"


def options(**given) -> argparse.Namespace:
    base = {"deployment": None, "example": False, "agent": None, "mount": None, "issuer": None, "client_id": None,
            "subject": None, "image": None, "port": None, "upstream_url": None, "api_key": None, "timeout": 1.0}
    return argparse.Namespace(**{**base, **given})


@pytest.fixture(autouse=True)
def no_docker(monkeypatch):
    monkeypatch.setattr(operate, "_docker", lambda *args: "")


def test_without_a_terminal_it_names_every_missing_value():
    with pytest.raises(SystemExit) as stopped:
        operate.gather(options(), interactive=False)
    message = str(stopped.value)
    for flag in ("--issuer", "--client-id"):
        assert flag in message


def test_without_a_terminal_an_agents_mounts_are_required(package_dir):
    (package_dir / "vessel.toml").write_text((package_dir / "vessel.toml").read_text().replace(
        "[sandbox]", '[sandbox]\nmounts = [{ source = "notes", target = "/notes" }]'))
    with pytest.raises(SystemExit, match="--mount agent:/notes=FOLDER"):
        operate.gather(options(agent=[str(package_dir)], issuer=ISSUER, client_id="c"), interactive=False)


def test_a_key_on_the_command_line_is_refused_and_env_names_must_be_set(monkeypatch):
    with pytest.raises(SystemExit, match="env:NAME"):
        operate.gather(options(api_key="sk-literal", upstream_url="https://m.test/v1"), interactive=False)
    monkeypatch.delenv("UNSET_PROVIDER_KEY", raising=False)
    with pytest.raises(SystemExit, match="not set"):
        operate.gather(options(api_key="env:UNSET_PROVIDER_KEY", upstream_url="https://m.test/v1"),
                       interactive=False)
    with pytest.raises(SystemExit, match="http"):
        operate.gather(options(issuer="ftp://nope"), interactive=False)
    with pytest.raises(SystemExit, match="upstream-url"):
        operate.gather(options(api_key="env:HOME", issuer=ISSUER, client_id="c"), interactive=False)


def test_flags_write_a_deployment_that_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_PROVIDER_KEY", "sk-from-env")
    answers = operate.gather(options(example=True, issuer=ISSUER + "/", client_id="astral-app", subject="owner-1",
                                     image="registry.test/node:7", port=9001, upstream_url="https://m.test/v1",
                                     api_key="env:MY_PROVIDER_KEY"), interactive=False)
    folder = tmp_path / "demo"
    folder.mkdir()
    path = operate.write_deployment(folder, answers, example=True)
    deployment = load_deployment(path)
    assert deployment.owner.issuer == ISSUER and deployment.owner.owner_id.endswith(".owner-1")
    assert deployment.node.image == "registry.test/node:7" and deployment.node.port == 9001
    assert deployment.egress.upstream_key == "env:MY_PROVIDER_KEY" and list(deployment.agents) == ["field-guide"]
    assert (folder / "agents" / "field-guide" / "vessel.toml").exists() and (folder / "data").is_dir()
    env = (folder / ".env").read_text()
    assert env.startswith("# Secrets") and "VESSEL_SANDBOX_TOKEN='" in env and "sk-from-env" not in env
    assert oct((folder / ".env").stat().st_mode & 0o777) == "0o600"
    assert tomllib.loads(path.read_text())["node"]["project"] == "demo"


def test_a_node_alone_is_a_valid_deployment(tmp_path):
    answers = operate.gather(options(issuer=ISSUER, client_id="astral-app"), interactive=False)
    path = operate.write_deployment(tmp_path, answers, example=False)
    deployment = load_deployment(path)
    assert deployment.agents == {} and deployment.egress.upstream_url is None
    assert "[egress]" not in path.read_text() and "image" not in tomllib.loads(path.read_text())["node"]


def test_prompts_ask_for_the_issuer_and_client_and_retry_a_bad_issuer(monkeypatch, capsys):
    answers = iter(["not a url", ISSUER + "/", "astral-app"])
    monkeypatch.setattr(operate, "_ask", lambda prompt, secret=False: next(answers))
    got = operate.gather(options(), interactive=True)
    assert (got.issuer, got.client_id) == (ISSUER, "astral-app")
    assert "give an http(s) issuer URL" in capsys.readouterr().out


def test_prompts_ask_for_a_missing_mount_folder_until_it_exists(package_dir, tmp_path, monkeypatch, capsys):
    (package_dir / "vessel.toml").write_text((package_dir / "vessel.toml").read_text().replace(
        "[sandbox]", '[sandbox]\nmounts = [{ source = "notes", target = "/notes" }]'))
    answers = iter([str(tmp_path / "missing"), str(tmp_path)])
    monkeypatch.setattr(operate, "_ask", lambda prompt, secret=False: next(answers))
    got = operate.gather(options(agent=[str(package_dir)], issuer=ISSUER, client_id="c"), interactive=True)
    assert got.mounts[(0, "/notes")] == str(tmp_path) and "is not a folder" in capsys.readouterr().out


def test_a_free_subnet_and_project_name_are_chosen(monkeypatch, tmp_path):
    def docker(*args):
        if args == ("network", "ls", "-q"):
            return "n1\n"
        if args[:2] == ("network", "inspect"):
            return "172.31.247.0/24 172.17.0.0/16"
        if args[0] == "ps" and "label=com.docker.compose.project=demo" in args:
            return "/somewhere/else\n"
        return ""
    monkeypatch.setattr(operate, "_docker", docker)
    assert str(operate.pick_subnet()) == "172.31.246.0/24"
    assert operate.pick_project(tmp_path / "demo") == "demo-2"
    assert operate.pick_project(tmp_path / "My Stack!") == "my-stack"


class FakeStack:
    def __init__(self, health: list[str], states: str = "node running\n"):
        self.health, self.states, self.logged = health, states, []

    def python(self, service, source, interpreter="python", timeout=300):
        out = self.health.pop(0) if self.health else ""
        return subprocess.CompletedProcess([], 0, stdout=out, stderr="")

    def compose(self, *args, **kw):
        return subprocess.CompletedProcess([], 0, stdout=self.states, stderr="")

    def logs(self, service, tail=40):
        self.logged.append(service)
        return "log lines"


def sandbox_state(profile="p", confined=True):
    return json.dumps({"ok": True, "confined": confined, "profile": profile})


def two_agents():
    return argparse.Namespace(
        agents={"one": argparse.Namespace(fingerprint="p"), "two": argparse.Namespace(fingerprint="p")},
        node=argparse.Namespace())


def test_waiting_reports_the_node_and_agents_that_never_became_ready(monkeypatch, capsys):
    monkeypatch.setattr(operate.time, "sleep", lambda s: None)
    monkeypatch.setattr(operate.node, "health", lambda section: "ok")
    deployment = two_agents()
    stack = FakeStack([sandbox_state(), sandbox_state(confined=False)] * 3)
    clock = iter([0, 0.2, 0.4, 0.6, 2, 3])
    monkeypatch.setattr(operate.time, "monotonic", lambda: next(clock))
    assert operate.wait_ready(stack, deployment, timeout=1) is False
    assert "not ready after 1s: two" in capsys.readouterr().out and stack.logged == ["sandbox-two"]
    monkeypatch.setattr(operate.time, "monotonic", lambda: 0)
    assert operate.wait_ready(FakeStack([sandbox_state(), sandbox_state()]), deployment, timeout=1) is True
    stale = FakeStack([sandbox_state(), sandbox_state(profile="old")] * 2)
    clock = iter([0, 0.5, 2, 3])
    monkeypatch.setattr(operate.time, "monotonic", lambda: next(clock))
    assert operate.wait_ready(stale, deployment, timeout=1) is False


def test_waiting_reports_a_node_that_is_not_ready_and_dead_containers(monkeypatch, capsys):
    monkeypatch.setattr(operate.time, "sleep", lambda s: None)
    monkeypatch.setattr(operate.node, "health", lambda section: "/readyz answered HTTP 503")
    deployment = two_agents()
    clock = iter([0, 0.5, 2, 3])
    monkeypatch.setattr(operate.time, "monotonic", lambda: next(clock))
    slow = FakeStack(["not json", "not json"] * 2)
    assert operate.wait_ready(slow, deployment, timeout=1) is False
    assert "not ready after 1s: node, one, two" in capsys.readouterr().out and slow.logged[0] == "node"
    monkeypatch.setattr(operate.time, "monotonic", lambda: 0)
    dead = FakeStack(["not json", "not json"], states="node running\nsandbox-two exited\n")
    assert operate.wait_ready(dead, deployment, timeout=1) is False and dead.logged == ["sandbox-two"]


def test_the_node_checks_name_every_failure(tmp_path, monkeypatch, capsys):
    path = operate.write_deployment(tmp_path, operate.Answers(issuer=ISSUER, client_id="c"), example=False)
    deployment = load_deployment(path)
    good = {"format": "astral.node.descriptor/v1", "node_id": "n", "node_key": {"crv": "Ed25519"},
            "accepted_issuers": [ISSUER], "capabilities": ["chat", "tool-dispatch"], "owner_hash": None}
    monkeypatch.setattr(operate.node, "health", lambda section: "ok")
    monkeypatch.setattr(operate.node, "fetch_descriptor", lambda section: good)
    modes = {"directory": "0o700", "plane.sqlite3": "0o600", "node-ed25519.pem": "0o600"}
    assert operate.node_checks(FakeStack([json.dumps(modes)]), deployment) == []
    assert capsys.readouterr().out.count("PASS") == 5
    monkeypatch.setattr(operate.node, "health", lambda section: "node is down")
    monkeypatch.setattr(operate.node, "fetch_descriptor", lambda section: {**good, "accepted_issuers": []})
    loose = {"directory": "0o755", "plane.sqlite3": "0o644", "node-ed25519.pem": "0o644"}
    failed = operate.node_checks(FakeStack([json.dumps(loose)]), deployment)
    assert len(failed) == 5 and "node: answers /readyz (node is down)" in capsys.readouterr().out
    assert len(operate.node_checks(FakeStack(["garbage"]), deployment)) == 4


def test_a_missing_node_image_says_how_to_get_one(tmp_path, monkeypatch):
    path = operate.write_deployment(tmp_path, operate.Answers(issuer=ISSUER, client_id="c"), example=False)
    deployment = load_deployment(path)
    with pytest.raises(SystemExit, match="astralvessel-node:local is not on this machine"):
        operate.require_image(deployment)
    monkeypatch.setattr(operate, "_docker", lambda *args: "sha256:abc\n")
    operate.require_image(deployment)


def _named(package_dir, tmp_path, name):
    import shutil
    copy = tmp_path / f"pkg-{name}-{len(list(tmp_path.iterdir()))}"
    shutil.copytree(package_dir, copy)
    manifest = copy / "vessel.toml"
    manifest.write_text(manifest.read_text().replace('name = "tester"', f'name = "{name}"'))
    return copy


def test_mount_folders_are_expanded_and_must_exist(package_dir, tmp_path, monkeypatch):
    (package_dir / "vessel.toml").write_text((package_dir / "vessel.toml").read_text().replace(
        "[sandbox]", '[sandbox]\nmounts = [{ source = "notes", target = "/notes" }]'))
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "notes").mkdir()
    base = {"agent": [str(package_dir)], "issuer": ISSUER, "client_id": "c"}
    got = operate.gather(options(**base, mount=["agent:/notes=~/notes"]), interactive=False)
    assert got.mounts[(0, "/notes")] == str(tmp_path / "notes")
    with pytest.raises(SystemExit, match="is not a folder"):
        operate.gather(options(**base, mount=["agent:/notes=~/missing"]), interactive=False)
    with pytest.raises(SystemExit, match="use AGENT_FOLDER"):
        operate.gather(options(**base, mount=["nonsense"]), interactive=False)
    with pytest.raises(SystemExit, match="is not one of the agents"):
        operate.gather(options(**base, mount=["other:/notes=~/notes"]), interactive=False)


def test_the_example_is_never_copied_over_existing_folders(tmp_path):
    answers = operate.gather(options(example=True, issuer=ISSUER, client_id="c"), interactive=False)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "mine.md").write_text("mine")
    with pytest.raises(SystemExit, match="already exist"):
        operate.write_deployment(tmp_path, answers, example=True)
    assert [p.name for p in (tmp_path / "data").iterdir()] == ["mine.md"] and not (tmp_path / ".env").exists()


def test_agent_ids_stay_unique_and_the_gitignore_is_extended(package_dir, tmp_path):
    agents = [_named(package_dir, tmp_path, n) for n in ("x-2", "x", "x")]
    answers = operate.Answers(agents=agents, issuer=ISSUER, client_id="c")
    folder = tmp_path / "deploy"
    folder.mkdir()
    (folder / ".gitignore").write_text("node_modules/")
    operate.write_deployment(folder, answers, example=False)
    assert list(load_deployment(folder / "deployment.toml").agents) == ["x-2", "x", "x-3"]
    assert (folder / ".gitignore").read_text() == "node_modules/\n.env\n.generated/\ncompose.generated.yml\n"


def test_a_key_with_shell_characters_survives_the_env_file(tmp_path, monkeypatch):
    from astralvessel.stack import load_env_file
    answers = operate.Answers(issuer=ISSUER, client_id="c", upstream_url="https://m.test/v1", api_key="ab$cd#x y")
    operate.write_deployment(tmp_path, answers, example=False)
    monkeypatch.delenv("VESSEL_UPSTREAM_MODEL_KEY", raising=False)
    load_env_file(tmp_path / ".env")
    assert operate.os.environ["VESSEL_UPSTREAM_MODEL_KEY"] == "ab$cd#x y"
    assert operate._check_key("it's") and operate._check_key("a\nb")


def test_a_failed_write_leaves_nothing_behind(tmp_path, monkeypatch):
    def broken(path):
        raise OSError("disk went away")
    monkeypatch.setattr(operate, "load_deployment", broken)
    with pytest.raises(OSError):
        operate.write_deployment(tmp_path, operate.Answers(issuer=ISSUER, client_id="c"), example=False)
    assert not (tmp_path / ".env").exists() and not (tmp_path / "deployment.toml").exists()


def test_a_stopped_deployments_name_is_not_reused(monkeypatch, tmp_path):
    def docker(*args):
        if args[0] == "volume" and "label=com.docker.compose.project=demo" in args:
            return "demo_node_data\n"
        return ""
    monkeypatch.setattr(operate, "_docker", docker)
    assert operate.pick_project(tmp_path / "demo") == "demo-2"


def test_the_executor_offer_is_printed_or_explained(tmp_path, monkeypatch, capsys):
    path = operate.write_deployment(tmp_path, operate.Answers(issuer=ISSUER, client_id="c"), example=False)
    (tmp_path / "compose.generated.yml").write_text("services: {}\n")
    seen = []

    def python(self, service, source, interpreter="python", timeout=300):
        seen.append(service)
        return subprocess.CompletedProcess([], 0, stdout=replies.pop(0), stderr="")

    monkeypatch.setattr(operate.Stack, "python", python)
    args = argparse.Namespace(deployment=str(path), executor=True, label=None, token_file=None)
    replies = [json.dumps({"status": 200, "code": "1234 5678", "payload": "PAYLOAD", "expires_ms": 1})]
    operate.pair(args)
    printed = capsys.readouterr().out
    assert "1234 5678" in printed and "PAYLOAD" in printed and seen == ["executor"]
    for code, text in (("executor.waiting_for_node", "pair the node first"),
                       ("executor.enrolled", "already in the owner's mesh"),
                       ("executor.no_kernel", "no kernel binding"), ("executor.odd", "executor.odd")):
        replies = [json.dumps({"status": 409, "code": code})]
        with pytest.raises(SystemExit, match=text):
            operate.pair(args)
    replies = ["not json"]
    with pytest.raises(SystemExit, match="did not answer"):
        operate.pair(args)


def test_check_can_skip_the_node_checks(tmp_path, monkeypatch):
    path = operate.write_deployment(tmp_path, operate.Answers(issuer=ISSUER, client_id="c"), example=False)
    (tmp_path / "compose.generated.yml").write_text("services: {}\n")
    monkeypatch.setenv("VESSEL_SANDBOX_TOKEN", "m" * 16)
    monkeypatch.setattr(operate, "run_probes", lambda *a: argparse.Namespace(passed=3, failed=[], skipped=0))

    def boom(*args):
        raise AssertionError("node checks must not run")

    monkeypatch.setattr(operate, "node_checks", boom)
    operate.check(argparse.Namespace(deployment=str(path), isolation_only=True))
    monkeypatch.setattr(operate, "node_checks", lambda *a: ["node: answers /readyz"])
    with pytest.raises(SystemExit):
        operate.check(argparse.Namespace(deployment=str(path), isolation_only=False))
