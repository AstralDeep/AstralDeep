# Project deletion and sidebar implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task by task. One implementer owns product edits; targeted independent review checks deletion and authorization.

**Goal:** Remove the global Mac sidebar New Chat button, explain an empty project list, and confirm deletion of a project and all its chats while retaining audit logs and local folders.

**Architecture:** Deep composes existing public Plane repositories and the canonical chat-unavailable lifecycle in one caller-owned transaction. It locks the current owned project, deletes every paginated chat, and tombstones the empty project with its current revision. Native confirmation dispatches the existing authenticated endpoint and reconciles the deleted project and active conversation.

**Tech stack:** Python 3.11, existing PostgreSQL/Plane repositories, Swift/SwiftUI and the existing signed Mac deployment pipeline. No schema or Plane API change is planned.

**Spec:** The owner's 2026-10-28 sidebar/deletion request and follow-up authorization to push, attempt GitHub merges, and update the sandbox server.

## Global constraints

- Scope is the Mac client and its required backend behavior; other clients retain their existing entry points.
- Deletion is owner scoped, atomic, serialized with project chat creation, and keeps project identity tombstones.
- Audit rows/chains, other projects/owners, settings and local folders remain untouched.
- Stale sidebar revisions and non-empty projects must not prevent confirmed deletion; real auth/storage failures remain truthful.
- Confirmation names the project and states that its chats will also be deleted; Cancel sends no deletion request.
- Preserve canonical voice unavailability, project-grant retirement, chat-deleted notifications and history broadcasts.
- Never restore consent, weaken CI, or treat a failed check as passing. Push/merge attempts are authorized; releases are not.

## Review focus

- Concurrent creation or rename serializes against deletion without leaving orphan chats.
- A later chat/version failure rolls back earlier chat/voice/project changes.
- Multiple pages, empty/archived chats and a stale revision are deleted completely.
- Another owner, another project, local folders and exact audit records survive.
- Cancel, late responses, connection/account changes and deleting an active chat do not navigate or clear another chat.

### Task 1: Atomic owner-scoped deletion

- [ ] Add failing real-Plane tests for non-empty/stale-revision deletion, pagination, owner/audit preservation, rollback and create/delete serialization.
- [ ] Forward an optional existing transaction through HistoryManager.delete_chat and VoiceSessionRepository.mark_chat_unavailable, including artifact-version cleanup.
- [ ] Compose the current-project lock, canonical paginated chat deletion and current-revision project tombstone in ProjectService.delete.
- [ ] Reuse postcommit voice/chat/timeline notifications and project-authority revocation in the REST endpoint.
- [ ] Run owning project/voice/REST tests, lint and changed-line coverage; independently review the resulting diff.

### Task 2: Mac sidebar and confirmation

- [ ] Add failing behavior checks for empty-project presentation, Cancel/confirmed deletion, and active/other-chat preservation.
- [ ] Remove the global sidebar button in project presentation, add a compact empty-project placeholder, and replace Delete empty project with a destructive confirmation.
- [ ] Reconcile successful or already-absent deletion without masking authentication or storage failures; retain account/connection/navigation fences.
- [ ] Run affected Core/native tests, formatting and provenance checks, then build the signed monotonic Mac update.

### Task 3: Integrate, deploy and publish source

- [ ] Incorporate current main without discarding independent changes; qualify exact dependency pins and composition.
- [ ] Build the exact source image; back up and rehearse the compatible server replacement without touching history-reset receipts or consent.
- [ ] Install the matching signed Mac update and deploy the server; verify health/auth/pins/config preservation and live behavior with a disposable project.
- [ ] Push required component/product branches, create and attach PRs, inspect exact-head checks and attempt merges in dependency order without bypassing protections.
- [ ] Update the curated wiki and precise handoff with actual live, test, deployment and GitHub outcomes, including any remaining blockers.
