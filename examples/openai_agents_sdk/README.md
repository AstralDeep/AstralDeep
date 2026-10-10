# Isolated OpenAI Agents SDK Example

This example demonstrates how an external **OpenAI Agents SDK** consumer can interact with Astral Deep’s Work/MCP APIs **without** pulling any of Astral’s internal agent runtime into the main repository.

## Goals

* Use a **separate** Python environment (its own `requirements.txt`).
* Consume the public Work endpoints (`submit`, `status`, `events`, `artifact`, `cancel`).
* Disable any default OpenTelemetry/Cloud‑trace export so that no tool, model or audio payload is sent to external telemetry back‑ends.
* Show a full lifecycle:
  1. Submit synthetic work.
  2. Idempotent retry on network glitches.
  3. Poll for terminal state with bounded retries.
  4. Retrieve an artifact.
  5. Cancel a running job (demonstrated in the test suite).

## Running the example

```bash
# 1️⃣ Create an isolated venv
python -m venv .venv
source .venv/bin/activate

# 2️⃣ Install pinned dependencies
pip install -r requirements.txt

# 3️⃣ Export required credentials (these are **synthetic/attenuated** for the demo)
export ASTRAL_API_URL="https://staging.astraldeep.example/api"
export ASTRAL_API_KEY="synthetic-demo-key"

# 4️⃣ Run the script
python example.py
```

The script prints each step and exits with status 0 on success.

## Tests

The example ships with deterministic offline tests that mock all HTTP interactions.

```bash
pytest -q
```

All tests run in < 30 seconds and do not require network access.

## Security notes

* Tracing is explicitly disabled by setting `OTEL_TRACES_EXPORTER=none` **before** any OpenTelemetry import.
* No credentials are ever written to logs; they are only sent in the `Authorization: Bearer …` header of the request.
* The example does **not** import any `backend/agents` package from AstralDeep, preserving namespace isolation.

---  

For any questions, open an issue or contact the repository maintainers.
