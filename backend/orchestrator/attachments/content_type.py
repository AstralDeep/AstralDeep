"""Server-side upload content-type allow-list, extension normalization, and libmagic
sniffing; mirrors frontend/src/lib/attachmentTypes.ts (the two must stay in sync) and
backs attachments/router.py's upload validation.
"""

from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

try:  # pragma: no cover
    import magic  # type: ignore
    _HAS_MAGIC = True
except Exception:  # pragma: no cover
    magic = None  # type: ignore
    _HAS_MAGIC = False


AttachmentCategory = str


_COMPOUND_EXTENSIONS: Tuple[str, ...] = (
    "nii.gz",
    "ome.tif",
    "ome.tiff",
)


ACCEPTED_EXTENSIONS: Dict[str, AttachmentCategory] = {
    "pdf": "document",
    "docx": "document",
    "doc": "document",
    "rtf": "document",
    "odt": "document",
    "xlsx": "spreadsheet",
    "xls": "spreadsheet",
    "ods": "spreadsheet",
    "tsv": "spreadsheet",
    "csv": "spreadsheet",
    "pptx": "presentation",
    "ppt": "presentation",
    "odp": "presentation",
    "txt": "text",
    "md": "text",
    "json": "text",
    "yaml": "text",
    "yml": "text",
    "xml": "text",
    "html": "text",
    "htm": "text",
    "log": "text",
    "py": "text",
    "js": "text",
    "ts": "text",
    "tsx": "text",
    "jsx": "text",
    "sql": "text",
    "sh": "text",
    "ps1": "text",
    "css": "text",
    "png": "image",
    "jpg": "image",
    "jpeg": "image",
    "gif": "image",
    "webp": "image",
    "dcm": "medical",
    "dicom": "medical",
    "nii": "medical",
    "nii.gz": "medical",
    "czi": "medical",
    "nrrd": "medical",
    "mha": "medical",
    "mhd": "medical",
    "raw": "medical",
    "ome.tif": "medical",
    "ome.tiff": "medical",
    "tif": "medical",
    "tiff": "medical",
    "svs": "medical",
    "ndpi": "medical",
}


_EXTENSION_TO_MIME_PREFIXES: Dict[str, Tuple[str, ...]] = {
    "pdf": ("application/pdf",),
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
        "application/octet-stream",
    ),
    "doc": ("application/msword", "application/x-ole-storage", "application/octet-stream"),
    "rtf": ("application/rtf", "text/rtf", "text/plain"),
    "odt": ("application/vnd.oasis.opendocument.text", "application/zip", "application/octet-stream"),
    "xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/zip",
        "application/octet-stream",
    ),
    "xls": ("application/vnd.ms-excel", "application/x-ole-storage", "application/octet-stream"),
    "ods": ("application/vnd.oasis.opendocument.spreadsheet", "application/zip", "application/octet-stream"),
    "tsv": ("text/", "application/octet-stream"),
    "csv": ("text/", "application/csv", "application/octet-stream"),
    "pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/zip",
        "application/octet-stream",
    ),
    "ppt": ("application/vnd.ms-powerpoint", "application/x-ole-storage", "application/octet-stream"),
    "odp": ("application/vnd.oasis.opendocument.presentation", "application/zip", "application/octet-stream"),
    "txt": ("text/",), "md": ("text/",), "json": ("text/", "application/json"),
    "yaml": ("text/",), "yml": ("text/",),
    "xml": ("text/", "application/xml"),
    "html": ("text/",), "htm": ("text/",), "log": ("text/",),
    "py": ("text/",), "js": ("text/", "application/javascript"),
    "ts": ("text/", "application/typescript"),
    "tsx": ("text/",), "jsx": ("text/",),
    "sql": ("text/",), "sh": ("text/",), "ps1": ("text/",), "css": ("text/",),
    "png": ("image/png",),
    "jpg": ("image/jpeg",), "jpeg": ("image/jpeg",),
    "gif": ("image/gif",),
    "webp": ("image/webp",),
    "dcm": ("application/dicom", "application/octet-stream"),
    "dicom": ("application/dicom", "application/octet-stream"),
    "nii": ("image/x.nifti", "application/octet-stream"),
    "nii.gz": ("application/gzip", "application/x-gzip", "application/octet-stream"),
    "czi": ("application/octet-stream",),
    "nrrd": ("image/x.nrrd", "application/octet-stream", "text/plain"),
    "mha": ("application/octet-stream", "text/plain"),
    "mhd": ("application/octet-stream", "text/plain"),
    "raw": ("application/octet-stream",),
    "ome.tif": ("image/tiff", "application/octet-stream"),
    "ome.tiff": ("image/tiff", "application/octet-stream"),
    "tif": ("image/tiff", "application/octet-stream"),
    "tiff": ("image/tiff", "application/octet-stream"),
    "svs": ("image/tiff", "application/octet-stream"),
    "ndpi": ("image/tiff", "application/octet-stream"),
}


LEGACY_BINARY_FORMATS = frozenset({"doc", "ppt"})


_MB = 1024 * 1024
_GB = 1024 * _MB

MAX_BYTES_BY_CATEGORY: Dict[AttachmentCategory, int] = {
    "document": 30 * _MB,
    "spreadsheet": 30 * _MB,
    "presentation": 30 * _MB,
    "text": 30 * _MB,
    "image": 30 * _MB,
    "medical": 2 * _GB,
    "data": 100 * _MB,
    "archive": 100 * _MB,
}


_TEXT_EXTS_031: Tuple[str, ...] = (
    "markdown", "rst", "tex", "org", "adoc",
    "toml", "ini", "cfg", "conf", "properties", "ndjson", "jsonl", "geojson",
    "java", "c", "h", "cpp", "hpp", "cc", "cxx", "cs", "go", "rs", "rb",
    "php", "swift", "kt", "kts", "scala", "r", "lua", "dart", "vue", "svelte",
    "bat", "cmd", "groovy", "gradle", "clj", "cljs", "ex", "exs", "erl", "hs",
    "fs", "vb", "asm", "proto", "graphql", "gql", "pl", "pm",
    "ipynb",
)
_DATA_EXTS_031: Tuple[str, ...] = (
    "parquet", "avro", "feather", "orc", "arrow",
    "h5", "hdf5", "npy", "npz", "mat",
    "sav", "dta", "sas7bdat",
    "db", "sqlite", "sqlite3",
)
_ARCHIVE_EXTS_031: Tuple[str, ...] = (
    "zip", "tar", "gz", "tgz", "bz2", "tbz2", "xz", "txz", "7z", "rar", "epub",
)

_ARCHIVE_MIME_031: Dict[str, Tuple[str, ...]] = {
    "zip": ("application/zip",),
    "tar": ("application/x-tar",),
    "gz": ("application/gzip", "application/x-gzip"),
    "tgz": ("application/gzip", "application/x-gzip"),
    "bz2": ("application/x-bzip2",),
    "tbz2": ("application/x-bzip2",),
    "xz": ("application/x-xz",),
    "txz": ("application/x-xz",),
    "7z": ("application/x-7z-compressed",),
    "rar": ("application/x-rar", "application/vnd.rar"),
    "epub": ("application/epub+zip", "application/zip"),
}
_DATA_MIME_031: Dict[str, Tuple[str, ...]] = {
    "sqlite": ("application/x-sqlite3", "application/vnd.sqlite3"),
    "sqlite3": ("application/x-sqlite3", "application/vnd.sqlite3"),
    "db": ("application/x-sqlite3", "application/vnd.sqlite3"),
}

AUTO_PARSE_CATEGORIES: Tuple[str, ...] = ("data", "archive")

for _ext in _TEXT_EXTS_031:
    ACCEPTED_EXTENSIONS.setdefault(_ext, "text")
    _EXTENSION_TO_MIME_PREFIXES.setdefault(
        _ext, ("text/", "application/json", "application/xml", "application/octet-stream")
    )
for _ext in _DATA_EXTS_031:
    ACCEPTED_EXTENSIONS.setdefault(_ext, "data")
    _EXTENSION_TO_MIME_PREFIXES.setdefault(
        _ext, _DATA_MIME_031.get(_ext, ()) + ("application/octet-stream",)
    )
for _ext in _ARCHIVE_EXTS_031:
    ACCEPTED_EXTENSIONS.setdefault(_ext, "archive")
    _EXTENSION_TO_MIME_PREFIXES.setdefault(
        _ext, _ARCHIVE_MIME_031.get(_ext, ()) + ("application/octet-stream",)
    )


def normalise_extension(filename: str) -> str:
    lower = filename.lower()
    for compound in _COMPOUND_EXTENSIONS:
        if lower.endswith("." + compound):
            return compound
    _, ext = os.path.splitext(lower)
    return ext[1:] if ext else ""


def category_for_extension(extension: str) -> Optional[AttachmentCategory]:
    return ACCEPTED_EXTENSIONS.get(extension.lower())


def max_bytes_for_category(category: AttachmentCategory) -> int:
    cap = MAX_BYTES_BY_CATEGORY.get(category)
    if cap is None:
        return min(MAX_BYTES_BY_CATEGORY.values())
    return cap


def sniff_content_type(blob_path_or_bytes) -> str:
    if not _HAS_MAGIC:
        return "application/octet-stream"
    try:
        if isinstance(blob_path_or_bytes, (bytes, bytearray)):
            return magic.from_buffer(bytes(blob_path_or_bytes), mime=True)
        return magic.from_file(str(blob_path_or_bytes), mime=True)
    except Exception:  # pragma: no cover
        return "application/octet-stream"


def is_consistent(extension: str, sniffed_mime: str) -> bool:
    extension = extension.lower()
    if not sniffed_mime:
        return True
    prefixes = _EXTENSION_TO_MIME_PREFIXES.get(extension)
    if not prefixes:
        return False
    sniffed_lower = sniffed_mime.lower()
    return any(sniffed_lower.startswith(p) for p in prefixes)


__all__ = [
    "ACCEPTED_EXTENSIONS",
    "AUTO_PARSE_CATEGORIES",
    "AttachmentCategory",
    "LEGACY_BINARY_FORMATS",
    "MAX_BYTES_BY_CATEGORY",
    "category_for_extension",
    "is_consistent",
    "max_bytes_for_category",
    "normalise_extension",
    "sniff_content_type",
]
