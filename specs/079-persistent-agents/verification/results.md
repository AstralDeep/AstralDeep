# Feature 079 verification record

## Current production-preparation checkpoint — 2026-09-05

The owner authorized PRs and merges where gates permit. Plane
[#7](https://github.com/AstralDeep/AstralPlane/pull/7) and Projection
[#14](https://github.com/AstralDeep/AstralProjection/pull/14) are pushed draft PRs.
Plane is `94caad85422287188842b33fa60c8af382595026`; Projection is now
`a61dce71147c9904605356575259e618b7e4d8e3`, after correcting the Apple drift-test
line wrapping identified by owner CI. Their independent CI/review and affected
live-client requirements remain open. No merge or production deployment occurred.

Deep runtime hardening is committed at
`42b7ce56e1a9238728cf9b3f29e7847e635e810c`. It fixes shared browser/background
refresh rotation, current-token logout, typed URL privacy views, unescaped full
source injection checks, source-only redaction before durable hashing, model
evidence envelopes and public-reader trailing-slash/relative redirects. The
subsequent tooling changes correct native C# coverage parsing and atomic private
NTFS report creation, and add the new parser tests to owner CI. The migration
remains 079.001 with the prior qualified digest; no runtime dependency was added.

Fresh local evidence, with exact commands and native reports in ignored
`build/079/verification/`:

| Check | Result |
| --- | --- |
| Persistent-agent, privacy, personalization and changed chrome suites | 548 passed in 218.45s; new privacy module 100% statement coverage; full original source is scanned before redaction/truncation |
| Shared session/grant/authentication qualification | 89 passed in 20.07s; 96.23% changed-line coverage across 212 measured lines |
| Plane credential/history repair against isolated PostgreSQL | 68 passed; 98.03% slice coverage, 100% new measured lines |
| Page reader/shared egress tests | 235 passed; 12/12 changed executable lines covered |
| Native C# coverage parser plus existing policy regressions | 163 passed, one environment skip; 99% changed-line coverage |
| Live evidence driver | Windows 36 passed; Linux 35 passed, one NTFS-only skip; each 434/445 statements (97.53%) |
| Voice/backend diagnostic fixture corrections | 152 passed in 26.24s; evaluator coverage 98%; clean main reproduces 14 prior failures |
| Current schema/rollback fixtures | 16 passed against isolated PostgreSQL; historical 074/075 checks retained and exact 079 lineage added |
| MCP denial fixtures | 25 passed; production-mode signing-key denial retained with a synthetic session-encryption prerequisite |
| Full root Ruff 0.15.21 | Passed; the three new style errors from clean preflight are fixed |
| Projection native Windows | 1,080 passed, nine skips in 1471.89s |
| Projection C# / Node / shipped-client browser fixture | 37 / 28 / 5 passed; earlier Android gates remain passed |
| Worker producer | 372 passed, two skips; 90.41% branch-inclusive coverage from the exact worker test image |

The complete clean pre-hardening snapshot at Deep `3c5696f2` passed 773 root CI
tooling tests (92% coverage) and 413 supplemental script tests, but its broader
backend run returned 7,927 passes, 26 failures and 14 skips. Its explicit nested
modules returned 2,350 passes, five failures and seven skips, with a separate
worker-package collection prerequisite. Later targeted repairs above are real
passes; a complete rerun of the final clean source is still required. The WS
handshake test requires a qualified isolated running app. Worker tests belong
in the worker image with its declared ONNX dependency, not the orchestrator image.

The owner signed in and approved a bounded public Python release-page test.
Its first creation was refused before an assignment/grant existed. The installed
Presidio detector accepted a later complete public-page diagnostic after
source-only redaction, and the real page reader succeeded after the redirect
fix. These diagnostics do not claim an authenticated assignment/restart result.

A clean LF runtime image from `42b7ce56` built successfully as
`astraldeep:079-runtime-42b7ce56`, image
`sha256:5222c2b266d77936c0e4c52880070598f1eec7e6a69ed657fb21e8dfb2c38e59`.
It is not yet the final deployed candidate. Its clean composition digest is
`d6b0095fa7e0ed7fbbeda1dbbfd719f11fe88d209462fe4c4b9005dc41ce189b`.
The current declaration additionally repins Projection's tested formatting repair;
build and live evidence must bind that final declaration separately.

See [production-readiness.md](production-readiness.md) for the bounded test,
clean-source rebuild, backup/restore, protected staging and publication gates.
The curated vault checkpoint for runtime hardening and component draft PRs is
`3676fd1`, pushed separately. Deep itself remains local at this checkpoint.

## Historical local integration checkpoint — 2026-09-05

The current section above supersedes authorization, component heads, native
producer availability and runtime changes in the earlier checkpoint below.

The owner authorized local feature commits and directed `AGENTS.md` to permit
task-scoped local commits without additional approval. Push/merge/release/store
and external-issue mutations retain their separate authorization boundaries.

Reviewed component implementation is now committed locally:

- Plane: `4e959739578a7eb2aef0d52e59745230d17a1810` (23 intentional files).
- Projection: `d2ce8be6359bac22f63423091d72ac3c0b155a44` (13 intentional files).

Both component worktrees are clean. Thirty-five Plane stat-only paths were
verified byte-for-byte against HEAD before refreshing their index; no additional
content changes were committed. Deep's declaration and staged gitlinks now point
to these exact commits, schema `079.001` and the qualified migration/UI digests.
Both local composition checks pass, with composition digest
`5f58f51be3ce6ae31c710437683ef73c6b1195a22f0bd9a5accfdea5a01a8013`.
The static verifier now reads only the exact reviewed data-only assignment SQL
tuple; it never executes component imports or accepts arbitrary new expressions.
Its native Windows suite passed **121 tests in 35.50s**; all **38 changed
executable statements** were covered. Command: `uv tool run --python
.venv/Scripts/python.exe --from pytest==8.4.2 --with pytest-cov==7.0.0 pytest
scripts/tests/test_verify_composition.py -q --tb=short --cov=verify_composition_074
--cov-report=term-missing --cov-report=json:$env:TEMP/079-composition-host-coverage.json`
(PowerShell, no environment overrides; report copied under
`build/079/verification/079-composition-host-coverage.json`).
The broader security rerun passed **348 tests in 94.38s**, with zero failures,
errors or skips, resolving the previous constructor errors. Exact commands,
JUnit, coverage and hashes are in `build/079/verification/security/`.

The credential-free public source and existing real Keycloak discovery are
reachable. The selected local Docker verification override preserves all existing
authentication/environment settings except enabling `FF_PERSISTENT_AGENTS` and
uses image-baked agent code instead of the broad mutable agent-source mount.
Owner sign-in/consent and in-product model availability still need live evidence.
No product push, merge or release has occurred.

Deep implementation is committed locally at
`756b338f3054bb8f509a2b94f0ac7c8b9b1b8cc3` (65 scoped files); the preexisting
untracked `android-client/` remains untouched. The candidate image built
successfully as `astraldeep:079-local`, image
`sha256:64c788a91176f45def3a84dc15e991a84c274e63cbb0e60c5842ceda469f660c`.
Its four exact local component wheels passed digest verification and `pip check`.

The local application was upgraded through normal guarded startup after a
verified paired snapshot of PostgreSQL and all five configured durable/source
roots. The private backup is outside Git at
`Y:/WORK/MCP/AstralDeep-079-precutover-20260905`; its manifest SHA-256 is
`58862c5fbcf7178329eb6b46c4ec324f0285e83273267d670360eebccd826100`.
All **1,335 files** were preserved and hashed, and every PostgreSQL custom archive
section was readable. Same-dump attachment metadata contained **zero READY/live
attachments**, two deleted records and 181 unreferenced snapshot files; all were
preserved. This is archive/copy verification, not a restored-database rehearsal
or a global data-repair claim. The local custom artifact root now has an explicit
mount from a new `backend/data/personal-agent-artifacts-079` directory; the
preexisting host artifact directory was left intact.

First candidate boot passed `/healthz` and `/readyz`, reports installed schema
`079.001`, and retains real authentication with the feature enabled only by the
local override. The evidence driver's deployment binding matched **590 reviewed
runtime files** against the actual running image and refused mutable code mounts.
Unauthenticated and invalid-bearer assignment requests both returned **401** with
`Cache-Control: no-store`; the unauthenticated shell returned **302** to login.
Exact read-only diagnostic command: `.venv/Scripts/python.exe
build/079/verification/check_candidate.py
build/079/verification/079-candidate-first-boot.json`. This establishes local
deployment and denial behavior; owner monitoring/controls/approval are still
unverified.

A second boot (`docker restart --timeout 30 astraldeep`) passed the same five
checks and all 590 runtime hashes against the already-upgraded schema; report
`build/079/verification/079-candidate-second-boot.json`. The existing voice worker
was restarted afterward; app/PostgreSQL are healthy and LiveKit/voice worker run.
This restart has no consented assignment and therefore does not establish live
assignment recovery or action deduplication.

Browser verification reached the real Keycloak login form through the configured
`http://localhost:8001` address. The numeric `127.0.0.1` callback is not permitted
by the existing OIDC client, although it works for the read-only deployment/API
probes. No Keycloak settings were changed. The owner login tab is left open;
owner sign-in, model availability and explicit offline consent remain pending.

Native strict coverage collection validates these exact Deep/Projection commit
identities but **does not pass the full report matrix**: Deep lacks a voice-worker
producer report; Projection lacks six required producer reports (Windows Python,
C#, JavaScript, iOS, macOS and watchOS). Authentic
partial unions, unchanged input recordings, hashes, exact commands and missing
inputs are retained in `build/079/verification/coverage/`. These diagnostic unions
do not waive any platform or release gate. Android XML reports were exported with
the committed Kover tasks from unchanged successful binary recordings, excluding
the two test tasks; the authoritative report parser accepts both XML reports.
The focused Projection rerun passed **45 tests in 1.87s**, tracing all three
changed Python files; **203/204 changed executable lines (99.51%)** were covered.

## Historical local implementation checkpoint — 2026-09-05

**Implemented and tested locally; exact component integration and authenticated live
qualification remain pending.** Deep, embedded Plane, and embedded Projection are
on `codex/079-persistent-agents`. Product changes are uncommitted and unpushed.
Sibling repositories remain on refreshed `main`. The existing running app is the
baseline image described below, not the feature candidate.

### Implemented behavior and ownership

- Plane owns guarded `079.001` migration, frozen records, durable assignment/event/action/activity storage, revision/control fencing, bounded claims and recovery, atomic task joins, budgets, approvals, reconciliation and retention. Representative `075.001` upgrade and repeat startup pass on isolated PostgreSQL.
- Deep owns standing-instruction policy, explicit offline grant capture, scheduled source checks, quiet waiting, bounded model plans and child execution, retained observations/findings, fresh per-effect authorization, exact attended sensitive execution, activity, REST/chat commands, and account-retirement coordination.
- Projection owns shared forms/cards/activity and cross-client action dispositions. The actual web form preserves every selected tool and unchecked consent. Deep removes only validated browser transport metadata before strict request parsing. Schedule inspection and pause/stop/revoke remain available without personal LLM setup, while new work and approval stay gated.
- The no-extra-connector example is a public release-page monitor through `web-research-1:fetch_page`. A general registered reader requires trusted host resource/precondition bounds and existing permission checks. No bundled inbox or bug-tracker reader was invented or presumed connected.
- `FF_PERSISTENT_AGENTS` defaults off. Tick/concurrency/lease configuration is bounded and fails closed. Mandatory daily/lifetime model/tool/token/time caps apply across children and retries; optional currency caps require trusted finite quotes. Unknown monetary usage is never represented as free.
- `backend/pytest.ini` now includes the new agent suite in normal discovery. The stdlib evidence driver is included in the existing measured tooling CI lane. No runtime dependency or new primitive was added.

### Automated results

All real PostgreSQL tests use disposable schemas in `astraldeep-079-postgres` via
the private DSN in `astraldeep-079-tests`. They do not mutate the application's
database. IAM, model responses and external responses in engine integration tests
are controlled fixtures; those tests are not authenticated live-source evidence.

| Check | Result and scope |
| --- | --- |
| Final Deep feature/UI/retirement/remote-confirmation run | **432 passed in 203.34s**, no skips; includes final memory, child authority, supervision, action-result and owner API changes |
| Full Plane suite | **2,176 passed, 9 explicit platform skips in 522.69s**; skips require Windows-native filesystem semantics unavailable in the Linux test container |
| Final Plane assignment slice | **96 passed in 14.17s**, enforced combined branch coverage **90.77%**; covers final currency and observation-cleanup edits made after the full suite began |
| Plane catalog/rollback/boot/provenance | **241 passed, 1 deselected**; the slow deselected startup case passed in the full suite |
| Deep central host/dispatch hooks | **23 passed in 9.95s**; existing failure and denial behavior retained |
| Final focused Deep UI | **173 passed**; browser request envelope, owner isolation, no-LLM controls and foreign-owner denial included |
| Existing first-run LLM gate | **14 passed** using the compatible installed baseline Plane; separate disabled-feature regression posture |
| Evidence driver | **34 passed**, **97.67% statement coverage**; missing source/session/deployment evidence fails closed |
| Tooling CI ownership and driver checks | **57 passed, 1 platform skip in 4.45s** after adding the new driver/test to exact CI ownership lists |
| Projection shared views | **45 passed** |
| Protocol/chrome/extraction boundaries | **34 passed** |
| Actual web client fixtures | **120 passed** across continuity, voice, and 079 suites; ESLint and product-isolation checks passed |
| Android | `ktlintCheck :app:lintDebug :core:test :app:testDebugUnitTest :core:koverVerify :app:assembleDebug` passed, **65 tasks in 1m26s** |
| Windows offscreen suite | **1,064 passed, 12 skipped, 3 failed**; all three BYO process-cleanup timing failures reproduced on unchanged Projection `b69597a` |
| Source hygiene | Root/component `git diff --check` passed. No Ruff diagnostics on changed Python lines; same tracked files show 731 current diagnostics versus 740 on their HEAD bytes. Full-repository lint is not claimed green. |

The real-PostgreSQL engine cases exercise source → plan → two children → atomic
join, fresh-runner memory after meaningful and initial `UNCHANGED` observations,
two crash boundaries with actual retry backoff, no repeated completed reads or
children, pause/resume/revision, late results after stop, completion, and trusted
currency accounting. Twenty-five idle assignments made zero model/tool calls;
the earlier retained fixture measurement observed maximum pause latency 49.61ms.
That latency is a local controlled workload measurement, not a live-client SLO.

### Coverage and retained reports

Current working-tree diagnostics report every new Deep production module above
90% statement coverage (90.80–100%); central dispatcher changes **72/76 = 94.74%**,
Deep UI **282/283 = 99.65%**, LLM control gate **18/18**, remote confirmation
**10/10**, retirement purge **18/18**, and retirement route **6/6**. Plane changed
production statements are **1,329/1,419 = 93.66%**. Multiline constant members have
no separate executable line in coverage; their containing statements and behavior
are tested. The final Deep coverage omits test files from the denominator.

Reports and exact owner-lane commands are retained under ignored
`build/079/verification/`, including `079-deep-final-{coverage.json,coverage.xml,results.xml}`,
`079-host-final-coverage.{json,xml}`, `079-driver-final-coverage.json`,
`chrome-llm-079-coverage.json`, `working-tree-diagnostics.json`, and
`plane/{handoff.md,full-junit.xml,full-coverage.xml,assignment-coverage.xml}`.
Use the final assignment report for Plane assignment files; the earlier full
report remains valid for unchanged API/schema/migration modules. Current central
coverage unions only reports from the same unchanged source bytes.

These are working-tree diagnostics. The canonical immutable-candidate
`scripts/check_changed_coverage.py` gate, final clean component identities and
qualified-image evidence have not run and are not replaced by this calculation.
The Projection bundle in `build/079/verification/projection/README.md` records
exact client commands and which results have native reports versus only explicit
tool-output transcriptions. `report-sha256.json` inventories the retained local
diagnostic files. `scripts/check_doc_links.py` passed for its 25 maintained inputs.

The curated knowledge checkpoint was separately committed and pushed as
`dd7a96edb9f54a61fdea35850e583c601aa171a8` in `Kentucky-Open-Science/kos-wiki`;
the vault is clean and matches origin/main. This preserves the decision/evidence
summary remotely, not the uncommitted product implementation.

### Exact Deep commands

From the repository root in PowerShell, with the isolated test container already
configured and without echoing its private DSN:

```text
docker exec -w /workspace/backend -e COVERAGE_FILE=/tmp/.coverage-079-final -e PYTHONPATH=/workspace/components/AstralPlane/src:/workspace/components/AstralProjection/src:/workspace/backend astraldeep-079-tests python -m pytest -q persistent_agents/tests tests/chrome/test_surface_assignments.py tests/chrome/test_surface_personalization.py tests/attachments/test_account_assignments_079.py tests/test_remote_confirmation_063.py --cov-config=/workspace/build/079/verification/coverage-079.ini --cov=persistent_agents --cov=orchestrator.orchestrator --cov=orchestrator.chain_authority --cov=orchestrator.remote_confirmation --cov=orchestrator.attachments.purge --cov=orchestrator.attachments.router --cov=orchestrator.attachments.account_lifecycle --cov=orchestrator.projection_surfaces.personalization --cov=orchestrator.projection_controllers --cov-report=xml:/tmp/079-deep-final-coverage.xml --cov-report=json:/tmp/079-deep-final-coverage.json --cov-report=term:skip-covered --junitxml=/tmp/079-deep-final-results.xml -o junit_family=xunit1 --tb=short

docker exec -w /workspace/backend -e PYTHONPATH=/workspace/components/AstralPlane/src:/workspace/components/AstralProjection/src:/workspace/backend astraldeep-079-tests python -m pytest -q persistent_agents/tests/test_host_wiring.py persistent_agents/tests/test_dispatch_integration.py tests/test_call_llm_wave0.py::test_reasoning_effort_passed_when_set

docker exec -w /workspace -e PYTHONPATH=/workspace/backend astraldeep-079-tests python -m pytest -q backend/tests/test_python_ci_supply_chain_060.py backend/tests/test_release_tooling_coverage_060.py scripts/tests/test_component_build_surfaces_074.py scripts/tests/test_verify_persistent_agents_079.py --tb=short
```

The coverage config contains `[run]` and `omit = */tests/*`. For the full Plane
and final assignment commands, see the copied Plane handoff; they use the same
isolated real PostgreSQL runtime. Client commands are also in `quickstart.md`.

### Known failures and pending inputs

1. **Exact component integration:** clean component commits are required before
   Deep can pin and build these Plane/Projection bytes. The existing composition
   remains unchanged. Target Plane schema is `079.001`, migration digest
   `2353261227ed72d030ab2426b1a7229c8a1302c669a241dc6b84e3e77e003cad`, catalog digest
   `ea985cd52e622f9febaed5783b312ca7177cc088ad9804d71891647087d99eeb`.
   Repository policy requires authorization before product commits; none has
   been created. No bypass, temporary fake pin or candidate-as-baseline claim is used.
   The concrete next step is local feature commits for the reviewed Plane and
   Projection changes, Deep's exact repin, then a local Deep feature commit for
   immutable candidate checks. After authorization, qualify that composition,
   rebuild the candidate image and rerun the blocked constructor/live checks.
   No product push is included in this proposed local integration step.
2. **Broader security regression:** explicit scheduler/machine-authority/offline-
   grant/chain-budget/delegation/permissions/remote-confirmation/admission suites
   returned **337 passed, 11 constructor setup errors** because the local 079
   Plane correctly rejects the still-pinned 075 composition. Rerun those cases
   after exact integration. These errors are pending integration, not baseline failures.
3. **Demonstrated baseline failures:** the unsupported-reasoning-effort retry test
   raises `KeyError` for its cache key in both unchanged baseline and candidate;
   a delegated dispatch-parity fixture lacks a raw token in both. Windows has
   the three reproduced BYO cleanup timing failures above. Assertions were not weakened.
4. **Live feature checks:** driver `--help` and deterministic tests pass. Read-only
   comparison with the actual app refuses `deployed_runtime_differs_from_candidate`.
   No public-page monitor, owner consent, sensitive approval or post-restart
   assignment has been exercised against the live candidate. The driver cannot
   create consent, sign in, approve, or restart the server. Its reports explicitly
   separate controlled revisions/fault tests from live upstream observations.
5. **Client availability:** authenticated live web/Windows/Android flows remain
   pending the candidate. Android connected device tests were not run. Apple
   builds/tests and macOS/iOS/watchOS live flows need a qualified Mac/Xcode host.
   No platform waiver, deployment, merge or release is claimed.

## Initial baseline — 2026-09-05

Historical state at the initial checkpoint: specification/design preparation only.
The local implementation checkpoint above supersedes this implementation status.

- Root main and origin/main: `34609998a008c5f49fbaa8b24363f794b23c2ba2`; feature branch `codex/079-persistent-agents` starts there.
- Sibling main checkouts: Projection `b69597a05fa9c98272a66d69500553160712c94f`; Plane `9c1990c4d02d80a310f6d1c35f1e8b1d814a854d`; Primitives `4056df95acd992a9f84d883e572760f6da24c88e`; LETS `583f2d3e6dca85acbee24b046a703319d72a66a4`. All matched refreshed origin/main.
- The product retains its exact composed pins, which differ from sibling main where deliberately pinned. No incidental component update was introduced.
- 25 ancestral historical local refs fast-forwarded using old-SHA compare-and-swap. Divergent `codex/external-agent-identity-claims` retained at `1a63ea5fb4ff457433b5d708db17e478295638be`. No branch/worktree deletion. Untracked root `android-client/` preserved.
- `python scripts/verify_composition.py --root .`: initially failed due to Primitives `base.py` CRLF checkout; exact HEAD blob restoration and component-local autocrlf=false plus unchanged-index refresh restored a clean component. Final PASS, composition digest `9e9c9dc1af2c3aa88dcf82cf182a2a70bd0f876a7b8ed8bfab6b2492e9718105`. No tracked Primitives change or modified contract pin.
- Host `.venv/Scripts/python.exe --version`: Python 3.11.15. Initial host environment lacks pytest/runtime dependencies; container test runtime supplies pytest and psycopg2.
- `docker compose up -d --build`: PASS. Baseline app image `sha256:a33e6c5b7a1c1b74b1b2095b95e5f5c9a2e85fcb4d1e09ef10d0665dd44cf810`; app and PostgreSQL healthy, LiveKit and voice worker running. This is baseline infrastructure, not Feature 079 live verification.
- `docker exec astraldeep bash -c 'cd /app/backend && python -m pytest -q tests/test_machine_turn_authority.py tests/test_chain_budget.py scheduler/tests/test_handler_eligibility_060.py'`: **32 passed in 2.51s**.
- Existing `.gitignore` and `.dockerignore` exclude virtualenvs, compiled output, credentials and runtime state; no new ignore rule required for the proposed package.
- Spec ownership prerequisite paths for clarify/plan/tasks matched `specs/079-persistent-agents` with explicit `SPECIFY_FEATURE_DIRECTORY`.
- Curated vault checkpoint committed/pushed as `f060e56e2bcd4bd1f2fa982eefef5a6c3a909894`; product work remains local and uncommitted.

The original baseline observations remain historical evidence. The current
integration checkpoint supersedes their commit, pin and constructor-error status.
