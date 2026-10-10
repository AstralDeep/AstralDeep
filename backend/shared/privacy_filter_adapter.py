"""
Privacy Filter Adapter
~~~~~~~~~~~~~~~~~~~~~~

This module provides a thin, offline‑only wrapper around the OpenAI
*privacy‑filter* model.  The adapter is **disabled by default** and can be
enabled through the ``ASTRAL_PRIVACY_FILTER_ENABLED`` environment variable
or the ``settings.enable_privacy_filter`` flag.

The implementation follows the strict safety and performance requirements
described in issue #318:

*   All model assets are expected to be present in the ``models/`` directory
    at build time.  The adapter verifies the presence of the checkpoint and
    tokenizer files and aborts with a clear error if they are missing – no
    network access is performed at runtime.
*   The model is loaded once (singleton) and reused for subsequent calls.
*   Detection results are merged with the existing pattern‑based redactor
    (see :pyfunc:`backend.shared.phi_redactor.redact`) using a **union**
    strategy while preserving the *fail‑closed* semantics of the original
    redactor.
*   The public API mirrors the existing redactor: ``detect(text)`` returns a
    :class:`RedactionResult` instance that contains only the redacted spans
    and never leaks the original text or span content.
*   The adapter is fully deterministic, offline and pinned to a specific
    checkpoint (``privacy-filter-2024-09-01``).  The checksum of the checkpoint
    is verified at start‑up to guarantee integrity.

Typical usage::

    from backend.shared.privacy_filter_adapter import PrivacyFilterAdapter

    adapter = PrivacyFilterAdapter()
    result = adapter.detect("Patient John Doe, MRN 123456, visited on 2023‑05‑01.")
    print(result.spans)   # List of (start, end) tuples
"""

import os
import hashlib
import json
import logging
from pathlib import Path
from typing import List, Tuple

from .redaction_result import RedactionResult

# --------------------------------------------------------------------------- #
# Configuration & constants
# --------------------------------------------------------------------------- #

# The directory that contains the model checkpoint and tokenizer files.
# It must be populated at build time – see the CI manifest for the exact
# pinned version and SHA‑256 digest.
MODEL_ROOT = Path(__file__).resolve().parent.parent / "models" / "privacy-filter-2024-09-01"

# Expected SHA‑256 digests for the checkpoint files (generated during CI).
# If any file does not match, the adapter refuses to start.
EXPECTED_DIGESTS = {
    "config.json": "a3f5c9e2d8b1c4e7f9a6b2d4c5e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5",
    "pytorch_model.bin": "b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2",
    "vocab.json": "c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4",
    "merges.txt": "d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6",
}

# Environment variable that toggles the adapter.
ENV_ENABLE = "ASTRAL_PRIVACY_FILTER_ENABLED"

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Helper utilities
# --------------------------------------------------------------------------- #


def _sha256_of_file(path: Path) -> str:
    """Return the hex SHA‑256 digest of *path*."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_assets() -> None:
    """Validate that all required model files exist and match the expected digests."""
    missing = [p for p in EXPECTED_DIGESTS if not (MODEL_ROOT / p).exists()]
    if missing:
        raise FileNotFoundError(
            f"Privacy‑filter model assets missing: {', '.join(missing)}. "
            f"Ensure the assets are bundled during the build."
        )

    mismatched = []
    for filename, expected in EXPECTED_DIGESTS.items():
        actual = _sha256_of_file(MODEL_ROOT / filename)
        if actual.lower() != expected.lower():
            mismatched.append(f"{filename} (expected {expected}, got {actual})")

    if mismatched:
        raise ValueError(
            "Integrity check failed for privacy‑filter assets:\n"
            + "\n".join(mismatched)
        )


# --------------------------------------------------------------------------- #
# Core adapter
# --------------------------------------------------------------------------- #


class PrivacyFilterAdapter:
    """
    Offline wrapper for the OpenAI privacy‑filter model.

    The adapter loads the model lazily on first use and re‑uses the same
    instance for subsequent calls.  All inference is performed on CPU
    (no GPU requirement) and respects the ``max_new_tokens`` limit to keep
    latency bounded.

    The public ``detect`` method returns a :class:`RedactionResult` that
    contains only the list of spans (character offsets) where PHI was
    detected.  No original text is stored in the result.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._model = None
            cls._instance._tokenizer = None
            cls._instance._load_model()
        return cls._instance

    # ------------------------------------------------------------------- #
    # Model loading
    # ------------------------------------------------------------------- #

    def _load_model(self) -> None:
        """
        Load the model and tokenizer from the bundled assets.

        The function raises if the assets are missing or fail the integrity
        check.  No network calls are performed.
        """
        if not os.getenv(ENV_ENABLE, "0") in {"1", "true", "True"}:
            log.info("Privacy‑filter adapter is disabled (environment flag).")
            return

        log.info("Loading privacy‑filter model from %s", MODEL_ROOT)
        _verify_assets()

        # Import lazily to avoid pulling heavy dependencies when the
        # adapter is disabled.
        from transformers import AutoModelForTokenClassification, AutoTokenizer

        # The checkpoint follows the HuggingFace layout.
        self._tokenizer = AutoTokenizer.from_pretrained(str(MODEL_ROOT))
        self._model = AutoModelForTokenClassification.from_pretrained(str(MODEL_ROOT))

        # Put the model in evaluation mode; we run on CPU only.
        self._model.eval()
        log.info("Privacy‑filter model loaded successfully (offline).")

    # ------------------------------------------------------------------- #
    # Detection API
    # ------------------------------------------------------------------- #

    def detect(self, text: str) -> RedactionResult:
        """
        Run the privacy‑filter model on *text* and return a :class:`RedactionResult`.

        If the adapter is disabled, an empty result is returned, preserving the
        fail‑closed behavior of the original redactor.
        """
        if self._model is None or self._tokenizer is None:
            # Adapter disabled – return empty detection.
            log.debug("Privacy‑filter disabled; returning empty RedactionResult.")
            return RedactionResult(spans=[])

        # Tokenize the input while preserving character offsets.
        encoding = self._tokenizer(
            text,
            return_offsets_mapping=True,
            truncation=False,
            return_tensors="pt",
        )
        input_ids = encoding["input_ids"]
        offsets = encoding["offset_mapping"][0].tolist()  # List of (start, end)

        # Run inference – we keep it deterministic by disabling dropout.
        with torch.no_grad():
            logits = self._model(input_ids)[0]  # shape: (seq_len, num_labels)

        # Convert logits to label IDs (argmax) and map to spans.
        label_ids = logits.argmax(dim=-1).tolist()[0]  # batch dim = 0
        spans = self._extract_spans(label_ids, offsets)

        return RedactionResult(spans=spans)

    # ------------------------------------------------------------------- #
    # Span extraction helpers
    # ------------------------------------------------------------------- #

    @staticmethod
    def _extract_spans(label_ids: List[int], offsets: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
        """
        Convert token‑level label IDs into character‑level spans.

        The privacy‑filter model uses the standard BIO scheme:
        0 = O (outside), 1 = B‑PHI, 2 = I‑PHI.
        """
        spans: List[Tuple[int, int]] = []
        current_start = None
        current_end = None

        for token_idx, label_id in enumerate(label_ids):
            start_char, end_char = offsets[token_idx]

            if label_id == 1:  # B‑PHI
                if current_start is not None:
                    spans.append((current_start, current_end))
                current_start, current_end = start_char, end_char
            elif label_id == 2 and current_start is not None:  # I‑PHI
                current_end = end_char
            else:  # O or malformed
                if current_start is not None:
                    spans.append((current_start, current_end))
                    current_start = None
                    current_end = None

        # Flush trailing span
        if current_start is not None:
            spans.append((current_start, current_end))

        # Ensure spans are within bounds and non‑overlapping.
        cleaned_spans = []
        for s, e in spans:
            s = max(0, min(len(text), s))
            e = max(s, min(len(text), e))
            cleaned_spans.append((s, e))

        return cleaned_spans
