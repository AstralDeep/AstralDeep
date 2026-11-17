# Joint restore qualification and session retirement

## Status and scope

`astralplane.joint-restore/v1` qualifies one explicitly selected PostgreSQL database together
with every configured blob root as a single restored candidate. The facade verifies the exact
current schema revision, migration digest, catalog, root identities, and a caller-supplied
manifest of relative blob keys, sizes, and SHA-256 values. It then delegates restored-session
retirement to the existing commit-safe recovery transaction.

The qualification step is read-only. Retirement is a separate committed database transaction.
The two are not atomic with the blob filesystem, and a successful result is not a backup
certificate, an election, a writer fence, an admission decision, or proof that another process
has stopped writing. The host retains backup selection, inventory evidence, quiescence,
election/fencing, external-authority reconciliation, approval, and admission.

The facade is exported as `astralplane.qualify_joint_restore`,
`astralplane.recover_joint_restore`, `astralplane.RestoreBlobEntry`,
`astralplane.JointRestoreEvidence`, `astralplane.JointRestoreCompletion`, and
`astralplane.JointRestoreError`. It adds no database table, service port, second store, or
implicit root discovery.

## Recovery unit

The supported unit is one current Plane database plus the complete named set of attachment,
artifact, workspace, generated-knowledge, and other durable roots from the same recovery point.
The caller supplies:

- an explicit PostgreSQL `database_url`;
- `blob_roots`, a non-empty mapping from stable root names to explicit absolute paths;
- `blob_inventory`, a sequence of `RestoreBlobEntry(root, storage_key, size_bytes, sha256)` records;
- the exact `expected_database`, `expected_schema`, `expected_schema_revision`, and
  `expected_migration_digest`;
- optionally, the backup's `expected_blob_inventory_digest` and `expected_blob_roots`.

A root that is absent, relative, aliased, duplicated by filesystem identity, or crossed by a
symlink, junction, or reparse point is refused. Every regular file below each root must appear
exactly once in the manifest, and every manifest entry must exist with the exact byte count and
SHA-256. Unexpected files, missing files, truncated content, digest changes, unsafe keys, links,
special objects, and malformed internal control entries fail closed. The root-level
`.astralplane-owner-locks` registry is validated as Plane control state and is not counted as a
durable blob. Persistent staging replay fences named
`.astralplane-stage-<64 lowercase hexadecimal characters>.lock` are included in the manifest as
zero-byte entries. They are accepted only beneath a valid owner and parent key used by blob
staging; a root-level marker cannot be produced by that API. Links,
hard-linked markers, non-empty markers, and temporary staging files are refused. Normal blob
storage-key validation continues to reject all `.astralplane-*` components.

`expected_blob_roots` binds the configured root-name set to the backup evidence, including an
empty root. `expected_blob_inventory_digest` binds the manifest's contract version, root names,
and every entry to the digest recorded for the same recovery point. Omitting either field still
produces deterministic evidence, but the host must compare that evidence with its retained
backup record before opening admission.

## Evidence

`qualify_joint_restore(...)` returns a frozen `JointRestoreEvidence` only after the blobs and the
database fence agree. `recover_joint_restore(...)` performs the same qualification, then returns a
frozen `JointRestoreCompletion` carrying that evidence and the committed retirement count.

The evidence contains no database URL, credential, root path, storage key, blob content, or
session identity. Its stable fields are:

| Field | Meaning |
| --- | --- |
| `contract_version` | `astralplane.joint-restore/v1`. |
| `schema_revision`, `migration_digest` | Exact current Plane composition verified by the guarded catalog check. |
| `database_identity`, `schema_identity` | Opaque observations from the endpoint fence. |
| `blob_roots` | Sorted configured root names, not paths. |
| `blob_identity` | Aggregate opaque filesystem identity of the scanned root set. |
| `blob_inventory_digest` | Canonical digest of the verified manifest and root-name set. |
| `blob_count`, `blob_size_bytes` | Exact verified inventory file count and total bytes, including persistent staging replay fences. |
| `fence_digest` | Digest of the endpoint fence binding database, schema, roots, inventory, and composition. |
| `digest` | Digest binding the restore contract, fence, inventory, counts, and byte total. |

`to_dict()` adds `state: "qualified"` for an inspection or `state: "recovered"` plus
`retired_sessions` for a completed recovery. Repeated qualification of an unchanged candidate
produces the same evidence. After writers are independently quiesced, repeating
`recover_joint_restore(...)` on the same unit must return zero retired sessions while retaining
equivalent evidence. A later restore must run qualification and retirement again even when its
snapshot contains an earlier completion count.

## Recovery sequence

1. Close admission, stop every writer, and fence the failed node or old serving process. Plane
   cannot prove those conditions.
2. Restore the verified PostgreSQL snapshot and every paired root from the same recovery point
   into explicit recovery destinations. Do not mix snapshots or derive missing bytes from rows.
3. Build the complete manifest from the retained backup inventory and pass the exact current
   database, schema, revision, migration digest, expected root names, and expected inventory
   digest.
4. Call `qualify_joint_restore(...)`. Any input, root, blob, schema, digest, catalog, or fence
   mismatch leaves the candidate unqualified and does not mutate sessions.
5. Call `recover_joint_restore(...)` under the same closed admission. It repeats qualification
   and commits complete `web_session` retirement only after the exact current catalog and
   metadata are verified. Preserve its evidence with the backup, root, fencing, and approval
   records.
6. Repeat recovery after quiescence and require a zero retirement count. Discard every process
   and cache, require fresh authentication and consent, reconcile external authority, effects,
   deliveries, revocations, grants, and uncertain liabilities, then let the host decide whether
   admission may reopen.

Qualification and retirement do not alter audit events, grants, assignments, operation
receipts, issued permits, uncertain effects, spend, blob content, or accounting liabilities.
A restored snapshot still requires host-owned reconciliation of work that happened outside
PostgreSQL after the recovery point.

## Denials and failures

`JointRestoreError` is data-free. Its bounded metadata names a reason but never a path, DSN,
storage key, secret, or raw content.

| Code | Meaning |
| --- | --- |
| `joint_restore_input_incompatible` | Invalid DSN, target identifier, non-current revision/digest/layout, malformed roots, malformed inventory, duplicate entries, or mismatched expected root names. |
| `joint_restore_inventory_mismatch` | Expected inventory digest differs, root set differs, membership/size/digest differs, root identity changed, or the database fence disagrees with the scanned roots. |
| `joint_restore_database_unqualified` | The endpoint is unavailable, not primary, stale, incompatible, ambiguous, or the exact current catalog does not verify. |
| `joint_restore_unavailable` | An unexpected qualification failure occurred; the outcome is not usable evidence. |

Retirement failure remains `SessionRetirementError` with code
`session_retirement_unavailable`. Qualification never calls retirement after a denial.
A retirement exception, timeout, lost acknowledgement, or interruption does not prove rollback
and never returns completion evidence. Keep admission closed, re-read the durable state, or
repeat the whole explicit recovery. Only a completed transaction reports a count.

## Forward repair

Use guarded forward repair only from an exactly recognized state:

- Rebuild the same root set from the verified backup when membership, byte count, digest, or root
  identity differs. Never synthesize missing blobs from rows, delete unexplained files to force
  agreement, or replace the expected inventory digest merely to match an observed mix.
- Move the database through the guarded migration registry when it is a recognized predecessor.
  Do not rewrite `schema_meta`, run inferred down-SQL, adopt namesakes, or repair a damaged
  current catalog under the current marker.
- Re-run the same qualification and retirement after correcting the failed prerequisite. A
  changed expected digest or root-name set must come from revised, retained backup evidence and
  the host's approval, not from the failed candidate itself.
- If retirement is ambiguous, repeat `recover_joint_restore(...)` after quiescence until the
  committed result is observable and a later repeat returns zero.
- If the database, roots, catalog, or external effects cannot be reconciled, preserve all failed
  copies, keep admission closed, and restore the verified joint database/blob backup with the
  exact matching composition. Then reapply qualification, retirement, and every host gate.

A renamed root is accepted only when the caller explicitly supplies its new destination and the
complete manifest still matches. The facade records a new opaque root identity; it never infers
that a path rename preserves root identity or writable safety.

## Verification evidence

Focused deterministic cases:

- `tests/test_joint_restore.py::test_static_input_denial_precedes_blob_and_database_io` covers
  wrong schema, revision, digest, layout, inventory digest, and DSN input before I/O.
- `tests/test_joint_restore.py::test_incomplete_or_changed_blob_inventory_is_refused` covers
  missing, extra, truncated, and digest-mismatched blobs.
- `tests/test_joint_restore.py::test_expected_root_set_must_match_the_configured_roots` covers a
  wrong expected root set.
- `tests/test_joint_restore.py::test_nested_link_is_refused_without_following_it` and
  `tests/test_joint_restore.py::test_blob_link_and_internal_control_objects_are_checked` cover
  links and Plane control-state validation.
- `tests/test_joint_restore.py::test_qualified_evidence_is_deterministic_and_path_free` covers
  repeatable, opaque evidence.
- `tests/test_joint_restore.py::test_recovery_returns_evidence_and_repeat_is_explicit` covers the
  first committed retirement and a zero-count repeat.
- `tests/test_joint_restore.py::test_retirement_failure_propagates_without_completion_evidence`
  covers failed retirement propagation.

Real-PostgreSQL cases:

- `tests/repositories/test_session_restore_postgres.py::test_joint_restore_refuses_unqualified_unit_without_session_mutation`
  covers wrong database/schema/revision/digest, missing/extra/truncated/changed blobs, and wrong
  inventory digest without deleting a session.
- `tests/repositories/test_session_restore_postgres.py::test_restore_retirement_preserves_liabilities_and_authentic_settlement`
  proves repeated recovery preserves audit, grants, receipts, assignment usage, issued permits,
  uncertain effects, and configured blobs.
- `tests/repositories/test_session_restore_postgres.py::test_joint_restore_retirement_failure_returns_no_completion_evidence`
  proves an interrupted combined recovery leaves every restored session present.

Run the focused evidence against an isolated PostgreSQL database:

```text
ASTRALPLANE_TEST_POSTGRES_DSN='<isolated test database URL>' \
  python -m pytest -q tests/test_joint_restore.py \
  tests/repositories/test_session_restore_postgres.py
```

Docker-hosted platform acceptance, real backend dispatch, institutional staging, mesh
leadership, and external authority reconciliation remain separate host qualifications.
