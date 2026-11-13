# Spec 105 stopping-point handoff

**Date:** 2026-11-12. **State:** stopped at the owner's explicit request. All task agents and active
checks have stopped. No further integration is authorized by this handoff itself; resume only on a
new owner instruction. Product changes are local and unpushed. No phase, protected-CI, deployment or
release completion is claimed.

## Exact stopping state

Deep's primary composition pin remains unchanged. The qualified Plane update, portable contention
adapter and schema-reference security fix are preserved in a separate candidate; they were **not**
merged into the primary branch. Apple operations/wrap-key custody is also a separate, tested branch.
The production mesh runtime is not wired end to end.

| Repository / checkout | Branch and frozen revision | State |
|---|---|---|
| Deep primary | `feat/105-personal-mesh`; implementation through `5097101d`, prior handoff `f552b4e9` | Reliability/node/protocol lanes and authorization repairs integrated. Canonical verification harness requires the newer cleanup API; old-pin setup refuses before fixture writes. This stopping handoff follows those commits. |
| Deep candidate | `codex/105-plane-candidate` at `db602926`; worktree `/tmp/astral105-verification-candidate` | Clean, named local recovery anchor. Contains `5100bd29` (Plane pin/floor/schema guards + portable retry), `fa8d2ab3` (schema reference fix), `db602926` (dependency authority hashes). Not adopted by primary. |
| Deep schema lane | `codex/105-schema-egress` at `02dc072` | Same security fix already cherry-picked into the candidate; do not apply it twice. Implicit reference retrieval disabled, bounded errors returned before dispatch. |
| Kernel | `codex/105-kernel-integration` at `29f3d68` | All crate lanes integrated; actual provider/model admission, resumed authorization, branch context, canonical FFI and real encrypted-log sink repaired. Original main preserved. |
| Projection primary | `feat/105-personal-mesh` at `5762791` | Identity, voice, ROTE, exact consent binding and voice setup/synthesis follow-up integrated locally. Full native gate remains failing. |
| Projection custody | `codex/105-apple-custody` at `1cb707c` (includes `758b093`) | Not merged. Device-only Ed25519/X25519 keys, stable marker access, pending/ready provisioning and fail-closed legacy/corruption/restore handling. Only process-local initialization serialization is claimed. |
| Primitives | `feat/105-personal-mesh` at `f4314c8` | Stable A2UI 1.0 schemas pinned to upstream `8bf644a37d680a227296b8c0849a7f4b328b8b73`; unreleased 0.13.0. |
| Plane | `feat/105-plane-sqlite` at `cbf621a35018c2491dc6ee5865a6798fe2d8a396` | Clean/frozen separate worktree `AstralPlane-wt/sqlite`. SQLite implementation and shared qualification complete within the scope below; primary Deep pin unchanged. |
| LETS / Vessel | LETS `1321306`; Vessel `fd0f086` | Device-profile work exists in LETS. Vessel convergence and Mac node packaging remain unfinished. |

Preserved unrelated Deep working-tree state: `AGENTS.md`, the pre-existing Plane/Projection component
pointer changes and `.playwright-mcp/`. Do not stage these blindly. Deep's Plane checkout remains at
`2762386`; it is an ancestor of `cbf621a`, but it was not advanced. The ROTE composition lane is still
separate. Do not recreate another machine's work because these commits are absent from a remote.

## Last recorded verification

| Scope | Result and limit |
|---|---|
| Deep root suite at `236354d0` | **23,248 passed**, 16 skipped, 44 passing subtests; 775.73s. Changed executable Python coverage 1,563/1,723 = **90.71%**. Precedes later harness/security work. |
| Initial Plane/portable-retry candidate full root run | **23,251 passed, two failed**, 16 skipped, 44 passing subtests; 381.17s. Failures were exact gitlink/config mismatch and dependency inventory hash; both corrected afterward. This run preceded the schema-reference fix. |
| Corrected candidate guards | **49 passed** after staging the exact Plane gitlink and refreshing the reviewed dependency hash. Historical cutover evidence was not rewritten. |
| Latest complete candidate focused run | **154 passed** in 18.14s: schema/reference/dispatch checks, dependency guards, full verification suite and real canonical cleanup probe. A full root rerun against final `db602926` is still required. |
| Canonical verification | Focused 50 passed; new source 33/33 executable lines covered. Candidate full verification 83 passed; real cleanup probe preserves neighboring issuer/run rows. Primary old pin: 75 passed, four failures/four setup errors at intentional preflight before writes. |
| Schema-reference fix | **71 focused passed**, 97% module coverage, 6/6 changed executable production lines covered. Existing `referencing==0.36.2` promoted to direct backend/CI dependency; every resolved package version/hash unchanged. Candidate authority hashes refreshed. |
| Portable contention | **28 passed**, including real PostgreSQL concurrent voice checks and new genuine/forged/post-callback/exhaustion regressions. Retries stay bounded to four attempts before callback start. |
| Plane exact frozen Linux source | **4,671 passed**, 169 explicit PostgreSQL/Win32 skips; 79.49s. Final PostgreSQL/Linux seams **307 passed**, seven backend/platform skips. Changed-line gate **1,605/1,692 = 94.85%**. Publication predicates/savepoint rollback, native lock classification, full cursor deadlines and fresh clocks covered. |
| Kernel exact integrated workspace | **657 passed**; formatting and strict workspace/all-target clippy passed. Existing `proc-macro-error2` future-compatibility warning remains. |
| Built Kernel bindings | All five Apple targets rebuilt in 413s; wrapper six tests passed on each platform. Linux arm64 wheel built with two compiler jobs in 226s; Python 3.11 store/signature/canonical-refusal/local-endpoint-reopen smoke passed with external networking disabled. |
| Primitives | **2,477 passed**; schema generation and Ruff passed; A2UI module coverage 96%. Test-only `regex` dependency declared in its owning development/CI groups. No publication. |
| Combined Apple consent/voice | **51 passed on each platform** using final Kernel artifacts; voice UI **three passed on each platform**. Voice model suite 39 tests with one optional diagnostic skip; seven real-weight parity checks passed. |
| Unmerged Apple custody | **38 passed on macOS and 38 on iOS 26.5**, zero failures/skips. Real Keychain round-trip and hardware-reporting tests were explicitly not run for this change. |
| Latest primary full macOS gate | **1,345 tests, one skip, 32 failure records across 23 testcases**, exit 65; 283.116s. Four records are known scheduled-work baseline failures; **28 accessibility/window/action records remain unqualified**. No fixture workaround or weakened assertion landed. |
| Mac local-input comparison | The formerly failing CGEvent testcase passed on exact baseline `102ea6a`, primary `5762791`, and within the latest primary full run. Its earlier six assertions were not reproduced; this does not qualify the full native gate. |

Earlier combined nested-module diagnostic: 3,471 passed, five failures/four setup errors. Two timing
failures passed unchanged independently; canonical-owner failures drove the repaired harness and
candidate proof above. Run nested suites separately as CI does. A prior full run killed by overlapping
Docker Rust compilation is invalid and must never be counted as a pass or a product failure.

No live backend/client end-to-end mesh conversation, real biometric approval, interactive enrollment,
real microphone/speaker/route qualification, protected CI, deployment or release was performed.

## Plane metadata and adoption boundary

- Package/lockfile: `0.1.12`; schema: `089.022`.
- PostgreSQL migration digest: `dde302967743cf8f6f5d55e82f8355ef405823dfee638909c3a2f11b85f5cb27`.
- SQLite migration digest: `e4ae392ed2951f1446cf065185a6b348bbed49c3e8cf37ca81546874887f6ef5`.
- SQLite structure digest: `5f45079d23dc20e1dc0bb1367e8076c55a582ba7df7a76224fc0cdbe1a2b4d0b`.

A Plane wheel was built into `/tmp/astral105-plane-wheel`; it was not installed or smoke-tested in a
new task image before stopping. Source-overlay qualification is not a production image rebuild.
Deep still calls `create_postgres_runtime` and expects PostgreSQL's registry. Its SQLite host selection
and exact SQLite composition metadata remain to implement; changing the library pin alone does not
enable a SQLite node. PostgreSQL timeline/failover/joint-restore APIs remain backend-specific. Mac
native no-replace publication failures reproduce on Plane base `529120e`; Linux counterparts pass.

## Resume order

1. Read current repository/vault instructions and statuses before changing anything. Inspect the
   named candidate rather than redoing finished lanes. Qualify its final source with the full backend
   root suite and separate touched module suites, verify the built Plane wheel/new API in an isolated
   image, then deliberately integrate the three candidate commits and exact Plane checkout/pin/floor.
   Preserve the unrelated working tree. Product push remains unauthorized.
2. Review/integrate Apple custody branch `1cb707c` with its parent; it is not in primary. Complete real
   Keychain/Secure Enclave verification and investigate the latest full native accessibility failures.
   Do not classify all failures as baseline or weaken native gates.
3. Continue [runtime-composition-plan.md](runtime-composition-plan.md): verified durable roster/key
   providers, enrollment/feed/restore coordination, engine/gate/consent/tool composition and complete
   FFI; then the actual native chat/voice link. `LogEventSink` exists but is not a full runtime.
4. Resolve the uncapped-read/action-receipt contract seam and model-token accounting across attempts.
   Preserve current authorization/PHI/egress/confirmation gates and unknown-effect outcomes.
5. Finish Deep SQLite composition, ROTE component adoption, supervised Mac node packaging and Vessel
   loop convergence. Voice release needs the owner-published immutable model revision; live sign-in
   and biometric decisions stay with the owner. Android/Windows runtime ports retain their scoped plan.

## Retained local evidence and services

Durable local diagnostic copies and SHA-256 manifest:
`.superpowers/sdd/handoff-2026-11-12/retained-evidence.json` (gitignored). It retains 21 reports, logs,
runners and the Plane wheel. These are diagnostics, not canonical protected release evidence.
Original XCResults remain under their `/tmp/astral*` derived-data paths and may be purged; important
counts are preserved here. The named candidate branch keeps its commits recoverable if `/tmp` is lost.

Backend qualification used only isolated PostgreSQL 17/task containers, unprivileged init-enabled
workers, exact read-only component sources, Node 24, scratch tmpfs and a masked `.env`. The shared
`astraldeep` stack/database were not recreated or modified. The task-owned `ad105-codex-pg` fixture is
stopped without removal for this handoff; restarting it is a future test setup action, not deployment.
No model weights, credentials, generated user code or private runtime data were staged.

## Second stop, later on 2026-11-12

The owner said "continue" after the first stop, then asked for a stopping point again. Every agent and
every task build is stopped, task containers are stopped (not removed), and nothing is pushed. The
first stop's state above still holds except where this section says otherwise.

| Lane | Branch and revision | State |
|---|---|---|
| Kernel runtime | `k/runtime` at `55ad4fd`, from `codex/105-kernel-integration` | Real facade and FFI for mesh creation with attestation, pairing over `astral/pair/1`, durable roster and keys, chats, sync with relays configurable, listener. Eight two- and three-runtime mesh flow tests pass. Rust traits `HostKeys`, `PresenceSigner`, `ConsentPromptSpec` in `astral-kernel/src/consent.rs`; FFI `HostSigner`, `ConsentPrompt` in `astral-kernel-ffi/src/host.rs`. `Kernel.open(config, signer, listener)` replaces the spike constructors. Not done: mailbox client in the sync loop, restore marker after a pairing join, recovery-word add flow, `preview_pairing`, `record_spoken`, Apple and Linux binding builds, the Python binding test (written, uncommitted). Uncommitted follow-up work is saved as `wip/k-runtime-followups` (`6740e9f`). |
| Kernel turns | `k/turns` at `024c301` | Committed: the receipt seam (effect kind is a verifier input, reads carry no receipt, `ticket.receipt_unexpected`, contract commit `3e146215` here), model-token metering, per-attempt model admission that fails closed with `admission.no_context`, label sources on gate requests, no redirects to model or MCP servers. `astral-engine` 180 tests pass. `TurnService`, providers, egress validation, the SQLite allowance store and the turn FFI were in progress and are uncommitted and untested; they are saved as `wip/k-turns-turnservice` (`7cb66f8`). |
| Apple mesh app | Projection `feat/105-mesh-app` at `55a83ec` | Custody branch merged. Keychain service names derived from the bundle identifier. `MeshKernelAPI` protocol mirroring the FFI, an in-memory kernel, `MeshRuntime`, and mesh screens (create with recovery words, join with code and QR, add device by paste or scan, devices with remove and stop, chats from the log, consent cards, provider form, allowance, voice link). 81 mesh tests pass on macOS and iOS. Full macOS `AstralAppTests`: 1,426 tests, six records in one CGEvent test that passes alone. Full iOS: four records in the known scheduled-work test. Not bound to the real kernel: a production build says the mesh runtime is not included yet. Asks for FFI `previewPairing` and `recordSpoken` and a handle resolver. |
| Deep SQLite node | `feat/105-sqlite-node` at `dd61b1a6` | Merged the Plane 0.1.12 candidate (`4cbdcff0`), added SQLite or PostgreSQL selection for Plane with the exact SQLite registry pin (`e7240511`), composed AstralKernel and `astral-rote` (`044c90b8`, `dd61b1a6`). The full root suite on this branch and the SQLite node smoke were not finished. One uncommitted test edit is saved as `wip/feat-105-sqlite-node` (`12949f8f`). |

### Resume order now

1. Finish `k/turns` from `wip/k-turns-turnservice` (review it, test it, commit), then the runtime
   follow-ups from `wip/k-runtime-followups`.
2. Merge `k/runtime` and `k/turns` into one kernel branch: adapt `PresenceSigner` to the turns lane's
   consent signer, append the turn variants to `KernelChange`, reconcile the `ticket_vectors` count,
   then build the Apple bindings and the Linux wheel and run the Python binding test.
3. Bind Projection `feat/105-mesh-app` to the generated bindings and update the wrapper's
   `KernelService` for `Kernel.open`.
4. Finish `feat/105-sqlite-node`: the full root suite and module suites, then boot a personal node on
   SQLite end to end.
5. Then the command-line peer that loads the model credentials from `.env` as a sealed item, the node
   joining the mesh through the Python binding, the Mac node pack, Vessel convergence, and the live
   tests on the Mac and the iPhone simulator, which need the owner for Keycloak sign-in, Touch ID and
   the Hugging Face upload of the LFM3 weights.

Task containers stopped without removal: `ad105-codex-pg`, `adwt-pg-phase-r`, `adwt-pg-rote`,
`plane-pls-pg`, `plane-sqlite-contract-pg`, `sqlite-node-issuer`. The shared `astraldeep` stack was not
touched.

## Third stop, 2026-11-13

The owner asked for a stopping point and approved pushing every spec 105 branch except
AstralKernel. All agents are stopped. Nothing is merged to a default branch, released or deployed.

### Where everything is

| Repository | Branch and head | State |
|---|---|---|
| Deep | `feat/105-integration` (this handoff) | Every Deep lane merged: SQLite node, node peer with requests and tickets, institution profile and push wake, Vessel executor adapter (per-agent tokens, no owner or workspace in the action), and all contracts through attachments, node minting and ticket expiry. Contracts: 23 vector files match, 1,274 reference tests pass. Kernel wheel pin is still 12e7cdc. |
| Deep | `feat/105-personal-mesh` de9f5f8c | Spec and contracts up to invitations. Behind `feat/105-integration`. Not moved because the main checkout holds unrelated owner changes. |
| Kernel (local only) | `k/integration` 34ef4a5 | Merges `k/node-mint` and `k/attach` with a schema 2 to 3 log upgrade test. The full test run, flake loops and artifact rebuild after the merge did NOT finish. Last verified artifacts are 22513f9 (1,054 tests) and the two lanes alone (1,084 and 1,111 tests). |
| Projection | `feat/105-mesh-final` 503988a | Full UX pass on Mac and iOS, invitation pairing, rename, Markdown, metric cards, settings rows, node pack merged. macOS 202 tests, iOS 900 (1 skip), MeshModeUITests 17, all passing. Kernel artifacts 22513f9. |
| Vessel | `feat/105-vessel-final` 3c50ea8 | Executor with the pinned kernel wheel, per-agent tokens, persisted replay memory, real ticket end to end, node env wiring. 147 passed, 1 skipped. |
| Plane | `feat/105-plane-sqlite` f8f2a63 | SQLite backend with the write-lock fix. |
| LETS, Primitives | `feat/105-personal-mesh` 1321306, f4314c8 | Unchanged since the second stop. |

### Live state on the owner's Mac

- The redesigned localtest apps (503988a, kernel 22513f9) are staged. The iPhone simulator
  33EFFC80 opens the existing mesh with its chats and three devices.
- The Mac app waits on a Keychain prompt for `com.personalailabs.astraldeep.localtest.mesh-device`
  caused by the new ad-hoc signature. The owner answers it, then checks chats, devices and provider.
- Backup of both apps' data before staging:
  `AstralProjection-wt/localtest-backup-20261113/` (no Keychain items). Restoring
  `mac/ApplicationSupport` is safe if the Mac fails after the prompt.

### Resume order

1. Kernel `k/integration` 34ef4a5: run `cargo test --workspace --locked --no-fail-fast` with
   vectors from this branch, loop the known flaky tests 20 times, then rebuild the Apple
   xcframework, the Linux wheel (smoke), the Linux mailbox and the macOS CLI with SHA256SUMS.
2. Deep: pin the new wheel and mailbox, let the node call `mint_ticket_for_request` for executor
   tools, and trace why a Vessel node listed no tools ("node signing key is missing").
3. App: bind attachments and the merged artifacts, run the AstralCore package tests, re-stage with
   a fresh backup.
4. Write the missing automated tests listed in [qualification.md](qualification.md), smallest first.
5. Measure the live valid-surface rate with the provider credentials from `.env` through a script.
6. Owner: the UX review in the staged apps, invitation pairing, and the voice test on the Mac.

### Open gaps

- No mesh event tells an owner device that a node needs consent, so node tools that need it fail
  closed with `consent.required`.
- The model does not see attachments, and nothing imports A2UI from tool results automatically.
- There is no owner-initiated allowance transfer and no way to create a PHI chat in the app.
- No Android or Windows kernel build. Vessel CI needs an arm runner and the wheel artifact.
- The website downloads change stays unpushed until the new client is ready to deploy.
