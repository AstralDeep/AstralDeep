"""read_czi keeps random-access parsing inside a scoped Plane lease."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from agents.general.file_tools.medical.read_czi import read_czi

read_czi_module = importlib.import_module(
    "agents.general.file_tools.medical.read_czi"
)


def test_read_czi_passes_scoped_path_to_parser(monkeypatch):
    observed = {}

    class FakeCzi:
        dims = "SCZYX"
        pixel_type = "gray16"

        def __init__(self, source):
            observed["path"] = source

        @staticmethod
        def get_dims_shape():
            return [{"Z": (0, 3)}]

        @staticmethod
        def is_mosaic():
            return False

        @staticmethod
        def read_image(**kwargs):
            observed["kwargs"] = kwargs
            return np.arange(16, dtype=np.uint16).reshape(1, 1, 4, 4), None

    monkeypatch.setattr(
        read_czi_module,
        "resolve_attachment",
        lambda *_args, **_kwargs: (
            SimpleNamespace(filename="sample.czi"),
            Path("leased-sample.czi"),
            None,
        ),
    )
    import aicspylibczi

    monkeypatch.setattr(aicspylibczi, "CziFile", FakeCzi)

    result = read_czi("att-czi", user_id="alice", scene=2)

    assert observed["path"] == str(Path("leased-sample.czi"))
    assert observed["kwargs"] == {"S": 2, "C": 0, "Z": 1}
    assert result["filename"] == "sample.czi"
    assert "thumbnail_png_base64" in result
