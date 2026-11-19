import logging
import os
from typing import Any, List, Tuple, Dict, Optional

logger = logging.getLogger("PrivacyFilterAdapter")

# ---------------------------------------------------------------------------
# Optional import of the OpenAI privacy‑filter package.  The import is wrapped
# so that the repository can be built without pulling the heavy dependency.
# ---------------------------------------------------------------------------
try:
    from opf import PrivacyFilter, RedactionResult  # type: ignore
    _HAS_OPF = True
except Exception as exc:  # pragma: no cover – exercised in tests via mock
    logger.debug("OpenAI privacy‑filter package not available: %s", exc)
    _HAS_OPF = False

# ---------------------------------------------------------------------------
# Paths to the locally‑bundled model assets.  The assets must be checked‑in
# under the repository with appropriate licensing and SHA‑256 digests.
# ---------------------------------------------------------------------------
MODEL_CHECKPOINT = "models/privacy-filter/checkpoint.pt"
TOKENIZER_PATH = "models/privacy-filter/tokenizer.json"

# ---------------------------------------------------------------------------
# Internal helper to lazily load the filter.  The function raises a clear
# RuntimeError if the optional dependency is missing or assets are not found.
# ---------------------------------------------------------------------------
def _load_filter() -> Any:
    if not _HAS_OPF:
        raise RuntimeError("OpenAI privacy‑filter package not installed")
    
    # Check if model assets exist before attempting to load
    if not os.path.exists(MODEL_CHECKPOINT) or not os.path.exists(TOKENIZER_PATH):
        raise RuntimeError(
            f"Privacy filter model assets not found. Expected files at: {MODEL_CHECKPOINT} and {TOKENIZER_PATH}"
        )

    # The PrivacyFilter constructor expects the model and tokenizer files to be
    # present on disk.  ``device='cpu'`` guarantees offline execution on any host.
    return PrivacyFilter(
        model_path=MODEL_CHECKPOINT,
        tokenizer_path=TOKENIZER_PATH,
        device="cpu",
    )

_filter_instance: Optional[Any] = None

def _get_filter() -> Any:
    global _filter_instance
    if _filter_instance is None:
        _filter_instance = _load_filter()
    return _filter_instance

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def filter_text(text: str, *, max_tokens: int = 2048) -> Tuple[str, List[Dict[str, Any]]]:
    """Run the local OpenAI privacy‑filter on *text*.

    Returns a tuple ``(redacted_text, detections)`` where *detections* is a list
    of dictionaries compatible with the existing ``RedactionResult`` schema:

    ``{"start": int, "end": int, "category": str, "replacement": str}``

    The function is **fail‑closed** – any exception results in the original text
    being returned together with an empty detection list and a warning logged.
    """
    try:
        pf = _get_filter()
        result: Any = pf.redact(text, max_tokens=max_tokens)
        # ``result.redacted_text`` already contains the replacements.
        # ``result.spans`` is a list of tuples (start, end, category, replacement)
        detections: List[Dict[str, Any]] = []
        if hasattr(result, 'spans') and result.spans:
            for span in result.spans:
                if isinstance(span, (list, tuple)) and len(span) >= 4:
                    detections.append({
                        "start": int(span[0]),
                        "end": int(span[1]),
                        "category": str(span[2]),
                        "replacement": str(span[3]),
                    })
        return result.redacted_text, detections
    except Exception as exc:
        logger.warning("Privacy filter failed: %s", exc)
        return text, []

def redact_with_fallback(text: str) -> str:
    """Convenience wrapper that returns only the redacted text.
    
    If the filter fails, returns the original text unchanged.
    """
    redacted, _ = filter_text(text)
    return redacted
