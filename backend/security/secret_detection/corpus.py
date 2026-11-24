"""Synthetic evaluation corpus for secret detector benchmark.

Strict audit requirements:
- Uses ONLY synthetic, generated mock tokens and dummy patterns.
- Pinned digests and reproducible test populations.
- Balanced positive and clinical negative populations to measure recall,
  precision, and clinical meaning preservation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Dict, List, Tuple

CORPUS_VERSION = "2026.11.1-synthetic"


@dataclass(frozen=True, slots=True)
class TestCase:
    case_id: str
    category: str
    is_positive: bool
    text: str
    description: str


# ---------------------------------------------------------------------------
# Synthetic Positives Population (Must be detected and redacted)
# ---------------------------------------------------------------------------

SYNTHETIC_POSITIVES: Tuple[TestCase, ...] = (
    TestCase(
        case_id="pos-openai-standard",
        category="openai",
        is_positive=True,
        text="export OPENAI_API_KEY=" + "sk-" + "testabc1234567890abcdef1234567890",
        description="Standard legacy-style OpenAI API key",
    ),
    TestCase(
        case_id="pos-openai-proj",
        category="openai",
        is_positive=True,
        text="Configuration loaded " + "sk-proj-" + "1234567890abcdefABCDEF1234567890abcdefABCDEF for agent runtime",
        description="Modern OpenAI Project-scoped API key",
    ),
    TestCase(
        case_id="pos-openai-svcacct",
        category="openai",
        is_positive=True,
        text="Auth token: " + "sk-svcacct-" + "9876543210fedcbaFEDCBA9876543210fedcbaFEDCBA",
        description="OpenAI Service Account API key",
    ),
    TestCase(
        case_id="pos-anthropic",
        category="anthropic",
        is_positive=True,
        text="Client configured with key " + "sk-ant-" + "api03-abcdef1234567890ABCDEF1234567890-test123456",
        description="Anthropic Claude API Key",
    ),
    TestCase(
        case_id="pos-google-ai",
        category="google",
        is_positive=True,
        text="Gemini client init: " + "AIzaSy" + "D1234567890abcdefABCDEF123456789",
        description="Google Gemini AIza key format",
    ),
    TestCase(
        case_id="pos-github-pat",
        category="github",
        is_positive=True,
        text="Using " + "ghp_" + "1234567890abcdefghijklmnopqrstuvwxyz12 for checkout",
        description="GitHub classic personal access token",
    ),
    TestCase(
        case_id="pos-github-fine-grained",
        category="github",
        is_positive=True,
        text="Token " + "github_pat_" + "11AAAAAAA0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
        description="GitHub fine-grained personal access token",
    ),
    TestCase(
        case_id="pos-aws-access-key",
        category="aws",
        is_positive=True,
        text="AWS credentials: user " + "AKIA" + "IOSFODNN7EXAMPLE logged into region",
        description="AWS Access Key ID format",
    ),
    TestCase(
        case_id="pos-huggingface",
        category="huggingface",
        is_positive=True,
        text="Downloading weights with token " + "hf_" + "abcdefghijklmnopqrstuvwxyz12345678",
        description="Hugging Face User Access Token",
    ),
    TestCase(
        case_id="pos-slack-token",
        category="slack",
        is_positive=True,
        text="Bot notification: " + "xoxb-" + "123456789012" + "-" + "1234567890123" + "-" + "abcdefghijklmnopqrstuvwx",
        description="Slack bot token format",
    ),
    TestCase(
        case_id="pos-jwt-bearer",
        category="jwt",
        is_positive=True,
        text="Headers: Authorization: Bearer " + "eyJhbGciOi" + "JIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        description="Full JWT authorization header",
    ),
    TestCase(
        case_id="pos-private-key",
        category="crypto",
        is_positive=True,
        text="Certificate stored: " + "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEowIBAAKCAQEA0syntheticKeyMaterial1234567890\n-----END RSA PRIVATE KEY-----",
        description="RSA Private key PEM block",
    ),
    TestCase(
        case_id="pos-db-connection-string",
        category="database",
        is_positive=True,
        text="Connecting to " + "postgresql://" + "dbuser:StrongPass987#@postgres-cluster.local:5432/clinical_db",
        description="PostgreSQL URI with embedded basic auth password",
    ),
    TestCase(
        case_id="pos-contextual-secret",
        category="contextual",
        is_positive=True,
        text="settings = {'client_secret': '" + "x9#kP9$vL1@qZ8!w" + "'}",
        description="Contextual JSON assignment with high-entropy credential",
    ),
)

# ---------------------------------------------------------------------------
# Synthetic Negatives Population (MUST NOT be redacted / 0 Meaning Loss)
# ---------------------------------------------------------------------------

SYNTHETIC_NEGATIVES: Tuple[TestCase, ...] = (
    TestCase(
        case_id="neg-clinical-note",
        category="clinical",
        is_positive=False,
        text="Patient presents with acute shortness of breath and wheezing. Administered albuterol nebulizer 2.5mg.",
        description="Standard clinical doctor consultation note",
    ),
    TestCase(
        case_id="neg-clinical-secretion",
        category="clinical_exemptions",
        is_positive=False,
        text="Pulmonary examination demonstrated thick mucous secretion and increased bronchial secretions.",
        description="Clinical text with physiological secretion terminology",
    ),
    TestCase(
        case_id="neg-clinical-secretin",
        category="clinical_exemptions",
        is_positive=False,
        text="Patient scheduled for secretin stimulation testing to evaluate pancreatic exocrine function.",
        description="Clinical diagnostic test mentioning hormone secretin",
    ),
    TestCase(
        case_id="neg-clinical-secretory",
        category="clinical_exemptions",
        is_positive=False,
        text="Endometrial biopsy demonstrated secretory phase endometrium consistent with normal menstrual cycle.",
        description="Clinical pathology report with secretory terminology",
    ),
    TestCase(
        case_id="neg-genomic-sequence",
        category="genomic",
        is_positive=False,
        text="Target DNA primer sequence: ACGTGATCGTAGCTAGCTAGCATCGATCGATCG",
        description="Genomic oligonucleotide sequence (must not be confused with base64 secret)",
    ),
    TestCase(
        case_id="neg-medical-codes",
        category="medical_codes",
        is_positive=False,
        text="Encounter diagnoses: ICD-10 E11.9, I10. Lab orders: LOINC 2345-7 glucose, LOINC 2093-3 cholesterol.",
        description="Formal clinical diagnostic ICD-10 and LOINC laboratory codes",
    ),
    TestCase(
        case_id="neg-ndc-medication",
        category="medical_codes",
        is_positive=False,
        text="Dispensed prescription under NDC 00069-3150-83 for atorvastatin 20mg daily.",
        description="National Drug Code (NDC) medication identification",
    ),
    TestCase(
        case_id="neg-token-metrics",
        category="accounting",
        is_positive=False,
        text="Usage metrics report: prompt_tokens=450, completion_tokens=120, total_tokens=570, tokens_used=570.",
        description="LLM token accounting keys that must not trigger secret redaction",
    ),
    TestCase(
        case_id="neg-code-identifiers",
        category="code",
        is_positive=False,
        text="def compute_token_count(text: str) -> int: return len(text.split())",
        description="Safe programming language token counters",
    ),
    TestCase(
        case_id="neg-general-prose",
        category="prose",
        is_positive=False,
        text="The conference organizers requested all attendees to register their badges at the entrance.",
        description="Ordinary English documentation prose",
    ),
)

# ---------------------------------------------------------------------------
# Synthetic Edge Cases (Must not crash, leak, or enter infinite loop)
# ---------------------------------------------------------------------------

SYNTHETIC_EDGE_CASES: Tuple[TestCase, ...] = (
    TestCase(
        case_id="edge-empty-string",
        category="edge",
        is_positive=False,
        text="",
        description="Empty string input",
    ),
    TestCase(
        case_id="edge-nul-byte",
        category="edge",
        is_positive=False,
        text="Clinical observation text with embedded NUL byte \x00 character safely preserved.",
        description="Text with NUL character",
    ),
    TestCase(
        case_id="edge-unicode-multilingual",
        category="edge",
        is_positive=False,
        text="患者主诉头痛伴恶心，体温 37.2°C。生命体征平稳。👨‍⚕️ 处方：布洛芬缓释胶囊。",
        description="Multilingual UTF-8 clinical text with Chinese and emojis",
    ),
    TestCase(
        case_id="edge-oversized-safe",
        category="edge",
        is_positive=False,
        text="Safe benign diagnostic data chunk. " * 3000,
        description="Oversized input exceeding 100KB",
    ),
)


def get_all_test_cases() -> Tuple[TestCase, ...]:
    return SYNTHETIC_POSITIVES + SYNTHETIC_NEGATIVES + SYNTHETIC_EDGE_CASES


def compute_corpus_digest() -> str:
    """Compute deterministic SHA-256 digest of entire test corpus."""
    hasher = hashlib.sha256()
    for tc in get_all_test_cases():
        hasher.update(tc.case_id.encode("utf-8"))
        hasher.update(tc.category.encode("utf-8"))
        hasher.update(b"1" if tc.is_positive else b"0")
        hasher.update(tc.text.encode("utf-8"))
    return hasher.hexdigest()
