"""Tests for agents/general/file_tools/read_text.py: UTF-8 and charset-fallback
decoding, JSON/YAML/XML/HTML/code language tagging, and max-chars truncation.
"""

from __future__ import annotations

from agents.general.file_tools.read_text import read_text
from conftest import _persist


def _put(repo, upload_root, *, name, ext, payload):
    return _persist(repo, user_id="alice", filename=name,
                    category="text", extension=ext,
                    content_type="text/plain", upload_root=upload_root,
                    payload=payload)


def test_plain_text(repo, upload_root):
    aid = _put(repo, upload_root, name="x.txt", ext="txt", payload=b"hello world")
    out = read_text(attachment_id=aid, user_id="alice")
    assert out["text"] == "hello world"
    assert out["language"] == "text"
    assert out["plaintext"] is None


def test_python_language_tag(repo, upload_root):
    aid = _put(repo, upload_root, name="x.py", ext="py",
               payload=b"def f():\n    return 1\n")
    out = read_text(attachment_id=aid, user_id="alice")
    assert out["language"] == "python"
    assert "def f" in out["text"]


def test_html_strips_to_plaintext(repo, upload_root):
    raw = b"<html><body><h1>Hello</h1><p>World</p><script>bad()</script></body></html>"
    aid = _put(repo, upload_root, name="x.html", ext="html", payload=raw)
    out = read_text(attachment_id=aid, user_id="alice")
    assert "Hello" in out["plaintext"]
    assert "bad()" not in out["plaintext"]


def test_xml_strips_to_plaintext(repo, upload_root):
    raw = b"<root><a>Alpha</a><b>Beta</b></root>"
    aid = _put(repo, upload_root, name="x.xml", ext="xml", payload=raw)
    out = read_text(attachment_id=aid, user_id="alice")
    assert "Alpha" in out["plaintext"]
    assert "Beta" in out["plaintext"]


def test_max_chars_truncation(repo, upload_root):
    aid = _put(repo, upload_root, name="big.txt", ext="txt", payload=b"x" * 5000)
    out = read_text(attachment_id=aid, user_id="alice", max_chars=100)
    assert out["truncated"] is True
    assert len(out["text"]) == 100


def test_charset_fallback(repo, upload_root):
    payload = "café\n".encode("latin-1")
    aid = _put(repo, upload_root, name="x.txt", ext="txt", payload=payload)
    out = read_text(attachment_id=aid, user_id="alice")
    assert "caf" in out["text"]
