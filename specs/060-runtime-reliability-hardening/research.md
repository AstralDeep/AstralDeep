# Phase 0 Research: Runtime Reliability and Release Readiness (060)

All planning unknowns are resolved. Findings were verified against the 2026-07-15 working tree on
`060-runtime-reliability-hardening@ceab9e1c`, the prior 054/057/058 contracts, the committed
constitution, and the reproduced evidence in [review-findings.md](review-findings.md). Product-runtime
security behavior was deliberately excluded; release provenance, CI evidence trust, and protected
approval controls remain in scope.

## R1. One operation and admission authority

**Decision**: Add `orchestrator/work_admission.py` with an `OperationCoordinator` used by connection
messages, background work, scheduler runs, generation, and maintenance. It assigns full UUID4
operation identities, persists lifecycle/ownership, enforces configured active limits, uses finite
queues with maximum wait, returns explicit retryable refusal, and exposes non-sensitive counts/ages.
The current `BackgroundTaskManager` and task-state machinery become compatibility views, not
independent state authorities.

Each UI connection owns a UUID scope with a five-second registration deadline, bounded
pre-registration queue, ordered reader/writer data lane, tracked task set, and terminal drain.
Consecutive reads may overlap, but no admitted read overlaps a mutation because the legacy router
reads shared live state; cancellation and transport control bypass the data lane. Disconnect
immediately rejects queued work, cancels
connection-owned work, and force-drains within five seconds; user/system-owned background work is
not incorrectly cancelled merely because one viewer disconnects.

**Rationale**: `orchestrator.py` currently creates an untracked task per frame and waits indefinitely
for registration; `async_tasks.py` declares a cap but executes through it, truncates UUIDs, and has
no retention purge. A local semaphore in one subsystem cannot give all accepted work the identity,
owner, capacity, retention, cancellation, and terminal semantics required by FR-001..008.

**Alternatives considered**: Patch only `BackgroundTaskManager` with an `asyncio.Semaphore` —
rejected because foreground frames, registration waiters, scheduler, ownership, and disconnect
drain remain unbounded. Make every frame fully serial — rejected because slow read-only work would
block control/cancellation and violate responsiveness.

## R2. Durable scheduled occurrences, leases, and effects

**Decision**: Materialize one `scheduled_occurrence` per `(job_id, scheduled_for)` with a stable UUID,
state, lease token/expiry, retry identity, and attempts. Advance `scheduled_job.next_run_at` in the
same transaction that creates/claims the occurrence. Claim batches with PostgreSQL
`FOR UPDATE SKIP LOCKED`; retry an expired lease with the same occurrence ID. Add
`job_run.occurrence_id` and an effect ledger keyed by `(occurrence_id, effect_kind, effect_key)`.
Thread the ID through operation records, chat/history publication, notification, and audit.

The claim owner starts a dedicated renewal loop immediately after the claim transaction commits,
before the attempt enters the operation queue, and renews at no more than one third of the configured
lease while queued or running. Renewal compare-and-sets `(occurrence_id, claim_generation,
lease_token)` using database time. Losing renewal refuses/cancels that attempt and removes all effect
authority; recovery allocates a new operation/attempt only after database expiry, retaining the
occurrence ID and using fresh claim and operation fences. Thus a 30-second admission wait cannot
outlive an unrenewed 15-second claim.

Scheduled handlers must declare an AstralDeep-controlled idempotency boundary; handlers that cannot
honor it are refused before unattended scheduling. `FF_SCHEDULER_EXECUTION` remains default-off.
For scheduled chat effects, effect publication and the full conversation commit share one fenced
transaction. An explicit owner-validated target chat wins; otherwise the scheduled job's UUID4 is
the stable fallback chat identity across attempts and recoveries.

**Rationale**: `list_due()` is currently a read-only query, `_dispatched` is unused, and the next run
advances only after work. Repeated polls or multiple service instances can therefore submit the
same job more than once. PostgreSQL is already the shared coordination authority.

**Alternatives considered**: Populate the existing in-memory `_dispatched` set — rejected because it
does not survive crash or coordinate replicas. Mark the job complete before running without an
occurrence record — rejected because it loses retry/recovery truth.

## R3. Sticky authoritative BYO host with complete fencing

**Decision**: Give every host, delivery, revision, runtime instance, process, and request immutable
identities. Every tunnel frame and acknowledgement carries the complete generation tuple; the
server accepts only the tuple in the current durable instance row. Host selection is sticky:
retain a healthy incumbent, rebind a reconnect from the same stable host, otherwise keep eligible
hosts on standby and promote the oldest server-accepted standby after loss. Deliver only to the
selected host.

The host emits child/runtime liveness at least once per second. Exit or UI-host loss immediately
terminalizes instance-bound pending calls; five seconds without a valid signal marks a hang and
settles calls within the next two seconds. Bundles and hosts advertise a
`BYO_RUNTIME_CONTRACT_VERSION` plus release-lock digest; incompatibility is a prompt explicit
refusal, not a registration timeout.

**Rationale**: Current delivery broadcasts to every host while routing is keyed only by owner and
agent; a later socket rewrites the `TunnelSocket`. Child exit stays local, stale frames are not
fenced, and pending requests wait for generic timeout after known host failure.

**Alternatives considered**: Last-connected host wins — rejected as unstable and stale-frame-prone.
Key only by agent ID plus heartbeat — rejected because it cannot distinguish revision/process or
settle the right requests.

## R4. Two-phase revision activation, durable deletion, and reconciliation

**Decision**: Persist immutable `user_agent_revision` candidates. Generate and start a candidate
while the active revision/last-known-good runtime remain unchanged; transactionally promote
`user_agent.active_revision_id` only after the candidate is confirmed running and its host bundle is
durably placed. Stop the old instance only after the promotion commit. Promotion failure stops the
candidate and retains the prior pointer/bundle.

Deletion first commits `deleted_at`, disabled state, and a bumped lifecycle generation, then
terminalizes instances/removes routing/sends cleanup. Reconnecting hosts perform an inventory
reconciliation before starting retained bundles and remove refused/deleted revisions.

**Rationale**: The host currently deletes the live directory before `os.replace`, swallows promotion
failure, and can still stop the old process. Server deletion removes routing before the tombstone,
and a delayed registration can restore current maps.

**Alternatives considered**: Repair the current in-place directory swap — rejected because the
server still lacks a durable candidate/active boundary and crash recovery. Delete routing first for
fast UX — rejected because delayed frames can resurrect the row.

## R5. Optimistic authoring concurrency and atomic artifacts

**Decision**: Add immutable UUID-based `agent_id`, `revision`, and generation-claim fields to drafts.
Every save, phase advance, Analyze result, and generation claim uses compare-and-swap
`WHERE revision = expected_revision`, increments the revision, and returns the current revision plus
a refresh action on conflict. Same-name drafts keep distinct storage identities; revising an
existing agent explicitly carries its existing agent ID.

Generate under UUID/revision-specific staging directories, validate fully, flush/fsync, then
`os.replace` into an immutable revision directory. Publish only a small current-pointer update.

**Rationale**: Draft row IDs are UUIDs, but names derive a shared slug and filesystem path; allocation
is a read-then-write check. Phase and Analyze writes are unconditional after stale reads, and files
are written directly into shared targets.

**Alternatives considered**: Per-process locks — rejected because they do not coordinate tabs,
replicas, or restarts. A unique display-name constraint — rejected because same-name drafts are a
valid scenario and identity must not depend on presentation.

## R6. Coordinated startup and independent policy revision

**Decision**: After the minimal `schema_meta` bootstrap, acquire a stable PostgreSQL advisory
transaction lock, re-read schema state, apply the additive idempotent migration once, and commit the
marker in the same transaction. `backend/orchestrator/agent_analyze.py` owns the explicit
`ANALYZE_POLICY_REVISION`; `backend/orchestrator/agent_constitution.py` validates the baked
constitution SemVer and exports the combined `USER_AGENT_POLICY_REVISION` in canonical
`constitution=<semver>;analyze=<positive-integer>` form. The feature-060 initial value is
`constitution=0.1.0;analyze=1`. A missing/malformed component fails startup closed. Store that exact
combined value separately in `schema_meta` and check it on every startup, even when
`SCHEMA_REVISION` is current.

**Rationale**: Two service starters can both enter `_apply_full_schema`; user-agent revalidation is
currently nested inside that full path and compares only major constitution versions, so a policy-
only change can be skipped.

**Alternatives considered**: File/process lock — rejected because replicas do not share it. Tie the
policy marker to `SCHEMA_REVISION` — rejected because they have independent lifecycles.

## R7. Durable maintenance units and atomic synthesis

**Decision**: Claim `maintenance_unit` rows with leases/attempts and explicit input membership using
`SKIP LOCKED`. Per-agent and cross-agent synthesis are separate units. Mark only successful inputs
complete; failures retain retry/error state and identity. Write knowledge output to a same-directory
temporary file, flush/fsync, then atomically replace; rebuild the index only after committed output.
Run process/database/filesystem maintenance in a small dedicated executor.

**Rationale**: `knowledge_synthesis.py` catches unit failures then marks every selected interaction
synthesized and overwrites files directly. Synchronous storage work can also contend with the
interactive executor/event loop.

**Alternatives considered**: Keep a failed-ID set in memory — rejected because crash loses it and
replicas can still overlap. One unit for the whole 500-row batch — rejected because one failure
would unnecessarily replay every successful independent output.

## R8. Immutable runtime registry and bounded process supervision

**Decision**: Replace parallel mutable agent dictionaries at cross-thread read seams with a
lock-protected `runtime_registry.py` that atomically upserts/removes immutable records, increments a
registry version, and returns tuple snapshots. Implement the same documented supervision contract
twice: `backend/shared/process_supervision.py` for server children and
`windows-client/win_agent/process_supervision.py` for the frozen desktop host. Neither imports the
other; parity tests feed both from the neutral tracked corpus
`backend/tests/fixtures/runtime_reliability_060/process-supervision-vectors.json`. Each provides
continuous fixed-size stdout/stderr
reads, bounded line and ring-buffer sizes, exit monitoring, POSIX process groups or Windows process-
tree termination, five-second escalation, thread joins, and explicit pipe closure. Packaged-EXE
tests prove the Windows implementation is actually bundled and executed.

**Rationale**: Worker-thread readers currently iterate `agent_cards` while the event loop mutates it.
Server and Windows child shutdown do not guarantee descendant termination, bounded output, wait, or
pipe closure; the Windows `_kill` implementation ends after `terminate()`.

**Alternatives considered**: Scatter `dict.copy()` at known call sites — rejected because maps can
still describe different versions and new callers can regress. `communicate()` only at shutdown —
rejected because a verbose live child can fill a pipe and deadlock first.

## R9. Atomic conversation snapshots and generation filtering

**Decision**: Add a server-owned `conversation_snapshot` push containing `chat_id`, semantic
transcript, complete ROTE-adapted canvas (including explicit empty), connection generation, request
generation, explicit `snapshot_purpose` (`hydration` or `commit`), and render revision. It is the
only frame permitted to advance committed transcript and
canvas, both for hydration and every live `conversation_commit`; each durable commit increments the
per-chat revision once and emits one complete snapshot. Clients retain the old committed view while
fetching and atomically replace transcript+canvas in one reducer action. Persist only an account-
scoped active-chat locator, written before registration/load; scope the key to an opaque digest of
issuer+subject. Every client target, including Watch, owns a native store with that key/clear contract.
Clear it only for explicit new chat, definitive sign-out/account switch, or confirmed deletion.

Client load/resume and submitted-turn generations are fresh client UUID4s. Detached/REST mutations,
scheduled turns, persisted stream terminals, and long-running-job results instead allocate a fresh
server UUID4 and emit an exact six-field `conversation_commit_ready` value (`type`, integer-one
`schema_version`, `chat_id`, `connection_generation`, `request_generation`, and target
`render_revision`) immediately before the one matching commit snapshot. Exact-key/UUID4 checks,
active-chat/current-connection equality, and a strictly newer revision are mandatory. A malformed,
foreign, stale, or duplicate prelude changes nothing; it also cannot steal an unfinished
client-created commit fence. Loss/refusal of the pair is safe because the durable revision is
observed on later hydration.

The browser remains a thin server-rendered client rather than gaining a second primitive renderer.
After ROTE adaptation for a web socket, the server adds one exact reserved `_presentation` member to
each non-empty top-level component with the canonical rendered fragment and identical effective
workspace flags. The browser validates all of those members before one atomic DOM/state swap; an
empty component array clears without them. Native targets receive no member. The presentation
envelope is excluded from durable workspace state and semantic equality and is never client input.

Existing `ui_*`/stream frames may update a request-scoped transient overlay but never the committed
surfaces or last committed revision; status/progress frames are non-mutating. Direct turns,
component mutations, scheduled turns, persisted stream terminals, detached updates, and long-job
results each publish through one logical `conversation_commit`. Reducers accept a full snapshot
only when chat/connection/request generations match a client-opened load/turn fence or a valid
server prelude. A greater revision always replaces
committed state. The first complete snapshot explicitly marked `snapshot_purpose='hydration'` for a
new `(connection_generation, request_generation)` opened for hydration may also replace at an equal
revision, because reconnect creates a
fresh snapshot identity and ROTE adaptation may differ while the durable commit does not. After that
generation is marked hydrated, an equal-revision replay of the accepted `snapshot_id` is a no-op and
a different identity/content is a conflict; an equal `commit` snapshot or equal snapshot for a new-
turn generation is rejected, and a lower revision is stale. Transient frames use a
strictly increasing sequence rooted at the current base revision. The server validates resume
ownership before snapshot delivery and never sends welcome first for a valid resume.

**Rationale**: Android and Apple explicitly keep `activeChatId` only in memory; web starts with null.
`chat_loaded` and a later optional `ui_render` clear/replace in two stages, and Android decodes only
string transcript content. None of the render frames carries enough identity to reject old output.

**Alternatives considered**: Add a shared hydration ID to the existing two frames — rejected because
empty-canvas, lost-frame, and partial-failure completion remain fragile. Persist transcript/canvas
locally — rejected because the server is authoritative and local copies would drift. Reimplement
the Python primitive renderer in browser JavaScript — rejected because it creates a second UI
semantic authority and can drift from ROTE/server escaping. Add a second HTML frame — rejected
because loss or reordering would restore only half of the committed view. Send an unannounced
server-generated request generation — rejected because a client could not distinguish it from stale
or injected work. Reuse whichever client request generation is current — rejected because detached
work could steal or relabel a live user-turn fence.

## R10. General operation status and responsive Apple LLM setup

**Decision**: Add `operation_status` with operation ID, action/surface/chat scope, generation tuple,
canonical state (`accepted`, `validating`, `persisting`, `running`, `completed`, `failed`,
`cancelled`, `retryable`), safe label/error code, and terminal/retryable flags. Clients create an
operation/request ID and acknowledge locally before sending. Exactly one server terminal wins;
stale/late terminals are ignored.

Apple's generic `ParamPicker` uses the contract for `chrome_llm_save`: immediate busy feedback,
single flight, editable preserved fields on failure, accessible phase/status, and responsive
navigation/focus/window/scene behavior. The server uses a ten-second outer attempt deadline and a
probe budget below it (eight seconds) so persistence, unlock, and terminal delivery fit. Invalid
credentials are corrective; provider/network failure is retryable. A late success after the owned
deadline is not permitted.

**Rationale**: The Apple form currently emits and presents no in-flight or terminal state, while
Apple deliberately ignores `llm_config_ack`. The backend probe timeout is 15 seconds, already beyond
the specification's ten-second bound.

**Alternatives considered**: Extend only `llm_config_ack` — rejected because the same ambiguous
progress/terminal problem exists across long operations and FR-043 requires a shared contract.
Block the main actor until save completes — rejected because it reproduces the release blocker.

## R11. Canonical personal-agent lifecycle status

**Decision**: Add `agent_lifecycle` with agent ID, authoritative runtime/revision/generation and one
state vocabulary: `starting`, `online`, `updating`, `failed`, `offline`. All clients handle it,
reject stale generations, and refresh/patch the shared authoring surface. Keep legacy
`agent_offline` temporarily as a compatibility signal but do not use it as the new state authority.

**Rationale**: `agent_list` currently labels connected indiscriminately, Android/Apple ignore
`agent_offline`, and authoring clients retain stale state until reload.

**Alternatives considered**: Push the whole authoring surface on every state transition — rejected
as wasteful and still unable to fence stale updates without generations.

## R12. One bundled immutable Windows deployment profile

**Decision**: Add a frozen `DeploymentProfile` resolver and bundle one reviewed non-secret JSON
profile for Windows 0.4.0. Select whole profiles in this order: explicit managed or
`--deployment-profile` override, permitted atomic QSettings profile, bundled release profile,
explicit development-only defaults. Validate/freeze once before constructing Qt, transport, auth,
or agents; `windows-client/win_agent/agent.py` receives that same immutable object for endpoint,
mode, and disposition and never rereads environment/default values. Never merge partial overlays or
reread mutable environment later.

The production profile uses the authenticated UI tunnel for BYO agents and therefore contains no
shared agent credential. The legacy external Windows-tools process stays disabled unless the
existing managed `AGENT_API_KEY` source is explicitly present. A pre-Qt
`--validate-deployment` mode reports only non-sensitive profile/release/source/digest/disposition
facts. Connection failure retains the profile and offers retry with redacted diagnostics.

**Rationale**: Current clean launch intentionally prompts; CLI values are overwritten by later
resolution; app and agent read separate mutable settings. `AstralDeep.spec` bundles no profile.

**Alternatives considered**: Bake environment variables or a shared key into the executable —
rejected as mutable/secret and contrary to the approved tunnel path. Continue merging individual
CLI/settings/env fields — rejected because it creates internally inconsistent deployments.

## R13. Windows 0.4.0, strict updates, and a reproducible runtime

**Decision**: Release the feature-058 host and 060 profile as semantic minor version 0.4.0, leaving
v0.3.0 immutable. Compare versions with a strict stdlib SemVer parser, including rejection of
leading-zero numeric prerelease identifiers (newer/equal/older/prerelease),
and expose an explicit verified-update action. Add a direct input manifest plus a complete
Windows/Python-3.11 hash lock including exact PyInstaller, sigstore, and `astralprims`; install with
`--require-hashes`, run `pip check`, record the resolved metadata/digest, and compare two clean
resolutions.

The Windows release order is build → clean-profile profile validation → frozen worker round trip →
GUI no-dialog/chat-host smoke → evidence check → hash/sign → immutable publication. Existing release
assets cannot be overwritten.

**Rationale**: v0.3.0 predates the host; the client treats any unequal remote version as newer;
`sigstore>=3,<4` and `astralprims>=0.2.0` make the claimed exact release set drift.

**Alternatives considered**: Patch 0.3.0 assets — rejected because signed release identity is
immutable. Lock only direct dependencies — rejected because transitive changes still alter the EXE.

## R14. Truthful examples, docs, apply behavior, and accessibility

**Decision**: Change the curated dice request to exactly six six-sided dice and normalized results
to include `sides: 6`/notation; automatically validate prompt bounds and narrative record fields.
Explicitly unignore and track `docs/byo-client-agents.md`; add `make apply-config` using Compose
recreate plus a non-sensitive effective-setting verification; check links across `git ls-files`.
Give changed Android/Apple/web controls stable accessible name, role, state, focus, and live status.

**Rationale**: Welcome asks for 6d20 while the tool hardcodes d6; the guide is ignored/missing;
`make restart` cannot reload `.env`; two Android switches are unnamed.

**Alternatives considered**: Extend dice to arbitrary sides/chart — valid but unnecessarily expands
the tool contract for a curated-copy defect. Keep docs ignored and force-add one file — rejected
because future updates can silently disappear again.

## R15. Android next-major compatibility

**Decision**: Remove `android.builtInKotlin=false` and `android.newDsl=false`, migrate off the
`kotlin-android` plugin/obsolete variant APIs and Project-object dependency notation, and use the
covered Python driver `scripts/run_android_next_major_canary.py` in an isolated temporary checkout.
As of 2026-07-16, AGP 10 is scheduled for late 2026 and neither AGP 10 nor Gradle 10 has a stable
public release, so the tracked declaration says `unreleased` instead of inventing versions or a
wrapper checksum. Alpha, beta, release-candidate, milestone, nightly, and snapshot artifacts do not
qualify. The default command fails closed; CI's explicitly selected diagnostic checks Google's AGP
metadata and Gradle's official versions feed and fails only when both stable major releases exist.
Spec 060 does not activate or adopt them. A separately authorized future change may replace the
sentinels with exact stable versions, official distribution URL, and SHA-256, then run the true
major-asserting canary before changing the shipping toolchain.

**Rationale**: Current builds pass, but the remaining warning originates in the isolated Kover test
plugin rather than shipping source. A truthful unavailable declaration preserves the strict future
gate without turning guessed coordinates into false release evidence.

**Alternatives considered**: Upgrade the shipping toolchain immediately — rejected because a canary
should expose incompatibilities without coupling remediation to an unproven production upgrade.

## R16. Same-SHA artifact evidence and protected CI publication

**Superseding owner decision (2026-07-16)**: Release-evidence collection, normalization, and parsing
must run locally before push and emit diagnostic canonical evidence/digests only. Protected CI must
independently validate those bytes and trusted run/job/artifact identities before authorization.
The bounded exception/debt semantics below remain, but their protected approval, registration, and
resolution are native environment-approved GitHub Actions jobs using the built-in short-lived
`GITHUB_TOKEN`. Release publication remains there as well. Repository-scoped GitHub Apps,
installation tokens, and a custom token broker are rejected; references below to App-issued identity
or brokered tokens are historical design context, not implementation authority.

**Decision**: Add a release-readiness matrix whose browser, build-once packaged Windows, connected Android,
macOS, iOS, and watchOS jobs all emit schema-validated JSON bound to candidate SHA and artifact
digest. Each check carries typed measurements and immutable raw-evidence references so trial counts,
percentiles, maxima, rates, and zero-violation claims are machine-verifiable rather than buried in
Markdown.
Aggregate only same-candidate evidence for sign-in, rendered chat, reconnect/resume, lifecycle, and
applicable authoring/hosting. A passed platform requires every mandatory check to be `passed`; only
the policy's explicit capability-authorized checks may be `not_applicable` (currently Watch
authoring and macOS hosting when the candidate map says unsupported). An unavailable affected
platform fails unless an owner-approved exception request names the missing evidence and candidate
and the resulting approval expires within seven days. Even then, an always-running control producer must retain the exact built
artifact and qualifying-stage identity and bind an independently re-hashed observation of the
attempted target runner/platform failure. Approval is valid only when the protected
`release-evidence-exception` request exposes the immutable exception artifact ID/digest before an
allowlisted release owner other than the requester approves it, the same-repository API payload is
re-queried, and a separately pinned registrar appends canonical debt bytes create-only to the
protected non-force-push `refs/heads/release-evidence-debt` ref. A protected-builder-attested approval
manifest binds the exact request bytes, actual reviewer/time/expiry, parent/new ledger commits,
unique entry path, and entry digest/reference; guessed `approved_by`, candidate-tree debt, or a post-
request mutation proves nothing. The protected final decision requires that completed registration.
On every run the validator reads one exact protected-ledger commit; a prior
`blocks_next_release=true` debt is resolved only by a current passing result for each formerly missing
platform/check and a create-only `resolutions/<resolution_id>.json` record plus attested
`trusted_debt_resolution` receipt bound to that later evidence/provenance. A resolution applies only
to its named debt, so a later outage for the same check creates a fresh debt. Apple first-login macOS and iOS evidence is categorically
non-waivable for the rejection-remediation release.
The ledger registrar proves each new commit is a direct descendant of the verified protected parent,
that every prior debt/resolution is byte-identical, and that exactly one previously absent UUID path
under `debts/` or `resolutions/` was added;
candidate SHA calculation never includes the current exception request or debt entry.

Every raw artifact is either bundled beneath the evidence root with a traversal-safe `bundle://`
reference; identified by canonical same-repository `gh://owner/repo/runs/<run>/attempts/<attempt>/
artifacts/<artifact>/members/<member>` workflow provenance; identified after publication by canonical
`gh://owner/repo/releases/<release-id>/assets/<asset-id>` provenance; or identified by a digest-
qualified `oci://registry/repository@sha256:<digest>` reference. A protected reusable trusted-
builder/verifier workflow is pinned independently of the candidate by repository rules and protected-
environment signer digest/certificate identity. Each producer job, stage deployment, used
exception approval, and debt resolution emits its own trust manifest. The protected verifier reconstructs each exact
current-run job/artifact identity from GitHub API state, verifies attestations and subject bytes,
schema-validates `release-trust.schema.json`, and runs policy/coverage code from its protected pinned
revision rather than the candidate checkout. It reads the protected ledger head before/after
evaluation, rejects stale/concurrent movement, and emits an attested `trusted_release_decision` that
binds every consumed manifest ID to its canonical run/artifact/member and re-hashed bytes, exact
ledger commit/tree/canonical debt+resolution snapshot, and bounded `valid_until`. Repository rules
require the installed protected workflow identity rather than a name-only check; a candidate
aggregate or same-name job cannot substitute. Manifest-
declared workflow/builder values are cross-checks, and a candidate-controlled workflow ref, filename,
embedded hash, policy result, or verdict is not trusted.
Before qualifying execution, a first reviewed landing installs this verifier, its policy/all three
schemas, the bridge-signer template, protected registrar/debt ref, and protected publisher/controller
using only the built-in job token on the protected default branch; it configures reviewer/tag/ref
gates; and records immutable
identities without enabling the automatic candidate caller or required check. The candidate rebases
onto and verifies that root, and only a second checkpoint enables the caller/required check. The
verifier executes a fresh archive extracted from the pinned commit, not a same-HEAD dirty working
tree or a bootstrap run that is simultaneously installing its own trust root.
The protected validator derives each canonical URI from verified manifest fields, resolves allowed
references, rejects HTTP/mutable/unknown schemes and symlinks/path traversal, and recomputes the
referenced bytes' SHA-256. It matches each platform report's run/attempt/job/runner to exactly one
attestation-verified producer manifest, and compares staging identity and endpoint only to the
verified `stage-deploy` manifest before any probe. Windows candidate build emits one unsigned EXE and
provenance manifest; the matrix exercises those bytes, and protected publication re-hashes them
without rebuilding.
The passing readiness set remains a pre-sign decision. Intended publication authority belongs to an
owner-approved protected publisher whose exact workflow SHA is checked before its job-scoped built-in
`GITHUB_TOKEN` is used; no GitHub App or installation token exists. T120 must still enforce trusted-only
signer-tag creation before moved-main or candidate workflows can be excluded repository-wide. Because shipped v0.3.0 accepts
only the Sigstore SAN for `release-windows.yml@refs/tags/<tag>`, the publisher preserves that trust
root through an exact-byte-pinned compatibility bridge. It refuses collisions, creates exactly
`v0.4.0` at the protected-decision SHA, proves the candidate bridge blob equals the installed
template, and lets that contents/actions/attestations-read plus OIDC bridge retrieve T068 by exact originating
run/attempt/artifact ID, re-hash it, and sign those exact EXE bytes under the legacy SAN.
The bridge cannot publish. Before mutation and transition, the protected publisher re-opens the
decision/approval/ledger inputs and rejects current time at or after decision `valid_until` or any
used approval expiry. It verifies the bundle with the actual v0.3.0 policy,
creates `SHA256SUMS`, uploads exactly `AstralDeep.exe`, `SHA256SUMS`, and `cosign.bundle` create-only to
a new draft, resolves/re-downloads each numeric asset ID, validates exact protected decision and
publisher approval/identity plus draft state/count, hashes, release name equal to tag, latest-on-
publish disposition, tag, and target in `windows_draft_verification_provenance`, and only then makes
it public as latest. Official mode re-queries the API-shaped `/releases/latest` result consumed by
v0.3.0 before success. Failure deletes the just-
created tag/draft before publication. Isolated disposable mode never creates an official tag.

**Rationale**: Source tests cannot prove frozen workers, bundled profiles, browser auth, connected
Android, or Apple release behavior. Existing workflows are fragmented and do not aggregate one
candidate evidence set.

**Alternatives considered**: Treat another client as equivalent — rejected by the spec and
constitution. Store results only in workflow logs — rejected because they are not machine-bound to
the candidate or aggregatable.

## R17. Qualifying staging and validation tooling

**Decision**: Build the candidate container once, publish its immutable OCI digest, and have a
designated configured staging host deploy that exact artifact into a request-namespaced Compose
environment. It uses pinned Keycloak, PostgreSQL restored from the tracked sanitized synthetic
`representative-057.sql` fixture and migrated only by normal startup, plus the product's real
background and scheduler paths. A non-secret `keycloak-realm.json` fixture defines PKCE clients;
runtime users/passwords come only from staging secrets. `fixture-manifest.json` binds hashes,
source revision, synthetic provenance, sanitization assertions, and required representative rows,
and a test fails for secrets, missing coverage, or fingerprint drift.

The deploy job emits a TLS endpoint, request namespace, candidate image digest, data fingerprint,
capability digest, and service identities, then leaves the deployment alive. Local pre-push tooling
collects and parses the canonical evidence and its digests without authorization. Protected CI
reconstructs/downloads exact artifact IDs from the current run and accepts staging identity only
after repository/candidate, subject-digest, and producer-runner verification, never from an
evidence-controlled file, URL, or committed local verdict.
Digest-pinned Playwright and the separate
Windows, Android, and Apple runners require that job, consume its reachable endpoint, and repeat the
same identity in every report. A final `if: always()` cleanup job waits for the complete matrix before
removing the namespace. Missing credentials or reachability blocks the release gate; no localhost,
mock-auth, source-only, or empty-database fallback qualifies.

The standard-library release validator implements every assertion keyword used by the tracked
schemas: local `$defs`/`$ref`, types and union types, object/array/string/number
constraints, composition/conditionals, `contains`, and active UUID/date-time/URI formats. Remote
references, unknown assertion keywords, duplicate JSON keys, non-finite numbers, and excessive
input/nesting fail closed; mutation tests exercise every supported keyword and branch. Policy
validation, beyond schema shape, rejects duplicate check IDs within a platform document and duplicate
platform reports within an evidence set.

**Rationale**: Constitution v2.8.0 permits ephemeral staging but requires the actual candidate, real
dependencies, representative migrated data, affected clients, candidate-bound evidence, local
pre-push preparation, and independent protected-CI authorization.

**Alternatives considered**: Call ordinary local pytest plus an empty database “staging” — rejected
by Principle X. Start Compose on the Linux job and give other hosted runners `localhost` — rejected
because the deployment disappears with that runner and is not reachable from Windows/Apple/Android.

## R18. Maintained-language lint and changed-code coverage

**Decision**: Keep one isolated, lock-pinned `tooling/web-ci` Node.js 24 manifest for ESLint and the
Playwright harness. Browser execution uses the official Playwright image whose version matches the
lock and whose full image digest is tracked in `playwright-image.txt`, pinning Chromium and its Linux
runtime dependencies; no host/system-browser fallback exists. The tracked ESLint configuration
covers maintained backend-served JavaScript. Apple CI uses the installed Xcode toolchain's
`xcrun swift-format lint --strict
--recursive` so no product or third-party runtime dependency is added. Existing Ruff and Android
lint remain mandatory.

Each platform job emits a machine-readable coverage report from the tests that exercise changed
code: backend Python XML, separately measured root `scripts/*.py` tooling XML, Windows Python XML,
Playwright V8 precise coverage converted by the exact lock-pinned `v8-to-istanbul` producer and
filtered through a directly pinned JavaScript parser into canonical executable-statement Istanbul
JSON, Android app/core Kover XML, and Apple app/core/Watch coverage from real test targets. Neither
raw V8 byte ranges nor unfiltered `v8-to-istanbul` physical-line output is executable-line evidence:
a top-level executed range may span blank lines, comments, source directives, and other
non-executable text. The protected collector rejects both noncanonical shapes, and the pinned-image
producer regression proves comment padding cannot raise coverage. All executable release
orchestration, including the Android canary, is Python and must appear in the tooling report.

`scripts/check_changed_coverage.py` parses a null-delimited diff from an immutable event-aware base
(PR base SHA, main-push `before`, or verified manual ancestor), records both SHAs, and obtains each
maintained added/modified file's hunks with Git text mode so candidate `.gitattributes` cannot hide
them with `-diff`, `binary`, or a custom diff driver. A maintained added/modified path with no parsed
hunk fails closed. The collector maps executable lines to the native or converted reports, rejects a
missing/unparseable/unmapped applicable report and an unexpected empty executable diff, merges repeat
platform observations by source path/line, and requires at least 90% for each changed maintained
language and for the combined unique changed executable lines. Test sources and generated/vendor/
build/declarative configuration are excluded only by explicit tested path/line rules, never by a
blanket language or platform waiver.

`ci.yml` invokes the reusable readiness workflow for every PR and main push and supplies the immutable
base/candidate identities. All platform reports feed one named required aggregate that runs both
release-evidence and changed-code policy; main publication depends on it. Manual dispatch remains for
release-candidate reruns but requires an explicit verified base SHA.

**Rationale**: The constitution's changed-code gate applies to all code changed by the feature, not
only backend Python. Producing reports beside the native tests and aggregating them once preserves
clear platform diagnostics while preventing a large well-covered language from hiding an uncovered
one.

**Alternatives considered**: Enforce only an overall percentage — rejected because it masks a weak
platform. Require repository-wide coverage from every native tool — rejected because the governing
metric is changed executable lines and repository-wide legacy coverage is separately aspirational.

## R19. Executable feature-059 applicability signal

**Decision**: Determine macOS-hosting applicability from one immutable server-owned build-capability
map returned identically through authenticated `GET /api/dashboard` and `system_config.config`:
`capabilities.personal_agent_host.macos = {supported, runtime_contract_versions, source_feature}`.
Feature 060 publishes `{false, [], null}`; feature 059 changes it to `{true, [2], "059"}` only when
its implementation lands. Candidate staging records that sanitized subtree and canonical digest.
Missing/malformed is unknown and blocks the gate. `supported=false` makes only the distinct macOS-
hosting check not applicable. `supported=true` additionally requires the exercised direct-download
macOS artifact to advertise the structured v2 `register_ui.agent_host` object, receive server-issued
`agent_host_registered`, and pass the host flow; malformed/refused/missing acknowledgement is failure.

**Rationale**: Feature 059 may be absent, partially implemented, or merged independently. A branch
name, directory, or manually authored report cannot prove that the exercised artifact actually
advertised and negotiated hosting with the candidate server.

**Alternatives considered**: Detect `specs/059-macos-agent-host/` — rejected because the spec exists
before implementation. Infer support from the client-declared legacy `agent_host` boolean or live
connection count — rejected because it is ephemeral and cannot distinguish unsupported from broken.
Let the Apple report self-declare applicability — rejected because the release aggregator needs a
candidate-owned, artifact-bound source of truth.
