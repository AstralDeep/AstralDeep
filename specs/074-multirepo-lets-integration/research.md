# Research: Astral Multi-Repository Decomposition and LETS Integration

**Feature**: `074-multirepo-lets-integration`

**Date**: 2026-08-13
**Source baseline**: AstralDeep planning anchor `3862a5d1c4ab1969cb8589b023e7f7a7c9f19e68`; refreshed extraction source `fc113c4f99121b2053bb71523835c5c4743f1f56` after the ordinary merge of PR #175; LETS tag `v1.0.10` (`82dbe4f5ddf410cc86778784bb612440725ec66d`)

This document resolves the technical choices required by the feature specification. Repository inventories were read-only; each choice is revalidated against refreshed remote refs before it becomes a mutation.

## Decision 1: Compose independent repositories with exact Git submodules

**Decision**: AstralDeep remains the product-composition repository and pins four submodules under `components/`: `AstralProjection`, `AstralPlane`, `AstralPrimitives`, and `LETS`. A committed composition manifest records each canonical repository identity, exact commit, release/tag where applicable, contract version, and compatibility digest. Clean checkout, build, and startup checks reject missing, dirty, unexpected, or incompatible submodule state.

**Rationale**: A Gitlink is reviewable, reproducible, and preserves independent histories. The separate manifest adds semantics Gitlinks do not carry: release identity, contract compatibility, schema revision, UI-protocol digest, and qualification status.

**Alternatives considered**:

- Package registries alone: rejected because they do not pin client source, release workflows, or the LETS case-study source and tests.
- Git subtree or vendored copies: rejected because they create a second mutable source of truth and blur project independence.
- Floating branches: rejected because a branch tip is not an immutable system composition.

## Decision 2: Preserve default history with ordinary replacement commits, without archive refs

**Decision**: Refresh the live remote state, record the legacy default and observed branch tips for provenance, create `main` from the refreshed `master`, change the GitHub default branch, retain `master`, and publish replacement content as ordinary descendant commits through named feature branches and draft pull requests. Feature 074 creates no archive tags or archive branches. No orphan branch, force-push, or published-branch deletion is used. Extraction provenance records the AstralDeep source commit and path map.

**Rationale**: A normal content-replacement commit retains the refreshed default branch as an ancestor, which meets the owner's requested overwrite semantics without creating extra archive refs. Three Plane commits found only behind already-deleted remote branch names are not ancestors of the default branch and are not guaranteed to remain recoverable on GitHub; the owner explicitly accepted not archiving them, so their observed hashes are recorded only as a non-preservation risk.

**Alternatives considered**:

- Reinitialize each repository: rejected because it destroys normal default-branch history and would require destructive force-pushes.
- Force-reset the default branch: rejected because it rewrites published history and breaks auditability.
- Archive tags/branches: declined by the owner; default ancestors remain reachable through normal history, but already-deleted unmerged tips receive no new remote retention guarantee.
- Merge legacy Plane/Projection architectures into the extracted system: rejected because they are different products, not compatible implementations of the current boundaries.

## Decision 3: AstralProjection owns rendering and complete client products

**Decision**: AstralProjection owns the server rendering packages, ROTE adaptation, shared UI-protocol manifest, web assets, Windows client, Android client, Apple clients, their platform tests/tooling, and their build/release workflows. Deep-owned route, authorization, orchestration, and domain controllers call Projection through host-neutral interfaces; Projection must not import AstralDeep. The package initially preserves the public Python import names `webrender` and `rote` to reduce cutover risk, and adds an `astralprojection` contract/resource package.

**Rationale**: Native client trees combine rendering with authentication, transport, voice, signing, and platform build logic. Splitting individual visual files out of those build units would make each client span two repositories and increase drift. Moving whole products gives one accountable release owner while preserving server-owned UI definitions.

**Alternatives considered**:

- Move only components and images: rejected because native builds and release verification would still be coupled to AstralDeep.
- Keep server rendering in AstralDeep: rejected because it would split ownership of one UI contract between repositories.
- Convert the system to the legacy React/Vite Projection application: rejected because it is not the current Astral product architecture.

## Decision 4: Break Projection-to-Deep cycles with ports, not reverse dependencies

**Decision**: Host-specific surface controllers currently under `backend/webrender` move to a Deep-owned adapter/surface layer when they import orchestration or policy. Projection owns deterministic rendering, sanitization, accessibility, static assets, capability adaptation, and presentation data structures. Deep injects authenticated data and callbacks through typed ports. UI protocol and assets are loaded from installed Projection package resources rather than hard-coded monorepo paths.

**Rationale**: Current `webrender` modules import agent-authoring, remote-machine, and feature-flag behavior from Deep, while Deep imports renderers and ROTE. A repository boundary cannot retain that cycle. Ports keep dependency direction from Deep to Projection and preserve security gates in Deep.

**Alternatives considered**:

- Allow Projection to import the Deep submodule: rejected because it makes the component non-independent and creates a dependency cycle.
- Duplicate the controller modules: rejected because duplicate mutable code violates the ownership requirement.
- Move orchestration APIs into Projection: rejected because it would move policy and runtime control into the presentation project.

## Decision 5: AstralPlane is an embedded durable-state library first

**Decision**: AstralPlane becomes a Python 3.11 package installed into the AstralDeep process. It owns PostgreSQL pooling, transactions, schema metadata and migrations, repositories, persistent attachment/artifact/knowledge stores, durable claims/fences, and transactional outbox mechanics. It does not add a service port or second database. AstralDeep owns domain policy, authorization, orchestration, outbox handlers, A2A/MCP/WebSocket transport, in-memory streams, real-time voice/media, and remote execution.

**Rationale**: The existing data layer relies on same-transaction guarantees across conversations, workspaces, scheduling, effect claims, voice metadata, and personal-agent publication. An immediate network boundary would split those transactions and create distributed failure modes without product value. An embedded package establishes code ownership first while preserving one PostgreSQL truth.

**Alternatives considered**:

- A new AstralPlane FastAPI service: rejected for the initial split because it adds a network hop, distributed transactions, service authentication, and availability coupling.
- Move streaming, LiveKit, and SSH execution into Plane: rejected because they are transport/execution planes with direct policy and orchestration coupling, not durable-state ownership.
- Leave raw SQL in Deep: rejected as a permanent state because it defeats independent data-plane ownership; compatibility shims are allowed only during the phased cutover.

## Decision 6: Extract Plane in transactional clusters

**Decision**: First establish pool/transaction/migration contracts and a `shared.database` compatibility facade. Then move leaf repositories and blob stores. Move the transactionally coupled admission/history/workspace/scheduler/effect, voice-session, and personal-agent publication cluster together. Separate deterministic database migrations from product reconciliation and UI seed work. Finish with an import/static guard that forbids production AstralDeep modules from importing psycopg or embedding SQL.

**Rationale**: SQL currently appears in dozens of product files, and several storage modules import Deep domain types. Moving whole files without first extracting neutral records would simply recreate the dependency cycle. Clustered migration preserves atomicity and allows narrow regression checks after each slice.

**Alternatives considered**:

- Move `backend/shared/database.py` unchanged: rejected because it imports product policy and mixes DDL with filesystem hashing, agent retirement, policy reconciliation, and tutorial seeding.
- Migrate one method at a time without transaction mapping: rejected because it risks breaking fences and atomic publication.
- Change schemas and storage roots during repository extraction: rejected because simultaneous data movement makes rollback much harder.

## Decision 7: Add a PostgreSQL transactional outbox and detached command results

**Decision**: Plane exposes a detached `CommandResult` instead of returning a cursor after its pooled connection is released, standardizes SQL on native psycopg placeholders, and provides durable outbox enqueue/claim/lease/ack/retry/dead-letter operations. Deep owns authorized handlers. Audit retry and file-purge follow-up work migrate from lossy local handling to this outbox.

**Rationale**: The current cursor lifetime can race with pool reuse. Lexical placeholder replacement can alter literals or comments. The current audit JSONL retry path can lose or drop events, and attachment deletion can report success while a sensitive blob remains. These are boundary-critical correctness issues and the owner authorized improvements discovered during migration.

**Alternatives considered**:

- Preserve cursor return behavior indefinitely: rejected because its lifetime is not valid outside the connection scope.
- Keep a file retry queue: rejected because database transactions already define the authoritative commit point and can atomically enqueue follow-up work.
- Swallow blob deletion failures after soft-delete: rejected because purge incompleteness must remain visible and retryable.

## Decision 8: Preserve current data and blob locations during the first cutover

**Decision**: Existing PostgreSQL tables, schema revision progression, advisory-lock identities, and configured filesystem roots remain unchanged in the first composed deployment. Repository extraction never copies runtime databases, logs, uploads, credentials, generated knowledge, or user-authored artifacts. A later root relocation must quiesce writers, copy without following links, verify count/size/SHA-256 against durable records, switch configuration atomically, retain the prior copy for rollback, and expose incomplete purge/migration state.

**Rationale**: Source ownership can change without moving data. Combining repo replacement with a live-data relocation would multiply risk and obscure whether failures came from code, schema, or filesystem movement.

**Alternatives considered**:

- Move blobs into the submodule tree: rejected because runtime data must never be committed and submodule updates could overwrite it.
- Derive paths from Plane package `__file__`: rejected because installed package locations are not durable data roots.

## Decision 9: Integrate LETS as an external warden with local public-client and executor contracts

**Decision**: AstralDeep uses the immutable LETS `v1.0.10` source/release as its initial client/executor baseline while the warden remains separately deployed over authenticated HTTPS. An Astral-owned integration package uses LETS public `LETSClient`, `ReplicaAuthorizer`, `AstralDeepAuthorizer`, `Receipt`, and `ReceiptVerifier` contracts. The operator-configured warden URL is validated, redirects remain disabled, TLS verification is mandatory outside explicit test posture, secrets remain runtime-only, calls have bounded total deadlines and bounded responses, and failures deny protected effects.

**Rationale**: LETS already contains a host-neutral Astral profile for the six declared tool scopes and a fail-closed replay-aware receipt verifier. Reusing these public contracts keeps the projects independent and avoids reproducing cryptographic validation.

**Alternatives considered**:

- Import LETS core or its database into AstralDeep: rejected because it collapses independent ownership.
- Trust an orchestrator-only allow response: rejected because alternate tool paths could bypass it.
- Reimplement receipt cryptography in AstralDeep: rejected because parallel security implementations drift.

## Decision 10: Enforce LETS after Astral gates and again at the final effect boundary

**Decision**: Initial enforcement is off by default and targets dynamic/user-authored agents only. Existing Astral identity, delegated-token, owner, permission, policy, taint, security-flag, PHI, egress, and confirmation gates run first. A durable operation ID becomes the LETS request ID; a unique nonce and canonical evidence digest bind the exact proposed effect. The final tool gateway compares the returned receipt to the expected tenant, envelope, lease, agent subject, policy/config epoch, request ID, transition, audience, nonce, cost, and evidence digest, then atomically verifies and claims it before executing the effect. Every enabled execution path must route through this gateway.

**Rationale**: LETS constrains remaining finite authority; it must never widen Astral permissions. Final-boundary comparison plus replay claim closes wrong-operation, wrong-agent, wrong-audience, stale, and replay paths that signature verification alone cannot close.

**Alternatives considered**:

- Call LETS before Astral authorization: rejected because it leaks activity and consumes authority for requests Astral already denies.
- Check only signature, issuer, and expiry: rejected because a valid receipt can still be bound to a different operation or subject.
- Fail open during LETS outages: rejected because enabled enforcement would become bypassable.

## Decision 11: Persist LETS lifecycle and operation state in AstralPlane

**Decision**: Plane owns neutral records for authority bindings, lifecycle operations, protected-effect attempts, and correlated receipt claims. Deep owns mapping, rollout, and lifecycle policy. Bindings are owner-isolated and fence tenant, envelope, lease, policy digest, configuration epoch, subject, lineage, state, and latest confirmed sequence. Lifecycle changes use stable idempotency keys and explicit pending/succeeded/failed/uncertain states; uncertain remote mutations are reconciled before retry.

**Rationale**: Process memory cannot safely support retries, restarts, revocation, or correlated audits. Persistence in Plane keeps durable state in its owning project without letting Plane decide policy.

**Alternatives considered**:

- Reconstruct bindings from LETS on every request: rejected because owner isolation and local lifecycle intent would be lost during outages.
- Store only a lease ID on the agent row: rejected because policy/config epochs and sequence fences are needed to reject stale receipts.

## Decision 12: Use one versioned composition contract and domain-specific digests

**Decision**: `config/astral-composition.json` conforms to a committed JSON Schema and records contract versions for Projection, Plane, Primitives, and LETS. Projection publishes its UI-protocol digest; Plane publishes library contract and schema compatibility; Primitives publishes package/protocol version; LETS publishes release, receipt wire type, API compatibility, and scope-profile version. Deployment-specific LETS policy/machine/configuration digests are verified from the authenticated runtime trust manifest and recorded in runtime/case-study evidence rather than a repository-global file. AstralDeep verifies the build-time composition before producing an artifact and the deployment trust identity before normal governed traffic.

**Rationale**: Four independent release trains need an explicit compatibility join. A single manifest allows clean-checkout checks, image labels, diagnostics, case-study evidence, and rollback to refer to the same composition.

**Alternatives considered**:

- Infer compatibility from Git commit dates: rejected because chronology is not an interface contract.
- Keep versions only in prose: rejected because startup/build gates cannot enforce prose.
- Encode every domain into one shared package: rejected because it would create a fifth coupling repository and blur ownership.

## Decision 13: Stage client release trust rather than silently renaming it

**Decision**: Projection becomes the build/test owner, but existing installed-client trust is preserved through a bounded compatibility transition. The old AstralDeep workflow identity remains only as an exact-purpose compatibility publisher/bridge while a transition client release learns the authenticated Projection identity. New verification accepts only explicitly pinned old/new identities and immutable decision/artifact digests; later removal of the old identity requires a separately verified release. This feature implements and tests the path but does not publish a release.

**Rationale**: Existing Windows integrity checks pin the old repository/workflow identity. Simply changing strings would strand installed clients or silently expand trust.

**Alternatives considered**:

- Replace the trusted repository name immediately: rejected because repository redirects are not signer continuity.
- Trust any workflow in the AstralDeep organization: rejected because it broadens the trust boundary.
- Leave all client release ownership in Deep permanently: rejected because it conflicts with Projection ownership.

## Decision 14: Defer broad CI, not narrow verification

**Decision**: Each extraction slice runs targeted local tests, import guards, contract checks, and repository cleanliness checks. Near completion, a clean recursive checkout runs Plane migration/recovery tests against PostgreSQL 17, Deep backend/module/security tests, LETS golden and denial matrices, web tests, Windows tests offscreen, Android lint/unit/build, Apple tests where a macOS runner is available, image build/boot checks, updater trust checks, and a representative composed flow. Hosted CI is used only after local qualification and draft checkpoint pushes.

**Rationale**: This honors the owner's CI-usage request while catching boundary errors close to the change that introduced them. Local qualification remains evidence, not a claim that unavailable platform checks passed.

**Alternatives considered**:

- Run the full matrix after every move: rejected as slow and wasteful during mechanical extraction.
- Ignore all tests until the end: rejected because boundary regressions would become difficult to localize.

## Decision 15: Treat the paper as a local, exact-revision case study

**Decision**: The ignored manuscript remains local. The immutable v1.0.10 results remain labeled as release evidence. Astral integration measurements are stored separately with exact five-repository revisions, composition manifest digest, policy/machine digests, commands, environment, raw outputs, and reproduction status. If LETS runtime changes, they receive a successor release and the paper updates its version claim. The named paper cites Armstrong et al., “A Secure Sandbox Environment for Orchestrating Medical AI Agents Using Model Context Protocols and Role-Based Access Control,” AMIA Jt Summits Transl Sci Proc. 2026:57, PMCID PMC13274365; the anonymous paper uses neutral self-citation and distinguishes the historical published architecture from the current case-study system.

**Rationale**: This preserves the meaning of the signed v1.0.10 tag, prevents post-hoc relabeling, and makes new claims reproducible without prematurely publishing manuscript sources.

**Alternatives considered**:

- Rewrite v1.0.10 in place: rejected because signed release evidence is immutable.
- Cite the AMIA paper as proof of current implementation: rejected because it documents a historical system version.
- Commit the active submission tree now: rejected because the owner explicitly kept it local.

## Boundary-critical issues carried into implementation

The following findings are not optional cleanup; they affect correctness at the new boundaries and must be tracked in tasks and kos-wiki:

1. `Database.execute()` returns a cursor after releasing its pooled connection; replace it with a detached result.
2. SQL placeholder conversion uses lexical replacement; migrate Plane-owned SQL to native psycopg placeholders.
3. Audit retry storage can lose/drop entries; replace it with the transactional outbox.
4. Attachment account deletion can report success while blob removal failed; add durable purge tombstones and visible incomplete state.
5. Audit retention can remove the beginning of a hash chain while verification restarts at genesis; add an authenticated retention checkpoint/anchor.
6. Schema startup currently includes product reconciliation and filesystem/UI work; split deterministic schema work from Deep and Projection reconciliation.
7. Repeated `Database()` construction currently performs schema initialization; retain compatibility initially, then move to one explicit boot initializer.
8. Ignored Android `keystore.properties` and `local.properties` must be preserved before the source client tree is removed.
9. Ignored LETS manuscript and result trees and ignored Plane database/log state must never be selected by cleanup or replacement commands.

No unresolved `NEEDS CLARIFICATION` items remain.
