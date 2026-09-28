# Feature Specification: Runtime Reliability and Release Readiness

**Feature Branch**: `060-runtime-reliability-hardening`

**Created**: 2026-07-15

**Status**: Implementation and verification in progress

**Input**: User description: "Review current main, including merged feature 058, for current and future non-security problems; exercise the Android, Windows, and web clients end to end; make the Windows release ship with its deployment connection profile instead of showing the first-run Configure AstralDeep dialog; and create the next collision-safe remediation specification. Security behavior is explicitly out of scope."

## Owner Decisions (2026-07-16)

- Spec 060 does not adopt AGP 10 or Gradle 10. Activation remains blocked until stable public
  releases of both tools exist and a separately authorized future compatibility change begins;
  alpha, beta, release-candidate, milestone, nightly, and snapshot artifacts do not qualify.
- Release-evidence collection, normalization, and parsing run locally before push and remain
  diagnostic. Protected CI independently validates canonical evidence, identities, digests, and
  policy before authorization.
- Release verification and publication remain in GitHub Actions with protected environments,
  immutable workflow identity, and the built-in short-lived job token. Spec 060 does not create or
  require repository-scoped GitHub Apps, installation tokens, or a custom token broker.
- The existing bounded platform-unavailability policy remains available only through protected,
  environment-approved CI using the built-in job token. Local evidence cannot approve an exception,
  and no repository-scoped GitHub App, installation token, or custom broker is introduced.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Work completes once under load (Priority: P1)

A user sends messages, starts background work, or relies on scheduled work while the service is
busy or reconnecting. Accepted work stays within declared capacity, completes once, and reaches a
clear terminal state. Closing a client releases everything owned by that connection instead of
leaving invisible work behind.

**Why this priority**: Current stress reproductions show that declared limits can be exceeded,
scheduled work can be submitted twice, and disconnected clients can leave pending work. These are
direct correctness and availability failures.

**Independent Test**: Drive concurrent messages, background jobs, repeated scheduler polls, and
disconnects against one deployment; verify the configured concurrency ceiling is never exceeded,
every accepted operation has one terminal result, each scheduled occurrence has at most one visible
effect, and connection-owned work drains after disconnect.

**Acceptance Scenarios**:

1. **Given** the service is at its configured work limit, **When** additional work arrives,
   **Then** it is either admitted to a bounded queue or refused with a retryable result, and active
   work never exceeds the limit.
2. **Given** the same scheduled occurrence is observed by repeated polls or multiple service
   instances, **When** it is dispatched, **Then** users observe one effect and every retry retains
   the same occurrence identity.
3. **Given** a client has incomplete registration or in-flight work, **When** its connection closes,
   **Then** all connection-owned waiters and work reach a terminal state within five seconds.

---

### User Story 2 - BYO agents remain honest through failure and revision (Priority: P1)

An owner runs a personal agent on a desktop client. If its child process crashes, its host drops,
another host connects, or a revision fails, the system reports the real state promptly. It never
routes to a stale process, duplicates the same agent across hosts, loses the last working revision,
or makes a caller wait for a timeout that is already inevitable.

**Why this priority**: Feature 058 introduced a distributed lifecycle whose happy path passes, but
current instance identity, failure propagation, and promotion behavior do not fully cover child
death, multiple hosts, disconnects, or failed file promotion.

**Independent Test**: Run one benign personal agent, introduce child crash, child hang, host loss,
two connected hosts, stale acknowledgements, deletion races, and failed revision promotion, then
verify one authoritative instance, prompt terminal calls, and preservation of the last working
revision.

**Acceptance Scenarios**:

1. **Given** a hosted agent is running, **When** its child exits or stops responding while the
   desktop app remains connected, **Then** the agent becomes offline and all calls assigned to that
   instance terminate within two seconds for an exit and within seven seconds for a detected hang.
2. **Given** two eligible desktop hosts are connected, **When** an agent is delivered or restored,
   **Then** exactly one current host instance is authoritative and stale host frames cannot change
   its state.
3. **Given** a working agent receives a revision, **When** the revision fails to start or cannot be
   promoted durably, **Then** the prior revision remains available and is not removed.
4. **Given** an agent has been deleted, **When** a delayed registration or reconnect arrives,
   **Then** the deleted agent stays deleted and the delayed instance is stopped.

---

### User Story 3 - Resume the same conversation after interruption (Priority: P1)

A user can move through a temporary network loss, service restart, app backgrounding, or mobile
process recreation without being returned to a new welcome screen. The active conversation,
transcript, and last committed canvas return together and do not mix with delayed output from a
different turn or connection.

**Why this priority**: Live Android testing reproduced an authenticated process restart that lost
the active chat and showed the welcome experience, and reconnect hydration displayed fewer visible
messages than the stored transcript.

**Independent Test**: Complete a rendered turn on every client, interrupt the connection, restart
the service, terminate and recreate the mobile app process, and reorder or omit acknowledgement and
render frames; verify the intended chat and semantically equivalent committed UI are restored.

**Acceptance Scenarios**:

1. **Given** an authenticated user has an active chat with a rendered canvas, **When** the Android
   process is terminated and recreated, **Then** the same chat, transcript, and canvas return within
   five seconds without showing a new-chat welcome.
2. **Given** a reconnect is hydrating an existing chat, **When** delayed output from an earlier
   connection or another chat arrives, **Then** it is ignored and cannot replace the visible chat.
3. **Given** stored transcript content contains structured assistant output, **When** any client
   reloads that chat, **Then** the visible transcript remains semantically equivalent rather than
   becoming blank or losing turns.
4. **Given** a scheduled turn, persisted stream terminal, detached mutation, or long-running job
   commits while the intended chat is active, **When** no client-originated turn generation exists
   for that update, **Then** the server announces one fresh commit generation with the exact
   six-field `conversation_commit_ready` prelude and the client applies only its one complete paired
   snapshot without exposing a partial transcript or canvas.

---

### User Story 4 - Install a ready-to-use Windows release (Priority: P1)

A user downloads the official Windows client, launches it on a clean profile, signs in, and uses
AstralDeep without being asked to supply service URLs or agent credentials. The distribution arrives
with the intended deployment profile and any required agent connection credential already available
through the existing provisioning path, and all parts of the client use that same resolved profile.

**Why this priority**: The current clean-client behavior deliberately opens a Configure AstralDeep
dialog, while the user requires the distributed production application to ship ready for its
deployment. The merged BYO host also cannot be published under the already-used client version.

**Independent Test**: Install the packaged Windows artifact in a clean operating-system profile
with no saved settings or runtime overrides; verify no configuration dialog appears, the main
window opens with the release-provided authority, service endpoint, and agent connection profile,
and a normal chat plus hosted-agent smoke test succeeds.

**Acceptance Scenarios**:

1. **Given** a clean profile and the official production artifact, **When** the user launches it,
   **Then** the main window opens without the Configure AstralDeep dialog and uses the complete
   release-provided deployment profile.
2. **Given** an allowed administrator or command-line override, **When** the client resolves its
   effective profile, **Then** the documented precedence is deterministic and every transport and
   hosted-agent component uses the same immutable result.
3. **Given** a production artifact has a missing, placeholder, inconsistent, or development-only
   deployment value, **When** release validation runs, **Then** publication stops before signing.
4. **Given** an existing released client checks for this release, **When** versions are compared,
   **Then** the new artifact has a distinct semantic version, the prior release remains immutable,
   and the upgrade is offered and verifies correctly.

---

### User Story 5 - Finish Apple first-login LLM setup responsively (Priority: P1)

A first-time macOS or iOS user enters their LLM API credentials and saves them. The app responds
immediately, remains interactive while validation and persistence are in progress, explains what it
is doing if the work is not nearly instantaneous, and advances to the next page when the credentials
are accepted. The user is never left on an apparently frozen form.

**Why this priority**: Both Apple apps were rejected from App Store review because the reviewer could
not proceed after saving LLM credentials. This blocks distribution and prevents every first-time
user from reaching the product when reproduced.

**Independent Test**: On supported macOS and iOS review devices with an active internet connection,
complete first login with valid, invalid, slow, and temporarily unavailable provider responses;
verify immediate feedback, continued UI responsiveness, bounded completion or failure, and correct
navigation to the next page after success.

**Acceptance Scenarios**:

1. **Given** a first-time Apple-app user has entered valid LLM API credentials with an active
   connection, **When** they choose Save, **Then** the control acknowledges the action within 250
   milliseconds, the UI remains responsive, and successful setup advances to the next page.
2. **Given** credential validation and saving take longer than one second, **When** work is still in
   progress, **Then** the app shows a visible loading state that identifies the current user-facing
   phase and remains responsive until a terminal outcome.
3. **Given** validation, persistence, or provider connectivity cannot complete within ten seconds,
   **When** the attempt reaches that bound, **Then** the loading state ends with a clear retryable
   result and the user can retry or edit the form without restarting the app.
4. **Given** the App Store review environment described in this feature, **When** the macOS and iOS
   release candidates repeat the first-login flow, **Then** neither app becomes unresponsive and both
   can proceed beyond credential setup.

---

### User Story 6 - Author and maintain agents without race corruption (Priority: P2)

An owner can work on an agent from multiple tabs or devices while service replicas and background
maintenance are active. Concurrent edits produce an explicit conflict or a well-defined winner;
they do not silently overwrite state, share storage with another draft, resurrect deleted agents,
or mark failed maintenance work as complete.

**Why this priority**: Current authoring transitions, slug allocation, publication, schema startup,
and knowledge synthesis contain read-then-write or partial-failure paths that become unsafe under
ordinary concurrency or restart.

**Independent Test**: Race same-name draft creation, phase advancement, analysis, generation,
deletion, registration, two service starters, and partially failing synthesis; verify isolated
identities, version conflicts, atomic publication, single migration ownership, and retryable failed
work.

**Acceptance Scenarios**:

1. **Given** two clients edit the same draft revision, **When** both advance it, **Then** one accepted
   transition is recorded and the other receives a refreshable conflict instead of overwriting it.
2. **Given** two same-name drafts are created concurrently, **When** they generate artifacts,
   **Then** each has a distinct durable identity and one draft cannot overwrite the other.
3. **Given** only part of a maintenance batch succeeds, **When** the cycle completes, **Then** only
   successful units are marked complete and failed units remain retryable with their error state.
4. **Given** two service instances start against the same older data store, **When** startup updates
   run, **Then** one coordinated update completes and both instances reach the same valid revision.

---

### User Story 7 - Trust examples, progress, status, and operating guidance (Priority: P2)

A user can rely on built-in examples, generated explanations, lifecycle status, and progress
messages to describe what the system can actually do. An operator can enable merged capabilities
using documented commands whose effects are verifiable after restart.

**Why this priority**: A shipped welcome example requests behavior the advertised tool cannot
perform, lifecycle status is not handled consistently by every client, long turns can appear
stalled, and a referenced BYO operating guide is absent while the documented restart does not load
changed deployment values.

**Independent Test**: Execute every curated example, compare visible explanations with tool
results, exercise agent online/offline changes on each client, run the documented enablement flow,
and validate every tracked documentation link.

**Acceptance Scenarios**:

1. **Given** a user selects a curated example, **When** it runs, **Then** its request is supported or
   explicitly constrained and the explanation agrees with the actual tool inputs and results.
2. **Given** an operation takes longer than two seconds, **When** it is still progressing, **Then**
   every affected client shows meaningful progress and later shows one clear terminal outcome.
3. **Given** an operator changes a boot-time capability setting, **When** the documented apply
   command completes, **Then** the running service uses the new value and exposes a non-sensitive
   confirmation.
4. **Given** an agent changes lifecycle state, **When** each supported client receives the change,
   **Then** all clients show the same current state without requiring a full reload.

---

### User Story 8 - Prove release behavior before publication (Priority: P2)

A maintainer receives candidate-bound staging evidence for the actual artifacts and supported
clients before a change can ship. Source-only tests are supplemented by a qualifying isolated
candidate deployment, a clean packaged Windows run, a real browser flow, a connected Android flow,
and equivalent Apple coverage on an available Apple runner.

**Why this priority**: Current pipelines do not regularly exercise the packaged Windows worker,
connected Android behavior, or live browser authentication path before release, allowing packaging,
configuration, and cross-client continuity defects to escape source tests.

**Independent Test**: Run the release candidate through its platform matrix from clean profiles and
verify sign-in, one ordinary chat turn, reconnect/resume, lifecycle state, and a benign personal-agent
round trip where the client supports hosting.

**Acceptance Scenarios**:

1. **Given** a pull request changes runtime, protocol, packaging, or client behavior, **When** its
   release gates run, **Then** the same immutable candidate is deployed with real configured
   authentication, representative migrated data, and real workers, and every affected shipping
   client completes its smoke flow before merge.
2. **Given** the Windows candidate artifact, **When** it runs from a clean profile, **Then** both the
   normal application and packaged personal-agent worker complete their smoke checks and terminate
   cleanly.
3. **Given** an affected client platform is temporarily unavailable, **When** validation runs,
   **Then** the release remains blocked unless the existing candidate-bound, seven-day protected-CI
   exception and debt-resolution policy is satisfied; another client is never equivalent evidence.
4. **Given** release evidence has been collected, **When** a maintainer prepares a push, **Then** the
   deterministic local parser validates and canonicalizes it without claiming authorization, and
   protected CI later validates those canonical inputs independently before release.

---

### User Story 9 - Defer future toolchains without losing readiness (Priority: P3)

A maintainer can prove that the next Android major toolchain is not yet eligible for adoption,
retain a stable-release-only activation path for a future authorized change, and ensure users
relying on assistive technology can identify every interactive control.

**Why this priority**: The current Android toolchain is supported and shipping. Premature adoption
of alpha or milestone major releases would create avoidable instability, while removal blockers and
unnamed controls still need to be eliminated before any future activation.

**Independent Test**: Run the official-availability diagnostic and automated accessibility
inspection on every changed surface. Verify that prereleases do not activate the canary, the
declaration remains `unreleased` until both stable public majors exist, known source blockers are
absent, and no scoped control is unnamed. The true next-major canary remains a future activation
gate and is not a Spec 060 release requirement.

**Acceptance Scenarios**:

1. **Given** either next-major tool has no stable public release, **When** readiness is checked,
   **Then** prerelease artifacts are ignored, the stable-release sentinel remains unavailable, and
   the shipping toolchain is unchanged.
2. **Given** an interactive authoring surface, **When** it is inspected through accessibility
   semantics, **Then** every control has a stable role, name, state, and focus behavior.
3. **Given** a mobile text field has invoked the system keyboard, **When** the user completes entry
   or dismisses it, **Then** the native iOS or Android keyboard action and dismissal gesture are
   used and no application-drawn Done accessory overlays the keyboard.

### Edge Cases

- A client floods messages before completing registration, disconnects during registration, or
  reconnects while old connection work is still completing.
- Work reaches capacity while its owner cancels, disconnects, or submits a duplicate retry.
- A scheduled occurrence is claimed immediately before a service crash, its lease expires, or its
  visible effect succeeds while acknowledgement is lost.
- A desktop host stays connected while its child exits, hangs, writes no output, or is replaced by a
  second host.
- A stale agent acknowledgement, result, heartbeat, or disconnect arrives after a newer revision or
  host instance is active.
- Revision preparation succeeds but durable promotion fails at each intermediate point, including
  loss of power between staging and activation.
- Delete, registration, generation, and revision overlap for the same agent.
- Two owners choose the same agent name, or one owner creates two same-name drafts concurrently.
- A mobile process is recreated before the first-turn chat acknowledgement arrives, during
  hydration, or after the server has committed structured output.
- A canonical hydration snapshot is delayed, incomplete, invalid, or scoped to an old connection or
  request generation; any bounded-compatibility trailing render arrives late and may affect only a
  disposable overlay, never the committed transcript or canvas.
- A malformed, extra-field, foreign-chat, stale-revision, or duplicate
  `conversation_commit_ready` arrives, or a valid server-originated prelude arrives while an
  unfinished client commit owns the fence; it cannot replace/steal that fence or mutate committed
  state, and the durable update remains recoverable through hydration.
- A clean Windows profile is offline, the deployment is temporarily unavailable, or the local
  settings schema predates the bundled deployment profile.
- An Apple user presses Save more than once, backgrounds the app, changes focus or window size,
  loses connectivity, receives an invalid-provider response, or retries while the first LLM
  credential attempt is still resolving.
- Apple credential validation succeeds but persistence or navigation fails, or the save completes
  immediately after the ten-second failure boundary.
- A released client and server advertise incompatible personal-agent runtime contracts.
- A maintenance batch partially succeeds, crashes after output publication, or retries the same
  unit.
- Two service instances begin the same startup update concurrently, and one exits mid-update.
- A supervised child emits output faster than it can be consumed, spawns descendants, or closes one
  pipe while leaving another open during shutdown.
- A built-in example is valid syntactically but unsupported by the selected tool's actual bounds.
- Documentation is tracked selectively under an otherwise ignored documentation directory.
- A platform runner is unavailable when a cross-client change is ready to release.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST enforce configured active-work limits for client messages and
  background operations; reaching a limit MUST result in a queue whose finite item limit and maximum
  wait are published in the effective deployment configuration, or an explicit retryable refusal,
  never silent over-admission.
- **FR-002**: Every accepted operation MUST have a globally unambiguous identity, owner, lifecycle
  state, and terminal outcome; completed operation records MUST be queryable for a documented
  retention period whose default is 24 hours and MUST be removed within one hour after that period
  expires.
- **FR-003**: A client MUST complete registration within five seconds before other messages from that
  connection can create work; otherwise the connection and its queued messages MUST terminate with
  an explicit registration-timeout result.
- **FR-004**: When a client disconnects, the system MUST cancel or drain all connection-owned
  waiters and work within five seconds and MUST NOT leave work awaiting an event that connection can
  no longer produce.
- **FR-005**: State-changing messages from one connection MUST be ordered or deduplicated so that
  concurrent handling cannot silently overwrite a newer state.
- **FR-006**: Each scheduled occurrence MUST have a durable unique identity and one authoritative
  claim at a time, including across repeated polls, retries, multiple service instances, and
  claim-holder failure.
- **FR-007**: Retried scheduled work MUST preserve its occurrence identity and every
  AstralDeep-controlled effect MUST deduplicate on that identity, producing at most one externally
  visible effect. A handler that cannot honor this contract MUST remain ineligible for unattended
  scheduling and MUST report that limitation before acceptance.
- **FR-008**: Capacity, queue depth, rejection, oldest-work age, duplicate suppression, cancellation,
  claim recovery, and terminal outcomes MUST be observable without exposing message contents or
  deployment credentials.

- **FR-009**: Every running personal agent MUST have one authoritative runtime-instance identity
  that binds its owner, selected host, delivery, revision, process, and request generation.
- **FR-010**: The system MUST select at most one active host instance for a personal agent and MUST
  apply a deterministic replacement policy when another eligible host connects.
- **FR-011**: Child exit, host loss, and explicit stop MUST be detected immediately; a child hang MUST
  be detected no later than five seconds after its last valid liveness signal. Derived status and
  calls assigned to the failed instance MUST terminate within two additional seconds, for a maximum
  seven-second user-observable hang-failure path.
- **FR-012**: Frames and acknowledgements from a superseded host, delivery, revision, process, or
  request generation MUST be ignored and MUST NOT alter current state.
- **FR-013**: Revision activation MUST keep the current working version available until the proposed
  version is both confirmed running and durably promoted.
- **FR-014**: Failed or interrupted revision promotion MUST retain a recoverable last-known-good
  version and MUST NOT stop it merely because preparation of the new version succeeded.
- **FR-015**: Agent deletion MUST become durable before asynchronous cleanup; delayed registration,
  reconnect, delivery, or revision work MUST NOT restore a deleted agent.
- **FR-016**: An offline host that later reconnects MUST reconcile stopped and deleted agents before
  restoring retained bundles, so refused or obsolete bundles do not relaunch on every startup.
- **FR-017**: Host and bundle compatibility MUST be declared with a versioned runtime contract;
  incompatible combinations MUST be refused within two seconds of delivery or registration with an
  actionable, non-sensitive reason.
- **FR-018**: Dependencies included in a packaged personal-agent runtime MUST resolve to one
  reproducible, reviewable release set.

- **FR-019**: Drafts MUST use immutable owner-scoped identities independent of display names and
  storage names, including for concurrent same-name creation.
- **FR-020**: Draft phase changes, analysis results, generation, and lifecycle transitions MUST
  require the expected current revision or an idempotency identity; a stale update MUST leave state
  unchanged and return the current revision identity and a refresh action within one second.
- **FR-021**: Generated artifacts MUST be staged separately and published atomically so concurrent
  drafts or failed generation cannot overwrite a working artifact set.
- **FR-022**: Concurrent startup updates MUST have one coordinated owner, recheck current state after
  acquiring ownership, and leave all service instances on one valid revision after success or
  recovery.
- **FR-023**: Maintenance and synthesis work MUST claim units durably, mark only successful units
  complete, retain retry state for failures, and publish output atomically.
- **FR-024**: While long-running process, data-store, filesystem, or maintenance work is active, at
  least 95% of unrelated interactive operations MUST receive acknowledgement or visible progress
  within two seconds at the release load target, and none may wait longer than five seconds.
- **FR-025**: Shared runtime registries MUST provide a stable view to concurrent readers and writers
  and MUST NOT fail or return partially mutated results during ordinary client registration.

- **FR-026**: Every supported client MUST persist enough non-credential conversation resume state to
  identify the intentionally active chat before reconnect registration begins.
- **FR-027**: Active conversation state MUST be cleared only by an explicit new-chat, sign-out, or
  confirmed deletion action, not by process recreation or transient connection loss.
- **FR-028**: Reconnect hydration MUST preserve the committed transcript and canvas until a complete,
  correctly scoped replacement is ready, then replace them as one coherent state. Every direct
  turn, component mutation, scheduled turn, persisted stream terminal, detached/REST update, and
  long-running-job result MUST likewise publish transcript plus the complete canvas as one durable
  logical conversation commit and one authoritative snapshot, or leave the prior revision
  authoritative. A web-targeted snapshot MUST carry the canonical server-rendered presentation in
  the reserved per-component envelope defined by the continuity contract so the browser performs
  the same atomic replacement without implementing a second primitive renderer; native snapshots
  MUST remain semantic-only.
- **FR-029**: Conversation and render updates MUST be scoped to the intended chat, connection
  generation, and request generation so delayed updates cannot replace current content. Clients
  MUST generate fresh request UUID4s for loads/resumes and submitted turns. A server-originated
  scheduled, detached, persisted-stream, or long-job commit MUST instead use a fresh server UUID4
  opened on a client only by an exact six-field `conversation_commit_ready` prelude
  (`type`, `schema_version`, `chat_id`, `connection_generation`, `request_generation`, and
  `render_revision`) that immediately precedes its one commit snapshot; invalid/stale preludes and a
  prelude that would steal an unfinished client commit MUST be no-ops.
- **FR-030**: Every client MUST decode all valid stored transcript content forms into semantically
  equivalent visible turns and MUST surface an explicit recovery state instead of silently emitting
  blank turns.
- **FR-031**: A welcome experience MUST appear only when the user intentionally has no active chat.
- **FR-032**: Agent online, starting, updating, failed, and offline status changes MUST be handled
  consistently by every supported client.

- **FR-033**: The official Windows production distribution MUST provide a complete release profile
  containing the authority, service endpoint, client identity or mode, and either an explicit
  credential-free agent connection disposition or the required agent connection credential through
  the project's existing approved noninteractive provisioning mechanism. A clean-profile user MUST
  not enter any of these values manually.
- **FR-034**: A clean launch of the official Windows production artifact MUST NOT show the Configure
  AstralDeep dialog or require a user to enter deployment values; the dialog MAY remain available
  only in an explicitly generic/developer distribution or explicit reconfiguration flow.
- **FR-035**: Windows deployment-profile precedence MUST be documented and tested as: explicit
  managed or command-line override, permitted persisted override, bundled release profile, then
  development-only local defaults.
- **FR-036**: The Windows client MUST resolve one immutable effective deployment profile before it
  constructs user-facing surfaces, connects transports, or starts hosted agents; all components
  MUST consume that same result.
- **FR-037**: A configured deployment failure MUST retain the selected profile and provide retry and
  non-sensitive diagnostics; it MUST NOT silently switch to a different deployment.
- **FR-038**: Production packaging MUST fail before signing when its bundled profile is missing,
  invalid, placeholder-valued, internally inconsistent, or contains a value not approved for that
  named release profile. Production authorities/endpoints MUST be non-local and contain no userinfo,
  query, or fragment. An intentionally approved local-only generic/developer profile remains valid;
  an accidental production fallback to a local default does not.
- **FR-039**: The Windows artifact MUST expose a non-interactive deployment validation that proves
  the effective profile and packaged personal-agent runtime are complete without displaying or
  exporting credential values.
- **FR-040**: The merged personal-agent Windows capability MUST ship under a new semantic client
  version and immutable release identity, with verified upgrade behavior from the prior published
  version.

- **FR-041**: Curated prompts and examples MUST be checked against the actual selected tool
  capabilities and bounds; unsupported requests MUST be constrained or refused explicitly.
- **FR-042**: For every curated-example and release-smoke flow, the user-visible narrative's tool
  identity, quantities, units, bounds, labels, and reported values MUST exactly match the normalized
  recorded inputs and result; facts absent from that record MUST be labeled as interpretation.
- **FR-043**: Operations lasting longer than two seconds MUST show a visible operation or phase label
  and current status by the two-second mark, and every progress sequence MUST end in exactly one
  completed, failed, cancelled, or retryable state.
- **FR-044**: The personal-agent operating guide MUST be tracked, reachable from every existing
  reference, and cover enablement, effective-setting verification, hosting modes, lifecycle states,
  recovery, compatibility, and rollback.
- **FR-045**: Changing a boot-time deployment value MUST use an apply operation that recreates or
  otherwise reloads the service environment; documentation MUST include a way to verify the
  effective value after startup.
- **FR-046**: All tracked documentation references affected by this feature MUST pass automated
  target validation, including files intentionally tracked beneath an otherwise ignored directory.
- **FR-047**: Every interactive control changed by this feature MUST expose a stable accessible name,
  role, state, and keyboard or focus behavior. Mobile text entry and keyboard dismissal MUST use the
  native platform IME/keyboard behavior; the application MUST NOT draw a replacement keyboard
  dismissal control over the system keyboard.

- **FR-048**: Release validation MUST exercise the actual packaged Windows application and hosted
  worker from a clean profile before signing, including startup, deployment validation, one benign
  personal-agent round trip, output handling, and clean termination. Signing/publication MUST
  consume those exact archived unsigned bytes from the same-SHA passing readiness run without
  rebuilding. The existing detached Sigstore flow MUST leave the EXE bytes unchanged, create
  `SHA256SUMS` and `cosign.bundle`, upload all three only to a draft/quarantined release, record the
  identical pre/post-sign EXE digest plus official tag/release/asset/target-SHA identities, re-
  download every asset by immutable ID, verify the checksum and exact Sigstore identity/issuer, and
  only then make the release public; any failure MUST delete or retain the draft as non-public. The
  official tag MUST be created as exactly `v${release_version}` at the protected readiness SHA, where
  `release_version` is strict SemVer without a `v`, and any existing tag/release/asset collision MUST
  fail without replacement. The release name MUST equal that exact tag, publication MUST mark it as
  the latest release, and the publisher MUST confirm the resulting `/releases/latest` metadata so
  the shipped v0.3.0 updater selects and verifies 0.4.0; a post-transition mismatch MUST remove only
  the just-created release/tag, emit a protected failure, and never report publication success.
  Candidate-modifiable workflows MUST have no release-mutation authority. To preserve the verifier
  shipped in v0.3.0, the protected CI publisher MAY invoke an exact-byte-pinned compatibility signer
  with only `contents: read`, `actions: read`, and `id-token: write`; the signer MUST retrieve the EXE
  by exact originating run/attempt/artifact identity, re-hash and sign only those bytes under the
  existing tag-ref SAN, and have no tag/release mutation. Tag/release creation, asset mutation,
  approval verification, and public transition MUST run in a separately pinned, environment-
  approved GitHub Actions publisher using only the built-in short-lived job token. No repository-
  scoped GitHub App, installation token, or custom token broker may be required.
- **FR-049**: Changes affecting runtime, protocol, packaging, or cross-client behavior MUST deploy
  the same commit-derived candidate artifact in a qualifying persistent or isolated ephemeral
  staging environment with real configured authentication, representative data migrated through
  the normal product mechanism, and real background workers. That exact deployment MUST remain
  reachable to and alive for a matrix covering a real web browser, the Windows artifact, a connected
  Android emulator or device, and affected Apple clients on an Apple runner, with cleanup only after
  the matrix finishes. Evidence MUST bind the candidate SHA, immutable artifact digests, fixture
  fingerprints, target runner identities, one shared staging identity, and re-hashed raw bytes. The
  per-producer workflow and stage-deploy identities MUST be downloaded by exact artifact ID from the
  current workflow run and independently verified through GitHub artifact attestations against the
  expected repository and candidate. Each platform report's job and runner MUST match its verified
  producer identity; a candidate-controlled filename, embedded hash, or local verdict is not a trust
  root. Evidence collection, normalization, and parsing MUST complete locally before push and emit
  canonical evidence plus digests without release authorization. The repository-required final
  decision MUST be produced by protected CI, which independently schema-validates and re-hashes those
  inputs, reconstructs bounded current-run identities from GitHub API state, and executes pinned
  policy/coverage code. A committed local verdict or candidate-declared success MUST NOT qualify.
  Archived endpoints MUST contain no userinfo, query, or fragment.
  Runner-local, caller-selected URL, mutable-reference, source-only, or mock fallbacks do not qualify. One reusable
  readiness aggregate MUST run automatically for pull requests and main pushes using the immutable
  event base, while explicit candidate reruns MAY use a verified manual ancestor. The publisher,
  signer template, policy, schemas, protected environment, and tag/ref rules MUST be reviewed and
  protected before release activation; candidate jobs remain read-only.
- **FR-050**: The matrix MUST cover sign-in, one ordinary rendered chat turn, reconnect and resume,
  lifecycle status, and personal-agent authoring or hosting wherever that client supports it. macOS
  hosting applicability MUST come from a complete server-owned capability value in the exercised
  candidate—not a branch/spec path, live client count, or client-authored report. Unsupported makes
  only the macOS-hosting check not applicable; supported-but-refused or unacknowledged is a failure.
- **FR-051**: Every affected shipping client and feature-designated non-waivable check MUST produce
  passing evidence for the exact candidate before release unless a temporary runner/platform outage
  satisfies the existing candidate-bound, independently verified seven-day exception policy.
  Exception approval, append-only debt registration, and later resolution MUST run in protected,
  environment-approved GitHub Actions using the built-in short-lived job token; local evidence and
  candidate-controlled jobs cannot approve themselves. Product failures, qualifying staging, trust
  and policy integrity, and feature-designated non-waivable checks remain ineligible, and no client
  may substitute for another. No repository-scoped GitHub App, installation token, or custom token
  broker may be required.
- **FR-052**: Client build configuration MUST include a compatibility canary for the next declared
  major toolchain and MUST remove known-removal blockers before adopting that toolchain. Exact pins
  MUST identify stable public, independently resolvable artifacts; prerelease artifacts do not
  qualify. While either stable major is unpublished, the declaration MUST remain explicitly
  unavailable, fail closed as a canary, and use only a separate official-availability diagnostic.
  Spec 060 closes on that verified unavailable state and leaves activation to a separately authorized
  future change after both stable public releases exist.
- **FR-053**: The feature MUST add no new product runtime dependency without the project approval
  required by the constitution; test-only tooling MUST remain isolated from released artifacts.
- **FR-054**: On macOS and iOS first login, choosing Save for LLM API credentials MUST produce visible
  acknowledgement within 250 milliseconds and MUST NOT make navigation, focus, window management, or
  other visible controls unresponsive while validation and persistence continue.
- **FR-055**: A successful Apple LLM credential setup on an active connection MUST advance to the
  next page within five seconds in at least 95% of release trials; any attempt still active after one
  second MUST show a user-facing loading phase until it terminates.
- **FR-056**: An Apple LLM credential attempt that cannot complete MUST leave the loading state within
  ten seconds. Invalid credentials MUST produce a corrective validation result; provider or network
  unavailability MUST produce a retryable result. In both cases the form MUST remain editable and
  the user MUST be able to try again without restarting the app.
- **FR-057**: Apple release validation MUST exercise first-login LLM credential setup on both macOS
  and iOS, including the reported App Store review device/OS profile when that profile is available.
- **FR-058**: Every supervised child process MUST have its standard output and error consumed
  continuously with documented per-process memory and line-size limits; stop, quit, failure, and
  cancellation MUST terminate the complete process tree and close its pipes within five seconds.
- **FR-059**: User-agent policy or constitution version changes MUST trigger their required
  revalidation independently of schema revision changes, including when the stored schema marker is
  already current. Analyze policy and the baked agent constitution MUST have independent explicit
  revision owners and one fail-closed canonical combined marker; feature 060 starts at
  `constitution=0.1.0;analyze=1`.

### Key Entities

- **Accepted work item**: One user or background operation with a durable identity, owner, admission
  state, retry identity, timestamps, and terminal outcome.
- **Scheduled occurrence**: One intended execution of a schedule, distinguished from the schedule
  definition and carrying claim, lease, retry, and completion state.
- **Agent runtime instance**: One concrete personal-agent process bound to an owner, host, delivery,
  revision, generation, liveness state, and assigned requests.
- **Agent revision**: One immutable candidate artifact set with compatibility information,
  last-known-good relationship, promotion state, and acknowledgement identity.
- **Draft revision**: One owner-scoped, immutable authoring version used to detect stale transitions
  and isolate staged publication.
- **Conversation resume state**: The intended active-chat identity, active connection/request
  fence, and last coherent transcript/canvas revision needed to restore the user's place without
  storing credentials; server-generated commit fences are accepted only through the exact
  `conversation_commit_ready` prelude.
- **Deployment profile**: The versioned set of release-provided connection and client-mode values
  resolved once for a Windows client installation.
- **Maintenance unit**: One independently claimable synthesis or maintenance input with attempt,
  output, error, retry, and completion state.
- **Release evidence set**: Results tied to one candidate artifact and revision across each affected
  client, deployment smoke, documentation validation, and compatibility canary.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In a 1,000-message connection stress test, active work never exceeds the configured
  ceiling, every accepted message reaches one terminal state, and zero connection-owned tasks remain
  five seconds after disconnect.
- **SC-002**: Across 10,000 repeated-poll, two-instance, slow-work, and crash-recovery scheduler
  trials, each eligible scheduled occurrence produces at most one visible effect and every accepted
  test occurrence completes or explicitly fails within 60 seconds after the final recovery event.
- **SC-003**: In 100 child-crash, child-hang, host-loss, and host-replacement trials, personal-agent
  state and assigned calls reach a terminal outcome within two seconds for exit/host loss and within
  seven seconds for a hang, with zero stale-instance results accepted.
- **SC-004**: In 100 fault-injected revision trials covering every promotion boundary, the previous
  working revision remains invocable whenever the proposed revision is not durably active.
- **SC-005**: In 100 concurrent authoring and delete/register interleavings, there are zero lost
  updates, shared draft storage identities, duplicate publications, or deleted-agent resurrections;
  stale writers receive an explicit conflict.
- **SC-006**: After network loss and service restart—and after Android/iOS process recreation,
  Windows/macOS app restart, or web reload as applicable—the intended chat, semantically equivalent
  transcript, and last committed canvas return within five seconds in a single resume cycle per
  supported client, with no unintended welcome screen.
- **SC-007**: On a clean Windows profile with no saved settings or runtime overrides, the production
  artifact opens the main window without a configuration dialog in 100% of release-smoke runs and
  completes an ordinary chat turn using its release-provided deployment profile.
- **SC-008**: The new Windows release has a distinct immutable version; an installed prior release
  reports the update, and the new downloaded artifact passes identity and packaged-runtime
  validation in every release trial.
- **SC-009**: In partial-failure and crash-recovery maintenance tests, 100% of successful units are
  completed, 0% of failed units are falsely completed, and every retry retains its unit identity.
- **SC-010**: Every curated example passes an automated capability check, and 100% of release-smoke
  narratives agree with the normalized tool inputs and recorded results.
- **SC-011**: Users see a visible operation or phase label and current status within two seconds for
  every release-smoke operation that exceeds that duration, and no progress sequence remains
  non-terminal after its operation ends.
- **SC-012**: One release-candidate evidence set identifies the qualifying staging topology,
  representative migration, candidate SHA/artifact digests, and quantitative trials demonstrating
  sign-in, rendered chat, continuity, and applicable personal-agent behavior on every affected
  shipping client against one reachable deployment before publication. Missing platform evidence,
  duplicate checks, illegal N/A outcomes, unverified bytes, or conflicting staging identity is never
  silently treated as passing.
- **SC-013**: All affected tracked documentation links resolve, the personal-agent operating guide
  is present in a clean checkout, and the documented enablement flow proves the running effective
  value on its first execution.
- **SC-014**: The independently verified official-availability diagnostic confirms that either one
  or both stable next-major tools remain unpublished, prereleases do not trigger activation, the
  compatibility canary fails closed, and automated accessibility inspection reports zero unnamed
  interactive controls on changed surfaces. After both stable public releases exist, a separately
  authorized future change must pass the true canary before adopting them; that future activation is
  not a Spec 060 distribution gate.
- **SC-015**: All new or changed behavior meets the repository's ≥90% changed executable-line
  coverage gate in every changed maintained language and in aggregate, with missing applicable
  reports or an unexpected empty event-base comparison failing automatically on pull requests and
  main pushes, and adds no unapproved released dependency.
- **SC-016**: Across 30 first-login trials on each Apple platform, Save is visibly acknowledged
  within 250 milliseconds, valid active-connection attempts reach the next page within five seconds
  in at least 95% of trials, every longer-than-one-second attempt shows a loading phase, and no
  attempt remains unresolved after ten seconds. During every in-progress attempt, scripted focus,
  navigation, and window or scene interactions continue to respond within 250 milliseconds.
- **SC-017**: In 50 two-instance startup and crash interleavings, exactly one startup updater applies
  each revision and both instances reach the same valid state; a constitution-only version change
  triggers revalidation in 100% of trials even when the schema revision is unchanged.
- **SC-018**: In 10,000 overlapping agent registration, removal, list, and dashboard operations, no
  concurrent-mutation exception, partial registry view, or stale current-state result is observed.
- **SC-019**: During the release maintenance and process stress profile, at least 95% of unrelated
  interactive operations acknowledge or show progress within two seconds and none is blocked for
  more than five seconds.
- **SC-020**: In 100 high-output, descendant-process, cancellation, quit, and failure trials, process
  output remains within the declared memory and line limits and zero descendant or open pipe remains
  five seconds after termination begins.
- **SC-021**: Two clean builds from the same Windows release definition resolve the identical locked
  runtime set, and every supported old/new host-and-bundle compatibility pairing either completes a
  benign round trip or returns the declared incompatibility result within two seconds.
- **SC-022**: In 20 consecutive online, starting, updating, failed, and offline transition sequences,
  every supported authoring client shows the same current agent state within two seconds of the
  service publishing the transition, without a full reload.

## Assumptions

- Feature 058 is merged and is the baseline for personal-agent authoring, Windows hosting, delivery,
  tunneling, and lifecycle behavior.
- Feature 059 owns implementation of the macOS personal-agent host. This feature does not modify or
  duplicate that work; after 059 integration, 060 validates it against the common reliability,
  compatibility, lifecycle, and release-evidence outcomes defined here.
- The production Windows deployment authority, service endpoint, client identity or mode, and agent
  connection credential or explicit credential-free disposition are supplied by the release owner
  at build or release time. Any required credential is made available through the project's existing
  approved noninteractive provisioning mechanism; this feature does not introduce a new credential
  storage or validation policy. End users do not enter these values on first launch.
- The Windows deployment connection profile and the Apple user's LLM provider credentials are
  distinct concerns. Apple users still enter their own LLM API credentials through the existing
  first-login mechanism; this feature changes only that flow's responsiveness, progress, terminal
  outcomes, and release evidence, not credential storage or validation policy.
- The App Store rejection report applies to both the macOS and iOS submissions. The supplied review
  environment was a 14-inch MacBook Pro (November 2024), macOS 26.5.2, with an active internet
  connection; the iOS review device was not supplied, so validation uses each currently supported
  iOS review target in addition to the reported Mac profile when available.
- Generic developer builds may retain an explicit configuration workflow; the official deployment-
  specific production artifact may not fall back to it during ordinary clean launch.
- Existing product-runtime authentication, authorization, credential handling, agent/data trust
  boundaries, and other runtime security behavior remain unchanged. Security review and runtime
  security remediation are outside this feature by explicit user direction. Release-artifact
  provenance, CI evidence trust, and protected evidence-exception approval are in scope because
  FR-048–FR-051 make them part of release correctness, not a product authorization-policy change.
- Android, Apple, and web are locally testable in the current macOS pickup environment; Windows
  source behavior is locally testable, while the packaged Windows artifact requires a Windows
  runner or machine. No shipping client may substitute for another client's release evidence.
- User-visible results may be semantically equivalent across clients without being byte-for-byte
  identical, provided content, ordering, state, and supported interactions are preserved.
- At-most-one visible effect applies to handlers that honor the stable occurrence identity. Planning
  must identify every legacy handler that cannot yet do so and keep it ineligible for unattended
  scheduling until an idempotent boundary exists.
- Existing product runtime dependencies are sufficient for this remediation unless a later plan
  records and obtains the constitution's required approval.

## Out of Scope

- Any change to product-runtime authentication, authorization, secret storage, agent/data trust
  boundaries, permission policy, or other runtime security behavior. This exclusion does not cover
  the local evidence-preparation, protected-CI validation, and native CI publisher controls required
  by FR-048–FR-051. Repository-scoped GitHub Apps and custom release token brokers are explicitly out
  of scope.
- Building the macOS personal-agent host itself, which remains owned by feature 059.
- A visual redesign, new UI primitive family, or new agent-sharing/publishing capability.
- Replacing the current scheduling, client, or deployment product with a different platform; this
  feature corrects behavior and release evidence within the existing architecture.
