"""Model-facing file arguments must not read arbitrary container files (H3).

``general.modify_data`` and ``medical.analyze_csv_file`` both run in-process
in the orchestrator (FF_INPROCESS_AGENTS), so an unconstrained path argument
reads the orchestrator's own filesystem. These tests pin that:

  * no absolute path reaches ``open()`` through ``file_path``/``file_handle``,
  * neither tool advertises a path parameter to the model, and
  * a legitimately owned attachment still resolves and reads.
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from agents.general.mcp_tools import TOOL_REGISTRY as GENERAL_TOOLS, modify_data
from agents.medical.mcp_tools import TOOL_REGISTRY as MEDICAL_TOOLS, analyze_csv_file

SENSITIVE_PATHS = ["/etc/passwd", "/app/backend/orchestrator/orchestrator.py"]


def _alert_messages(result):
    return " ".join(
        str(c.get("message", ""))
        for c in (result.get("_ui_components") or [])
    )


# ---------------------------------------------------------------------------
# modify_data (general agent)
# ---------------------------------------------------------------------------

def test_modify_data_refuses_absolute_file_path():
    for path in SENSITIVE_PATHS:
        result = modify_data(
            file_path=path,
            modifications=[{"action": "add_column", "name": "x", "value": "1"}],
            user_id="alice",
        )
        assert result["_data"] is None, f"{path} was read"
        assert "attachment_id" in _alert_messages(result)


def test_modify_data_refuses_absolute_path_as_file_handle():
    """The resolver is the only path source — a handle is never a path."""
    fake_repo = MagicMock()
    fake_repo.get_by_id.return_value = None
    with patch("orchestrator.attachments.repository.AttachmentRepository", return_value=fake_repo):
        result = modify_data(
            file_handle="/etc/passwd",
            modifications=[],
            user_id="alice",
        )
    assert result["_data"] is None
    assert "not a valid attachment" in _alert_messages(result)


def test_modify_data_refuses_traversal_out_of_user_directory(tmp_path):
    """A path that walks out of the caller's own directory is refused."""
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    escape = os.path.join(backend_dir, "tmp", "alice", "..", "..", "requirements.txt")
    result = modify_data(file_path=escape, modifications=[], user_id="alice")
    assert result["_data"] is None
    assert "attachment_id" in _alert_messages(result)


def test_modify_data_refuses_another_users_directory():
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    bob_dir = os.path.join(backend_dir, "tmp", "bob")
    os.makedirs(bob_dir, exist_ok=True)
    victim = os.path.join(bob_dir, "bob_secret.csv")
    with open(victim, "w") as f:
        f.write("secret\n1\n")
    try:
        result = modify_data(file_path=victim, modifications=[], user_id="alice")
        assert result["_data"] is None
        assert "attachment_id" in _alert_messages(result)
    finally:
        os.remove(victim)


def test_modify_data_reads_owned_attachment_via_file_handle(tmp_path):
    """The supported path still works end to end."""
    src = tmp_path / "owned.csv"
    src.write_text("name,age\nAlice,25\n")
    fake_repo = MagicMock()
    fake_repo.get_by_id.return_value = MagicMock(storage_path=str(src))
    with patch("orchestrator.attachments.repository.AttachmentRepository", return_value=fake_repo):
        result = modify_data(
            file_handle="att-owned",
            modifications=[{"action": "add_column", "name": "status", "value": "ok"}],
            user_id="alice",
        )
    fake_repo.get_by_id.assert_called_with("att-owned", "alice")
    assert result["_data"] is not None, _alert_messages(result)
    out_path = result["_data"]["file_path"]
    try:
        with open(out_path) as f:
            content = f.read()
        assert "status" in content and "Alice" in content
    finally:
        os.remove(out_path)


def test_modify_data_schema_has_no_path_parameter():
    props = GENERAL_TOOLS["modify_data"]["input_schema"]["properties"]
    assert "file_path" not in props
    assert "file_handle" in props
    assert "file_path" not in GENERAL_TOOLS["modify_data"]["description"]


# ---------------------------------------------------------------------------
# analyze_csv_file (medical agent)
# ---------------------------------------------------------------------------

def test_analyze_csv_file_refuses_absolute_file_path():
    """``file_path`` is no longer a parameter — it lands in **kwargs and is
    ignored, leaving the tool with no attachment to read."""
    for path in SENSITIVE_PATHS:
        result = analyze_csv_file(file_path=path, user_id="alice")
        assert result["_data"] is None, f"{path} was read"
        assert "attachment_id is required" in _alert_messages(result)


def test_analyze_csv_file_refuses_path_as_attachment_id():
    fake_repo = MagicMock()
    fake_repo.get_by_id.return_value = None
    with patch("agents.general.file_tools.AttachmentRepository", return_value=fake_repo), \
         patch("agents.general.file_tools._get_database", return_value=MagicMock()):
        result = analyze_csv_file(attachment_id="/etc/passwd", user_id="alice")
    assert result["_data"] is None
    assert "not found" in _alert_messages(result)


def test_analyze_csv_file_refuses_call_without_user_context():
    result = analyze_csv_file(attachment_id="att-1")
    assert result["_data"] is None
    assert "without a user context" in _alert_messages(result)


def test_analyze_csv_file_reads_owned_attachment(tmp_path, monkeypatch):
    monkeypatch.setenv("ATTACHMENT_UPLOAD_ROOT", str(tmp_path))
    blob = tmp_path / "alice" / "att-1" / "vitals.csv"
    blob.parent.mkdir(parents=True)
    blob.write_text("patient,age\nP-1,45\nP-2,52\n")

    fake_repo = MagicMock()
    fake_repo.get_by_id.return_value = MagicMock(
        storage_path="alice/att-1/vitals.csv", extension="csv"
    )
    with patch("agents.general.file_tools.AttachmentRepository", return_value=fake_repo), \
         patch("agents.general.file_tools._get_database", return_value=MagicMock()):
        result = analyze_csv_file(attachment_id="att-1", user_id="alice")

    fake_repo.get_by_id.assert_called_with("att-1", "alice")
    assert result["_data"] is not None, _alert_messages(result)
    assert result["_data"]["processed_rows"] == 2
    assert result["_data"]["columns"] == ["patient", "age"]


def test_analyze_csv_file_schema_has_no_path_parameter():
    entry = MEDICAL_TOOLS["analyze_csv_file"]
    props = entry["input_schema"]["properties"]
    assert "file_path" not in props
    assert props["attachment_id"]["type"] == "string"
    assert entry["input_schema"]["required"] == ["attachment_id"]
    assert "file_path" not in entry["description"]
