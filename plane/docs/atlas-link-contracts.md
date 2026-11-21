# Atlas link contracts

Schema `089.008` adds bounded same-owner Atlas links behind
`create_atlas_link_repository()` and the `atlas_links` catalog member. The store owns only link
membership, per-source link revisions, exact replacement receipts, and one-hop backlink reads.
Page content, project authorization, rendering, link policy, PHI checks, and audit remain with the
embedding host.

## Endpoints and replacement

A link is identified by `(owner_id, source_page_id, target_page_id)`. Both page identities must
already exist as live pages under the same owner. A source page may link to pages in the same or a
different project, but never to another owner and never to itself. Deleted or unknown endpoints are
refused with the same owner-scoped not-found result, so a cross-owner page identity is not
disclosed.

`AtlasLinkSetCommand` carries the owner, canonical command UUID, source page identity,
`expected_page_revision`, `expected_link_revision`, at most 16 unique target identities, and a
caller timestamp. Targets are canonicalized into sorted order for a stable request digest. An empty
target tuple clears the page's outgoing links. The first replacement expects link revision `0`;
every successful replacement advances the source page's link revision by exactly one.

The command validates the source page, its live state, and its current revision under a row lock.
It validates every target as a live page of the same owner and locks those rows for the
transaction. The exact owner/target set must match the command. A stale page revision, stale link
revision, regressed link clock, tombstoned source, unknown endpoint, duplicate target, self link,
or more than 16 targets fails before any link write.

## Replay and concurrency

Replaying the same owner/command with the same request digest returns the stored receipt and the
current live link records with `replayed=True`; it performs no new write. Reusing a command UUID
with changed semantics or another page fails with `RepositoryConflictError`. Distinct commands
with the same expected page and link revisions serialize through the shared owner advisory lock;
only one can advance the link revision, and the others conflict. Page creation, page deletion, and
link replacement use the same owner lock, so a link edit cannot race a page identity change.

Link rows are immutable while present. Replacement deletes and reinserts the source page's complete
outgoing set in the caller transaction. PostgreSQL triggers refuse direct link-row updates, any
head update that is not exactly one revision forward, and command-receipt updates or deletes.

## Deletion and tombstones

Page deletion is terminal. In the same transaction that writes the page tombstone, Plane removes
every link whose source or target is that page. It deletes the deleted page's link head but advances
the link revision of every remaining source page that pointed at the deleted target. That prevents a
writer holding the old link revision from silently recreating or replacing a set affected by the
deletion. The original link command receipts remain immutable historical evidence.

After deletion, `list_links()` and `list_backlinks()` on the tombstone fail with
`RepositoryNotFoundError`. Replaying an earlier link command returns its stored receipt with no
current links rather than restoring membership. A source page cannot be recreated at the same page
identity, so a deleted source head is never revived.

## Bounded reads and backlinks

`list_links()` reads the bounded outgoing set for one live owner-scoped page.
`list_backlinks()` reads the one-hop incoming set for one live owner-scoped page. Both join the
source and target page rows with `deleted_at IS NULL` in SQL and put the owner predicate in the
statement. Neither method recursively traverses the graph.

Each method returns at most 100 records ordered by `(linked_at DESC, source_page_id DESC,
target_page_id DESC)` and returns `AtlasLinkBatch`. The cursor binds the owner, root page,
direction, timestamp, source identity, and target identity; a cursor from another owner, page, or
direction is refused before SQL. The query fetches one extra row to decide whether a continuation
exists, so an exact-capacity result has no false continuation. Keyset iteration is not a snapshot:
a later replacement can change or remove links between pages.

All methods accept an explicit caller-owned transaction or query executor. They never commit,
roll back, borrow a connection, retain a cursor, or mutate a returned record.

## Migration and recovery

The `089.007 -> 089.008` edge attests the exact predecessor registry digest
`fe60420679846e7ea06ca5da3cef250cde5ad75a4ca47b6eb15739893a8ebd09` and complete catalog before
creating only the three link tables, their indexes, and their guard triggers. It does not rewrite
any page, command receipt, or existing row. A repeat run verifies the complete current catalog and
is a no-op. Partial DDL or a stamp failure rolls the edge back with its registry metadata; a
same-name foreign object or damaged predecessor fails before adoption.

Follow the closed-admission backup, guarded upgrade, populated repeat, corruption, and rollback
checks in [migration and recovery](migration-and-recovery.md). Once the edge commits, keep all three
link tables and the exact `089.008` revision/digest intact. Prefer a reviewed forward repair. If the
prior composition must be restored, restore the verified PostgreSQL backup and every paired durable
root together under closed admission, then retire restored sessions and reconcile uncertain effects
before reopening. Never drop the tables, rewrite revision markers, or execute ad-hoc downgrade SQL.
A restored backup reflects only the link set present at backup time; it cannot reconstruct a later
replacement.