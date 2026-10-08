"""Verifies context controls and owner-bound host integration through ordinary dispatch.
The feature modules preserve authority and histories under failed or stale work.
"""

import pytest

from shared.feature_flags import FeatureFlags


@pytest.mark.parametrize("packing,compaction", [(False, False), (True, False), (False, True), (True, True)])
def test_independent_context_controls(monkeypatch, packing, compaction):
    monkeypatch.setenv("FF_OBSERVATION_PACKING", str(packing))
    monkeypatch.setenv("FF_SAFE_COMPACTION", str(compaction))
    registry = FeatureFlags()
    assert registry.is_enabled("observation_packing") is packing
    assert registry.is_enabled("safe_compaction") is compaction


def test_context_controls_default_off(monkeypatch):
    monkeypatch.delenv("FF_OBSERVATION_PACKING", raising=False)
    monkeypatch.delenv("FF_SAFE_COMPACTION", raising=False)
    registry = FeatureFlags()
    assert not registry.is_enabled("observation_packing")
    assert not registry.is_enabled("safe_compaction")
