# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the AstralDeep native Windows client (single-file .exe).

Build (from windows-client/, in the venv):
    .venv/Scripts/pyinstaller --noconfirm AstralDeep.spec
Output: dist/AstralDeep.exe
"""
import pathlib
import re
import hashlib
import json
import sys

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo,
    VarStruct, VSVersionInfo,
)

sys.path.insert(0, str(pathlib.Path(SPECPATH)))
from astral_client.deployment import (
    resolve_effective_profile,
    validate_packaged_deployment,
)

# Single version source: astral_client/__init__.py. Read textually (SPECPATH
# is the spec's directory, injected by PyInstaller) — the spec is exec'd, so
# the package is not necessarily importable here.
__version__ = re.search(
    r'__version__\s*=\s*"([^"]+)"',
    (pathlib.Path(SPECPATH) / "astral_client" / "__init__.py").read_text(encoding="utf-8"),
).group(1)

_root = pathlib.Path(SPECPATH)
_profile_path = _root / "deployment" / "release-profile.json"
_runtime_manifest_path = _root / "deployment" / "runtime-manifest.json"
_release_lock_path = _root / "requirements-release.lock.txt"
_requirements_input_path = _root / "requirements.in"
for _required in (
    _profile_path,
    _runtime_manifest_path,
    _release_lock_path,
    _requirements_input_path,
):
    if not _required.is_file():
        raise SystemExit(f"required Windows release input is missing: {_required.name}")

_profile = json.loads(_profile_path.read_text(encoding="utf-8"))
_manifest = json.loads(_runtime_manifest_path.read_text(encoding="utf-8"))
_profile_canonical = json.dumps(
    _profile, sort_keys=True, separators=(",", ":"), ensure_ascii=False
).encode("utf-8")
def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __version__ != "0.4.0" or _profile.get("client_version") != __version__:
    raise SystemExit("Windows release profile and client version must both be 0.4.0")
if _manifest.get("client_version") != __version__:
    raise SystemExit("Windows runtime manifest version does not match the client")
if _manifest.get("release_id") != _profile.get("release_id"):
    raise SystemExit("Windows profile/runtime release identities disagree")
if _manifest.get("deployment_profile_sha256") != hashlib.sha256(_profile_canonical).hexdigest():
    raise SystemExit("Windows release profile is not the reviewed manifest input")
if _manifest.get("requirements_lock_sha256") != _sha256(_release_lock_path):
    raise SystemExit("Windows release lock digest does not match the runtime manifest")
if _manifest.get("requirements_input_sha256") != _sha256(_requirements_input_path):
    raise SystemExit("Windows direct-requirements digest does not match the runtime manifest")
if _manifest.get("required_runtime_lock_sha256") != _sha256(_release_lock_path):
    raise SystemExit("Windows BYO runtime is not bound to the final release lock")

# Run the same strict, duplicate-rejecting, exact-field validation used by the
# frozen executable. This happens before Analysis/EXE construction, so malformed
# or unapproved deployment/runtime inputs cannot produce candidate bytes.
_effective_profile = resolve_effective_profile(
    bundled_profile_path=_profile_path,
    expected_client_version=__version__,
    environment={},
)
validate_packaged_deployment(
    _effective_profile,
    runtime_manifest_path=_runtime_manifest_path,
    requirements_lock_path=_release_lock_path,
    requirements_input_path=_requirements_input_path,
    expected_client_version=__version__,
)

# Windows VERSIONINFO resource derived from the single version constant, so
# the shipped exe's file properties (FileVersion/ProductVersion) always match
# astral_client.__version__ — the same constant the launch integrity check
# compares against the latest GitHub release tag.
_ver_tuple = tuple(int(p) for p in __version__.split(".")[:3]) + (0,)
version_res = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_ver_tuple, prodvers=_ver_tuple),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("ProductName", "AstralDeep"),
            StringStruct("FileDescription", "AstralDeep native Windows client"),
            StringStruct("FileVersion", __version__),
            StringStruct("ProductVersion", __version__),
            StringStruct("CompanyName", "AstralDeep"),
            StringStruct("OriginalFilename", "AstralDeep.exe"),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

hiddenimports = (
    collect_submodules("PySide6.QtCharts")
    # Feature 065: the frozen client is a direct-RTC participant. Keep the
    # exact livekit.rtc Python closure explicit so offline analysis cannot
    # silently omit lazily imported room/audio/data modules.
    + collect_submodules("livekit.rtc")
    + collect_submodules("aiohttp")
    + collect_submodules("sigstore")
    # Feature 058: the frozen exe IS the interpreter for every BYO agent worker
    # (it re-invokes itself with --byo-worker), so the delivered bundle's only
    # third-party import must resolve INSIDE the bundle — it can never pip-install.
    + collect_submodules("astralprims")
    # websockets resolves `connect` through a runtime __import__ in its
    # imports.py, so the concrete client module is not a statically visible
    # dependency. It has survived freezing only because the `if TYPE_CHECKING:`
    # block in websockets/__init__.py leaves IMPORT_NAME bytecode that
    # modulegraph happens to follow — incidental, and it would break silently if
    # upstream reorganized that block. Collect the package explicitly.
    + collect_submodules("websockets")
    + ["PySide6.QtCharts", "PySide6.QtMultimedia", "websockets",
       "livekit", "livekit.rtc",
       "win_agent", "win_agent.agent", "win_agent.tools",
       "win_agent.byo_host", "win_agent.byo_worker",
       "astral_client.phi_gate", "astral_client.audit_log", "astral_client.integrity",
       "astral_client.confirm",
       "psutil", "pyperclip", "sigstore", "astralprims"]
)

# Trim heavy, unused Qt modules to keep the binary lean.
excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtQuick3D",
    "PySide6.QtPdf", "PySide6.QtPositioning", "PySide6.QtSql", "PySide6.QtTest",
    "tkinter", "PySide6.QtDesigner",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    # The Windows livekit wheel carries its RTC FFI native artifact. Collect it
    # deliberately instead of relying on import discovery inside a one-file exe.
    binaries=collect_dynamic_libs("livekit"),
    # The brand icon ships inside the bundle too, so the running app can set
    # its window/taskbar icon (assets resolve via sys._MEIPASS when frozen).
    datas=[
        ("assets/astraldeep.ico", "assets"),
        ("deployment/release-profile.json", "deployment"),
        ("deployment/runtime-manifest.json", "deployment"),
        ("requirements-release.lock.txt", "deployment"),
        ("requirements.in", "deployment"),
    ] + collect_data_files("livekit", include_py_files=False),
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AstralDeep",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,        # GUI app, no console window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=version_res,  # VERSIONINFO stamped from astral_client.__version__
    icon="assets/astraldeep.ico",
)
