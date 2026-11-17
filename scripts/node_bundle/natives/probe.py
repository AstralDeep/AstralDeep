"""Runs inside a relocated node pack with the pack's Python and proves libmagic, poppler, ripgrep, spaCy and Presidio all resolve from the pack alone.
build_macos.py starts it with a minimal environment, and the launcher's native_environment supplies the paths the node would have.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(os.path.realpath(sys.argv[1]))
FAILED = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + label + (" " + detail if detail else ""))
    if not ok:
        FAILED.append(label)


def inside(path: str) -> bool:
    return os.path.realpath(path).startswith(str(ROOT) + os.sep)


def pdf(objects: list[bytes]) -> bytes:
    body, offsets = b"%PDF-1.4\n", []
    for number, content in enumerate(objects, 1):
        offsets.append(len(body))
        body += b"%d 0 obj\n" % number + content + b"\nendobj\n"
    start = len(body)
    body += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    body += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return body + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, start)


def stream(dictionary: bytes, data: bytes) -> bytes:
    return b"<< " + dictionary + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def page_objects(content: bytes, resources: bytes = b"<< >>", extra: list[bytes] | None = None) -> list[bytes]:
    return [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R /Resources " + resources + b" >>",
        stream(b"", content),
        *(extra or []),
    ]


def square_pdf() -> bytes:
    return pdf(page_objects(b"0 0 0 rg 10 10 80 80 re f"))


def jpeg_pdf() -> bytes | None:
    try:
        import io

        from PIL import Image
    except ImportError:
        return None
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), (0, 0, 0)).save(buffer, "JPEG")
    image = stream(b"/Type /XObject /Subtype /Image /Width 20 /Height 20 /ColorSpace /DeviceRGB "
                   b"/BitsPerComponent 8 /Filter /DCTDecode", buffer.getvalue())
    return pdf(page_objects(b"q 80 0 0 80 10 10 cm /Im0 Do Q", b"<< /XObject << /Im0 5 0 R >> >>", [image]))


def flate_pdf() -> bytes:
    pixels = zlib.compress(bytes(20 * 20 * 3))
    image = stream(b"/Type /XObject /Subtype /Image /Width 20 /Height 20 /ColorSpace /DeviceRGB "
                   b"/BitsPerComponent 8 /Filter /FlateDecode", pixels)
    return pdf(page_objects(b"q 80 0 0 80 10 10 cm /Im0 Do Q", b"<< /XObject << /Im0 5 0 R >> >>", [image]))


def dark_pixels(ppm: bytes) -> int:
    header = ppm.split(b"\n", 3)
    return sum(1 for value in header[3] if value < 128)


def render(document: bytes, directory: Path) -> int:
    target = directory / "page.pdf"
    target.write_bytes(document)
    done = subprocess.run(["pdftoppm", "-r", "36", str(target)], capture_output=True, cwd=directory, check=False)
    pages = sorted(directory.glob("page-*.ppm"))
    return dark_pixels(pages[0].read_bytes()) if done.returncode == 0 and pages else -1


def probe_environment() -> None:
    sys.path.insert(0, str(ROOT / "launcher"))
    import astral_node_launch

    os.environ.update(astral_node_launch.native_environment(ROOT))
    for name in ("pdfinfo", "pdftoppm", "rg"):
        found = shutil.which(name)
        check(f"{name} resolves inside the pack", found is not None and inside(found), str(found))


def probe_magic() -> None:
    import magic

    check("libmagic is loaded from the pack", inside(magic.libmagic._name), magic.libmagic._name)
    check("the magic database is inside the pack", inside(os.environ.get("MAGIC", "")), os.environ.get("MAGIC", ""))
    check("magic.from_buffer sniffs a PDF", magic.from_buffer(square_pdf(), mime=True) == "application/pdf")
    check("magic.from_buffer sniffs plain text", magic.from_buffer(b"hello world\n", mime=True) == "text/plain")


def probe_poppler(directory: Path) -> None:
    info = subprocess.run(["pdfinfo", "-"], input=square_pdf(), capture_output=True, check=False)
    check("pdfinfo reads the fixture", info.returncode == 0 and b"Pages:           1" in info.stdout,
          info.stderr.decode()[:200])
    check("pdftoppm renders drawn content", render(square_pdf(), directory) > 100)
    check("pdftoppm renders Flate image content", render(flate_pdf(), directory) > 100)
    from pdf2image import convert_from_bytes

    pages = convert_from_bytes(square_pdf(), dpi=36)
    check("pdf2image converts through the pack's poppler", len(pages) == 1 and pages[0].size == (50, 50))
    document = jpeg_pdf()
    if document is not None:
        dark = render(document, directory)
        print(f"INFO a JPEG image in a PDF renders {dark} dark samples (0 means DCT images draw blank)")


def probe_ripgrep(directory: Path) -> None:
    sample = directory / "sample.txt"
    sample.write_text("alpha\nneedle here\nomega\n")
    found = subprocess.run(["rg", "--no-config", "-n", "needle", str(sample)], capture_output=True, text=True,
                           check=False)
    check("rg matches", found.returncode == 0 and "2:needle here" in found.stdout, found.stdout)


def probe_language() -> None:
    import spacy

    nlp = spacy.load("en_core_web_lg")
    document = nlp("Dr. Alice Johnson works at Mayo Clinic in Rochester.")
    check("spacy loads the model from the pack", inside(str(nlp.path)), str(nlp.path))
    check("spacy finds a person", any(entity.label_ == "PERSON" for entity in document.ents),
          str([(entity.text, entity.label_) for entity in document.ents]))
    from presidio_analyzer import AnalyzerEngine

    analyzer = AnalyzerEngine()
    text = "Patient John Smith has social security number 536-90-4399."
    kinds = {found.entity_type for found in analyzer.analyze(text=text, language="en")}
    check("presidio finds PERSON", "PERSON" in kinds, str(sorted(kinds)))
    check("presidio finds US_SSN", "US_SSN" in kinds, str(sorted(kinds)))


def main() -> None:
    probe_environment()
    with tempfile.TemporaryDirectory(prefix="node-pack-probe-") as scratch:
        directory = Path(scratch)
        probe_magic()
        probe_poppler(directory)
        probe_ripgrep(directory)
    probe_language()
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
