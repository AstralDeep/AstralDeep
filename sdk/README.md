# astral-sdk

Python SDK for driving Astral's **Work** operations from your own external
tooling — a script, an agent framework, an MCP-aware host — using an
owner-issued **framework credential** rather than a live interactive session.

A framework credential is minted from the product's **Connections** surface
(while you are signed in) and carries its own independent expiry, scope set
and admission allowance — it is never a copy of your session and it can
never review a proposed action, resolve an uncertain outcome, or delete
retained work on your behalf (see "Human-only commands" below).

## Install

```bash
pip install astral-sdk
# with an integration extra, e.g.:
pip install astral-sdk[openai]
```

This package's only *runtime* dependency is `httpx`. Every framework
integration under `astral_sdk.integrations` (and the MCP stdio bridge) is an
optional extra imported lazily, only when you actually call into it.

## Quickstart

```python
from astral_sdk import AstralClient

client = AstralClient("https://your-astral-instance.example", token)

op = client.submit_operation(
    idempotency_key="my-script-run-1",
    name="Draft a note",
    instructions="Write one short paragraph about the weather today.",
)
op = client.wait_for_terminal(op.id)
if op.disposition == "completed":
    artifact = client.get_artifact(op.id)
    print(artifact.result)
```

An `AsyncAstralClient` with the identical method set is available for
`asyncio` code.

## What this talks to

A framework credential's only ingress into Deep is the MCP endpoint
(`POST {base_url}/mcp`, JSON-RPC 2.0) — there is no separate REST route for
this credential class. `AstralClient` speaks that wire format directly; you
never need to construct JSON-RPC envelopes yourself.

The tool catalog (`astral_sdk.tools.ASTRAL_TOOLS`) and the `Operation` model's
fields are generated from the live server's own source
(`scripts/export_work_contract.py` in the main repository) into the checked-in
`astral_sdk/work_contract.json` — this SDK cannot silently drift from what
the server actually implements.

## Human-only commands

Approving/rejecting an uncertain outcome, reconciling it, and deleting
retained work are refused for every framework credential
(`assignment_human_required`) — by design, before any read or mutation. These
are not projected as tools at all; only a signed-in human, in the product's
own UI, can perform them.

## Scopes

| Scope | Unlocks |
|---|---|
| `operations.submit` | `astral_submit_operation` |
| `operations.read` | `astral_get_operation`, `astral_list_operations`, `astral_get_operation_events` |
| `operations.control` | `astral_cancel_operation`, `astral_pause_operation` |
| `artifacts.read` | `astral_get_artifact` |

`resume`/`wait`/`wake` are intentionally absent — the server does not yet
implement them for a framework caller (they refuse honestly with
`framework_control_unavailable` rather than silently no-op), so this SDK
never advertises them.

## Framework integrations

```python
from astral_sdk.integrations import openai_agents, anthropic, langchain, generic

tools = openai_agents.as_openai_tools()          # OpenAI Chat Completions tools=[...]
tools = anthropic.as_anthropic_tools()           # Anthropic Messages tools=[...]
tools = langchain.as_langchain_tools(client)     # list[StructuredTool]
schemas = generic.FUNCTION_SCHEMAS               # framework-agnostic {name, description, parameters}
```

## MCP bridge

To expose Astral's tools to a local MCP-speaking host over stdio:

```bash
python -m astral_sdk.mcp_bridge  # see astral_sdk.mcp_bridge.Bridge for embedding it directly
```

`Bridge.serve_stdio()` works with no extra dependency; `Bridge.serve_with_official_sdk()`
uses the official `mcp` package (`pip install astral-sdk[mcp]`) for full
protocol compliance with MCP hosts that expect it.

## CLI

```bash
python -m astral_sdk submit --base-url https://your-astral-instance.example \
    --token "$ASTRAL_TOKEN" --idempotency-key run-1 --name "Draft a note" \
    --instructions "Write one short paragraph."
python -m astral_sdk get <operation_id>
python -m astral_sdk poll <operation_id> --after-revision 1
python -m astral_sdk cancel <operation_id> --expected-revision 2
```

`--base-url`/`--token` may also come from `$ASTRAL_BASE_URL`/`$ASTRAL_TOKEN`.
Every command prints one JSON document to stdout on success, or a JSON error
object to stderr with a non-zero exit code.
