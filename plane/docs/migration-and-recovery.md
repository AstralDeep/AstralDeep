# AstralPlane migration and recovery

AstralPlane changes source ownership without moving live data during the first cutover. The
PostgreSQL database and the configured attachment, artifact, workspace, and generated-knowledge
roots remain one recovery unit. Package or submodule directories are never durable-data roots.
Attachment composition must bind `create_streaming_blob_store(root=...)`, the materialization
coordinator, and the durable purge executor to the same recorded absolute attachment root. The
factory never selects a root.
With `create_root=True` it may securely create only the final missing directory after validating
the complete existing ancestry; operators can require fail-closed pre-provisioning with
`create_root=False`.

## Before upgrade

Revision `088.001` adds the assignment execution-profile discriminator and original-key
operation receipts. Its predecessor is the exact qualified `079.001` registry/digest;
existing assignment JSON, submission digests and unresolved effects are not rewritten.
Legacy workers must use profile-filtered claims, and the matching Deep composition must
remain closed until its new exact Plane schema/digest pin is qualified. An older binary
is not declared compatible with the new schema merely because its rows remain present.
The populated predecessor, repeat-upgrade and new-catalog corruption cases are exercised
in `tests/repositories/test_assignments_postgres.py`.

Revision `088.002` adds `web_session.incarnation_id` using PostgreSQL `gen_random_uuid()`,
with non-null, uniqueness and canonical UUID4 constraints. It requires the exact `088.001`
catalog and registry digest `b6eaa819e9bd471350e48e431686c1ed6922e206608f1b673e0544c14014552d`.
The immutable `088.001` statements are unchanged. Existing session columns, grant envelopes,
assignment JSON, usage and unresolved effects are preserved; existing sessions receive an identity
once in the guarded transaction. Repeat startup verifies the complete catalog and never replaces
identities or retires sessions. A catalog collision/corruption aborts rather than adopting supplied
identities. `tests/integration/test_session_incarnation_upgrade.py` exercises populated upgrade (including
real synthetic v1 issued and uncertain action ledgers, operation receipts, usage and grant bindings),
repeat and constraint corruption. New writes must use the matching incarnation-aware repository
and host contract; an older binary is not qualified against this schema.

1. Close admission and quiesce all writers.
2. Record the exact AstralDeep composition, AstralPlane commit, contract version, schema revision,
   migration digest, blob-layout version, and configured durable roots.
3. Create a transactionally consistent PostgreSQL backup and a non-following copy or snapshot of
   every configured blob/workspace root from the same maintenance window.
4. Verify backup readability, file counts, byte counts, and SHA-256 values against durable records.
5. Confirm the target Plane revision declares the observed schema readable. Do not infer a
   destructive downgrade or skip an unknown revision.

## Upgrade

The application boot initializer obtains advisory lock `(1095980114, 60001)`, runs the exact
repeat-safe migration registry, and commits its revision plus migration-set digest atomically.
Required product reconciliation then runs separately under `(1095980114, 60002)` with durable
versioned hook markers. Traffic remains closed until both phases report durable completion.

`create_postgres_runtime(...)` owns psycopg2 pool construction and applies one additional guarded
fresh-install step. If structural inspection proves that the current application schema is empty,
Plane creates the schema-only `066.001` compatibility baseline and its legacy revision marker in
one transaction under the same migration advisory lock. It then runs the canonical registry in a
second transaction. A crash between those transactions leaves an ordinary recoverable `066.001`
predecessor; the next startup resumes at the registry edge. Existing databases never replay the
baseline.

PostgreSQL `TEMPLATE template0` databases legitimately start with schema `public` owned by the
special `pg_database_owner` role and PUBLIC `USAGE`. Predecessor attestation accepts only that exact
schema name, owner, and default ACL as a bounded fresh-database variant; any arbitrary owner or ACL
change still fails before the first edge. Revision `074.004` then transfers the selected schema to
the migration user and revokes every PUBLIC schema privilege in the migration transaction, so the
single current-schema digest has the same owner/ACL posture for default `public` and private
application schemas.

The canonical current path is
`066.001 -> 067.001 -> 074.001 -> 074.002 -> 074.003 -> 074.004 -> 075.001 -> 079.001 -> 088.001 -> 088.002 -> 088.003 -> 088.004 -> 088.005 -> 088.006 -> 088.007 -> 088.008 -> 089.001 -> 089.002 -> 089.003 -> 089.004 -> 089.005 -> 089.006 -> 089.007 -> 089.008 -> 089.009 -> 089.010 -> 089.011 -> 089.012 -> 089.013 -> 089.014 -> 089.015 -> 089.016 -> 089.017 -> 089.018 -> 089.019 -> 089.020 -> 089.021 -> 089.022`; every edge required
for one run commits in the same transaction. Before the first write, the runner compares the exact source
revision's complete normalized catalog with its pinned predecessor allowlist. Every edge then runs
its own postcondition. This prevents a later `IF NOT EXISTS` statement from repairing or concealing
a damaged predecessor. The `067.001` edge adds transactional outbox, audit-retention
anchor, purge-tombstone, and reconciliation-marker storage. It also verifies the legacy audit-chain
topology, backfills an explicit per-owner sequence without changing existing schema-v1 HMAC bytes,
and installs a same-lock transition trigger so legacy schema-v1 and Plane schema-v2 writers cannot
allocate the same sequence. The `074.001` edge adds neutral LETS authority binding, lifecycle,
protected-effect, receipt-claim, sequence-watermark, and authority-outbox constraints. Neither edge
relocates an existing table or blob.

The `074.002` edge adds owner-partitioned qualification-audit runs, case results, evidence,
versioned audit entries, and LaTeX artifacts. Existing legacy audit-entry hashes remain version 1;
new atomic review transitions use the version-2 canonical full-record hash. Re-running the edge is
repeat-safe and never silently rewrites an existing audit history.

The `074.003` edge preserves preexisting runtime-contract-v2 host-session history under an explicit
legacy marker while requiring all newly created host sessions to use the exact current
runtime-contract version 3. Versions outside that bounded compatibility set remain rejected.

The `074.004` edge adds the hidden pending/READY attachment-materialization lifecycle, DB-clock
leases, exact owner/attachment/filename storage identity, typed attachment-prefix and owner-namespace
purge scopes, a retired-owner admission fence, and case-fold uniqueness for owner and attachment
identities. It backfills canonical legacy live rows as READY, creates deterministic prefix purge
work for every predecessor soft-deleted attachment, and moves unfenced legacy exact-key tombstones
to manual review. It drops the lifecycle default after backfill so a stale baseline-shaped insert
fails closed. Partial lifecycle namesakes, noncanonical legacy READY locators, case aliases, or
incompatible owner-state objects fail before any mutation or revision stamp.

The `075.001` edge requires the exact `074.004` predecessor revision, historical registry digest,
and attested catalog. Under closed admission it adds nullable `voice_session.speech_backend`,
backfills every predecessor session to `llm_factory`, then makes the discriminator non-null. Only
after verifying the exact predecessor transport, identity, revision, media-grant, and worker-grant
constraints does it replace them. Remote-only columns become conditionally nullable: an
`llm_factory` row retains a `livekit|watch_pcm_websocket` transport and complete bounded remote
media/grant shape, while a `client_local` row uses only the `client_local` transport and carries no
room, participant, worker, media-grant, or worker-grant metadata. Backend and transport remain
immutable repository identities. Voice turns are unchanged and remain bound to their parent
session; no audio, transcript, digest, proof, local-engine, or capability field is added.

The additive `079.001` edge requires the attested `075.001` catalog and historical registry
digest. It creates owner-isolated assignment, source-event, action/approval, and activity tables.
Assignments retain bounded JSON checkpoints and task graphs; indexed event and action identities
protect replay independently of working memory. Existing voice, audit, conversation, grant, and
blob data remain unchanged. The current structural verifier checks the four tables, constraints,
indexes, and immutable remote-proposal association index on every startup.

### `088.004` declarative definitions and recovery

This edge requires the exact `088.003` registry digest and catalog. Every existing
agent and revision is classified `executable`; existing artifact fields, revision
states, host/runtime records, ownership, trust, grants, and outstanding assignment
liabilities remain unchanged. No historical definition is inferred. A new
`declarative` agent stores immutable versioned definition snapshots, including the
host-validated policy, in the existing revision table. Its selection uses
`selected_definition_revision_id`; executable promotion pointers remain empty.
Runtime instances and artifact publications have explicit executable-only foreign
keys. An authored definition cannot become an executable fallback.

The public agent facade exposes closed `DeclarativeAgentCommand` preparation and
application, plus the existing owner-scoped revision read/history API. Preparation
only reads and locks. The host retains current identity, policy validation, audit,
and final caller checks in the same transaction. Application independently repeats
preparation and uses a savepoint for every lifecycle/receipt write. A failure after
the host's audit still requires rollback of the enclosing transaction; returned
records are provisional until commit succeeds. Activation selects metadata only:
it creates no assignment, consent, grant, tool permission, trust, or runnable host.
Revision clears the selection and advances the head state fence. A consumer must
check that fence and exact selected revision before using unfinished references;
this storage change does not register an execution consumer.

New metadata operations lock shared owner domain 79, the owner-retirement row,
then agent-owner domain 0. Any caller session lock must precede agent-owner 0.
Sorted agent-identity locks in the distinct two-integer advisory namespace
`_AGENT_IDENTITY_LOCK_NAMESPACE` follow and prevent absent-row
creation races with legacy trust/ownership writers. Those legacy writers never
acquire owner/session locks after an identity lock and take it only when no
immutable-kind head exists. This preserves legacy head-then-ownership transitions;
their next SQL statement checks kind again after any absence lock wait. Composed
callers must take owner locks before legacy identity writes and sort multiple
identities. Executable creation itself takes owner 0 before its identity lock.
New metadata methods do not
acquire Work, session, configuration, or guidance locks after agent locks. A future
definition consumer must preserve that order; no guidance SQL belongs in Deep.

Accepted metadata-only receipts bind owner, command UUID4, target, request digest,
and resulting revision/state. Identical replay returns the original receipt with
the current head, including an archived/deleted head; it never activates or audits
again and still requires a current owner. There are at most 4096 receipts per
target: ordinary commands stop at 4094, leaving one state-changing archive and one
delete slot. A new ID for an already archived/deleted state is refused; accepted
IDs remain replayable. Receipts are never evicted to permit repeated mutation.

Before deployment, quiesce writers and record the exact joint database/root backup,
component identity, and predecessor digest. Apply the guarded edge and verify the
complete current catalog, populated executable/legacy/remote compatibility,
declarative lifecycle/denials, and repeat startup before opening authoring. A failed
transaction must leave predecessor data and metadata intact. Prefer a guarded
forward repair; if restoration is necessary, keep admission closed and restore the
verified database and durable roots together with the matching application. Do not
drop definitions/receipts, rewrite revision markers, infer a downgrade, or activate
definitions during startup. Apply the existing governed session-retirement restore
procedure before reopening restored authority. The migration itself does not
qualify Deep policy, UI, runtime adoption, or institutional staging.

### `088.005` owner guidance and recovery

This additive edge requires the exact `088.004` registry and catalog. It creates
skill heads/immutable revisions/cutover markers, current encrypted-note rows, and
an assignment guidance selection header/reference index. Existing executable and
declarative agents, automatic memory, grants, issued permits, usage, and uncertain
liabilities remain byte-for-byte unchanged. The migration does not import files,
infer note values, bind assignments, or enable a host/UI consumer.

Before applying it, close writers and record the joint database/root backup and
component identities. Run the guarded registry, complete current-catalog check,
populated upgrade/repeat and rollback/corruption checks before reopening admission.
A failed edge rolls back its DDL and registry metadata. Prefer a guarded forward
repair; do not drop new tables or rewrite the schema marker to simulate downgrade.
Restore database and roots with their exact matching application and keys under
closed admission, then apply governed session retirement and account for any
post-backup note Forget/expiry before allowing reads. Logical current-row deletion
does not erase MVCC, WAL, replicas, or old backups.

The [owner guidance storage contract](owner-guidance-contracts.md) defines controlled
file cutover, receipt/CAS semantics, note erasure and fair expiry, and ordered
assignment invalidation. A successful schema qualification alone does not qualify
Deep privacy/authentication, filesystem materialization, UI, T036 guidance adoption,
or institutional staging.

### `089.001` TypeSafe credentials, consent and recovery

This additive edge requires the exact `088.008` registry and catalog. It adds
two independent owner-keyed tables and alters nothing that exists.

`user_typesafe_credential` holds one owner's TypeSafe System One key as
ciphertext. AstralPlane never sees the plaintext: the product encrypts with the
credential key it already owns and stores the opaque token. The row also carries
`key_fingerprint`, a 12-hex-character digest of the plaintext, and a
`last_verification_outcome` constrained to `unverified`, `valid`, `rejected` or
`unavailable`. The fingerprint is a write condition, not a display value:
`record_outcome` updates a row only when the stored fingerprint still matches
the key the outcome was observed on, so a slow rejection from a revoked key
cannot mark its freshly saved replacement rejected.

There is deliberately no `system_typesafe_credential`. A deployment-wide key is
not representable, which is what makes the bring-your-own-key rule enforceable
rather than advisory. There is also no foreign key to `user_llm_config`:
clearing an LLM configuration never removes a TypeSafe credential, and clearing
a credential never removes an LLM configuration.

`user_data_sharing_acknowledgment` records that an owner accepted a specific
version of the third-party data-sharing notice, keeping both the first and the
latest acknowledgment time. Acknowledgment means the stored `notice_version`
equals the version the product currently shows, so changing the wording requires
a fresh acknowledgment instead of inheriting the old one. It is consent
evidence, not a setting: it has no delete path outside account-level purge, and
it survives clearing either credential.

Follow the same closed-admission backup, guarded upgrade, catalog, repeat and
rollback checks described above. Failed DDL rolls back with registry metadata.

**Recovery.** Once this transaction commits, keep both tables and the exact
`089.001` revision/digest intact. A matching qualified application may disable
TypeSafe routing through its governed product policy; doing so does not erase
encrypted credentials or the durable data-sharing acknowledgment. An `088.008`
binary is not compatible merely because these tables are additive.

Prefer a reviewed forward repair through the guarded migration registry. If the
previous composition must be restored, close admission and quiesce every writer,
then restore the verified pre-upgrade PostgreSQL backup and all paired durable
roots together, with the exact prior application and unchanged credential/audit
keys. Account for post-backup revocations, Forget/expiry and acknowledged consent,
retire restored sessions through the governed recovery contract, and reconcile
external LETS authority, receipt/replay and uncertain-effect state before reopening
admission. A database snapshot must not roll back external authority or replay state.
Rehearse this paired restore and re-upgrade on an isolated copy first. Never drop
these tables, rewrite revision/digest markers, or execute ad-hoc downgrade SQL.

Schema qualification does not establish the product's probe path, its encryption
key resolution, the routing adapter's behavior, or the enforcement of
acknowledgment before a credential save. Those are product concerns verified
outside this repository.

### `089.003` owner stop epochs and recovery

The guarded `089.002 -> 089.003` edge requires predecessor registry digest
`8570c65182f5c90f12319435f9b2614d6ebfff0d1404e4650df6f2bd34962565`
and its full catalog. It adds neutral owner stop anchors/epochs and immutable
peer acknowledgment rows plus immutable operation epoch bindings, without rewriting mesh, IAM, audit, operation, grant,
credential, accounting or blob records. Current catalog digest is
`3441e750601f9b78fa7057d36e0b0c51bed6a912450baca6a30db7aa33fcae22`.
Partial DDL or stamp failure rolls back together; a repeat verifies the complete
current catalog and performs no write. Unknown/same-name tables are refused.

Quiesce host admission and writers, retain the matching old composition and
verify a joint PostgreSQL/durable-root backup before adopting the new pin. Run
the matched registry through normal host initialization; verify repeat startup,
retained representative state and the new host stop/admission transactions
before reopening. An old binary is not compatible merely because its rows
remain present. Host authorization, audit, peer verification and control effects
are independently qualified; schema completion does not establish them.

On failure, preserve failed state and keep admission closed. Retry only from
the exact qualified predecessor/current catalog, or restore the verified paired
pre-upgrade database/durable roots and matching composition. Do not drop tables,
alter revision markers or reset stop epochs to simulate downgrade. After a
joint restore, explicitly retire restored sessions through the matching Plane
recovery facade, restart all application caches, obtain fresh IAM authority and
reconcile stop state before admission. Restored stop/receipt observations remain
historical; the host must not claim that a remote peer has resumed from them.

### `089.004` completion subscriptions and wake receipts

This additive edge requires the exact `089.003` registry digest
`773f574efc9657de7692d6ae27352edbb07ef93ed967f4d5ca121ab5c0e1ac7e`
and its catalog. It adds
two independent owner-scoped tables and alters nothing that exists.

`completion_subscription` binds a waiter operation to a source operation with a
terminal condition and a source-revision fence. `wake_receipt` records exactly
one acceptance per subscription idempotency key and points replays at the
original receipt. The tables stand alone: no foreign keys into operation state
and no writes into wait-state JSON, so subscriptions never smuggle data into
current wait state and survive independent lifecycle changes. Ownership, live
state, terminal coverage and the revision fence are enforced inside the mutation statements
with rowcount checks. Acceptance first locks the owner-scoped subscription until the caller's
transaction ends. A committed revocation or deletion prevents a new acceptance; acceptance
that holds the lock first commits or rolls back before revocation or deletion proceeds.

Follow the same closed-admission backup, guarded upgrade, catalog, repeat and
rollback checks described above. Failed DDL rolls back with registry metadata.

**Recovery.** Once this transaction commits, keep both tables and the exact
`089.004` revision/digest intact. An `089.003` binary is not compatible merely
because these tables are additive.

### `089.005` notification records and recovery

This additive edge requires the exact `089.004` registry digest
`08b7d824a62284351d1ce178708bb185d7c81f77c96ab14e890f7e5675a008fc`
and its catalog. It adds three independent owner-scoped tables and alters nothing that
exists. Its historical catalog digest is
`2aae4fa6281bda3ca690ce9eb174a5e43b454f0862b0544129bd537eb67bb798`.

`notification_preference` stores explicit per-channel/per-event booleans for in-app, inbox,
and push delivery of completion, failure, and required-action events, plus a monotonic revision
and host-supplied update time. `notification_inbox_entry` stores deduplication identity, a
bounded event kind and source reference, an opaque content format, ciphertext and its SHA-256,
creation/retention times, an acknowledgment time, a deletion tombstone, and a monotonic revision.
`notification_delivery_identity` stores an immutable host-supplied delivery key for one live
inbox entry. A composite owner/entry foreign key prevents a delivery identity from referencing
another owner's entry.

Creation is deduplicated by `(owner_id, deduplication_key)`; an exact replay returns the
original entry, including a tombstone, while changed immutable content conflicts. Preferences,
acknowledgment, and deletion use owner plus expected-revision fences in the mutation statement.
The retention sweep is owner-scoped, bounded, and uses `FOR UPDATE SKIP LOCKED`; it only
tombstones entries whose retention boundary has passed. Physical deletion remains an explicit
host purge decision. Plane never decrypts content, selects a route or provider, validates IAM,
or decides that a notification should be sent.

Follow the same closed-admission backup, guarded upgrade, catalog, repeat and rollback checks
described above. Failed DDL rolls back with registry metadata.

**Recovery.** Once this transaction commits, keep all three tables and the exact
`089.005` revision/digest intact. An `089.004` binary is not compatible merely because these
tables are additive. Preserve tombstones and acknowledged state for recovery inspection. A
guarded forward repair is preferred; if the prior composition must be restored, restore the
verified pre-upgrade PostgreSQL backup and paired durable roots together under closed admission,
then retire restored sessions and reconcile any post-backup notification records before reopening.
Never drop the tables, rewrite revision markers, or infer a downgrade.

### `089.006` Atlas pages and recovery

This additive edge requires the exact `089.005` registry digest
`98a9082bb3317192cd6e48df3b63c76dbb9404814cb4a0180eb997f8737e37b0` and its
catalog. Its catalog digest is
`88896cd0498562ecd0b4d610c4453f7ed01f527e0845cacf7300d88374578e48`.
It adds `atlas_page` and `atlas_page_command` and alters nothing that
exists. The page table stores a stable owner/page identity, exact optional
project, live slug, current revision, bounded source references, opaque
host-ciphertext, its SHA-256 digest, caller timestamps, and a content-free
tombstone. The command table stores immutable create/delete receipts under an
owner-scoped command identity and request digest.

The migration infers no existing page, imports no legacy memory/envelope rows,
and does not rewrite preferences, notes, knowledge, or audit state. A same-name
foreign object, damaged predecessor catalog, partial DDL, or failed stamp rolls
back or fails closed before adoption. Repeat startup verifies both tables,
indexes, constraints, and lifecycle/immutability triggers and performs no write.
Reads, listings, and writes carry the owner predicate in SQL. Deletion advances
the current revision under its fence, erases ciphertext and source metadata, and
leaves a terminal tombstone; replaying the original create or delete command
cannot restore content. Revision history and links are not part of this edge;
`089.007` adds immutable revisions and fenced edits on the same repository.

Follow the same closed-admission backup, guarded upgrade, catalog, populated
repeat and rollback checks described above. Failed DDL rolls back with registry
metadata. The populated upgrade, interruption, namesake, and current-catalog
damage cases are exercised by `tests/integration/test_atlas_pages_upgrade.py`;
owner, replay, pagination, concurrency, and tombstone behavior are exercised by
`tests/repositories/test_atlas_pages_postgres.py`.

**Recovery.** Once this transaction commits, keep both tables and the exact
`089.006` revision/digest intact. An `089.005` binary is not compatible merely
because these tables are additive. Prefer a reviewed forward repair through the
guarded registry. If the previous composition must be restored, close admission
and restore the verified pre-upgrade PostgreSQL backup and every paired durable
root together with the exact prior application and unchanged keys. Account for
post-backup Atlas creates and deletes, retire restored sessions through the
governed recovery contract, and reconcile uncertain effects before reopening
admission. Never drop the tables, rewrite revision/digest markers, restore a
tombstone to a live page, or execute ad-hoc downgrade SQL. A restored database
snapshot cannot recover content that was never stored in Plane.
### `089.009` owner capability publications and recovery

This additive edge requires the exact `089.008` registry digest
`3845856d7280e5ebd5e516135344b660cde8e3cff2a86f2ab50b0d4e0cf7999d` and its catalog. It adds one owner-scoped
publication table, its live-publication index, and no provider, discovery, IAM,
or transport object. The catalog digest immediately after this edge is
`50c157e6451628840bb6b2a7ae245dfbd281a9ef1e3a5248831de4ce776ad3a5`; the current migration registry digest is
`dde302967743cf8f6f5d55e82f8355ef405823dfee638909c3a2f11b85f5cb27`. Existing notification, Atlas, mesh, stop,
completion-wake, assignment, session, audit, grant, credential, accounting, and
blob records remain unchanged.

`capability_publication` binds an opaque capability identifier, audience
identifier, exact capability revision, and finite lifetime. A composite
owner/capability/audience key prevents duplicate live audience records without
a deployment-wide published catalog. Publication metadata is stored only as
host-encrypted ciphertext plus its host-computed SHA-256 digest. Publish,
withdraw, and expiry transitions carry the owner predicate and state-revision
fence in one `UPDATE`; a first publish uses `ON CONFLICT DO NOTHING` so a
concurrent exact replay returns the winner. Withdrawal and expiry are terminal
until an explicit republish. Active reads require published state, the
host-supplied lower lifetime bound, and an unexpired upper bound in the same
owner-scoped statement. No read consults the database clock, and no method
commits or rolls back the caller's transaction.

Quiesce host publication and A2A writers and verify the paired PostgreSQL and
durable-root backup before adopting the new pin. Run the guarded registry
through normal host initialization; verify repeat startup, retained
representative rows, the empty new table, and the host's publish/withdraw/expire
transactions before reopening discovery. A failed DDL transaction rolls back
with its registry marker and leaves admission closed. Unknown, same-name, or
damaged catalog objects fail closed rather than being adopted.

**Recovery.** Once this transaction commits, keep the table and the exact
`089.009` revision/digest intact. An `089.008` binary is not compatible merely
because the table is additive. Prefer a reviewed forward repair. If the prior
composition must be restored, keep admission closed and restore the verified
pre-upgrade PostgreSQL backup and every paired durable root together with the
unchanged encryption and audit keys. Account for post-backup publications,
withdrawals, and expiries, retire restored sessions through the governed
recovery contract, refresh host IAM and capability approval, and reconcile
discovery state before reopening admission. Never drop the table or rewrite a
revision marker to simulate downgrade. Schema qualification does not qualify
Deep discovery, IAM, A2A invocation, or client behavior.

### `089.007` Atlas revisions and recovery

This additive edge requires the exact `089.006` registry digest
`1176ee5a65f74e0447abe2e63bb90808acc40289acd1a6a09ec6a10ae1a967b1` and its
catalog digest `88896cd0498562ecd0b4d610c4453f7ed01f527e0845cacf7300d88374578e48`.
It adds `atlas_page_revision`, its bounded history index, immutable revision
trigger, exact-head verification trigger, and the edit command's expected
revision column and receipt constraints. It removes only the predecessor's
one-command-per-page uniqueness so multiple edits can append separate receipts.
Live predecessor heads are backfilled once into exact revision rows; deleted
predecessor pages remain content-free because their ciphertext was already
erased. No unrelated table or row is rewritten.

Fenced edits insert the exact next immutable revision and advance the owner's
page head only when revision, sources, ciphertext, digest, and edit time match
that row. Stale fences, changed command replay, foreign owners, regressive
clocks, exhausted safe-integer revisions, and edits to tombstones fail with a
typed conflict or denial before a new write. Deleted page history requires an
explicit `include_deleted=True` recovery read, and replaying an earlier edit
cannot resurrect the page. PostgreSQL refuses revision updates, revision
deletes, and live head changes without an exact backing revision.

Follow the same closed-admission backup, guarded upgrade, catalog, populated
repeat and rollback checks described above. Failed DDL rolls back with registry
metadata. The populated predecessor, repeat, interruption, namesake, and
damaged-predecessor cases are exercised by
`tests/integration/test_atlas_revisions_upgrade.py`; edit fences, immutable
history, pagination, concurrency, database denial, corrupt reads, and caller
rollback are exercised by
`tests/repositories/test_atlas_revisions_postgres.py`.

**Recovery.** Once this transaction commits, keep `atlas_page`,
`atlas_page_command`, and `atlas_page_revision` with the exact `089.007`
revision/digest intact. An `089.006` binary is not compatible merely because
these changes are additive. Prefer a reviewed forward repair through the guarded
registry. If the previous composition must be restored, close admission and
restore the verified pre-upgrade PostgreSQL backup and every paired durable root
together with the exact prior application and unchanged keys. Account for
post-backup Atlas creates, edits, and deletes, retire restored sessions through
the governed recovery contract, and reconcile uncertain effects before reopening
admission. Never rewrite or delete revision rows, restore a tombstone to a live
page, or execute ad-hoc downgrade SQL. A restored database snapshot cannot
recover content that was never stored in Plane or that was erased from a deleted
head.
### `089.008` Atlas links and recovery

This additive edge requires the exact `089.007` registry digest
`fe60420679846e7ea06ca5da3cef250cde5ad75a4ca47b6eb15739893a8ebd09` and its
catalog digest `939f224d8a11622964a53b37a63dde6ec37095636145b018486883afe70e9dff`. It adds `atlas_page_link`, `atlas_page_link_head`, and
`atlas_page_link_command` and alters no existing table or row. The link table
stores owner, source page, target page, link revision, and caller time with a
reflexive-link prohibition and live foreign keys to both page identities. The
head table stores the current link revision and last link time per source page.
The command table stores immutable request digests and exact page/link fences.

The migration infers no link from page content or legacy graph rows. A same-name
foreign object, damaged `089.007` predecessor, partial DDL, or failed stamp
rolls back or fails closed before adoption. Repeat startup verifies all three
tables, both ordering indexes, constraints, and replacement/head/receipt
triggers and performs no write. Every endpoint read and mutation carries the
owner predicate in SQL; a cross-owner, missing, tombstoned, reflexive, duplicate,
or over-bound endpoint is refused. Mutations replace the complete outgoing link
set under the expected page revision and link revision. Replaying the exact
command returns the stored receipt without writing; changed semantics conflict.
Page deletion removes links in both directions and advances every remaining
source-page link revision, so a stale link writer cannot resurrect a removed
target.

Follow the same closed-admission backup, guarded upgrade, catalog, populated
repeat, and rollback checks described above. The populated upgrade, interruption,
namesake, damaged-predecessor, and current-catalog damage cases are exercised by
`tests/integration/test_atlas_links_upgrade.py`; same-owner reads, replay,
fencing, backlink pagination, concurrency, and tombstone cleanup are exercised by
`tests/repositories/test_atlas_links_postgres.py`.

**Recovery.** Once this transaction commits, keep all three link tables and the
exact `089.008` revision/digest intact. An `089.007` binary is not compatible
merely because the tables are additive. Prefer a reviewed forward repair through
the guarded registry. If the previous composition must be restored, close
admission and restore the verified pre-upgrade PostgreSQL backup and every
paired durable root together with the exact prior application and unchanged
keys. Account for post-backup link edits and page deletions, retire restored
sessions, and reconcile uncertain effects before reopening admission. Never drop
the tables, rewrite revision/digest markers, restore deleted endpoints, or
execute ad-hoc downgrade SQL. A restored snapshot cannot reconstruct a replaced
link set that was not present when the backup was taken.

### `089.010` operation harness bindings and recovery

This additive edge requires the exact `089.009` registry digest
`c3e2b3d2d2a2f88a7b995eb6525f6f01df463bad5129a989f7ed70be5e95ed4b` and its
catalog. The catalog digest immediately after this edge is
`1e1fb4ca7f6ec63e790a65c3ebbaf028702f0ddefb084152fba6cb19cb161c68`.
It adds `operation_harness_binding`, a profile/version/artifact index, and an
immutability function/trigger without altering operation, assignment, notification,
guidance, credential, or audit rows.

Each operation has at most one binding, written once by an owner- and
execution-fence-qualified statement. The identity is a typed profile, numeric
semantic version, and lowercase SHA-256 artifact digest; Plane does not store
runner configuration, provider route, rollout flag, or arbitrary metadata keys.
A missing binding is the legacy state and reads as unbound. Exact replay returns
the original binding, a changed identity conflicts, a stale fence cannot write,
and direct updates or deletes are refused. Operation retention purge removes the
binding through its foreign-key cascade.

The migration infers no harness profile for existing operations, imports no host
catalog, and does not rewrite in-flight operation state. A same-name foreign
object, damaged predecessor catalog, partial DDL, or failed stamp rolls back or
fails closed before adoption. Repeat startup verifies the table, index, function,
trigger, constraints, and foreign key and performs no write.

Follow the same closed-admission backup, guarded upgrade, catalog, populated
repeat and rollback checks described above. The populated upgrade, interruption,
namesake, and current-catalog damage cases are exercised by
`tests/integration/test_harness_bindings_upgrade.py`; legacy absence, owner and
stale-fence denials, immutable replay, restart persistence, and purge cascade are
exercised by `tests/repositories/test_harness_bindings_postgres.py`.

**Recovery.** Once this transaction commits, keep the table and the exact
`089.010` revision/digest intact. An `089.009` binary is not compatible merely
because the table is additive. Prefer a reviewed forward repair through the
guarded registry. If the previous composition must be restored, close admission
and restore the verified pre-upgrade PostgreSQL backup and every paired durable
root together with the exact prior application and unchanged keys. Account for
post-backup bindings, retire restored sessions through the governed recovery
contract, and reconcile uncertain effects before reopening admission. Never
rewrite or drop persisted bindings, infer an operation's profile identity, or
execute ad-hoc downgrade SQL. A restored binding does not prove that its host
deployment artifact or profile identity is still available.

### `089.011` push destinations and claimed deliveries

This additive edge requires the exact `089.010` registry digest
`facf476fbde30deb1952b352e2f80d56742956c368b9f97e3e04478934cc2d9b` and its catalog. It adds
`notification_device_destination` and `notification_push_delivery` and alters nothing that exists.
The destination row stores an owner-scoped opaque device key, host-encrypted token ciphertext,
its SHA-256 and format, a monotonic `token_version`, a current `revision`, and revocation/deletion
tombstones. Deletion clears token material. The delivery row snapshots the inbox event content,
destination token version and revision, and carries a bounded retry policy, lease owner, claim
identity, attempt count, availability time, and terminal acknowledgment/error state.

Enqueue requires an owner-owned live inbox entry and a live destination at the caller's expected
revision and token version. Claims select only work whose snapshot still equals the destination's
current token version and revision, lock the delivery with `SKIP LOCKED` and the destination for
share, and return the encrypted destination token to the host. Rotation, revocation, or deletion
therefore makes older pending work unclaimable until the host supersedes it. Expired claims are
reclaimed up to `max_attempts`; exhausted leases become `dead_letter`. A late acknowledgment is
accepted only for the exact current claim identity, even after lease expiry, so it cannot mark a
newer claim delivered. Plane never decrypts a token, calls a provider, chooses a device, or makes
an IAM or notification-policy decision.

Follow the same closed-admission backup, guarded upgrade, catalog, repeat and rollback checks
described above. Failed DDL rolls back with registry metadata. The populated upgrade, interruption,
namesake, and current-catalog damage cases are exercised by
`tests/integration/test_push_deliveries_upgrade.py`; destination, claim, retry, late
acknowledgment, concurrency, restart, and caller-rollback behavior are exercised by
`tests/repositories/test_push_deliveries_postgres.py`.

**Recovery.** Once this transaction commits, keep both tables and the exact `089.011`
revision/digest intact. An `089.010` binary is not compatible merely because these tables are
additive. Preserve claims, retry counters, dead letters, and content-free destination tombstones
for recovery inspection. Prefer a guarded forward repair. If the prior composition must be
restored, close admission and restore the verified pre-upgrade PostgreSQL backup and every paired
durable root together with the exact prior application and unchanged keys. Reconcile deliveries
that may have reached a provider before the backup boundary before reopening, and never re-deliver
a claimed delivery merely because its row was restored. Never drop the tables, restore a deleted
destination token, reset attempt counters, rewrite revision/digest markers, or execute ad-hoc
downgrade SQL.

### `089.012` Tool observation records and recovery

This additive edge requires the exact `089.011` registry digest
`8677b4e37ea98a7471ca97a82bc0182aed0f68002cace976ecfaacf8487d04fc` and its
catalog. The catalog digest immediately after this edge is
`e506006358926677d9dba3261e0a17b4c56815132f747953048f6fcca1e1e38d`.
It adds `tool_observation` and `tool_observation_page` and alters nothing
that exists. The parent table stores the owner/conversation/observation
identity, per-conversation capture order, originating operation reference,
caller-held encryption seal, bounded source references, page extent, the
whole-observation SHA-256 digest, the capture request digest, caller integer
second timestamps, an absolute expiry inside a 24-hour retention ceiling, and
a content-free tombstone. The child table stores the bounded opaque ciphertext
pages with their per-page SHA-256 digests under the parent foreign key.

The migration infers no existing observation, imports no legacy memory or
audit rows, and does not rewrite preferences, notes, knowledge, atlas, or
audit state. A same-name foreign object, damaged predecessor catalog, partial
DDL, or failed stamp rolls back or fails closed before adoption. Repeat
startup verifies both tables, indexes, constraints, and lifecycle/immutability
triggers and performs no write. Reads, listings, recall, and writes carry the
owner and conversation predicates in SQL. Capture replays exactly under its
request digest and cannot exceed the bounded per-conversation capacity.
Recall returns bounded pages with continuation or explicit end-of-source
state, fails closed on expired, purged, foreign, or integrity-damaged
sources, and never renews expiry. Purge and expiry sweeps tombstone the
parent, erase every page, and leave a terminal durable tombstone; replaying
a capture cannot restore content. Recall authorization, audience, packing,
compaction, and provider policy remain with the host.

Follow the same closed-admission backup, guarded upgrade, catalog, populated
repeat and rollback checks described above. Failed DDL rolls back with registry
metadata. The populated upgrade, interruption, namesake, and current-catalog
damage cases are exercised by `tests/integration/test_observations_upgrade.py`;
owner isolation, replay, paged recall, expiry, purge, capacity, races,
corruption, and caller-rollback behavior are exercised by
`tests/repositories/test_observations_postgres.py`.

**Recovery.** Once this transaction commits, keep both tables and the exact
`089.012` revision/digest intact. An `089.011` binary is not compatible merely
because these tables are additive. Prefer a reviewed forward repair through the
guarded registry. If the previous composition must be restored, close admission
and restore the verified pre-upgrade PostgreSQL backup and every paired durable
root together with the exact prior application and unchanged keys. Account for
post-backup captures, recalls, and purges, retire restored sessions through the
governed recovery contract, and reconcile uncertain effects before reopening
admission. Never drop the tables, rewrite revision/digest markers, restore a
tombstone to a live observation, or execute ad-hoc downgrade SQL. A restored
database snapshot cannot recover observation content that was never stored in
Plane, and an expired or purged observation stays unavailable after recovery.

### `089.002` mesh enrollment records and recovery

This additive edge requires the exact `089.001` registry and catalog. It adds
eight independent objects and alters nothing that exists.

`mesh_record` holds one owner's personal mesh with its bounded display name and
the two monotonic epoch counters: `membership_epoch` for member activations and
`revocation_epoch` for revocations. `mesh_member` stores each device, agent, or
companion identity with the membership epoch it was activated at, its status,
and a `record_version` used as a compare-and-set fence. `mesh_public_identity`
stores only public key material plus its digest; private device keys never reach
AstralPlane. `mesh_enrollment_challenge` and `mesh_enrollment_invitation` store
opaque SHA-256 digests of the possession secret and the invitation secret, never
the secrets themselves, with host-supplied BIGINT issue/expiry windows. The
invitation state machine (`pending` → `consumed` → `confirmed`, plus `expired`
and `revoked`) uses fenced `UPDATE ... RETURNING` statements. Confirmation and
member activation share a mesh-first lock and a savepoint, with current mesh,
member, invitation and host-time fences; a losing transition leaves no partial
confirmation or epoch advance. Concurrent consumers or confirmations admit one
winner. `mesh_member_revocation` records each revocation with its own epoch and
reason. Possession proof, IAM, QR/network handling, and admission policy remain
with the host; the host computes every digest it stores.

Follow the same closed-admission backup, guarded upgrade, catalog, repeat and
rollback checks described above. Failed DDL rolls back with registry metadata.

**Recovery.** Once this transaction commits, keep the new tables and the exact
`089.002` revision/digest intact. Epoch counters are monotonic by design and must
never be rewritten. A matching qualified application may stop enrolling meshes
through its governed product policy; doing so does not erase membership or
revocation evidence. An `089.001` binary is not compatible merely because these
tables are additive.

Prefer a reviewed forward repair through the guarded migration registry. If the
previous composition must be restored, close admission and quiesce every writer,
then restore the verified pre-upgrade PostgreSQL backup and all paired durable
roots together, with the exact prior application and unchanged credential/audit
keys. Retire restored sessions through the governed recovery contract and
reconcile uncertain effects before reopening admission. Rehearse this paired
restore and re-upgrade on an isolated copy first. Never drop these tables,
rewrite revision/digest markers, or execute ad-hoc downgrade SQL.

### `088.008` framework credentials and recovery

This additive edge requires the exact `088.007` registry and catalog. It creates the owner-keyed
`framework_credential` table, which stores only a SHA-256 digest and a short display prefix of each
bearer token, and adds nullable `max_admissions` and `consumed_admissions` columns with an
allowance constraint to `user_offline_grant`. Existing offline grants keep null allowances and
therefore their unlimited-admission semantics; no framework credential is inferred for an existing
owner or session.

Follow the same closed-admission backup, guarded upgrade, catalog, repeat and rollback checks
described above. Failed DDL rolls back with registry metadata. After a committed upgrade, keep the
table, the columns, and the exact `088.008` revision/digest intact; an older composition refuses
the upgraded schema rather than reading it. Prefer a guarded forward repair. Otherwise, under closed
admission, restore the verified pre-upgrade PostgreSQL backup and paired durable roots together
with the matching prior application, then retire restored sessions through the governed recovery
procedure before reopening. Never drop the table or columns, rewrite revision markers, or execute
ad-hoc downgrade SQL. Schema qualification does not establish the host's issuance, scope, or
execution-authority policy; see `credentials-and-grants.md`.

### `088.007` scheduler policy and recovery

This additive edge requires the exact `088.006` registry and catalog. It adds
the optional `scheduled_job_policy` row and the `scheduled_occurrence_assignment`
binding described in `knowledge-scheduler-and-async-contracts.md`. No existing
scheduler, occurrence, run or effect row changes, no policy row is inferred for
an existing definition, and no run limit is imposed on old jobs. Charges and
bindings created after the upgrade are never decremented or rewritten by Stop.

Follow the same closed-admission backup, guarded upgrade, catalog, repeat and
rollback checks described above. Failed DDL rolls back with registry metadata.
After committed upgrade, recover with the matching database and application;
a `088.006` binary must not be asserted compatible with policy rows or bindings.
Schema qualification does not establish Deep's admission wiring, the monitoring
outcome classification, or the schedule-surface Stop.

### `088.006` selected input and recovery

This additive edge requires the exact `088.005` registry and catalog. It adds a
nullable immutable selected-input envelope and a separate owned declarative-agent
revision index. Existing headers keep a null envelope; no historical selected
definition, expansion, or private value is inferred. Existing action accounting,
skill history, encrypted notes and declarative revisions remain unchanged.

Follow the same closed-admission backup, guarded upgrade, catalog, repeat and
rollback checks described above. Failed DDL rolls back with registry metadata.
After committed upgrade, recover with the matching database, durable roots and
application; apply governed session retirement and post-backup Forget/expiry
reconciliation before admission. Never drop references or rewrite the schema
marker to make stale selected guidance executable. The
[selected input contract](selected-input-contracts.md) defines immutable binding,
ordered invalidation and final database-clock assertions. Schema qualification
does not establish host authentication, keyed binding verification or T036
execution/publication integration.

### `079.001` deployment and recovery

1. Quiesce all writers and take the joint database/durable-root backup described above. Record
   the exact `075.001` revision and migration digest, the candidate Plane commit, and target digest.
2. Apply the guarded registry and verify `079.001` and its exact catalog. Repeating boot must be
   a verified no-op. Keep the product's assignment worker admission disabled until its foreground
   authorization, cancellation, cost bounds, and activity paths have passed qualification.
3. Enable only the qualified product worker. Recover expired assignment leases through
   `recover_expired_for_administration`; use the returned operation bindings to reconcile the
   ordinary work-admission coordinator. Never copy a lease token into a new worker.
4. A reserved action has not begun and may release its reservation. A committed dispatch permit
   is the durable start boundary: controls prevent subsequent permits, while prior permitted
   work may finish and report its exact receipt. Read-only interruption is charged at its reserved
   maximum before bounded retry. Uncertain effects retain their maximum liability and require an
   exact late receipt or owner-authorized reconciliation; never reset their ledger to ready.
   A `reconciled_applied` result with `result_available: false` records the effect classification
   without recovering its response. Keep dependent work in reconciliation until usable evidence
   or revised owner instructions permit continuation; never publish a fabricated result.
5. Stop and inspect assignments before account retirement. `retire_owner` permanently fences
   new owner work and removes safe terminal assignments. A result containing unresolved action
   IDs must be committed and reported as awaiting reconciliation; physical owner purge must wait.
   Linked pending/approved remote proposals are expired before their inverse association is removed.
6. If qualification fails before admission reopens, prefer guarded forward repair. Restore the
   verified joint backup with the previous application only under closed admission. Never drop
   the assignment tables or rewrite revision metadata as an inferred rollback; doing so destroys
   the replay and approval evidence for actions that may already have begun.

### `075.001` deployment profile

1. Quiesce voice admission and drain or end every active session; keep all other writers closed
   under the normal maintenance procedure.
2. Record and verify the exact `074.004` revision/digest and create the paired PostgreSQL and
   durable-root backup described above.
3. Apply the pinned Plane candidate and verify `075.001`, its exact migration digest, the current
   structural verifier, every predecessor session backfilled as `llm_factory`, and preserved voice
   turns.
4. Start the candidate with the product selector still set to `llm_factory`; run the ordinary
   authenticated remote LiveKit/watch smoke before reopening that profile.
5. Qualify `client_local` separately in a candidate environment: create a local-profile session,
   confirm every remote-only database field is null, and exercise ordinary authenticated dispatch
   while remote speech media endpoints are blocked. Do not infer local execution from the database
   discriminator alone.
6. If validation fails before traffic reopens, prefer a guarded forward repair. Otherwise keep
   writers quiescent and restore the verified pre-upgrade PostgreSQL and durable-root snapshots as
   one unit with the prior application. Never run inferred down-SQL or rewrite `client_local` rows
   into a predecessor shape.

An authority provisioning row uses deterministic `pending:<field>:<32hex>` remote identities and
zero lease metadata until a compare-and-set activation supplies the issued LETS identities. That
pending shape is valid only while `provisioning` or after an unissued attempt is durably `closed`;
all other states forbid the reserved prefix and require positive lease expiry. Closing an unissued
attempt preserves its lifecycle evidence while releasing the nonterminal uniqueness slot.

An empty database is an approved source only for Plane's guarded baseline initializer. Structural
inspection accepts either no application tables or a metadata-table-only shell with no metadata; any
other non-empty database must carry a known revision and every required baseline table. A partial or
unknown schema fails closed without being overwritten. Every predecessor from `067.001` through
`089.007` must carry its own pinned historical registry digest. A current `089.022` marker and
digest are still insufficient on their own. The verifier binds the owned schema owner/ACL and the
behavior, durability, authorization, namespace, dependency, and lifecycle shape of all Plane-owned
tables, columns, sequences, constraints, indexes, functions, triggers, policies, rules, and
inheritance edges after a transition and on every already-current startup. A legacy `066.001` marker
has no Plane digest and is accepted only through its explicitly attested registry edge.

Database-level ACLs and connection-level database/search-path selection are host composition policy
outside Plane's owned-schema digest. The host must connect with the intended database and put the
configured Plane schema first. Plane does attest and harden the selected schema namespace itself,
including its owner and normalized ACL, and every owned relation is resolved by exact namespace OID;
it never falls through to a later search-path namesake. Owned PL/pgSQL functions use an explicit
`pg_catalog, <owned schema>, pg_temp` search path so temporary objects cannot shadow their durable
dependencies.

## Representative fixture replay

`tests/fixtures/pre_split/` contains a targeted synthetic non-PHI PostgreSQL and blob snapshot. It
is migration evidence, not a complete AstralDeep backup. `loader.py` requires an absent unique test
schema, runs the pinned extracted `066.001` historical builder exactly once, and then applies a
DML/supplement-only `baseline.sql`; it never replays repeat-safe builder DDL over a partial
predecessor. The fixture preserves representative records for history, preferences, attachments,
workspaces, scheduling, remote metadata, and voice metadata. `expected.json` binds the historical
builder source blob and SHA-256, loader SHA-256, normalized pre-migration catalog row count/digest,
and two canonical nested blob files by relative key, byte count, and SHA-256. The loader stages
blobs without following links, promotes the verified tree, and commits only after both halves are
ready.

Run the PostgreSQL replay only against an isolated test database whose role may create and drop
schemas and databases:

```text
$env:ASTRALPLANE_TEST_POSTGRES_DSN='<isolated test database URL>'
python -m pytest -q tests/integration/test_pre_split_upgrade.py
python -m pytest -q tests/integration/test_empty_database_startup.py
python scripts/record_migration_evidence.py
```

On POSIX, use the shell's normal `export` form. The evidence recorder runs eight cases sequentially
with one worker and writes only input/output digests and bounded status metadata to
`provenance/checks.json`; it never stores the database URL or raw test output. The committed
`provenance/checks.json` is the historical record of that matrix at `074.004` (2026-08-21); its
recorded input digests no longer match current bytes, so it is not evidence for the current schema
(see `provenance/README.md`). The integration suite creates only schemas named
`astralplane_fixture_<hex>` or `astralplane_hostile_<hex>`, databases named `plane_tpl_<hex>`,
`plane_clone_<hex>`, `astralplane_db_<hex>` or `astralplane_qualification_<hex>`, roles named
`astralplane_role_<hex>` (each `<hex>` is 32 lowercase hex characters), and temporary blob roots
supplied by pytest. Tests that need only an already-migrated catalog run against a
`CREATE DATABASE ... TEMPLATE` copy of a database that the real migration runner builds once per session
(`tests/conftest.py`). Upgrade suites that start many tests from one predecessor revision copy a
predecessor database that the historical runner builds once per module and still run the real
upgrade edges in every test; every from-empty and pre-split case replays from its own empty or
loaded starting state. It verifies predecessor damage rejection before any repair, direct upgrade,
repeat-upgrade no-op behavior, whole-transaction rollback on an injected second-edge failure, staged
blob failure, forward retry, and the joint restore procedure below. An unset URL skips this suite
and is not qualification evidence.

## Acceptance

Before reopening admission, verify every representative owner-scoped conversation, workspace,
artifact, preference, scheduled operation, voice-session record, remote-metadata record, and audit
chain. Re-run the migration to prove it is already current, verify every required reconciliation
marker, and check database/blob referential integrity. A partial physical purge remains visibly
incomplete and retryable.

## Failure and rollback

- A failed baseline transaction rolls back to an empty schema and leaves admission closed.
- A failure after the baseline commit but before registry completion leaves a recoverable
  `066.001` predecessor; the next closed-admission startup resumes forward.
- A failed registry migration transaction rolls back and leaves admission closed.
- A failed reconciliation writes a durable failure marker and leaves admission closed; repair the
  named hook and use the explicit retry procedure.
- Expired hidden materializations are soft-deleted and scheduled for typed prefix purge before
  ordinary purge reconciliation. Global readiness stays degraded while any expired pending row,
  pending/failed tombstone, or manual-review record remains.
- Migrated legacy exact-key tombstones cannot be sent to the hardened automatic deleter. An
  authorized operator must quiesce the predecessor publisher, inspect the exact persisted owner and
  locator digest, complete the documented external storage procedure, retain its evidence artifact,
  and call `resolve_legacy_exact_for_administration(...)` with that evidence SHA-256. Only exact
  replay succeeds; a changed evidence digest conflicts.
- Prefer forward repair after a schema advance. An older composition may be reselected only when
  its recorded Plane compatibility explicitly reads the observed schema.
- Never run inferred down-SQL. Restore the verified PostgreSQL and blob snapshots together only
  under the operator recovery procedure.
- Keep the prior compatible composition and verified backup until the acceptance and rollback
  retention window closes.

### Joint restore procedure

Use this procedure only after admission and every writer are quiescent and the backup from the same
maintenance window has been verified:

1. Record the failed composition, observed schema marker/digest, and database error category without
   copying connection material or raw user content into evidence.
2. Preserve the failed database and blob roots for diagnosis when capacity and policy allow; do not
   mutate them into an assumed predecessor shape.
3. Restore the verified PostgreSQL backup into the selected recovery database and restore every
   paired blob/workspace root into new explicit destinations without following links.
4. Verify restored revision, table/record checks, blob membership, byte counts, and SHA-256 before
   selecting a composition. Supply the complete root-name set and relative-key/size/digest
   manifest to `qualify_joint_restore`; it rejects wrong roots, incomplete or extra blobs,
   non-current metadata, and a damaged catalog before any session mutation. A restored `066.001`
   state has no Plane digest and must match its pinned predecessor catalog. Restored `067.001`, `074.001`, `074.002`, `074.003`, `074.004`, `075.001`, `079.001`, `088.001`, `088.002`, `088.003`, `088.004`, `088.005`, `088.006`, `088.007`, `088.008`, `089.001`, `089.002`, `089.003`, `089.004`, `089.005`, `089.006`, `089.007`, `089.008`, `089.009`, `089.010`, `089.011`, `089.012`, `089.013`, `089.014`, `089.015`, `089.016`, `089.017`, `089.018`, `089.019`, `089.020`, `089.021`
   states must carry their exact pinned historical registry digests and match their pinned
   predecessor catalogs. A restored `089.022` state must carry the exact current registry
   digest and pass the current structural verifier.
5. Select a composition whose Plane metadata declares the restored revision readable. Prefer the
   current composition and forward-retry the full guarded registry when possible.
6. Re-run migration and required product reconciliation under closed admission, then call
   `recover_joint_restore` to repeat qualification and retire every restored session. Retain its
   opaque evidence, repeat until retirement returns zero after quiescence, rerun the acceptance
   checks, and only then let the host decide whether traffic may reopen. Retain the previous and
   restored copies until the rollback window closes.

A qualified restore returns only after the exact current database/catalog and every supplied blob
root agree. Repeating it on the same quiescent unit returns equivalent evidence and a zero
retirement count. The full public contract, denial reasons, and forward-repair procedure are in
[joint restore qualification](joint-restore-qualification.md).

If a restore, migration, reconciliation, or session-retirement transaction loses its connection or
commit acknowledgement, keep admission closed and treat the outcome as unknown. Re-read the durable
state or repeat only the declared idempotent step; never infer success, rollback, or a safe
connection from the exception. The exact failure and connection-custody rules are in
[connection loss and uncertain outcomes](connection-loss-and-uncertain-outcomes.md).

The restore procedure ends with a closed-admission candidate, not a writable state. The neutral
preconditions and evidence required before the host may open admission, including host-owned
election/fencing, complete root verification, restored-session retirement, commit ambiguity, and
forward-repair limits, are defined in
[recovery and writable readiness](recovery-and-writable-readiness.md). The combined
`recover_joint_restore` facade does not replace that host decision.

The read-only [endpoint failover readiness](endpoint-failover-readiness.md) facade qualifies
the selected external PostgreSQL primary and its paired blob roots and returns fence evidence
for reconnect and stale-writer refusal. It does not elect, promote, or fence a host, and a
writable report does not replace restored-session retirement or reconciliation.

## Later blob-root relocation

A later relocation is a separate operation: quiesce writers, reject symlink/reparse traversal,
copy to a new explicit root, verify count/size/SHA-256 against durable metadata, switch the root
atomically, and retain the prior copy for rollback. Source checkout or submodule updates must never
delete, overwrite, or discover runtime data implicitly.

## Schema-neutral repository cutover

Identity, agent, draft-authoring, and tool-policy repository extraction is schema-neutral. Those
repositories operate on tables already present in the `066.001` compatibility baseline and do not
add a migration edge. Their rollback is therefore a composition rollback: restore the prior
Plane/Deep code pins without changing or deleting durable rows. See `identity-agent-state.md`.

Credential, offline-grant, and share-grant extraction is likewise schema-neutral. It reads and
writes the existing `user_credentials`, `machine_credential`, `user_offline_grant`, and
`share_grant` tables without altering their shape. Roll back only the Plane/Deep composition;
never decrypt, rewrite, or delete ciphertext or immutable snapshots as part of code rollback. See
`credentials-and-grants.md`.

Chat-step, conversation-file, and saved-component public repository extraction also reuses only
`066.001` baseline structures. The saved-component factory deliberately points to the existing
publication-aware Canvas repository rather than duplicating its SQL. Rollback is composition-only;
leave `chat_steps`, `messages.step_count`, `chat_files`, and `saved_components` untouched. See
`conversation-extended-state.md`.


### `088.002` session identities after a joint restore

A backup restores session and grant identities together, so row incarnation alone cannot prove
that a restored session was not retired after the backup. Keep admission, scheduling, assignments
and all other writers closed. Recovery must deliberately retire every restored web session before
reopening IAM-backed work, require fresh login and new consent, and preserve outstanding assignment
usage, issued permits, uncertain effects and reconciliation records. Do not rebind a restored grant
or operation to a new session. No startup migration or normal process restart performs retirement.

The explicit public `astralplane.retire_restored_sessions` recovery entry retires the entire
restored `web_session` table in one transaction. It requires `database_url`, `expected_database`,
`expected_schema`, `expected_schema_revision` and `expected_migration_digest` as keyword inputs;
there is no implicit application environment or target fallback. Supply connection material only
through the embedding operator's private configuration, never command-line arguments or receipts.
The current revision and digest must match the installed Plane code. The connected database and
first selected schema must match the explicit target. Before reading application metadata, a
qualified-builtin bootstrap gives `pg_catalog` implicit precedence, the existing migration advisory
lock coordinates maintenance, and relation locks retain `schema_meta` and `web_session` while the
complete current catalog is verified. Unknown/predecessor schemas, corrupted metadata and catalog
namesakes are refused; no initializer, migration or product reconciliation hook is called.

The entry then calls `SessionRepository.retire_all_for_recovery(transaction)`, which locks and deletes
every session and verifies emptiness without decoding credentials or joining an owner inventory.
Expired, resumed, inactive-owner and unknown-owner rows cannot be missed. It returns a frozen
`RestoredSessionRetirement(retired_sessions: int)` only after commit and private pool closure. It
does not return or log owners, session identities, credentials or ciphertext. Session retirement
does not alter grants, assignments, receipts, audit, blobs or accounting liabilities. Existing
grants keep their old binding and require fresh consent; no new session can revive that binding.

SQL retains the existing transaction-local lock/statement upper bounds of 100/1000 milliseconds;
stricter configured nonzero limits remain effective. The private pool bounds checkout to one second
and connection establishment to five seconds. These are separate limits, not a total network or
physical worker deadline. Contention, timeout and interruption roll back uncommitted retirement.
`SessionRetirementError` is data-free; any failure after a possible commit remains unconfirmed.
Keep admission closed, inspect or repeat explicitly, and never compensate by restoring credentials.
A completed repeat returns zero. A later restore must run retirement again even if its snapshot
contains old completion markers. No existing reconciliation marker can skip this operation.

This API neither verifies the joint backup nor proves that writers are stopped. It never changes
admission, invokes IAM, clears another process's memory or reopens services. The operator must verify
those external preconditions, retain a private recovery record, discard every application process
and cache, and require fresh institutional sign-in before reopening. The embedding Deep operator
tool, private-database end-to-end qualification and final staging remain separate requirements;
this API is not an independently authorized operational procedure. Ordinary startup/restart and
owner retirement keep their existing behavior. No operator SQL outside the Plane recovery facade
is authorized by this procedure.

For recovery from a failed upgrade, preserve the failed database, keep writers closed, and restore
the verified pre-upgrade PostgreSQL and paired durable-root snapshot as one unit. Do not drop the
new column/constraints or alter revision markers to simulate a downgrade. Forward retry is allowed
only after the original predecessor/current catalog and exact composition have passed verification.


### `088.003` issuing metadata upgrade and recovery

The guarded `088.002 -> 088.003` edge requires the exact predecessor registry digest
`22d086ef60c5e73f124f3569268c4b48138871b1614ac538ab7f685a1b313afc` and full catalog.
It adds nullable `web_session.issuing_issuer` / `issuing_client_id` columns and a
paired, bounded-text constraint. Existing rows receive null/null; no issuer or
client is guessed from ciphertext, configuration or JWT claims. Session
incarnations, credentials, lifetimes, grants, assignments, permits, usage, audit and
blob records are not rewritten. Same-name columns or a weakened predecessor catalog
are refused before the edge; partial failure rolls back columns and revision stamp.
Repeated initialization verifies the complete current catalog and performs no
metadata conversion. The immutable historical migration statements/digests remain
pinned, including `088.002`.

Before applying the edge, quiesce admission and all writers, retain the exact old
composition, and verify the joint database/durable-root backup described above.
Run the matched new composition's normal migration registry, then verify repeat
startup and host compatibility before reopening. This storage prerequisite neither
establishes new native credentials nor changes legacy execution eligibility.

On failure, keep writers closed and preserve the failed state. Use verified forward
retry or restore the paired pre-upgrade database/durable roots and matching
composition. Do not drop fields or alter schema markers to simulate downgrade.
After any joint restore, the explicit complete restored-session retirement procedure
still applies: restart all process caches and obtain fresh institutional authority
before reopening; preserve grants/operations and issued liabilities for refusal and
settlement. The recovery API verifies the matching current revision/digest and will
not operate against an old schema using new metadata assumptions.

The same 088.003 edge adds nullable `auth_revocation_queue.issuing_issuer` and a
conditional issuer/client structural constraint. Existing queue records retain
their ciphertext, client (including null), timestamps, attempt count, and IDs;
issuer remains unknown. This migration does not dispatch, resolve, or relabel
queued revocations.

### `089.013` Projects and recovery

The guarded `astralplane-089-projects` edge requires the exact `089.012` predecessor
registry digest `58ba65b80bf3c1019c6c110d59cc618813668f7916f1621d851e58199738dc9e`
and catalog digest `e506006358926677d9dba3261e0a17b4c56815132f747953048f6fcca1e1e38d`.
It adds `chat_project`, `chat_project_membership`, their ordering indexes, and the
`chats_owner_identity` unique constraint on existing `(user_id,id)` values.
Existing chat, message, publication, observation, preferences, and blob contents remain unchanged.
The current catalog digest is `cb564e2d95f3b58fe9861227bde05e16d56e9852f89a2abdcc825e156b08cbc9`;
the current registry digest is `dde302967743cf8f6f5d55e82f8355ef405823dfee638909c3a2f11b85f5cb27`.
Package version is `0.1.11`; read-compatible floor remains `066.001`.

Projects use composite owner/project identities and integer millisecond timestamps supplied
by the host. Create replay compares original name and position, even after rename, and does
not compare a newly sampled timestamp. Rename and delete require the current revision.
Deletion refuses a nonempty project and retains a private tombstone, hidden from public
reads, which permanently refuses create replay for that owner/project identity. Chat deletion
cascades membership cleanup. Attachment validates owned live project and owned chat under
locks, replays the same attachment, and refuses reassignment to another project.
Composite foreign keys enforce same-owner endpoints. Plane stores no folder paths, bookmarks,
file access permissions, or product authorization in these tables.

Project listing uses immutable `(position,project_id)` keysets. Chat listings use chat-ID
keysets and include empty and archived chats. Owner cleanup listing binds its cursor to an
exclusive creation cutoff; legacy null creation times count as zero. Returned chat rows are
neutral inventory metadata, not a substitute for host publication authorization or message
history. Cursors are typed and scope-validated; the host must authenticate any serialized cursor.

`tests/integration/test_projects_upgrade.py` covers populated predecessor preservation,
repeat/no-op, old-binary refusal, interruption rollback/retry, namesake collisions, and damaged
constraints/indexes. `tests/test_projects.py` covers caller rollback, owner denials, replay,
revision and clock fences, complete pagination, FK cleanup, and independent-connection races.
The established pre-split restore suite also traverses this edge.

Keep admission closed after a failed migration; its transaction rolls back new tables and the
chat constraint together, leaving the predecessor revision/digest intact. Correct the cause
and retry the guarded runner. For post-upgrade defects, prefer reviewed forward repair. If
rollback is unavoidable, restore PostgreSQL and configured blob roots from the same qualified
checkpoint with admission closed, verify the restored catalog and registry, retire restored
sessions, and reconcile post-backup creates, attachments, and deletions before reopening.
A restored pre-deletion row must not reactivate stale host folder grants: reconcile host access
state before admission. Never erase project tombstones, rewrite markers, infer down-SQL, or
run an older binary against the new revision. A database restore cannot restore local Mac grants.

### `089.014` Harness admission capture and recovery

The guarded `astralplane-089-harness-captures` edge accepts only the exact `089.013`
predecessor catalog and registry digest. It adds `operation_harness_capture`, with a
bounded private capture, immutable update/delete guard and operation-parent retention
cascade. A second trigger preserves the initial optional assignment capture across every
assignment update. Existing operations and assignments remain unselected; migration never
infers selection from a provider URL, backfills a capture, or changes an existing binding.

Owner selection uses the existing preferences document through `preferences.harness` and
its separate strict CAS revision. Admission locks the owner state before preferences and
copies the selected identity only for genuinely new work. Operation submission and assignment
receipt replay return the original capture before current selection assertions. Persistent
episodes can inherit only their exact owned assignment capture. A post-claim harness binding
must match the captured identity and the existing owner/execution fence. Host configuration
and qualification checks remain mandatory; Plane does not declare deployments qualified.

The current binary can migrate declared predecessor revisions. An older binary cannot run
against `089.014`: its guarded registry rejects the unknown revision and catalog. Exact Deep
composition pin checks must also pass before admission opens. Do not downgrade a composition
that could ignore captured work; quiesce admission and use a verified joint restore of the
matching database and blob state, or forward repair while preserving captures. Never drop
capture rows or rewrite assignment capture to recover availability. Normal parent operation
retention purges captures by cascade; assignment retention deletes the owning record normally.

Representative PostgreSQL tests populate a prior catalog, preserve retained rows, repeat the
migration, interrupt and recover the edge transaction, reject foreign namesakes and damaged
triggers, and prove old-reader denial. Capture tests cover owner isolation, CAS concurrency,
original replay after rollback/restart, immutable storage and parent retention.

A generic `parent_operation_id` grants no harness inheritance. The distinct public
`submit_retry` facade creates a transaction-bound private proof only for the host's
server retry path. It compares the full detached expected parent and exact capture,
requires current same-owner RETRYABLE state and retention, preserves kind/admission class/
normalized identity and exact retry child identity, then locks that parent through child
admission. Existing child replay returns its original capture before new parent assertions.
The host retains its existing finite retry limits and every independent authority/config gate;
no JSON field or guessed namespace/key can supply the private proof to ordinary submit.

The host may call `preferences.harness.lock_owner` followed by
`work_admission.has_admission_replay` inside the same caller-owned transaction. Inspection
uses normal request validation and the identical submission/idempotency locks retained through
ordinary submit. It returns only a replay predicate, including original refusals or input
collisions; it confers no owner, policy or execution authority. On a genuine new admission the
host checks the exact current selection/config before normal class/slot admission locks. On
replay ordinary submit alone decides the original accepted/refused/conflicting outcome; current
rollout or configuration must not reinterpret it. Authentication and current-caller checks remain
mandatory in either path.

### `089.015` Audit conversation index and recovery

The guarded `astralplane-089-audit-conversation-index` edge accepts only the exact `089.014`
predecessor catalog and registry digest. It adds one partial btree index,
`idx_audit_user_conversation_recorded ON audit_events (actor_user_id, conversation_id,
recorded_at DESC, event_id DESC) WHERE conversation_id IS NOT NULL`. No audit row, chain
sequence, digest, key identity or retention anchor is read for rewrite or changed. The index
supports owner-scoped `audit.list_page(conversation_id=...)` reads in the existing descending
cursor order. The same index serves the owner-scoped
`audit.list_conversation_range` read and the chain-ordered `audit.load_conversation_segment`
proof read, which add no schema of their own.

The edge runs inside the registry transaction like every other edge. A plain `CREATE INDEX`
holds a write-blocking lock on `audit_events` while it builds, so audit appends wait for the
build. Plan the upgrade with the normal startup migration window, when the host has not yet
opened admission. On a 2,000,000-row local table the build took about 2.3 seconds. An interrupted
edge rolls back completely and the next normal runner applies it again. A predecessor that
already has a same-name index, with any definition, fails catalog attestation before the first
write. Never drop or rename such an object to force the edge through; investigate its origin
first. A current `089.022` database missing or changing the index fails current verification.

The current binary can migrate every declared predecessor revision. An older binary cannot run
against `089.015`: its guarded registry rejects the unknown revision and catalog. Exact Deep
composition pin checks must also pass before admission opens. To recover availability, forward
repair under the current binary, or use a verified joint restore of the matching database and
blob state. Never drop the index under an `089.015` marker, rewrite revision or digest markers,
or run ad-hoc downgrade SQL. The current catalog digest is
`cb564e2d95f3b58fe9861227bde05e16d56e9852f89a2abdcc825e156b08cbc9`; the current registry digest is
`dde302967743cf8f6f5d55e82f8355ef405823dfee638909c3a2f11b85f5cb27`.

Representative PostgreSQL tests populate an `089.014` catalog with a mixed-owner audit chain and
retained rows from earlier slices, then upgrade it. They check that the rows are preserved and
the conversation page works, that a repeated run does nothing, that an interrupted edge rolls
back and recovers, that same-name predecessor indexes are refused, that a dropped current index
is refused, and that an old reader is denied.

### `089.016` Chat follow-up queue and recovery

The guarded `astralplane-089-chat-queue` edge accepts only the exact `089.015`
predecessor catalog and registry digest. It adds `chat_queue_item`, an owner- and
chat-scoped queued follow-up row with a bounded text body, a per-item revision fence,
a chat-local ordering position, and `ON DELETE CASCADE` retention through its parent
chat. Deleting a chat retires its queued items in the same transaction; no queue row
outlives its conversation, and no queue row stores tokens, credentials, approval
state, or PHI beyond the owner-authored text the host already stores for messages.

Queue mechanics stay neutral: enqueue is idempotent by caller-supplied item identity
and serialized per chat under an advisory lock, every text update, deletion, and full
permutation reorder carries the owner predicate and the expected revision in the same
statement, and `claim_next` is a single atomic statement that hands the head item to
exactly one turn dispatcher. A dispatcher that loses the head race receives a typed
conflict and retries; it never observes a partially reordered queue because the
ordering constraint defers only inside the reorder statement's own transaction.

The current binary can migrate declared predecessor revisions. An older binary cannot
run against `089.016`: its guarded registry rejects the unknown revision and catalog.
Exact Deep composition pin checks must also pass before admission opens. Never delete
queue rows or rewrite revisions to recover availability; quiesce admission and use a
verified joint restore of the matching database and blob state, or forward repair,
which re-runs this additive edge from the attested `089.015` catalog. Queue rows are
recreated only by their owner re-enqueueing the follow-up.

Representative PostgreSQL tests populate a prior catalog, preserve retained rows,
repeat the migration, interrupt and recover the edge transaction, reject foreign
namesakes and damaged current structure, and prove old-reader denial. Queue tests
cover owner isolation for list, edit, reorder, and claim; stale-revision conflicts
that change nothing; full-permutation reorder refusals; concurrent claim handoff to
exactly one dispatcher; and caller-rollback composition.

### `089.017` Conversation branches and recovery

The guarded `astralplane-089-conversation-branches` edge accepts only the exact `089.016`
predecessor catalog and registry digest. It adds `messages.parent_message_id`, the
per-conversation `chat_branch_head` with a compare-and-set version and optional
workspace-snapshot pointer, and fork lineage columns on `chats`. The backfill chains each
chat's visible messages in the exact `list_visible` order, so migrated chats become one
linear branch whose head is the last visible message; staged, aborted, and deleted chat
rows are never made visible and visible order never changes. Rows are never rewritten
except for the new parent pointer, and the original exchange stays reconstructible from
commit positions alone.

Branch and fork rows carry stable parent identifiers; fork creation copies references,
never task grants, approvals, bypass state, or pending cards, and every read stays
owner-scoped in the SQL predicate. Moving a branch head is one compare-and-set statement:
a stale version is refused with a typed conflict and the caller reloads the head record
and retries. Messages are appended with an explicit visible parent from the same
conversation; a parent from another owner or conversation is refused as not found.
A branch-head move accepts a head message and a workspace-snapshot pointer only from the
same conversation and owner and refuses any other, or a missing snapshot, as not found
before the head changes. The repository enforces that snapshot scope; the `089.017` catalog
constrains the pointer only to an existing snapshot.

The current binary can migrate declared predecessor revisions. An older binary cannot run
against `089.017`: its guarded registry rejects the unknown revision and catalog. Exact Deep
composition pin checks must also pass before admission opens. Do not downgrade a composition
that already serves branch reads; quiesce admission and use a verified joint restore of the
matching database and blob state, or forward repair while preserving parent links and branch
heads. Never delete parent links, zero branch-head versions, or infer lineage from message
order at read time to recover availability. Normal chat deletion cascades messages, branch
heads, and fork lineage references; deleting a source chat or message nulls the surviving
fork's lineage columns without touching its own rows.

Representative PostgreSQL tests populate long chats, chats with committed, staged, and
aborted publications, deleted chats, and multiple owners, then prove identical visible
output, visible-order parent backfill, one branch head per chat, repeat safety, whole-edge
rollback after an injected interruption with full recovery, foreign namesake and damaged
catalog refusal, and old-reader denial. Repository tests cover owner-scoped branch reads,
sibling counts, compare-and-set head moves including a concurrent single-winner race,
fork lineage replay, and typed cross-owner refusals.

### `089.018` Mac computer-use bookkeeping and recovery

The guarded `astralplane-089-computer-use` edge accepts only the exact `089.017`
predecessor catalog and registry digest. It adds `computer_session`,
`computer_task_grant`, `computer_host_effect_intent`, and `computer_approval_fence`
with their owner-scoped ordering indexes and lifecycle constraints. Existing
operations, assignments, captures, and every earlier record remain unchanged; the
edge installs empty bookkeeping tables and never backfills sessions, grants,
intents, or fences from any other store.

Sessions carry host id, host incarnation, bounded state (`active`, `paused`,
`ended`), and a bounded pause reason; transitions are revision-fenced
compare-and-set writes scoped to the calling owner in the same statement. Task
grants carry a SHA-256 binding digest, expiry, hard cap, use count, and revocation;
consumption is a fenced increment that refuses exhausted, expired, or revoked
grants with a typed error so the caller can fail closed. Host effect intents bind
the remote operation id, a bounded verb, and an argument digest; states move one
way only from `pending` to `committed` or `unknown`, then to `acknowledged`, and an
`unknown` intent never returns to `pending`. Bulk startup recovery marks an owner's
pending intents unknown in one bounded statement and returns the records. Approval
fences are single-use: `open` becomes `consumed` or `released` exactly once.

Records hold only digests and bounded identifiers; screenshots, file contents,
command output, and clipboard text have no column and no path into Plane. Another
owner cannot read, acknowledge, revoke, consume, or delete a record because every
statement filters on the owner. Retention is explicit: ended sessions and expired
or revoked grants are deleted in bounded batches ordered by their end timestamps,
and no purge tombstone is required because these rows never gate admission.

The current binary can migrate declared predecessor revisions. An older binary
cannot run against `089.018`: its guarded registry rejects the unknown revision and
catalog. Exact Deep composition pin checks must also pass before admission opens.
Do not downgrade a composition that could lose effect bookkeeping; quiesce
admission and use a verified joint restore of the matching database and blob state,
or forward repair while preserving the bookkeeping tables. Never drop intent rows
or rewrite revision markers to recover availability: after a crash, list open
intents per owner, mark them unknown in bulk, and let Deep decide the recovery
policy for the uncertain host effects. A database restore cannot restore live Mac
sessions; the host must re-establish them.

Representative PostgreSQL tests populate a prior catalog, preserve retained rows,
repeat the migration, interrupt and recover the edge transaction, reject foreign
namesakes and damaged indexes, and prove old-reader denial. Repository tests cover
owner isolation, monotonic intent transitions under concurrency, revision fences,
expiry and hard-cap refusal, single-use fences, retention bounds, and rollback
safety. The live check restarts a sandbox server mid-task only after the Deep work
lands; until then this remains host-side acceptance, not Plane qualification.

### `089.019` Verification credential storage and recovery

The guarded `astralplane-089-verification-credentials` edge accepts only the exact
`089.018` predecessor catalog and registry digest. It adds `verification_credential` and
`verification_challenge_consumption`, holding only owner-scoped public key material (SEC1 or
SPKI), device and algorithm labels, state timestamps, and opaque challenge digests. No private
key, token, or biometric sample is ever stored or accepted. A state-shape constraint keeps
`revoked` rows paired with a revocation timestamp; the per-owner uniqueness constraint makes
each challenge digest consumable exactly once, and an owner index bounds lookups and pruning.

Enrollment refuses an owner above the bounded active-credential count under a per-owner
advisory lock. Revocation is a record-version compare-and-set carrying the owner predicate in
the same statement; a revoked credential can never verify again. Verification looks up the
credential with a locked owner-scoped read, so a lookup concurrent with a revocation
transaction blocks, then observes the committed revoked state instead of returning an active
record. Consumed challenges support a bounded retention prune that deletes only the calling
owner's rows consumed before an explicit cutoff, with a bounded delete count.

The current binary can migrate declared predecessor revisions. An older binary cannot run
against `089.019`: its guarded registry rejects the unknown revision and catalog. Exact Deep
composition pin checks must also pass before admission opens. Do not downgrade a composition
that could accept a revoked credential; quiesce admission and use a verified joint restore of
the matching database and blob state, or forward repair while preserving revocations. Never
delete revocation rows, rewrite consumed challenges, or reuse a consumed digest to recover
availability; enroll a new credential instead. Normal retention pruning deletes only consumed
challenge rows; credential rows are owner records and are removed only by explicit host policy
through its own administration paths.

Representative PostgreSQL tests populate a prior catalog, preserve retained rows, repeat the
migration, interrupt and recover the edge transaction, and prove old-reader denial. Behavior
tests cover owner isolation, bounded enrollment, revocation CAS, replay, exactly-once
challenge consumption, bounded pruning, and the revocation-lookup race on real PostgreSQL.

### `089.020` Chat read receipts and recovery

The guarded `astralplane-089-chat-read-receipts` edge accepts only the exact `089.019`
predecessor catalog and registry digest. It adds `chat_read_receipt`, one owner- and
chat-scoped row holding the chat's last-seen commit position and a monotonic revision,
with `ON DELETE CASCADE` retention through its parent chat. Deleting a chat retires its
receipt in the same transaction; a re-created chat id starts with no receipt, and no
receipt stores message content, tokens, credentials, approval state, or PHI. Receipts
are owner-isolated non-audit metadata: every read and write carries the owner predicate,
the monotonic advance and revision fence share one statement, and each accepted write
returns the new revision so the host can resolve conflicts between the owner's devices
deterministically.

The current binary can migrate declared predecessor revisions. An older binary cannot
run against `089.020`: its guarded registry rejects the unknown revision and catalog.
Exact Deep composition pin checks must also pass before admission opens. Never delete
receipt rows or rewrite revisions to recover availability; quiesce admission and use a
verified joint restore of the matching database and blob state, or forward repair,
which re-runs this additive edge from the attested `089.019` catalog. Receipts are
recreated only by their owner marking the chat seen again.

Representative PostgreSQL tests populate a prior catalog, preserve retained rows,
repeat the migration, interrupt and recover the edge transaction, reject foreign
namesakes and damaged current structure, and prove old-reader denial. Receipt tests
cover owner isolation for reads and marks, monotonic no-op regression, stale-revision
conflicts that change nothing, the batched read that includes chats without receipts,
cascade retirement with non-inheriting re-creation, and caller-rollback composition.

### `089.021` Mesh event store and recovery

The guarded `astralplane-089-mesh-event-store` edge accepts only the exact `089.020`
predecessor catalog and registry digest. It adds `event_mesh`, `event_feed`, `event_entry`,
`event_fork_evidence`, `event_body`, `event_roster_entry`, `event_checkpoint` and
`event_checkpoint_countersignature`, and a nullable unique `event_id` column on `chats` and
`messages`. Existing rows are not rewritten and every new column starts empty. All new
tables carry the owner in their primary key and cascade from `event_mesh`, so removing a
mesh removes its events, bodies, roster entries and checkpoints together.

Feeds hold a head sequence and head hash that advance only by one under a row lock. An
append whose sequence skips ahead, whose previous hash does not match the head, or whose id
is not the hash of its signed header is refused. A repeat of a stored event is a no-op. A
different event at a stored position is kept as fork evidence next to the stored one, the
feed is marked frozen, and later new events for that feed are refused. Evidence is never
deleted by the repository. Bodies are addressed by the hash of their sealed bytes and may be
absent. Derived chat and message rows are upserted by event id under an advisory lock, and
a row already linked to another event is refused.

The current binary can migrate declared predecessor revisions. An older binary cannot run
against `089.021`: its guarded registry rejects the unknown revision and catalog. Exact Deep
composition pin checks must also pass before admission opens. Never delete feed, evidence or
checkpoint rows to recover availability, and never clear a frozen state by hand. A frozen feed
means its device must be re-enrolled by the owner. Use a verified joint restore of the matching
database and blob state, or forward repair, which re-runs this additive edge from the attested
`089.020` catalog.

Representative PostgreSQL tests populate a prior catalog, preserve retained rows, repeat the
migration, interrupt and recover the edge transaction, reject same-name tables and columns and
damaged current structure, and prove old-reader denial. Repository tests cover chain checks,
idempotent replay, fork evidence and freezing, concurrent competing appends, clock-ordered
scope reads, absent bodies, roster ordering, checkpoint comparison with feed heads, derived
row upserts, owner isolation on every read and write, and caller-rollback composition.

### `089.022` Run leases, goals and the learning ledger and recovery

The guarded `astralplane-089-run-and-learning-ledger` edge accepts only the exact `089.021`
predecessor catalog and registry digest. It adds `run_lease_entry`, `run_goal_transition` and
`learning_ledger_entry`, each keyed by owner and mesh and cascading from `event_mesh`. No
existing row is read or rewritten and all three tables start empty.

Each table is a history. A lease entry carries an epoch, a kind (take, transfer, takeover or
state), the executor feed, a run state and the event that produced it. The repository appends only
the next epoch, refuses a second take, a handoff to the current executor, a state change by another
executor and any entry after the run is completed, failed or cancelled. A goal transition carries
the next revision, the goal text, a state, round and stall counts and its event. Rounds never
decrease, the text never changes and nothing follows an achieved or abandoned goal. A ledger entry
is a proposal, an adoption or a reversal with a content hash, a scope and one or more justifying
event ids. A partial unique index allows one adoption and one reversal per proposal, and an
adoption must name a review of `automatic` or `owner`. Every entry is idempotent by its own event
id, and the same id with different content is refused. Repositories never update or delete these rows.

The current binary can migrate declared predecessor revisions. An older binary cannot run
against `089.022`: its guarded registry rejects the unknown revision and catalog. Exact Deep
composition pin checks must also pass before admission opens. Never delete or edit history rows
to recover availability or to undo an adoption. A reversal is a new entry. Use a verified joint
restore of the matching database and blob state, or forward repair, which re-runs this additive
edge from the attested `089.021` catalog.

Representative PostgreSQL tests populate a prior catalog, preserve retained rows, repeat the
migration, interrupt and recover the edge transaction, reject same-name tables and damaged
current structure, and prove old-reader denial. Repository tests cover epoch and revision
contiguity, handoff and takeover rules, finished runs and goals, the proposal, adoption and
reversal order, idempotent replay, owner isolation and caller-rollback composition.
