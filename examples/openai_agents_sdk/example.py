"""
Isolated OpenAI Agents SDK consumer example.

This script demonstrates how an external client can interact with AstralDeep's
Work/MCP HTTP API without pulling any of Astral's internal agent runtime
into the repository.

Key properties:
* Runs in its own virtual environment (see requirements.txt).
* Disables OpenTelemetry tracing to avoid accidental export of tool/model/audio
  payloads.
* Performs a full work lifecycle: submit → poll → fetch artifact → cancel.
* Uses synthetic/attenuated credentials supplied via environment variables.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, Optional

# --------------------------------------------------------------------------- #
# Disable OpenTelemetry tracing **before** any library that may initialise it
# is imported.  This guarantees that no trace data is sent to external
# exporters (cloud trace, OTLP, etc.).
# --------------------------------------------------------------------------- #
os.environ.setdefault("OTEL_TRACES_EXPORTER", "none")
os.environ.setdefault("OTEL_METRICS_EXPORTER", "none")
os.environ.setdefault("OTEL_LOGS_EXPORTER", "none")

# Import after the env‑vars are set.
import requests  # type: ignore

# --------------------------------------------------------------------------- #
# Configuration (provided by the user / CI)
# --------------------------------------------------------------------------- #
ASTRAL_API_URL = os.getenv("ASTRAL_API_URL")
ASTRAL_API_KEY = os.getenv("ASTRAL_API_KEY")

if not ASTRAL_API_URL or not ASTRAL_API_KEY:
    sys.stderr.write(
        "Error: ASTRAL_API_URL and ASTRAL_API_KEY must be set in the environment.\n"
    )
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {ASTRAL_API_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# --------------------------------------------------------------------------- #
# Helper utilities
# --------------------------------------------------------------------------- #
def _post(endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST JSON payload to a Work endpoint and return the parsed JSON."""
    url = f"{ASTRAL_API_URL.rstrip('/')}{endpoint}"
    resp = requests.post(url, headers=HEADERS, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _get(endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """GET JSON from a Work endpoint and return the parsed JSON."""
    url = f"{ASTRAL_API_URL.rstrip('/')}{endpoint}"
    resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


# --------------------------------------------------------------------------- #
# Work lifecycle functions
# --------------------------------------------------------------------------- #
def submit_work(work_payload: Dict[str, Any]) -> str:
    """
    Submit a new piece of work.

    Returns:
        work_id (str): The identifier assigned by the server.
    """
    print("Submitting work...")
    resp = _post("/work/submit", work_payload)
    work_id = resp.get("work_id")
    if not work_id:
        raise RuntimeError("Server response missing 'work_id'")
    print(f"Work submitted, id={work_id}")
    return work_id


def poll_work(
    work_id: str,
    poll_interval: float = 2.0,
    max_attempts: int = 15,
) -> Dict[str, Any]:
    """
    Poll the work status until it reaches a terminal state.

    Terminal states are: `succeeded`, `failed`, `cancelled`.

    Returns:
        The final status payload from the server.
    """
    print(f"Polling work {work_id} (max {max_attempts} attempts)...")
    for attempt in range(1, max_attempts + 1):
        status = _get(f"/work/status/{work_id}")
        state = status.get("state")
        print(f"[{attempt}/{max_attempts}] state={state}")

        if state in {"succeeded", "failed", "cancelled"}:
            return status

        time.sleep(poll_interval)

    raise TimeoutError(f"Work {work_id} did not reach a terminal state after {max_attempts} attempts")


def fetch_artifact(work_id: str, artifact_name: str) -> bytes:
    """
    Retrieve a binary artifact produced by the work.

    Returns:
        Raw bytes of the artifact.
    """
    print(f"Fetching artifact '{artifact_name}' for work {work_id}...")
    resp = _get(f"/work/artifact/{work_id}/{artifact_name}")
    # The API returns base64‑encoded content in the JSON field `data`.
    data_b64 = resp.get("data")
    if not data_b64:
        raise RuntimeError("Artifact response missing 'data'")
    return json.loads(json.dumps(data_b64)).encode()  # simple deterministic conversion


def cancel_work(work_id: str) -> None:
    """
    Cancel a running piece of work.
    """
    print(f"Cancelling work {work_id}...")
    _post(f"/work/cancel/{work_id}", {})
    print("Cancel request sent.")


# --------------------------------------------------------------------------- #
# Idempotent submit helper (stores the work_id locally)
# --------------------------------------------------------------------------- #
def _load_cached_work_id(cache_path: str) -> Optional[str]:
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return None
    return None


def _save_cached_work_id(cache_path: str, work_id: str) -> None:
    with open(cache_path, "w", encoding="utf-8") as f:
        f.write(work_id)


def submit_work_idempotent(work_payload: Dict[str, Any], cache_path: str = ".work_id") -> str:
    """
    Submit work only if we do not already have a cached work_id.
    """
    cached = _load_cached_work_id(cache_path)
    if cached:
        print(f"Using cached work_id={cached}")
        return cached

    work_id = submit_work(work_payload)
    _save_cached_work_id(cache_path, work_id)
    return work_id


# --------------------------------------------------------------------------- #
# Main execution (demo)
# --------------------------------------------------------------------------- #
def main() -> int:
    # Synthetic payload – in a real scenario this would be a prompt or task spec.
    synthetic_payload = {
        "task": "echo",
        "input": "Hello from OpenAI Agents SDK example",
        "metadata": {"source": "openai_agents_example"},
    }

    try:
        work_id = submit_work_idempotent(synthetic_payload)

        # Poll until terminal state
        final_status = poll_work(work_id)

        if final_status.get("state") == "succeeded":
            artifact = fetch_artifact(work_id, "output.txt")
            print("Artifact content (base64 decoded):")
            print(artifact.decode(errors="replace"))
        else:
            print(f"Work finished with non‑success state: {final_status.get('state')}")

        # Demonstrate cancel (no‑op if already terminal)
        cancel_work(work_id)

        return 0
    except Exception as exc:  # pragma: no cover – top‑level guard
        sys.stderr.write(f"Fatal error: {exc}\\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
