# Loads an agent package: vessel.toml and its declared tools.
# The manifest lists what the executor may run for an agent; anything not listed does not exist.

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

BUILTIN_TOOLS = ("read", "grep", "glob", "list", "bash")
# Astral's six permission scopes, weakest first; each tool has one and an agent's catalog entry carries the strongest.
SCOPES = ("tools:read", "tools:search", "tools:files", "tools:write", "tools:system", "tools:execute")
BUILTIN_SCOPES = {"read": "tools:files", "grep": "tools:search", "glob": "tools:files", "list": "tools:files",
                  "bash": "tools:execute"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentSection(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    description: str
    version: str = "0.1.0"


class Mount(Strict):
    source: str
    target: str
    read_only: bool = True


class SandboxSection(Strict):
    workdir: str = "/workspace"
    mounts: list[Mount] = []
    deadline_seconds: int = Field(default=600, ge=5, le=3600)
    tool_timeout_seconds: int = Field(default=60, ge=1, le=600)
    tool_output_chars: int = Field(default=20000, ge=1000, le=200000)
    # A Dockerfile in the package, built FROM the AstralVessel image, for tools that need extra software.
    dockerfile: str | None = None


class ToolsSection(Strict):
    builtin: list[str] = []
    declared: list[str] = []

    @field_validator("builtin")
    @classmethod
    def known_builtins(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(BUILTIN_TOOLS))
        if unknown:
            raise ValueError(f"unknown builtin tools: {', '.join(unknown)}")
        return value


class OutputSection(Strict):
    # File types a tool may return by writing into $VESSEL_OUTBOX; none unless listed.
    files: list[Literal["png", "svg", "csv", "pdf"]] = []


class Manifest(Strict):
    agent: AgentSection
    tools: ToolsSection = ToolsSection()
    sandbox: SandboxSection = SandboxSection()
    output: OutputSection = OutputSection()


class DeclaredTool(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    description: str
    command: list[str] = Field(min_length=1)
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    timeout_seconds: int | None = None
    input: Literal["json-stdin", "args"] = "json-stdin"
    scope: Literal[SCOPES] = "tools:read"


class AgentPackage(BaseModel):
    root: Path
    manifest: Manifest
    declared: list[DeclaredTool]

    def loaded_digest(self) -> str:
        # The manifest and declared tools as loaded at start.
        body = {"manifest": self.manifest.model_dump(mode="json"),
                "declared": [tool.model_dump(mode="json") for tool in self.declared]}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]

    def tool_scopes(self) -> dict[str, str]:
        scopes = {name: BUILTIN_SCOPES[name] for name in self.manifest.tools.builtin}
        scopes.update({t.name: t.scope for t in self.declared})
        return scopes

    def strongest_scope(self) -> str:
        return max(self.tool_scopes().values(), key=SCOPES.index, default="tools:read")


def load_package(path: str | Path) -> AgentPackage:
    root = Path(path).resolve()
    manifest = Manifest.model_validate(tomllib.loads((root / "vessel.toml").read_text()))
    declared = []
    for rel in manifest.tools.declared:
        tool_path = (root / rel).resolve()
        if root not in tool_path.parents:
            raise ValueError(f"declared tool {rel} is outside the package")
        declared.append(DeclaredTool.model_validate(tomllib.loads(tool_path.read_text())))
    dockerfile = manifest.sandbox.dockerfile
    if dockerfile and root not in (root / dockerfile).resolve().parents:
        raise ValueError("the sandbox dockerfile must be inside the package")
    names = [t.name for t in declared]
    clash = (set(names) & set(BUILTIN_TOOLS)) | {n for n in names if names.count(n) > 1}
    if clash:
        raise ValueError(f"declared tool names collide: {', '.join(sorted(clash))}")
    return AgentPackage(root=root, manifest=manifest, declared=declared)
