Written for: the reviewer of the AstralPlane pull request.

# Schema 089.001 — TypeSafe credentials and data-sharing consent

**Merge order: second**, after AstralPrimitives 0.4.0 and before
AstralProjection and AstralDeep.

## What this adds

Guarded migration `088.008 → 089.001` creating two owner-scoped tables:

- `user_typesafe_credential` — the key as ciphertext, a 12-character
  fingerprint of the plaintext, and the last verification outcome;
- `user_data_sharing_acknowledgment` — the notice version, and the first and
  latest acknowledgment times.

With them: `EncryptedTypeSafeCredentialRepository`,
`DataSharingAcknowledgmentRepository`, catalog registration, structure and
registry digests, verifier checksums, and the
`PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS` entry.

## The two things worth reviewing closely

**There is no system-scope counterpart, and that is the design.** A
deployment-wide TypeSafe key would route every user's traffic through one
operator credential without anyone noticing. The table is owner-keyed only,
and AstralDeep refuses to start in a production posture if `TYPESAFE_API_KEY`,
`TYPESAFE_BASE_URL` or `TYPESAFE_DEFAULT_MODEL` is set in its environment.

**Plane never sees plaintext.** The key is encrypted with the **existing**
`CREDENTIAL_ENCRYPTION_KEY`, so one rotation covers both this and the LLM
credential store. A row that cannot be decrypted after a rotation is discarded
and audited; that user simply has no TypeSafe key until they save one again.

Neither table has a foreign key into `user_llm_config`. Clearing an LLM
configuration does not remove a TypeSafe key, and clearing a TypeSafe key does
not re-gate a user.

## Rollback

Drops both tables and restores the `088.008` marker; the rollback count moves
38 → 39. Documented in `docs/migration-and-recovery.md`. Exercised against a
real schema, including that `user_llm_config` is left intact.

## Verification

- `uv run --group ci python -m pytest -q` — 2502 passed, 3 pre-existing
  baseline failures, **0 new**
- Against a live PostgreSQL at 089.001:
  `tests/repositories/test_assignments_postgres.py` (103),
  `tests/repositories/test_typesafe_credential.py` (33, of which 15 run
  against the real schema), `tests/integration/test_catalog_caller_rollback.py`
  (40) — all passed

`tests/repositories/test_typesafe_credential.py` covers table presence at
089.001, the **absence** of any `system_typesafe_credential`, owner isolation,
a re-save replacing a rejected key, stale-outcome rejection, per-owner outcome
isolation, `last_verified_at` advancing only on `valid`, the live CHECK
constraints, and acknowledgment upsert preserving `first_acknowledged_at`
across a version bump.

## A deliberate deviation

`provenance/transformations.json` is **not** modified, though the task list
named it. That file is not a change log: it is the extraction ledger from the
074 Deep-to-Plane split, bound by `sourceManifestSha256` to a one-time source
manifest, and every entry names a Deep source blob that was absorbed into
Plane. Feature 089 absorbs no Deep source — both tables are new — so there is
no source blob to cite, and a synthetic entry would put an unverifiable fact
into a provenance record (Constitution XIII). The migration is documented in
`docs/migration-and-recovery.md` instead.

## Owner exceptions in force

- **E1: CI is ignored.** Every check was run locally and recorded in
  `specs/089-typesafe-a8p-integration/verification.md`. No workflow file is
  modified.
- **E4: staging is a local candidate stack.** No deployment is authorised.

## Known divergences

None in this repository.

## Outstanding

Nothing. T057 -- the rehearsal of a **populated** `088.008 -> 089.001` upgrade
on the candidate stack with the synthetic dataset, a repeat start and the
rollback procedure -- was completed on 2026-09-17 and is recorded in
verification.md 8.9.4: the rollback restores `088.008` and drops both tables,
the upgrade applies exactly `astralplane-089-typesafe-credentials`, the repeat
start reports `already_current=True` with zero steps, and `users` and
`user_llm_config` are byte-identical end to end.

One follow-up is owed to this repository rather than outstanding against this
PR: restoring `chrome_typesafe_save` to the durable credential path needs a
**fenced TypeSafe commit** here. Deep currently routes that action through the
ordinary chrome dispatch instead, because the durable executor it was pointed at
could only perform an LLM config set -- see the AstralDeep PR and
verification.md 7e.2.
