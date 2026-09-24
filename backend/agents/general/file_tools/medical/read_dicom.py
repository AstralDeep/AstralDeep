"""read_dicom tool: parses a DICOM file and strips PHI-tagged fields by default
(PS3.15); include_phi=True surfaces them and should be used only for
already-authorized data.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from agents.general.file_tools import attachment_parser_scope, resolve_attachment
from agents.general.file_tools.medical import _common

logger = logging.getLogger("FileTools.read_dicom")


# PS3.15 PHI tags: hidden unless include_phi=True
_PHI_TAGS = (
    "PatientName",
    "PatientID",
    "PatientBirthDate",
    "PatientSex",
    "PatientAge",
    "PatientAddress",
    "OtherPatientIDs",
    "OtherPatientNames",
    "EthnicGroup",
    "StudyDate",
    "StudyTime",
    "SeriesDate",
    "SeriesTime",
    "AcquisitionDate",
    "AcquisitionTime",
    "ContentDate",
    "ContentTime",
    "AccessionNumber",
    "InstitutionName",
    "InstitutionAddress",
    "ReferringPhysicianName",
    "PerformingPhysicianName",
    "OperatorsName",
    "StudyID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "SOPInstanceUID",
)

_SAFE_TAGS = (
    "Modality",
    "Manufacturer",
    "ManufacturerModelName",
    "StudyDescription",
    "SeriesDescription",
    "BodyPartExamined",
    "ViewPosition",
    "ImageLaterality",
    "Rows",
    "Columns",
    "NumberOfFrames",
    "BitsAllocated",
    "BitsStored",
    "PhotometricInterpretation",
    "SamplesPerPixel",
    "PixelSpacing",
    "SliceThickness",
    "SliceLocation",
    "KVP",
    "ExposureTime",
    "XRayTubeCurrent",
    "RepetitionTime",
    "EchoTime",
    "MagneticFieldStrength",
    "ContrastBolusAgent",
    "ProtocolName",
    "SoftwareVersions",
    "TransferSyntaxUID",
)


def _safe_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return value.hex()
    if isinstance(value, (list, tuple)):
        return [_safe_value(v) for v in value]
    return str(value)


def _collect_tags(ds: Any, names) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name in names:
        if name in ds:
            try:
                out[name] = _safe_value(ds.data_element(name).value)
            except Exception:
                continue
    return out


@attachment_parser_scope
def read_dicom(
    attachment_id: str,
    user_id: Optional[str] = None,
    include_phi: bool = False,
    **_ignored: Any,
) -> Dict[str, Any]:
    att, path, err = resolve_attachment(attachment_id, user_id)
    if err is not None:
        return err

    try:
        import pydicom  # type: ignore
    except Exception as exc:
        return _common.missing_dep("pydicom", exc)

    try:
        ds = pydicom.dcmread(os.fspath(path), force=True)
    except Exception as exc:
        logger.exception("dicom parse failed")
        return _common.error("parse_failed", f"Failed to read DICOM: {exc}")

    result: Dict[str, Any] = {
        "filename": att.filename,
        "content_type": "application/dicom",
        "phi_included": bool(include_phi),
        "metadata": _collect_tags(ds, _SAFE_TAGS),
    }

    if include_phi:
        result["phi"] = _collect_tags(ds, _PHI_TAGS)

    try:
        pixels = ds.pixel_array  # type: ignore[attr-defined]
    except Exception as exc:
        result["pixel_stats"] = None
        result["thumbnail_error"] = f"Pixel data unavailable: {exc}"
        return result

    if pixels.ndim >= 3 and pixels.shape[0] > 1 and result["metadata"].get("NumberOfFrames"):
        mid = pixels.shape[0] // 2
        slice_2d = pixels[mid]
    else:
        slice_2d = pixels if pixels.ndim == 2 else _common.middle_slice(pixels)

    result["pixel_stats"] = _common.basic_stats(pixels)
    try:
        result.update(_common.thumbnail_field(slice_2d))
    except Exception as exc:
        logger.warning("dicom thumbnail failed: %s", exc)
        result["thumbnail_error"] = str(exc)

    return result


__all__ = ["read_dicom"]
