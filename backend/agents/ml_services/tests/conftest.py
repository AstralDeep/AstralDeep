"""Shared fixtures for the ML Services tool suites.

These tests hand ``resolve_attachment_path`` a real temp file as the
``file_handle``. The resolver has no absolute-path escape hatch (it must
not — the handle comes straight from a model's tool arguments), so the
suite stands in a fake attachments repository that treats an existing
temp path as an attachment the caller owns. The real ownership query is
still the code under test: a handle the fake repo does not recognise
resolves to ``None`` and the resolver raises, exactly as in production.
"""
import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def stub_attachment_repo():
    def _get_by_id(handle, user_id):
        if handle and os.path.isabs(str(handle)) and os.path.exists(str(handle)):
            return MagicMock(storage_path=str(handle))
        return None

    fake_repo = MagicMock()
    fake_repo.get_by_id.side_effect = _get_by_id
    with patch("orchestrator.attachments.repository.AttachmentRepository", return_value=fake_repo), \
         patch("shared.attachment_resolver._open_db", return_value=MagicMock()):
        yield fake_repo
