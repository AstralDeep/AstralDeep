# Integrated UI contract and implementation map

Status: design for feature 088; no runtime implementation or qualification is implied.
Source baselines and approved scope are recorded in `../spec.md`.
Paths beginning `Projection/` below mean the pinned `components/AstralProjection/`
repository; `Deep/` means AstralDeep. New files are explicitly identified as proposed.

## Experience and submission policy

The default experience has one composer, recent work and a result area. The rewrite's
logical grouping is retained, with less introductory text and fewer simultaneous
controls. Use the existing self-hosted Inter typography, semantic theme roles and
saved theme preference. Use flat lists, restrained borders, generous spacing and a
single accent for the principal action; avoid dashboard cards for every datum.
Conversation and result text should normally remain within approximately 70
characters per line. Preserve code typography and high-contrast/reduced-motion modes.

The owner rejected the first sparse-canvas/narrow-rail screenshots on 2026-09-10.
The revised web start view therefore has one centered 44rem working column:
welcome heading, any required permission notice, multiline composer, three compact
example shortcuts and an additional-examples disclosure. The composer is the main
visual surface. The empty transcript rail and canvas do not occupy the starting
screen; Send, restoration, real output or opening history reveals the existing
responsive workspace. New chat returns to the start arrangement.

Retain the semantic midnight tokens: background `#0F1221`, surface `#1A1E2E`,
primary `#6366F1`, text `#F3F4F6` and muted `#9CA3AF`; user-selected themes still
override them. Use the existing locally hosted Inter with a medium-weight heading,
regular shortcut labels and one restrained composer border. Remove decorative
background gradients and repeated control containers. Narrow layouts use small
fixed inline gutters so enlarged text keeps usable writing space.

Projection owns empty welcome placement hosts around the permanently mounted
composer. Its web adapter moves only recognized top-level server-rendered welcome
nodes, preserving their identity and accessibility wrappers. It never clones the
composer or invents a second example catalog. Full replacement, partial update,
retirement and account cleanup govern these hosts; late welcome cannot overwrite
active work. DOM, reading and keyboard order match the visible layout. Enter sends;
Shift+Enter inserts a line and IME composition never accidentally submits. These
are web presentation changes within the approved web-first scope, with no new
primitive, shared protocol frame or IAM path.

The 2026-09-11 native follow-up applies this arrangement to Android and Apple
clients. `Projection/contracts/ui_protocol.json` now records
`presentation_contracts.workspace_088`, with an actual server-generated welcome
fixture under `contracts/fixtures/workspace_088/`. Only recognized top-level
`data-welcome` roles with absent or `wel_` identity may enter start slots;
identified results and nested hints remain ordinary content. The same ephemeral
owner-bound composer survives resizing and same-owner reconnect, and clears on
sign-out, owner change and New chat. No second menu, example catalog, settings
authority or effect approval path is introduced.

Native audit navigation uses the server's owner-scoped filters, UTC boundaries,
cursor history, grouped navigation events and detail return state through the
normal `chrome_open`/`chrome_audit_page` surface. Agent settings use the same
owner-scoped host reads and permission resolver as web: personal permissions and
credential settings remain available independently of ownership, visibility is
owner-only, and trust requires owner/admin authority. Existing command handlers
remain authoritative. An unlinked ORCID identity explicitly hands off to the
signed-in web flow because its start route requires a browser session.

Android/iOS/macOS charts use the existing bundled Plotly library inside isolated
native chart views. `scripts/build_native_chart.py` extracts the web chart options
into a reproducible, CSP-hashed offline template, accepts only a base64 JSON
component envelope, and supplies no native action bridge. Navigation and network
are denied; charts requiring remote assets report an explicit web handoff. Native
shells own bounded geometry, title, provenance, and authenticated export controls.
Shared Inter/JetBrains assets retain the approved web font outlines and licenses.

Authenticated downloads use exact-origin credential checks before local
save/share. Windows retains its existing compatible client under the owner's
explicit exclusion. Watch size/capability handoffs, browser-bound ORCID setup,
remote-asset chart handoffs, and outstanding authenticated live checks are
qualification gaps, not evidence of exact parity.

The primary action is **Send**. Ordinary chat and requested public research enter the
existing authenticated dispatcher immediately, with normal admission, provider,
permission, PHI, egress and budget checks. There is no mandatory assignment-review
step. Advanced source, agent, skill and private-note selection is available on demand;
valid defaults do not require opening it. Attachment, voice, background-work and
existing multi-turn conversation behavior remain accessible.

Send does not approve saving/publishing a result, a consequential effect or a new
grant. Those reviews show the exact content/effect, destination and material scope,
with an explicit action such as **Save result** or **Approve action**. Revisions,
digests, expiry and control epochs bind the server command but do not become routine
explanatory copy. A changed, expired or already-consumed review cannot authorize a
different effect. An uncertain response is reconciled before presenting another
effectful attempt.

## Destinations and donor coverage

Primary navigation exposes New task, recent work and Saved results. A secondary
workspace destination exposes recurring work and agents; Settings holds guidance,
private notes, providers and connections. Exact labels may be refined during live
usability checks, but every capability below must remain reachable. Activity and
usage are contextual disclosures; administrator diagnostics are role-filtered.

| Donor destination/capability | Integrated destination and preserved behavior |
| --- | --- |
| Observatory and task composer | New task; a brief prompt and optional examples, immediate ordinary Send, expandable source/guidance controls. Preserve attachment, voice, slash selection, chat history and generated workspace. |
| Operations list/detail | Recent work and task detail; owner-authorized lifecycle, current result, sources, pending review and contextual cancel/pause/resume/reconcile controls. Preserve conversation continuity and existing operation identifiers. |
| Artifact list/detail | Saved results; exact reviewed proposal, source/task provenance and existing workspace export/share/download paths. Merely viewing a result is separate from saving/publishing it. |
| Schedules | Recurring work; one-shot/interval/cron functionality plus bounded monitoring, initial/unchanged/changed/insufficient-evidence outcomes, allowance and effective Stop. Reuse existing assignment controls where their semantics match. |
| Agents/editor/history | Agents; declarative definition, revision, activation, archival, clone and history alongside bundled agents and existing user-hosted/generated agents. |
| Skills | Reusable guidance library with aliases, applicability, revision, enable/disable/delete and explicit selection. Existing procedural packs/recipes and tool permissions remain available. Do not relabel tool grants as if they were donor instruction skills. |
| Memories | Private notes with current encrypted value, category, expiry, correction, enable/disable, search, explicit selection and Forget. Preserve existing personalization memory features; selected notes never become source evidence or authority. |
| Provider settings | Existing LLM settings retain working provider breadth and user/system separation; compatible local inference/accounting is additional. Endpoint/key edits clearly express keep/replace/remove; retaining a secret when changing its destination must be explicit and validated. |
| Grants | Bounded unattended permissions, current scope/limits/expiry and revoke. An authority increase requires explicit approval; reducing authority remains usable when no provider is configured. |
| Framework credentials | Connections; scoped issuance, one-time secret presentation, expiry and revoke through current institutional identity/delegation. No donor session or alternative IAM is introduced. |
| Usage and diagnostics | Per-task/owner outcome, timing and usage disclosures; bounded admin diagnostics. Unknown/incomplete usage differs from zero; uncertain effects differ from success. |
| Login, deep links, adaptive shell, notifications, installable shell | Existing Keycloak sign-in; safe navigation/back/forward and account-safe return location; responsive layouts and accessible notices. Adopt installable/public-shell behavior through packaged Projection assets with no private response caching. |

Donor implementation references are `REWRITE/web/assets/js/views/`,
`components/composer.js`, `components/assignment-preview.js`, `core/router.js`,
`core/operation-controls.js`, `core/skills.js`, `core/owner-memories.js`,
`core/host-interactions.js`, `surface/`, `web/sw.js` and `web/manifest.webmanifest`.
These are behavioral references, not a second production frontend.

## Component ownership and concrete source targets

| Owner and existing source | Implementation responsibility |
| --- | --- |
| `Projection/backend/webrender/templates/shell.html`, `static/astral.css`, `static/client.js` | Simplify shell, navigation and composer; preserve connection/status, staged attachments, voice controls, modal accessibility and existing ROTE layouts. Split focused vanilla JS modules only as useful; keep one packaged frontend source. |
| `Projection/backend/webrender/chrome/menu_model.py`, `topbar.py` | Extend the single role/capability-filtered navigation model for new destinations. Web and native derive navigation from this server model. Current model is version 1; evolve compatibly or negotiate a new version rather than silently changing its exact shape. |
| `Projection/src/astralprojection/chrome/personalization.py`, `assignments.py`, `agents.py`, `workspace.py`; proposed `work.py` and `guidance.py` | Pure shared view builders consume authorized snapshots and emit existing components. Reuse assignment revision/control bindings and existing provider/permission presentations. General task composition must not be put into `backend/webrender/chrome/composer_model.py`, which currently owns the voice composer state. |
| `Projection/src/astralprojection/models.py`, `protocol.py`; `contracts/ui_protocol.json`; `backend/rote/capabilities.py`, `adapter.py`, `fallback.py` | Define/validate shared presentation, closed actions and negotiated client dispositions. Preserve existing theme/layout semantics and unknown-content refusals. Any new frame/action is recorded with cross-client drift fixtures. |
| `Projection/backend/webrender/renderer.py`, `sanitize.py`, `a11y.py`; `src/astralprojection/resources.py` | Render through existing escaping, sanitization, accessibility and packaged-resource accessors. Package any new JS, manifest or public worker with matching cache/version/CSP behavior. |
| `Deep/backend/orchestrator/projection_surfaces/__init__.py`, `chrome_events.py`; proposed host adapters `work.py`, `guidance.py`, `connections.py` | Authorize, query domain services, assemble snapshots and dispatch registered commands. Deep owns side effects; Projection does not import Deep or query its persistence. Update the explicit surface ownership registry and tests for each added surface. |
| `Deep/backend/orchestrator/projection_surfaces/personalization.py`, `authoring.py`, `llm.py`, `agents.py`, `attachments.py`, `workspace_timeline.py`, `theme.py`; `welcome.py` | Preserve existing workflows while reusing shared builders. Replace the six-card welcome with brief ordinary-dispatch examples; do not create unnecessary permission prompts. Keep stored theme selection and context-sensitive controls. |
| `Deep/backend/orchestrator/api.py`, `backend/shared/protocol.py`, existing admission/personalization/scheduler/persistent-agent services | Provide owner-authorized work/guidance/settings read models and normal-dispatch commands. Existing `GET /api/operations/{operation_id}` is deliberately payload-free reconciliation; richer task detail needs a separate versioned read model or explicitly compatible extension, not replacement with donor payloads. |
| `Deep/config/astral-composition.json` | Pin exact qualified component commits/contracts only after component changes are validated. No UI-owned SQL, independent store, provider credential store or donor database is added. |

The recent-work read model links existing conversations, operations, assignments and
saved results without inventing a competing lifecycle authority. Preserve existing
chat deep links; add server-validated work destinations without returning owner data
for unauthorized or absent identifiers. UI commands carry server-issued current
bindings, not authority inferred from enabled buttons or client navigation state.

## Existing primitive mapping

Use the current manifest vocabulary and `to_dict()`/`create_ui_response()`. No new
primitive or runtime dependency is needed for this mapping.

| Donor semantic component | Existing Projection/Primitives representation |
| --- | --- |
| text | `text`, with escaped/validated content |
| stack | `container` with bounded server layout |
| facts | `keyvalue` or accessible `list` |
| sources | `list`/`text` with vetted source links and explicit completeness |
| timeline | `timeline`; detailed events behind a disclosure by default |
| callout | `alert`, reserved for actionable state or material uncertainty |
| artifact | `text`/`code` plus authorized `download_card`/`file_download`; approval includes the complete exact result |
| action | `button` with registered server action and current binding |
| handoff | `alert`/`text` plus a server-authorized destination; no client-invented permission |

User/model text never becomes raw HTML, a CSS authority channel or a trusted
`Primitive.attributes` override. The donor's surface validator may inform validation
tests, but the ecosystem manifest and renderer remain authoritative.
The existing Deep shell injects request/session material and CSP nonces: a service
worker must not copy the donor's cache-the-root-page rule onto this shell. Offline
startup needs a separate public, non-personalized disconnected document, with only
allowlisted public static assets cached.

## Account state, bootstrap and operation state acceptance

- Drafts are keyed only after verified issuer-and-subject identity is available.
  Reuse the existing account-change seams in `client.js` (`prepareAccountIdentity`,
  `clearActiveChatLocator`, `clearCommittedConversationView` and sign-out). Do not
  restore a global donor `sessionStorage` draft. Explicit logout or verified owner
  change erases the prior draft, selections and private rendered caches. Temporary
  same-owner recovery may preserve a draft while withholding it until identity is
  reverified. A failed logout still clears local private UI immediately.
- Public configuration has a lifecycle independent of authenticated request
  generation. Either serialize bootstrap deliberately or separate public requests
  from the owner fence. Exercise both config-first and session-first completion,
  genuine config failure/retry, logout during bootstrap and owner changes. Discarded
  stale work must not produce a false configuration warning.
- Task views cover loading, empty, active, waiting, review-required, paused,
  completed, cancelled, failed, uncertain/reconciliation and unavailable states.
  These labels map to backend states; they do not create a second state machine.
  Current result and next action lead; IDs, attempts, usage and event detail are
  disclosed. Retain existing terminal-status ownership and stale-frame fences.
- Unknown components/actions, unsupported client versions, expired reviews,
  disabled guidance and cross-owner results fail closed with actionable messages.
  Navigation away, reconnect and double activation cannot repeat an accepted effect.

## Server-owned web-first disposition

The user authorized redesigned web first on 2026-09-10; native redesign remains
required in a later milestone. During the web milestone, existing native sign-in,
conversation, attachments, tools, voice and shared chrome continue working.
Server negotiation selects a supported shared-component rendering, read-only view
with explicit handoff, or version-required refusal for each new destination/action.
Clients do not independently hide unsupported controls to simulate parity. No
consequential action becomes executable through a fallback that cannot show its
full review. Existing working actions must not be withdrawn merely because layout
redesign has not shipped. Record the disposition matrix and fixtures in Projection's
authoritative contract alongside Deep's host policy.

Native implementation targets are `Projection/windows-client/astral_client/chrome.py`,
`protocol.py` and `protocol_manifest.py`; Android
`android-client/core/src/main/kotlin/com/personalailabs/astraldeep/core/chrome/ChromeMenu.kt`,
`android-client/core/src/main/kotlin/com/personalailabs/astraldeep/core/protocol/ProtocolManifest.kt`
and `android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/ui/AppViewModel.kt`;
Apple
`AstralCore/Sources/AstralCore/Chrome/ChromeMenu.swift` and
`AstralApp/AstralApp/Views/ComponentChrome.swift`. All later client redesigns reuse
the same authorized view snapshots, action meanings and semantic theme roles.

## Acceptance evidence and baseline commands

Run source baselines before mutation; the commands below are planned checks, not
reported passes. Use the pinned component environment and CI tooling. Python remains
3.11; web tooling is Node 24 with Corepack npm 11.16.0. Existing runtime dependencies
must already be installed through the repository's approved setup.

From the Projection root:

```text
python -m pytest tests/chrome tests/webrender tests/rote tests/test_protocol.py tests/test_resources.py -q
python -m pytest windows-client/tests/test_chrome.py windows-client/tests/test_protocol_manifest.py windows-client/tests/test_theme_live.py -q
```

Set `QT_QPA_PLATFORM=offscreen` for Windows tests. From
`Projection/tooling/web-ci`, run `corepack npm ci --ignore-scripts`,
`corepack npm run check:package-manager`, `corepack npm run check:product-isolation`
and `corepack npm run lint`. Run the existing Playwright continuity, voice and
persistent-assignment suites using the pinned image and fixture-generation steps
in Projection's `.github/workflows/ci.yml`. The present lint script explicitly names
`client.js`; include all added static modules and correct module parsing if the
frontend is split. Keep Python changed-line coverage at least 90% and run relevant
full component/Deep CI before qualification.

From `Deep/backend`:

```text
python -m pytest tests/test_projection_surface_ownership_074.py tests/test_projection_resources_integration.py tests/test_projection_protocol_integration.py tests/test_projection_controllers.py tests/test_chrome_events.py tests/test_shell_assets.py tests/test_shell_security_headers.py tests/test_client_js_contract.py tests/test_first_turn_contract.py tests/test_history_surface.py tests/test_operation_api_060.py tests/chrome -q
```

Add feature-088 fixtures and browser tests for every destination and the acceptance
states above. Especially cover A logout to B login in the same tab, return to A,
same-owner reconnect, both bootstrap completion orders, double Send, late terminal
frames, exact review rejection/expiry, retained providers/tools, selected guidance
invalidation and 320px/200%-text operation. Existing tests include
`tests/chrome/test_menu_model_contract.py`, `test_shell_blocks.py`,
`test_personalization_views.py`, `test_assignments.py`,
`tests/webrender/test_chrome_host_neutral.py` and `test_escaping.py`.

Native compatibility gates include Windows protocol/chrome tests, Android
`ProtocolManifestTest`, `ChromeMenuTest`, `ChromeSurfaceReducerTest` and standard
lint/build/coverage tasks, and Apple `ManifestDriftTests`, `ChromeSurfaceModeTests`
and `AppModelChromeSurfaceTests` on macOS. Record live native compatibility evidence
for the web milestone; redesigned-native evidence is a distinct later requirement.

Exercise ordinary chat/public research and denial/effect-review journeys against a
real authenticated backend. Verify layout, focus, screen-reader semantics, keyboard
navigation, reduced motion and accessible error recovery at wide/narrow sizes.
The five-person first-task usability check in the spec is human evidence; automated
fixtures cannot claim it passed. Donor tests and mocked browser fixtures alone do
not establish integrated capability, authorization or live usability.
