"""Runs inside a copied bundle and proves its Python, native libraries, tools and model resolve from the bundle alone.
verify_relocation.sh runs it with a stripped environment and the original bundle directory moved away.
"""
# ruff: noqa: E402

import importlib
import io
import os
import shutil
import subprocess
import sys

BUNDLE = os.path.realpath(os.path.join(sys.prefix, os.pardir))


def inside(path):
    return os.path.realpath(path).startswith(BUNDLE + os.sep)


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {label} {detail}".rstrip())
    if not ok:
        raise SystemExit(1)


check("interpreter inside bundle", inside(sys.executable), sys.executable)
check("no other python on PATH", shutil.which("python3") is None and shutil.which("python") is None)

import magic

check("python-magic loads bundle libmagic", inside(magic.libmagic._name), magic.libmagic._name)
check("magic database from bundle", os.environ["MAGIC"].startswith(BUNDLE))
check("mime sniff", magic.from_buffer(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n", mime=True) == "application/pdf")

from reportlab.pdfgen import canvas

buffer = io.BytesIO()
page = canvas.Canvas(buffer)
page.drawString(72, 720, "bundle relocation check")
page.save()
from pdf2image import convert_from_bytes, pdfinfo_from_bytes

check("pdftoppm from bundle", inside(shutil.which("pdftoppm")), shutil.which("pdftoppm"))
check("pdfinfo reads page count", pdfinfo_from_bytes(buffer.getvalue())["Pages"] == 1)
check("pdftoppm renders a page", convert_from_bytes(buffer.getvalue(), dpi=50)[0].size[0] > 0)
from pypdf import PdfReader

check("pypdf text", "relocation" in PdfReader(io.BytesIO(buffer.getvalue())).pages[0].extract_text())

check("ripgrep from bundle", inside(shutil.which("rg")), shutil.which("rg"))
found = subprocess.run(["rg", "--no-config", "-c", "relocation", "-"], input=b"relocation\n", capture_output=True)
check("ripgrep runs", found.stdout.strip() == b"1")

import spacy

nlp = spacy.load("en_core_web_lg")
check("spaCy model from bundle", inside(nlp.path), str(nlp.path))
check("spaCy parses", any(ent.label_ == "PERSON" for ent in nlp("Barack Obama visited Lexington.").ents))
from presidio_analyzer import AnalyzerEngine

hits = AnalyzerEngine().analyze(text="Patient John Smith, SSN 536-90-4399", language="en")
check("presidio finds PHI with the model", {hit.entity_type for hit in hits} >= {"PERSON", "US_SSN"})

sys.path.insert(0, os.path.join(BUNDLE, "app", "backend"))
for module in ("astral_kernel", "astralplane", "astralprojection", "astralprims", "lets", "rote",
               "psycopg2", "openslide", "SimpleITK", "orchestrator.orchestrator", "start"):
    loaded = importlib.import_module(module)
    check(f"import {module}", True, os.path.dirname(getattr(loaded, "__file__", "") or ""))

import astral_kernel

check("kernel dylib from bundle", inside(astral_kernel.__file__))
mailbox = subprocess.run([os.path.join(BUNDLE, "native", "bin", "astral-mailbox"), "--help"], capture_output=True, text=True)
check("mailbox binary executes", mailbox.returncode in (0, 2, 64), f"exit {mailbox.returncode}")

maps = subprocess.run(["vmmap", "-w", str(os.getpid())], capture_output=True, text=True).stdout
outside = sorted({line.split()[-1] for line in maps.splitlines() if ("/opt/homebrew" in line or "/usr/local" in line) and ".dylib" in line})
check("no Homebrew or /usr/local dylib mapped", not outside, " ".join(outside))
print("ALL CHECKS PASSED")
