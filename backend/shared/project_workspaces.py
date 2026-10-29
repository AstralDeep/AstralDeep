"""Validates bounded project folder manifests and immutable conversation authority contexts.
Host admission and effect dispatch share these values without deriving authority from UI selection.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import uuid
from dataclasses import dataclass

MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_PROJECTS = 64
MAX_ROOTS = 256
MAX_MANIFEST_BYTES = 262144


class ProjectWorkspaceError(ValueError):
    pass


def _shape(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ProjectWorkspaceError("Invalid project workspace fields")


def _integer(value):
    if type(value) is not int or not 1 <= value <= MAX_SAFE_INTEGER:
        raise ProjectWorkspaceError("A positive safe integer is required")
    return value


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise ProjectWorkspaceError("A canonical project UUID is required") from None
    return value


def _text(value, maximum):
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= maximum
        and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    )


@dataclass(frozen=True)
class ProjectRoot:
    name: str
    path: str

    @classmethod
    def from_dict(cls, value):
        _shape(value, ("name", "path"))
        name, path = value["name"], value["path"]
        if (
            not _text(name, 128)
            or not _text(path, 1024)
            or not path.startswith("/")
            or path.startswith("//")
            or path == "/"
            or posixpath.normpath(path) != path
        ):
            raise ProjectWorkspaceError("Invalid project folder")
        return cls(name, path)

    def to_dict(self):
        return {"name": self.name, "path": self.path}


@dataclass(frozen=True)
class ProjectWorkspace:
    project_id: str
    grant_revision: int
    roots: tuple[ProjectRoot, ...]

    @classmethod
    def from_dict(cls, value):
        _shape(value, ("project_id", "grant_revision", "roots"))
        project_id = _uuid(value["project_id"])
        revision = _integer(value["grant_revision"])
        roots = value["roots"]
        if not isinstance(roots, list) or len(roots) > 16:
            raise ProjectWorkspaceError("A project supports at most 16 folders")
        roots = tuple(ProjectRoot.from_dict(root) for root in roots)
        if len({root.path for root in roots}) != len(roots):
            raise ProjectWorkspaceError("Duplicate project folders")
        return cls(project_id, revision, roots)

    def to_dict(self):
        return {
            "project_id": self.project_id,
            "grant_revision": self.grant_revision,
            "roots": [root.to_dict() for root in self.roots],
        }


@dataclass(frozen=True)
class ProjectManifest:
    generation: int
    projects: tuple[ProjectWorkspace, ...]

    @classmethod
    def from_dict(cls, value):
        _shape(value, ("generation", "projects"))
        generation = _integer(value["generation"])
        entries = value["projects"]
        if not isinstance(entries, list) or len(entries) > MAX_PROJECTS:
            raise ProjectWorkspaceError("A manifest supports at most 64 projects")
        projects = tuple(ProjectWorkspace.from_dict(entry) for entry in entries)
        if (
            len({entry.project_id for entry in projects}) != len(projects)
            or sum(len(entry.roots) for entry in projects) > MAX_ROOTS
        ):
            raise ProjectWorkspaceError("Duplicate projects or too many folders")
        result = cls(generation, projects)
        if len(result.encoded()) > MAX_MANIFEST_BYTES:
            raise ProjectWorkspaceError("The project manifest exceeds its byte limit")
        return result

    def to_dict(self):
        return {
            "generation": self.generation,
            "projects": [entry.to_dict() for entry in self.projects],
        }

    def encoded(self):
        try:
            return json.dumps(
                self.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (UnicodeError, ValueError):
            raise ProjectWorkspaceError("Invalid manifest encoding") from None

    @property
    def digest(self):
        return hashlib.sha256(self.encoded()).hexdigest()

    def project(self, project_id):
        return next(
            (entry for entry in self.projects if entry.project_id == project_id), None
        )


@dataclass(frozen=True)
class ProjectContext:
    chat_id: str
    project_id: str
    grant_revision: int | None
    manifest_generation: int

    @classmethod
    def from_dict(cls, value):
        _shape(
            value, ("chat_id", "project_id", "grant_revision", "manifest_generation")
        )
        chat_id = value["chat_id"]
        if not isinstance(chat_id, str) or not chat_id.strip() or len(chat_id) > 512:
            raise ProjectWorkspaceError("Invalid origin conversation")
        return cls(
            chat_id,
            _uuid(value["project_id"]),
            None
            if value["grant_revision"] is None
            else _integer(value["grant_revision"]),
            _integer(value["manifest_generation"]),
        )

    def to_dict(self):
        return {
            "chat_id": self.chat_id,
            "project_id": self.project_id,
            "grant_revision": self.grant_revision,
            "manifest_generation": self.manifest_generation,
        }
