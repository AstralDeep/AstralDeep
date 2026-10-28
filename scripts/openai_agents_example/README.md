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
- `qualify.py` — the qualification runner; records client checkout and observed server image separately.
- `server_identity.py` — read-only observation of a directly published local Docker container; it reads no environment or credential values.
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

The original producer reported a local deployment run on 2026-10-27. Its unchanged
raw receipt is retained at `qualification/history/2026-10-27-unverified-receipt.json`
for provenance. It is **not current qualification**: it labels the client checkout
as the server candidate and predates corrections that reject artifact errors,
unapplied cancellation, non-idempotent control replay, unrelated policy failures,
and missing observation. The reported prior result is not independently established.
No fresh configured-auth staging run is claimed for the repaired candidate.

The runner now requires a clean, stable client checkout and independently observes
the actual serving container ID/image ID before and after the checks. Configure
`ASTRAL_BASE_URL` to the backend's directly published local port and
`ASTRAL_QUALIFICATION_SERVER_CONTAINER` to its exact container name/ID. The observer
accepts only local Docker contexts, loopback URLs, and a matching published backend
port8001; it pins the inspected Docker context explicitly, and refuses remote,
proxied, ambiguous, stopped, missing, or replaced targets. An external/proxied
installation needs an independently qualified observation mechanism; it cannot be
qualified by supplying an arbitrary SHA or copying the client revision.
The image ID identifies the observed immutable image, not an attestation that its
running filesystem is unmodified or that the image equals the client source.
The local receipt is a diagnostic and does not replace protected release evidence.

Provide the main, readonly, other-owner, and revoked framework credentials through
the approved product flow, using `ASTRAL_WORK_BEARER`,
`ASTRAL_WORK_BEARER_READONLY`, `ASTRAL_WORK_BEARER_OTHER_OWNER`, and
`ASTRAL_WORK_BEARER_REVOKED`. The human completes sign-in and issuance. Run from the
clean repository root in the isolated SDK environment, writing outside the checkout:

```bash
python -m scripts.openai_agents_example.qualify --output /private/path/new-receipt.json
```

Any failed or skipped required check exits nonzero. The artifact probe requires
successful authorized access to the actual versioned result envelope. A server
`unsupported`, `not_completed`, or `not_retained` envelope records its explicit
unavailability; it does not prove generated artifact content. Unexpected tool errors,
malformed data and generic `unavailable` results fail. Cancellation must apply to the
submitted operation, return `cancelled`, and replay without applying again. Refusal
checks accept specific policy outcomes; an outage is not a policy-denial success.
Logs/receipts retain bounded codes and identities, without server error payloads.

### Remaining qualification under #321

The earlier producer reported framework-origin one-shot operations staying `queued`:
Plane's administrative discovery selected interactive authority only. This is a
historical observation, not a verified defect at the repaired candidate. Cancelled
terminal control and an explicitly unavailable artifact must not be described as
successful model execution or artifact production. Recheck the real configured
server, worker, owner isolation, scope/admission/PHI refusal and revocation paths
against the exact repaired client and observed server artifact before merge.
No shared deployment, provider sign-in, component schema or orchestration change is
part of this isolated consumer example.
