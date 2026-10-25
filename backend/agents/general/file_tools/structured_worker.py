"""Isolated worker that turns bounded PDF/DOCX bytes into ordered text, table cells, and
source locations. It is launched only by structured_document.py through ProcessSupervisor
with a host-selected profile, read-only runtime copies, and disposable scratch.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import sys
import unicodedata
import zipfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from defusedxml import ElementTree

ABSOLUTE_LIMITS = {
    "input_bytes": 64 * 1024 * 1024,
    "pdf_pages": 500,
    "pdf_page_text_chars": 4_000_000,
    "docx_archive_entries": 20_000,
    "docx_uncompressed_bytes": 256 * 1024 * 1024,
    "docx_entry_bytes": 64 * 1024 * 1024,
    "docx_compression_ratio": 1000,
    "blocks": 100_000,
    "table_rows": 50_000,
    "table_cells": 200_000,
    "warnings": 256,
    "fragment_chars": 100_000,
    "max_chars": 2_000_000,
    "result_bytes": 32 * 1024 * 1024,
    "deadline_seconds": 120.0,
    "concurrency": 8,
    "memory_mb": 4096,
    "file_size_mb": 256,
    "open_files": 1024,
    "processes": 1024,
    "cpu_seconds": 120,
}
_LIMIT_KEYS = frozenset(ABSOLUTE_LIMITS)
_ALLOWED_STATUSES = frozenset(
    {"complete", "partial", "truncated", "timeout", "cancelled", "failed"}
)
_NUMERIC = re.compile(
    r"^[+\-−]?(?:\d+(?:[.,]\d+)?|[.,]\d+)(?:[eE][+\-]?\d+)?\s*(?:%|[a-zA-Zµμ]+)?$"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_XML_SPACE = re.compile(r"[ \t\r\f\v]+")


class WorkerFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _failure(code: str, *, status: str = "failed") -> dict[str, Any]:
    return {
        "status": status,
        "complete": False,
        "truncated": False,
        "text": "",
        "blocks": [],
        "tables": [],
        "pages": [],
        "warnings": [],
        "vision_required": False,
        "error": {"code": code},
    }


def _clean_inline(value: str, maximum: int) -> str:
    normalized = unicodedata.normalize("NFC", value)
    normalized = _CONTROL.sub("", normalized)
    normalized = _XML_SPACE.sub(" ", normalized)
    return normalized.strip()[:maximum]


def _clean_block(value: str, maximum: int) -> str:
    lines = []
    for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        cleaned = _clean_inline(line, maximum)
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)[:maximum]


def _round(value: float) -> float:
    return round(float(value), 3)


def _bbox(values: list[float]) -> list[float]:
    x0, y0, x1, y1 = values
    return [_round(min(x0, x1)), _round(min(y0, y1)),
            _round(max(x0, x1)), _round(max(y0, y1))]


def _merge_bbox(left: list[float], right: list[float]) -> list[float]:
    return _bbox([min(left[0], right[0]), min(left[1], right[1]),
                  max(left[2], right[2]), max(left[3], right[3])])


def _warn(result: dict[str, Any], code: str, limit: int) -> None:
    if len(result["warnings"]) < limit and code not in result["warnings"]:
        result["warnings"].append(code)


def _parse_page_range(spec: Any, total: int, maximum: int) -> tuple[list[int], bool]:
    if total < 1:
        raise WorkerFailure("empty_document")
    if spec is None or spec == "":
        requested = list(range(1, total + 1))
    else:
        if not isinstance(spec, str) or len(spec) > 512:
            raise WorkerFailure("invalid_page_range")
        requested = []
        for part in spec.split(","):
            item = part.strip()
            if not item:
                continue
            try:
                if "-" in item:
                    start_text, end_text = item.split("-", 1)
                    start = int(start_text) if start_text else 1
                    end = int(end_text) if end_text else total
                else:
                    start = end = int(item)
            except ValueError:
                raise WorkerFailure("invalid_page_range") from None
            if start < 1 or end < start:
                raise WorkerFailure("invalid_page_range")
            requested.extend(range(start, min(end, total) + 1))
    requested = list(dict.fromkeys(requested))
    if not requested:
        raise WorkerFailure("invalid_page_range")
    truncated = len(requested) > maximum
    return requested[:maximum], truncated


def _effective_position(cm: Any, tm: Any) -> tuple[float, float]:
    return (
        float(cm[0]) * float(tm[4]) + float(cm[2]) * float(tm[5]) + float(cm[4]),
        float(cm[1]) * float(tm[4]) + float(cm[3]) * float(tm[5]) + float(cm[5]),
    )


def _pdf_fragments(page: Any, limits: dict[str, int]) -> list[dict[str, Any]]:
    fragments: list[dict[str, Any]] = []

    def visitor(text: Any, cm: Any, tm: Any, _font: Any, size: Any) -> None:
        if not isinstance(text, str) or not text:
            return
        try:
            x, y = _effective_position(cm, tm)
            font_size = float(size) if size is not None else 10.0
        except (TypeError, ValueError, IndexError):
            return
        if not math.isfinite(x) or not math.isfinite(y) or not math.isfinite(font_size):
            return
        font_size = max(1.0, min(font_size, 200.0))
        for offset, raw_line in enumerate(text.replace("\r", "\n").split("\n")):
            cleaned = _clean_inline(raw_line, limits["fragment_chars"])
            if not cleaned:
                continue
            line_y = y - offset * font_size * 1.2
            width = min(
                limits["fragment_chars"],
                max(font_size, len(cleaned) * font_size * 0.5),
            )
            fragments.append({
                "text": cleaned,
                "x": x,
                "y": line_y,
                "x0": x,
                "x1": x + width,
                "y0": line_y - font_size * 0.2,
                "y1": line_y + font_size * 0.8,
                "size": font_size,
            })

    page.extract_text(visitor_text=visitor)
    return fragments


def _group_lines(fragments: list[dict[str, Any]], limits: dict[str, int]) -> list[dict[str, Any]]:
    ordered = sorted(fragments, key=lambda item: (-item["y"], item["x"]))
    lines: list[dict[str, Any]] = []
    for fragment in ordered:
        target = None
        for line in reversed(lines[-8:]):
            tolerance = max(2.0, min(line["size"], fragment["size"]) * 0.5)
            if abs(line["y"] - fragment["y"]) <= tolerance:
                target = line
                break
        if target is None:
            target = {
                "y": fragment["y"],
                "size": fragment["size"],
                "fragments": [],
            }
            lines.append(target)
        target["fragments"].append(fragment)
        target["y"] = sum(item["y"] for item in target["fragments"]) / len(
            target["fragments"]
        )
        target["size"] = max(target["size"], fragment["size"])

    grouped: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        cells: list[dict[str, Any]] = []
        for fragment in sorted(line["fragments"], key=lambda item: item["x"]):
            merge_gap = max(4.0, fragment["size"] * 0.6)
            if cells and fragment["x"] - cells[-1]["bbox"][2] <= merge_gap:
                cell = cells[-1]
                cell["text"] = _clean_inline(
                    f"{cell['text']} {fragment['text']}", limits["fragment_chars"]
                )
                cell["bbox"] = _merge_bbox(cell["bbox"], [
                    fragment["x0"], fragment["y0"], fragment["x1"], fragment["y1"]
                ])
            else:
                cells.append({
                    "text": fragment["text"],
                    "bbox": _bbox([
                        fragment["x0"], fragment["y0"], fragment["x1"], fragment["y1"]
                    ]),
                })
        if not cells:
            continue
        grouped.append({
            "id": f"line-{index}",
            "y": line["y"],
            "size": line["size"],
            "cells": cells,
            "bbox": _merge_bbox(cells[0]["bbox"], cells[-1]["bbox"]),
            "text": " ".join(cell["text"] for cell in cells),
        })
    return sorted(grouped, key=lambda item: (-item["y"], item["bbox"][0]))


def _columns_match(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    if len(left) != len(right) or len(left) < 2:
        return False
    if abs(left[0]["bbox"][0] - right[0]["bbox"][0]) > 24.0:
        return False
    for first, second in zip(left, right, strict=True):
        if abs(first["bbox"][0] - second["bbox"][0]) > 24.0:
            return False
    return True


def _looks_like_table(lines: list[dict[str, Any]]) -> bool:
    if len(lines) < 2:
        return False
    cells = [cell for line in lines for cell in line["cells"]]
    if len(cells) < 4:
        return False
    lengths = sorted(len(cell["text"]) for cell in cells)
    median = lengths[len(lengths) // 2]
    short = sum(1 for length in lengths if length <= 48)
    numeric = sum(1 for cell in cells if _NUMERIC.match(cell["text"]))
    return median <= 24 and short * 4 >= len(cells) * 3 and (
        numeric > 0 or sum(len(line["cells"]) for line in lines) >= len(lines) * 2
    )


def _detect_tables(lines: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    runs: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for line in lines:
        if len(line["cells"]) >= 2:
            if current and _columns_match(current[-1]["cells"], line["cells"]):
                current.append(line)
            else:
                if _looks_like_table(current):
                    runs.append(current)
                current = [line]
        else:
            if _looks_like_table(current):
                runs.append(current)
            current = []
    if _looks_like_table(current):
        runs.append(current)
    return runs


def _table_block(
    page_number: int,
    index: int,
    lines: list[dict[str, Any]],
    limits: dict[str, int],
    result: dict[str, Any],
) -> dict[str, Any]:
    rows = []
    cells_seen = 0
    for row_index, line in enumerate(lines):
        if row_index >= limits["table_rows"]:
            result["truncated"] = True
            _warn(result, "table_rows_truncated", limits["warnings"])
            break
        cells = []
        for column_index, cell in enumerate(line["cells"]):
            if cells_seen >= limits["table_cells"]:
                result["truncated"] = True
                _warn(result, "table_cells_truncated", limits["warnings"])
                break
            cells_seen += 1
            cells.append({
                "row": row_index,
                "column": column_index,
                "text": cell["text"],
                "source": {
                    "kind": "pdf",
                    "page": page_number,
                    "region": cell["bbox"],
                    "coordinate_system": "pdf_user_space_points",
                },
            })
        rows.append({"index": row_index, "cells": cells})
    region = _bbox([
        min(line["bbox"][0] for line in lines),
        min(line["bbox"][1] for line in lines),
        max(line["bbox"][2] for line in lines),
        max(line["bbox"][3] for line in lines),
    ])
    return {
        "id": f"p{page_number}-t{index}",
        "type": "table",
        "bbox": region,
        "source": {
            "kind": "pdf",
            "page": page_number,
            "region": region,
            "coordinate_system": "pdf_user_space_points",
        },
        "rows": rows,
    }


def _clusters(lines: list[dict[str, Any]]) -> list[float]:
    starts = sorted(line["bbox"][0] for line in lines)
    groups: list[list[float]] = []
    for value in starts:
        if groups and value - groups[-1][-1] <= 24.0:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(group) / len(group) for group in groups if len(group) >= 2]


def _column_order(lines: list[dict[str, Any]]) -> list[float] | None:
    centers = _clusters(lines)
    if len(centers) < 2:
        return None
    for line in lines:
        for right in centers[1:]:
            if line["bbox"][0] < right - 4.0 < line["bbox"][2]:
                return None
    return centers


def _split_cells(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    split: list[dict[str, Any]] = []
    for line in lines:
        if len(line["cells"]) == 1:
            split.append(line)
            continue
        for index, cell in enumerate(line["cells"]):
            split.append({
                "id": f"{line['id']}-c{index}",
                "y": line["y"],
                "size": line["size"],
                "page": line["page"],
                "cells": [cell],
                "bbox": cell["bbox"],
                "text": cell["text"],
            })
    return split


def _ordered_entries(
    lines: list[dict[str, Any]], tables: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    lines = _split_cells(lines)
    entries = [{"kind": "line", "item": line} for line in lines]
    entries.extend({"kind": "table", "item": table} for table in tables)
    centers = _column_order(lines)
    if centers:
        def key(entry: dict[str, Any]) -> tuple[float, float, float]:
            x0 = entry["item"]["bbox"][0]
            column = min(range(len(centers)), key=lambda index: abs(centers[index] - x0))
            return (column, -entry["item"]["bbox"][3], x0)
    else:
        def key(entry: dict[str, Any]) -> tuple[float, float, float]:
            return (0.0, -entry["item"]["bbox"][3], entry["item"]["bbox"][0])
    return sorted(entries, key=key)


def _table_text(table: dict[str, Any]) -> str:
    return "\n".join(
        "\t".join(cell["text"] for cell in row["cells"])
        for row in table["rows"]
    )


def _render_entry(
    entry: dict[str, Any], remaining: int, result: dict[str, Any], limits: dict[str, int]
) -> tuple[str, bool]:
    if entry["kind"] == "line":
        text = entry["item"]["text"]
        block = {
            "id": entry["item"]["id"],
            "type": "text_line",
            "text": text,
            "source": {
                "kind": "pdf",
                "page": entry["item"]["page"],
                "region": entry["item"]["bbox"],
                "coordinate_system": "pdf_user_space_points",
            },
        }
        if len(text) > remaining:
            block["text"] = text[:remaining]
            block["truncated"] = True
            result["blocks"].append(block)
            return block["text"], True
        if len(result["blocks"]) >= limits["blocks"]:
            result["truncated"] = True
            _warn(result, "block_limit_truncated", limits["warnings"])
            return "", True
        result["blocks"].append(block)
        return text, False

    table = entry["item"]
    text = _table_text(table)
    if len(text) > remaining:
        table = dict(table)
        table["truncated"] = True
        text = text[:remaining]
        result["truncated"] = True
    if len(result["blocks"]) >= limits["blocks"]:
        result["truncated"] = True
        _warn(result, "block_limit_truncated", limits["warnings"])
        return "", True
    result["blocks"].append(table)
    result["tables"].append(table)
    return text, len(text) >= remaining


def _page_has_image(page: Any) -> bool:
    try:
        resources = page.get("/Resources")
        if resources is None:
            return False
        resources = resources.get_object()
        xobjects = resources.get("/XObject")
        if xobjects is None:
            return False
        for reference in xobjects.get_object().values():
            target = reference.get_object()
            if target.get("/Subtype") == "/Image":
                return True
    except Exception:
        return False
    return False


def _parse_pdf(
    payload: bytes,
    page_range: Any,
    max_chars: int,
    limits: dict[str, int],
    identity: dict[str, Any],
) -> dict[str, Any]:
    from pypdf import PdfReader

    result = _failure("parse_failed")
    result.update({
        "status": "complete",
        "complete": True,
        "truncated": False,
        "parser": identity,
        "source": {"kind": "pdf", "sha256": hashlib.sha256(payload).hexdigest(),
                   "bytes": len(payload), "extension": "pdf"},
        "page_count": 0,
        "selected_pages": [],
        "pages": [],
        "text": "",
        "blocks": [],
        "tables": [],
        "warnings": [],
        "vision_required": False,
    })
    try:
        reader = PdfReader(io.BytesIO(payload), strict=False)
        if reader.is_encrypted:
            raise WorkerFailure("encrypted_document")
        total = len(reader.pages)
        selected, range_truncated = _parse_page_range(page_range, total, limits["pdf_pages"])
    except WorkerFailure as exc:
        failed = _failure(exc.code)
        failed["parser"] = identity
        return failed
    except Exception:
        failed = _failure("malformed_document")
        failed["parser"] = identity
        return failed

    result["page_count"] = total
    result["selected_pages"] = selected
    result["truncated"] = range_truncated
    if range_truncated:
        _warn(result, "page_limit_truncated", limits["warnings"])

    text_parts: list[str] = []
    remaining = max_chars
    failed_pages = 0
    image_without_text = False
    for page_number in selected:
        page_info = {
            "page": page_number,
            "status": "complete",
            "text_chars": 0,
            "blocks": 0,
            "tables": 0,
            "has_image": False,
        }
        result["pages"].append(page_info)
        if remaining <= 0:
            page_info["status"] = "truncated"
            result["truncated"] = True
            continue
        try:
            page = reader.pages[page_number - 1]
            page_info["has_image"] = _page_has_image(page)
            fragments = _pdf_fragments(page, limits)[: limits["blocks"] * 4]
            lines = _group_lines(fragments, limits)
            table_lines = _detect_tables(lines)
            consumed = {line["id"] for run in table_lines for line in run}
            plain_lines = [line for line in lines if line["id"] not in consumed]
            for line in plain_lines:
                line["page"] = page_number
            tables = [
                _table_block(page_number, index + 1, run, limits, result)
                for index, run in enumerate(table_lines)
            ]
            for table in tables:
                table["source"]["page"] = page_number
            entries = _ordered_entries(plain_lines, tables)
            page_text_parts = []
            page_block_start = len(result["blocks"])
            table_start = len(result["tables"])
            for entry in entries:
                rendered, exhausted = _render_entry(entry, remaining, result, limits)
                if not rendered:
                    break
                page_text_parts.append(rendered)
                remaining -= len(rendered)
                if exhausted:
                    result["truncated"] = True
                    break
            page_text = "\n".join(page_text_parts)
            page_info["text_chars"] = len(page_text)
            page_info["blocks"] = len(result["blocks"]) - page_block_start
            page_info["tables"] = len(result["tables"]) - table_start
            if not page_text:
                page_info["status"] = "empty"
                if page_info["has_image"]:
                    page_info["status"] = "partial"
                    image_without_text = True
                    _warn(result, "page_image_without_text", limits["warnings"])
            if page_text:
                text_parts.append(page_text)
        except Exception:
            failed_pages += 1
            page_info["status"] = "failed"
            _warn(result, "page_extraction_failed", limits["warnings"])

    text = "\n".join(text_parts)
    if len(text) > max_chars:
        text = text[:max_chars]
        result["truncated"] = True
    result["text"] = text
    if failed_pages == len(selected):
        failed = _failure("parse_failed")
        failed["parser"] = identity
        failed["page_count"] = total
        failed["selected_pages"] = selected
        return failed
    if failed_pages or image_without_text:
        result["status"] = "partial"
    elif result["truncated"]:
        result["status"] = "truncated"
    result["complete"] = result["status"] == "complete" and not result["truncated"]
    result["vision_required"] = image_without_text
    result.pop("error", None)
    return result


def _preflight_docx(payload: bytes, limits: dict[str, int]) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            if len(infos) > limits["docx_archive_entries"]:
                raise WorkerFailure("archive_limit")
            names = set()
            total = 0
            for info in infos:
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    raise WorkerFailure("unsafe_archive")
                if info.file_size > limits["docx_entry_bytes"]:
                    raise WorkerFailure("archive_limit")
                total += info.file_size
                if total > limits["docx_uncompressed_bytes"]:
                    raise WorkerFailure("archive_limit")
                if (
                    info.compress_size > 0
                    and info.file_size > 1024 * 1024
                    and info.file_size / info.compress_size > limits["docx_compression_ratio"]
                ):
                    raise WorkerFailure("archive_limit")
                names.add(name)
            if "word/document.xml" not in names:
                raise WorkerFailure("malformed_document")
            blocked = (
                "word/vbaproject.bin",
                "word/activex/",
                "word/embeddings/",
                "word/altchunk/",
            )
            if any(name.lower().startswith(blocked) for name in names):
                raise WorkerFailure("active_content_rejected")
            for name in names:
                if not (name.endswith(".xml") or name.endswith(".rels")):
                    continue
                try:
                    root = ElementTree.fromstring(archive.read(name))
                except Exception:
                    raise WorkerFailure("malformed_document") from None
                if name == "[Content_Types].xml":
                    if any(
                        "macroEnabled" in child.attrib.get("ContentType", "")
                        for child in root
                    ):
                        raise WorkerFailure("active_content_rejected")
                if name.endswith(".rels"):
                    for relationship in root:
                        if relationship.attrib.get("TargetMode") == "External":
                            raise WorkerFailure("external_resource_rejected")
    except WorkerFailure:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
        raise WorkerFailure("malformed_document") from None


def _docx_cell_text(cell: Any, maximum: int) -> str:
    return _clean_block(cell.text or "", maximum)


def _parse_docx(
    payload: bytes,
    max_chars: int,
    limits: dict[str, int],
    identity: dict[str, Any],
) -> dict[str, Any]:
    try:
        _preflight_docx(payload, limits)
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        document = docx.Document(io.BytesIO(payload))
    except WorkerFailure as exc:
        failed = _failure(exc.code)
        failed["parser"] = identity
        return failed
    except Exception:
        failed = _failure("malformed_document")
        failed["parser"] = identity
        return failed

    result = _failure("parse_failed")
    result.update({
        "status": "complete",
        "complete": True,
        "truncated": False,
        "parser": identity,
        "source": {"kind": "docx", "sha256": hashlib.sha256(payload).hexdigest(),
                   "bytes": len(payload), "extension": "docx"},
        "text": "",
        "blocks": [],
        "tables": [],
        "pages": [],
        "warnings": [],
        "vision_required": False,
    })
    counters: dict[str, dict[str, int]] = {}

    def next_index(container: str, kind: str) -> int:
        counts = counters.setdefault(container, {})
        value = counts.get(kind, 0)
        counts[kind] = value + 1
        return value

    def add_block(block: dict[str, Any], text: str) -> bool:
        if len(result["blocks"]) >= limits["blocks"]:
            result["truncated"] = True
            _warn(result, "block_limit_truncated", limits["warnings"])
            return False
        block["text"] = text
        result["blocks"].append(block)
        return True

    def walk(container: Any, base: str, depth: int = 0, *, tables_only: bool = False) -> None:
        if depth > 8:
            result["truncated"] = True
            _warn(result, "nesting_limit_truncated", limits["warnings"])
            return
        for child in container.iterchildren():
            tag = str(child.tag).rsplit("}", 1)[-1]
            if tag == "p" and not tables_only:
                index = next_index(base, "p")
                path = f"{base}/p[{index}]"
                paragraph = Paragraph(child, document)
                text = _clean_block(paragraph.text or "", limits["fragment_chars"])
                if text:
                    add_block({
                        "id": path,
                        "type": "paragraph",
                        "source": {"kind": "docx", "block_id": path, "path": path},
                    }, text)
            elif tag == "tbl":
                index = next_index(base, "tbl")
                path = f"{base}/tbl[{index}]"
                table = Table(child, document)
                rows = []
                table_text_rows = []
                for row_index, row in enumerate(table.rows):
                    if row_index >= limits["table_rows"]:
                        result["truncated"] = True
                        _warn(result, "table_rows_truncated", limits["warnings"])
                        break
                    cells = []
                    for column_index, cell in enumerate(row.cells):
                        if sum(len(item["cells"]) for item in rows) >= limits["table_cells"]:
                            result["truncated"] = True
                            _warn(result, "table_cells_truncated", limits["warnings"])
                            break
                        try:
                            cell_index = list(row._tr.tc_lst).index(cell._tc)
                        except ValueError:
                            cell_index = column_index
                        cell_path = f"{path}/tr[{row_index}]/tc[{cell_index}]"
                        cells.append({
                            "row": row_index,
                            "column": column_index,
                            "text": _docx_cell_text(cell, limits["fragment_chars"]),
                            "source": {
                                "kind": "docx",
                                "block_id": cell_path,
                                "path": cell_path,
                                "row": row_index,
                                "column": column_index,
                            },
                        })
                    rows.append({"index": row_index, "cells": cells})
                    table_text_rows.append("\t".join(cell["text"] for cell in cells))
                table_block = {
                    "id": path,
                    "type": "table",
                    "source": {"kind": "docx", "block_id": path, "path": path},
                    "rows": rows,
                }
                if add_block(table_block, "\n".join(table_text_rows)):
                    result["tables"].append(table_block)
                for row_index, row in enumerate(table.rows):
                    for column_index, cell in enumerate(row.cells):
                        try:
                            cell_index = list(row._tr.tc_lst).index(cell._tc)
                        except ValueError:
                            cell_index = column_index
                        cell_base = f"{path}/tr[{row_index}]/tc[{cell_index}]"
                        if any(
                            str(nested.tag).rsplit("}", 1)[-1] == "tbl"
                            for nested in cell._tc.iterchildren()
                        ):
                            walk(cell._tc, cell_base, depth + 1, tables_only=True)
            elif tag == "sdt":
                index = next_index(base, "sdt")
                content = next(
                    (item for item in child.iterchildren()
                     if str(item.tag).rsplit("}", 1)[-1] == "sdtContent"),
                    None,
                )
                if content is not None:
                    walk(content, f"{base}/sdt[{index}]", depth + 1,
                         tables_only=tables_only)

    walk(document.element.body, "body")
    previous_table = None
    for block in result["blocks"]:
        if block["type"] != "table":
            previous_table = None
            continue
        first_row = block["rows"][0]["cells"] if block["rows"] else []
        header = tuple(cell["text"] for cell in first_row)
        if (
            previous_table is not None
            and header
            and header == previous_table[0]
            and len(previous_table[1]["rows"]) >= 2
        ):
            block["continuation_of"] = previous_table[1].get(
                "continuation_of", previous_table[1]["id"]
            )
            block["header_repeated"] = True
        previous_table = (header, block)

    text_parts = []
    remaining = max_chars
    retained_blocks = []
    retained_tables = []
    for block in result["blocks"]:
        block_text = (
            _table_text(block) if block["type"] == "table" else block["text"]
        )
        if remaining <= 0:
            result["truncated"] = True
            break
        if len(block_text) > remaining:
            block = dict(block)
            block["text"] = block_text[:remaining]
            block["truncated"] = True
            result["truncated"] = True
            retained_blocks.append(block)
            if block["type"] == "table":
                retained_tables.append(block)
            text_parts.append(block["text"])
            remaining = 0
            break
        retained_blocks.append(block)
        if block["type"] == "table":
            retained_tables.append(block)
        text_parts.append(block_text)
        remaining -= len(block_text)
    result["blocks"] = retained_blocks
    result["tables"] = retained_tables
    result["text"] = "\n".join(text_parts)
    if result["truncated"]:
        result["status"] = "truncated"
    result["complete"] = result["status"] == "complete" and not result["truncated"]
    result.pop("error", None)
    return result


def _validate_profile(profile: Any) -> dict[str, Any]:
    if not isinstance(profile, dict) or profile.get("schema_version") != 1:
        raise WorkerFailure("invalid_profile")
    if not isinstance(profile.get("profile_id"), str) or not profile["profile_id"]:
        raise WorkerFailure("invalid_profile")
    adapter = profile.get("adapter")
    if not isinstance(adapter, dict) or not all(
        isinstance(adapter.get(key), str) and adapter[key]
        for key in ("id", "version")
    ):
        raise WorkerFailure("invalid_profile")
    limits = profile.get("limits")
    if not isinstance(limits, dict) or set(limits) != _LIMIT_KEYS:
        raise WorkerFailure("invalid_profile")
    for key, ceiling in ABSOLUTE_LIMITS.items():
        value = limits.get(key)
        if type(value) is int:
            if value <= 0 or value > ceiling:
                raise WorkerFailure("invalid_profile")
        elif type(value) is float:
            if not math.isfinite(value) or value <= 0 or value > ceiling:
                raise WorkerFailure("invalid_profile")
        else:
            raise WorkerFailure("invalid_profile")
    containment = profile.get("containment")
    if (
        not isinstance(containment, dict)
        or not isinstance(containment.get("profile"), str)
        or not containment["profile"]
        or containment.get("network") != "denied"
        or containment.get("environment") != "scrubbed"
        or containment.get("runtime_assets") != "read-only-copy"
        or containment.get("scratch") != "disposable"
    ):
        raise WorkerFailure("invalid_profile")
    enrichment = profile.get("enrichment")
    if (
        not isinstance(enrichment, dict)
        or not enrichment
        or any(value is not False for value in enrichment.values())
    ):
        raise WorkerFailure("invalid_profile")
    if profile.get("assets") != []:
        raise WorkerFailure("invalid_profile")
    dependencies = profile.get("dependencies")
    if not isinstance(dependencies, list):
        raise WorkerFailure("invalid_profile")
    for dependency in dependencies:
        if (
            not isinstance(dependency, dict)
            or not isinstance(dependency.get("name"), str)
            or not isinstance(dependency.get("version"), str)
        ):
            raise WorkerFailure("invalid_profile")
    return profile


def _verify_dependencies(profile: dict[str, Any]) -> None:
    for dependency in profile["dependencies"]:
        try:
            observed = version(dependency["name"])
        except PackageNotFoundError:
            raise WorkerFailure("dependency_missing") from None
        if observed != dependency["version"]:
            raise WorkerFailure("dependency_mismatch")


def _read_json(path: Path, maximum: int) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise WorkerFailure("io_failure") from None
    if len(raw) > maximum:
        raise WorkerFailure("request_too_large")
    try:
        value = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        raise WorkerFailure("invalid_request") from None
    if not isinstance(value, dict):
        raise WorkerFailure("invalid_request")
    return value


def _safe_name(value: Any) -> str:
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise WorkerFailure("invalid_request")
    return value


def _write_result(path: Path, result: dict[str, Any], maximum: int) -> None:
    def encode(value: dict[str, Any]) -> bytes:
        return json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")

    raw = encode(result)
    if len(raw) > maximum:
        result = dict(result)
        result["truncated"] = True
        result["complete"] = False
        result["status"] = "truncated"
        result["blocks"] = []
        result["tables"] = []
        result["warnings"] = result.get("warnings", [])[:16]
        while len(raw) > maximum and result.get("text"):
            result["text"] = result["text"][: len(result["text"]) // 2]
            raw = encode(result)
        if len(raw) > maximum:
            result["text"] = ""
            raw = encode(result)
    try:
        flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            view = memoryview(raw)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
        finally:
            os.close(descriptor)
    except OSError:
        raise WorkerFailure("io_failure") from None


def _network_denied(*_args: Any, **_kwargs: Any) -> None:
    raise OSError("network access denied by the document worker profile")


def _install_network_guard() -> None:
    import socket
    import urllib.request

    def audit(event: str, _args: Any) -> None:
        if event in {
            "socket.__new__",
            "socket.bind",
            "socket.connect",
            "socket.getaddrinfo",
        }:
            raise OSError("network access denied by the document worker profile")

    sys.addaudithook(audit)
    socket.create_connection = _network_denied
    socket.getaddrinfo = _network_denied
    urllib.request.urlopen = _network_denied


def _run(request_path: Path) -> dict[str, Any]:
    request = _read_json(request_path, 64 * 1024)
    expected = {
        "version", "extension", "page_range", "max_chars", "input", "output",
        "profile", "profile_sha256",
    }
    if set(request) != expected or request.get("version") != 1:
        raise WorkerFailure("invalid_request")
    if request["extension"] not in ("pdf", "docx"):
        raise WorkerFailure("unsupported_extension")
    if type(request["max_chars"]) is not int or request["max_chars"] < 1:
        raise WorkerFailure("invalid_request")
    profile_path = Path.cwd().parent / "runtime" / _safe_name(request["profile"])
    try:
        profile_raw = profile_path.read_bytes()
    except OSError:
        raise WorkerFailure("missing_profile") from None
    if hashlib.sha256(profile_raw).hexdigest() != request["profile_sha256"]:
        raise WorkerFailure("profile_digest_mismatch")
    try:
        profile = json.loads(profile_raw)
    except (ValueError, TypeError, RecursionError):
        raise WorkerFailure("invalid_profile") from None
    profile = _validate_profile(profile)
    _verify_dependencies(profile)
    if sys.platform != "linux" or os.name != "posix":
        raise WorkerFailure("unsupported_platform")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise WorkerFailure("root_worker_rejected")

    input_path = Path.cwd() / _safe_name(request["input"])
    output_path = Path.cwd() / _safe_name(request["output"])
    try:
        payload = input_path.read_bytes()
    except OSError:
        raise WorkerFailure("io_failure") from None
    limits = profile["limits"]
    if len(payload) > limits["input_bytes"]:
        raise WorkerFailure("input_too_large")
    _install_network_guard()
    identity = {
        "profile_id": profile["profile_id"],
        "profile_sha256": hashlib.sha256(profile_raw).hexdigest(),
        "adapter": profile["adapter"]["id"],
        "adapter_version": profile["adapter"]["version"],
        "worker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "dependency_lock": [
            {"name": item["name"], "version": item["version"]}
            for item in profile["dependencies"]
        ],
    }
    max_chars = min(request["max_chars"], limits["max_chars"])
    if request["extension"] == "pdf":
        result = _parse_pdf(
            payload, request["page_range"], max_chars, limits, identity
        )
    else:
        result = _parse_docx(payload, max_chars, limits, identity)
    _write_result(output_path, result, limits["result_bytes"])
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv if argv is None else argv)
    if len(arguments) != 2:
        return 2
    request_path = Path(arguments[1])
    try:
        _run(request_path)
    except WorkerFailure as exc:
        try:
            request = _read_json(request_path, 64 * 1024)
            output = Path.cwd() / _safe_name(request.get("output"))
            _write_result(output, _failure(exc.code), 64 * 1024)
        except Exception:
            return 3
    except Exception:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())