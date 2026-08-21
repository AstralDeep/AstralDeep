"""Medical imaging reader / extractor tools.

Each tool follows the contract documented in
``backend/agents/general/file_tools/__init__.py``: takes ``attachment_id`` +
``user_id``, uses Plane's bounded byte reader whenever its parser accepts a
file-like object, and requests a scoped path lease only for filename-only
libraries. Tools return a plain dict on success or
``{"error": {"code": ..., "message": ...}}``.

Extensions served:
  * ``read_dicom``            — .dcm / .dicom (single-file)
  * ``read_nifti``            — .nii / .nii.gz
  * ``read_czi``              — .czi (Zeiss microscopy)
  * ``read_bio_tiff``         — .tif / .tiff / .ome.tif / .ome.tiff
  * ``read_volume_itk``       — .nrrd / .mha / .mhd
  * ``read_wsi``              — .svs / .ndpi (whole-slide pathology)
  * ``extract_volume_slice``  — arbitrary slice from any volumetric format
  * ``extract_wsi_region``    — region crop from a whole-slide image
  * ``compute_volume_statistics`` — histogram + MIP projections
"""

from agents.general.file_tools.medical.compute_volume_statistics import (
    compute_volume_statistics,
)
from agents.general.file_tools.medical.extract_volume_slice import extract_volume_slice
from agents.general.file_tools.medical.extract_wsi_region import extract_wsi_region
from agents.general.file_tools.medical.read_bio_tiff import read_bio_tiff
from agents.general.file_tools.medical.read_czi import read_czi
from agents.general.file_tools.medical.read_dicom import read_dicom
from agents.general.file_tools.medical.read_nifti import read_nifti
from agents.general.file_tools.medical.read_volume_itk import read_volume_itk
from agents.general.file_tools.medical.read_wsi import read_wsi

__all__ = [
    "compute_volume_statistics",
    "extract_volume_slice",
    "extract_wsi_region",
    "read_bio_tiff",
    "read_czi",
    "read_dicom",
    "read_nifti",
    "read_volume_itk",
    "read_wsi",
]
