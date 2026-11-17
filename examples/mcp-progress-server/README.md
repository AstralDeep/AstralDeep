# MCP progress server

A stateless MCP server for protocol `2026-07-28` with two tools. `echo` returns its text. `long_task` counts through up to 20 steps and sends a progress notification after each one, then its result. It needs no key and calls nothing outside this machine.

## Run it

```text
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py --port 8765
```

It listens on `127.0.0.1` only.

## Add it on a Mac

1. Open Connections and choose Add.
2. Paste `http://127.0.0.1:8765/mcp` and mark the connection local to this Mac.
3. Approve the card. The server asks for no sign-in, so the tools appear in chat once you approve.

A node refuses loopback addresses, so this works as a `local` connection and not as a shared one.

## What a request needs

Every call except `server/discover` carries the `MCP-Protocol-Version: 2026-07-28` header and the `io.modelcontextprotocol/protocolVersion` key in `params._meta`. A call without both is refused with `UnsupportedProtocolVersionError`. A `tools/call` for `long_task` that sets `params._meta.progressToken` answers as a server-sent event stream. The notifications come first and the result is the last event.

To write your own connection or skill, see [your own agents and skills](../../docs/your-own-agents-and-skills.md).
