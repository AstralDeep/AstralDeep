"""Loads operator-qualified context and completion bounds for one exact owner/provider route.
The evidence host adapter rechecks this binding before proposing or dispatching model work.
"""

from dataclasses import dataclass
import hashlib
import json
import os
import stat


class ContextBudgetUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class ContextBudget:
    context_tokens: int
    max_output_tokens: int
    output_parameter: str
    fingerprint: str


def _object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ContextBudgetUnavailable("context_budget_invalid")
        result[key] = value
    return result


def load_budget(path, *, owner_id, provider, base_url, model):
    required = {"owner_id", "provider", "base_url", "model", "context_tokens", "max_output_tokens", "output_parameter"}
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError
            data = source.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError
        entries = json.loads(data.decode("utf-8"), object_pairs_hook=_object)
        if type(entries) is not list or len(entries) > 256:
            raise ValueError
        matches = []
        for entry in entries:
            if type(entry) is not dict or set(entry) != required:
                raise ValueError
            for name in ("owner_id", "provider", "base_url", "model"):
                value = entry[name]
                if (type(value) is not str or not value.strip() or len(value.encode()) > 2048
                        or any(ord(character) < 32 or ord(character) == 127 for character in value)):
                    raise ValueError
            window, output = entry["context_tokens"], entry["max_output_tokens"]
            if (type(window) is not int or not 1024 <= window <= 2_000_000
                    or type(output) is not int or not 1 <= output <= min(8192, window - 512)
                    or entry["output_parameter"] not in {"max_tokens", "max_completion_tokens"}):
                raise ValueError
            if all(entry[name] == value for name, value in (
                    ("owner_id", owner_id), ("provider", provider), ("base_url", base_url), ("model", model))):
                matches.append(entry)
        if len(matches) != 1:
            raise ValueError
        entry = matches[0]
        fingerprint = hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return ContextBudget(entry["context_tokens"], entry["max_output_tokens"], entry["output_parameter"], fingerprint)
    except (OSError, TypeError, ValueError, UnicodeError, RecursionError):
        raise ContextBudgetUnavailable("context_budget_unavailable") from None
