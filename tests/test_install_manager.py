import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = REPO_ROOT / ".astralvessel_state.json"

# Helper to import the manager module
def import_manager():
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("install_manager", REPO_ROOT / "scripts" / "install_manager.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["install_manager"] = module
    spec.loader.exec_module(module)
    return module

@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    # Ensure a clean state file for each test
    manager = import_manager()
    monkeypatch.setattr(manager, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(manager, "STATE_FILE", tmp_path / ".astralvessel_state.json")
    state_path = tmp_path / ".astralvessel_state.json"
    if state_path.is_file():
        state_path.unlink()
    yield
    if state_path.is_file():
        state_path.unlink()

def test_resume_no_incomplete(monkeypatch):
    manager = import_manager()
    # Empty state => no incomplete install
    monkeypatch.setattr(manager, "load_state", lambda: {})
    with mock.patch.object(manager, "run_cmd") as mock_run:
        exit_code = manager.cmd_resume(mock.MagicMock())
        assert exit_code == 0
        mock_run.assert_not_called()

def test_resume_success(monkeypatch, tmp_path):
    manager = import_manager()
    state = {"status": "incomplete"}
    monkeypatch.setattr(manager, "load_state", lambda: state)
    monkeypatch.setattr(manager, "save_state", lambda s: None)
    # Mock keystore existence
    keystore = tmp_path / "vessel" / "keystore"
    keystore.mkdir(parents=True)
    monkeypatch.setattr(manager, "ensure_keystore_exists", lambda: None)
    with mock.patch.object(manager, "run_cmd") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        exit_code = manager.cmd_resume(mock.MagicMock())
        assert exit_code == 0
        mock_run.assert_called_once_with([manager.ASTRALVESSEL_CMD, "up", "--no-build"])

def test_upgrade_not_ready(monkeypatch):
    manager = import_manager()
    monkeypatch.setattr(manager, "load_state", lambda: {"status": "incomplete"})
    exit_code = manager.cmd_upgrade(mock.MagicMock())
    assert exit_code == 1

def test_upgrade_success(monkeypatch, tmp_path):
    manager = import_manager()
    monkeypatch.setattr(manager, "load_state", lambda: {"status": "ready"})
    monkeypatch.setattr(manager, "ensure_keystore_exists", lambda: None)
    with mock.patch.object(manager, "run_cmd") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        exit_code = manager.cmd_upgrade(mock.MagicMock())
        assert exit_code == 0
        # pull then up
        expected_calls = [
            mock.call([manager.ASTRALVESSEL_CMD, "pull"]),
            mock.call([manager.ASTRALVESSEL_CMD, "up", "--no-build", "--force-recreate"]),
        ]
        mock_run.assert_has_calls(expected_calls)

def test_remove_not_ready(monkeypatch):
    manager = import_manager()
    monkeypatch.setattr(manager, "load_state", lambda: {"status": "incomplete"})
    exit_code = manager.cmd_remove(mock.MagicMock())
    assert exit_code == 1

def test_remove_success(monkeypatch, tmp_path):
    manager = import_manager()
    monkeypatch.setattr(manager, "load_state", lambda: {"status": "ready"})
    # Create state file to be removed
    state_file = tmp_path / ".astralvessel_state.json"
    state_file.write_text(json.dumps({"status": "ready"}))
    with mock.patch.object(manager, "run_cmd") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        exit_code = manager.cmd_remove(mock.MagicMock())
        assert exit_code == 0
        mock_run.assert_called_once_with([manager.ASTRALVESSEL_CMD, "down"])
        assert not state_file.is_file()
