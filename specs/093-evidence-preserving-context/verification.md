# Implementation and qualification record

Date: 2026-10-08. Full implementation remains on the authorized `codex/093-evidence-preserving-context` branch for original PR #232. Design commit: `bc19e27eadb31263a893e6b3826f384e1027950c`; incorporated main: `5c35bb38227055dadf25a02d0ac17e70d4bfbc8e`. Qualification reports produced after the clean candidate commit must bind that exact SHA and remain outside the candidate tree; a local passing report is not a hosted CI or release decision.

## Implemented behavior

- Separate default-off packing and safe-compaction controls. No new runtime dependency, database migration, component pin, primitive or protocol change.
- Explicit exact-scope, absolute-expiry retention grants; ephemeral integral UTF-8 archive, bounded pages/previews, no silent eviction or recovery after restart. The host privacy cap is stricter than archive capacity.
- Original-task typed IAM/work-control lease and current source/delegation/permission/policy/confirmation/privacy checks through ordinary dispatch. Capture, source release and provider admission recheck after awaited audit or authority work. Revoked references and lost source-authority metadata cannot reveal prior existence.
- Bounded owner/provider-qualified request windows, actual SDK message serialization, completion bounds and fresh encrypted USER provider capture. Full governing instructions/current request/tool groups/outcomes survive proposal failure, cancellation, stale state and irreducible limits. Auxiliary compaction has no system/provider fallback or persistent-reservation reuse.
- Stable, deduplicated physical-attempt audit receipts account for direct/auxiliary/retry/cache/failure/cancelled/late work and recall bytes. Usage disclosure refreshes audit provenance; historical or unlinked calls preserve a partial-accounting label and known tracked totals. Missing provider usage/prices never establishes zero cost or savings.
- Code-owned literal KeyValue/Badge/Alert/Button source presentation and supported watch handoff. Pages, previews, derived answers and reasoning stay transient; they cannot become workspace/chat/completion-summary copies, generated skills or action-shaped UI. Unavailable generated summaries render blocked rather than claiming completion.
- Frozen 24 independent synthetic documents/tasks, separate 12/12 development and held-out controls, actual corruption/cancellation diagnostics and candidate/workflow/scoring/provider/cost/formative bindings. Optional JSON consistency does not authenticate provider execution or human observations and never changes defaults.

## Local evidence collected during implementation

These results precede the final clean-candidate whole-suite and hosted gates; they are recorded separately rather than relabeled as those gates.

| Check | Observed result |
| --- | --- |
| Python 3.11 feature + adjacent authorization/dispatch/audit suites with isolated real PostgreSQL and signed synthetic IAM | 1,213 passed in 166.68 seconds at 19 unchanged source anchors; changed executable lines 2,632/2,685 (98.03%), orchestrator 203/210 (96.67%). Every affected source file exceeds 90%; the final summary flag-toggle and disabled-before-admission regressions pass. |
| Authority + service source-release tests | 245 passed in 80.14 seconds at unchanged source anchors, including late-page PHI changes, actual post-hook revocation, and refreshed unavailable/accounting cases. The independent archive suite also passed 126 cases. |
| Real SDK wire + model suite after SDK/disabled-reference guards | 74 passed in 18.54 seconds. |
| Ledger refresh and atomic legacy-gap recovery | 98 passed; 402/415 module statements covered (96.87%). |
| Root Ruff and whitespace diff check | Passed after source integration; repeat on final candidate. |
| Exact composition and initialized local component validation | Passed; all four original pins unchanged. |
| Swift core, `swift test --package-path components/AstralProjection/apple-clients/AstralCore` | 296 tests passed. |
| Portable Windows-client Qt suite on macOS, `QT_QPA_PLATFORM=offscreen python -m pytest components/AstralProjection/windows-client/tests -q` | 2,081 passed, 19 OS-specific skips. This is not a Windows OS or Windows live result. |
| Android committed wrapper, `ktlintCheck :app:lintDebug :core:test :app:testDebugUnitTest :core:koverVerify :app:assembleDebug` | Passed with configured local SDK/JBR; 79 Gradle tasks. |
| Unsigned canonical `AstralApp` iOS Simulator and macOS builds, and `AstralWatch` watchOS Simulator build | All three passed with candidate endpoint build override; no client source/pin change. |

Detailed local coverage and source-identity reports are retained outside Git. Synthetic fixture DSNs, keys, tokens, user grants and source content are excluded from this record and commits.

## Pending candidate and hosted qualification

Run the clean production image build, development/fail-closed production boot smoke, all three complete backend/module groups, strict changed-code coverage and remaining owner-CI gates on the final exact SHA. The required hosted aggregate is `Deep owner CI aggregate`; a portable native test result or production-configuration boot smoke does not prove real authentication or live UI acceptance.

Exercise ordinary authenticated source capture, preview, exact paging, summary, missing, blocked and usage behavior on web, Android, macOS, iOS and watchOS against that candidate image and configured IAM/PostgreSQL/worker environment. The owner will complete native sign-in. These live checks are pending at this record's creation and are not claimed as passed.

The owner explicitly directed on 2026-10-08: “Don’t worry about windows but record that windows is not tested.” Windows OS/live verification is therefore omitted and untested. Preserve that limitation in the PR and final handoff; the portable Qt suite cannot replace it. This instruction does not create Windows release qualification or satisfy all-client default-promotion inputs.

## Promotion evidence

The deterministic diagnostic currently reports 96/96 passing task/boundary runs, 48 actual synthetic auxiliary attempts, four integrity refusals and four provider cancellations. Rerun it against the final clean candidate identity. Actual paired provider complete costs, five representative users' unaided distinction/recovery observations and complete ordinary product/all-six-client qualification are missing inputs. Their absence is explicit, not a passing score, a cost saving or a population-wide usability finding. Both defaults remain off; no deployment, signing, release or default promotion is authorized or claimed.
