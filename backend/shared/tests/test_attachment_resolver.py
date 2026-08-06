"""Tests for shared.attachment_resolver.resolve_attachment_path (T036, T041)."""
from unittest.mock import MagicMock, patch

import pytest

from shared.attachment_resolver import resolve_attachment_path


def test_empty_handle_raises() -> None:
    with pytest.raises(ValueError, match="required"):
        resolve_attachment_path("", "alice")


def test_absolute_existing_path_is_not_trusted(tmp_path) -> None:
    """An existing absolute path is still just an unknown handle — it must go
    through the ownership query, never short-circuit it."""
    f = tmp_path / "data.csv"
    f.write_text("col1,col2\n1,2\n")
    fake_repo = MagicMock()
    fake_repo.get_by_id.return_value = None
    with patch("orchestrator.attachments.repository.AttachmentRepository", return_value=fake_repo), \
         patch("agents.general.file_tools._get_database", return_value=MagicMock()):
        with pytest.raises(ValueError, match="not a valid attachment"):
            resolve_attachment_path(str(f), "alice")
        fake_repo.get_by_id.assert_called_with(str(f), "alice")


def test_system_path_is_not_trusted() -> None:
    """A well-known sensitive path must not be readable through the handle."""
    fake_repo = MagicMock()
    fake_repo.get_by_id.return_value = None
    with patch("orchestrator.attachments.repository.AttachmentRepository", return_value=fake_repo), \
         patch("agents.general.file_tools._get_database", return_value=MagicMock()):
        with pytest.raises(ValueError, match="not a valid attachment"):
            resolve_attachment_path("/etc/passwd", "alice")


def test_unknown_handle_raises_value_error() -> None:
    """When the DB returns None, the resolver surfaces a clear user-facing error."""
    fake_repo = MagicMock()
    fake_repo.get_by_id.return_value = None
    with patch("orchestrator.attachments.repository.AttachmentRepository", return_value=fake_repo), \
         patch("agents.general.file_tools._get_database", return_value=MagicMock()):
        # Need to also patch the imports at the function level
        with pytest.raises(ValueError, match="not a valid attachment"):
            resolve_attachment_path("att-nonexistent", "alice")


def test_user_isolation_enforced_by_repo_query() -> None:
    """The resolver delegates to ``repo.get_by_id(handle, user_id)`` —
    the repository's per-user filter ensures user A can't read user B's attachments."""
    fake_repo = MagicMock()
    fake_repo.get_by_id.return_value = None  # repo returns None for foreign user
    with patch("orchestrator.attachments.repository.AttachmentRepository", return_value=fake_repo), \
         patch("agents.general.file_tools._get_database", return_value=MagicMock()):
        with pytest.raises(ValueError):
            resolve_attachment_path("att-belongs-to-bob", "alice")
        fake_repo.get_by_id.assert_called_with("att-belongs-to-bob", "alice")


def test_storage_path_must_exist_on_disk(tmp_path) -> None:
    """If the DB row points to a file that's been deleted, surface a clear error."""
    nonexistent = tmp_path / "ghost.csv"  # not created
    fake_repo = MagicMock()
    fake_repo.get_by_id.return_value = MagicMock(storage_path=str(nonexistent))
    with patch("orchestrator.attachments.repository.AttachmentRepository", return_value=fake_repo), \
         patch("agents.general.file_tools._get_database", return_value=MagicMock()):
        with pytest.raises(ValueError, match="no longer exists on disk"):
            resolve_attachment_path("att-deleted", "alice")


def test_resolved_path_is_returned(tmp_path) -> None:
    f = tmp_path / "real.csv"
    f.write_text("a,b\n1,2\n")
    fake_repo = MagicMock()
    fake_repo.get_by_id.return_value = MagicMock(storage_path=str(f))
    with patch("orchestrator.attachments.repository.AttachmentRepository", return_value=fake_repo), \
         patch("agents.general.file_tools._get_database", return_value=MagicMock()):
        result = resolve_attachment_path("att-real", "alice")
        assert result == str(f)
