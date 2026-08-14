"""AstralDeep-to-AstralPlane compatibility and startup admission contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLANE_SOURCE = ROOT / "components" / "AstralPlane" / "src"
if str(PLANE_SOURCE) not in sys.path:
    sys.path.insert(0, str(PLANE_SOURCE))

from astralplane.compatibility import (  # noqa: E402
    BLOB_LAYOUT_VERSION,
    CONTRACT_VERSION,
    MIGRATION_DIGEST,
    PACKAGE_VERSION,
    SCHEMA_REVISION,
    CompatibilityState,
    inspect_compatibility,
)
from astralplane.database.revision import READ_COMPATIBLE_FROM  # noqa: E402


def _declared_plane() -> dict[str, str]:
    composition = json.loads(
        (ROOT / "config" / "astral-composition.json").read_text(encoding="utf-8")
    )
    return composition["compatibility"]["data_plane"]


def test_composed_plane_contract_is_exact_and_startup_compatible() -> None:
    declared = _declared_plane()
    assert declared == {
        "contract_version": CONTRACT_VERSION,
        "schema_revision": SCHEMA_REVISION,
        "read_compatible_from": READ_COMPATIBLE_FROM,
        "migration_sha256": MIGRATION_DIGEST,
        "blob_layout_version": BLOB_LAYOUT_VERSION,
    }

    report = inspect_compatibility(
        expected_contract_version=declared["contract_version"],
        observed_schema_revision=declared["schema_revision"],
        consumer_version=PACKAGE_VERSION,
    )
    assert report.state is CompatibilityState.COMPATIBLE
    assert report.compatible is True
    assert report.reasons == ()


def test_plane_compatibility_attributes_each_startup_failure() -> None:
    wrong_contract = inspect_compatibility(
        expected_contract_version="astralplane.contract/v2",
        observed_schema_revision=SCHEMA_REVISION,
        consumer_version=PACKAGE_VERSION,
    )
    assert wrong_contract.state is CompatibilityState.INCOMPATIBLE
    assert wrong_contract.reasons == ("contract_version_mismatch",)

    wrong_schema = inspect_compatibility(
        expected_contract_version=CONTRACT_VERSION,
        observed_schema_revision="999.999",
        consumer_version=PACKAGE_VERSION,
    )
    assert wrong_schema.reasons == ("schema_revision_incompatible",)

    old_consumer = inspect_compatibility(
        expected_contract_version=CONTRACT_VERSION,
        observed_schema_revision=SCHEMA_REVISION,
        consumer_version="0.0.1",
    )
    assert old_consumer.reasons == ("consumer_version_too_old",)

    all_mismatched = inspect_compatibility(
        expected_contract_version="wrong",
        observed_schema_revision="bad",
        consumer_version="bad",
    )
    assert all_mismatched.reasons == (
        "contract_version_mismatch",
        "schema_revision_incompatible",
        "consumer_version_too_old",
    )
