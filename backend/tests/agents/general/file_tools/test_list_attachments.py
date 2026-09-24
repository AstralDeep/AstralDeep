"""Tests for agents/general/file_tools/list_attachments.py: ownership-scoped listing,
category filtering, and the required-user check.
"""

from __future__ import annotations

from agents.general.file_tools.list_attachments import list_attachments
from conftest import _persist, make_csv, make_png


def test_list_returns_only_caller_files(repo, upload_root):
    _persist(repo, user_id="alice", filename="a.csv",
             category="spreadsheet", extension="csv",
             content_type="text/csv", upload_root=upload_root,
             payload=make_csv([["x"], [1]]))
    _persist(repo, user_id="alice", filename="b.png",
             category="image", extension="png",
             content_type="image/png", upload_root=upload_root,
             payload=make_png())
    _persist(repo, user_id="bob", filename="c.csv",
             category="spreadsheet", extension="csv",
             content_type="text/csv", upload_root=upload_root,
             payload=make_csv([["x"], [1]]))

    alice = list_attachments(user_id="alice")
    assert len(alice["attachments"]) == 2
    bob = list_attachments(user_id="bob")
    assert len(bob["attachments"]) == 1


def test_list_category_filter(repo, upload_root):
    _persist(repo, user_id="alice", filename="a.csv",
             category="spreadsheet", extension="csv",
             content_type="text/csv", upload_root=upload_root,
             payload=make_csv([["x"], [1]]))
    _persist(repo, user_id="alice", filename="b.png",
             category="image", extension="png",
             content_type="image/png", upload_root=upload_root,
             payload=make_png())

    only_images = list_attachments(user_id="alice", category="image")
    assert len(only_images["attachments"]) == 1
    assert only_images["attachments"][0]["category"] == "image"


def test_list_requires_user(repo, upload_root):
    out = list_attachments()
    assert "error" in out
