# macOS projects and autonomous computer use

Date: 2026-10-27
State: builder review passed; design awaiting owner review. No product implementation or deployment yet.

## Intent and owner decisions

The Mac client must complete computer tasks without repeated permission requests. When its existing Bypass permissions switch is on, Astral must never show an action approval card, approval notification, or approval-waiting state. Allowed actions execute through the normal authorized dispatcher; denied actions return a concrete failure to the agent and user. A failed auto-authorization must not fall through into a manual approval flow.

Replace the Mac's flat chat list and host-wide workspace selection with projects containing chats and multiple local folders. New chats belong to a project and inherit its read, write, and execute grant without another permission question. Projects and chat membership sync through the server across the owner's Macs; folder paths and native access grants stay specific to each Mac.

The owner explicitly chose to remove existing chat history and start with projects only. Apply that reset to the owner's existing sandbox account, not every account. The owner authorized direct sandbox fixes over the VPN, macOS client work only, and CI work at the end. The request authorizes local implementation, signed local client testing, sandbox deployment, and that account's history reset. Product pushes, merges, public releases, store submissions, and other issue-tracker changes are not included in this request.

## Verified starting point

- Deep is clean at `ca64c066`, refreshed against `origin/main`; its tracked local pointer still names already-merged `102-macos-harness-parity`. This design does not reopen or modify that feature's artifacts.
- Deep pins Projection `411b09b2` and Plane `86b50d59`. The sibling Projection checkout is clean main at `e8aed25`; implementation must choose one current base deliberately and keep component work in its owning repository.
- Live read-only inspection confirmed sandbox Deep `50962239458a60199b8b844880e070b750b7d5e2`, image `sha256:18d1e10f603fcd223364897bea8565b0a4534b661d3b47b401491d31288bacbf`, healthy database/LETS, 15 agents and zero container restarts. The host source checkout is clean and detached. Root filesystem usage is 94%, with 28 GiB available; allow for image/backup capacity before rollout. The handoff's installed Mac 2.0.2 (73) remains to be independently verified.
- VPN networking, SSH and sudo access are established for `sam@sandbox.ai.uky.edu`. After the initial existing-key rejection, the owner authorized a dedicated key and entering SSH/sudo passwords directly in Terminal. The dedicated key was created and successfully installed through that Terminal flow. No password entered chat or logs, and no application/server configuration was changed.
- There is no durable project catalog or chat membership field. Plane's chat organization preferences contain pinned/archived IDs, and optional personalization `project_id` is only a memory namespace.
- `MacComputerFiles` already retains multiple owner/server-scoped native bookmarks and physical directory identities. The host advertises one flat `workspace_roots` array, currently capped at 16.

## Product behavior

The Mac sidebar lists projects. Each project expands to its chats and has a direct New chat action. The top-level New chat command creates a chat in the selected project; with no project selected it opens the project chooser/create flow. There is no ungrouped Chats section in the new Mac experience.

Creating a project accepts a name and lets the user choose one or more folders through the native directory picker. The first chosen folder is the primary working folder. Project settings allow rename, add/remove folders, and choosing another primary folder. New chats immediately inherit those grants; chat creation does not present a permissions sheet. On another Mac, the same project/chats appear, but local file tools remain unavailable until folders are associated on that Mac. Text chat remains usable without local folders.

Ordinary project file reads, writes, deletes, and command launches inside the granted scope use the standing project grant even with bypass off. Bypass additionally supplies standing consent for otherwise supported computer, browser, app-input, and consequential actions on that Mac. It covers session start, end, and valid server-recovery resume without per-action prompts. Normal review remains for operations outside standing project grants when bypass is off, subject to existing hard denials.

No approval is fabricated in the UI. An explicit decision in review mode enters a pending/submitted state until the server confirms it. A refused or stale decision leaves a visible failed/expired state. Auto-authorized operations appear as normal activity with their actual result; they do not create a hidden card, approved chip, badge count, or toast.

Bypass does not turn an invalid folder, absent host, stale session, lost audit sink, locked desktop, human Stop/Pause, or missing macOS permission into success. Such cases produce a direct, actionable tool failure and no approval prompt. The agent receives enough structured context to choose a valid project path or another supported method instead of repeating the failed request.

## Authority model

Keep Keycloak identity, owner isolation, attenuated delegation, tool permissions, policy hard denials, PHI/taint/egress gates, audit provenance, cancellation, and native TCC in the normal dispatch path. Bypass changes consent policy, not transport trust or OS privilege.

Replace the current boolean bypass predicate/fallback with one shared decision used by both remote confirmation and HITL: standing consent, direct refusal, or ordinary review when bypass is off. Check the live host's bypass state again at dispatch/effect time. Turning bypass off while a task waits invalidates its unconsumed auto-authority.

Terminal launch/input can use standing bypass consent only with the existing exact, single-use, session/window/application-bound terminal permit. Prefer `run_command` for normal programs. Never obtain terminal authority merely by deleting a floor check. Password entry/sign-in automation, OS privilege escalation, and manipulation of Astral's own permission/account controls remain direct denials without an Astral approval card. User-completed OS authentication is distinct from action approval and cannot be synthesized by the agent.

Human takeover and explicit local Pause/Stop remain effective. Do not auto-resume those states. Recover `approval_not_completed` only when it was generated by the server for the same owner, chat, project, task, host instance, connection, and still-current grant revision. Session expiry requires a new valid session rather than reviving expired authority. Ignore local-input events from Astral's own non-sensitive chat/project controls only after establishing their real target identity; do not ignore genuine input to another application.

Project access is server-authorized and host-enforced. Derive a request's project from the stored owner/chat membership, never from a model argument or whichever sidebar project is selected now. Bind execution tickets, sessions, command jobs, and reviewed argument fingerprints to that project and the current local grant revision. The host verifies that binding against its local bookmark snapshot before every effect. Switching projects cannot lend one project's roots to another chat. Removing a folder or deleting a project invalidates affected tickets and interrupts affected jobs before another effect can be admitted.

File operations retain descriptor-relative traversal, symlink/reparse protection, root identity checks, protected-directory restrictions, size bounds, and effect-time authorization. Commands retain sanitized environments, bounded output/time, job ownership and their current user-level execution model. The existing command host validates its working directory but is not an OS filesystem sandbox; this feature must not claim that a working-directory check confines arbitrary subprocess access. Adding an OS process sandbox is outside this design.

## Durable project model in AstralPlane

Use normalized PostgreSQL persistence owned by Plane, rather than hiding a potentially large chat membership map in preferences JSON. Add owner-scoped projects and conversation membership, with a nullable membership for compatibility with existing clients. Enforce owner/project/chat consistency in database predicates and constraints. Project records carry stable ID, owner, display name, ordering, optimistic revision, and timestamps. Each conversation belongs to at most one project.

Plane exposes neutral typed records/repositories for project CRUD, paginated project/chat enumeration including empty chats, atomic chat creation with membership, and revision-fenced membership changes. The host supplies owner identity and transaction boundaries. No SQL or driver access is introduced in Deep. File paths, native bookmarks, and plaintext credentials do not belong in this catalog.

Creation uses idempotency keys so a lost response cannot create duplicate projects or chats. Renames and concurrent membership changes use optimistic revisions. Deleting a nonempty project is refused with a useful result until its chats have been explicitly deleted or moved; this avoids silently deleting conversations. The initial reset below is a separate owner-authorized maintenance operation, not an implicit schema-migration effect.

Ship the guarded migration edge, schema revision/digest, current-catalog verification, representative populated-data upgrade tests, repeat/no-op tests, and recovery instructions in Plane. Preserve legacy conversations during migration. Run normal startup migration against the sandbox's backed-up representative data. An additive schema alone does not establish old-binary compatibility; use the declared Plane compatibility contract and a tested recovery plan.

## Deep integration and protocol

Deep owns project policy, authenticated owner APIs, project/chat creation and navigation, discovery, and tool authorization. Add bounded, paginated project APIs and additive `project_id` fields to chat creation/list/detail and supported navigation frames. All operations check owner identity server-side. Do not repurpose `FF_PROJECT_MEMORY` or turn it on as a side effect.

Projection owns the shared project presentation contract and the Mac rendering. Declare an additive project-workspace capability and its macOS disposition in `ui_protocol.json`. The server selects the projects-only Mac experience only for clients negotiating that capability; old/native/web consumers retain compatible payloads and existing behavior. No Windows/Android/iOS/watch UI implementation is included in this scope. Record that explicit owner-requested macOS scope in the feature artifact and contract; do not silently hide new shared chrome in clients. Other client drift guards still validate the additive manifest and their declared disposition.

The Mac persists folder grants keyed by owner, canonical server, project ID, and local device identity. A legacy host-wide bookmark is not silently granted to every project. Preserve it for recovery, then associate a verified existing bookmark only when the user explicitly selects it for a project. Security-scoped bookmark bytes never leave the Mac.

Advertise a bounded per-project workspace manifest over the authenticated host channel, with the project ID, ordered roots/primary root, and grant revision. The server validates that every advertised project belongs to the socket owner. Only ephemeral host metadata contains these paths. Session/effect requests carry server-derived project/grant binding; a legacy request must not inherit the union of project roots. The precise additive wire field names are an implementation choice, but the binding and revocation behavior are required. Reject oversized announcements before granting partial authority.

At session start and tool discovery, return the active project's roots, primary root and permitted operations to the model. Resolve relative file paths against the primary folder; commands with omitted `cwd` use it. Normalize path segments and reject escapes, malformed paths, foreign host syntax and paths outside this project's roots before creating any review/proposal. Return the permitted roots in a structured refusal. Repeat checks at host dispatch and effect time to handle revocation races. Never infer a home folder from a nickname or persona.

## Repair the handoff failures

1. Add shared file-path preflight for `read_file`, `list_dir`, `write_file` and `delete_path` before the remote gate's early return for unclassified verbs. Apply it to standing-grant, bypass and reviewed calls, and update tool descriptions/results with real project roots.
2. Extend the consumed remote-review bridge beyond `run_command` to exact reviewed file mutations. Keep owner/chat/host/project/task/argument binding and replay protection. With bypass off and no standing grant, only one complete review can be created.
3. Capture recoverable server-paused authority explicitly, or refuse before minting any proposal. Widening a later current-authority predicate alone does not fix capture. Audit refused decisions and visibly retire stale cards. Human pause and changed authority are not recoverable through this path.
4. Return typed `not_found` from Mac file reads for genuine ENOENT, distinct from permission, stale authority, encoding and transport failures. Model the write pre-read as present/missing/unavailable. Missing files show a `/dev/null` diff and a Create label even if the model requested overwrite; do not treat unreadable content as empty. At execution, preserve explicit overwrite semantics and reject a new-file race rather than unexpectedly overwriting.
5. Preserve typed host error metadata through the computer MCP server. Report no effect only when trusted phase information establishes a refusal before any effect. A generic `permission_denied` alone is insufficient because input may fail after partial typing. Timeouts, lost replies, and interrupted partial effects stay uncertain and must not trigger blind replay.
6. Make Mac activity/alerts follow server results. Test an Alert while review is displayed, a refused approval, duplicate clicks, and out-of-order status updates. In bypass mode verify that no approval UI is constructed at all.

Relevant current seams: Deep `computer_use_policy.py`, `remote_confirmation.py`, `hitl_confirmation.py`, `write_review.py`, `computer_sessions.py`, `computer_hosts.py`, computer agent tool/MCP modules, history and chat APIs; Projection `MacComputerFiles.swift`, `MacComputerHost.swift`, `MacComputerDriver.swift`, `MacComputerCommands.swift`, `ComputerWorkspace.swift`, `AppModel.swift`, `ChatListView.swift`, and protocol/provenance ledgers; Plane history/repository/migration/facade contracts.

## Existing-history reset

Perform one reset only after the new project path is functional on the sandbox and an owner-scoped inventory plus recoverable private backup exist. Bind the maintenance operation to the verified owner ID and a fixed cutoff, so chats created during rollout are not accidentally included. The normal chat list defaults to 20 nonempty chats and is not a complete purge inventory: enumerate every owner-owned pre-cutoff conversation, including archived, pinned and empty conversations, through Plane.

Use the existing owner-scoped deletion lifecycle, currently reached by `DELETE /api/chats/{id}` and `HistoryManager.delete_chat`. It already fences voice/publication work and artifact cleanup. Preserve append-only audit records and accounts/provider settings; never use raw SQL deletes. Chat-owned artifacts follow the product's deletion semantics, while project files on the Mac remain untouched. Record partial failure and make the bounded inventory retryable. Verify the same owner has zero pre-cutoff chats and that a synthetic other owner's data is unchanged. Do not perform this purge automatically on all users during startup or schema migration.

## Delivery and verification

Implement in dependency order in the owning repositories: Plane persistence and recovery, Projection contracts/Mac folder binding and UI, then Deep orchestration and exact composition pins. The original failure repair may be locally qualified alongside this work, but a folder grant must never become broader temporarily to make a demo pass.

Run narrow behavioral tests during implementation. Leave hosted CI troubleshooting and broad final gates until the end, as requested; do not change or weaken CI to conceal a failure. Backend changed Python coverage remains at least 90%. Before declaring the final result, report exact checks and any baseline failures separately. No public release or main-branch mutation is implied.

Required automated coverage includes:

- Every supported bypass verb produces zero proposal rows/cards/approval notices and the expected effect/audit. Retained hard denials also produce zero cards. Turning bypass off restores review where no project standing grant exists.
- New chats inherit the project's read/write/execute grant without a prompt; a project/owner/host mismatch, removed root, stale bookmark, changed root identity and forged/stale grant revision are denied before effect.
- Relative and absolute paths, multiple roots/primary selection, traversal, symlinks, missing/no roots, Unicode paths, and revocation between preflight and effect.
- Exactly one review in normal mode; changed or replayed reviewed arguments cannot execute; server-failure recovery works while human Pause/Stop and lost authority do not resume.
- Missing-file Create diff, existing-file diff, unreadable/truncated/encoding errors, concurrent creation, and pre-effect versus partial/uncertain failure classification.
- Project CRUD/membership idempotency, concurrency and ownership; atomic chat creation; pagination including empty chats; representative migration/recovery; the owner-only pre-cutoff reset and deletion failure/retry.
- Mac project sidebar/new-chat actions, bookmark rebinding and grant revocation, server-confirmed activity state, accessible controls, and backward-compatible protocol dispositions.

Live acceptance uses the candidate Mac connected to the candidate sandbox through real configured IAM and the normal dispatch path. Create a project with two test folders, then several chats; ask for a poem saved as `hello.md` without supplying a path, read it back, edit it, run a command and delete a disposable file. Repeat with bypass on and off to demonstrate project standing grants. In bypass mode also exercise a browser/app task, valid server-error recovery and a definite outside-project refusal with no approval card. Verify Stop/local takeover, cancellation and folder revocation with a running job. Open another project and another signed-in Mac to demonstrate synced membership and separate local grants; if a second Mac is unavailable, say so rather than claiming that live check. Verify ordinary review still works for an eligible nongranted action with bypass off.

Build an exact source-bound backend image, preserve the existing sandbox configuration/data and companion containers, deploy only the application after backup, verify readiness/real denials, and retain a tested rollback path. Update/install the same-identity signed Mac candidate with a retained old-app backup. Collect the account reset results only after the projects flow passes. Finish with local and hosted checks that are actually available/authorized, recording implemented, test-passing, live-verified, deployed and released as distinct states. Update the curated knowledge vault at durable checkpoints.

## Research and implementation choices

Codex's public app-server protocol has a real project object, stable IDs, multiple workspace roots and separate thread/project/permission fields; its state package has normalized project persistence. This supports separating the catalog, membership, workspace grants and consent policy. It does not expose the proprietary desktop sidebar implementation. Reference snapshot retrieved 2026-10-27: `openai/codex@4d3ed3f3b47d39f8fe0b60df74a20f528b6fc618`:

- [Project protocol](https://github.com/openai/codex/blob/4d3ed3f3b47d39f8fe0b60df74a20f528b6fc618/codex-rs/app-server-protocol/src/protocol/v2/project.rs)
- [Thread protocol](https://github.com/openai/codex/blob/4d3ed3f3b47d39f8fe0b60df74a20f528b6fc618/codex-rs/app-server-protocol/src/protocol/v2/thread.rs)
- [Projects migration](https://github.com/openai/codex/blob/4d3ed3f3b47d39f8fe0b60df74a20f528b6fc618/codex-rs/state/migrations/0049_projects.sql)

The [Claude Code public repository](https://github.com/anthropics/claude-code/tree/08c6a4f5121ae140d3c1b5c96e3e5ae53587b54b) contains documentation, examples and issue infrastructure, not the private desktop/core permission implementation to copy. Its [official permissions documentation](https://code.claude.com/docs/en/permissions#working-directories), retrieved 2026-10-27, describes additional working-directory grants and separates access from configuration discovery. Adapt those observable behaviors; do not claim to have copied unavailable implementation.

Calls made by the designer, not additional owner decisions: normalized Plane persistence instead of bounded preferences JSON; one primary folder for deterministic relative paths; no unrequested shared-memory behavior; project deletion refuses nonempty projects; owner-only pre-cutoff history reset; project-context binding through every host effect; compatibility for unnegotiated clients; retain user-level command execution without claiming filesystem confinement. Exact class/field names, registry revision allocation, API pagination representation and test grouping remain choices for the implementer after checking the then-current tree.
