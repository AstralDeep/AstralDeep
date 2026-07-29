# Feature Specification: MCP `2026-07-28` Revision — Conformance Read and Adoption Decision

**Feature Branch**: `064-followups-mcp-upgrade`
**Created**: 2026-07-29
**Status**: Decided — no product migration required
**Input**: Standing instruction recorded 2026-07-23: "the next time AstralDeep is picked up, implement the new MCP revision … read the *published* `2026-07-28` spec, not the RC, and start with the **MCP Apps** extension, which lands in the same space as Astral's SDUI contribution."

## Overview

The Model Context Protocol specification's `2026-07-28` revision **published on schedule**, superseding `2025-11-25`. The standing instruction called it an implementation task. Reading the published specification against the codebase shows it is not one: **AstralDeep speaks none of the wire protocol this revision changes**, so there is nothing to migrate and nothing that breaks.

What the revision *does* demand is a decision, because two of its official extensions land squarely on ground AstralDeep already occupies:

- **MCP Apps** (server-rendered interactive UI) sits exactly where AstralDeep's central contribution sits — and resolves it the **opposite** way.
- **Tasks** (long-running operations with durable handles) is a near-isomorph of what feature 063 shipped last week as `tracked_job`.

This feature records the verified conformance read and the three adoption verdicts. It is a **decision/documentation feature: no product code changes**, in the pattern of [049-ietf-id-decision](../049-ietf-id-decision/spec.md) and [050-cresco-integration-decision](../050-cresco-integration-decision/spec.md).

**Verdicts:**

| Item | Verdict | One-line reason |
|---|---|---|
| Core protocol (all 9 major changes) | **NO-OP** | Astral speaks no MCP wire protocol; zero occurrences of every changed construct |
| MCP Apps | **DIVERGE — deliberately, and this is the thesis result** | Apps ships sandboxed HTML; Astral ships a declarative vocabulary rendered per target. Apps structurally cannot reach Astral's non-web clients |
| Tasks | **DIVERGE, and cite the convergence** | Astral's 063 `tracked_job` independently converged on the same design and is strictly stronger at the consent boundary |
| Authorization hardening | **ALREADY CONFORMANT** — one doc-only gap | Keycloak/OIDC + RFC 8693 already; `iss` validation is the IdP library's, not Astral's, since Astral is not an MCP client |
| JSON Schema 2020-12 | **NO-OP** | The change *loosens* what is permitted; Astral's `input_schema` dicts were never constrained by MCP in the first place |

## Verified conformance read (2026-07-29, against `main@90474ef`)

The 2026-07-23 wiki note asserted this posture from the RC announcement and pinned it at `bde0b58`. It is **re-verified here against the published specification and the current tree**, and it holds — now including the constructs the RC summary never mentioned.

A repository-wide search across `backend/**/*.py`, `*.json` and `*.js` returns **zero occurrences of every wire construct this revision adds, changes, or removes**:

| Construct | This revision | Occurrences in `backend/` |
|---|---|---|
| `Mcp-Session-Id` | removed | 0 |
| `protocolVersion` | moved into `_meta` | 0 |
| `notifications/initialized` | removed (stateless core) | 0 |
| `server/discover` | **newly REQUIRED of servers** | 0 |
| `subscriptions/listen` | replaces GET + `resources/subscribe` | 0 |
| `resultType` | **newly required on all results** | 0 |
| `Last-Event-ID` | removed (no SSE resumability) | 0 |
| `logging/setLevel`, `ping`, `notifications/roots/list_changed` | removed | 0 |
| `listRoots`, `createMessage`, `sampling/` | deprecated | 0 |
| `inputSchema` (camelCase) | loosened to JSON Schema 2020-12 | 0 |
| `tasks/get` | moved to extension | 0 |

The reason is structural, not incidental. AstralDeep carries MCP as a **message shape** — the `MCPRequest`/`MCPResponse` dataclasses in [`backend/shared/protocol.py`](../../backend/shared/protocol.py) — over its **own** A2A WebSocket transport, and since feature 040 over an in-process `LoopbackSocket` for the built-in agents. There is no MCP HTTP transport, no session handshake to remove, and no server/client role in the MCP sense. Tool schemas are snake_case `AgentSkill.input_schema` dicts; camelCase `inputSchema` never appears on Astral's wire.

**Consequences:** the nine major changes are transport-layer changes to a transport Astral does not speak. The three deprecation clocks (Roots, Sampling, Logging) are moot because all three are unused. **Nothing breaks on 2026-07-28, and no deadline exists.**

### Corrections to the 2026-07-23 RC-based note

The pre-publication note recorded "eight changes" from the RC announcement. The published revision has **nine major changes plus twelve minor ones**, and four majors were absent from the RC summary entirely: the required `server/discover` RPC, `subscriptions/listen`, the removal of `ping`/`logging/setLevel`, and the removal of SSE resumability. Two further corrections:

- The RC note said "**Full** JSON Schema 2020-12 for tool schemas." The published change (SEP-2106) **loosens** `inputSchema`/`outputSchema` to *allow* any 2020-12 keyword — a permission, not a requirement.
- The RC note listed two official extensions. The published overview lists **three** notable ones: Tasks, MCP Apps, and **Skills over MCP** (a working-group effort — noted below as a watch item, not a verdict).

Also new and not in the RC summary: a formal **feature lifecycle policy** with a minimum twelve-month deprecation window and a deprecated-features registry; and the deprecation of **OAuth 2.0 Dynamic Client Registration** in favour of Client ID Metadata Documents.

## Verdict 1 — MCP Apps: DIVERGE (the load-bearing finding)

MCP Apps and AstralDeep's SDUI pipeline answer the **same question** — how does a server put interactive UI into a chat? — and answer it with **opposite mechanisms**.

| | MCP Apps | AstralDeep SDUI |
|---|---|---|
| What the server emits | An HTML page bundled with its JS and CSS, at a `ui://` resource | A declarative component tree (`astralprims` `.to_dict()`) |
| Who renders | The host, into a **sandboxed iframe** | The orchestrator, per target (`webrender`), then ROTE adapts |
| Back-channel | `postMessage` JSON-RPC, its own `ui/*` MCP dialect | Ordinary `ui_event` / `ui_render` frames |
| Security model | **Contain** untrusted code — sandbox isolation, CSP, host-controlled capability grants | **Admit no code** — closed primitive vocabulary, escape-by-default `esc()` |
| Reach | Anywhere there is a browser engine | Web HTML, PySide6, Jetpack Compose, SwiftUI, and voice profiles |
| Accessibility | Whatever the app author wrote into their HTML | Guaranteed once, centrally, in the renderer |

**Verdict: diverge, deliberately.** Three reasons, in order of weight:

1. **The iframe is a hard reach ceiling.** MCP Apps requires a browser engine to render its payload. AstralDeep's Windows (PySide6/Qt Widgets), Android (Compose), and Apple (SwiftUI) clients have no such surface, and its voice profiles have no visual surface at all — yet all of them render the *same* component tree today, which is precisely what feature 063 demonstrated when the remote-machines surface reached every client with zero per-client code. Adopting Apps would mean either shipping a browser engine into every native client or maintaining two parallel UI paths.
2. **The security models are not variants of one idea, they are opposites.** Apps assumes the server's code is untrusted and invests in containing it. Astral never admits code into the render path at all, so there is no sandbox to escape — the same reasoning that made feature 058's BYO validator pure-AST. Bridging would import the threat model Astral was built to avoid.
3. **It is evidence *for* the thesis, not against it.** An official MCP extension arriving in this space confirms the problem is real and general. That it resolves to generated-markup-in-a-sandbox, while Astral resolves to a rendered declarative vocabulary, is the comparison [[astral-sdui-vs-generated-markup]] already frames — now against a standard rather than a strawman.

**What this obligates:** nothing in code. It obligates the thesis to *name* MCP Apps as the closest standardized alternative and state the trade honestly — Apps buys unbounded expressiveness inside one sandbox on one class of client; Astral buys cross-client and cross-modality reach and central accessibility, at the cost of being able to render only what its vocabulary can say.

**Watch item:** if a future MCP Apps revision adds a declarative, non-HTML rendering mode, that is the trigger to revisit — it would collapse the mechanism gap and turn this from *diverge* into *bridge*.

## Verdict 2 — Tasks: DIVERGE, and cite the convergence

The Tasks extension and feature 063's `tracked_job` were designed independently, weeks apart, and arrived at nearly the same design:

| MCP Tasks | AstralDeep 063 (`orchestrator/remote_jobs.py`, `tracked_job`) |
|---|---|
| `CreateTaskResult` with `taskId`, durable **before** the response is sent | `tracked_job` row INSERTed durably before `run_job` returns |
| Statuses `working` / `input_required` / `completed` / `failed` / `cancelled` | Job states, plus a structured `unconfirmed` for consequential non-retryable timeouts |
| `tasks/get` polling, `pollIntervalMs` | `_remote_job_poll_loop` read-only background poller |
| Task ID survives client disconnect/restart | Survives page reload and orchestrator restart; boot reconciliation resolves jobs that finished during an outage |
| `input_required` → client answers via `tasks/update` | US3 destructive-confirmation proposals |
| `notifications/tasks` | `notify_on_finish` client banner |

**Verdict: keep the in-house vocabulary, and document the divergence as a result.** Two substantive places where 063 is *stronger*, both of which adopting the standard wire would dilute:

- **`unconfirmed` has no MCP equivalent.** Tasks offers `failed` and `cancelled`; neither is honest about a submit whose outcome is genuinely unknown. 063 introduced `unconfirmed` precisely because reporting a possibly-submitted Slurm job as `failed` invites a duplicate submission.
- **`input_required` is a status; Astral's confirmation is an authorization.** A 063 proposal is durable, single-use, TTL-bound, owner-bound, and **argument-fingerprint-bound**, and a machine (unattended) turn is refused outright *before any transport contact*. MCP's `input_required` carries none of those properties — it is a request for input, not a consent artifact.

The convergence is worth citing in the thesis as independent validation of the design; the deltas are worth citing as where a general standard stops and a consent-anchored system has to keep going.

## Verdict 3 — Authorization: already conformant, one doc-only gap

The revision's authorization items are a conformance checklist, and Astral is Keycloak/OIDC-rooted with RFC 8693 attenuated delegation already:

- **RFC 9207 `iss` validation (SEP-2468)** binds *MCP clients* redeeming an authorization code. Astral is not an MCP client; its OIDC redemption is Keycloak's, through `python-jose` and the established `web_auth` flow. **No action.**
- **`application_type` in Dynamic Client Registration (SEP-837)** and **credential-to-issuer binding (SEP-2352)** apply to DCR, which Astral does not use — its clients are statically configured in the realm. **No action.**
- **DCR deprecated in favour of Client ID Metadata Documents.** Astral never adopted DCR, so it is on the recommended side of this by accident. **No action.**

The one real gap is unrelated to this revision and already tracked: `docs/keycloak-realm-settings.md` and the `.env.example` `KEYCLOAK_ALLOWED_AZP` comment were **verified fixed** during this pass — the Apple client-id entry in the follow-ups register is stale and has been closed.

## Verdict 4 — JSON Schema 2020-12: NO-OP

SEP-2106 *loosens* `inputSchema`/`outputSchema` to permit any 2020-12 keyword, adds `$ref` resolution requirements, and bounds composition keywords. The 2026-07-23 note called this "the one bounded, concrete piece of work."

It is not work, for two reasons: Astral's `AgentSkill.input_schema` is a plain snake_case dict that no MCP validator ever sees, and the change grants permission rather than imposing a constraint. Declaring `$schema: "https://json-schema.org/draft/2020-12/schema"` on Astral's tool schemas would add a field nothing reads. **Revisit only if Astral ever exposes a conformant MCP endpoint** (see the trigger below).

## The one trigger that would change all of this

Every verdict above rests on a single fact: **AstralDeep does not expose an MCP endpoint.** If that ever changes — publishing the agent catalog as a conformant MCP server for third-party hosts — then the entire revision becomes live at once: `server/discover` becomes mandatory, every result needs `resultType`, `subscriptions/listen` replaces the change-notification path, the required `Mcp-Method`/`Mcp-Name` headers apply, and `ttlMs`/`cacheScope` become required on all list/read results.

That would be its own feature with its own spec. Nothing in this one presumes it, and the stateless core actually makes it **cheaper** than it would have been under `2025-11-25` — there is no session store to build.

## Non-goals

- No product code changes. (The follow-up defect fixes shipped in the same branch are separate work; see the branch's other commits.)
- Does not implement any MCP extension, endpoint, or transport.
- Does not write the thesis prose; it records the verdicts the thesis will cite.

## Sources

- `https://modelcontextprotocol.io/specification/2026-07-28` and `/changelog` — published revision and its full key-changes list (retrieved 2026-07-29)
- `https://modelcontextprotocol.io/extensions/apps/overview` — MCP Apps mechanism, iframe/postMessage security model
- `https://modelcontextprotocol.io/extensions/tasks/overview` — Tasks lifecycle, statuses, polling and `tasks/update`
- `extrepo:AstralDeep/AstralDeep@90474ef` — `backend/shared/protocol.py`, `backend/shared/a2a_bridge.py`, `backend/orchestrator/remote_jobs.py`, and the absence checks tabulated above
