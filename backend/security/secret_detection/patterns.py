"""Deterministic contextual secret detection patterns and entropy heuristics.

SOURCE NOTICE & ATTRIBUTION:
Pattern concepts and candidate classification structures are adapted from
OpenAI Guardrails (openai-guardrails-python, commit 4e3f3717017cc5575ca7522719f34a340c210029,
licensed under Apache 2.0 / MIT). The patterns and entropy heuristics are implemented as a pure,
dependency-free evaluation and detection module without importing the Guardrails runtime, its
external network services, or conflicting dependencies.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Pattern, Set, Tuple

# Pinned metadata for audit provenance
SOURCE_PIN = "openai-guardrails-python@4e3f3717017cc5575ca7522719f34a340c210029"
LICENSE_NOTICE = "Adapted under MIT / Apache 2.0 terms. Dependency-free implementation."

# ---------------------------------------------------------------------------
# Shannon Entropy Calculation
# ---------------------------------------------------------------------------

def calculate_shannon_entropy(data: str) -> float:
    """Calculate Shannon entropy in bits per character.
    
    Higher entropy indicates higher randomness, typical of generated API keys
    and cryptographic tokens.
    """
    if not data:
        return 0.0
    entropy = 0.0
    length = len(data)
    frequencies: Dict[str, int] = {}
    for char in data:
        frequencies[char] = frequencies.get(char, 0) + 1
    for count in frequencies.values():
        p = count / length
        entropy -= p * math.log2(p)
    return round(entropy, 4)


# ---------------------------------------------------------------------------
# Clinical & Domain Exemptions (Protection against Clinical Meaning Loss)
# ---------------------------------------------------------------------------

CLINICAL_EXEMPTION_WORDS: frozenset[str] = frozenset({
    "secretion",
    "secretions",
    "secretin",
    "secretase",
    "secreting",
    "secretory",
    "secretor",
    "secretagogue",
    "secretomotor",
    "neurosecretion",
    "hypersecretion",
    "hyposecretion",
})

TOKEN_ACCOUNTING_EXEMPTIONS: frozenset[str] = frozenset({
    "token_count",
    "tokens_count",
    "token_counts",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "tokens_used",
    "token_usage",
    "max_tokens",
})

# Genomic DNA/RNA sequence filter (e.g. FASTA format or base repeats)
RE_GENOMIC_SEQUENCE = re.compile(r"^[ACGTUN\s]{20,}$", re.IGNORECASE)

# Standard medical identifier formats (should not be flagged as secrets)
RE_ICD10_CODE = re.compile(r"^[A-TV-Z][0-9][0-9AB](?:\.[0-9A-KXZ]{1,4})?$")
RE_LOINC_CODE = re.compile(r"^\d{3,5}-\d$")
RE_NDC_CODE = re.compile(r"^\d{4,5}-\d{3,4}-\d{1,2}$")


def is_clinical_exemption(token: str) -> bool:
    """Return True if the candidate token represents medical/clinical terminology
    or genomic sequence that must be preserved without false-positive redaction."""
    lowered = token.strip().lower()
    if lowered in CLINICAL_EXEMPTION_WORDS or lowered in TOKEN_ACCOUNTING_EXEMPTIONS:
        return True
    # Fast check for DNA/RNA sequence
    if len(token) >= 20 and RE_GENOMIC_SEQUENCE.match(token):
        return True
    if RE_ICD10_CODE.match(token) or RE_LOINC_CODE.match(token) or RE_NDC_CODE.match(token):
        return True
    return False


# ---------------------------------------------------------------------------
# High-Fidelity Provider & Infrastructure Pattern Definitions
# ---------------------------------------------------------------------------

PROVIDER_SECRET_PATTERNS: Tuple[Tuple[str, Pattern[str], str], ...] = (
    # Anthropic Claude API keys (checked before openai to avoid sk- prefix overlap)
    (
        "anthropic_api_key",
        re.compile(r"\bsk-ant-(?:api\d{2}-)?[A-Za-z0-9_\-]{32,}\b"),
        "[REDACTED:anthropic_key]",
    ),
    # OpenAI: legacy, project, admin, and service-account keys
    (
        "openai_api_key",
        re.compile(r"\bsk-(?!ant-)(?:proj-|admin-|svcacct-)?[A-Za-z0-9_\-]{20,}\b"),
        "[REDACTED:openai_key]",
    ),
    # Google AI / Gemini API keys
    (
        "google_ai_key",
        re.compile(r"\bAIza[0-9A-Za-z\-_]{30,45}\b"),
        "[REDACTED:google_key]",
    ),
    # GitHub personal access tokens and OAuth tokens
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{36,}|github_pat_[A-Za-z0-9_]{50,90})\b"),
        "[REDACTED:github_token]",
    ),
    # AWS Access Key IDs (Standard, Temporary, Assumed Role)
    (
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA|AROA)[0-9A-Z]{16}\b"),
        "[REDACTED:aws_key]",
    ),
    # Hugging Face user access tokens
    (
        "huggingface_token",
        re.compile(r"\bhf_[a-zA-Z0-9]{34,}\b"),
        "[REDACTED:hf_token]",
    ),
    # Slack Bot / User / App tokens
    (
        "slack_token",
        re.compile(r"\bxox[baprs]-[0-9]{10,13}-[0-9]{10,13}-[a-zA-Z0-9]{24,32}\b"),
        "[REDACTED:slack_token]",
    ),
    # JSON Web Tokens (JWT)
    (
        "jwt_token",
        re.compile(r"\beyJ[a-zA-Z0-9_\-]{10,}\.eyJ[a-zA-Z0-9_\-]{10,}\.[a-zA-Z0-9_\-]{10,}\b"),
        "[REDACTED:jwt]",
    ),
    # PKCS#8, RSA, EC, OPENSSH Private Keys
    (
        "private_key",
        re.compile(r"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+)?PRIVATE KEY-----"),
        "[REDACTED:private_key]",
    ),
    # Database Connection URIs with embedded basic auth passwords
    (
        "database_url_credentials",
        re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://(?P<user>[^:\s/]+):(?P<pass>[^@\s/]+)@"),
        "[REDACTED:database_credentials]",
    ),
    # Generic Bearer Authorization Headers
    (
        "bearer_token",
        re.compile(r"(?i)\bBearer\s+[a-zA-Z0-9_.\-]{25,}\b"),
        "[REDACTED:bearer_token]",
    ),
)

# ---------------------------------------------------------------------------
# Contextual Key-Value Secret Assignment Patterns
# ---------------------------------------------------------------------------

# Contextual assignments e.g. api_key="...", password: "...", client_secret='...'
RE_CONTEXTUAL_ASSIGNMENT = re.compile(
    r"""(?i)(?:^|[\s,;{(])["']?(?P<key>api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|app[_-]?secret|password|passwd|pwd|private[_-]?key|secret[_-]?key)["']?\s*[:=]\s*["']?(?P<val>[A-Za-z0-9!@#$%^&*()_+\-=\[\]{}|;:,.<>?/~`]{10,})["']?"""
)

# Minimum entropy threshold for contextual values
CONTEXTUAL_MIN_ENTROPY = 3.0
CONTEXTUAL_MIN_LENGTH = 12
