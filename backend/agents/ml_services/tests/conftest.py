"""Shared bounded-reader fixture for the ML Services tool suites.

Production resolves an attachment identity through Plane and yields metadata
plus a bounded reader. These isolated HTTP-wrapper tests use existing temporary
files as synthetic identities and patch only the imported reader seam; resolver
ownership and integrity behavior have their own focused integration tests.
"""
from contextlib import contextmanager
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def scoped_attachment_blob_reader():
    class _Reader:
        def __init__(self, path):
            self._path = path

        def iter_chunks(self):
            with open(self._path, "rb") as stream:
                while chunk := stream.read(64 * 1024):
                    yield chunk

    @contextmanager
    def _reader(handle, user_id):
        if not user_id:
            raise ValueError("user_id is required to resolve attachments")
        path = os.fspath(handle)
        if not os.path.isabs(path) or not os.path.isfile(path):
            raise ValueError("file_handle is not an available test attachment")
        yield SimpleNamespace(filename=os.path.basename(path)), _Reader(path)

    targets = (
        "agents.ml_services.classify_tools.open_attachment_blob_reader",
        "agents.ml_services.forecaster_tools.open_attachment_blob_reader",
        "agents.ml_services.llm_factory_tools.open_attachment_blob_reader",
    )
    with patch(targets[0], _reader), patch(targets[1], _reader), patch(targets[2], _reader):
        yield
