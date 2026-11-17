# Owner guidance storage (`088.005`)

Guidance is owner-controlled context, never source evidence, execution authority,
consent, a provider credential, or a permission grant. The host owns current human
authentication, privacy/content validation, encryption, policy, and transactional
audit. Plane owns the typed rows, revision/receipt checks, locks, and invalidation.
The legacy automatic personalization memory API remains separate and unchanged.

## Transactions and reads

The caller takes shared owner domain 79 and any current-session guards first.
Guidance mutations reacquire owner 79, lock affected assignments in sorted ID order,
then their actions in sorted ID order, and finally the guidance head. No session,
owner-0, or external call may follow those resource locks. Configuration/policy and
host audit may follow, with the final caller guard in that same bounded transaction.
The host must use normal SQL/transaction time bounds; a preparation is not a
capability that can be reused in another transaction.

`preferences.skills.lock_owner` and
`preferences.personalization.lock_explicit_note_owner` take owner 79 and verify the
active owner state without locking guidance heads. Use them once in a final owner
read transaction to serialize a page with corrections/retirement. They establish
consistency only, not authentication. Ordinary `Query` get/list methods do not
silently acquire those locks. A note's expiry must still be checked at delivery;
when reading multiple notes, a final current read of the earliest finite expiry
bounds the whole selection by the database clock.

Resource writes use a savepoint and preserve all earlier host transaction state.
If an audit or final caller check fails, abort the outer transaction. In particular,
a caught failure in final application must not commit a preceding successful audit.
All returned mutation DTOs are provisional until the outer commit succeeds.

## Skills and controlled legacy materialization

`SkillCommand` supports create, replace, and delete with exact owner, UUID4 resource
and command IDs, and expected revision. `prepare_change` reads/locks and recognizes
accepted commands before CAS; `apply_change` independently rechecks and uses its
captured immutable command. Each successful change appends one immutable snapshot.
The revision stores the metadata receipt, so an identical retry returns the original
receipt and current head, with `revision=None`: it cannot replay old instructions
after a later edit or deletion. Reusing the command ID with different data fails.
The maximum live revision reserves its successor for deletion.

An owner has at most 20 nondeleted heads. Slugs and nonempty aliases remain unique
while disabled. Reusing a deleted slug or alias requires a new UUID, so a selected
old reference can never revive. History is immutable but not current eligibility.
The closed definition bounds are name 60, instructions 4000, alias 24, and at most
eight applicability IDs; the host owns their semantic validation.

`materialize_legacy_skills` accepts a complete immutable tuple of at most 20 typed
entries. Each includes the proposed new UUID, slug, parsed definition, exact UTF-8
Markdown bytes (at most 32768), format `deep_owner_markdown_v1`, and original integer
update time in seconds. The initial revision retains those raw bytes and their
SHA-256. There is no synthetic earlier value history.

The raw directory digest covers the owner, version/kind domain, and filename plus
raw SHA-256 pairs sorted by **filename**. A separate parsed-entry digest covers the
interpretation and original times. Proposed UUIDs do not change either retry
identity. The immutable cutover marker exists even for an empty directory; matching
retry returns the original mappings, not today's revisions. A conflicting manifest,
interpretation, foreign ID, alias, or existing catalog refuses the complete batch.

The host must converge every application file writer before cutover, capture a
bounded stable directory, and recheck its exact manifest under the owner transaction
before materialization commits. SQL cannot lock an out-of-band file editor. Retained
Markdown is recovery input only after the marker; later file differences must be
reported as recovery conflicts, never silently adopted into SQL reads or writes.
No filesystem scan or materialization occurs during schema startup.

## Current encrypted notes and erasure

`ExplicitNoteRecord` stores only the current bounded ciphertext and authenticated
metadata supplied by the host: owner, UUID4 note, exact revision, version, category,
enabled flag, creation/update times, and optional expiry. Live revisions stop at
`2^53-2`; the final `2^53-1` remains available to Forget or expire. Timestamps use
exact nonnegative integer milliseconds up to `2^53-1`, with creation <= update <
expiry when present. Successive revisions may share one millisecond. Ciphertext is
opaque bytes (1..16384); Plane cannot assert decryption or plaintext authenticity.

`prepare_explicit_note` observes the current row under owner/assignment/head locks.
`put_explicit_note` freezes both preparation and record, checks the exact previous
revision/value, preserves creation time, and requires successor update time to equal
that original database observation. It checks current expiry again before writing.
It overwrites current ciphertext; it creates no value history, prior-value digest,
or plaintext receipt. A lost acknowledgement followed by a newer correction remains
an honest CAS conflict rather than reapplying an old value.

`prepare_explicit_note_retirement` reads the exact live row (including expired rows)
or minimal tombstone under the same locks. Its `replayed` flag lets a host avoid a
duplicate audit for concurrent identical Forget/expiry. The host then calls the
existing `forget_explicit_note` or `expire_explicit_note`, which independently repeats
preparation. Identical replay requires the original expected revision and reason.
A tombstone contains only owner, note ID, successor revision, deletion time, and
`forgotten`/`expired`; category, creation/expiry data, and ciphertext become NULL.
Retirement is allowed after owner retirement, but does not authorize a user request.

Expiry is unavailable immediately to current read/selection, even before physical
row retirement. `page_expired_explicit_notes` captures a database cutoff and returns
bounded keyset pages ordered by expiry/owner/note. Retire with `skip_locked=True` to
skip a busy owner's advisory lock and continue to later owners; wrap at the captured
end. Keep cursor/cutoff private to that drainer instance. No purge is performed at
startup. Host maintenance must authenticate itself, bound waits, audit actual new
erasures in the same transaction, and not count a replay as a second erasure.

This is logical current-row erasure, not a claim to erase PostgreSQL MVCC/WAL,
replicas, or historical backups. Backup retention and joint restore procedures must
account for post-backup Forget/expiry and matching encryption keys before reopening
reads. An old backup must not silently become proof that a forgotten value is live.

## Selected references and invalidation

`assignments.bind_guidance_references` binds the initial exact immutable tuple,
including an empty tuple, under owner/assignment CAS before any claim, action, or
plan. It supports at most 20 skills and eight notes. A durable selection header
prevents empty-to-nonempty replacement. `assert_guidance_current` is a factual read,
not session/execution authority. Consumers must use the ordinary execution guards
as well. This slice does not implement adoption during a new instruction revision.

Replacing, disabling, deleting, forgetting, or expiring a selected resource locks
all indexed unfinished dependants and invalidates their exact references. Supported
work is paused, its claim/control fence advances, pending/running task generations
advance, and scheduled wake/retry times clear without resetting retry history.
Existing authorization, event, approval, budget and reconciliation holds remain.
Unstarted reservations/proposals are invalidated through the existing ledger path;
started/uncertain/opaque liabilities and authentic settlement remain intact. Opaque
future assignment envelopes are not rewritten, but their reference is invalidated.

Terminal assignments leave the active reference index; retained accounting/results
are not overwritten. Exact old selected revisions stay stale after later resource
changes. Resume must not silently adopt a newer head: explicit operation revision
and host integration remain required by T036.
