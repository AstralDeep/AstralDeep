# Local integration handoff

**Date:** 2026-11-12. **State:** implementation in progress. Product branches are local and unpushed;
there is no phase-completion, protected-CI, merge, deployment or release claim. Continue with
[runtime-composition-plan.md](runtime-composition-plan.md), then the phase criteria in [plan.md](plan.md).

## Integrated code

| Repository | Local integration | Behavior and important files |
|---|---|---|
| Deep | `feat/105-personal-mesh`, through `5097101d` | Provider deadline/fallback/argument recovery; issuer-and-subject owners; audience validation; authorized global trust writes; signed A2A cards and publication; MCP definition pins rechecked at dispatch. Verification now uses real verified canonical owners and refuses setup before writes if issuer-scoped cleanup is unsupported. |
| Kernel | `codex/105-kernel-integration`, through `29f3d68` | Core/log/mesh/authz/engine/UI/ROTE/net/mailbox integrated. Actual provider/model admission, current resumed-dispatch gates and branch-specific context repaired. Strict canonical-JSON facade/FFI and scoped log query added. `LogEventSink` adapts the real encrypted log off the Tokio executor, preserves labels and refuses unavailable bodies. |
| Projection | `feat/105-personal-mesh`, through `5762791` | Identity, voice and ROTE branches integrated. Exact canonical consent binding, unavailable-biometry refusal and the shared Rust parser are in place. Voice setup is shared by authenticated shells; Start returns through the owner/chat-fenced model route, and synthesis errors surface without recording speech as heard. |
| Primitives | `feat/105-personal-mesh`, `f4314c8` | Stable A2UI 1.0 upstream schemas/license frozen offline at `8bf644a37d680a227296b8c0849a7f4b328b8b73`; catalog metadata/reserved keys/Text variants aligned. Python and Rust conformance checks use the same frozen inputs. |
| Plane | Event/run/learning lane at `529120e`; SQLite WIP checkpoints `bc6b277`, `b0fcca8` | Schema `089.022` and package 0.1.12 are not adopted by Deep. Shared SQLite/PostgreSQL suites passed, but Linux publication integration found an additional SQLite CTE branch requiring repair. Issuer-scoped cleanup is implemented and proven in a temporary Deep candidate. |
| LETS / Vessel | LETS feature `1321306`; Vessel unchanged `fd0f086` | LETS device-profile formats and vectors exist. Vessel loop retirement, ticket executor integration and the supervised Mac node pack remain work to do. |

Deep's pre-existing `AGENTS.md`, Plane/Projection component-pointer changes and `.playwright-mcp/`
were preserved. Do not stage them incidentally. The ROTE composition branch remains separate until
exact component revisions can be qualified together. No model weights, credentials, generated user
agents, uploads or private runtime data belong in a commit.

## Verification recorded

| Check | Result and boundary |
|---|---|
| Changed backend cohort | 1,366 tests passed across 52 changed modules; changed executable Python coverage 96.17% at that checkpoint. |
| Full backend root suite | At Deep `236354d0`: 23,248 passed, 16 skipped, 44 passing subtests in 775.73 seconds. Exact full-run changed executable coverage 1,563/1,723 = 90.71%. This precedes the later verification-harness change. |
| Security repairs | 259 related tests plus two additional denials passed; changed security code 46/46 executable lines covered. |
| Exact schema guards | Seven tests passed after updating stale `089.015`/digest assertions to Deep's already-pinned `089.020`. Four old failures were first reproduced on starting commit `14c0f906`. |
| Changed Python lint | Ruff passed for all 119 changed Python files, using the isolated CI tooling image with `--no-cache`. |
| Kernel workspace | At `29f3d68`: `cargo test --locked --workspace` passed 657 tests. Formatting and strict clippy passed for the integrated workspace before the sink, then the sink/facade all-target clippy passed. Dependency `proc-macro-error2` retains a future-compatibility warning. |
| Log query | Two new real-store tests cover peer sync, reopen, exact scope/labels/order, unknown/new scopes, absent keys and corrupt bodies; the whole log crate suite passed. |
| Built bindings | All five final Apple library targets rebuilt in 413 seconds. Swift wrapper six tests passed on each platform. Linux arm64 wheel built with two jobs in 226 seconds; Python 3.11 smoke passed canonical refusals, store open, signing/tamper denial and local endpoint identity after reopen, with external networking disabled. |
| Consent binding | Original substring bypass reproduced RED. Current focused tests: 29 passed on macOS and 29 on iOS 26.5, with no skips. No real biometric prompt or sign-in was performed. |
| Combined Apple changes | Primary integration: 51 consent/pipeline tests passed on each of macOS and iOS with final rebuilt Kernel artifacts. Voice UI: three passed on each platform. Full voice-lane macOS AppTests remain unqualified: 1,290 tests, one skip, ten assertions across two untouched test cases; four scheduled-work assertions reproduced on baseline, six local-input assertions remain unqualified. No AX fixture workaround landed. |
| Verification harness | At `5097101d`: focused 50 passed; new executable source 33/33 covered. Old Plane pin: 75 passed, four failed/four errors at intentional setup preflight before writes. Temporary `b0fcca8` Plane candidate: full 83 passed plus a real cleanup probe preserving neighboring issuer/run rows. Primary pins unchanged. |
| Nested-module diagnostic | Combined diagnostic: 3,471 passed, five failed/four setup errors. Two timing failures passed unchanged when rerun independently; canonical-owner failures led to the harness repair and candidate proof above. This combined invocation is not the separate-suite CI gate. |
| Primitives | Full suite: 2,477 passed. Schema-generation check and Ruff passed; A2UI module coverage 96%. Test-only `regex` dependency added to its owning development/CI groups and lock. Version 0.13.0 is unreleased. |

Full Deep qualification against the final Plane candidate remains necessary before adopting its pin.
SQLite publication repair, complete native qualification and the production runtime remain unfinished.

The discarded full-backend rerun in `astral105-deep-final-checked.log` is **invalid**: a concurrent
Linux Rust build exhausted Docker memory and killed xdist workers. The build was stopped, and the
Linux build script now defaults to two compiler jobs. Run backend qualification and Linux compilation
sequentially. Do not count the invalid run as a test pass or a product failure.

## Reproducing the local checks

All backend checks used an isolated PostgreSQL 17 container/network with disposable databases, an
unprivileged test container with an init process, read-only source and exact pinned component sources,
the committed backend CI lock, Node 24, scratch tmpfs mounts and `.env` masked. The shared
`astraldeep` stack and `astraldeep-postgres` were not recreated or modified.

Local runner `/tmp/astral105-run-final.py` captures that setup. Test commands from the backend root:

```text
python -m pytest -q tests -n4 --dist=loadfile --cov=orchestrator --cov=llm_config --cov=shared --cov=verification --cov-report=json:/checks/deep-clean-coverage.json --cov-report=term:skip-covered -o cache_dir=/tmp/pytest-cache
python -m pytest -q tests/test_schema_revision_guard.py tests/test_voice_migration_065.py tests/test_voice_backend_rollback_075.py -o cache_dir=/tmp/pytest-cache
```

Run each nested suite independently, following `scripts/run_backend_web_tests.py` and CI. Root
`backend/tests` is not all backend verification. Local temporary logs are diagnostic and are not
canonical protected release evidence.

## Remaining acceptance boundaries

- The facade still needs host operations/wrap-key custody, persisted verified roster/key providers,
  enrollment/feed coordination, the production engine/gate/consent/tool adapters and the complete FFI.
- Uncapped reads currently have no allowance dimension while offline ticket verification requires an
  action-bound receipt. Resolve the contract/reference/LETS/Rust/vector seam together before enabling
  real peer effects. Model-token accounting and per-attempt current authorization also need wiring.
- Native consent supports roster add/remove factories only; unsupported kinds refuse. Kernel
  certificate/roster-state validation remains separate from host display binding.
- Voice still needs the real runtime link, delivery-aware history for interrupted speech and safe
  separation of generation interruption from unsafe tool effects. A verified adapter does not establish
  a working offline conversation. Model release qualification needs an owner-published immutable revision.
- Apple native builds/tests, live microphone/playback, interactive identity enrollment, real biometric
  approval/denial, offline chat and two-device synchronization remain distinct acceptance evidence.
- SQLite qualification and portable contention handling must finish before Deep's Plane pin changes.
  Packaging, Vessel convergence, institution authority, runs/learning and later-client runtime ports
  retain their own phase criteria.
