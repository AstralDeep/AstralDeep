# Feature Specification: MCP `2026-07-28` — Conformance Decision and Phased Upgrade

**Feature Branch**: `064-mcp-2026-07-28-decision`

**Created**: 2026-07-29

**Amended**: 2026-07-29 (upgrade scope added; verdict sections re-verified)

**Status**: In progress — decision verdicts RATIFIED; implementation started 2026-07-31

**Input**: Standing instruction recorded 2026-07-23: "the next time AstralDeep is picked up, implement the new MCP revision … read the *published* `2026-07-28` spec, not the RC, and start with the **MCP Apps** extension, which lands in the same space as Astral's SDUI contribution." Amended 2026-07-29 by owner direction: *"create a spec to upgrade to this latest version of MCP"* → scope decision **Both, phased** (D1).

## Overview

The Model Context Protocol specification's `2026-07-28` revision published on schedule, superseding `2025-11-25`. It carries **nine major changes and twelve minor ones**, plus four deprecations and a new feature-lifecycle policy.

This spec does two things, in order:

1. **Records the conformance read and the adoption verdicts** (§"Verified conformance read" and §"Adoption verdicts"). These are **decided**: nothing in AstralDeep breaks on this revision, because AstralDeep speaks none of the wire protocol it changes.
2. **Specifies the upgrade the owner asked for**, in two independently shippable phases:
   - **Phase A — internal dialect alignment.** Astral's own MCP-shaped message envelope adopts the `2026-07-28` constructs it can honestly carry, and the additive-field intolerance that currently makes *any* such change fail as a silent 30-second hang is fixed first.
   - **Phase B — a conformant MCP `2026-07-28` server endpoint**, behind `FF_MCP_SERVER` (default **OFF**), publishing the per-user-authorized agent catalog to third-party MCP hosts over Streamable HTTP with an OAuth 2.1 protected-resource posture.

Phase B is the trigger the original decision named: exposing an MCP endpoint is what makes the revision live. Phase A is a prerequisite for it and is independently valuable, because the defect it fixes is real today.

**This is no longer a documentation-only feature.** Constitution XIII no longer applies to the whole diff; Phases A and B carry product code, and the Principle III/X/XI gates apply to them in full. The verdict sections remain governed by XIII's documentation-integrity bar.

## Owner Decisions (2026-07-29)

Decisions, not assumptions; they close what would otherwise be `[NEEDS CLARIFICATION]` markers.

| # | Question | Decision |
|---|---|---|
| D1 | What does "upgrade to the latest MCP" mean, given Astral exposes no MCP endpoint? | **Both, phased.** Phase A aligns the internal dialect; Phase B exposes a conformant endpoint. Phase A may ship alone; Phase B may be deferred. |
| D2 | New feature directory or amend 064? | **Amend `specs/064-mcp-2026-07-28-decision/`.** The decision and the upgrade it triggers are one feature. The directory slug is retained so every existing reference (CLAUDE.md, kos-wiki, PR #150) stays valid; the branch name is set equal to it. |
| D3 | Does Phase A change the client-facing wire protocol? | **No.** `mcp_request`/`mcp_response` appear nowhere in `backend/shared/ui_protocol.json`; MCP is agent-channel-only. Phase A touches no client. |
| D4 | Does Phase B adopt MCP Apps? | **No — the DIVERGE verdict stands.** Phase B instead registers MCP as a **`webrender` render target**, which is the seam Constitution II already provides. |
| D5 | Does Phase B adopt the Tasks extension? | **No.** `resultType: "task"` is an **extension-defined** value, not a core reservation. Because the endpoint never advertises `io.modelcontextprotocol/tasks` in its `server/discover` capabilities, a task-shaped result is outside the negotiated value set and is refused. The 063 `tracked_job` divergence stands. |
| D6 | Does Phase B implement `subscriptions/listen`? | **Minimally, and honestly — not fully.** The core says "All implementations MUST support the base protocol, versioning, and the message patterns", and subscribe-and-notify is one of the three named patterns, so refusing the method outright would be a silent non-conformance. The endpoint therefore *accepts* `subscriptions/listen` and acknowledges with an **empty agreed notification set**, which the specification explicitly provides for ("notification types the server does not support are omitted"). No change-notification type is advertised or emitted: `StreamManager` states in code that surviving an orchestrator restart is not a goal, and no `tools/list` change-notification wiring exists. |
| D7 | Can an MCP caller invoke a destructive verb? | **No.** An MCP request has no live human on an interactive channel, which is exactly what feature 063's `_no_live_human` refuses. Destructive verbs are refused before any transport contact, honestly, with the reason. |
| D8 | Does Phase B reuse the existing bearer-token path? | **No.** A new FastAPI **dependency callable** (`mcp_authz.py` — not a new third-party package; see FR-060) and a **new Keycloak audience** (`astral-mcp`) with real audience validation. Reusing `get_current_user_payload` would import its `verify_aud: False` posture, which is the exact confused-deputy scenario RFC 8707 exists to prevent. |
| D9 | Default posture of the new flag? | **`FF_MCP_SERVER` default `False`, fail-closed**, read once at import (container recreate to enable), matching `FF_REMOTE_COMPUTE`/`FF_BYO_AGENTS`. Flag-off MUST be byte-identical: no route, no metadata document, no advertisement. |
| D10 | Is Phase B live-verified before merge? | **Yes, against a real third-party MCP host** on a staging deployment of the candidate image, per Constitution X. A purpose-built conformant client MAY be used as diagnostic evidence when no third-party host is available, but it does **not** close FR-058 or SC-014 and Phase B remains code-shaped-but-unproven until the real-host gate passes. |
| D11 | Populate or remove the dead `output_schema` field? | **Populate it.** `AgentSkill.output_schema` is declared, echoed by `AgentCard.to_dict`, and written by nothing in product code. Phase B's `tools/list` has a slot for it, so removing it would have to be undone. It is populated at registration where derivable, and set explicitly to `null` where not (FR-016). |

## Why now (verified problem statement)

Three claims, each verified against `main@e492668b` on 2026-07-29.

1. **The revision published and the RC-based note was materially incomplete.** The pre-publication note recorded "eight changes" from the RC announcement. The published revision has **nine major changes plus twelve minor ones**, and four majors were absent from the RC summary entirely: the `server/discover` RPC servers MUST implement, `subscriptions/listen`, the removal of `ping`/`logging/setLevel`/`notifications/roots/list_changed`, and the removal of SSE resumability. — `https://modelcontextprotocol.io/specification/2026-07-28/changelog` (retrieved 2026-07-29)

2. **Astral's MCP envelope cannot survive an additive field wherever `Message.from_json` parses it.** That dispatcher splats raw JSON keys — `MCPRequest(**data)` / `MCPResponse(**data)` at [`backend/shared/protocol.py:94`](../../backend/shared/protocol.py#L94)–`97` — with no unknown-key filtering, so a peer emitting *any* new field raises `TypeError`. The exception is caught and the frame is **dropped** (agent side [`base_agent.py:292`](../../backend/shared/base_agent.py#L292)–`293`; orchestrator side [`orchestrator.py:3774`](../../backend/orchestrator/orchestrator.py#L3774)–`3775`), so `pending_requests[request_id]` is never resolved and the caller hangs to the 30-second timeout reporting "Tool call timed out" ([`orchestrator.py:14156`](../../backend/orchestrator/orchestrator.py#L14156)). `RegisterUI.from_json` already fixed exactly this class of bug with an explicit valid-field filter and a comment saying so ([`protocol.py:1466`](../../backend/shared/protocol.py#L1466)–`1477`); the MCP envelopes never got it.

   **Exposure, stated precisely** (it is narrower than "every transport"): the request direction is affected only where an agent parses a serialized frame — [`base_agent.py:279`](../../backend/shared/base_agent.py#L279), i.e. networked WebSocket agents. The BYO bundle runner and the Windows desktop client parse requests as plain dicts (`req.get(...)`) and already ignore unknown request fields. The in-process path passes a live `MCPRequest` object to the agent without serializing it ([`orchestrator.py:14350`](../../backend/orchestrator/orchestrator.py#L14350)), so it has no request-direction parse at all. The **response** direction goes through `Message.from_json` on every transport ([`orchestrator.py:3703`](../../backend/orchestrator/orchestrator.py#L3703)) and is affected everywhere. **This is a live defect independent of MCP** and it is the prerequisite for every other change here.

3. **`MCPResponse` has no success/failure discriminator, and one failure shape already delivers renderable output.** Every decision in the codebase keys on `error is None` ([`orchestrator.py:13104`](../../backend/orchestrator/orchestrator.py#L13104), `:13142`, `:13530`, `:10611`), yet [`agents/general/mcp_server.py:119`](../../backend/agents/general/mcp_server.py#L119)–`124` — and the same shape in `dice_roller`, `ml_services`, `remote_compute`, and the generated BYO runner — returns an error dict **alongside `ui_components`**. A failed call therefore still delivers components to the canvas, and callers disagree about whether it succeeded. `resultType` is the construct this revision adds for precisely that ambiguity. (Note the narrower pair — a non-`None` `result` *and* a non-`None` `error` on the same response — is not constructed by any first-party code. Two sites pass both keywords, and both are pass-through relays of a peer-supplied dict: [`orchestrator.py:2466`](../../backend/orchestrator/orchestrator.py#L2466) relays a fenced personal-agent frame, [`base_agent.py:679`](../../backend/shared/base_agent.py#L679) relays a hop response. Neither relay enforces exclusivity — the personal-agent allowlist at [`orchestrator.py:2403`](../../backend/orchestrator/orchestrator.py#L2403) permits both keys — so FR-010's single-outcome rule has to be enforced at those relays too.)

## Verified conformance read (2026-07-29, against `main@e492668b`)

A repository-wide search across `backend/**/*.py`, `*.json` and `*.js` plus all four client trees returns **zero occurrences of every wire construct searched**. The table below is chosen to cover all nine major changes and the wire-visible minor ones; it uses the specification's own method names rather than SDK identifiers.

| Construct | This revision | Occurrences |
|---|---|---|
| `Mcp-Session-Id` | removed | 0 |
| `initialize`, `notifications/initialized` | removed (stateless core) | 0 |
| `protocolVersion` / `protocol_version` (as a wire field) | moved into `_meta`, required per request | 0 |
| `clientCapabilities`, `clientInfo`, `serverInfo` | new required/recommended `_meta` keys | 0 |
| `server/discover` | **servers MUST implement** | 0 |
| `subscriptions/listen`, `resources/subscribe`, `resources/unsubscribe` | replaced | 0 |
| `resultType` / `result_type` | **required on all results** | 0 |
| `Last-Event-ID` | removed (no SSE resumability) | 0 |
| `logging/setLevel`, `ping`, `notifications/roots/list_changed` | **removed outright** | 0 |
| `roots/list`, `sampling/createMessage` | deprecated capabilities | 0 |
| `notifications/elicitation/complete`, `elicitationId` | removed (introduced in `2025-11-25`) | 0 |
| `Mcp-Method`, `Mcp-Name` | newly required headers | 0 |
| `x-mcp-header`, `Mcp-Param-` | new schema annotation (optional for servers, MUST for clients) and its derived headers | 0 |
| `ttlMs`, `cacheScope` | newly required on list/read results | 0 |
| `inputSchema` (camelCase), `structuredContent` | loosened to JSON Schema 2020-12 | 0 |
| `tasks/get`, `tasks/list` | moved to extension / removed | 0 |
| `_meta`, `extensions` (as wire fields on an MCP frame) | reserved / new | 0 as wire keys (`_meta` occurs 7× as an unrelated Python local identifier) |

The reason is structural. AstralDeep carries MCP as a **message shape** — the `MCPRequest`/`MCPResponse` dataclasses in [`backend/shared/protocol.py:149`](../../backend/shared/protocol.py#L149)–`185` — over its **own** A2A WebSocket transport, since feature 040 over an in-process `LoopbackSocket`, and since feature 058 over a `TunnelSocket`. There is no MCP HTTP transport, no session handshake to remove, no MCP server/client role, and no JSON-RPC 2.0 framing anywhere on the MCP path (the single `"jsonrpc": "2.0"` literal in the tree, [`orchestrator.py:14455`](../../backend/orchestrator/orchestrator.py#L14455), is A2A `message/send` carrying the MCP method name as opaque data). Tool schemas are snake_case `AgentSkill.input_schema` dicts; camelCase `inputSchema` never appears on Astral's wire.

**Consequence: nothing breaks on 2026-07-28, and no deadline exists.** The `logging/setLevel` RPC is removed outright; the Logging *capability*, Roots, and Sampling are separately **deprecated**, with earliest removal in the first revision released on or after 2027-07-28. All are moot because all are unused.

### Corrections carried by this read

- **`server/discover` is a server MUST and a client MAY.** Servers MUST implement it. On **stdio** a dual-era client SHOULD send it first as the era probe; on **Streamable HTTP** — the transport Phase B builds — the era probe is instead a modern request whose `400 Bad Request` body is inspected for a recognized modern JSON-RPC error. It is *not* a handshake: modern MCP is per-request metadata. Phase B's HTTP era detection therefore depends on the endpoint emitting recognizable modern 400 bodies (FR-023, FR-024).
- **JSON Schema 2020-12 (SEP-2106) loosens the keyword set *and adds constraints*.** The RC note called it "the one bounded, concrete piece of work" and the first pass of this read called it a pure no-op. Both are wrong. **New in this revision (SEP-2106):** it permits any 2020-12 keyword and any JSON value in `structuredContent`, **and** it adds `$ref` resolution requirements and composition-keyword resource bounds — implementations MUST NOT automatically dereference a `$ref` resolving to a network URI; an opt-in fetch mode MUST be disabled by default and SHOULD enforce a host allowlist rejecting loopback, link-local and private addresses; a schema failing on an unresolved external `$ref` SHOULD be rejected rather than treated as permissive; and implementations SHOULD bound schema depth, subschema count, or validation time against DoS. **Pre-existing since `2025-11-25` and unchanged** (binding on Astral, but not new): implementations MUST validate against the declared or default dialect and error on an unsupported one, and an absent `$schema` defaults to 2020-12. Net: it is an SSRF-and-DoS surface, not a permission grant.
- **Extensions are versioned *independently of the core revision*, not with it.** The authorization page's extension section says they are "Versioned independently — Extensions follow the core MCP versioning cycle but may adopt independent versioning as needed" (that sentence is scoped to authorization extensions), and the extensions overview says they "evolve independently of the core protocol" and directs authors to prefer capability flags or settings-object versioning, taking a new identifier only for a breaking change that cannot be absorbed that way. There is therefore no "2026-07-28 Tasks" or "2026-07-28 Apps": Tasks is now presented as an experimental extension while its normative document remains on its independent `draft` track, and MCP Apps' published release is `2026-01-26`, carrying its own `protocolVersion` axis.
- **The MCP Apps extension identifier is `io.modelcontextprotocol/ui`**, not `…/apps`.
- **Skills over MCP is not published.** It is a Working Group (converted from an Interest Group 2026-02-01, WG since 2026-04-16) whose current direction, SEP-2640 (Skills Extension, Resources-based, Extensions Track, a `skill://` resource convention), is status **In Review** with no target date. Nothing to conform to. Watch item only.
- **The Tasks draft collides semantically with the core error-code assignments.** It specifies `-32003` for "Missing Required Client Capability" where core `2026-07-28` assigns `-32021`. Core leaves `-32000..-32019` implementation-defined; Astral adopts the stricter house rule of allocating none there so extension drift cannot silently acquire conflicting meanings.

## Adoption verdicts

| Item | Verdict | One-line reason |
|---|---|---|
| Core protocol (all 9 major changes) | **NO-OP today; in scope for Phase B** | Astral speaks no MCP wire protocol; zero occurrences of every construct searched. Phase B answers all three message patterns, one of them (subscribe-and-notify) minimally — see D6 |
| MCP Apps (`io.modelcontextprotocol/ui`) | **DIVERGE — deliberately** | Apps ships sandboxed HTML; Astral ships a declarative vocabulary rendered per target. The iframe is a hard reach ceiling for 4 of 5 clients and both non-visual targets |
| Tasks (`io.modelcontextprotocol/tasks`) | **DIVERGE, and cite the convergence** | 063's `tracked_job` independently converged on the same design and is stronger where it matters — `unconfirmed`, `orphaned`, and enforcement-by-schema |
| Authorization hardening | **CONFORMANT as a non-participant; NEW work in Phase B** | Every new authorization requirement binds the *client* or the *authorization server*. A server's own obligations are enumerated below and Phase B must build all of them |
| JSON Schema 2020-12 | **NO-OP for existing schemas; NEW work in Phase A** | The keyword change is free, but SEP-2106's `$ref` and DoS constraints are real and Astral accepts BYO-authored schemas |
| Skills over MCP | **NO VERDICT — watch item** | Not published; SEP-2640 In Review |

### Verdict 1 — MCP Apps: DIVERGE

MCP Apps and AstralDeep's SDUI pipeline answer the **same question** — how does a server put interactive UI into a chat? — with **opposite mechanisms**.

| | MCP Apps | AstralDeep SDUI |
|---|---|---|
| What the server emits | An HTML page bundled with its JS and CSS, at a `ui://` resource | A declarative component tree drawn from a closed 35-type vocabulary (`allowed_primitive_types()`, verified by execution) |
| Who renders | The host — web hosts *typically* into a sandboxed iframe under a declared CSP | The orchestrator, per target (3 registered: `web`, `voice`, `aom`), then ROTE adapts |
| Back-channel | `postMessage` JSON-RPC, its own `ui/*` dialect | Ordinary `ui_event` / `ui_render` / `ui_upsert` frames, validated against a committed 94-action manifest |
| Security model | **Contain** untrusted code — sandbox, CSP, host-granted capabilities | **Admit no code** — `esc()` at 64 sites, a hard attribute allowlist, 7 non-interactive ARIA roles, escape-before-format markdown |
| Reach | Anywhere there is a browser engine | Web HTML, PySide6/Qt, Jetpack Compose, SwiftUI (iOS/macOS/watchOS), SSML voice, and an accessibility-object-model tree |
| Accessibility | Whatever the app author wrote into their HTML | Guaranteed once, centrally, in the renderer; `aom` is a first-class target |

**Verdict: diverge, deliberately.** Three reasons, in order of weight:

1. **The iframe is a hard reach ceiling for four of five clients and both non-visual targets.** Windows (PySide6 Qt Widgets, 33 native types, PyInstaller-frozen) would need QtWebEngine it does not ship; Android (Compose, 33 types) would need a WebView-per-component with its own JS bridge; Apple iOS/macOS (SwiftUI, 33 of 35) would need WKWebView plus a message handler per component; **watchOS (10 native types, everything else text-falls-back) cannot ever**. The `voice` target speaks SSML and the `aom` target emits a navigable role/name/state tree — both consume *structure*, so an opaque HTML document is silence on one and an unnamed leaf on the other. ROTE's own honest outcome for an unrenderable type is the fallback ladder down to `text`, which is what it already does for `audio` and `generative` on all four native clients.
2. **The security models are not variants of one idea, they are opposites.** Apps assumes the server's code is untrusted and invests in containing it. Astral never admits code into the render path — [`webrender/renderer.py`](../../backend/webrender/renderer.py) refuses `onclick`/`style`/`src`/`href`/`class` by design, and `_scrub_plotly_html` ([`renderer.py:659`](../../backend/webrender/renderer.py#L659)) actively strips `<iframe>` from the one agent-supplied HTML-bearing field. **No `<iframe>`, `srcdoc` or `postMessage` is ever emitted by the render path.** The only first-party occurrences of the token are strip-or-refuse code — the Plotly scrub list, the web client's unsafe-element guard, and the summarizer/web_research HTML tag-strip lists — plus one client-side top-frame check for the auth-renew flow. `srcdoc` and `postMessage` occur **zero** times first-party. Bridging would import the threat model Astral was built to avoid — the same reasoning that made feature 058's BYO validator pure-AST.
3. **It is evidence *for* the thesis, not against it.** An official MCP extension arriving in this space confirms the problem is real and general. Astral's own counter-design for the same goal already exists and is deliberately not code: [`webrender/generative.py`](../../backend/webrender/generative.py) lets the model compose a *novel* widget as a typed, bounded (120 nodes / depth 6), post-validated grammar of 3 containers and 7 leaves with escape-by-default and fixed CSS classes — shipped as one of the 35 primitive types rather than as an escape from them.

**What this obligates:** nothing in code. It obligates the thesis to *name* MCP Apps as the closest standardized alternative and state the trade honestly — Apps buys unbounded expressiveness inside one sandbox on one class of client; Astral buys cross-client and cross-modality reach and central accessibility, at the cost of rendering only what its vocabulary can say.

**Watch item:** a future MCP Apps revision adding a declarative, non-HTML rendering mode would collapse the mechanism gap and turn this from *diverge* into *bridge*. Note the draft has moved the *other* way — apps may now serve `tools/list`/`tools/call` back to the host, making them bidirectional MCP peers.

### Verdict 2 — Tasks: DIVERGE, and cite the convergence

The Tasks extension and feature 063's `tracked_job` were designed independently, weeks apart, and arrived at nearly the same design:

| MCP Tasks | AstralDeep 063 (`orchestrator/remote_jobs.py`, `tracked_job`) |
|---|---|
| `CreateTaskResult` with `taskId`; server MUST NOT return it until the task is durably created | Row INSERT is *attempted* before `run_job`/`submit_job` returns ([`remote_control/mcp_tools.py:386`](../../backend/agents/remote_control/mcp_tools.py#L386), `:469`) — see the gap below |
| `working` / `input_required` / `completed` / `failed` / `cancelled` | Slurm-derived states + an 11-member `TERMINAL_STATES` classifier, plus a structured `unconfirmed` verdict |
| `tasks/get` polling, `pollIntervalMs` | `_remote_job_poll_loop`, 30 s, read-only, host-key-pinned |
| Task ID survives client disconnect/restart | Survives page reload *and* orchestrator restart; the first poll pass **is** boot reconciliation |
| `input_required` → answered via `tasks/update` | 063 destructive-confirmation proposals |
| `notifications/tasks` | `notify_on_finish` → `notification` frame |

> **Known gap, stated rather than smoothed over.** Astral does **not** currently meet the durability rule in row 1. Both call sites wrap the INSERT in `except Exception: pass` and then unconditionally return a job card reporting `tracked: true`, so a failed INSERT hands the caller a job id while no durable row exists. This is the failure mode the MCP requirement forbids, it is a real defect, and it belongs in the follow-ups register rather than in a table cell claiming conformance. It is **out of scope for this feature** (it is 063 code, not MCP code) and is recorded here so the convergence is not overclaimed.

**Verdict: keep the in-house vocabulary, and document the divergence as a result.** The strongest divergences are the two states MCP has no target for, plus a difference in enforcement:

- **`unconfirmed` has no MCP target.** It is produced when a consequential (non-retryable) call times out, or when `sbatch` succeeds but returns no parsable id. In both paths **no `tracked_job` row is written**, so a retry cannot duplicate. Mapping it to `failed` invites the duplicate submission the `--comment=astral:<nonce>` marker exists to prevent; mapping it to `working` claims a job that may not exist.
- **`orphaned` has no MCP target either.** It is reached two ways: the machine or credential is gone ([`remote_jobs.py:178`](../../backend/orchestrator/remote_jobs.py#L178)), or the machine was unreachable for eight consecutive poll passes (`_FAIL_CEILING`, [`:32`](../../backend/orchestrator/remote_jobs.py#L32)/`:265`). In both cases the job may still be running, and neither `failed` nor `cancelled` nor `working` says "I have stopped being able to observe a job I believe is still running."
- **The consent properties are the same set; the enforcement differs.** MCP specifies them as SHOULDs on stateless `requestState`: bind the authenticated principal, a short TTL, and "an identifier for the originating request, e.g. the method name and a digest of its salient parameters" — an argument fingerprint — with integrity protection a MUST wherever that state influences authorization, resource access, or business logic (HMAC or AEAD) — which is exactly the consent case. 063 makes the same properties a **durable row** with a five-state `CHECK`, a 15-minute absolute TTL, owner binding at consumption *and* decision, sha256 argument-fingerprint binding, single-use via a guarded `UPDATE … RETURNING`, and hash-chain audit across eight action types — schema-enforced MUSTs rather than SHOULDs left to the server author. Core MRTR is candid that its anti-replay measures "do not by themselves guarantee single-use", and that a server needing at-most-once consumption "MUST enforce that invariant server-side"; 063 is that enforcement. (Note the Tasks draft calls task `inputRequests` "a distinct mechanism" from MRTR, and `requestState` occurs zero times in it — the two must not be conflated.)
- **The unattended refusal has no MCP analogue at all.** MCP has no notion of "this operation's continuation requires a live human principal on an interactive channel", which is what 063 enforces *before any transport contact*.

### Verdict 3 — Authorization: conformant as a non-participant; real work in Phase B

Reading the full authorization tree confirms it is **overwhelmingly client- and AS-binding**. RFC 9207 `iss` validation, `application_type` in DCR, credential-to-issuer binding, and Client ID Metadata Documents all bind an MCP *client* or an *authorization server*. Astral is neither, and does not use DCR at all — its Keycloak clients are statically provisioned. The "at least one of two metadata discovery mechanisms" rule likewise binds the **authorization server**, not the MCP server. **No action for the decision.**

An MCP *server*'s own obligations, none of them new in this revision, are:

1. Publish RFC 9728 Protected Resource Metadata naming at least one authorization server (MUST). *(The "at least one of two metadata discovery mechanisms" rule binds the **authorization server**, not this one — it is a deployment prerequisite on the named Keycloak realm, recorded under Assumptions.)*
2. Validate access tokens per OAuth 2.1 and validate that the token was issued for *this* resource as audience per RFC 8707 (MUST); accept no other token and transit none (MUST).
3. Never forward the received token to an upstream API (MUST).
4. Return 401 / 403 / 400 appropriately (MUST), and account for scope hierarchies when deciding sufficiency (MUST).
5. Emit a `WWW-Authenticate` challenge naming the resource metadata and all required scopes for the operation in a single response (SHOULD).
6. Exclude `offline_access` from the challenge and from `scopes_supported` (SHOULD) — refresh tokens are not a resource requirement.

Phase B must build all six (FR-038..FR-046, which add two Astral-specific constraints: FR-040's isolation of the new validator, and FR-046's prohibition on widening revocation authority). The load-bearing one is audience validation: all three of Astral's current JWT validators disable it (`verify_aud: False`) and substitute an `azp` check — an allow-list in [`auth.py:197`](../../backend/orchestrator/auth.py#L197) and [`orchestrator.py:18037`](../../backend/orchestrator/orchestrator.py#L18037), a hardcoded two-value comparison in [`a2a_security.py:88`](../../backend/shared/a2a_security.py#L88) — and **all three skip that check entirely when the token omits `azp`** ([`shared/auth_clients.py:50`](../../backend/shared/auth_clients.py#L50)). That is weaker than an allow-list and is the opposite of MCP's core anti-confused-deputy rule, which is why FR-040 puts audience validation on a *new* dependency rather than tightening these.

### Verdict 4 — JSON Schema 2020-12: no-op for existing schemas, new work for the constraints

The keyword vocabulary in use across every agent — `type`, `properties`, `required`, `default`, `items`, `minimum`, `maximum`, `enum`, `additionalProperties`, `const`, `oneOf`, `minItems`, `maxItems` — is entirely inside 2020-12, with **zero** `$ref`, `$defs`, or `prefixItems` to reconcile. Declaring the dialect is therefore free for the schemas themselves, and is optional besides (an absent `$schema` defaults to 2020-12).

What is *not* free is SEP-2106's added constraints, and they matter here specifically because Astral accepts **BYO-authored** tool schemas: the `$ref` network-dereference prohibition and the recommended validation bounds are exactly the untrusted-schema case (FR-017). The one further conflict is behavioral: `_sanitize_tool_schema` ([`orchestrator.py:16713`](../../backend/orchestrator/orchestrator.py#L16713)–`16751`) silently **rewrites** agent-supplied schemas to satisfy the OpenAI function grammar, and a declared dialect demands validate-and-reject at registration instead of silent repair (FR-015).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The MCP envelope survives a version-skewed peer (Priority: P1)

An operator upgrades the orchestrator while a networked agent, a BYO desktop agent, or an external A2A agent is still running the previous build. The two ends now disagree about which fields the MCP envelope carries. Today that mismatch is invisible and fatal wherever `Message.from_json` parses the frame: the receiver raises `TypeError` on an unknown key, logs it, drops the frame, never resolves the pending future, and the user watches a tool call hang for 30 seconds before being told it "timed out."

**Why this priority**: It is a live defect on `main` today, independent of MCP, and it is the structural prerequisite for every other change in this feature. Nothing else in Phase A can ship safely until an additive field is survivable.

**Independent Test**: Run a current-build orchestrator against a peer emitting an MCP frame carrying an unknown key; the call completes normally and the unknown key is ignored, with no timeout and no dropped frame.

**Acceptance Scenarios**:

1. **Given** an `mcp_response` frame carrying a field the receiving build does not know, **When** it is parsed, **Then** the known fields populate the envelope, the unknown field is discarded, and the pending request resolves normally.
2. **Given** an `mcp_request` frame carrying an unknown field arriving at a networked WebSocket agent, **When** it is parsed, **Then** the tool executes and returns a normal response.
3. **Given** an MCP frame that is malformed rather than merely unfamiliar, **When** it is parsed, **Then** it is rejected with a logged protocol error and the caller is failed promptly rather than left to time out.
4. **Given** a fenced personal-agent response carrying a newly added envelope field, **When** the strict field allowlist validates it, **Then** the field is accepted because it is on the allowlist — and any field genuinely outside the allowlist is still refused fail-closed.

### User Story 2 - Astral's MCP dialect honestly claims `2026-07-28` (Priority: P2)

A reviewer, an auditor, or a thesis committee member asks what revision of MCP AstralDeep implements. Today the honest answer is "a message shape loosely derived from an older revision, with no version marker anywhere on the wire." After this story the envelope states its protocol version and the caller's declared capabilities on every request, identifies both peers, discriminates a completed result from one needing more input, and uses error codes drawn from the partition the specification reserves.

**Why this priority**: It is the substance of "upgrade to the latest version of MCP" for the transport Astral actually speaks, and it removes a real ambiguity — that a failed call currently still delivers renderable components with no discriminator saying so.

**Independent Test**: Capture the MCP frames of one tool call on each of the three internal transports; every request carries a protocol version and the caller's declared capabilities, every response carries a result-type discriminator and responder identity.

**Acceptance Scenarios**:

1. **Given** any tool dispatch over the in-process, WebSocket, or tunnel transport, **When** the request is emitted, **Then** it carries the protocol version `2026-07-28` and the caller's declared capabilities, both of which the specification marks required.
2. **Given** a tool that completes normally, **When** the response is emitted, **Then** its result-type is `complete` and it carries no error.
3. **Given** a tool that fails, **When** the response is emitted, **Then** it carries an error, its result-type is not `complete`, and it does not also deliver renderable components as though it had succeeded.
4. **Given** a peer declaring a protocol version this build does not support, **When** the request is processed, **Then** it is refused with the specification's unsupported-version error naming the versions this build does support — never silently processed under assumed semantics.
5. **Given** an agent registering a tool schema, **When** the schema is validated against the declared or default dialect, **Then** a violating schema is **refused at registration with a named reason**, rather than silently rewritten.
6. **Given** a tool schema containing a `$ref` that resolves to a network URI, **When** it is validated, **Then** the reference is never fetched and the schema is refused rather than treated as permissive.

### User Story 3 - A third-party MCP host can discover and list Astral's tools (Priority: P3)

A user who already runs an MCP-capable host points it at their AstralDeep deployment. The host discovers the server, negotiates a protocol version, authenticates the user, and sees exactly the tools **that user** is authorized to run — not the full catalog.

**Why this priority**: It is the first half of Phase B and the point at which the revision becomes live. It is independently valuable — discovery and listing without invocation is a real, safe capability — and it is the natural place to prove the transport, headers, and authorization before any tool executes.

**Independent Test**: With the flag on, point a real third-party MCP host at the deployment; it completes discovery, authenticates, and lists a tool set that changes when the user's agent permissions change.

**Acceptance Scenarios**:

1. **Given** an unauthenticated request to the MCP endpoint, **When** it is received, **Then** the response is 401 carrying a challenge that names where to find the protected-resource metadata and which scopes are required.
2. **Given** a valid token issued for a *different* audience, **When** it is presented to the MCP endpoint, **Then** it is refused — a token minted for the web client is never accepted here.
3. **Given** an authenticated host calling discovery, **When** the server responds, **Then** it advertises the protocol versions it supports, its capabilities, its identity, and a cache lifetime and scope.
4. **Given** two users with different agent permissions, **When** each lists tools, **Then** each sees only their own authorized set, the response carries both required cache fields, and its scope is private rather than shareable.
5. **Given** a request whose protocol-version header disagrees with its body, or which omits a required header, **When** it is received, **Then** it is refused with the specification's header-mismatch error and HTTP 400.
6. **Given** a request omitting a required metadata field, or requiring a capability the caller never declared, **When** it is received, **Then** it is refused with the specification's respective error code and HTTP 400 — the bodies a dual-era client inspects to recognize a modern server.
7. **Given** a request using an HTTP method this revision removed, **When** it is received, **Then** it is refused with 405 rather than served.

### User Story 4 - A third-party MCP host can invoke a tool and render its result (Priority: P3)

Having listed tools, the host calls one. The result comes back as MCP content the host can display, and every gate a chat dispatch would face is faced here too.

**Why this priority**: It is the payoff of Phase B, and it is where the SDUI architecture is tested against a foreign host. It depends on US3 and must not ship before it.

**Independent Test**: Invoke a read-only tool from a real MCP host; the result renders in that host, and the same invocation with the user's permission for that agent revoked is refused.

**Acceptance Scenarios**:

1. **Given** an authorized user invoking a permitted read-only tool, **When** it executes, **Then** the host receives MCP content blocks rendered from the same component tree the web and native clients receive, plus the machine-readable data as structured content.
2. **Given** a user whose permission for that agent has been revoked, **When** they invoke its tool, **Then** the call is refused, and the refusal is the decision of the same permission gate the chat path uses — not a separate copy of it.
3. **Given** an MCP invocation of a **destructive** verb, **When** it is dispatched, **Then** it is refused before any transport contact with a message naming the reason, because an MCP request carries no live human on an interactive channel.
4. **Given** a tool that raises, **When** the failure is reported, **Then** it is reported as a tool-level error inside a completed result, not as a protocol error.
5. **Given** a host that closes its response stream mid-invocation, **When** the server observes the disconnect, **Then** it treats the close as cancellation, stops work as soon as practical, sends nothing further for that request, and releases the admission slot.
6. **Given** any MCP invocation, **When** it completes, **Then** it appears in the audit trail with the same event class and correlation as a chat dispatch, distinguishable by its channel.

### User Story 5 - The endpoint declares honestly what it does not support (Priority: P3)

A host that supports the Tasks extension, subscriptions, or MCP Apps connects. Rather than half-implementing them, the server says plainly that it does not support them, and the host degrades gracefully.

**Why this priority**: An over-claiming capability advertisement is worse than an absent one — it produces silent hangs on the host side. This is small, and it is what makes D5/D6 safe to ship.

**Independent Test**: A host requesting an unsupported extension or notification type receives an explicit non-advertisement or a specification-defined error, never a stall.

**Acceptance Scenarios**:

1. **Given** discovery, **When** capabilities are advertised, **Then** no unsupported extension is listed and list-changed notification support is advertised as absent.
2. **Given** a host calling a method the server does not implement, **When** it is received, **Then** it is refused with HTTP 404 and the method-not-found error, per the transport rules.
3. **Given** a host that declared the Tasks extension, **When** it invokes a tool, **Then** it never receives a task handle, because the server did not advertise that extension.
4. **Given** a long-running invocation, **When** it is dispatched over MCP, **Then** the host is told the operation is tracked in Astral and given its identifier, rather than being handed a task-shaped result the server cannot maintain.

### Edge Cases

- A BYO agent bundle already delivered to a user's desktop carries a **frozen copy** of the old dispatch code and will never be rebuilt on its own; FR-004 governs whether it keeps working or is marked as requiring revalidation.
- Draft and attachment-parser agents generated by the orchestrator's own codegen template carry a **second** frozen dispatch implementation, and unlike the BYO bundle that one imports the shared dataclasses (FR-003).
- The fenced personal-agent path validates responses against an exact eight-name allowlist and compares the fence dict by **exact equality**, so any envelope addition fails it closed until FR-005 lands.
- Six of the ten per-agent servers splat `tool_fn(**arguments)` with no signature filter, so anything added inside the arguments map becomes a `TypeError` classified as non-retryable — which is why FR-008 forbids that placement.
- `AgentCard.to_dict` hand-builds a fixed key list, so a new `AgentSkill` field is **silently dropped** on serialization; `AgentCard.from_dict` splats, so an unknown card key raises (FR-006).
- The A2A card bridge already discards `input_schema`, `output_schema`, and `metadata` in both directions, so anything added to the card does not survive an A2A round trip; A2A card fidelity is **out of scope** for this feature and is unchanged by it.
- A conformant host caches the tool listing for its advertised lifetime, so an admin revoking an agent's safe marking leaves that cache stale; FR-033 bounds the staleness and FR-047 makes the refusal happen at invocation regardless.
- A tool name or resource URI containing non-ASCII characters must be carried in an HTTP header (FR-022).
- An MCP host on a different origin makes a browser-based request; widening CORS with credentials enabled would expose every existing cookie-authenticated route (FR-031).
- The endpoint is a new public POST surface in a codebase with no general rate limiter and no global request-body cap (FR-028).
- A destructive verb reached through a *chain* — an MCP-invoked tool that hops to another agent — must hit the same refusal as a direct call (FR-048).
- An MCP caller invokes a tool whose result is a component the workspace would normally upsert into a chat, and there is no chat.

## Requirements *(mandatory)*

### Functional Requirements

#### Phase A — envelope tolerance and correctness (US1)

- **FR-001**: The MCP request and response envelopes MUST ignore unknown fields when parsing, discarding them and populating known fields, so that a peer on a different build cannot cause a dropped frame.
- **FR-002**: A malformed MCP frame MUST fail the pending request promptly with a logged protocol error, rather than being swallowed and left to time out.
- **FR-003**: Every place that constructs or parses an MCP frame MUST be updated in the same change, and the set MUST be enumerated in the feature's contracts. It comprises **five independent implementations of the dispatch contract** — the ten `agents/*/mcp_server.py` servers, the orchestrator's draft/attachment-parser codegen template, the generated BYO bundle runner, the Windows desktop client, and the A2A executor bridge — plus the shared envelope definitions they depend on, the `base_agent`/`local_transport`/`a2a_bridge` carriers, and the orchestrator's own synthetic response-construction sites. Two of the five are *generated* and differ: the draft/parser template imports the shared dataclasses and inherits the parse defect; the BYO bundle runner parses plain dicts and does not.
- **FR-004**: Already-delivered BYO agent bundles and already-generated draft agents MUST continue to function unchanged after Phase A, or MUST be marked as requiring revalidation through the existing mechanism; a delivered bundle MUST NOT fail silently.
- **FR-005**: The strict field allowlist and fence-equality check on the fenced personal-agent path MUST be updated in the same change as any envelope addition, so that path never fails closed on a field the orchestrator itself emits.
- **FR-006**: `AgentCard` serialization MUST NOT silently drop a newly added skill field, and card deserialization MUST tolerate unknown keys.

#### Phase A — protocol metadata, result type, error codes, schema dialect (US2)

- **FR-007**: Every MCP request MUST carry, on the envelope, the protocol version and the caller's declared capabilities — both of which the specification marks REQUIRED — and SHOULD carry caller identity, which the specification marks optional. Every MCP response SHOULD carry responder identity. All four MUST use the specification's reserved metadata key names.
- **FR-008**: Protocol metadata MUST be carried on the envelope itself and MUST NOT be placed inside the tool arguments map, because six of the ten agent servers pass that map to the tool function unfiltered.
- **FR-009**: Every MCP response MUST carry a result-type discriminator, defaulting to the specification's completed value.
- **FR-010**: A response reporting an error MUST NOT also deliver renderable components as though the call had succeeded; the paths that currently emit both MUST be resolved to a single, unambiguous outcome.
- **FR-011**: A request declaring a protocol version this build does not support MUST be refused with the specification's unsupported-version error carrying the list of supported versions, and MUST NOT be processed under assumed semantics. A request omitting a metadata field the specification marks required MUST likewise be refused with the specification's invalid-params semantics rather than processed under assumed defaults — the internal-transport analogue of FR-023.
- **FR-012**: The system MUST NOT allocate error codes in the range the specification reserves for implementation-defined legacy use, and MUST use specification-defined codes only with their specified meanings.
- **FR-013**: Existing routing behavior MUST continue to key on the retryability flag rather than on numeric error codes, and the three coexisting error-code namespaces MUST be documented with the reconciliation point named.
- **FR-014**: Tool input schemas MAY declare the JSON Schema 2020-12 dialect; an absent declaration MUST be treated as that dialect, and a declared dialect the system does not support MUST produce an error naming it rather than a silent fallback.
- **FR-015**: A tool schema that fails validation against its declared or default dialect MUST be refused at agent registration with a message naming the tool and the reason, replacing the current silent rewrite; the rewrite that adapts a schema to the model-facing function grammar MUST be applied downstream of validation, never in place of it.
- **FR-016**: `AgentSkill.output_schema` MUST be populated at agent registration where the tool's result shape is derivable, and MUST be set explicitly to a null value where it is not; the system MUST NOT ship a declared field that nothing ever writes.
- **FR-017**: Schema validation MUST NOT dereference a `$ref` that resolves to a network URI, MUST refuse a schema that cannot be validated without one rather than treating it as permissive, and MUST bound validation cost by schema depth, subschema count, or time budget so that a hostile schema cannot act as a denial-of-service vector.
- **FR-018**: Phase A MUST NOT change any client-facing frame, action, or component type, and MUST leave the published client wire manifest byte-identical.

#### Phase B — endpoint, transport, and headers (US3)

- **FR-019**: The MCP server endpoint MUST be gated by a feature flag defaulting to off, read once at process start; with the flag off the route, its metadata document, and every advertisement of it MUST be absent, and behavior MUST be byte-identical to a build without the feature.
- **FR-020**: The system MUST expose a single HTTP endpoint path accepting POST, and MUST answer the HTTP methods this revision removed with the specification's method-not-allowed status.
- **FR-021**: Every request MUST carry the protocol-version, method, and (where the specification requires it) name headers; a missing header, or a header whose value disagrees with the message body after decoding, MUST be refused with the specification's header-mismatch error and HTTP 400.
- **FR-022**: A name header value that cannot be represented as plain ASCII MUST be accepted in the specification's base64 sentinel encoding and decoded before comparison with the body.
- **FR-023**: A request omitting a metadata field the specification marks required MUST be refused with the specification's invalid-params error and HTTP 400.
- **FR-024**: A request whose processing would require a capability the caller did not declare MUST be refused with the specification's missing-capability error, carrying the list of missing capabilities in its error data, and HTTP 400.
- **FR-025**: The system MUST validate the request origin and MUST refuse a present-but-invalid origin with HTTP 403.
- **FR-026**: A request for a method the system does not implement MUST be refused with HTTP 404 and the method-not-found error.
- **FR-027**: The system MUST ignore the removed session and stream-resumption headers and MUST NOT mint or echo a session identifier.
- **FR-028**: A request body exceeding the documented size limit MUST be refused, and the endpoint MUST be registered with the existing durable work-admission mechanism under its own admission class with the documented concurrency and queue limits.
- **FR-029**: A closed response stream MUST be treated as cancellation of that request; the system MUST stop work as soon as practical, MUST NOT emit any further message for it, and MUST release its admission slot.
- **FR-030**: The endpoint MUST accept credentials only in the authorization header; it MUST NOT accept the browser session cookie and MUST NOT accept a token in the query string.
- **FR-031**: Cross-origin policy for the MCP path MUST be separate from the existing application policy and MUST NOT enable credentialed cross-origin access to existing routes.

#### Phase B — discovery, listing, and capability honesty (US3, US5)

- **FR-032**: The system MUST implement the discovery RPC, advertising the protocol versions it supports, its capabilities, its identity, natural-language guidance for a model, and a cache lifetime and scope.
- **FR-033**: The tool listing MUST return the requesting user's authorized tool set as resolved by the same permission gate the chat path uses, MUST NOT return the full catalog, and MUST carry both cache fields the specification requires — a documented freshness lifetime, and a scope of private because the set is per-user.
- **FR-034**: Each listed tool MUST carry its input schema in the specification's field naming, and MUST carry read-only and destructive hints derived from the existing single-source destructive classification.
- **FR-035**: The authorization scope that governs a tool MUST be carried without being presented as a protocol-level authorization artifact; a scope carried in metadata MUST NOT be treated by the system as sufficient authorization.
- **FR-036**: The system MUST NOT advertise support for any extension it does not implement, and MUST NOT advertise list-changed notification support while no change-notification mechanism exists. It MUST nonetheless **accept** the subscription-listen request and acknowledge it with an **empty agreed notification set** rather than refusing the method — so the specification's subscribe-and-notify message pattern is answered rather than silently unimplemented — and MUST emit no notification on that subscription.
- **FR-037**: The system MUST refuse a task-shaped result type, and MUST NOT return a task handle to any caller.

#### Phase B — authorization (US3)

- **FR-038**: The system MUST publish protected-resource metadata naming at least one authorization server, served at the specification's well-known location and referenced from the authentication challenge.
- **FR-039**: The system MUST validate that a presented token was issued for the MCP endpoint's own audience and MUST refuse a token issued for any other audience, including tokens minted for the existing web and native clients.
- **FR-040**: Token validation for the MCP endpoint MUST be a distinct path from the existing bearer dependency, so that enabling audience validation here changes no existing route's behavior.
- **FR-041**: The system MUST validate the token issuer against the configured identity provider.
- **FR-042**: An unauthenticated or invalid-token request MUST receive HTTP 401 with a challenge naming the resource metadata location and the required scopes; a valid token lacking required scope MUST receive HTTP 403 with an insufficient-scope challenge naming all scopes needed for the operation in a single response.
- **FR-043**: The system MUST account for scope hierarchies — where a broader scope implies narrower ones — when deciding whether a token is sufficient for an operation.
- **FR-044**: The system MUST NOT advertise the offline-access scope in the authentication challenge or in the published protected-resource metadata.
- **FR-045**: The system MUST NOT forward a token it received at the MCP endpoint to any upstream service; any upstream call MUST use a separately minted credential.
- **FR-046**: Enabling the endpoint MUST NOT widen which client identifiers may drive refresh-token revocation.

#### Phase B — invocation, gating, and result projection (US4, US5)

- **FR-047**: A tool invoked over MCP MUST pass through the same authorization and gate stack as a chat dispatch of the same tool, and MUST NOT use a separate or reduced copy of it.
- **FR-048**: A destructive verb invoked over MCP MUST be refused before any transport contact, with a message naming the reason, and the refusal MUST hold on chained and parallel dispatch paths as well as direct invocation.
- **FR-049**: A tool result MUST be projected into MCP content by a render target registered through the existing renderer registry seam, so that adding MCP as a consumer requires no change to the primitive definitions and no change to any agent.
- **FR-050**: The machine-readable portion of a tool result MUST be returned as structured content alongside the rendered content.
- **FR-051**: A tool that fails MUST be reported as a tool-level error within a completed result, not as a protocol-level error.
- **FR-052**: A tool that starts a tracked long-running operation MUST return that operation's identifier and its Astral-side status, and MUST NOT present it as a protocol task.
- **FR-053**: Every MCP invocation MUST emit the same audit events as the equivalent chat dispatch, distinguishable by an invocation-channel attribute.

#### Rollout, testing, and reversibility

- **FR-054**: Phase A MUST be independently shippable and MUST NOT require any part of Phase B; no Phase A code path may reference a Phase B module.
- **FR-055**: After the Phase B flag has been enabled and then disabled, the system MUST retain no persisted state — cached metadata document, admission-class registration, or advertisement record — that changes behavior relative to a deployment where the flag was never on. FR-019 governs the never-enabled case.
- **FR-056**: The feature MUST include an adversarial test suite covering, at minimum, these seven categories: unknown-field tolerance on every envelope path, foreign-audience token refusal, header-body mismatch, origin refusal, cross-user tool-list isolation, destructive-verb refusal on direct and chained paths, and revoked-permission refusal. A category with zero cases MUST fail the suite.
- **FR-057**: The feature MUST include a drift-guard test asserting that the set of tools advertised over MCP equals the set the chat path would authorize for the same user.
- **FR-058**: Phase B MUST be validated end-to-end against a real third-party MCP host on a staging deployment of the candidate image before merge; anything not so validated MUST be labelled code-shaped and unproven.
- **FR-059**: The feature MUST ship an operator document covering enablement, the required identity-provider configuration, the audience and scope model, the honest statement of what is not supported, and the rollback path.
- **FR-060**: The feature MUST introduce no new third-party runtime dependency; if one is unavoidable it MUST be approved and recorded in the pull request with its transitive set.
- **FR-061**: The MCP endpoint MUST emit structured failure logs that contain no bearer material, request payload, tool arguments, user identifier, or chat identifier, and MUST publish low-cardinality request-count, refusal-count, completion-count, duration, and admission-state metrics through the existing `RuntimeObservability` boundary.

### Key Entities

- **MCP envelope**: the request and response message shape Astral carries over its own transports; gains protocol metadata and a result-type discriminator in Phase A.
- **Result type**: the discriminator distinguishing a completed result from one requiring further input.
- **Protocol metadata**: the reserved-key map carrying the protocol version and caller capabilities (both required by the specification), caller identity, and responder identity (both recommended).
- **MCP endpoint**: the Phase B HTTP surface accepting MCP requests from third-party hosts; flag-gated, audience-bound, header-validated.
- **Protected resource metadata**: the document naming the authorization servers that issue tokens for the MCP endpoint.
- **MCP audience**: a dedicated identity-provider client identity for which MCP tokens are minted, distinct from the web and native client identities.
- **MCP render target**: a renderer registered through the existing registry seam that projects a component tree into MCP content blocks — the fourth target alongside web, voice, and accessibility-object-model.
- **Authorized tool set**: the per-user, per-turn resolved list of invocable tools; the thing the MCP listing publishes, as distinct from the catalog.
- **Destructive classification**: the existing single-source per-verb classification; the source of both the MCP destructive hint and the MCP invocation refusal.
- **Tracked operation**: the existing durable long-running-job record; surfaced over MCP by identifier and status rather than as a protocol task.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With a peer emitting an MCP frame carrying an unknown field, tool calls complete normally in **100% of 20 trials** on the WebSocket transport in both directions and on the in-process and tunnel transports in the response direction, with **zero** dropped frames and **zero** 30-second timeouts. On the pre-change build those same trials fail.
- **SC-002**: **Zero** MCP responses in a full test-suite run carry an error together with renderable components. On the pre-change build at least four agent servers produce that shape.
- **SC-003**: **100%** of MCP requests emitted in a full test-suite run carry both required metadata fields, and **100%** of responses carry a result-type discriminator.
- **SC-004**: A peer declaring an unsupported protocol version is refused with the specification's error naming supported versions in **100%** of trials, and is never processed.
- **SC-005**: **Zero** error codes emitted anywhere on the MCP path fall in the range the specification reserves for legacy implementation-defined use.
- **SC-006**: A tool schema failing validation against its declared or default dialect is refused at registration with the tool named, in **100%** of trials; **zero** schemas are rewritten before validation.
- **SC-007**: Phase A leaves the published client wire manifest byte-identical, and every client's drift-guard suite passes unchanged.
- **SC-008**: With the Phase B flag off, the deployment exposes **no** MCP route and **no** protected-resource metadata document, and the full test suite result is identical to the pre-feature build.
- **SC-009**: A token minted for the existing web client is refused at the MCP endpoint in **100% of 20 attempts**; a token minted for the MCP audience is accepted.
- **SC-010**: Across at least **20 adversarial attempts** — missing header, mismatched header, non-ASCII name without encoding, missing required metadata field, undeclared required capability, invalid origin, removed HTTP method, oversized body, cookie-only credentials, and token in query string — **zero** requests are served and every refusal carries the specification-defined status and error code.
- **SC-011**: For two users with different agent permissions, each MCP tool listing equals that user's chat-path authorized set exactly, in **100%** of trials, with **zero** tools leaked across users.
- **SC-012**: A tool whose permission is revoked between listing and invocation is refused at invocation in **100% of 10 trials**.
- **SC-013**: Across at least **20 attempts** spanning direct, chained, and parallel dispatch, **zero** destructive verbs execute when invoked over MCP, and every refusal states the reason.
- **SC-014**: A read-only tool invoked from a real third-party MCP host renders its result in that host in **at least 9 of 10 trials**, and the structured data is present on **100%** of results.
- **SC-015**: **100%** of MCP invocations appear in the audit trail with the same event class and correlation as the equivalent chat dispatch, and the hash chain verifies.
- **SC-016**: After the Phase B flag has been enabled and then disabled, **zero** residual routes, advertisements, or persisted records remain, in **100%** of trials. SC-008 covers the never-enabled case.
- **SC-017**: The feature adds **zero** new third-party runtime dependencies.
- **SC-018**: Changed-line coverage across the feature's diff is **at least 90%**, and all six CI gates pass.
- **SC-019**: A tool schema containing a network-resolving `$ref` produces **zero** outbound network requests during validation across at least **10 attempts**, and is refused rather than accepted as permissive.
- **SC-020**: Every endpoint refusal and unexpected failure emits one redacted structured log record; the runtime metrics snapshot exposes MCP accepted, refused, completed, and failed counters plus the latest request duration and admission gauges, with **zero** high-cardinality or payload-bearing labels.

## Assumptions

- The deployment's identity provider can be configured with an additional client identity and audience mapper; the existing delegation setup documents the same pattern for the agent-service audience.
- Third-party MCP hosts available for live verification implement the `2026-07-28` revision. If none does by implementation time, a purpose-built conformant client is used for diagnostic coverage and the gap is recorded explicitly; Phase B remains unproven and is not merge-ready until a real third-party host closes FR-058 and SC-014.
- The reverse proxy is configured such that the deployment's own canonical HTTPS URL is derivable from the request, since the protected-resource metadata document's resource identifier depends on it.
- MCP-invoked tools have no chat context; results are returned to the caller and are not upserted into a workspace. Whether MCP invocations should ever materialize into a chat canvas is out of scope.
- The `2026-07-28` revision is stable and will not be amended in place; a subsequent revision is a new feature.

## Dependencies

- Feature 040's in-process agent registry and the per-user permission baseline, which the MCP tool listing must resolve against.
- Feature 056's shared gate stack, which FR-047 requires MCP invocation to route through rather than duplicate.
- Feature 063's destructive classification and unattended-refusal mechanism, which FR-048 depends on.
- Feature 026/029's renderer registry seam, which FR-049 depends on for the MCP render target.
- Feature 058's BYO bundle delivery and revalidation mechanism, which FR-004 depends on.
- The existing Keycloak realm and the operator's ability to add a client; documented alongside the existing delegation setup.
- **External record-keeping**: the kos-wiki pages `astral-mcp-2026-07-28-upgrade` and `astral-feature-timeline` are updated on ship. They live in a separate repository and are **not part of this feature's diff**.

## Out of Scope

- **MCP Apps** in any form (D4). No `ui://` resources, no sandboxed iframe, no `postMessage` bridge, on any client.
- **The Tasks extension** (D5). No `tasks/*` methods, no task handles, no task notifications.
- **Every change-notification type** — `toolsListChanged`, `promptsListChanged`, `resourcesListChanged`, `resourceSubscriptions` (D6). `subscriptions/listen` itself is accepted and acknowledged with an empty agreed set so the message pattern is answered; no notification is ever emitted on it.
- **Acting as an MCP client.** Astral consuming third-party MCP servers is a separate feature; every client-binding requirement in the revision's authorization tree stays out of scope until then.
- **Dynamic Client Registration** and Client ID Metadata Documents. Astral is not an authorization server, and MCP clients are pre-registered by the operator.
- **Prompts and resources primitives.** Only tools are published.
- **Skills over MCP.** Not published; watch item only.
- **A2A card fidelity.** The A2A bridge's discarding of `input_schema`, `output_schema` and `metadata` is unchanged by this feature.
- **The 063 tracked-job durability gap** noted under Verdict 2. It is real, it is 063 code rather than MCP code, and it belongs in the follow-ups register.
- **Retiring the A2A endpoint**, and the pre-existing unauthenticated-route findings surfaced while surveying the HTTP surface. Both are real and both are separate work; they are recorded in the follow-ups register, not fixed here.
- **Thesis prose.** This spec records the verdicts the thesis will cite; it does not write them.

## Sources

- `https://modelcontextprotocol.io/specification/2026-07-28/changelog` — the nine major and twelve minor changes, deprecations, lifecycle policy (retrieved 2026-07-29)
- `https://modelcontextprotocol.io/specification/2026-07-28/basic` and `/basic/versioning` — stateless core, `_meta` reserved keys and their required/optional split, `resultType`, the error-code allocation policy, JSON-Schema usage rules (retrieved 2026-07-29)
- `https://modelcontextprotocol.io/specification/2026-07-28/server/discover` — the discovery RPC and the stdio-only era probe (retrieved 2026-07-29)
- `https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http`, `/basic/patterns/mrtr`, `/basic/patterns/subscriptions`, `/basic/transports/stdio` (retrieved 2026-07-29)
- `https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization` and `/authorization/client-registration` — the server-vs-client obligation split (retrieved 2026-07-29)
- `https://modelcontextprotocol.io/specification/2026-07-28/deprecated` and `https://modelcontextprotocol.io/community/feature-lifecycle` — Roots/Sampling/Logging/DCR, twelve-month minimum window (retrieved 2026-07-29)
- `https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/schema/2026-07-28/schema.ts` — authoritative types and the error-code constants `-32020`/`-32021`/`-32022` (retrieved 2026-07-29)
- `https://modelcontextprotocol.io/extensions/overview` — the extension mechanism, independent versioning, the `io.modelcontextprotocol/ui` identifier and its client settings object (retrieved 2026-07-29)
- `https://modelcontextprotocol.io/extensions/tasks/overview` and the `ext-tasks` `specification/draft/tasks.md` — Tasks lifecycle, its `-32003` collision with the core partition, and its statement that task `inputRequests` are distinct from MRTR (retrieved 2026-07-29)
- `https://modelcontextprotocol.io/extensions/apps/overview` and the `ext-apps` `specification/2026-01-26/apps.mdx` — MCP Apps mechanism and its own `protocolVersion` axis (retrieved 2026-07-29)
- `https://modelcontextprotocol.io/community/working-groups/skills-over-mcp` — SEP-2640, status In Review, no target date (retrieved 2026-07-29)
- `extrepo:AstralDeep/AstralDeep@e492668b` — `main` after PR #150. `backend/shared/protocol.py`, `backend/shared/base_agent.py`, `backend/shared/local_transport.py`, `backend/shared/a2a_bridge.py`, `backend/shared/a2a_security.py`, `backend/shared/auth_clients.py`, `backend/orchestrator/orchestrator.py`, `backend/orchestrator/auth.py`, `backend/orchestrator/agent_generator.py`, `backend/orchestrator/remote_jobs.py`, `backend/orchestrator/remote_confirmation.py`, `backend/agents/*/mcp_server.py`, `backend/webrender/`, `backend/rote/`, `windows-client/win_agent/agent.py`, and the absence checks tabulated above
