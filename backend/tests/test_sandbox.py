"""Feature 033 (capability C-S6) — sandboxed generated-code execution.

Covers the flag, env-tunable resource limits, the injectable limit application
(setrlimit calls, hard-cap clamping, opt-in CPU), preexec construction across
platforms, and the secret-scrubbing / temp-scoping child environment. A POSIX-
only live test asserts a real child is actually killed past its memory limit.
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


# ───────────────────────── flag ──────────────────────────────────────────────

def test_sandbox_default_on(monkeypatch):
    # H4 remediation: draft/parser children never inherit secrets by default.
    monkeypatch.delenv("FF_SANDBOX_CODEGEN", raising=False)
    assert sandbox.sandbox_enabled() is True


def test_sandbox_explicit_kill_switch(monkeypatch):
    monkeypatch.setenv("FF_SANDBOX_CODEGEN", "false")
    assert sandbox.sandbox_enabled() is False


@pytest.mark.parametrize("v", ["true", "1", "yes", "on"])
def test_sandbox_on_values(monkeypatch, v):
    monkeypatch.setenv("FF_SANDBOX_CODEGEN", v)
    assert sandbox.sandbox_enabled() is True


# ───────────────────────── limits from env ───────────────────────────────────

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
    monkeypatch.setenv("SANDBOX_NOFILE", "abc")  # invalid → default
    lim = sandbox.build_limits()
    assert lim.address_space_mb == 512 and lim.cpu_seconds == 30
    assert lim.open_files == 512


# ───────────────────────── limit application (injected resource) ──────────────

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
    # memory/fsize/nofile/nproc are set; CPU is NOT (cpu_seconds default 0)
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
    res = _fake_resource(hard=100 * 1024 * 1024)  # inherited 100MB hard cap
    sandbox._apply_limits(SandboxLimits(address_space_mb=2048), res)
    soft, hard = res._calls["AS"]
    assert soft == 100 * 1024 * 1024 and hard == 100 * 1024 * 1024  # clamped down


def test_apply_limits_swallows_errors():
    def boom(*a):
        raise OSError("nope")
    res = _fake_resource()
    res.setrlimit = boom
    sandbox._apply_limits(SandboxLimits(), res)  # must not raise


# ───────────────────────── preexec construction ──────────────────────────────

def test_make_preexec_none_on_non_posix(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    assert sandbox.make_preexec(SandboxLimits()) is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only resource module")
def test_make_preexec_callable_on_posix():
    fn = sandbox.make_preexec(SandboxLimits())
    assert callable(fn)


# ───────────────────────── env scrub + temp scope ────────────────────────────

def test_sandbox_env_scrubs_secrets_and_scopes_temp():
    base = {"OPENAI_API_KEY": "sk-x", "DATABASE_URL": "postgres://x",
            "AGENT_API_KEY": "keep-me", "PYTHONPATH": "/app", "PATH": "/usr/bin"}
    env = sandbox.sandbox_env(base, "/tmp/draft42")
    assert "OPENAI_API_KEY" not in env and "DATABASE_URL" not in env
    assert env["AGENT_API_KEY"] == "keep-me"   # framework key preserved
    assert env["PYTHONPATH"] == "/app"
    assert env["TMPDIR"] == env["TEMP"] == env["TMP"] == "/tmp/draft42"


def test_sandbox_env_does_not_mutate_input():
    base = {"OPENAI_API_KEY": "sk-x"}
    sandbox.sandbox_env(base, "/tmp/x")
    assert base == {"OPENAI_API_KEY": "sk-x"}  # original untouched


#: Every name here is read somewhere in backend/ and would be a real compromise
#: in a generated agent's os.environ. Pinned by name: a denylist that protects
#: env vars nothing sets (the pre-067 list did) protects nothing at all.
_MUST_SCRUB = [
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "SEARCH_API_KEY",
    "CREDENTIAL_ENCRYPTION_KEY",          # Fernet: user LLM keys + SSH creds
    "WEB_SESSION_ENC_KEY", "WEB_SESSION_SECRET",   # session forgery
    "OFFLINE_GRANT_ENC_KEY",
    "MEMORY_HMAC_KEY", "TXN_TOKEN_KEY", "MAS_MESSAGE_KEY",
    "DELEGATION_CHILD_SIGNING_KEY",
    "AUDIT_HMAC_SECRET",                  # audit chain forgery (DB_* stays readable)
    "KEYCLOAK_CLIENT_SECRET", "AGENT_SERVICE_CLIENT_SECRET",
    "VOICE_CONTROL_SECRET", "LIVEKIT_API_SECRET", "LIVEKIT_API_KEY",
    "DATABASE_URL", "GITHUB_TOKEN",
]

#: The child MUST keep these: the framework needs AGENT_API_KEY to register, and
#: a parser agent resolves its attachment through shared.database (DB_*).
_MUST_KEEP = {
    "AGENT_API_KEY": "keep-me", "PATH": "/usr/bin", "PYTHONPATH": "/app",
    "DB_HOST": "db", "DB_PORT": "5432", "DB_NAME": "astral",
    "DB_USER": "astral", "DB_PASSWORD": "pw",
}


@pytest.mark.parametrize("name", _MUST_SCRUB)
def test_sandbox_env_scrubs_every_known_secret(name):
    env = sandbox.sandbox_env({name: "secret-value", **_MUST_KEEP}, "/tmp/d")
    assert name not in env, f"{name} leaks into the generated agent's environment"


def test_sandbox_env_scrubs_audit_hmac_rotation_keys():
    """audit/pii.py resolves rotation keys as AUDIT_HMAC_SECRET_<KEY_ID_UPPER>;
    a retired-but-still-set key signs the same chain, so a fixed-name list is
    not enough."""
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


# ───────────────────────── live: a real child is actually limited ────────────

@pytest.mark.skipif(os.name != "posix", reason="POSIX preexec_fn + resource")
def test_live_memory_limit_kills_a_greedy_child():
    import subprocess
    fn = sandbox.make_preexec(SandboxLimits(address_space_mb=128))
    # try to allocate ~512MB inside a 128MB address-space cap → MemoryError/kill
    code = "b = bytearray(512*1024*1024); print(len(b))"
    proc = subprocess.run([sys.executable, "-c", code], preexec_fn=fn,
                          capture_output=True, timeout=30)
    assert proc.returncode != 0          # the child could not allocate
    assert b"536870912" not in proc.stdout
