"""Runs the rendered macOS profile under sandbox-exec and lists which Mach lookups and runtime features the child still has.
It runs only on macOS, because no other platform can apply a Seatbelt profile.
"""

import json
import os
import re
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


SYSTEM_SB = Path("/System/Library/Sandbox/Profiles/system.sb")
SYSTEM_SB_NAMES = {
    "com.apple.AppSSO.service-xpc",
    "com.apple.CARenderServer",
    "com.apple.CoreDisplay.Notification",
    "com.apple.CoreDisplay.master",
    "com.apple.analyticsd",
    "com.apple.analyticsd.messagetracer",
    "com.apple.appsleep",
    "com.apple.bsd.dirhelper",
    "com.apple.cfnetwork.cfnetworkagent",
    "com.apple.cfprefsd.agent",
    "com.apple.cfprefsd.daemon",
    "com.apple.cvmsServ",
    "com.apple.diagnosticd",
    "com.apple.dt.automationmode.reader",
    "com.apple.gpumemd.source",
    "com.apple.internal.objc_trace",
    "com.apple.logd",
    "com.apple.logd.events",
    "com.apple.lsd.mapdb",
    "com.apple.osanalytics.osanalyticshelper",
    "com.apple.runningboard",
    "com.apple.secinitd",
    "com.apple.system.DirectoryService.libinfo_v1",
    "com.apple.system.logger",
    "com.apple.system.notification_center",
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.system.opendirectoryd.membership",
    "com.apple.trustd",
    "com.apple.trustd.agent",
    "com.apple.xpc.activity.unmanaged",
}
REACHABLE = {
    "com.apple.appsleep",
    "com.apple.bsd.dirhelper",
    "com.apple.cfprefsd.agent",
    "com.apple.cfprefsd.daemon",
    "com.apple.dt.automationmode.reader",
    "com.apple.runningboard",
    "com.apple.secinitd",
    "com.apple.system.DirectoryService.libinfo_v1",
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.system.opendirectoryd.membership",
    "com.apple.xpc.activity.unmanaged",
}


def test_reachable_global_names_are_pinned(tmp_path: Path) -> None:
    found = set(re.findall(r'\(global-name "([^"]*)"\)', SYSTEM_SB.read_text()))
    assert found == SYSTEM_SB_NAMES, "system.sb names changed"
    codes = json.loads(run(tmp_path, LOOKUPS, json.dumps(sorted(found))))
    assert {name for name, code in codes.items() if code == 0} == REACHABLE
