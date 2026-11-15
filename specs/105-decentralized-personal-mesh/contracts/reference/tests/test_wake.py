"""Covers malformed clocks and opaque delegation diagnostics beyond the shared route vectors.
Real publisher cryptography, target binding and dispatch are verified by their owning runtime suites."""

import pytest

from astral_mesh_ref import wake
from astral_mesh_ref.errors import MeshError


@pytest.mark.parametrize("clock", [None, True, -1])
def test_invalid_clock_is_refused_without_echoing_capability(clock):
    secret = "sensitive_fixture_token"
    with pytest.raises(MeshError) as failure:
        wake.validate({"route": {"publisher": "astral", "capability": secret, "expires_at_ms": 2000}}, clock)
    assert failure.value.code == "wake.malformed" and secret not in str(failure.value)


def test_unpaired_unicode_cannot_escape_validation_as_an_encoding_exception():
    with pytest.raises(MeshError, match="wake.malformed"):
        wake.validate({"route": {"publisher": "astral", "capability": "\ud800", "expires_at_ms": 2000}}, 1000)
