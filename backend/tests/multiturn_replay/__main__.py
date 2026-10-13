"""Runs one bounded replay fixture from stdin through the diagnostic harness.
ReplayEngine produces the result JSON; invalid input exits without a result.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_REPOSITORY_ROOT = _BACKEND_ROOT.parent
for source_root in (
    _BACKEND_ROOT,
    _REPOSITORY_ROOT / "components/AstralPlane/src",
    _REPOSITORY_ROOT / "components/LETS/src",
):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from tests.multiturn_replay.engine import RESULT_FORMAT, ReplayError, run_scenario  # noqa: E402
from tests.multiturn_replay.scenarios import (  # noqa: E402
    REQUEST_FORMAT,
    SCENARIOS,
    ReplayScenarioError,
    compute_digest,
)

_MAX_STDIN_BYTES = 64 * 1024
_REQUEST_KEYS = {"format", "scenario_id"}


def _load_request(payload: bytes) -> str:
    if not payload or len(payload) > _MAX_STDIN_BYTES:
        raise ReplayError("invalid_input_size")

    def reject_constant(_value: str) -> None:
        raise ValueError

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ReplayError("invalid_request_json") from None
    if not isinstance(value, dict) or set(value) != _REQUEST_KEYS:
        raise ReplayError("invalid_request_shape")
    if value["format"] != REQUEST_FORMAT or not isinstance(value["scenario_id"], str):
        raise ReplayError("invalid_request_shape")
    return str(value["scenario_id"])


def main() -> int:
    try:
        scenario_id = _load_request(sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1))
        scenario = next(
            (item for item in SCENARIOS if item.scenario_id == scenario_id), None
        )
        if scenario is None:
            raise ReplayError("unsupported_scenario")
        if compute_digest(scenario) != scenario.digest:
            raise ReplayError("scenario_digest_mismatch")
        result = run_scenario(scenario)
        output = json.dumps(
            result,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (ReplayError, ReplayScenarioError) as exc:
        sys.stderr.buffer.write(f"multiturn replay refused: {exc}\n".encode("ascii"))
        return 2
    except Exception:
        sys.stderr.buffer.write(b"multiturn replay refused: execution_failed\n")
        return 2
    sys.stdout.buffer.write((output + "\n").encode("ascii"))
    assert result["format"] == RESULT_FORMAT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
