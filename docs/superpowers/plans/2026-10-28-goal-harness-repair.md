# Goal harness repair implementation plan

> **For agentic workers:** Use bounded cheaper implementation agents and independent verification, with root integration and critical-seam review.

**Goal:** Carry requests through real effects and verification instead of ending at generated instructions.

**Architecture:** Retain request-scoped progress, resolve the current authenticated project's folder context, and verify proposed final replies against tool evidence. Continue through the existing dispatcher; fail closed on missing verification, uncertainty, cancellation, or exhausted budgets.

**Tech Stack:** Existing Python 3.11 backend, AstralPlane facade, vanilla server-owned UI, existing Mac host and provider routing.

**Spec:** `docs/superpowers/specs/2026-10-28-goal-harness-repair-design.md`; existing `specs/099-macos-agent-workbench/spec.md` and `specs/102-macos-harness-parity/spec.md` are intent references, not mutation targets.

## Global constraints

- Keep primary checkouts on main; work in `codex/goal-harness-repair`.
- Preserve normal authentication, owner/project boundaries, approval, cancellation, audit, and egress.
- Changed executable Python lines retain at least 90% coverage; required CI remains deterministic and bounded.
- Never report an unrun test, unobserved effect, pending job, exhausted loop, or uncertain operation as completed.
- Use the protected release path after qualified merge; retain monotonic build numbering above the installed build 80.

## Review focus

- Informational questions must not require artificial tools.
- Missing, stale, or foreign project grants must not become server filesystem access.
- Background commands and uncertain writes must not be blindly repeated or called complete.
- A verifier response containing instructions, invalid JSON, or ungrounded completion must not gain authority.
- Stop or lease loss during verification must prevent another effect and successful publication.

## Tasks

- [x] Pure progress controller: failing tests, minimal implementation, real command-result fixtures, strict evaluator parser, bounded recovery, measured coverage.
- [x] Read-only project context: failing tests, validated owner/socket/project resolution, no grant/session creation, measured coverage.
- [x] Integrate evaluator and progress into ordinary chat behind the rollout switch; preserve original objective across compaction; cover actual chat-loop continuation, rejection, cancellation, and incomplete termination with failing regressions first.
- [ ] Qualify local source and exact candidate image, independently review, deploy only the candidate to sandbox with backup/rollback, run the Minecraft task via the installed Mac until local environment/tests/startup are actually verified, and exercise failure/Stop boundaries.
- [ ] Merge only qualified changes, release through the normal protected Mac lane, verify public feed/build and auto-update, and update curated vault checkpoints.

## Initial evidence

`test_turn_hooks.py`: 14 passed. The existing real-loop seam suite could not initialize its PostgreSQL pool using ambient configuration; an isolated task-owned PostgreSQL container is being prepared. No baseline failure is hidden or weakened.

## Qualification checkpoint

The controller, project context and ordinary-loop integration have regression coverage for missing tool outcomes, failed commands, pending jobs, truncation, compaction, cancellation, context limits and atomic incomplete publication. Projection PR 103 is merged as `df2750da3a450b84eeacd630da3ee85d81790358` after all required hosted checks pass; this candidate adopts that component. Baseline test fixtures are repaired against already qualified component pins, selected-harness admission reuses its current Plane transaction, and voice worker coverage is restored through behavioral tests without lowering gates. Complete backend candidate checks, actual deployed-catalog rehearsal, native Minecraft acceptance and release remain pending.

The final integration additionally binds background and delegated model identity to the admitted owner and execution fence. Parent cancellation and lease loss close child sockets and remove model identity; actual async/subtask regression suites cover the ordinary path and denials. Root integration passes 470 tests without skips, with a further 95-test async run after new denial cases; changed-source coverage is 96%, including all changed async/delegation lines. Hosted initial candidate CI revealed stale ingress fixtures, schema/dependency expectations and browser report-part consumption; those repairs retain the original policy, coverage and timeout gates. Projection PR 104 qualifies an action-order correction and a UIKit scene-lifecycle fixture repair; adoption remains pending required checks. Actual backup restoration and the guarded 089.013 to 089.014 migration have reached ready in an isolated copy, while complete preservation/repeat/joint qualification and native desktop acceptance remain in progress.

Plane PR 109 is normally merged as `b49834b3f3a7647819e0fd7961dfa420d7573d88` after all five required checks pass at exact head `6a433089`: 5,537 Linux PostgreSQL passes, nine existing platform skips, 33 subtests, 92.59% branch coverage and 100% changed-line coverage. Independent review's owner-prefix finding is repaired; root reruns 97 restore cases and verifies a private copy of all 401 actual-backup files across four roots, including six persistent replay fences, against the unchanged inventory digest. The consumer adopts that exact component without a schema or compatibility-tuple change. Full final-image joint rehearsal, native goal acceptance and release remain pending.

Projection PR 104 is normally merged as `a908ff1abdb3f7b2ef43815cdf694281a15fdc46` after all required checks pass at exact head `6c40544f`, including 481 iOS unit tests, every iOS UI shard, macOS, watchOS, Android, Windows, Core and formatter gates. This candidate adopts that exact pin and its canonical protocol digest together. The final component changes sort an existing action and repair a test precondition to accept the observed visible foreground-inactive scene; shipping scene configuration and identity contracts remain unchanged. Full consumer CI at the preceding pin had exactly one action-order failure and every other owner gate passing. Its exact image completed actual-backup database/four-root restoration, normal migration, repeat completion and row/sequence/policy preservation; the image rebuilt from this final pin still requires its own artifact-bound qualification. Native Minecraft acceptance and auto-update remain blocked by unavailable desktop controls.

Independent final review identifies and repairs an advisory-only completion seam before staging. A strict first-request classification now freezes a completion evidence contract; actual scoped computer-tool references are required for each local runnable-project category. The evaluator cannot satisfy the contract with canvas/search output, foreign identity, missing references, file-only test claims or an unobserved background job. Failure resolution is constrained to same invocation/identity, same job, or a digest-matching inspection of an unknown write; reading an existing file cannot prove deletion. Root observes the false-completion regressions fail before wiring the guard, then passes 430 real-PostgreSQL integration cases with 91% changed runtime coverage. Independent source review finds no blocking authorization, cancellation, currentness or MCP wrapper mismatch. Exact-candidate full CI/image/staging and physical native acceptance remain required.

Native access resumes on 2026-10-29 and the exact prompt is submitted in a fresh Minecraft project chat. The harness starts the Mac session and lists the granted folder, then the configured provider exhausts three 60-second response attempts; the project folder remains empty. Its ordinary app connection probe succeeds in 396 ms. This exposes an early no-response return that discards the staged request, and goal-mode narrative suppression also disables provider transport streaming. The repair routes no-response through the existing failed atomic publisher and retains allowed provider streaming while buffering all unverified narrative. Buffered transport requires a recognized terminal finish reason before returning any assembled tool calls; missing terminal evidence discards partial content and uses the existing safe fallback, while truncation remains visible to the outer gate. Classifier/verifier calls retain their separate nonstreaming behavior, and existing streaming kill switches, evidence, persistent and selected-harness restrictions remain authoritative. Root integration and exact-image native requalification are required before merge or release.

A targeted regression confirms that cancelling while the planner is awaiting, or during its pre-dispatch status send, previously allowed a late tool call to start. Goal-only cancellation checks at both boundaries now return to the existing cancelled publisher before dispatch. The early cancelled path records completed provider usage exactly once, while the existing recorder cancellation remains authoritative. Exact-head integration, CI and image qualification are repeated for this final change.

The next live attempt starts the Mac session and writes partial project files, but spends many calls on tiny edits and diagnostic probes without reaching setup or verification; its native terminal outcome remains unclear. This motivates a separate bounded progress checkpoint after eight outcomes. Checkpoints only advise continuation or a genuine block and retain the final proof gate. Regressions additionally isolate repeated provider IDs from internal evidence identity and permit an explicitly verified same-effect retry after a known failed write. Full admitted-connection tests cover incomplete publication with the real task state machine both enabled and disabled, without reproducing the native omission. Exact native requalification remains required.

An authenticated admitted-turn regression with a real macOS rendering profile reproduces the missing incomplete reply: storage contains the assistant warning but the rail reducer drops standalone Alert components, leaving a user-only emitted snapshot. Preserving standalone and nested Alerts and Lists fixes the projection without changing delivery, operation, owner or execution fences. Native snapshot assertions cover normal completion, failed provider output and budget exhaustion with tool UI; wrapper tests retain canvas deduplication.

## Causal diagnosis and current verification gates (2026-10-29)

The latest native trace identifies the immediate execution failure: remote-compute received a `make_directory` request for the selected Mac project path and stopped at its own approval boundary; the next local computer-use write failed because that parent directory did not exist. This is a host-routing and dependent-call ordering failure. The prior provider no-response path and rail publication loss are separate repaired failures; neither is evidence that a project was completed.

The current implementation rejects a whole proposed local-project batch when remote-compute targets a granted local root, routes only through registered computer-use tools for the exact selected host, and rejects explicit mismatched host arguments. Goal calls are serial and stop before dispatch when cancelled, and after a result unless the shared GoalProgress classifier marks it completed success. Thus a missing result, failure, uncertain effect, running background command, or pending approval cannot be followed by a dependent write or treated as proof. The actual result remains available to progress recording and recovery.

The verifier receives the original objective, frozen contract, and bounded observed evidence. It can retry the same tool-free review once after an unavailable or invalid response. A planner `None` response gets one repair turn because no tools from it were dispatched; it continues the same objective and cannot replay already completed effects. Approval remains a pending interlock, not proof: after owner response, continue the unchanged original contract through normal dispatch and require the effect’s observed successful result plus its category-specific verification. Cancellation, owner/project/host authority, audit and egress gates, selected-harness constraints, and privacy shaping are unchanged.

The privacy regression repaired by `4543763a1c63e636a2dccd3a0131e37e8b6672c4` involved malformed reserved usage/reference groups carrying a private warning. The rail now rejects the entire malformed reserved group rather than lifting its attached Alert through ordinary fallback, while still preserving valid canonical groups and ordinary standalone Alerts and Lists. The exact regression and its provenance are recorded in the design artifact.

The design addendum compares Codex, Claude Code, Hermes, Pi, and OpenClaw against exact pinned source revisions or official documentation. Claude Code’s implementation is closed source, so its public loop and Stop-hook docs are treated as behavioral guidance only. No code was copied. Primary links and the documented comparison are in the design artifact.

**Verification remains in progress.** Root’s current focused cohort is running. No new CI result, candidate-image qualification or deployment, native Minecraft acceptance, merge, or release is claimed by this checkpoint. Native acceptance remains open until the exact prompt produces files in the selected folder, an isolated environment and declared dependencies where needed, meaningful test results, and a startup check with observed success. Re-read the actual final test/CI and candidate-bound deployment receipts before advancing any gate.
