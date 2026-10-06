# Clarification gate — 2026-10-05

Explicit target: `specs/094-apple-wrapper-controls`, local branch `codex/094-apple-wrapper-controls`. Origin was refreshed and every origin spec tree checked; no remote feature 094 conflicts with this local target.

This is the initial October 5 allocation record. Main subsequently gains the separate `094-fhir-agent` directory; current integration preserves it and retains the explicit Apple target above. No other feature artifacts or ownership are changed.

The owner authorized the complete feature with "Fix it all and fix it good." No critical ambiguities detected worth formal clarification. Zero questions asked. The specification status and authorization assumption were updated; existing requirements and scope remain unchanged. Quality checklist: 16/16 → 16/16, zero regressions.

| Taxonomy | Status | Basis |
| --- | --- | --- |
| Functional scope / behavior | Clear | Full ordinary web/mobile parity; explicit web-only and watch capability dispositions |
| Domain / data | Clear | Existing account settings; no new durable entities or schema |
| Interaction / UX | Clear | Four stories, navigation/save/cancel/failure scenarios, web reference |
| Quality / reliability / security | Clear | Reachability, accurate failures, no duplicate effects, existing identity/authorization |
| Integrations / dependencies | Clear | Existing provider, machine and product boundaries retained |
| Edge cases | Clear | Interrupted/delayed operations, defaults, stale sessions, cancellation |
| Constraints / tradeoffs | Clear | Server-owned definitions and component ownership; no auth shortcuts |
| Terminology | Clear | Console reference, parity record, account settings, operation state |
| Completion | Clear | Seven measurable outcomes and exhaustive evidence inventory |
| Placeholders | Clear | No unresolved markers |

Optional before/after clarification hooks: `$speckit-git-commit`, auto-commit outstanding/clarification changes. They are not required for the gate and are deferred to scoped local checkpoints. Proceed to the plan; no owner decision is missing.

## Owner steering during implementation

The owner subsequently authorizes completing qualification, paired merges, Sandbox deployment and App Store Connect upload. The owner confirms released version 1.7 / build 66 and fixes all newly uploaded Apple products to version 1.8 / build 67. No separate pre-upload build-number inventory is requested, and no App Review submission is authorized. These numbers and upload authorization persist across turns; no additional login or collision-check question is required. Protected qualification and release policy remain mandatory.

On 2026-10-05 the owner explicitly authorized automatic entry of the attached provider/TypeSafe credentials into the app. Secret values remain private and are not fixtures, source, logs or handoff content. The owner then requested: "even if the connection test on save fails ... creds should still be saved ... after creds are saved ... run a connection test and alert the user if it fails." FR-009 and US1 now require validated encrypted persistence first, a separate post-save connection check, retained credentials on external failure, and truthful storage/validation failure. Existing IAM, acknowledgment, endpoint-change and owner boundaries remain. This is an explicit behavior decision; no further clarification question is needed.
