# macOS Projects and Autonomous Computer Use Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver synced projects with per-Mac inherited folder grants and autonomous, approval-free bypass behavior, repair the handoff failures, deploy the sandbox candidate and reset only the owner's prior chats.

**Architecture:** Plane owns normalized project persistence and guarded schema evolution. Deep derives project authority from authenticated chat membership, binds it to host execution and chooses standing consent or a direct refusal. Projection declares the additive contract and renders/executes the macOS flow using project-scoped bookmarks.

**Tech Stack:** Python 3.11, PostgreSQL, FastAPI, Swift/SwiftUI and existing signed macOS distribution tools; no planned new runtime dependency.

**Spec:** `docs/superpowers/specs/2026-10-27-macos-projects-autonomy-design.md` (owner approved).

## Global Constraints

- macOS client implementation only; compatible additive contracts and declared dispositions for other clients.
- “When its existing Bypass permissions switch is on, Astral must never show an action approval card, approval notification, or approval-waiting state.”
- “Projects and chat membership sync through the server across the owner's Macs; folder paths and native access grants stay specific to each Mac.”
- “New chats belong to a project and inherit its read, write, and execute grant without another permission question.”
- Existing normal-user command execution validates cwd; do not claim OS filesystem confinement.
- Keep normal dispatch, identity, owner isolation, attenuated delegation, policy/taint/PHI/egress, audit and effect-time fences.
- “Backend changed Python coverage remains at least 90%.” Local focused tests throughout; CI troubleshooting and broad final gates last.
- No product pushes, merges, public releases, issue changes or store submission. Local commits and the explicitly authorized sandbox deployment/history reset are in scope.

## Review Focus

- Every new-chat entry point, including initial send/resume/voice, must inherit a verified project or require project selection; Task 2/4 tests.
- Removing a folder while a command is queued/running must invalidate its old grant and interrupt affected jobs; Task 3/4 tests.
- A lost create response must not duplicate projects/chats; Task 1/2 idempotency tests.
- A host error can follow a partial effect; Task 3 tests must preserve uncertainty unless a trusted pre-effect witness proves no effect.
- A paginated recent list can omit empty/archived chats; Task 1/6 tests must enumerate all owner pre-cutoff chats before deletion.

### Task 1: Durable owner-isolated projects

**Files:** Plane `src/astralplane/repositories/projects.py` (new), `api.py`, `__init__.py`, `database/migrations.py`, `database/revision.py`, compatibility/package metadata and provenance; `tests/test_projects.py` (new), migration tests and `docs/migration-and-recovery.md`.

**Interfaces:** Produce `create_project_repository() -> ProjectRepository`; immutable `ProjectRecord(project_id, owner_id, name, position, revision, created_at, updated_at)`; owner-scoped `create`, `get`, `list`, revision-fenced `rename`/`delete`, `attach_chat`/`project_for_chat`, `list_chats` and `list_owner_chats_before`. All accept caller transaction/query and keyword-only owner identity; create uses project ID as idempotency identity. Use a separate membership table so legacy conversation records are unchanged; FK cleanup follows chat deletion. `attach_chat` validates owner/chat/project in the same transaction. List methods use stable ID cursors and include empty chats.

- [ ] Write real-PostgreSQL tests for create/replay conflict, foreign owner, stale revision, atomic create/attach rollback, nonempty deletion refusal, paginated empty/archived chats and FK cleanup. Observe failure against missing project facade.
- [ ] Implement repository and facade; add the next collision-free registry edge from current `089.012` with normalized project/membership tables, exact catalog verification and preserved historical digests. Bump schema/package metadata, regenerate only current provenance/evidence.
- [ ] Test representative populated upgrade, repeat/no-op, malformed catalog and recovery. Run `PYTHONPATH=src <deep-venv-python> -m pytest tests/test_projects.py <owning migration cases> -q` against task-owned PostgreSQL; require no skipped database cases.
- [ ] Commit only task files in Plane and record SHA, schema revision and digest in ledger.

### Task 2: Server project policy and chat creation

**Files:** Deep `backend/orchestrator/projects.py` (new), `history.py`, `models.py`, `api.py`, `orchestrator.py`; new `backend/tests/test_chat_projects.py` and focused creation/history tests.

**Interfaces:** Consume Task 1 repository through the application Plane context. Produce `ProjectService` owner CRUD/list, `project_for_chat(owner_id, chat_id)`, atomic `create_chat(owner_id, project_id, chat_id)` and paginated project chat summaries. Add optional `project_id` to compatible REST/WS chat fields. New negotiated Mac workbench creation requires verified project; legacy client creation remains compatible.

- [ ] Write tests for owner isolation, REST/WS idempotent project/chat creation, foreign/stale project, atomic rollback, empty chat listing and every new-chat entry point. Observe the new behavior failing.
- [ ] Implement host service/routes and small dispatch integration points; derive project on load/dispatch from storage, not client assertions; retain publication/voice cancellation semantics.
- [ ] Run focused project/API/new-chat tests, then relevant history tests. Commit task files after inspecting diff.

### Task 3: Approval, path and typed-outcome repairs

**Files:** Deep `computer_use_policy.py`, `remote_confirmation.py`, `hitl_confirmation.py`, `write_review.py`, `computer_sessions.py`, computer tool/MCP modules; existing bypass/approval/diff/authority tests. Projection `ComputerWorkspace.swift` and owning tests for typed missing/pre-effect errors.

**Interfaces:** Produce shared project-aware file path resolution; centralized consent decision (`standing`, `refuse`, `review`) consumed by both review gates; typed host outcome metadata preserved through MCP. Existing exact-call approval contexts remain the only effect release path.

- [ ] Add failing regression cases for duplicate file review, unclassified read/list path escape, missing-file vs unavailable diff, stale decision UI/audit, safe server-pause capture/recovery vs human pause, zero-card bypass for session/terminal/browser/file verbs and denial without card.
- [ ] Implement each repair with its RED→GREEN cycle. Bind relative paths to primary root and recheck at effect; preserve exact terminal permits; retain uncertainty for partial effects, delivery loss/timeouts and untyped failures.
- [ ] Run existing bypass/task-continuation/source-bound dispatch/diff/host-authority suites; measure at least 90% changed Python coverage before task completion. Commit Deep and Projection changes separately.

### Task 4: Project-scoped Mac grants and projects-only sidebar

**Files:** Projection protocol/fixtures/provenance, AstralCore project/transport models and tests; `MacComputerFiles.swift`, `MacComputerHost.swift`, driver/commands, `AppModel.swift`, `MacWorkbenchView.swift`, project UI and tests. Deep host protocol, registration/session/grant binding and tests.

**Interfaces:** Consume server Task 2 project records. Produce negotiated project-workspace capability, bounded project root manifest and immutable project/grant-revision binding on sessions/effect requests. Native bookmark key includes server/owner/project/device. Origin project survives sidebar changes; grant revocation invalidates outstanding authority and commands. All Mac new-chat actions consume selected project. Activity decision states follow server acknowledgment.

- [ ] Add failing protocol/Swift/Mac cases for synced catalog, no local grant on another device, project/account/server isolation, legacy bookmark not reused automatically, folder replacement/revocation and selection-required creation.
- [ ] Implement additive manifest/dispositions and shared server-owned project data; render native sidebar/create/rename/folder controls, deterministic primary root and direct New chat actions.
- [ ] Implement project-bound host preflight/effect verification and project standing grants for file/command verbs, including bypass-off inheritance. Stop/revoke affected jobs and retain user takeover.
- [ ] Run AstralCore tests, targeted unsigned Mac app tests/build, protocol/provenance/drift and affected Deep host tests. Commit each repository separately.

### Task 5: Exact candidate composition and sandbox/macOS rollout

**Files:** Deep `config/astral-composition.json`, component gitlinks, local evidence/handoff records and task-owned operator scripts only if needed.

**Interfaces:** Consume exact tested Plane/Projection SHAs and schema/protocol digests. Produce immutable source-bound candidate image, same-identity signed Mac build, private backups and rollback receipt, and live acceptance evidence.

- [ ] Verify exact composition and local package import; initialize matching component trees and refresh pins/digests without editing component source from Deep.
- [ ] Inspect live source/config/image/data and capacity, take private recoverable backup, build candidate image from exact local commits without product push, recreate only Astral and verify health/readiness/auth denial plus representative normal migration.
- [ ] Build/sign/install Mac candidate retaining the old app and owner preferences; use normal existing sign-in/consent. Exercise two-folder project inheritance, poem write/read/edit, command, disposable delete, bypass browser/app task, error recovery, Stop/takeover and revocation against live sandbox. Record second-Mac verification if available and distinguish unavailable from passed.
- [ ] Commit local composition and handoff; update curated vault before declaring deployment checkpoint.

### Task 6: Owner history reset and final verification

**Interfaces:** Consume Task 1 complete owner pre-cutoff enumeration and existing `HistoryManager.delete_chat` lifecycle; produce fixed-cutoff, owner-bound reset receipt and final exact test/live results.

- [ ] Test inventory beyond default pagination, empty/pinned/archived chats, partial delete/retry, post-cutoff preservation and synthetic other-owner preservation before the actual reset.
- [ ] After Task 5 live acceptance, inventory the authenticated owner's old chats, retain private backup/cutoff, quiesce their admission as needed and delete through existing owner lifecycle. Verify zero remaining pre-cutoff chats, retained account/audit state and working new project chat.
- [ ] Run broad relevant backend/module/Plane/Projection checks and changed-line coverage; handle CI last without weakening gates. Record actual failures/baselines, not inferred passes.
- [ ] Obtain a fresh whole-branch review, address important findings with regression tests, inspect final diffs/secret safety and finish local commits. Update vault and provide exact deployed/installed/tested/unverified state. No product push/merge/release without a later request.

## Execution record

The owner already approved implementation. Execute continuously in this chat without another permission round for the plan. Use the skill ledger to record task bases, RED→GREEN results, interface rulings, exact commits and remaining checks. Only new scope or genuinely missing external authority requires user input.
