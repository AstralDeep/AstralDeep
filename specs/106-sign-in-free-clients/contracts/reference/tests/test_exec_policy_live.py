"""Runs the rendered macOS profile under sandbox-exec, lists which Mach lookups and runtime features the child still has, and pins system.sb and every profile it imports.
It runs only on macOS, because no other platform can apply a Seatbelt profile.
"""

import hashlib
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
    assert all(code == SANDBOX_REFUSED for code in refusals.values()), refusals


def test_python_and_shell_features_still_work(tmp_path: Path) -> None:
    assert run(tmp_path, FEATURES).strip().startswith("features 0 ['/usr',")


PROFILES_DIR = Path("/System/Library/Sandbox/Profiles")
SANDBOX_REFUSED = 1100
TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|;[^\n]*|[()]|[^\s()";]+')
PINNED_ON = "macOS 27.2.1 (26B131)"
PINNED_DIGESTS = {
    "dyld-support.sb": "bdfc2354c683c8fb95a39e63fbc8610e9739b0256c89ab880564edefbe73a5ce",
    "system.sb": "0b038968903c13fc7c461a2cc275352e343eba67451eb1dd20556eed6d79827e",
}
PINNED_NAMES = {
    "com.apple.AppSSO.service-xpc",
    "com.apple.CARenderServer",
    "com.apple.CoreDisplay.Notification",
    "com.apple.CoreDisplay.master",
    "com.apple.SystemConfiguration.PPPController",
    "com.apple.SystemConfiguration.SCNetworkReachability",
    "com.apple.analyticsd",
    "com.apple.analyticsd.messagetracer",
    "com.apple.appsleep",
    "com.apple.bsd.dirhelper",
    "com.apple.cfnetwork.cfnetworkagent",
    "com.apple.cfprefsd.agent",
    "com.apple.cfprefsd.daemon",
    "com.apple.cvmsServ",
    "com.apple.diagnosticd",
    "com.apple.dnssd.service",
    "com.apple.dt.automationmode.reader",
    "com.apple.gpumemd.source",
    "com.apple.internal.objc_trace",
    "com.apple.logd",
    "com.apple.logd.events",
    "com.apple.lsd.mapdb",
    "com.apple.nehelper",
    "com.apple.nesessionmanager",
    "com.apple.networkd",
    "com.apple.networkscored",
    "com.apple.osanalytics.osanalyticshelper",
    "com.apple.runningboard",
    "com.apple.secinitd",
    "com.apple.symptomsd",
    "com.apple.system.DirectoryService.libinfo_v1",
    "com.apple.system.logger",
    "com.apple.system.notification_center",
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.system.opendirectoryd.membership",
    "com.apple.trustd",
    "com.apple.trustd.agent",
    "com.apple.usymptomsd",
    "com.apple.xpc.activity.unmanaged",
}
PINNED_MACH_RULES = {
    ("mach-bootstrap", ""),
    ("mach-lookup", "global-name"),
    ("mach-lookup", "local-name"),
    ("mach-lookup", "xpc-service-name-prefix"),
    ("mach-register", "local-name-prefix"),
}
PINNED_FORM_HEADS = {
    "allow",
    "apply",
    "control-name",
    "cryptex-graft-points",
    "define",
    "extension",
    "fcntl-command",
    "fsctl-command",
    "global-name",
    "import",
    "info-type",
    "iokit-connection",
    "iokit-property",
    "iokit-registry-entry-class",
    "iokit-user-client-class",
    "ipc-posix-name",
    "ipc-posix-name-prefix",
    "kernel-mig-routine",
    "let",
    "list",
    "literal",
    "local-name",
    "local-name-prefix",
    "mac-policy-name",
    "mac-syscall-number",
    "memorystatus-control-command",
    "oopjit-runner",
    "path-ancestors",
    "preference-domain",
    "prefix",
    "process-attribute",
    "require-all",
    "require-any",
    "sbpl-version-compatible?",
    "socket-domain",
    "socket-protocol",
    "subpath",
    "syscall-number",
    "sysctl-name",
    "system-attribute",
    "system-graphics",
    "system-network",
    "unless",
    "version",
    "vnode-type",
    "when",
    "with-filter",
    "xpc-service-name-prefix",
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
REPIN = f"pinned on {PINNED_ON}, re-pin as exec-policy.md section 4 says"


def read_forms(text: str) -> list:
    stack: list[list] = [[]]
    for token in TOKEN.findall(text):
        if token.startswith(";"):
            continue
        if token == "(":
            stack.append([])
        elif token == ")":
            done = stack.pop()
            stack[-1].append(done)
        else:
            stack[-1].append(token)
    assert len(stack) == 1, "unbalanced profile"
    return stack[0]


def walk(form: list):
    yield form
    for item in form:
        if isinstance(item, list):
            yield from walk(item)


def literal(item: object) -> str:
    assert isinstance(item, str) and item.startswith('"'), f"not a literal: {item}"
    return item[1:-1]


def head(form: object) -> str:
    return (
        form[0] if isinstance(form, list) and form and isinstance(form[0], str) else ""
    )


def system_profile() -> dict[str, object]:
    digests: dict[str, str] = {}
    names: set[str] = set()
    rules: set[tuple[str, str]] = set()
    heads: set[str] = set()
    pending = ["system.sb"]
    while pending:
        name = pending.pop()
        if name in digests:
            continue
        data = (PROFILES_DIR / name).read_bytes()
        digests[name] = hashlib.sha256(data).hexdigest()
        for form in walk(read_forms(data.decode("utf-8"))):
            heads.add(head(form))
            if head(form) == "import":
                pending += [literal(item) for item in form[1:]]
            if head(form) == "global-name":
                names |= {literal(item) for item in form[1:]}
            if head(form) == "allow":
                operations = [op for op in form[1:] if isinstance(op, str)]
                filters = [head(f) for f in form[1:] if isinstance(f, list)] or [""]
                rules |= {
                    (op, kind)
                    for op in operations
                    if op.startswith("mach") or op == "default"
                    for kind in filters
                }
    heads.discard("")
    return {"digests": digests, "names": names, "mach_rules": rules, "heads": heads}


def test_system_profile_reader_sees_every_literal_and_kind() -> None:
    forms = read_forms(
        '(allow mach-lookup ; (allow default)\n (global-name "a" "b;c")\n'
        ' (global-name "d"))\n(allow mach-lookup (global-name-prefix "e"))'
    )
    assert [head(f) for f in forms] == ["allow", "allow"]
    names = [literal(i) for f in walk(forms) if head(f) == "global-name" for i in f[1:]]
    assert names == ["a", "b;c", "d"]
    assert head(forms[1][2]) == "global-name-prefix"


def test_system_profile_is_pinned() -> None:
    found = system_profile()
    assert found["digests"] == PINNED_DIGESTS, REPIN
    assert found["names"] == PINNED_NAMES, REPIN
    assert found["mach_rules"] == PINNED_MACH_RULES, REPIN
    assert found["heads"] == PINNED_FORM_HEADS, REPIN


def test_reachable_global_names_are_pinned(tmp_path: Path) -> None:
    names = sorted(system_profile()["names"])
    codes = json.loads(run(tmp_path, LOOKUPS, json.dumps(names)))
    assert set(codes) == set(names)
    reachable = {name for name, code in codes.items() if code != SANDBOX_REFUSED}
    assert reachable == REACHABLE, REPIN
