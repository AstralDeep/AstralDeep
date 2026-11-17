"""Renders non-embedded Helvetica, Times-Roman and Courier text and a JPEG image page with the pack's pdftoppm, and the JPEG page again through pdf2image and PIL, and fails if any output is blank.
relocation_check.py runs it with the pack's own Python in the clean image, so a missing font, fontconfig file, JPEG decoder or PIL fails the pack.
"""

import io
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

TEXT_FONTS = ("Helvetica", "Times-Roman", "Courier")
MINIMUM_TEXT_PIXELS = 150
PAGE = (300, 120)
IMAGE_PAGE = 200
DARK = 128


def pdf(objects: list[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    table = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{table}\n%%EOF\n".encode()
    return bytes(out)


def stream(dictionary: str, data: bytes) -> bytes:
    return f"<< {dictionary} /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream"


def text_pdf(font: str) -> bytes:
    content = b"BT /F1 36 Tf 10 40 Td (Hamburgefons) Tj ET"
    return pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE[0]} {PAGE[1]}] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>".encode(),
        stream("", content),
        f"<< /Type /Font /Subtype /Type1 /BaseFont /{font} >>".encode(),
    ])


def jpeg_page_pdf() -> bytes:
    picture = Image.new("RGB", (64, 64), "white")
    picture.paste((0, 0, 0), (0, 0, 32, 64))
    encoded = io.BytesIO()
    picture.save(encoded, format="JPEG", quality=95)
    content = f"q {IMAGE_PAGE} 0 0 {IMAGE_PAGE} 0 0 cm /Im0 Do Q".encode()
    return pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {IMAGE_PAGE} {IMAGE_PAGE}] /Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>".encode(),
        stream("", content),
        stream("/Type /XObject /Subtype /Image /Width 64 /Height 64 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode",
               encoded.getvalue()),
    ])


def dark_fraction(image: Image.Image) -> tuple[int, float]:
    pixels = image.convert("L").tobytes()
    dark = sum(1 for value in pixels if value < DARK)
    return dark, dark / max(1, len(pixels))


def render(document: bytes, directory: Path, name: str) -> Image.Image:
    source = directory / f"{name}.pdf"
    source.write_bytes(document)
    subprocess.run(["pdftoppm", "-r", "72", "-png", "-singlefile", str(source), str(directory / name)], check=True)
    return Image.open(directory / f"{name}.png")


def main() -> int:
    failed = []

    def check(label: str, ok: bool, detail: str) -> None:
        print(("PASS " if ok else "FAIL ") + label + " " + detail)
        if not ok:
            failed.append(label)

    with tempfile.TemporaryDirectory(prefix="node-pack-render-") as scratch:
        directory = Path(scratch)
        for font in TEXT_FONTS:
            dark, _ = dark_fraction(render(text_pdf(font), directory, font))
            check(f"non-embedded {font} renders", dark >= MINIMUM_TEXT_PIXELS, f"{dark} dark pixels")
        document = jpeg_page_pdf()
        dark, fraction = dark_fraction(render(document, directory, "jpeg"))
        check("JPEG page renders with pdftoppm", 0.3 < fraction < 0.7, f"{fraction:.2f} dark")
        from pdf2image import convert_from_bytes

        pages = convert_from_bytes(document, dpi=72, last_page=1)
        dark, fraction = dark_fraction(pages[0])
        check("JPEG page renders through pdf2image and PIL", 0.3 < fraction < 0.7, f"{fraction:.2f} dark")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
