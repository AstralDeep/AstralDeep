# OpenAI Agents SDK example consumer of Astral Work

An isolated, pinned example that consumes Astral's existing Work operations through the
MCP `2026-07-28` endpoint (`POST /mcp`) with the external OpenAI Agents SDK. The SDK
runtime lives only in this directory's own environment; it is never installed into the
product image, `backend/requirements.txt`, or the `backend.agents` namespace.

## Layout

- `contract.py` — the MCP `2026-07-28` wire contract: JSON-RPC envelopes, the required
  `params._meta` metadata, and the `MCP-Protocol-Version` / `Mcp-Method` / `Mcp-Name`
  mirror headers, including the Base64 sentinel form for names that are not plain ASCII.
- `client.py` — `AstralWorkClient`: discover, scoped `tools/list`, `tools/call`, the Work
  operations (submit with idempotency key, get, list, events, artifact, cancel, pause),
  and `wait_terminal` bounded polling that reports only server-truthful dispositions.
- `redaction.py` — allow-listed log and receipt fields plus credential scrubbing; logs
  carry method/phase/outcome codes only, mirroring the server's observability posture.
- `agent.py` — the SDK binding. `AstralAgentSession` disables cloud trace export and
  clears every registered trace processor **before any request can run**, records the
  ordering evidence, and exposes six strict function tools (submit, get, list, events,
  artifact, cancel). The `agents` module is imported lazily and verified against the
  OpenAI SDK surface, because Astral's own `backend/agents` namespace shadows the import
  when the backend source root is on `PYTHONPATH`.
- `qualify.py` — the live qualification runner; writes a synthetic staging receipt.
- `tests/` — the real-SDK offline suite (runs in this directory's isolated environment).
- `../../tests/test_openai_agents_example.py` (`scripts/tests/`) — the deterministic
  offline suite repo CI runs; it needs neither the third-party SDK nor a network.

## Isolated environment

```bash
python3.11 -m venv /tmp/astral-agents-example
/tmp/astral-agents-example/bin/pip install --require-hashes \
    -r scripts/openai_agents_example/requirements.lock.txt
```

The qualified SDK pin is `openai-agents==0.23.1`, the newest PyPI release published
before the immutable source review of the OpenAI Agents SDK dated 2026-10-06
(reviewed commit `911f106d9987a61a59654b046b6e891b212b00e3`; 0.23.1 released
2026-10-02, next release 0.24.0 on 2026-10-15). The pin is declared and hash-locked in
`requirements.lock.txt`; regenerate with `pip-compile --generate-hashes`.

Run everything from the repository root with the backend source root OFF `sys.path`
(otherwise `import agents` resolves to `backend.agents` and the session refuses to
start):

```bash
env PYTHONPATH="$PWD" /tmp/astral-agents-example/bin/python -m pytest \
    scripts/openai_agents_example/tests scripts/tests/test_openai_agents_example.py -q
```

## Security posture

- **Trace export**: `set_tracing_disabled(True)` and `set_trace_processors([])` run in
  the session constructor before any HTTP request. The real-SDK suite registers probe
  processors before and after construction and proves neither records a single trace or
  span during an agent run. No generation, tool, or audio payload and no credential can
  therefore reach external telemetry, and the sanitized-logging suite proves the same
  for process logs and the qualification receipt.
- **Credentials**: the runner accepts operator-issued framework bearers only. Issue them
  in the product's Connections surface (owner session → issue key → copy the once-shown
  secret) and export them as `ASTRAL_WORK_BEARER*`; the example never signs in, mints,
  revokes, prints, or persists credentials. SDK guardrails, tracing, and handoffs confer
  no server authority: the server enforces scopes, admission, PHI, egress, and
  confirmation gates on every call regardless of client-side configuration.
- **Deliberately excluded tools**: pause, emergency status/stop/resume, and the
  completion-subscription family are excluded from the agent tool set. Resume/wait/wake
  and the human-only decide/reconcile/delete actions are refused by the server and are
  demonstrated as denials, not advertised. Completion-subscription and resume features
  remain owned by the separate Work-subscription efforts and are not advertised here.

## Qualification record

Distinguish: **source review** (the SDK pin's provenance above), **tests** (the two
offline suites; deterministic, no network), **live qualification** (below), and
**deployment** (none — this example is not deployed; platform acceptance runs in PR CI).

Live qualification ran against a real local deployment (development posture, real
Keycloak issuer, real PostgreSQL through AstralPlane, `FF_MCP_SERVER=true` with
`FF_FRAMEWORK_CREDENTIALS=true` and `FF_PERSISTENT_AGENTS=true` enabled for the
qualification environment). Credentials were issued and revoked through the product's
Connections surface by the operator; the MCP `2026-07-28` metadata/header contract, the
Work lifecycle (submit, idempotent retry, bounded poll to a truthful `cancelled`
terminal state, artifact access, cancel with idempotent replay), and every denial class
(cross-owner `work_not_found`, revoked credential `401` + `WWW-Authenticate`, out-of-scope
tool projection, PHI `assignment_sensitive_content_refused`, human-only refusals) were
verified through the real server. The receipt is `qualification/staging-receipt.json`.

### Discovered defect (follow-up issue required)

Live qualification exposed that the persistent-assignment runner never executes
framework-authority one-shot operations: the runner tick discovers due operations only
through AstralPlane's `discover_due_operations_for_administration`, whose query filters
`data->'operation'->'authority'->>'origin'='interactive'`, so framework-submitted
operations remain `queued` indefinitely. Cancel and other control paths work, and this
qualification reaches a truthful `cancelled` terminal state through them, but
`completed`/`failed` terminal states are unreachable for framework submissions until the
component adds a framework-aware discovery (an AstralPlane change under its own
constitution, adopted here by pin movement). Fixing it is deliberately out of scope
here: this issue adds no orchestration.
