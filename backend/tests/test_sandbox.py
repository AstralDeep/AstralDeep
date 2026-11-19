"""Tests for sandboxed generated-code execution (orchestrator/sandbox.py): the
development-only kill-switch flag, env-tunable resource limits, preexec construction,
the secret-scrubbing, temp-scoped child environment, and the loopback agent launch.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import sandbox  # noqa: E402
from orchestrator.sandbox import SandboxLimits  # noqa: E402


def test_sandbox_default_on(monkeypatch):
    monkeypatch.delenv("FF_SANDBOX_CODEGEN", raising=False)
    assert sandbox.sandbox_enabled() is True


def test_sandbox_explicit_kill_switch_in_development(monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("FF_SANDBOX_CODEGEN", "false")
    assert sandbox.sandbox_enabled() is False


@pytest.mark.parametrize("env", [None, "production", "staging", ""])
def test_the_kill_switch_is_ignored_outside_development(monkeypatch, env):
    if env is None:
        monkeypatch.delenv("ASTRAL_ENV", raising=False)
    else:
        monkeypatch.setenv("ASTRAL_ENV", env)
    monkeypatch.setenv("FF_SANDBOX_CODEGEN", "false")
    assert sandbox.sandbox_enabled() is True


@pytest.mark.parametrize("v", ["true", "1", "yes", "on"])
def test_sandbox_on_values(monkeypatch, v):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("FF_SANDBOX_CODEGEN", v)
    assert sandbox.sandbox_enabled() is True


def test_build_limits_defaults(monkeypatch):
    for k in ("SANDBOX_MEM_MB", "SANDBOX_FSIZE_MB", "SANDBOX_NOFILE",
              "SANDBOX_NPROC", "SANDBOX_CPU_S"):
        monkeypatch.delenv(k, raising=False)
    lim = sandbox.build_limits()
    assert lim.address_space_mb == 2048 and lim.cpu_seconds == 0
    assert lim.open_files == 512 and lim.processes == 512


def test_build_limits_env_override(monkeypatch):
    monkeypatch.setenv("SANDBOX_MEM_MB", "512")
    monkeypatch.setenv("SANDBOX_CPU_S", "30")
    monkeypatch.setenv("SANDBOX_NOFILE", "abc")
    lim = sandbox.build_limits()
    assert lim.address_space_mb == 512 and lim.cpu_seconds == 30
    assert lim.open_files == 512


def _fake_resource(hard=None):
    INF = -1
    calls = {}

    def getrlimit(r):
        return (INF, hard if hard is not None else INF)

    def setrlimit(r, pair):
        calls[r] = pair

    return SimpleNamespace(
        RLIMIT_AS="AS", RLIMIT_FSIZE="FSIZE", RLIMIT_NOFILE="NOFILE",
        RLIMIT_NPROC="NPROC", RLIMIT_CPU="CPU", RLIM_INFINITY=INF,
        getrlimit=getrlimit, setrlimit=setrlimit, _calls=calls)


def test_apply_limits_sets_server_safe_resources():
    res = _fake_resource()
    sandbox._apply_limits(SandboxLimits(), res)
    assert res._calls["AS"] == (2048 * 1024 * 1024, 2048 * 1024 * 1024)
    assert res._calls["FSIZE"] == (256 * 1024 * 1024, 256 * 1024 * 1024)
    assert res._calls["NOFILE"] == (512, 512)
    assert res._calls["NPROC"] == (512, 512)
    assert "CPU" not in res._calls


def test_apply_limits_cpu_is_opt_in():
    res = _fake_resource()
    sandbox._apply_limits(SandboxLimits(cpu_seconds=45), res)
    assert res._calls["CPU"] == (45, 45)


def test_apply_limits_never_raises_above_hard_cap():
    res = _fake_resource(hard=100 * 1024 * 1024)
    sandbox._apply_limits(SandboxLimits(address_space_mb=2048), res)
    soft, hard = res._calls["AS"]
    assert soft == 100 * 1024 * 1024 and hard == 100 * 1024 * 1024


def test_apply_limits_swallows_errors():
    def boom(*a):
        raise OSError("nope")
    res = _fake_resource()
    res.setrlimit = boom
    sandbox._apply_limits(SandboxLimits(), res)


def test_make_preexec_none_on_non_posix(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    assert sandbox.make_preexec(SandboxLimits()) is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only resource module")
def test_make_preexec_callable_on_posix():
    fn = sandbox.make_preexec(SandboxLimits())
    assert callable(fn)


def test_sandbox_env_scrubs_secrets_and_scopes_temp():
    base = {"OPENAI_API_KEY": "sk-x", "DATABASE_URL": "postgres://x",
            "AGENT_API_KEY": "keep-me", "PYTHONPATH": "/app", "PATH": "/usr/bin"}
    env = sandbox.sandbox_env(base, "/tmp/draft42")
    assert "OPENAI_API_KEY" not in env and "DATABASE_URL" not in env
    assert env["AGENT_API_KEY"] == "keep-me"
    assert env["PYTHONPATH"] == "/app"
    assert env["TMPDIR"] == env["TEMP"] == env["TMP"] == "/tmp/draft42"


def test_sandbox_env_does_not_mutate_input():
    base = {"OPENAI_API_KEY": "sk-x"}
    sandbox.sandbox_env(base, "/tmp/x")
    assert base == {"OPENAI_API_KEY": "sk-x"}


# Exact names only: an unused-var denylist protects nothing
_MUST_SCRUB = [
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "SEARCH_API_KEY",
    "CREDENTIAL_ENCRYPTION_KEY",
    "WEB_SESSION_ENC_KEY", "WEB_SESSION_SECRET",
    "OFFLINE_GRANT_ENC_KEY",
    "MEMORY_HMAC_KEY", "TXN_TOKEN_KEY", "MAS_MESSAGE_KEY",
    "DELEGATION_CHILD_SIGNING_KEY",
    "AUDIT_HMAC_SECRET",
    "KEYCLOAK_CLIENT_SECRET", "AGENT_SERVICE_CLIENT_SECRET",
    "VOICE_CONTROL_SECRET", "LIVEKIT_API_SECRET", "LIVEKIT_API_KEY",
    "DATABASE_URL", "GITHUB_TOKEN",
]

_MUST_KEEP = {
    "AGENT_API_KEY": "keep-me", "PATH": "/usr/bin", "PYTHONPATH": "/app",
    "DB_HOST": "db", "DB_PORT": "5432", "DB_NAME": "astral", "DB_USER": "astral",
}


@pytest.mark.parametrize("name", _MUST_SCRUB)
def test_sandbox_env_scrubs_every_known_secret(name):
    env = sandbox.sandbox_env({name: "secret-value", **_MUST_KEEP}, "/tmp/d")
    assert name not in env, f"{name} leaks into the generated agent's environment"


def test_sandbox_env_scrubs_audit_hmac_rotation_keys():
    env = sandbox.sandbox_env(
        {"AUDIT_HMAC_SECRET": "a", "AUDIT_HMAC_SECRET_K0": "b",
         "AUDIT_HMAC_SECRET_LEGACY": "c", **_MUST_KEEP},
        "/tmp/d",
    )
    assert not [k for k in env if k.startswith("AUDIT_HMAC_SECRET")]


def test_sandbox_env_keeps_what_the_agent_needs():
    env = sandbox.sandbox_env({**_MUST_KEEP, "OPENAI_API_KEY": "sk-x"}, "/tmp/d")
    for key, value in _MUST_KEEP.items():
        assert env[key] == value, f"{key} must survive the scrub"


@pytest.mark.parametrize("name", ["DB_PASSWORD", "PLANE_PASSWORD", "SOME_VENDOR_TOKEN", "NODE_PRIVATE_KEY"])
def test_sandbox_env_scrubs_secret_shaped_names_including_the_database_password(name):
    env = sandbox.sandbox_env({name: "secret-value", **_MUST_KEEP}, "/tmp/d")
    assert name not in env


def test_agent_launch_applies_limits_scrubs_and_binds_loopback(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    monkeypatch.setenv("FF_SANDBOX_CODEGEN", "false")
    launch = sandbox.agent_launch(str(tmp_path), {"HOST": "0.0.0.0", "DB_PASSWORD": "pw", "AGENT_API_KEY": "k"})
    assert launch.limits_applied is (os.name == "posix")
    env = launch.popen["env"]
    assert env["HOST"] == sandbox.LOOPBACK_HOST == "127.0.0.1"
    assert "DB_PASSWORD" not in env and env["AGENT_API_KEY"] == "k"
    assert env["TMPDIR"] == str(tmp_path / "_sandbox_tmp") and (tmp_path / "_sandbox_tmp").is_dir()


def test_agent_launch_binds_loopback_even_when_development_turns_the_limits_off(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("FF_SANDBOX_CODEGEN", "false")
    launch = sandbox.agent_launch(str(tmp_path), {"HOST": "0.0.0.0", "DB_PASSWORD": "pw"})
    assert launch.limits_applied is False and "preexec_fn" not in launch.popen
    assert launch.popen["env"] == {"HOST": "127.0.0.1", "DB_PASSWORD": "pw"}
    assert not (tmp_path / "_sandbox_tmp").exists()


def test_agent_launch_reads_the_process_environment_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    env = sandbox.agent_launch(str(tmp_path)).popen["env"]
    assert env["HOST"] == "127.0.0.1" and "OPENAI_API_KEY" not in env


_PROBE_AGENT = """
import asyncio, sys
from types import SimpleNamespace
sys.path.insert(0, sys.argv[2])
from shared.base_agent import BaseA2AAgent

class Probe(BaseA2AAgent):
    agent_id = "loopback-probe"
    service_name = "Loopback probe"
    description = "probe"

asyncio.run(Probe(SimpleNamespace(tools={}), port=int(sys.argv[1])).run())
"""


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _outward_address():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("10.255.255.255", 1))
            address = probe.getsockname()[0]
        except OSError:
            return None
    return None if address.startswith("127.") else address


def _reachable(host: str, port: int) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


@pytest.mark.skipif(os.name != "posix", reason="POSIX preexec_fn + resource")
def test_a_real_agent_server_launched_this_way_listens_on_loopback_only(tmp_path, monkeypatch):
    import subprocess
    import time

    outward = _outward_address()
    if outward is None:
        pytest.skip("this host has no non-loopback IPv4 address to probe")
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "probe.pem"))
    port = _free_port()
    launch = sandbox.agent_launch(str(tmp_path))
    proc = subprocess.Popen([sys.executable, "-c", _PROBE_AGENT, str(port), str(BACKEND_DIR)],
                            cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **launch.popen)
    try:
        deadline = time.monotonic() + 30
        while not _reachable("127.0.0.1", port):
            assert proc.poll() is None, proc.stderr.read().decode("utf-8", "replace")[-2000:]
            assert time.monotonic() < deadline, "the probe agent never listened"
            time.sleep(0.2)
        assert _reachable(outward, port) is False
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.mark.skipif(os.name != "posix", reason="POSIX preexec_fn + resource")
def test_live_memory_limit_kills_a_greedy_child():
    import subprocess
    fn = sandbox.make_preexec(SandboxLimits(address_space_mb=128))
    code = "b = bytearray(512*1024*1024); print(len(b))"
    proc = subprocess.run([sys.executable, "-c", code], preexec_fn=fn,
                          capture_output=True, timeout=30)
    assert proc.returncode != 0
    assert b"536870912" not in proc.stdout
