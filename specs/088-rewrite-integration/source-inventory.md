# Frozen source and adoption inventory

Feature 088, frozen 2026-09-10. [source-inventory.json](source-inventory.json) carries exact commit/blob/path anchors and the full machine-readable acceptance inventory.

This fixes capability scope; it is not an implementation or release claim. **All integrated implementation and verification remain pending.** Existing behavior can satisfy an adoption only after its observable behavior and security are verified. Donor source, fixtures or qualification reports cannot close an integrated requirement.

## Frozen baselines

- AstralDeep: `2de5d867413ce2aec5b7448dca8eeaa42674a7e3`.
- Rewrite: `daeea32b6e99b9f7cf0ff32abe372731744c639e`; all 878 tracked files are classified and anchored.
- Immutable 081–087 sources: `51f6ee22703fe11e5feb99fe26a9f556825219f9`. All 15 copied file hashes verified; 224 functional requirement IDs indexed without duplicating spec prose.
- Pinned components: AstralPlane `b20c8f3e06fc5302262fe3c8f049fa18a562e30f`; AstralPrimitives `8dadde18ea84b511e130ccc332cf2f62de2990cf`; AstralProjection `07e5c90cb310c48c2315997f74dc8dd6f5fa22ea`; LETS `6245189920c686353c4ced7a208d56ec266f745c`.

## Confirmed owner decisions

- Ordinary explicitly requested chat and public research begin on Send without separate assignment preflight; consequential actions, publication and new permissions retain approval.
- No standalone rewrite users, data, credentials, sessions, schedules, approvals or execution state are imported. Preserve Deep data and leave donor services untouched.
- Redesigned web first; preserve current native compatibility. Windows, Android and Apple/wrist redesign remains required in a later milestone.
- Reuse institutional Keycloak realm Astral, existing verified subject ownership, client registrations and Deep provider credential separation.

No donor database/state migration is required. Additive Deep/Plane schema evolution and representative populated-data recovery still require qualification. The isolated rewrite stays a read-only donor.

## Required capability register

Each row has source-present donor evidence and verified existing source anchors. Integrated evidence is pending for every row. Web core is a rollout milestone, not native redesign completion. Exact source families, FR mappings and repository owners are in JSON.

| ID | Capability | Integration owner | Required adaptation | Milestone | Evidence |
|---|---|---|---|---|---|
| C01 | One authenticated command boundary | Deep; Plane | Map verified context and immutable intent into one existing dispatch path. Preserve origin and refusals without parallel execution. | web-core | Pending |
| C02 | Durable lifecycle and task controls | Deep; Plane | Adopt queued/active/wait/authority/budget/retry/reconciliation/terminal meanings and cancel/pause/resume/wait/wake/delete controls; maintain current native protocol. | web-core | Pending |
| C03 | Idempotency, event delivery and reconnect | Deep; Plane; Projection | Retain operation/attempt/delivery separation, pagination, sequence cursors, resync and bounded SSE/polling. Reconnect cannot duplicate an effect. | web-core | Pending |
| C04 | Leases, reservations and uncertain-effect recovery | Deep; Plane; LETS | Adopt atomic reservation/settlement, heartbeat fences, cancellation and retry classification through Plane. Unknown external effects require reconciliation, not replay. | web-core | Pending |
| C05 | Ordinary chat and first useful submission | Deep; Projection | Send starts ordinary work immediately. Preserve existing multi-turn conversation and tool planning; donor single-turn chat is not a replacement. Guidance binding stays deterministic without mandatory preflight. | web-core | Pending |
| C06 | Public research and evidence extraction | Deep; Projection | Adopt source attribution, completeness, citation/envelope validation and insufficient-evidence outcomes; retain existing search discovery and refetch ephemeral evidence on retry. | web-core | Pending |
| C07 | Exact result proposals and saved artifacts | Deep; Plane; Projection | Map owner/revision/digest/target/expiry/one-use approval and deterministic publication to existing artifacts/workspaces. Preserve existing artifact types and sharing policy. | web-core | Pending |
| C08 | Recurring schedules and bounded grants | Deep; Plane | Adopt interval schedule lifecycle, run allowances, pause/resume/Stop and outstanding-episode fences. Keep existing cron and one-shot schedules. Correct cross-owner scan starvation. | web-core | Pending |
| C09 | Source-change monitoring and observation cursors | Deep; Plane; Projection | Add complete-source fingerprints, grounded initial observations, prior-episode binding, unchanged-source generation avoidance and meaningful-change results. Do not import donor schedules/cursors. | web-core | Pending |
| C10 | Declarative agent lifecycle and history | Deep; Plane; Projection | Adopt draft/revise/activate/archive/clone, immutable revisions, preview and current-agent binding while preserving bundled, generated and external agents. | web-core | Pending |
| C11 | Agent capability and trigger policy | Deep; LETS | Preserve capability attenuation, manual/scheduled trigger policy and current-revision invalidation. Donor three-capability registry and empty isolation registry cannot restrict existing authorized tools/remote execution. | web-core | Pending |
| C12 | Owner skill libraries and command expansion | Deep; Plane; Projection | Add applicability/aliases/history/enable-disable-delete and explicit selected revision/digest binding. Preserve existing procedural skill packs and recipes. | web-core | Pending |
| C13 | Encrypted explicit owner notes | Deep; Plane; Projection | Add profession/goal/preference/workflow_tag/context notes, correction/search/expiry/enable-disable/Forget and encrypted current-value binding. Keep existing memory/personalization; donor live memory qualification is not complete. | web-core | Pending |
| C14 | Deterministic guidance selection and dispatch fences | Deep; Plane; Projection | Bound combined skill/note expansion; recheck selected current revisions/expiry and bindings at dispatch/approval. Guidance never becomes authority or verified evidence. | web-core | Pending |
| C15 | Provider configuration and credential separation | Deep; Plane; Projection | Reuse owner and admin system records with existing no-cross-fallback policy. Preserve provider breadth and encrypted/redacted settings. Origin-bound key reuse is an additional design concern. | web-core | Pending |
| C16 | Optional local model and bounded transport | Deep; operator infrastructure | Qualify optional local Qwen profile/accounting and subprocess transport cleanup within existing provider seams. Donor custom providers cannot generate; that limitation cannot disable existing Deep providers. | web-core | Pending |
| C17 | Institutional IAM, sessions and native identity | Deep; Plane; Projection | Reuse institutional Astral realm/sub, existing BFF and native client ids. Reconcile strict donor audience/current-IAM reads and logout semantics; no donor user/session import or competing cookie store. | web-core | Pending |
| C18 | Scoped framework credentials and unattended consent | Deep; Plane | Add interactive issue/list/revoke, hashed secret, bounded allowance/expiry and provenance with final authority fence. Framework credentials never replace RFC8693 downstream authority or exact user approval. | web-core | Pending |
| C19 | REST interoperability and canonical read models | Deep; Projection | Expose/adapt operation/event/artifact/agent/tool/credential APIs and read schemas through one command boundary. Approval/reconciliation/settings stay nondelegable; private stream delivery remains current-authority checked. | web-core | Pending |
| C20 | MCP discovery, tool calls and legacy sessions | Deep | Adopt supported discover/initialize/list/call and exact-credential legacy sessions. Coordinate old astral-mcp JWT audience versus donor opaque credential; no weaker fallback. | web-core | Pending |
| C21 | A2A message/task/stream interoperability | Deep | Adopt SendMessage/SendStreamingMessage/GetTask/ListTasks/CancelTask/SubscribeToTask and human-input projection; explicitly refuse unsupported operations and preserve old integrations. | web-core | Pending |
| C22 | Sync/async Python SDK and stdio bridge | Deep public API; first-party SDK | Adopt sync/async clients, same-key submission, revision-pinned controls, polling/SSE, bounded retry and owner-action stops. Qualify packaging and fixed-destination stdio bridge. | web-core | Pending |
| C23 | External framework adapters and examples | Deep public API; first-party SDK | Track generic tools, OpenAI Agents, Anthropic/Claude, LangChain/LangGraph, CrewAI, PydanticAI and Microsoft/AutoGen wrappers separately. Source examples are not executed integrations. | web-core | Pending |
| C24 | Calmer task shell, navigation and owner-scoped drafts | Projection; Deep surface adapters | Deliver one composer/recent work/result surface with progressive disclosure. Preserve reachable old tools; fix draft owner boundary and public-config bootstrap race. No mandatory ordinary-work preview. | web-core | Pending |
| C25 | All settings and library destinations | Projection; Deep; Primitives if approved definition required | Retain operations/details, agents/editor/history, artifacts/details, provider, grants, credentials, skills, memories, schedules, usage and admin diagnostics. Recompose forms once on server. | web-core | Pending |
| C26 | Semantic UI validation, adaptation and generated contracts | Primitives defines; Projection renders/adapts; Deep composes | Map nine donor components and generic settings/actions onto existing rich vocabulary. Preserve rich media/data primitives and shape/depth/size/Unicode/style refusal, server-owned handoffs and one contract source. | web-core | Pending |
| C27 | Operation measurements, usage and admin diagnostics | Deep; Plane; Projection | Map owner timing/history and fixed-label admin counters. Preserve privacy/retention, unknown cost/coverage and overlapping/cutoff semantics. Diagnostic events grant no permission. | web-core | Pending |
| C28 | Security, audit provenance and outbound policy | Deep; Plane; LETS | Preserve owner isolation, taint/PHI, consent, egress, bounds and hash-chained audit. Donor public-data-only admission cannot erase clinical workflows or substitute for their PHI controls. | web-core | Pending |
| C29 | Durable storage, guarded upgrade and deletion | Plane owns migrations/repositories/blobs; Deep pins | Translate additive schema semantics to Plane registry/pinned digest; preserve old data/recovery. Never run donor SQLAlchemy registry against Deep storage; no donor database import. | web-core | Pending |
| C30 | Operations, bootstrap, backup/restore and readiness | Deep; Plane; operator/release infrastructure | Adopt readiness/shutdown/cleanup and backup/recovery verification into existing topology. Standalone ports, bundled Keycloak/accounts and donor secrets bootstrap are source evidence only; never replay on institutional IAM. | web-core | Pending |
| C31 | Browser/PWA and accessibility qualification | Projection; Deep | Adopt narrow/wide, keyboard/focus/screen-reader/reduced-motion/200-percent-text behavior and safe PWA caching/updates. Caches cannot leak private responses or resurrect other-owner drafts. | web-core | Pending |
| C32 | Windows redesigned native experience | Projection Windows; Deep contracts | Keep current PySide6 compatibility at web milestone; port donor operation/guidance/settings/current-state behavior and qualify redesigned Windows separately. Keep astral-desktop IAM. | native-redesign | Pending |
| C33 | Android redesigned native experience | Projection Android; Deep contracts | Keep current Compose compatibility at web milestone; port donor semantics and qualify Android build/accessibility/live journeys separately. Keep astral-mobile IAM/redirect. | native-redesign | Pending |
| C34 | Apple and wrist redesigned native experience | Projection Apple; Deep contracts | Preserve iOS/macOS/watchOS current behavior; adopt adapted semantic content, secure auth and server-owned handoffs. Native redesign/live evidence remains open after web release. | native-redesign | Pending |
| C35 | Evidence, drift guards and release qualification | All component owners; protected release verifier | Keep donor evidence identities separate; run integrated golden/denial/failure/recovery, changed-line coverage and live client/provider/IAM gates. No fixture, scan or web milestone closes the full feature. | all-milestones | Pending |

## API, UI, SDK and persistence coverage

The JSON indexes **99 route declarations/registrations**, **18 web destinations**, **12 catalog tools**, **22 SDK public classes**, and **24 donor tables**, with exact source anchors and pending dispositions. An established integrated endpoint can satisfy a mapping; omitting a donor behavior cannot.

- Core API: public/health and login/session/logout; operation admission/list/read/preview/events/SSE/surface/attempts/measurements/cancel/pause/resume/wait/wake/approve/reconcile/delete; artifact list/read; agent lifecycle/clone/preview/history; skills/aliases/history; notes/search/correction/expiry/Forget; providers/grants/schedules/framework credentials; UI contracts/negotiation; admin diagnostics.
- Interop: `/mcp`, `/a2a`, `/.well-known/agent-card.json`, `/api/interop/v1/*`, typed read models/OpenAPI/tool catalog and bounded current-authority streams. Approval and unsupported protocol operations retain explicit refusals.
- MCP tools: `astral_submit_operation`, `astral_get_operation`, `astral_get_operation_events`, `astral_list_operations`, `astral_cancel_operation`, `astral_pause_operation`, `astral_resume_operation`, `astral_wait_operation`, `astral_wake_operation`, `astral_list_artifacts`, `astral_get_artifact`, `astral_list_agents`.
- A2A methods: SendMessage, SendStreamingMessage, GetTask, ListTasks, CancelTask, SubscribeToTask; required human-action projection and unsupported-method dispositions.
- SDK: sync/async clients, bounded safe retries, exact revision controls, same-key submission, wait/poll/SSE, read/error models, fixed-endpoint stdio bridge and tool descriptors. All public function exports and framework examples remain anchored by the complete SDK file manifest.
- Framework families: generic tools, OpenAI Agents, Anthropic/Claude, LangChain/LangGraph, CrewAI, PydanticAI, Microsoft/AutoGen. Each wrapper/example needs an integrated acceptance disposition; source examples alone are not an executed integration.
- Web destinations: `observatory`, `login`, `operations`, `operation`, `agents`, `agent-new`, `agent`, `artifacts`, `artifact`, `provider`, `grants`, `credentials`, `skills`, `memories`, `schedules`, `settings`, `usage`, `diagnostics`.
- Shared UI: text, stack, facts, sources, timeline, callout, artifact, action and handoff; bounded appearance, action references, settings plans, capability negotiation and Unicode/generated exports. Map these to Primitives/Projection while retaining richer existing media/data controls.
- Native packages: Windows/PySide6, Android/Compose, Apple Swift/SwiftUI and wrist adaptation. Keep existing client/IAM contracts during web rollout; redesigned native build/live/accessibility evidence remains separately required.
- Storage: operation/attempt/event/approval/artifact, identity/credentials/grants, agent/revisions/policy, schedules/cursors, skills/revisions, memory and audit/schema rows are indexed. Plane owns integrated mechanics; donor table names and SQLAlchemy migrations do not become another schema authority.

## Retained existing platform breadth

The following baseline source groups were checked against the frozen Deep blob IDs. Historical donor breadth claims that skills/memory were absent are not copied as current facts; both were added in later donor source. Every retained family still needs integrated regression/live evidence.

| Existing family | Frozen source files | Required disposition | Evidence |
|---|---:|---|---|
| conversation_and_research | 34 | Preserve authorized behavior and records; verify additive adoption | Pending |
| voice_and_local_speech | 4 | Preserve authorized behavior and records; verify additive adoption | Pending |
| uploads_and_parsing | 13 | Preserve authorized behavior and records; verify additive adoption | Pending |
| authored_skills_and_commands | 4 | Preserve authorized behavior and records; verify additive adoption | Pending |
| memory_and_personalization | 3 | Preserve authorized behavior and records; verify additive adoption | Pending |
| persistent_and_scheduled_work | 33 | Preserve authorized behavior and records; verify additive adoption | Pending |
| declarative_and_executable_agents | 3 | Preserve authorized behavior and records; verify additive adoption | Pending |
| framework_transports | 1 | Preserve authorized behavior and records; verify additive adoption | Pending |
| office_developer_creative_connectors | 8 | Preserve authorized behavior and records; verify additive adoption | Pending |
| remote_compute_and_computer_control | 12 | Preserve authorized behavior and records; verify additive adoption | Pending |
| specialist_services | 46 | Preserve authorized behavior and records; verify additive adoption | Pending |
| identity_and_client_delivery | 4 | Preserve authorized behavior and records; verify additive adoption | Pending |

This retains multi-turn planning/search, attachments and medical/document/media parsing, clinical/ML/specialist agents, voice/local speech, procedural skills/recipes, memory/personalization, cron/one-shot/persistent work, generated/external agents, office/developer/creative/connectors, computer control/remote compute, institutional identity and all clients. Donor public-data-only policy, three registered tools, empty executable-isolation registry and single qualified local model are limitations to reconcile, never replacement ceilings.

## Five independently reproduced review defects

All five were reproduced on the donor and independently rerun by root on 2026-09-10. The corresponding integrated regression and live verification remain pending.

| ID | Priority | Exact donor anchor | Reproduced condition/outcome | Required correction | Evidence |
|---|---|---|---|---|---|
| R1 | P1 | `astral/security/sessions.py:501` | Issuing credential revoked after current-authority check, before owner-locked write. 201 issuance; new 90-day token reads with 200 while original reads with 401. | Repeat local credential/owner/expiry check with fresh time after owner lock before issuance. | Donor reproduced; integrated pending |
| R2 | P1 | `web/assets/js/core/prefs.js:78` | Owner A leaves prompt/sources, signs out; B signs in in same tab. Real session/prefs modules restore A draft under B in synthetic owner switch. | Bind draft to verified owner; erase on logout/verified change; preserve appropriate same-owner recovery. | Donor reproduced; integrated pending |
| R3 | P1 | `astral/runtime/worker.py:268` | Ten oldest schedules have outstanding budget-blocked episodes. Another owner ready schedule stays zero runs across three ticks; direct normal admission succeeds. | Bounded fair scan/rotation or eligibility-aware selection preserving authoritative admission. | Donor reproduced; integrated pending |
| R4 | P2 | `astral/runtime/executor.py:991` | memory_policy=none; source fetch succeeds, then one non-consuming provider_unavailable. retry_eligible becomes source_unavailable with one fetch/one model attempt despite healthy provider on retry. | Refetch transient evidence under remaining budgets without durable retention. | Donor reproduced; integrated pending |
| R5 | P2 | `web/assets/js/app.js:251` | Parallel public config completes after identity changes session generation. Live signed-out warning despite 200 endpoints; delayed-config module repro leaves config=null/error. | Sequence public bootstrap or separate genuinely public response handling; retain private generation fences. | Donor reproduced; integrated pending |

Additional concern A1: changing provider origin with a blank key field preserves/sends the prior key to the new host (`astral/services/commands.py:1358–1368`), verified with synthetic secrets and a network-free transport. The form documents retention, so this is separate from the five primary regressions. Specify origin-bound reuse or explicit reentry during provider adaptation.

## Identity and provider reuse

Verified public configuration: issuer `https://iam.ai.uky.edu/realms/Astral`; clients `astral-frontend`, `astral-desktop`, `astral-mobile`, `astral-watch`; real auth enabled. Allowlisted configuration inspection retained no secrets. Unauthenticated public discovery succeeded on 2026-09-10 and confirmed the exact issuer, relevant endpoints and S256; private client registration/mappers/privileges and authenticated integration were not verified.

Preserve institutional verified subject IDs; no donor usernames/accounts are matched or imported. Keep one BFF/session boundary: donor and Deep use incompatible `astral_session` formats. Retain native registrations/redirects. Reconcile the donor strict `astral-api` audience and current-IAM `view-users` account requirement through an administrative inventory; do not invent privileges, copy secrets or replay donor realm bootstrap. Preserve RFC 8693 agent delegation, `astral-mcp` audience, owner isolation and all tool/PHI/egress/approval gates.

Deep resolves interactive provider settings from the owner record and system work from its separate admin record (`backend/orchestrator/orchestrator.py:16854–16877`, `backend/llm_config/user_store.py`). Preserve no-cross-fallback and provider breadth. Actual saved endpoint/model metadata was not read; discover it through authorized repository/API seams. Donor optional local inference is additive.

## Complete donor source families

Every tracked file is classified once in JSON. This includes dependencies/lockfiles, SDK packages/examples, native projects, Compose/Docker, local IAM bootstrap, backup/restore, shutdown/readiness, schema/code generators, CI/tests, and dated security/qualification evidence. Recipes are assessed under existing ownership; presence does not authorize deployment or new dependencies.

| Family | Frozen files | Source evidence | Integrated evidence |
|---|---:|---|---|
| android_client | 85 | Tracked at frozen SHA | Pending |
| apple_clients | 38 | Tracked at frozen SHA | Pending |
| automation_release | 1 | Tracked at frozen SHA | Pending |
| backend_api | 18 | Tracked at frozen SHA | Pending |
| backend_composition | 7 | Tracked at frozen SHA | Pending |
| backend_tests | 74 | Tracked at frozen SHA | Pending |
| documentation_evidence | 26 | Tracked at frozen SHA | Pending |
| domain | 16 | Tracked at frozen SHA | Pending |
| generated_contracts | 22 | Tracked at frozen SHA | Pending |
| immutable_requirement_sources | 16 | Tracked at frozen SHA | Pending |
| infrastructure | 21 | Tracked at frozen SHA | Pending |
| infrastructure_tests | 7 | Tracked at frozen SHA | Pending |
| interop_server | 13 | Tracked at frozen SHA | Pending |
| interop_tests | 16 | Tracked at frozen SHA | Pending |
| native_shared | 169 | Tracked at frozen SHA | Pending |
| operations_codegen_qualification | 17 | Tracked at frozen SHA | Pending |
| python_sdk_frameworks | 26 | Tracked at frozen SHA | Pending |
| root_build_dependency_configuration | 19 | Tracked at frozen SHA | Pending |
| runtime | 9 | Tracked at frozen SHA | Pending |
| runtime_tests | 31 | Tracked at frozen SHA | Pending |
| security | 7 | Tracked at frozen SHA | Pending |
| services | 18 | Tracked at frozen SHA | Pending |
| storage | 3 | Tracked at frozen SHA | Pending |
| ui_contract | 9 | Tracked at frozen SHA | Pending |
| web_client | 87 | Tracked at frozen SHA | Pending |
| web_tests | 54 | Tracked at frozen SHA | Pending |
| windows_client | 69 | Tracked at frozen SHA | Pending |

Review-era donor suites passed: 327 web unit, 145 focused security, 73 focused runtime and 36 focused scheduler tests. Independent reproductions exposed the five defects despite those passes. Counts are retained review evidence, not integrated results. Donor production-security, grounding, human-usability, memory and native qualification gaps remain explicit; source inventories do not imply completion percentages.

## Closure rules

1. Assign every capability, API/UI/SDK behavior, retained group and source FR an implementation/equivalence mapping with acceptance evidence. Evidence-only files need a reason; product capabilities cannot be reclassified away.
2. Exercise normal integrated dispatch for golden paths, two-owner denials, changed/revoked authority, exact approval, retries, transient-source recovery, scheduling/Stop, skill/note changes, restarts/uncertain effects and representative populated-data recovery.
3. Bind evidence to source/build, real provider/IAM and tested client. Donor fixtures/package scans do not prove authenticated integration or new-user usability.
4. Web milestone requires existing native compatibility and server-owned dispositions. Full feature closure still requires redesigned native qualification and all required capabilities.
5. Institutional IAM, provider records, Plane ownership and no-import decisions stay authoritative unless explicitly superseded.
