# astral-sdk

This Python SDK supports older servers that accept owner-issued **framework credentials**
for Work operations over MCP. The quickstart and CLI examples below apply to those servers.

Personal mesh nodes authenticate external tools with an owner-minted caller ticket
and a signed proof for each request. This SDK version sends a bearer credential,
which a personal node refuses. [Personal-node caller protocol](../specs/106-sign-in-free-clients/contracts/caller-ticket.md)

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

op = client.get_operation(existing_operation_id)
if op.disposition == "completed":
    artifact = client.get_artifact(op.id)
    print(artifact.result)
```

On a server with the retired assignment engine, an exact replay of an accepted
`submit_operation` returns the original operation. A new submission returns
`turn_engine_retired`. Read existing work with `get_operation` or `list_operations`.
An `AsyncAstralClient` with the same methods is available for `asyncio` code.

## What this talks to

On servers with framework credential support, the SDK sends JSON-RPC 2.0 to
`POST {base_url}/mcp`. A current personal mesh node accepts signed caller tickets
on `/mcp` and `/a2a`. Those tickets do not grant the framework Work API, and the
node has no bearer REST route for submitting or resuming assignments.

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

Framework callers can poll an existing operation. The server does not expose commands
for resuming an assignment or waking a waiter through this SDK.

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
python -m astral_sdk list --base-url https://your-astral-instance.example
python -m astral_sdk poll <operation_id> --after-revision 1
python -m astral_sdk cancel <operation_id> --expected-revision 2
```

`--base-url`/`--token` may also come from `$ASTRAL_BASE_URL`/`$ASTRAL_TOKEN`.
Every command prints one JSON document to stdout on success, or a JSON error
object to stderr with a non-zero exit code.

## Terminal sessions on older servers

The terminal CLI and Python methods target servers that expose the owner-authenticated
`/api/terminals` routes. A personal mesh node returns HTTP 404 for these routes.
Its `/mcp` and `/a2a` endpoints accept owner-minted caller tickets. Neither endpoint
provides the SDK terminal-session API.

On an older server with the terminal routes enabled, start and input require an
exact approval in a fresh signed-in web review tab. The tab must have no selected
conversation or open settings. If the server returns `terminal_review_context_required`
(409), open the review tab before issuing a new request.
The operator must configure an isolated SSH runtime with `TERMINAL_SSH_PROFILE_FILE`
and enroll its exact machine and host key through the normal encrypted owner settings.
Execution is unavailable without that profile; the orchestrator never opens a host shell.
Terminal sessions are owner-only: they require a current owner
access token, and framework credentials (`afk_…`) are refused locally with
`terminal_owner_authentication_required` before any request is sent.

Keep a signed-in Astral UI connected. Creating a session or sending input publishes
a confirmation card there; approve the exact operation and target in that UI.
The CLI waits up to 120 seconds for its result and never approves or retries a write
itself. Use `--approval-timeout 0` to return immediately with
`terminal_confirmation_required` and a `proposal_id`, then inspect it with
`python -m astral_sdk terminal-approval PROPOSAL_ID --token "$ASTRAL_TOKEN"`.
After a timeout, inspect the original request before creating another.
Approvals expire with the original request authority or after five minutes, and
cannot survive a server restart. Existing sessions retain their normal owner,
permission, idle and lifetime checks independently of a completed request.

```bash
session=$(python -m astral_sdk terminal-create --cols 100 --rows 30 \
    --target "$ASTRAL_TERMINAL_MACHINE_ID" --directory /srv/project \
    --base-url "$ASTRAL_BASE_URL" --token "$ASTRAL_TOKEN" | jq -r .session_id)
python -m astral_sdk terminal-write "$session" --data "echo hello\n" \
    --token "$ASTRAL_TOKEN"
python -m astral_sdk terminal-read "$session" --offset 0 --token "$ASTRAL_TOKEN"
python -m astral_sdk terminal-resize "$session" --cols 120 --rows 40 --token "$ASTRAL_TOKEN"
python -m astral_sdk terminal-interrupt "$session" --token "$ASTRAL_TOKEN"
python -m astral_sdk terminal-attach "$session" --token "$ASTRAL_TOKEN"   # stream until the session ends
python -m astral_sdk terminal-close "$session" --token "$ASTRAL_TOKEN"
python -m astral_sdk terminal-list --token "$ASTRAL_TOKEN"
```

`terminal-create` accepts an optional approved `--target` machine ID and an
absolute `--directory` on that runtime. `terminal-read` accepts `--offset` for
reconnect-safe resumption: output is retained server-side in a bounded ring,
and `next_offset` feeds the next read. A closed read reports `close_reason` and
the runtime's actual `exit_code` when one was supplied; unavailable status stays
`null`. `terminal-write` sends text, or raw bytes with a `base64:` prefix.

The client methods (`create_terminal_session`, `write_terminal`,
`read_terminal`, `stream_terminal`, `resize_terminal`, `interrupt_terminal`,
`close_terminal`, `list_terminal_sessions`) exist on both `AstralClient` and
`AsyncAstralClient` and speak the same REST surface.

## Emergency control on older servers

`emergency-status` and `emergency-stop` accept a framework credential with the
corresponding read or control scope. `emergency-resume --expected-revision N`
requires a current owner access token. Owner tokens use `/api/emergency-stop`; framework credentials
use MCP for status and stop and are refused for resume. The SDK never retries
owner control writes automatically after a transport failure: check status
before making another explicit decision.
