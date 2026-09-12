# Issued session identity for durable work

Status: reviewed clarification of runtime sections 2–4 and FR-010/FR-026.
Storage, operation guards, authentication retention and atomic consent have passed
their scoped local qualification. Host continuation, complete restore tooling,
protected staging and release qualification remain pending. No new route, runner
or release has been activated. This contract extends the request-refresh
prerequisite; observing today's row is insufficient to identify yesterday's
issued session when SID, timestamps and encrypted credentials can be repeated.

## Storage and issuance

Plane adds `web_session.incarnation_id`: a non-null UUID with a database-generated
UUID4 default and a unique constraint. It is not secret or derived from SID,
timestamps, credentials or ciphertext. A new row receives a new value. Refresh,
resume, reads and process restart preserve it. Delete/recreate with all other
fields identical still changes it.

`SessionRecord` carries the value. An input record with no incarnation may be used
only at the existing `put` issuance boundary; returned records and execution
records always have a valid canonical UUID4. An identical issuance retry against
an existing row returns that row's incarnation. A supplied incarnation may only
replay that exact existing record; it must never create a row or restore an old
identity into an absent/recreated SID. Concurrent issuance retains one winner and
the existing identity-replay conflict behavior. No caller-supplied database UUID
is accepted as a new issuance.

Deep caches and returns the actual stored record from `create`, rather than the
pre-insert input. Authentication attachment also consumes that returned record;
it must not keep an input-only session in its own cache. Incarnation stays inside server state and encrypted grant
references; ordinary session responses, cookies and client protocols are unchanged.
Process-only fallback state cannot supply durable-work authority.

## Refresh, retirement and observations

Every session refresh CAS includes the immutable incarnation even when its
optional exact encrypted-state fence is absent. Existing generation, timestamp,
owner and ciphertext checks remain additional conditions. A held remote refresh
cannot write into a replacement session with reused timestamps and credentials.
The original creation time, interactive anchor and hard expiry are immutable
even when the optional encrypted-state fence is absent. Uncertain refresh may
retain a still-valid old access token only after a bounded exact-incarnation read
confirms the original issuance remains live; it cannot adopt replacement tokens.

Read-then-mutate paths for resume markers, expired/decrypt-failed cleanup and
logout deletion bind the original observed incarnation. The original value travels
through authentication caches and across awaited refresh, logout and resume work.
Mutation callers supply it explicitly; rereading today's row inside a helper is
not a replacement for that binding. A late HTTP 401 for retired session A must
not delete or revoke same-SID replacement B. A stale conditional delete returns
no deleted credential and cannot revoke the replacement's credential. Owner-wide
retirement deliberately covers all current sessions; expiry scans use their
current database predicates. Neither path deletes effect/accounting liabilities.

The typed `SessionCredentialFence` uses version 2 and requires the incarnation.
Version 1 fences are refused. The enclosing ephemeral observation keeps its
existing version and lifetime: exact owner, SID, incarnation and encrypted
generation, original database-clock start, at most 15 seconds, ordinary IAM,
and final transaction checks after lock waits. SQL caps remain transaction-local
and opt-in; they do not claim to terminate an awaiting Python worker physically.

An owner-scoped exact-incarnation lookup supports later worker refresh. It cannot
fall back to the owner's latest session or capture a different current row after
the original reference disappears.

## Stored operation version and committing boundaries

New one-shot operation records use outer version 2. Interactive authority uses
the closed `reference_kind=session_incarnation` and the selected canonical UUID4
as `reference_id`. This is metadata, not a bearer capability. Existing delegation,
framework and scheduled vocabulary remains closed and obtains no new execution
support from this change. Corresponding adapters must still supply their separately
qualified current-authority checks before they can be enabled.

Version 1 records remain decodable for safe owner inspection, cancellation and
authentic liability settlement. They cannot resume, wake into execution, claim,
prepare, reserve, issue a permit, incorporate output or publish. An accepted retry
returns its original version/binding; it does not upgrade the record, spend again
or replace authority from today's session. Unknown versions retain the existing
safe-inspection/refusal behavior. Decoding/settlement eligibility is separate from
execution eligibility: a version switch must not refuse an authentic old permit
before recording its mandatory charge or reconciling its uncertain outcome.
Older readers already treat outer version 2 as
unknown, avoiding a silent change to the understood version 1 vocabulary.

For a genuinely new interactive admission, `create_operation` requires the typed
current session observation and verifies owner plus selected incarnation while
locking owner then session before assignment/admission/action state. It rechecks
the same observation and original authority deadline after any later lock waits
and before committing the receipt. A matching existing receipt still requires
current authenticated access in Deep, but precedes new intent expansion and
admission checks as required by the existing retry contract. Admission refusal
rolls back the assignment, receipt and allowance together.

All guarded claim/action/result/publication boundaries compare the operation's
original incarnation to the supplied fresh observation, then independently verify
the exact current row. The operation's immutable authority expiry cannot exceed
the originally selected session hard expiry. Neither a newer JWT nor a new
same-SID row extends that bound. Authentic already-issued permits still settle
factual consumption once after retirement, while suppressing output, checkpoint
advance, wake and publication.

## Offline grants and compatibility

New consent stores the encrypted prefix `astral-offline-session/v2` with exactly
`session_id`, `incarnation_id`, `created_at`, and `interactive_anchor`. The existing
encryption and owner/grant/expiry checks remain. Exact current-session matching at
consent and every later refresh must include the incarnation.

Both schedule-chat consent and persistent-assignment consent must carry the
approving request's server-resolved session reference into grant capture. The
reference comes from ordinary authenticated dispatch, never a user/model field
or an owner-latest token lookup. Capture validates that exact owner, SID and
incarnation again. A bearer-only or other caller without an already qualified
selected-session reference receives the existing bounded authorization/re-consent
disposition; it cannot borrow another session belonging to the owner. Existing
accepted receipt replay precedes new grant capture and preserves its old binding.
A revision whose initial counters mismatch may only replay that exact accepted
receipt or return conflict. Future counters becoming current during an await must
never turn a request without new consent into a successful mutation.

Consent uses a distinct server-private `SessionConsentObservation`, never an
execution observation. It retains the original database-clock start, exact v2
credential fence and at most 15 seconds, also capped by the approving principal's
expiry and the session hard expiry. Ordinary cookie/Bearer IAM and CSRF checks
still run first. Preparation writes no grant and does not renew the observation.
The encrypted prepared reference is checked against its original selection and
the same application Plane runtime before use.

Grant insertion and its dependent schedule or assignment mutation share one
transaction with owner-before-session locking and SQL request bounds. Recheck
the original consent after dependent row/receipt/approval waits before commit.
Keep the original socket registration across awaits and recheck it around a
schedule transaction. A changed registration cannot create work for the new
connection. A failed or stale assignment mutation rolls back the candidate grant;
an accepted receipt returns its original grant without creating another one.
Never revoke a candidate grant as compensation for an unknown commit outcome:
the accepted operation may already depend on it. Audit successful durable consent
only after its dependent transaction returns a committed result.

Legacy raw-token and version 1 references cannot prove original issuance. Refuse
execution with the existing bounded re-consent disposition; do not convert them
by matching today's token, SID or timestamps, and do not rewrite the retained
grant silently. Preserve listing, revocation, audit and issued-effect accounting.
New consent creates a new reference through the ordinary approval path.

## Migration and recovery

Allocate a successor revision only after refreshing and checking all relevant
Plane local/remote revision declarations. Add one guarded edge without editing
the existing immutable `088.001` statements or digest. Attest the predecessor,
backfill each existing session once with a new UUID, enforce default/non-null/
uniqueness and verify the exact current catalog. Repeated startup preserves every
assigned UUID. Owner/content/ciphertext/usage records remain unchanged; never
bind historical operations or grants by joining their SIDs to current sessions.
Existing normal sessions remain usable after this forward upgrade.

A backup copies incarnations, so UUIDs alone cannot detect restoration of old
authority. Before reopening traffic after a joint restore, quiesce writers,
verify the paired database/blob state, retire all restored interactive sessions
through an explicit governed recovery operation, clear process session caches
and retain old grant/operation bindings unchanged. Require fresh institutional
sign-in and consent. Preserve and reconcile issued liabilities. This recovery
operation is never part of ordinary startup/migration and is not invoked against
the user's running application during implementation.

Rollback is qualified forward repair or a joint restore with closed admission;
old binaries cannot ignore the new schema. The signed cookie still binds SID,
not incarnation. This change protects stored work and grant references; it does
not claim to redesign cookie authentication or support deliberate SID reuse.

## Qualification

Use real PostgreSQL to cover initial/replayed/concurrent issuance, exact lookup,
refresh/resume/delete CAS, reused SID with identical time and ciphertext, and
replacement while refresh or authority locks are held. Prove old work refusal
at admission/claim/prepare/reserve/permit/result boundaries and new-bound work
success. Verify stale authentic permits settle once without usable content,
including version 1 uncertain-outcome reconciliation. Distinguish refused new
version 1 admission from authenticated exact receipt replay of accepted old work.
Exercise delayed refresh HTTP 401, logout and resume against replacement sessions
through the actual authentication callers, including caches and attachment.

Exercise legacy/foreign/malformed references, unchanged receipt replay, revoked
grants, new consent and legacy re-consent without unintended OAuth traffic.
Qualify populated/repeated upgrade, catalog corruption, no historical rebind,
and restore retirement under closed admission with preserved liabilities.
Retain ordinary session/auth/refresh/logout/grant compatibility suites, exact
Python 3.11 package installation, at least 90% changed Python coverage and
independent review before pinning. Synthetic repository tests do not replace
institutional IAM or final staging evidence.
