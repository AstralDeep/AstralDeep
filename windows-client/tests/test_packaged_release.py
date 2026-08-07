"""Actual frozen Windows candidate smoke tests (T059).

The source-contract test runs everywhere. Artifact tests require the reusable
Windows candidate job to set ``ASTRAL_WINDOWS_EXE``; the connected clean-window
check additionally uses its short-lived staging access token.
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _candidate_exe() -> Path:
    value = os.getenv("ASTRAL_WINDOWS_EXE")
    if not value:
        pytest.skip("ASTRAL_WINDOWS_EXE is supplied by the Windows candidate job")
    path = Path(value)
    if not path.is_file():
        pytest.fail(f"ASTRAL_WINDOWS_EXE is not a file: {path}")
    return path


def _clean_env(tmp_path: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "ASTRAL_MANAGED_DEPLOYMENT_PROFILE",
        "ASTRAL_WS_URL",
        "KEYCLOAK_AUTHORITY",
        "ASTRAL_CLIENT_ID",
        "KEYCLOAK_DESKTOP_CLIENT_ID",
        "ASTRAL_AUTH_BFF",
        "ASTRAL_TOKEN",
        "AGENT_API_KEY",
        "QT_QPA_PLATFORM",
    ):
        environment.pop(name, None)
    roaming = tmp_path / "Roaming"
    local = tmp_path / "Local"
    roaming.mkdir()
    local.mkdir()
    environment["APPDATA"] = str(roaming)
    environment["LOCALAPPDATA"] = str(local)
    return environment


def _clear_native_windows_settings() -> None:
    """Give each GUI smoke the same absent-HKCU starting state as a new user."""

    if sys.platform != "win32":
        return
    result = subprocess.run(
        [
            "reg.exe",
            "delete",
            r"HKCU\Software\AstralDeep\WindowsClient",
            "/f",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr


def _offline_profile(path: Path) -> Path:
    value = json.loads((ROOT / "deployment" / "release-profile.json").read_text())
    value.update(
        {
            "profile_id": "astraldeep-release-offline-smoke",
            "release_id": "windows-0.4.0-offline-smoke",
            "distribution": "generic_developer",
            "local_only": True,
            "authority": "http://127.0.0.1:1/realms/Astral",
            "websocket_endpoint": "ws://127.0.0.1:1/ws",
        }
    )
    value["override_policy"].update(
        {
            "configure_dialog_allowed": True,
            "development_defaults_allowed": True,
        }
    )
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_entrypoint_resolves_profile_and_validation_before_importing_qt():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert source.index("resolve_startup(") < source.index("from astral_client.app import main")
    assert source.index("_restore_frozen_standard_streams()") < source.index(
        "from win_agent.byo_worker import main"
    )
    assert source.index('if "--byo-worker"') < source.index("resolve_startup(")
    assert "--validate-deployment" not in (ROOT / "astral_client" / "app.py").read_text(
        encoding="utf-8"
    )


def test_actual_frozen_exe_validates_profile_worker_and_lock_without_qt(tmp_path):
    exe = _candidate_exe()
    report = tmp_path / "deployment-validation.json"
    result = subprocess.run(
        [str(exe), "--validate-deployment", "--report", str(report)],
        env=_clean_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(report.read_text(encoding="utf-8"))
    assert value["status"] == "valid"
    assert value["client_version"] == "0.4.0"
    assert value["source"] == "bundled_release"
    assert value["byo_host_disposition"] == "authenticated_ui_tunnel"
    assert value["legacy_tools_disposition"] == "disabled"
    assert value["requirements_lock_sha256"] == value["required_runtime_lock_sha256"]
    assert "authority" not in value and "websocket_endpoint" not in value


def test_actual_frozen_worker_completes_benign_hosted_agent_round_trip(tmp_path):
    exe = _candidate_exe()
    agent = tmp_path / "benign-agent"
    agent.mkdir()
    (agent / "agent_main.py").write_text(
        "import json, sys\n"
        "def main():\n"
        "    value = json.loads(sys.stdin.readline())\n"
        "    print(json.dumps({'type':'smoke_result','echo':value}), flush=True)\n"
        "    return 0\n",
        encoding="utf-8",
    )
    request = {"type": "smoke", "value": 6}
    result = subprocess.run(
        [str(exe), "--byo-worker", str(agent)],
        input=json.dumps(request) + "\n",
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "type": "smoke_result",
        "echo": request,
    }


@pytest.mark.skipif(sys.platform != "win32", reason="requires the frozen Windows GUI")
def test_actual_frozen_gui_completes_rendered_chat_with_one_profile(tmp_path):
    exe = _candidate_exe()
    token = os.getenv("ASTRAL_WINDOWS_SMOKE_TOKEN")
    if not token:
        pytest.skip("candidate staging token is required for the connected GUI smoke")
    _clear_native_windows_settings()
    report = tmp_path / "rendered-chat-smoke.json"
    environment = _clean_env(tmp_path)
    environment["ASTRAL_TOKEN"] = token
    result = subprocess.run(
        [
            str(exe),
            "--release-smoke-report",
            str(report),
            "--release-smoke-timeout",
            "60",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=75,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(report.read_text(encoding="utf-8"))
    assert value["status"] == "passed"
    assert value["detail_code"] == "rendered_turn_complete"
    assert value["transcript_turns"] >= 2
    assert value["canvas_components"] >= 1
    assert value["window_profile_match"] is True
    assert value["byo_profile_match"] is True
    assert value["tools_agent_profile_match"] is True


@pytest.mark.skipif(sys.platform != "win32", reason="requires the frozen Windows GUI")
def test_actual_frozen_gui_retains_selected_profile_during_offline_retry(tmp_path):
    exe = _candidate_exe()
    _clear_native_windows_settings()
    report = tmp_path / "offline-retry-smoke.json"
    profile = _offline_profile(tmp_path / "offline-profile.json")
    environment = _clean_env(tmp_path)
    environment["ASTRAL_TOKEN"] = "offline-smoke-token"
    result = subprocess.run(
        [
            str(exe),
            "--deployment-profile",
            str(profile),
            "--release-offline-smoke-report",
            str(report),
            "--release-smoke-timeout",
            "15",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(report.read_text(encoding="utf-8"))
    assert value["status"] == "passed"
    assert value["detail_code"] == "offline_retry_observed"
    assert value["source"] == "command_line_override"
    assert value["connection_failure_observed"] is True
    assert value["retry_attempt"] >= 1
    assert value["window_profile_match"] is True
    assert value["byo_profile_match"] is True
    assert value["tools_agent_profile_match"] is True
    assert "authority" not in value and "websocket_endpoint" not in value


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows HWND inspection")
def test_fresh_hkcu_connected_launch_has_no_configure_dialog_and_terminates(tmp_path):
    exe = _candidate_exe()
    token = os.getenv("ASTRAL_WINDOWS_SMOKE_TOKEN")
    if not token:
        pytest.skip("candidate staging token is required for the connected GUI smoke")
    _clear_native_windows_settings()
    environment = _clean_env(tmp_path)
    environment["ASTRAL_TOKEN"] = token
    process = subprocess.Popen([str(exe)], env=environment)
    titles: list[str] = []
    try:
        user32 = ctypes.windll.user32

        def _owned_pids() -> set[int]:
            """Every PID this launch owns, not just the one we spawned.

            ``AstralDeep.exe`` is packaged ONEFILE (``AstralDeep.spec`` passes
            ``a.binaries`` inline and has no ``COLLECT``), so the process we
            Popen is PyInstaller's bootloader: it unpacks to a temp dir and
            re-executes the real application as a CHILD. The Qt window therefore
            belongs to a descendant PID, and matching only ``process.pid`` found
            no windows at all — which is exactly how this test failed the first
            time it was ever executed. Recomputed each poll because the child
            does not exist yet on the first pass.
            """
            pids = {process.pid}
            try:
                import psutil

                pids.update(
                    child.pid
                    for child in psutil.Process(process.pid).children(recursive=True)
                )
            except Exception:
                # Never let inspection failure masquerade as a missing window:
                # the assertion below still fails honestly on the root pid.
                pass
            return pids

        def _window_titles() -> list[str]:
            found: list[str] = []
            owned = _owned_pids()
            callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

            def _visit(hwnd, _lparam):
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value not in owned or not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if buffer.value:
                    found.append(buffer.value)
                return True

            user32.EnumWindows(callback_type(_visit), 0)
            return found

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and process.poll() is None:
            titles = _window_titles()
            if any(title == "AstralDeep — Windows" for title in titles):
                break
            time.sleep(0.1)
        assert process.poll() is None
        assert "AstralDeep — Windows" in titles
        assert all("Configure AstralDeep" not in title for title in titles)
    finally:
        # Same onefile consequence: terminating the bootloader does not
        # necessarily take the extracted child with it, and a surviving GUI
        # process would outlive the job. Stop descendants first, then the root.
        try:
            import psutil

            for child in psutil.Process(process.pid).children(recursive=True):
                try:
                    child.terminate()
                except Exception:
                    pass
        except Exception:
            pass
        process.terminate()
        process.wait(timeout=10)
    assert process.returncode is not None
