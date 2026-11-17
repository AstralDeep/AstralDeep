"""Detached harness selections and immutable admission captures bind exact deployment and
config identities.
Preference, assignment and work-admission repositories validate these bounded private records.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field

from astralplane.repositories import RepositoryValidationError
from astralplane.repositories.harness_bindings import HarnessProfileIdentity

_DIGEST = re.compile(r"[a-f0-9]{64}")
_PROFILE = re.compile(r"[a-z][a-z0-9-]{0,95}/v[1-9][0-9]{0,3}")
_MAX_INTEGER = 2**53 - 1


def _text(value, maximum):
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise RepositoryValidationError("harness text is invalid")


def _integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= _MAX_INTEGER:
        raise RepositoryValidationError("harness revision is invalid")


def _digest(value):
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise RepositoryValidationError("harness digest is invalid")


@dataclass(frozen=True, slots=True)
class HarnessSelectionRecord:
    owner_id: str = field(repr=False)
    revision: int
    identity: HarnessProfileIdentity | None
    qualification_digest: str | None

    def __post_init__(self):
        _text(self.owner_id, 512)
        _integer(self.revision)
        if self.identity is None:
            if self.qualification_digest is not None:
                raise RepositoryValidationError("unselected harness has qualification")
        else:
            if type(self.identity) is not HarnessProfileIdentity or self.revision == 0:
                raise RepositoryValidationError("selected harness identity is invalid")
            _digest(self.qualification_digest)


@dataclass(frozen=True, slots=True)
class HarnessCapture:
    owner_id: str = field(repr=False)
    selection_revision: int
    identity: HarnessProfileIdentity
    qualification_digest: str
    research_profile: str
    config_binding_key_id: str = field(repr=False)
    config_revision: str = field(repr=False)
    version: int = 1

    def __post_init__(self):
        _text(self.owner_id, 512)
        _integer(self.selection_revision, 1)
        if type(self.identity) is not HarnessProfileIdentity:
            raise RepositoryValidationError("captured harness identity is invalid")
        _digest(self.qualification_digest)
        _digest(self.config_revision)
        _text(self.config_binding_key_id, 128)
        if (
            type(self.research_profile) is not str
            or _PROFILE.fullmatch(self.research_profile) is None
            or type(self.version) is not int
            or self.version != 1
        ):
            raise RepositoryValidationError("captured harness profile is invalid")
        _bounded(self.to_dict())

    def to_dict(self):
        return asdict(self)


def _bounded(value):
    try:
        if type(value) is not dict or len(json.dumps(value, allow_nan=False).encode()) > 4096:
            raise ValueError
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise RepositoryValidationError("harness capture exceeds its JSON bound") from None


def harness_capture_from_value(value) -> HarnessCapture:
    try:
        if not isinstance(value, Mapping):
            raise ValueError
        document = dict(value)
        if set(document) != {
            "version",
            "owner_id",
            "selection_revision",
            "identity",
            "qualification_digest",
            "research_profile",
            "config_binding_key_id",
            "config_revision",
        }:
            raise ValueError
        identity = document["identity"]
        if not isinstance(identity, Mapping):
            raise ValueError
        document["identity"] = dict(identity)
        _bounded(document)
        document["identity"] = HarnessProfileIdentity(**identity)
        return HarnessCapture(**document)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise RepositoryValidationError("stored harness capture is invalid") from None
