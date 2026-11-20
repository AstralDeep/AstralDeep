# LETS authority contract evidence

The feature-074 authority test family exercises the public repository on an isolated PostgreSQL
schema that holds the `067.001` outbox table and the authority DDL of the `074.001` edge. No later
edge alters the authority tables, so the tested shape is unchanged in the current `089.022` schema,
whose verifier attests it on every startup. The exact T140 requirements map to these live nodes:

- migration repeatability: `test_074_001_authority_ddl_is_repeat_safe_on_real_postgresql`;
- active-binding uniqueness and owner isolation:
  `test_partial_binding_uniqueness_and_owner_isolation_use_public_repository` plus the concurrent
  one-winner insert proof;
- request-fingerprint conflict:
  `test_lifecycle_replay_requires_the_same_request_fingerprint`;
- receipt-claim replay and strictly advancing sequence:
  `test_receipt_claim_replay_is_idempotent_and_equal_sequence_is_rejected`;
- caller-owned rollback:
  `test_claim_and_outbox_failure_roll_back_to_the_savepoint`.

The remaining authority unit and PostgreSQL tests cover bounded identities, provisioning closure,
owner-partitioned skip-locked recovery, protected-effect fencing, watermark/anchor structure,
corrupt-row visibility, and deterministic public exports. No test substitutes source-token matching
for the database uniqueness, concurrency, or rollback claims.
