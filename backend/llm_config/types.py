"""Shared LLM-config types — CredentialSource (recorded on every llm_call audit event),
ResolvedConfig, and LLMUnavailable — imported across llm_config, orchestrator, and
persistent_agents credential-resolution code.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CredentialSource(str, Enum):
    USER = "user"
    SYSTEM = "system"
    OPERATOR_DEFAULT = "operator_default"


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    base_url: str
    model: str


class LLMUnavailable(Exception):
    pass
