"""read_presentation: PPTX."""

from __future__ import annotations

import io

from agents.general.file_tools import read_presentation as presentation_module
from agents.general.file_tools.read_presentation import read_presentation
from conftest import _persist, make_pptx


def test_read_pptx(repo, upload_root):
    aid = _persist(repo, user_id="alice", filename="kickoff.pptx",
                   category="presentation", extension="pptx",
                   content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                   upload_root=upload_root,
                   payload=make_pptx([
                       ("Q4 Goals", "Body of slide one"),
                       ("Risks", "Body of slide two"),
                   ]))
    out = read_presentation(attachment_id=aid, user_id="alice")
    assert "error" not in out
    assert out["slide_count"] == 2
    titles = [s["title"] for s in out["slides"]]
    assert "Q4 Goals" in titles
    assert "Risks" in titles


def test_read_pptx_slide_range(repo, upload_root):
    aid = _persist(repo, user_id="alice", filename="kickoff.pptx",
                   category="presentation", extension="pptx",
                   content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                   upload_root=upload_root,
                   payload=make_pptx([(f"Title {i}", "body") for i in range(5)]))
    out = read_presentation(attachment_id=aid, user_id="alice", slide_range="2-3")
    assert [s["slide_number"] for s in out["slides"]] == [2, 3]


def test_odp_parser_accepts_bytes_io():
    from odf import draw, text
    from odf.opendocument import OpenDocumentPresentation

    document = OpenDocumentPresentation()
    page = draw.Page(name="page1", masterpagename="Default")
    frame = draw.Frame()
    box = draw.TextBox()
    box.addElement(text.P(text="Slide body"))
    frame.addElement(box)
    page.addElement(frame)
    document.presentation.addElement(page)
    payload = io.BytesIO()
    document.write(payload)

    result = presentation_module._read_odp(payload.getvalue(), None)

    assert result["slide_count"] == 1
    assert result["slides"][0]["text"] == "Slide body"
