"""Medical-imaging reader package: aggregates read_dicom, read_nifti, read_czi,
read_bio_tiff, read_volume_itk, read_wsi, extract_volume_slice, extract_wsi_region,
and compute_volume_statistics for mcp_tools.py.
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
