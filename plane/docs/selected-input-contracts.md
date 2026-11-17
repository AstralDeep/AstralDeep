# Selected input storage (088.006)

`AssignmentRepository.bind_selected_input` binds one initial typed
`SelectedInputEnvelope` under the existing owner/session → assignment order.
It accepts at most 20 exact skill revisions, eight exact note revisions, and one
owned declarative agent revision. Agent IDs retain their text domain; revision
IDs are UUID4. The envelope retains a version, expansion version, named binding
key ID and opaque combined binding. It contains no expanded instruction, note
ciphertext, source observation, credential, or provider configuration.

The embedding host authenticates the named key and combined binding and enforces
current human/original execution authority, permissions, privacy and input
limits. Storage metadata never supplies those permissions. The stable binding
can be constructed before fetching a source; the actual source/configuration
and full runtime request require their separate host proof.

`get_selected_input` returns the actual immutable metadata. A missing assignment
is an owner-scoped not-found error. `None` means no selection header or references
of either kind exist. An older 088.005 header returns `AssignmentSelectedInput`
with `envelope=None`; even an empty older header is an explicit stored selection,
not absence. Existing selections cannot be upgraded, re-expanded, or rebound.
Exact new-envelope binding replay performs no mutation and still checks current
resources. Accepted Work-command replay belongs to the host's earlier receipt
boundary and need not call binding again.

`assert_selected_input_current` compares the exact captured snapshot, instruction
revision, control epoch and state version, then checks selected resource state.
The older `assert_guidance_current` retains its existing call compatibility and validates the same
stored shape/currentness without comparing a caller's opaque binding. Both check
the selected agent's current active definition pointer, owner, declarative kinds
and definition digest, even if a mutation did not update the reverse index.
Both finish selected-note validation with the database clock after row reads.
Both accept an optional `authority_valid_until` aware datetime, detached into
stdlib UTC before any wait. When supplied, one final database timestamp checks
this original cutoff and every note expiry together, even for absent or
agent-only selections. Malformed cutoffs raise `RepositoryValidationError`;
elapsed cutoffs raise the existing `assignment_guidance_changed` conflict.
The cutoff is an additional lifetime bound and never establishes authority.
They grant no claim, admission slot, delegation or dispatch permit. A lifecycle
completion caller must use its returned current counters after retiring the
execution fence. The host invokes the final assertion after all other waiting
work and aborts its transaction if it fails. Passive retained result reads do not
reconstruct forgotten private values merely because an old operation selected
them.

Declarative authoring holds owner 79 and owner-state, all active indexed
assignments in ID order, then all their actions in ID order, then legacy agent
owner 0 and agent heads. This owner-wide prelock also covers existing callers
that acquire `lock_declarative_owner` before choosing the command. Public
assignment admission bounds active/paused records to 25 per execution profile.
Terminal records leave the active index; their immutable references remain as
history. Existing executable/remote mutations never gain owner 79. Callers must
not take agent owner 0/head locks and then enter declarative authoring or Work.

Revise, activation of a different definition, archive and delete invalidate active
references, retire claims and unstarted proposals, and pause affected work.
Known issued/uncertain obligations retain authentic settlement and charges.
Invalidation and the definition mutation/receipt share the same savepoint.
Accepted declaration receipts remain read-only replays and do not invalidate a
second time. If the host's audit or final caller/policy check fails, the enclosing
transaction must abort; an accepted preparation is never permission to commit
later outside that transaction. An ordinary resume cannot adopt new definitions.

The additive migration adds a nullable immutable envelope to existing headers
and a separate owned declarative revision/digest reverse index. Old rows stay
unmodified; no historical expansion is inferred. The guarded registry validates
the exact 088.005 predecessor before applying the edge and the current catalog
afterward. Interrupted DDL rolls back transactionally; repeat the guarded runner.
After a committed upgrade, recover through the governed backup/restore procedure
with current owner/session and Forget/expiry reconciliation. Do not downgrade
the schema marker or drop the selected-reference metadata to make stale Work run.
