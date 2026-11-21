"""Measures strict primitive-tree generation by an explicitly configured live provider using synthetic prompts.
It uses the approved egress helper and current Primitives validator, and stores counts and response hashes without credentials or raw responses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from shared import external_http  # noqa: E402

PROMPTS = (
    "Show a text greeting saying Hello.",
    "Show a metric card: Temperature, 21 C.",
    "Show a list containing Apples, Pears, and Oranges.",
    "Show a table with Item and Count columns and rows Apples 3 and Pears 2.",
    "Show a card titled Status containing a text saying Ready.",
)


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)


def load_config(path: Path) -> ProviderConfig:
    values = dotenv_values(path)
    fields = [str(values.get(key) or "").strip() for key in ("OPENAI_BASE_URL", "LLM_MODEL", "OPENAI_API_KEY")]
    if not all(fields):
        raise ValueError("provider configuration is incomplete")
    endpoint = urlsplit(fields[0])
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("provider endpoint must be HTTPS without credentials, query or fragment")
    return ProviderConfig(*fields)


def _object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def check_response(payload: dict) -> dict:
    from astralprims import StaticResolver, validate_generated

    try:
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError
    except (KeyError, IndexError, TypeError):
        return {"outcome": "protocol_error"}
    summary = {"response_sha256": hashlib.sha256(content.encode()).hexdigest()}
    try:
        if len(content.encode()) > 65536:
            raise ValueError("response is too large")
        tree = json.loads(content, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, RecursionError):
        return {**summary, "outcome": "invalid_json"}
    try:
        valid = not validate_generated(tree, StaticResolver())
    except (TypeError, ValueError, RecursionError):
        valid = False
    return {**summary, "outcome": "valid" if valid else "invalid_tree"}


def measure(config: ProviderConfig, count: int) -> dict:
    from astralprims import __version__, strict_schema

    if not 1 <= count <= 50:
        raise ValueError("sample count must be 1 to 50")
    schema = json.dumps(strict_schema(), sort_keys=True, separators=(",", ":"))
    instructions = (
        "Return exactly one JSON primitive component tree matching this schema. "
        "Use only literal values; no bindings, handles, actions, URLs, HTML or Markdown links. "
        "Do not wrap the component in a root or document field. Schema: " + schema
    )
    samples = []
    for index in range(count):
        try:
            response = external_http.request(
                "POST", config.base_url.rstrip("/") + "/chat/completions",
                api_key=config.api_key,
                json_body={
                    "model": config.model,
                    "messages": [{"role": "system", "content": instructions}, {"role": "user", "content": PROMPTS[index % len(PROMPTS)]}],
                    "temperature": 0,
                    "max_tokens": 1024,
                    "stream": False,
                    "response_format": {"type": "json_object"},
                },
                timeout=45, max_response_bytes=131072, allow_redirects=False, trust_environment=False,
            )
            result = check_response(response.json())
        except Exception:
            result = {"outcome": "transport_error"}
        samples.append({"sample": index + 1, "prompt": index % len(PROMPTS) + 1, **result})
    valid = sum(row["outcome"] == "valid" for row in samples)
    return {
        "format": "astral.surface-generation-diagnostic/v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "provider_host": urlsplit(config.base_url).hostname,
        "model": config.model,
        "primitives_version": __version__,
        "schema_sha256": hashlib.sha256(schema.encode()).hexdigest(),
        "scope": "strict primitive trees, literal values, JSON-object decoding; host surfaces and on-device models unmeasured",
        "attempted": count, "valid": valid, "valid_rate": valid / count, "samples": samples,
    }


def write_report(path: Path, report: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=5)
    args = parser.parse_args()
    try:
        report = measure(load_config(args.env_file), args.samples)
        write_report(args.report, report)
    except Exception as error:
        print("surface diagnostic failed: " + type(error).__name__, file=sys.stderr)
        return 2
    print(f"{report['valid']}/{report['attempted']} strict primitive trees valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
