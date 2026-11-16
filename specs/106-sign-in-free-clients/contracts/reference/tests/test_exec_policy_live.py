"""Runs the rendered macOS profile under sandbox-exec and lists which Mach lookups and runtime features the child still has.
It runs only on macOS, because no other platform can apply a Seatbelt profile.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import exec_policy
import pytest

VECTORS = Path(__file__).resolve().parents[2] / "vectors" / "exec-policy-v1.json"
CONSTANTS = json.loads(VECTORS.read_text(encoding="utf-8"))["constants"]
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists(),
    reason="Seatbelt needs macOS",
)

LOOKUPS = """
import ctypes, json
libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
port = ctypes.c_uint.in_dll(libc, "bootstrap_port")
look = libc.bootstrap_look_up
look.argtypes = [ctypes.c_uint, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint)]
out = {}
for name in json.loads(__import__("sys").argv[1]):
    handle = ctypes.c_uint(0)
    out[name] = look(port.value, name.encode(), ctypes.byref(handle))
print(json.dumps(out))
"""
FEATURES = (
    "import ssl, sqlite3, pwd, os, time, uuid, subprocess\n"
    "ssl.create_default_context()\n"
    "sqlite3.connect(':memory:').execute('create table t(x)')\n"
    "assert pwd.getpwuid(os.getuid()).pw_name and time.strftime('%Z') and uuid.uuid4()\n"
    "out = subprocess.run(['/bin/sh', '-c', 'ls -d /usr; date +%Y'], capture_output=True, text=True)\n"
    "print('features', out.returncode, out.stdout.split())\n"
)


def run(tmp_path: Path, code: str, *args: str) -> str:
    workspace = tmp_path.resolve() / "ws"
    workspace.mkdir()
    prefix = os.path.realpath(sys.base_prefix)
    policy = {
        "v": 1,
        "reads": [prefix],
        "workspace": str(workspace),
        "hosts": [],
        "limits": {
            "memory_bytes": 4294967296,
            "processes": 4,
            "open_files": 256,
            "file_bytes": 104857600,
        },
        "time": {"wall_ms": 60000},
    }
    profile = tmp_path / "profile.sb"
    profile.write_text(
        exec_policy.seatbelt_profile(policy, os.path.expanduser("~"), None)
    )
    result = subprocess.run(
        ["/usr/bin/sandbox-exec", "-f", str(profile), os.path.realpath(sys.executable)]
        + ["-I", "-B", "-c", code, *args],
        capture_output=True,
        text=True,
        cwd="/usr",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_every_denied_service_refuses_lookup(tmp_path: Path) -> None:
    names = [
        CONSTANTS["dnssd_service"],
        *CONSTANTS["denied_services"],
        *CONSTANTS["denied_log_services"],
    ]
    refusals = json.loads(run(tmp_path, LOOKUPS, json.dumps(names)))
    assert set(refusals) == set(names)
    assert all(code != 0 for code in refusals.values()), refusals


def test_python_and_shell_features_still_work(tmp_path: Path) -> None:
    assert run(tmp_path, FEATURES).strip().startswith("features 0 ['/usr',")
