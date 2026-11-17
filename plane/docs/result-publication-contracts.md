# Owner Save of a completed research result

The bounded canvas Save facade reuses `persistent_assignment_action`,
`conversation_commit`, and `saved_components`/`workspace_layout`. No migration,
worker claim, execution permit, generated-code publication, or second visibility
authority is introduced. Attachment/download publication remains outside this
facade.

`ResultPublicationProposal`, `ResultPublicationContent`,
`ResultPublicationPreparation`, and `ResultPublicationReceipt` are exported from
`repositories.assignments` and defined in `result_publication_models`.
Content consists of exact `PublicationRebaseComponent` and
`PublicationRebaseLayout` tuples. `result_publication_stage_digest` in
`repositories.result_publications` binds the complete ordered content, including
component row IDs, identities/types/titles/positions/payloads and layout trees.
It excludes database timestamps. The reviewed target component has a separate
payload digest. The stage admits at most 10,000 components and 10,000 layouts,
256 KiB per structured payload and 8 MiB in total. No staged messages are allowed.

1. `AssignmentRepository.put_result_publication_proposal(transaction, *,
   owner_id, assignment_id, expected_instruction_revision,
   expected_control_epoch, expected_state_version, proposal, content,
   expected_selected, authority, caller_valid_until)` creates a closed
   `result_publication` version-1 action and returns `AssignmentActionRecord`.
   The stable caller-provided UUID4 action ID protects proposal replay. Only
   bounded metadata/digests enter the action; no result text or private expansion
   is copied there. Existing action/history limits remain in force.
2. `prepare_result_publication(transaction, *, owner_id, assignment_id,
   action_id, decision, expected_state_version, authority=None,
   caller_valid_until=None)` is receipt-first, read/lock only. An accepted exact
   receipt is returned before stale CAS, original-session, or current-guidance
   checks. The host still guards the current owner request. Preparation is not a
   capability and cannot replace final validation.
3. `commit_result_publication` takes the same arguments plus `content`. It
   independently prepares, creates a previously absent publication and all its
   reviewed children, verifies their digests, then atomically advances the exact
   chat head and consumes the decision into one immutable receipt. It finally
   checks the original session and the exact selection against one final database
   clock cutoff. Only explicit `approve` decisions are supported in this slice.

Every miss requires an exact `SessionExecutionObservation` of the operation's
original incarnation and an aware current-caller cutoff, copied before waits.
Neither is a worker permit. The cutoff includes the original operation authority
expiry/deadline, fresh observation lifetime, proposal expiry, and caller lifetime.
The last guidance clock observation checks that bound and note expiries together,
using current returned assignment counters after completion retired its claim.

The operation must be a supported completed retained interactive research
operation. The result reference must name its authentic settled model action,
whose transient source reference resolves to an authentic settled current-revision
read action matching the operation's source request. Selected reference metadata
and key identities must agree. Deep separately verifies the keyed model/source
receipts, reconstructs the exact bounded public result, and applies current
permissions and content/egress policy. Plane does not infer that proof from
caller-supplied bytes or a model's output. No current model-provider configuration
is needed merely to read or Save an already completed public excerpt.

The host owns current authentication and the audit in the same outer transaction:
prepare, verify public content/current policy, audit, final commit, final current
caller guard, commit outer transaction. A final failure must abort the outer audit
as well. After that last caller SQL wait, the host must assert the exact selected
input again with the returned current counters and the combined original/caller/
proposal cutoff, so one final DB clock checks selected-note expiry and authority
lifetime together. Only pure local binding checks may follow. The nested savepoint removes every partial publication/action mutation
even when an infrastructure or final-guard error is caught. There are no callbacks
or external calls in the Plane boundary.

Locks follow owner 79, original session, assignment, sorted action ledger, then
publication/chat. Existing chat writers use both chat-to-publication and reverse
order, so publication/chat and replay child reads use NOWAIT savepoints. A fresh
publication UUID cannot be row-locked before it exists: its immediate unique/FK
write region uses a savepoint-local 1 ms lock timeout (the smallest positive
PostgreSQL resolution). Success explicitly restores the prior timeout; rollback
restores it automatically. Other host statement/deadline bounds are untouched.
Contention returns data-free `assignment_publication_busy`; it grants no retry
permission or new action. Relevant existing constraints are immediate, so no
deferred constraint wait escapes that region.

The facade never adopts an existing stage. Legacy canvas editing can change a
row while only its parent stage is locked; that behavior remains unchanged.
Fresh children and the visible pointer are unobservable until the same outer
transaction commits. Receipt replay locks and verifies the historical published
content and detects later content tampering, without depending on today's chat
head or resurrecting old selected values. These are bounded locked reads under
READ COMMITTED, not a global snapshot of owner state.

The reserved action subtype has no worker attempts and carries one truthful
owner-transaction receipt. Generic worker creation refuses it. Conservative
invalidation and purge recognize only the exact closed subtype; unknown fields,
future versions, malformed receipts, and inherited issued/uncertain liabilities
remain holds. Save never clears usage, schedules a wake, changes the terminal
result, or fabricates settlement for a previously issued effect.

## Bounded destination review

`read_result_publication_destination(tx, *, owner_id, conversation_id,
expected_render_revision, expected_publication_id, maximum_bytes=1048576)`
returns the complete `ResultPublicationContent` for that exact current head,
including an empty destination. It grants no original-session or publication
authority. The caller must supply the existing owner/session fences and retain
the same outer transaction through final checks.

The read locks owner 79, expected publication and chat, then sorted canvas and
layout row IDs. All row locks use NOWAIT savepoints. Immediate chat/publication
FKs exclude concurrent new children, including the legacy NULL publication
scope; public child replacements cannot change scope and conflict with the
held child locks. The reader checks at most 1,000 entries and the byte total of
every full serialized stored row before fetching payloads, then verifies the
exact row identities. It refuses oversized, incomplete or busy destinations
without truncation. The maximum accepted byte bound is 8 MiB; the host's review
and output limit is 1 MiB. Final proposal-content copying also stops as soon as
its aggregate bound is exceeded, before touching later component/layout entries.

These locks preserve the current bounded read, not a historical immutable copy
after the transaction ends. Approval still reconstructs and compares the exact
reviewed complete-stage digest and current destination head.
