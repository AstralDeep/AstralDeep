# Persistent agents: implementation and verification walkthrough

**Feature**: 079-persistent-agents

**Status**: Local implementation and qualification in progress. This walkthrough
is not a record of passed live checks; use `verification/results.md` for evidence.
Unavailable clients and skipped PostgreSQL tests are missing evidence, never passes.

## Starting conditions

The selected feature is `specs/079-persistent-agents` on
`codex/079-persistent-agents`. The first live assignment monitors a public release
page using the existing `web-research-1` agent's `fetch_page` tool. It requires no
new inbox, issue-tracker, search-provider, or other connector credentials.
Keycloak authentication, the normal product LLM configuration when model work is
used, mandatory finite model/tool/token/time and other usage limits, and explicit
offline consent remain required. A currency spending cap is optional. With no
currency cap, monetary cost is Unpriced/unknown, never zero or free; the public
monitor needs no new price configuration. Selecting a currency cap requires a
trusted finite quote covering each eligible action or activation/revision is
rejected. Zero currency spending requires an explicit trusted zero-cost quote.

Initially only Docker Engine was running. The original baseline was then
built successfully: app image
`sha256:a33e6c5b7a1c1b74b1b2095b95e5f5c9a2e85fcb4d1e09ef10d0665dd44cf810`,
with healthy app and PostgreSQL, and running LiveKit/voice worker; see
[verification/results.md](verification/results.md). This establishes baseline
infrastructure, not Feature 079 live verification. A worker/infrastructure change
must still be qualified with real PostgreSQL, Keycloak, workers, representative
migrated non-PHI data, and all affected client flows. An empty database or mock
authentication is insufficient for staging qualification.

Use the current repository root rather than absolute paths recorded on another
machine. Keep tokens, `.env`, account data, uploads, runtime stores and verification
session material out of source control and command output. The owner completes
Keycloak/provider sign-in; automated helpers must not type the owner's keys or
perform sign-in for them.

```powershell
git status --short --branch
git submodule status --recursive
docker info --format '{{.ServerVersion}}'
docker compose ps
python scripts/verify_composition.py --root .
python scripts/install_local_components.py validate --root . --require-gitlinks
```

The composition checks require exact clean qualified component identities. During
implementation, edited Plane/Projection work must be tested in their own component
environments; a dirty gitlink is not a qualified composition. After the exact Plane
schema revision/migration digest and Projection revision are qualified, update
Deep's composition declaration through the planned component integration work.
Do not bypass the verifier or point the image at an unrelated sibling `main`.

Inspect the existing deployment configuration privately. Production remains
fail-closed: `ASTRAL_ENV` unset means production; missing/placeholder secrets or
mock authentication must keep the documented exit-78 behavior. Feature 079 adds
`FF_PERSISTENT_AGENTS` default off; implementation must document its relationship
to existing scheduler execution and recursive-delegation flags in `plan.md` and
the operator configuration. Enabling 079 must not enable those other capabilities
implicitly or remove their security gates. Enable the intended reviewed posture
in the isolated verification deployment before testing live execution.

Operator bounds are `PERSISTENT_AGENTS_TICK_SECONDS` (default 15, range 1–30),
`PERSISTENT_AGENTS_CONCURRENCY` (default 4, range 1–25), and
`PERSISTENT_AGENTS_LEASE_SECONDS` (default 30, range 15–60). Invalid values refuse
startup when the feature is enabled. Each assignment additionally has finite
daily and lifetime call/token/time ceilings, cadence, retry count, task count,
concurrency, depth and per-step timeout. Failed episodes use bounded exponential
backoff; exhausted retries require attention. An unknown mutating outcome remains
in reconciliation and cannot be automatically replayed. A known read failure
retains its conservative charge and can retry within those same limits.

The initial public-page profile uses only the reviewed URL and optional explicitly
reviewed linked document URLs. Other registered readers must have a trusted host
resource bound and precondition validator; a tool claiming to be a reader does
not make it eligible. External observations cannot change instructions or consent.

If an operator supplies a trusted `AssignmentService.quote_provider`, its bounded
coverage contains `currency`, an aware RFC 3339 `expires_at`, integer
`model_call_micro_units` and `model_token_micro_units`, and a
`tool_call_micro_units` mapping keyed by exact `agent_id:tool_name` identities.
`quote_digest` is SHA-256 of canonical sorted compact UTF-8 JSON of the remaining
fields. Rates must conservatively cover every eligible model route and complete
downstream tool execution. These values come from trusted operator integration,
never owner input, a source response or the planner. There is no default price
provider; choosing a currency cap without this coverage refuses activation.

```powershell
make up
docker compose ps
Invoke-WebRequest -Uri 'http://localhost:8001/readyz' -UseBasicParsing
```

`make up` runs composition preflight, builds and starts the committed Compose
stack. Use the deployment's actual reachable HTTPS base URL for remote/native
clients. Do not print `docker compose config` or complete container environments:
they can contain secrets. The app image contains backend source and installed
component wheels. After source/component changes, use `make sync`; after boot-time
Compose environment changes, recreate the service. A restart alone does not
install source edits or reread a changed environment.

For local candidate verification, the recorded override is
`build/079/verification/compose-candidate.yaml` (ignored operator configuration).
It selects `astraldeep:079-local`, enables `FF_PERSISTENT_AGENTS`, preserves the
existing authentication settings and durable roots, and removes the mutable
`backend/agents` source mount so the evidence driver can verify image-baked code.
Any explicitly configured artifact root must have its own persistent mount if
it lies outside the existing mounted data roots. Preserve and verify its existing
contents before recreating the container; do not overwrite a different existing
host artifact directory.

Before upgrading the live database from `075.001`, follow Plane's joint
database/durable-root backup procedure with all writers stopped. Run schema
evolution only through normal guarded startup. The previous `075.001` image
cannot admit the upgraded `079.001` schema; recovery requires forward repair or
the verified paired backup under closed admission.

```powershell
docker compose -f docker-compose.yml -f build/079/verification/compose-candidate.yaml build astraldeep
docker compose -f docker-compose.yml -f build/079/verification/compose-candidate.yaml up -d --no-deps astraldeep
```

## First live assignment

1. Open the candidate backend at its configured public address in a real browser
   and sign in through Keycloak. On this local setup use `http://localhost:8001`;
   the existing OIDC client refuses a `127.0.0.1` callback.
   Check that Web Research is registered and `fetch_page` is permitted. Do not
   configure optional search-provider credentials for this example.
2. Ask in chat: “Watch `https://www.python.org/downloads/` daily. Remember the
   initial page, then notify me only when release information changes. Explain
   the change and include the source link. Do not change anything externally.”
   A temporary verification cadence of 60 seconds may be selected on the review
   card, subject to stricter deployment limits, then revised back to daily.
3. Review the complete instructions, URL, exact reader tool, cadence, resource
   mandatory usage limits, optional currency cap, destination conversation, and
   expiring offline grant. Leave the optional currency cap unset for the initial
   monitor if no trusted price quote is available; verify Unpriced/unknown is
   displayed instead of a false zero-cost claim.
   Explicitly approve creation. Confirm the durable assignment ID/revision and
   whether it is active or requires authorization; a missing grant must be
   reported honestly.
4. Open Settings → Personalization → Schedule → Ongoing agents. Inspect the
   assignment's next check, source, limits, tasks, activity, and instructions.
   The first source read establishes a baseline. Record its tool-dispatch and
   activity identities without retaining page secrets or authorization material.
5. Close the client without choosing “sign out everywhere,” which revokes grants.
   Let one later check occur unattended. Reopen the same owner conversation.
   An unchanged page updates Last checked and next wake, creates no repeated
   attention notification, and makes no model calls between eligible wakeups.
   An irrelevant revision also keeps the last meaningful finding visible.
6. If the public page changed, verify the linked finding is published once. If it
   did not, record only the demonstrated baseline/unchanged behavior. Deterministic
   new-revision behavior is verified using controlled source fixtures in the
   test suite; do not falsely claim a real upstream release occurred.
7. Revise the assignment's instructions while a check is in flight. Acknowledge
   the new revision within two seconds under the verification workload, reject
   stale publication, and retain already completed work. Repeat pause, resume,
   run-now, revocation, and terminal stop against separate disposable assignments.
   Restore the intended daily cadence or stop the verification assignment.

The example URL is an input to the approved reader, not a claim that a specific
release exists. Network denial, throttling, unavailable source, or an oversized
response must produce bounded retries or an actionable state through the normal
egress path. Never weaken TLS, address filtering, redirects, size limits, or tool
permissions to make the walkthrough pass.

Every action stores an immutable request and outcome receipt. Pause/resume can
replace only an invalidated intent whose durable history proves it never started;
completed receipts are reused. Revising instructions explicitly supersedes old
pending events and retains completed child evidence in activity history. Each
concurrent child has its own live authority binding and shares the parent's
durable resource ledger. Large evidence is deterministically excerpted with
digests for model input; owner instructions are never silently truncated.

Account retirement first fences the owner's assignments in the same Plane
transaction. Unresolved begun actions produce `409 reconciliation_required` and
retain evidence; this does not claim completed account deletion. After trusted
reconciliation, retrying retirement can schedule the ordinary attachment purge.
Pending confirmations cannot become ordinary chat actions after their assignment
is stopped or deleted.

## Automated backend and component gates

Run narrow tests first. The exact tests introduced under `backend/persistent_agents/tests`
must include model validation, owner-scoped API/chat/UI commands, trigger coalescing,
leases/fences, durable children, budget reservation, consent/approval/revocation,
source taint, uncertain outcomes, and atomic publication. Add targeted tests for
foreground approved-action claim acquisition after the background lease is
released, races against worker claim/pause/stop/revision/revocation, exact
single-action scope, and duplicate approval. Test `request_check` through UI/chat/
REST for idempotent replay, wake coalescing, live-claim preservation, cadence/rate/
budget limits, and paused/stopped refusal. Cover mandatory usage limits without
a currency cap, unknown monetary cost display, and activation/revision refusal
when a selected currency cap has no trusted finite quote. `--collect-only` ensures
the new package is present and selected before execution.

```powershell
docker exec astraldeep python -m pytest --collect-only -q /app/backend/persistent_agents/tests
docker exec astraldeep python -m pytest -q /app/backend/persistent_agents/tests
docker exec astraldeep bash -c "cd /app/backend && python -m pytest -q scheduler/tests tests/test_machine_turn_authority.py tests/test_offline_grant_lifecycle.py tests/test_chain_authority.py tests/test_chain_budget.py tests/test_subtask_decomposition.py tests/test_subtask_orphan.py tests/test_tool_permissions.py tests/test_permission_memo.py tests/test_security_gates_wiring.py tests/test_remote_confirmation_063.py"
ruff check .
```

Tests that inspect tracked specs/workflows/component trees require the same image
with a full read-only repository mount; the running product container does not
contain every tracked artifact. Use the following form for those tests, installing
the already locked test tools into an isolated test environment when needed:

```powershell
$featureRepo = (Get-Location).Path
docker run --rm -e PYTHONDONTWRITEBYTECODE=1 --mount "type=bind,source=$featureRepo,target=/workspace,readonly" --workdir /workspace/backend astraldeep:latest python -m pytest -p no:cacheprovider -q tests/test_ui_protocol_manifest.py tests/test_ws_chrome_protocol.py tests/test_projection_protocol_integration.py tests/chrome/test_chrome_surface.py tests/chrome/test_surface_personalization.py
```

The full backend root suite is not every module suite: `backend/pytest.ini`
discovers `tests` and the new `persistent_agents/tests`. Run other touched module suites explicitly and mirror the current
Deep and component owner CI workflows. Preserve and explain unrelated baseline
failures without weakening an assertion or selecting an empty test set.

For Plane, use its frozen CI environment and an isolated PostgreSQL database
through `ASTRALPLANE_TEST_POSTGRES_DSN`; set `ASTRALDEEP_SOURCE_REPO` to the current
composition root without printing secrets. Run the complete representative
predecessor migration and repeat-startup suites, plus the new persistent-work
repository/lease/approval/budget tests. From `components/AstralPlane`:

```powershell
uv lock --check
uv sync --frozen --group ci
uv run --frozen --group ci ruff check .
uv run --frozen --group ci python tests/architecture/test_dependency_direction.py
uv run --frozen --group ci pytest -q -p no:cacheprovider --cov=astralplane --cov-branch --cov-report=xml --cov-fail-under=88.75
uv run --frozen --group ci diff-cover coverage.xml --compare-branch origin/main --fail-under=90
```

An unset test DSN means PostgreSQL tests may skip; that is missing evidence.
Apply migrations through the normal Plane startup registry, never deployed ad-hoc
SQL. Verify representative predecessor data survives, rerunning is safe, stale
workers lose fences, and the documented recovery procedure works. Down-migration
must not erase assignment data to make an older image boot.

Measure changed Python lines at >=90% in each owning repository, including altered
Deep dispatcher/API lines and Projection builders, rather than reporting only the
new package's coverage. Preserve Cobertura reports, exact base/candidate/component
SHAs, image identities, and report hashes. Use each owner's committed coverage
gate; Deep's `scripts/check_changed_coverage.py` takes the owner profile and all
required report inputs. A missing required report must fail, not become zero
changed lines.

## Client gates and live parity

Projection Python tests, from `components/AstralProjection` with its locked CI
tools and exact local package installed:

```powershell
ruff check src backend tests scripts windows-client
python -m pytest -q -p no:cacheprovider tests/chrome/test_personalization_views.py tests/chrome/test_sdui_helpers.py tests/test_protocol.py
```

Web tooling, from `components/AstralProjection/tooling/web-ci`, uses the committed
Node 24/Corepack/npm lock and existing Playwright image. Add the 079 browser
contract spec to this suite; it must exercise the shared form, conflict/approval
states, keyboard navigation, escaped source text, and meaningful-notification
behavior. Contract fixtures do not replace the real-browser walkthrough above.

```powershell
corepack npm ci --ignore-scripts
corepack npm run check:package-manager
corepack npm run check:product-isolation
corepack npm run lint
corepack npm exec -- playwright test tests/persistent-agents-079.spec.js --browser=chromium --workers=1
```

Windows, from the Deep root with the client requirements installed:

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
python -m pytest components/AstralProjection/windows-client/tests -q
Remove-Item Env:QT_QPA_PLATFORM
```

Launch the Windows client normally with a complete deployment profile for the
candidate backend and real Keycloak sign-in; do not follow stale mock-auth
development instructions as live evidence. Exercise creation, revision conflict,
pause/resume/stop, usage/activity, and sensitive review with the shared surface.
An offscreen test does not prove the launched UI works.

Android, from `components/AstralProjection/android-client` using JDK 17 and the
committed wrapper:

```powershell
.\gradlew.bat ktlintCheck :app:lintDebug :core:test :app:testDebugUnitTest :core:koverVerify :app:assembleDebug
.\gradlew.bat :app:connectedDebugAndroidTest
```

The second command requires a running emulator/device. Launch the resulting app
against the same candidate backend, sign in using Keycloak PKCE, and run the same
flow on phone and a tablet/comparable large form factor. Record the actual device,
APK identity, backend identity, and results.

Apple requires a qualified macOS/Xcode host. From
`components/AstralProjection` on that host:

```bash
swift test --package-path apple-clients/AstralCore
xcodebuild -project apple-clients/AstralApp/AstralApp.xcodeproj -scheme AstralApp -destination 'generic/platform=iOS Simulator' -configuration Debug CODE_SIGNING_ALLOWED=NO build
xcodebuild -project apple-clients/AstralApp/AstralApp.xcodeproj -scheme AstralApp -destination 'platform=macOS' -configuration Debug CODE_SIGNING_ALLOWED=NO build
xcodebuild -project apple-clients/AstralApp/AstralApp.xcodeproj -scheme AstralWatch -destination 'generic/platform=watchOS Simulator' -configuration Debug CODE_SIGNING_ALLOWED=NO build
```

Run affected XCTest schemes on actual selected simulator/device destinations as
defined in the committed project and owner CI; generic destinations only build.
Keep Swift manifest/action-count drift guards green. Launch macOS, iOS, and watchOS
against the same candidate, verifying full-client controls and wrist status/chat
pause/resume/stop plus explicit handoff for detailed consent. Do not attempt to run
Xcode on this Windows host or label a missing Mac as a passing gate. Record the
missing platform and keep implementation/live-verification status distinct.

## Recovery, denial, and evidence driver

`scripts/verify_persistent_agents_079.py` provides
`--help`, `--base-url`, `--session-file`, `--source-url`, `--assignment-id`,
`--scenario`, and `--output`. `session-file` is private short-lived authentication
material created by the owner's authorized session outside the repository; the
driver must never log its content. It must use authenticated product API and tool
dispatch, validate TLS, refuse unsafe output paths/foreign-owner resources, emit
bounded diagnostic JSON, and exit nonzero for missing prerequisites or failed
assertions. It cannot fabricate a consent or approval click. It never signs in,
refreshes a token, creates an assignment, approves an action, restarts a container,
or provisions credentials. Its monitoring scenarios request one check through
the authenticated `run-now` API; the existing runner performs normal tool
dispatch under the assignment's reviewed grant and budgets.

The owner creates the session file through their authorized session, with this
exact schema (angle-bracket values below are placeholders, not usable credentials):

```json
{
  "schema_version": 1,
  "base_url": "http://127.0.0.1:8001",
  "owner_sub": "<authenticated Keycloak subject>",
  "access_token": "<existing owner session bearer token>",
  "expires_at": "<UTC RFC 3339 expiry, at most one hour from verification>",
  "assignment_id": "<UUID of the owner-approved disposable assignment>",
  "allowed_scenarios": ["live-monitor", "after-restart"]
}
```

The selected origin, assignment ID, and scenario must match this file. Its expiry
must leave at least 30 seconds and cannot exceed the bearer token's expiry.
The product validates the token; the driver's decoded subject/expiry checks are
additional consistency checks, not authentication. Session files and output
reports must be outside **every** Git repository and sensitive product stores.
Files must have a private current-user owner and no group/other access on POSIX
(`0600`); on Windows the current user must own the file, and only that user,
SYSTEM, and Administrators may have allow rules. Links, hard-linked session files,
and existing output files are refused. Use an already private output directory
on Windows so new reports inherit an acceptable ACL. Never put authentication
material in a command argument, committed fixture, screenshot, or report.

`--baseline` selects the prior successful `live-monitor` report for recovery.
Alternatively, the session JSON may include `"baseline_file"` with that external
path. `--container` selects the existing local verification container (default
`astraldeep`); it is inspected read-only. Local candidate binding requires a
literal loopback HTTP origin whose published port maps to that container's
`8001/tcp`. The driver hashes reviewed backend/component runtime files and the
composition manifest, records actual Git heads and working-tree digests, and
compares the runtime hashes with the running container. It refuses differing
bytes, files changed since container start, and mounts over runtime code. It
does not print full Docker environment or source content. This is diagnostic
runtime binding, not a release attestation or a qualification of every deployed
dependency. Remote HTTPS origins use normal certificate validation but currently
fail closed with `missing_input_local_deployment_binding`; no remote live result
is claimed without an authoritative deployment-binding mechanism.

Required scenarios are `live-monitor` (an existing owner-approved disposable
assignment), `after-restart` (continuity/dedup verification), and `controls`
(revision conflict/pause/resume/terminal stop on explicitly selected disposable
assignments). Controlled source revisions, two workers, exact crash boundaries,
cross-owner denial, spent-budget refusal, uncertain external effect, and child
incorporation are mandatory integration tests using deterministic fixtures;
the report must distinguish these from live upstream observations.

The `controls` scenario must also appear in the owner's session file before it
can run. It **permanently stops** the selected disposable assignment after
checking pause/resume and terminal refusal. Its revision-conflict request always
uses `consent: false` and an intentionally stale epoch; a successful instruction
revision still requires the owner's real review in the product. The driver
does not manufacture that review.

```powershell
python scripts/verify_persistent_agents_079.py --help
python scripts/verify_persistent_agents_079.py --base-url $featureBaseUrl --session-file $featureSessionFile --source-url 'https://www.python.org/downloads/' --assignment-id $featureAssignmentId --scenario live-monitor --output $featureLiveReport
docker compose restart astraldeep
python scripts/verify_persistent_agents_079.py --base-url $featureBaseUrl --session-file $featureSessionFile --source-url 'https://www.python.org/downloads/' --assignment-id $featureAssignmentId --scenario after-restart --baseline $featureLiveReport --output $featureRecoveryReport
```

Variables above identify the selected isolated deployment, owner-approved test
assignment, private session file, and output files outside sensitive product
stores. The restart is performed only against that selected verification stack.
Before/after evidence must show stable completed event/action/result identities,
new fenced recovery ownership, no duplicate finding/publication, no model activity
while waiting, and durable child results reused once. For two competing workers,
use the isolated PostgreSQL integration harness; do not improvise a second
production orchestrator or disable single-runtime composition guards.

The owner-only `GET /api/persistent-agents/{assignment_id}/actions` and `/events`
APIs expose bounded pages (`limit` 1–100, optional `after_id`, `next_cursor`) with
`Cache-Control: no-store`. Event views contain only event IDs, identity/context
digests, disposition, and result digest; they omit source context and cursors.
Action views omit downstream and dispatch capabilities. The assignment view's
`last_completed_generation` is a nonsecret integer from the last successful
episode completion. Foreign-owner reads return 404. The driver hashes findings
and action keys, keeps stable IDs/digests and numeric usage, and omits names,
instructions, request messages/arguments, source content, and credentials.

Restart verification requires the same candidate, owner, assignment revision and
control epoch, an observed new container start, and a newer completed episode
generation. It verifies that completed action attempts/events/child incorporations
remain identical, rejects new work for already completed events, and rejects
duplicate identities/findings. An unchanged source must not cause new model
usage. A bounded idle observation checks that usage and agent work remain stable;
notification delivery acknowledgements may advance independently. Missing
completed receipts, a missing idle window, retention removing required evidence,
or an unconfigured model/grant produces a failed diagnostic, not a live pass.
The driver immediately refuses actionable `waiting_approval`,
`waiting_authorization`, `budget_exhausted`, `reconciliation`, and `failed`
phases rather than waiting out the observation timeout.
This proves an observed restart plus a new fenced episode; exact mid-action crash
recovery and competing workers remain separately required integration evidence.
No controlled upstream release change or live native-client behavior is inferred
from an unchanged public page.

Sensitive approval validation uses a disposable reversible test target in the
authenticated integration harness. Demonstrate missing, denied, expired,
different-owner, changed-argument, changed-revision, revoked-permission, and replay
refusals. A valid approval re-enters normal dispatch exactly once; existing
interactive-only operations remain interactive. Release the waiting background
lease before approval and verify the attended approved-action claim can execute
the exact action without stale background authority. Competing claims and owner
control changes must refuse its start, and approval must not claim unrelated
background work. If the normal dispatch requires `remote_confirmation`, verify
the linked exact proposal is displayed and any second review is explained;
assignment approval must not bypass it. Approving through the existing attended
path re-enters all gates and durably records the assignment action outcome before
parent continuation, without dispatching or incorporating the result twice.
No public issue tracker, inbox,
store, production account, or external recipient needs mutation for this feature's
example. Unit fixtures cannot stand in for real dispatch/owner authority checks.

Retain exact commands/results, candidate and component identities, representative
dataset/migration identities, UTC timestamps, client versions/devices, redacted
traces, crash points, action/publication identities, latency observations, budget
measurements, coverage digests, and any unverified platform. Existing release
evidence preparation/bootstrap rules still govern any later authorized push or
release; these local feature reports are diagnostic and grant no publication
authority. The owner authorized local feature commits and made task-scoped local
commits a standing permission in `AGENTS.md` on 2026-09-05. Product pushes,
production deployment and app-store submission still require their own authority.
