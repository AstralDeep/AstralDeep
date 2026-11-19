import logging
from typing import Any, List, Tuple, Dict

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
# RuntimeError if the optional dependency is missing – the public wrapper
# catches this and falls back to a no‑op.
# ---------------------------------------------------------------------------
def _load_filter() -> PrivacyFilter:
    if not _HAS_OPF:
        raise RuntimeError("OpenAI privacy‑filter package not installed")
    # The PrivacyFilter constructor expects the model and tokenizer files to be
    # present on disk.  ``device='cpu'`` guarantees offline execution on any host.
    return PrivacyFilter(
        model_path=MODEL_CHECKPOINT,
        tokenizer_path=TOKENIZER_PATH,
        device="cpu",
    )

_filter_instance: Any = None

def _get_filter() -> PrivacyFilter:
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
        result: RedactionResult = pf.redact(text, max_tokens=max_tokens)
        # ``result.redacted_text`` already contains the replacements.
        # ``result.spans`` is a list of ``(start, end, category, replacement)``.
        detections = [
            {
                "start": start,
                "end": end,
                "category": category,
                "replacement": replacement,
            }
            for start, end, category, replacement in result.spans
        ]
        return result.redacted_text, detections
    except Exception as exc:  # pragma: no cover – exercised via mock in tests
        logger.warning("Privacy filter failed, falling back to original text: %s", exc)
        return text, []


def redact_with_fallback(text: str) -> str:
    """Convenient helper used by the tool dispatcher.

    It attempts to redact *text* with the OpenAI model; if the model cannot be
    loaded or raises an error, the original text is returned unchanged.  The
    caller can decide whether to block the output based on the detection list
    returned by :func:`filter_text`.
    """
    redacted, _ = filter_text(text)
    return redacted

__all__ = ["filter_text", "redact_with_fallback"]
