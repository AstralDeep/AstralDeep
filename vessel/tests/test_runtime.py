# Tests for process-mode preparation, the rendered units, the service manager calls and the foreground supervisor.

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_bundle import make_pack
from test_deploy import deployment_text

from astralvessel import bundle as bundle_module
from astralvessel import runtime as rt
from astralvessel import units
from astralvessel.bundle import install
from astralvessel.deployment import load_deployment

MASTER = "m" * 20


@pytest.fixture
def bundle(tmp_path):
    files = {"python/bin/python3.11": (b"#!/bin/sh\n", True),
             "app/backend/astral_exec/__init__.py": (b"X = 1\n", False),
             "bin/rg": (b"#!/bin/sh\n", True)}
    dists = [{"name": n, "version": v} for n, v in (("cryptography", "46.0"), ("httpx", "0.28.1"),
                                                      ("pydantic", "2.14.3"), ("starlette", "1.10.0"),
                                                      ("uvicorn", "0.54.0"), ("fastapi", "0.162.9"))]
    descriptor, digest = make_pack(tmp_path / "src", files=files)
    installed = install(str(descriptor), digest, tmp_path / "bundles", platform=bundle_module.platform_name())
    object.__setattr__(installed, "manifest", {**installed.manifest, "installed_distributions": dists})
    return installed


def make(tmp_path, package_dir, bundle, egress=False, **kwargs):
    folder = tmp_path / "dep"
    folder.mkdir()
    path = folder / "deployment.toml"
    path.write_text(deployment_text(package_dir, mounts="", egress=egress))
    calls = []

    def runner(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "active\n", "")

    runtime = rt.Runtime(load_deployment(path), folder, bundle, runner=runner, unit_dir=tmp_path / "units",
                         uid=501, user="sam", out=kwargs.pop("out", lambda text: None), **kwargs)
    return runtime, calls


def test_prepare_copies_code_writes_private_files_and_keeps_secrets(tmp_path, package_dir, bundle):
    runtime, _ = make(tmp_path, package_dir, bundle)
    runtime.prepare(MASTER)
    layout = runtime.layout
    assert (layout.code / "run.py").read_text().startswith("# Starts one Vessel role")
    assert (layout.code / "astralvessel" / "runtime.py").is_file() and (layout.code / "egress" / "app.py").is_file()
    assert (layout.code / "astral_exec" / "__init__.py").read_bytes() == b"X = 1\n"
    assert not list(layout.code.rglob("__pycache__"))
    assert layout.node_data.stat().st_mode & 0o777 == 0o700
    node_env = layout.generated / "node.env"
    first = node_env.read_bytes()
    runtime.prepare(MASTER)
    assert node_env.read_bytes() == first
    for name in ("node-launch.env", "executor-run.env", "sandbox-guide-run.env"):
        assert (layout.generated / name).stat().st_mode & 0o777 == 0o600
    assert not (runtime.folder / "compose.generated.yml").exists()


def test_the_process_environment_is_host_paths_on_loopback(tmp_path, package_dir, bundle):
    runtime, _ = make(tmp_path, package_dir, bundle)
    runtime.prepare(MASTER)
    node = runtime._env_values(runtime.env_file("node"))
    executor = runtime._env_values(runtime.env_file("executor"))
    sandbox = runtime._env_values(runtime.env_file("sandbox-guide"))
    values = [*node.values(), *executor.values(), *sandbox.values()]
    assert not any(v.startswith(("/node-data", "/run/secrets", "/agent", "/executor")) for v in values)
    assert node["ASTRAL_NODE_DATA_DIR"] == str(runtime.layout.node_data / "node")
    assert node["ASTRAL_NODE_PROFILE"] == "personal" and "WEB_SESSION_ENC_KEY" in node
    assert node["ASTRAL_VESSEL_EXECUTOR_URL"] == "http://127.0.0.1:8700"
    assert executor["VESSEL_HOST"] == sandbox["VESSEL_HOST"] == "127.0.0.1"
    assert json.loads(executor["VESSEL_SANDBOX_PORTS"]) == {"guide": 8710}
    assert sandbox["VESSEL_PORT"] == "8710" and sandbox["VESSEL_RG_BIN"].endswith("bin/rg")


def test_ports_are_stable_across_prepare(tmp_path, package_dir, bundle):
    runtime, _ = make(tmp_path, package_dir, bundle)
    before = runtime.ports
    runtime.prepare(MASTER)
    assert runtime.ports == before


def test_a_short_master_token_refuses(tmp_path, package_dir, bundle):
    runtime, _ = make(tmp_path, package_dir, bundle)
    with pytest.raises(rt.RuntimeProblem, match="16"):
        runtime.prepare("short")


def test_the_egress_needs_the_model_key_in_the_environment(tmp_path, package_dir, bundle, monkeypatch):
    runtime, _ = make(tmp_path, package_dir, bundle, egress=True)
    monkeypatch.delenv("VESSEL_UPSTREAM_MODEL_KEY", raising=False)
    with pytest.raises(rt.RuntimeProblem, match="model key"):
        runtime.prepare(MASTER)
    monkeypatch.setenv("VESSEL_UPSTREAM_MODEL_KEY", "k" * 12)
    runtime.prepare(MASTER)
    egress = runtime._env_values(runtime.env_file("egress"))
    assert egress["EGRESS_HOST"] == "127.0.0.1" and egress["LLM_API_KEY"] == "k" * 12
    assert runtime.roles == ["node", "egress", "sandbox-guide", "executor"]


def test_a_pack_missing_a_declared_dependency_is_refused(tmp_path, package_dir, bundle):
    runtime, _ = make(tmp_path, package_dir, bundle)
    trimmed = [d for d in bundle.manifest["installed_distributions"] if d["name"] != "starlette"]
    object.__setattr__(bundle, "manifest", {**bundle.manifest, "installed_distributions": trimmed})
    with pytest.raises(rt.RuntimeProblem, match="needs starlette"):
        runtime.prepare(MASTER)
    assert not runtime.layout.code.exists()


def test_a_pack_with_an_old_version_is_refused_and_egress_ranges_apply_only_with_a_gateway(tmp_path, package_dir,
                                                                                              bundle, monkeypatch):
    listed = bundle.manifest["installed_distributions"]
    old = [{**d, "version": "0.100.0"} if d["name"] == "fastapi" else d for d in listed]
    object.__setattr__(bundle, "manifest", {**bundle.manifest, "installed_distributions": old})
    runtime, _ = make(tmp_path, package_dir, bundle)
    runtime.check_dependencies()
    (tmp_path / "g").mkdir()
    gateway, _ = make(tmp_path / "g", package_dir, bundle, egress=True)
    with pytest.raises(rt.RuntimeProblem, match=r"fastapi>=0.162.6"):
        gateway.check_dependencies()


def test_a_pack_without_astral_exec_is_refused(tmp_path, package_dir, bundle):
    runtime, _ = make(tmp_path, package_dir, bundle)
    shutil.rmtree(bundle.astral_exec)
    with pytest.raises(rt.RuntimeProblem, match="astral_exec"):
        runtime.prepare(MASTER)


def test_golden_systemd_unit_for_a_sandbox_sets_limits_and_holds_no_secret(tmp_path, package_dir, bundle):
    runtime, _ = make(tmp_path, package_dir, bundle)
    runtime.prepare(MASTER)
    text = units.systemd_unit("dep", runtime.spec("sandbox-guide"))
    root = runtime.layout.code
    py = runtime.bundle.python
    env = f"{runtime.layout.generated}/sandbox-guide-run.env"
    assert text == f"""[Unit]
Description=AstralVessel dep sandbox-guide

[Service]
ExecStart="{py}" "-I" "-B" "{root}/run.py" "sandbox-guide" "--env-file" "{env}"
WorkingDirectory="{runtime.folder}"
Environment=PATH="{runtime.bundle.root}/bin:/usr/bin:/bin:/usr/sbin:/sbin"
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=2
NoNewPrivileges=yes
StandardOutput=append:{runtime.layout.logs}/sandbox-guide.log
StandardError=append:{runtime.layout.logs}/sandbox-guide.log
MemoryMax=2147483648
TasksMax=512
CPUQuota=200%

[Install]
WantedBy=default.target
"""
    for role in runtime.roles:
        body = units.systemd_unit("dep", runtime.spec(role)) + units.launchd_plist("dep", runtime.spec(role)).decode()
        assert MASTER not in body and "WEB_SESSION_ENC_KEY" not in body
    assert "MemoryMax" not in units.systemd_unit("dep", runtime.spec("node"))


def test_golden_launchd_plist(tmp_path, package_dir, bundle):
    runtime, _ = make(tmp_path, package_dir, bundle)
    runtime.prepare(MASTER)
    plist = plistlib.loads(units.launchd_plist("dep", runtime.spec("node")))
    assert plist["Label"] == "dev.astraldeep.vessel.dep.node"
    assert plist["KeepAlive"] == {"SuccessfulExit": False} and plist["RunAtLoad"] is True
    assert plist["ProgramArguments"][1:4] == ["-I", "-B", f"{runtime.layout.code}/run.py"]
    assert "--port" in plist["ProgramArguments"] and "--allow-remote" not in plist["ProgramArguments"]
    assert set(plist["EnvironmentVariables"]) == {"PATH", "PYTHONDONTWRITEBYTECODE"}


def test_a_public_bind_passes_allow_remote(tmp_path, package_dir, bundle):
    runtime, _ = make(tmp_path, package_dir, bundle)
    runtime.deployment.node.bind = "0.0.0.0"
    assert "--allow-remote" in runtime.spec("node").argv


def test_launchd_calls_are_recorded(tmp_path, package_dir, bundle):
    runtime, calls = make(tmp_path, package_dir, bundle, platform="darwin")
    runtime.prepare(MASTER)
    runtime.start()
    assert (tmp_path / "units" / "dev.astraldeep.vessel.astralvessel.node.plist").is_file()
    plist = tmp_path / "units" / "dev.astraldeep.vessel.astralvessel.node.plist"
    assert ["launchctl", "bootstrap", "gui/501", str(plist)] in calls
    assert ["launchctl", "kickstart", "-k", "gui/501/dev.astraldeep.vessel.astralvessel.executor"] in calls
    assert not any(c[0] == "loginctl" for c in calls)
    assert all(runtime.status().values())
    calls.clear()
    runtime.stop()
    assert [c[:2] for c in calls] == [["launchctl", "bootout"]] * 3
    assert calls[0][2].endswith(".executor")


def test_systemd_calls_and_the_linger_note_never_sudo(tmp_path, package_dir, bundle):
    notes = []
    runtime, calls = make(tmp_path, package_dir, bundle, platform="linux", out=notes.append)

    def runner(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "no\n" if argv[0] == "loginctl" else "inactive\n", "")

    runtime.runner = runner
    runtime.prepare(MASTER)
    runtime.start()
    assert notes and "loginctl enable-linger sam" in notes[0]
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "enable", "--now", "astralvessel-astralvessel-node.service"] in calls
    assert not any("sudo" in part for call in calls for part in call)
    assert (tmp_path / "units" / "astralvessel-astralvessel-sandbox-guide.service").is_file()
    assert not any(runtime.status().values())
    runtime.stop()
    assert ["systemctl", "--user", "disable", "--now", "astralvessel-astralvessel-node.service"] in calls


def fake_spec(tmp_path, name, code):
    return units.RoleSpec(name, [sys.executable, "-c", code], tmp_path, tmp_path / "logs" / f"{name}.log", "/usr/bin")


LISTEN = ("import socket,sys,time;s=socket.socket();s.bind(('127.0.0.1',0));s.listen();"
          "print(s.getsockname()[1],flush=True);time.sleep(60)")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_the_foreground_supervisor_restarts_a_crashed_child_and_leaves_none(tmp_path):
    marker = tmp_path / "pids"
    crash = f"import os;open({str(marker)!r},'a').write(str(os.getpid())+'\\n');import sys;sys.exit(3)"
    spawned = []

    def popen(*args, **kwargs):
        child = subprocess.Popen(*args, **kwargs)
        spawned.append(child)
        return child

    supervisor = units.Supervisor([fake_spec(tmp_path, "a", LISTEN), fake_spec(tmp_path, "b", crash)],
                                  home=str(tmp_path), popen=popen, backoff=0.0, grace=3)
    supervisor.start_all()
    deadline = time.monotonic() + 10
    while supervisor.restarts.get("b", 0) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert supervisor.restarts["b"] >= 2
    assert supervisor.status()["a"] is True
    pids = [c.pid for c in spawned]
    supervisor.stop_all()
    time.sleep(0.2)
    assert not any(alive(p) for p in pids) and supervisor.children == {}


def test_stop_kills_a_child_that_ignores_term(tmp_path):
    stubborn = ("import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('up',flush=True);"
                "time.sleep(60)")
    supervisor = units.Supervisor([fake_spec(tmp_path, "s", stubborn)], home=str(tmp_path), backoff=0.0, grace=0.3)
    supervisor.start_all()
    time.sleep(0.5)
    pid = supervisor.children["s"].pid
    supervisor.stop_all()
    assert not alive(pid)


def test_children_get_an_explicit_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("VESSEL_SANDBOX_TOKEN", "master-secret-value")
    out = tmp_path / "env.json"
    code = f"import os,json;open({str(out)!r},'w').write(json.dumps(dict(os.environ)))"
    supervisor = units.Supervisor([fake_spec(tmp_path, "e", code)], home=str(tmp_path), backoff=100, grace=1)
    supervisor.start_all()
    deadline = time.monotonic() + 5
    while not out.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    supervisor.stop_all()
    env = json.loads(out.read_text())
    assert "VESSEL_SANDBOX_TOKEN" not in env and env["HOME"] == str(tmp_path)


def test_runtime_foreground_start_and_stop(tmp_path, package_dir, bundle):
    runtime, calls = make(tmp_path, package_dir, bundle)
    runtime.prepare(MASTER)
    runtime.start(foreground=True)
    try:
        assert set(runtime.status()) == set(runtime.roles)
    finally:
        runtime.stop()
    assert calls == [] and runtime.supervisor.children == {}
    assert Path(runtime.layout.logs).is_dir()


def run_bootstrap(tmp_path, *args, env_text=""):
    (tmp_path / "run.py").write_text(rt.RUN_PY)
    env_file = tmp_path / "role.env"
    env_file.write_text(env_text)
    return subprocess.run([sys.executable, "-I", "-B", str(tmp_path / "run.py"), *args, "--env-file", str(env_file)],
                          capture_output=True, text=True, env={"PATH": os.environ["PATH"]})


def test_the_bootstrap_loads_the_env_file_then_hands_the_node_to_the_pack_launcher(tmp_path):
    pack = tmp_path / "pack"
    (pack / "launcher").mkdir(parents=True)
    out = tmp_path / "seen.json"
    (pack / "launcher" / "astral_node_launch.py").write_text(
        "import json, os, sys\n"
        f"open({str(out)!r}, 'w').write(json.dumps([sys.argv[1:], os.environ.get('SECRET_VALUE')]))\n")
    done = run_bootstrap(tmp_path, "node", env_text=f"VESSEL_PACK_ROOT={pack}\nSECRET_VALUE='abc'\n")
    assert done.returncode == 0, done.stderr
    argv, secret = json.loads(out.read_text())
    assert argv[0] == "--env-file" and secret == "abc"


def test_the_bootstrap_refuses_an_unknown_role(tmp_path):
    done = run_bootstrap(tmp_path, "shell")
    assert done.returncode != 0 and "usage" in done.stderr
