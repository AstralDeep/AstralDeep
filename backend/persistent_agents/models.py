"""Canonical, strictly bounded owner commands for persistent assignments."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_INSTRUCTION_CHARS = 4096
MAX_SOURCE_BYTES = 8192
MAX_TOOLS = 32
_IDENTITY = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
_SECRET_KEYS = frozenset({"password", "secret", "token", "access_token", "refresh_token",
                          "api_key", "apikey", "authorization", "cookie", "credentials"})


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def validate_id(value: str) -> str:
    parsed = UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("a canonical UUID4 is required")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)


class ToolReference(StrictModel):
    agent_id: str = Field(pattern=_IDENTITY)
    tool_name: str = Field(pattern=_IDENTITY)

    @property
    def identity(self) -> str:
        return f"{self.agent_id}:{self.tool_name}"


class SourceSelection(ToolReference):
    profile: Literal["public_page", "registered_reader"] = "public_page"
    arguments: dict[str, Any]
    linked_document_urls: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("arguments")
    @classmethod
    def bounded_arguments(cls, value):
        def check(node, depth=0):
            if depth > 8:
                raise ValueError("source arguments exceed nesting bound")
            if isinstance(node, dict):
                for key, child in node.items():
                    if not isinstance(key, str) or key.lower().replace("-", "_") in _SECRET_KEYS:
                        raise ValueError("credentials cannot be stored in source arguments")
                    check(child, depth + 1)
            elif isinstance(node, list):
                for child in node:
                    check(child, depth + 1)
            elif node is not None and type(node) not in (str, int, float, bool):
                raise ValueError("source arguments must be JSON values")
        check(value)
        if len(canonical(value).encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError("source arguments exceed byte bound")
        return value

    @model_validator(mode="after")
    def reviewed_public_source(self):
        if self.linked_document_urls:
            if self.profile != "public_page" or len(set(self.linked_document_urls)) != len(self.linked_document_urls):
                raise ValueError("linked document URLs must be unique public-page resources")
            for url in self.linked_document_urls:
                SourceSelection(agent_id=self.agent_id, tool_name=self.tool_name, arguments={"url": url})
        if self.profile == "public_page":
            if self.identity != "web-research-1:fetch_page" or set(self.arguments) != {"url"}:
                raise ValueError("public-page source requires the exact registered page reader and URL")
            url = self.arguments["url"]
            if not isinstance(url, str) or len(url) > 2048 or url != url.strip():
                raise ValueError("a bounded absolute public HTTPS URL is required")
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or parsed.fragment or parsed.port not in (None, 443)
                    or any(ord(char) < 33 for char in url)):
                raise ValueError("a public HTTPS URL without credentials or fragments is required")
            host = parsed.hostname.lower().rstrip(".")
            if host == "localhost" or host.endswith((".localhost", ".local")) or "." not in host:
                raise ValueError("a public HTTPS host is required")
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                raise ValueError("a public HTTPS address is required")
            if any(key.lower().replace("-", "_") in _SECRET_KEYS for key, _ in parse_qsl(parsed.query)):
                raise ValueError("credentials cannot be stored in source URLs")
        if len(canonical(self.model_dump()).encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError("reviewed source exceeds byte bound")
        return self


class UsageCeiling(StrictModel):
    model_calls: int = Field(default=50, ge=1, le=100_000)
    tool_calls: int = Field(default=200, ge=1, le=1_000_000)
    tokens: int = Field(default=100_000, ge=2048, le=100_000_000)
    elapsed_ms: int = Field(default=3_600_000, ge=1000, le=31_536_000_000)
    spend_micro_units: int | None = Field(default=None, ge=0, le=9_000_000_000_000)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def paired_money(self):
        if (self.spend_micro_units is None) != (self.currency is None):
            raise ValueError("a spending cap and currency must be selected together")
        return self


class AssignmentLimits(StrictModel):
    cadence_seconds: int = Field(default=3600, ge=60, le=31_536_000)
    max_retries: int = Field(default=2, ge=0, le=5)
    max_concurrent_tasks: int = Field(default=1, ge=1, le=5)
    max_depth: int = Field(default=0, ge=0, le=4)
    max_tasks: int = Field(default=16, ge=1, le=32)
    step_timeout_ms: int = Field(default=30_000, ge=1000, le=120_000)
    daily: UsageCeiling = Field(default_factory=UsageCeiling)
    lifetime: UsageCeiling = Field(default_factory=lambda: UsageCeiling(
        model_calls=1000, tool_calls=10_000, tokens=2_000_000, elapsed_ms=86_400_000))

    @model_validator(mode="after")
    def coherent_limits(self):
        for metric in ("model_calls", "tool_calls", "tokens", "elapsed_ms"):
            if getattr(self.daily, metric) > getattr(self.lifetime, metric):
                raise ValueError("daily usage limits cannot exceed lifetime limits")
        if self.step_timeout_ms > self.daily.elapsed_ms:
            raise ValueError("step timeout cannot exceed daily time limit")
        if self.daily.currency != self.lifetime.currency:
            raise ValueError("daily and lifetime spending caps must share a currency")
        if (self.daily.spend_micro_units is not None
                and self.daily.spend_micro_units > self.lifetime.spend_micro_units):
            raise ValueError("daily spending cannot exceed lifetime spending")
        return self

    def to_plane(self) -> dict[str, Any]:
        result = self.model_dump(exclude={"daily", "lifetime"})
        result.update(self.lifetime.model_dump(exclude_none=True))
        result.update({f"daily_{key}": value for key, value in
                       self.daily.model_dump(exclude_none=True, exclude={"currency"}).items()})
        return result


class Submission(StrictModel):
    submission_id: str = Field(min_length=36, max_length=36)
    _submission_uuid = field_validator("submission_id")(validate_id)


class CreateAssignmentRequest(Submission):
    name: str = Field(min_length=1, max_length=120)
    instructions: str = Field(min_length=1, max_length=MAX_INSTRUCTION_CHARS)
    source: SourceSelection
    allowed_tools: list[ToolReference] = Field(min_length=1, max_length=MAX_TOOLS)
    limits: AssignmentLimits = Field(default_factory=AssignmentLimits)
    completion_condition: str | None = Field(default=None, max_length=1024)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    consent: bool = False

    @field_validator("name", "instructions", "completion_condition", "conversation_id")
    @classmethod
    def meaningful_text(cls, value):
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("blank or null-containing text is not accepted")
        return value

    @model_validator(mode="after")
    def source_in_tools(self):
        tools = [tool.identity for tool in self.allowed_tools]
        if len(tools) != len(set(tools)) or self.source.identity not in tools:
            raise ValueError("allowed tools must be unique and include the source reader")
        return self


class ControlRequest(Submission):
    expected_instruction_revision: int = Field(ge=1)
    expected_control_epoch: int = Field(ge=1)


class ReviseAssignmentRequest(CreateAssignmentRequest):
    expected_instruction_revision: int = Field(ge=1)
    expected_control_epoch: int = Field(ge=1)


class ApprovalDecisionRequest(ControlRequest):
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["approve", "decline"]


class AssignmentError(Exception):
    """Stable non-sensitive public error, independent of exception diagnostics."""

    def __init__(self, code: str, status_code: int = 409):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code):
            code = "assignment_unavailable"
        self.code = code
        self.status_code = status_code
        super().__init__(code)
