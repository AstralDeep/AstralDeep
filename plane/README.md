# AstralPlane

AstralPlane is Astral's independent embedded durable-state library. It owns PostgreSQL and SQLite connection
and transaction mechanics, guarded schema evolution, owner-scoped repositories, audit-chain
storage, transactional outbox delivery, and explicit blob-purge recovery. AstralDeep installs the
package in-process; AstralPlane does not add a service port or a second database.

Personal nodes can select a persistent SQLite file through `create_sqlite_runtime(...)` or
the URL-selecting `create_runtime(...)`; institutional nodes keep `create_postgres_runtime(...)`.
See [SQLite composition, locking and recovery](docs/sqlite-backend.md) for the dialect-specific
registry pin, file requirements and single-writer contract.

## Contracts

- Python: 3.11 or newer
- Package: `astralplane`
- Contract: `astralplane.contract/v1`
- Current schema: `089.022`, read-compatible from `066.001`, with guarded upgrade entry points
  at `066.001`, `067.001`, `074.001`, `074.002`, `074.003`, `074.004`, `075.001`, `079.001`,
  `088.001`, `088.002`, `088.003`, `088.004`, `088.005`, `088.006`, `088.007`, `088.008`,
  `089.001`, `089.002`, `089.003`, `089.004`, `089.005`, and `089.006`
- Migration advisory lock: `(1095980114, 60001)`
- Reconciliation advisory lock: `(1095980114, 60002)`
- Endpoint readiness: `astralplane.endpoint-readiness/v1`, read-only fence evidence for one
  externally managed PostgreSQL primary and its paired blob roots
- Joint restore: `astralplane.joint-restore/v1`, exact database/catalog plus paired blob-root
  qualification and commit-safe restored-session retirement

`create_postgres_runtime(...)` owns psycopg2 driver-pool construction, bounded checkout, runtime
composition, and guarded startup. On a truly empty application schema it first installs the
schema-only `066.001` compatibility baseline under the migration advisory lock, then applies every
required edge of
`066.001 -> 067.001 -> 074.001 -> 074.002 -> 074.003 -> 074.004 -> 075.001 -> 079.001 -> 088.001 -> 088.002 -> 088.003 -> 088.004 -> 088.005 -> 088.006 -> 088.007 -> 088.008 -> 089.001 -> 089.002 -> 089.003 -> 089.004 -> 089.005 -> 089.006 -> 089.007 -> 089.008 -> 089.009 -> 089.010 -> 089.011 -> 089.012 -> 089.013 -> 089.014 -> 089.015 -> 089.016 -> 089.017 -> 089.018 -> 089.019 -> 089.020 -> 089.021 -> 089.022`
in one registry transaction. A pre-split `066.001` database has only its legacy revision marker.
Every later predecessor, from `067.001` through `089.006`, is accepted only when it carries its own
pinned historical migration-registry digest. Every supported predecessor is structurally attested
before the first migration write. A current `089.022` database must carry the exact current
registry digest and pass canonical catalog-structure verification over all Plane-owned tables,
sequences, functions, indexes, constraints, triggers, rules, policies, inheritance, and
owned-schema privileges. A same-name or unexpected object with changed behavior is rejected. A
non-empty partial or unrecognized schema is rejected rather than labeled current. The extracted
baseline contains only neutral schema and deterministic database mechanics; catalog cleanup, UI
seed content, filesystem discovery, and product policy remain explicit host reconciliation.

A fresh `TEMPLATE template0` database's exact PostgreSQL default `public` schema
(`pg_database_owner`, PUBLIC `USAGE`) is a qualified predecessor variant. Revision `074.004`
atomically transfers the selected schema to the migration user and revokes PUBLIC schema
privileges; any other predecessor owner or ACL shape remains a fail-closed mismatch.

Revision `079.001` adds persistent assignments with durable owner controls, source deduplication,
bounded task graphs, resource reservations, execution permits and immutable approval/effect
records. See [persistent assignment contracts](docs/persistent-assignment-contracts.md),
[result publication contracts](docs/result-publication-contracts.md) and
[migration and recovery](docs/migration-and-recovery.md).

The package contains no AstralDeep, AstralProjection, AstralPrimitives, LETS, API, UI, agent,
media, or transport implementation dependency. Product policy and authorization remain in
AstralDeep; callers pass neutral owner context and retain transaction ownership.

`create_repository_catalog()` returns the stable repository catalog. Its 59 members, in `RepositoryCatalog.as_mapping()` order, are
`assignments`, `agent_management`, `agents`, `artifacts`, `attachment_parsers`, `audit`, `audit_retention`, `authority`, `background_tasks`, `chat_queue`, `chat_read_receipts`, `chat_steps`, `derived_chat`, `event_log`, `event_roster`, `learning_ledger`, `runs`, `conversation_files`, `credentials`, `draft_agents`, `generated_agent_publications`, `encrypted_llm_config`, `encrypted_typesafe_credential`, `framework_credentials`, `history`, `harness_cleanup`, `identity`, `knowledge`, `maintenance`, `mesh_enrollment`, `verification_credentials`, `notifications`, `push_deliveries`, `projects`, `observations`, `offline_grants`, `outbox`, `preferences`, `personalization_graph`, `purge`, `quality_audit`, `remote`, `remote_operation_proposals`, `revocations`, `saved_components`, `scheduler`, `share_grants`, `stop_epochs`, `tool_policy_state`, `tracked_jobs`, `tutorials`, `voice`, `work_admission`, `workspaces`, `completion_wake`, `computer_use`, `capability_publications`, `atlas_pages`, and `atlas_links`.

`preferences.personalization.get_memory(query, owner_id=..., memory_id=...,
for_update=True)` reads and locks the current owner-scoped memory row until the caller's
transaction commits or rolls back. Callers must supply their open transaction when requesting
the lock; missing and foreign-owned rows return `None` without locking a foreign row. The
default `False` preserves the ordinary nonlocking read. Callers locking multiple sources must
take any guidance owner lock first, then memory rows in sorted identifier order, before
destination and audit writes. Memory updates, deletion, supersession, validity changes, and
recall writes serialize with the locked row through PostgreSQL.

The stable repository catalog includes four explicit stores for the first identity/agent
cutover slice:

- `identity`: detached Keycloak/OIDC subject observations; authentication and role policy remain
  in Deep.
- `agents`: first-party ownership/trust plus user-agent revisions, host sessions, runtime
  generations, and request fences.
- `draft_agents`: owner-scoped authoring, generation leases, transition idempotency, and immutable
  publication records.
- `tool_policy_state`: explicit scope rows, legacy and per-kind tool overrides, saved selections,
  and per-user agent opt-outs; permission decisions remain in Deep.

Use the matching `create_identity_repository()`, `create_agent_repository()`,
`create_draft_agent_repository()`, and `create_tool_policy_state_repository()` factories when a
composition does not need the full catalog. See `docs/identity-agent-state.md` for transaction and
owner-isolation rules.

The catalog also includes `mesh_enrollment` for neutral personal-mesh membership, enrollment, and
revocation state: owner-scoped mesh/member/public-identity/challenge/invitation records with
monotonic membership and revocation epochs, atomic single-use invitation
expiry/consumption/confirmation, and current-revision fences. Private device keys never reach
Plane; possession proof, IAM, QR/network handling, and admission policy remain in Deep. See
[mesh enrollment records](docs/mesh-enrollment-records.md).

Schema `089.003` adds durable owner stop epochs, bounded immutable peer receipts and
operation epoch bindings through `stop_epochs`. Admission and publication locks remain held
through the host's audit transaction. See [owner stop epochs](docs/owner-stop-epochs.md).

Schema `089.004` adds completion wake storage: owner-scoped `completion_subscription` rows
with live-state/terminal-coverage guards plus immutable `wake_receipt` rows. Accept and revoke
are fenced to owner/live rows with terminal coverage enforced, and acceptance locks the
subscription through the caller's transaction so it serializes with revocation and deletion.
See [migration and recovery](docs/migration-and-recovery.md#089004-completion-subscriptions-and-wake-receipts).

Schema `089.005` adds neutral notification records: explicitly typed per-channel/per-event
preferences, opaque ciphertext inbox entries with deduplication and retention, reversible
acknowledgment and deletion tombstones, and per-entry durable delivery identities. Plane stores
no plaintext notification content and never chooses a route, provider, recipient, or IAM decision.
See [notification records](docs/notification-records.md) and
[migration and recovery](docs/migration-and-recovery.md#089005-notification-records-and-recovery).

Schema `089.006` adds the bounded `atlas_pages` store: stable owner/project page identities,
host-sealed content and source metadata, exact create/delete receipts, owner-scoped reads and
keyset listings, and terminal tombstones that erase current head ciphertext. Content encryption,
authorization, and audit remain outside this slice. See
[Atlas page contracts](docs/atlas-page-contracts.md).

Schema `089.007` adds immutable `atlas_page_revision` snapshots and fenced edits. An edit inserts
the exact next revision before atomically advancing the page head, preserves bounded keyset
history, and rejects stale fences, invalid replay, foreign owners, and deleted pages.

Schema `089.008` adds bounded same-owner `atlas_links`: at most sixteen outgoing targets per
page, exact page/link revision fences, immutable command receipts, one-hop backlink keyset
traversal, and tombstone cleanup for either endpoint. Project traversal, rendering, content
policy, authorization, and audit remain outside Plane. See
[Atlas link contracts](docs/atlas-link-contracts.md).
Schema `089.009` adds `capability_publication` storage through the `capability_publications`
catalog member. An owner can publish one exact capability revision to one audience for a
finite lifetime, withdraw or expire it with a state-revision compare-and-set fence, and read
only live rows at a caller-supplied UTC instant. Encrypted audience metadata and its digest
stay opaque. Discovery, IAM, invocation, and A2A transport remain in Deep. See
[capability publication records](docs/capability-publication-records.md).
Schema `089.010` adds immutable operation harness-profile bindings. A typed profile/version/artifact
identity is written once under the operation owner and current execution fence; legacy operations
read as unbound, exact replay is idempotent, changed identities conflict, and stale fences cannot
overwrite. Runner selection, provider routing, profile rollout, and artifact availability remain in
the host. See [operation harness profile bindings](docs/harness-binding-contracts.md).
Schema `089.011` adds `push_deliveries`: owner-scoped opaque device destinations with encrypted
token ciphertext, monotonic token-version/revision fences, rotation, revocation and content-free
deletion tombstones, plus a bounded claimed-delivery outbox keyed to a live inbox entry. Claims
snapshot and re-check the destination token version, leases are bounded, expired claims are
recoverable, and exact-claim late acknowledgments cannot overwrite a newer claim. Provider calls,
token encryption, routing, IAM, and delivery policy remain in Deep. See
[push delivery records](docs/push-delivery-records.md).
Schema `089.012` adds the bounded `tool_observation` store: owner/conversation-scoped host-ciphertext
tool observations with capture-order identity, operation and source references, a caller-held seal,
SHA-256 content integrity, bounded 16 KiB pages, an absolute caller-supplied expiry within a 24-hour
retention ceiling, a 64 MiB per-conversation capacity, tombstoned purge/expiry cleanup, and
deterministic paged recall with end-of-source and integrity-failure states. Recall authorization,
audience, packing, compaction, and provider policy remain with the host. See
[observation recall contracts](docs/observation-recall-contracts.md).

`agents.reconcile_validation_policy_for_administration(...)` is the atomic, advisory-locked
startup surface for a Deep-supplied opaque product-policy revision. Exact marker replay is
write-free; a changed marker flags only live mismatched agents in the same caller transaction.

The next schema-neutral catalog slice exposes ciphertext and grant mechanics already present in
the `066.001` baseline:

- `credentials`: opaque user-agent credentials plus owner-bound remote-machine credentials, with
  explicit compare-and-set replacement and a bounded administrative re-encryption page.
- `offline_grants`: encrypted refresh-token records, token-free standing-grant lookup, and
  owner-scoped idempotent revocation.
- `share_grants`: immutable snapshot capabilities stored by digest, metadata-only owner listing,
  and an active-state-checked public open counter.

Use `create_credential_repository()`, `create_offline_grant_repository()`, and
`create_share_grant_repository()` for individual composition. Encryption, raw token handling,
Keycloak exchange, PHI policy, rendering, and audit decisions remain in AstralDeep. See
`docs/credentials-and-grants.md` for the owner, replay, and transaction contracts.

Conversation-adjacent durable state has three additional stable factories:

- `create_chat_step_repository()` for owner/turn-checked progress trails and terminal-state CAS;
- `create_conversation_file_repository()` for ordered opaque file-link metadata; and
- `create_saved_component_repository()` for the same publication-aware component implementation
  already used by Plane workspaces.

They are cataloged as `chat_steps`, `conversation_files`, and `saved_components`. Step redaction and
delivery, uploads, parsing, blob I/O, and canvas policy remain product-owned. See
`docs/conversation-extended-state.md`.

Attachment parser persistence and physical blob mechanics now have explicit composition surfaces:

- `create_attachment_parser_repository()` is cataloged as `attachment_parsers`. It exposes
  redacted global coverage separately from owner-scoped claim provenance, atomically deduplicates
  pending/live gaps, reclaims only failed/discarded gaps, and fences lifecycle changes by status
  plus `updated_at`.
- `create_streaming_blob_store(root=...)` adds pathless bounded readers, a narrowly scoped
  read-only parser lease, cross-process owner exclusion, and hidden staging reservations. It
  securely provisions only the configured root's missing suffix below the nearest existing,
  link-free absolute ancestor. Direct publication and deletion are deliberately absent.
- `create_attachment_materialization_coordinator(...)` is the only production creation composite:
  it commits a pending metadata intent, opens an unpublished staging session under the exact
  owner/lease row fence, and publishes bytes plus READY metadata in one short transaction.
- `create_durable_purge_executor(...)` consumes typed attachment-prefix/owner-namespace tombstones,
  performs capability-bound physical deletion on that same store, verifies absence, and records a
  version-fenced terminal result.

Parser generation/execution and administrator authorization remain in AstralDeep. See
`docs/attachment-parser-and-blob-composition.md` for ownership, retry, purge, and recovery rules.

The remaining knowledge, personalization-graph, and scheduler-extended baseline state is exposed
through `create_knowledge_repository()`, `create_personalization_graph_repository()`,
`create_background_task_repository()`, `create_maintenance_repository()`, and
`create_tracked_job_repository()`. `create_scheduler_repository()` owns scheduled definitions,
occurrences, runs, effects, and atomic chat publication while WorkAdmission remains separate. The
full catalog keys include `knowledge`, `personalization_graph`, `background_tasks`, `maintenance`,
`scheduler`, and `tracked_jobs`. Owner-scoped reads,
immutable replay identities, status/timestamp/lease-generation compare-and-set transitions, and
explicitly named administrative surfaces prevent a caller from accidentally treating global work
as ordinary user state.

`AsyncPlaneRuntime` is a bounded event-loop adapter over whole caller-owned synchronous
transactions. It does not provide async raw-SQL helpers or connection access. Awaitable callbacks
are rejected with the typed `async_plane_callback_type` error: coroutine functions before
admission, and awaitable results inside the transaction, which then rolls back instead of
committing. See
`docs/knowledge-scheduler-and-async-contracts.md` for the exact lifecycle and cancellation rules.

Connection loss at checkout, during a statement, or during commit keeps caller-owned transaction
custody explicit. Checkout failures raise `pool_checkout_failed` without leaking a borrow slot;
interrupted statements raise `transaction_statement_failed`; and a lost commit acknowledgement
raises `transaction_commit_outcome_unknown` without retrying or claiming a rollback. Every unsafe
connection is discarded, while ordinary PostgreSQL errors keep their driver types for savepoint
handling. See
[connection loss and uncertain outcomes](docs/connection-loss-and-uncertain-outcomes.md).

The public `work_admission` catalog member owns durable operation admission, finite hierarchical
capacity, submission replay, execution leases, fenced terminalization, request-generation binding,
and bounded retention. `configure()` and `load_existing_configs()` return detached snapshots;
`bind_configs()` publishes one only after the caller-owned transaction commits. The repository
validates every public type, timestamp, duration, code, terminal payload, and limit before SQL.

Revision `074.004` retains the owner-partitioned qualification-audit and bounded host-session
compatibility introduced through `074.003`, and adds durable pending attachment materialization,
typed purge scope, retired-owner admission fencing, canonical owner/attachment case-fold isolation,
expired-upload recovery, and whole-schema catalog verification. `quality_audit` provides run, case,
evidence, audit-entry, and LaTeX-artifact
records. Review plus case-status transition is one caller-owned atomic operation with a locked
owner chain head and a versioned full-record hash; legacy v1 entries remain readable without being
silently rewritten. Tutorial content/revisions, remote-operation proposals, feedback paging and
deduplication, personalization mutation, external-identity linking, and the other extended-state
facades are likewise available only through named typed catalog members.

Repository writes using the shared canonical JSON encoder require string keys in every mapping,
including mappings nested inside lists and tuples. Non-string keys raise `RepositoryValidationError` before SQL rather
than being converted to strings and colliding with existing string keys. Callers that supplied
non-string keys must update their inputs. Valid canonical JSON bytes remain unchanged; this
validation does not rewrite existing stored payloads, digests, or schema metadata.

Revision `075.001` adds the immutable `voice_session.speech_backend` discriminator. Historical
sessions backfill to `llm_factory`; new `client_local` rows carry no remote room, participant,
worker, or media-grant metadata. Voice-turn persistence remains unchanged, and Plane adds no audio,
transcript, local-engine, proof, or client-capability storage.

The revisions after `079.001`:

- `088.001` adds one-shot operation admission for persistent assignments, with an execution-profile
  discriminator and original-key operation receipts; see
  [persistent assignment contracts](docs/persistent-assignment-contracts.md).
- `088.002` adds a database-issued `web_session` incarnation identity, and `088.003` adds nullable
  issuing issuer/client metadata to sessions and an issuing issuer to deferred revocations; see
  `docs/identity-agent-state.md`.
- `088.004` stores declarative agent definitions as immutable revisions with metadata-only
  receipts.
- `088.005` adds owner guidance storage; see
  [owner guidance contracts](docs/owner-guidance-contracts.md).
- `088.006` adds immutable selected-input envelopes; see
  [selected input contracts](docs/selected-input-contracts.md).
- `088.007` adds optional scheduled-job policy and occurrence-assignment bindings; see
  `docs/knowledge-scheduler-and-async-contracts.md`.
- `088.008` adds hash-only framework credentials and finite offline-grant allowances; see
  `docs/credentials-and-grants.md`.
- `089.001` adds owner-keyed TypeSafe credential ciphertext and third-party data-sharing
  acknowledgments.
- `089.002` adds personal mesh enrollment and revocation records, `089.003` adds owner stop epochs,
  and `089.004` adds completion subscriptions and wake receipts.
- `089.005` adds notification preferences, ciphertext inbox entries, and delivery identities.
- `089.006` adds bounded owner/project Atlas page heads, command receipts, and tombstones.
- `089.007` adds immutable Atlas page revisions and fenced, repeat-safe edits; see
  [Atlas page contracts](docs/atlas-page-contracts.md).
- `089.008` adds bounded same-owner Atlas links, link revisions, command receipts, and backlink
  traversal; see [Atlas link contracts](docs/atlas-link-contracts.md).
- `089.009` adds fenced owner capability-publication records for selective host-controlled
  A2A exposure; see [capability publication records](docs/capability-publication-records.md).

- `089.009` adds owner-scoped capability publications; see [capability publication records](docs/capability-publication-records.md).
- `089.010` binds operations to immutable harness profile identities; see [harness binding contracts](docs/harness-binding-contracts.md).
- `089.011` adds opaque push destinations and claimed deliveries; see [push delivery records](docs/push-delivery-records.md).
- `089.012` adds bounded observation retention and paged recall; see [observation recall contracts](docs/observation-recall-contracts.md).

Upgrade and recovery procedures for every revision are in
[migration and recovery](docs/migration-and-recovery.md). The neutral preconditions for treating
a restored PostgreSQL/blob state as writable, including host-owned election and admission, are in
[recovery and writable readiness](docs/recovery-and-writable-readiness.md). Local
`PlaneHealth.ready` alone is not a writable-readiness decision.

The neutral, read-only fence evidence for an externally managed PostgreSQL primary and its
paired blob roots, including primary loss, reconnect, stale writer, and promoted-endpoint
refusal, is in [endpoint failover readiness](docs/endpoint-failover-readiness.md).

The explicit database-plus-blob restore unit is qualified by
`qualify_joint_restore(...)` and completed by `recover_joint_restore(...)`. Both reject wrong
roots, incomplete or changed blobs, non-current schema/digest/catalog, and failed session
retirement through the public facade. Their repeatable, path-free evidence and forward-repair
limits are in [joint restore qualification](docs/joint-restore-qualification.md).

## Error diagnostics

`PlaneError` retains at most 16 metadata entries, selected by lexicographic key order.
Keys and codes are non-empty ASCII identifiers (`A-Z`, `a-z`, `0-9`, `.`, `_`, `:`, `-`)
starting with a letter or digit and limited to 64 characters. Invalid keys and codes are
rejected. Values accept only exact built-in `str`, `bool`, `int`, `float`, or `None`;
unsupported objects and scalar subclasses become `<unsupported>` without calling user
serialization. Integers exceeding 1700 bits also become `<unsupported>`. Values are limited
to 512 characters after C0, DEL and C1 controls are escaped as `\xNN`; processing long text
is bounded before escaping. Metadata remains an immutable tuple of string pairs.

Call sites use identifiers, counts, revisions and error categories instead of payloads.
Plane does not detect arbitrary sensitive text. Hosts remain responsible for authorization
and for redacting messages and scalar metadata before storing or emitting diagnostics.

## Local verification

AstralPlane owns qualification of its Python source, architecture boundary, PostgreSQL migration
and repository behavior, and standalone package compatibility. Pull requests, merge groups, and `main` pushes run
the repository-owned `.github/workflows/ci.yml` jobs `quality`, `postgresql`, and
`package-compatibility`; the `gates` aggregate fails closed unless every owner job succeeds. The
PostgreSQL lane runs the complete Python 3.11 suite against PostgreSQL 17 with a measured-baseline
combined branch-coverage floor of 88.75% and a changed-line coverage threshold of 90%.
`scripts/check_changed_coverage.py` decides changed-line coverage from diff-cover's JSON report for
the committed range `BASE_SHA..HEAD`, where `BASE_SHA` is the pull request's base commit or, on a
push to `main`, the commit before the push. It fails below 90%; a change with no measurable
executable lines is recorded in the step summary as not applicable, naming the base and candidate
SHAs and every changed path considered. A missing, malformed, or all-zero `BASE_SHA`, a base equal
to the candidate, or a report that does not describe that range fails closed. Package
compatibility builds and installs a clean wheel on Python 3.11 and 3.14; it does not replace the
PostgreSQL production lane.

The PostgreSQL lane first builds a populated synthetic database using only `src/` archived from
the exact fetched `origin/main` SHA in an isolated interpreter. `scripts/qualify_main_upgrade.py`
records that baseline's revision, migration digest, catalog digest, and source-tree identity;
the candidate must admit its actual digest and preserve every historical migration edge, two
owners, draft rows, and share snapshots through upgrade and repeat application. Existing
pre-split and per-edge upgrade suites remain part of the complete suite.

Consumers that rerun the Plane suite from a pinned component checkout can supply
`ASTRALPLANE_UPGRADE_BASELINE_ROOT` and `ASTRALPLANE_UPGRADE_BASELINE_SHA` together, pointing
to a separately qualified owner checkout and its exact fetched `origin/main` SHA. The probe's
`--baseline-source-root` option accepts the same source checkout; archive admission still
refuses any SHA that differs from that checkout's fetched main. Missing owner evidence fails
qualification instead of skipping the upgrade tests.

`tests/integration/test_lifecycle_acceptance.py` exercises real owner/version fences, duplicate
generation claims, caller rollback and detached results, and both transaction orders for share
open versus revocation. `scripts/check_acceptance_faults.py` runs five declared faults against
these focused assertions in isolated source copies, plus a NUL-bearing proposal lock fault against
real proposal persistence. A surviving fault, missing case, skip,
unexpected failure, collection/import error, or watchdog expiration fails qualification;
only the intended behavioral assertion counts as a kill. The full suite emits the required
`acceptance-faults.json` receipt, and CI retains it with upgrade and coverage evidence.

Knowledge proposal creation encodes the advisory-lock namespace, agent, and tool as a canonical
JSON tuple without NUL delimiters, preserving independent pairs and PostgreSQL text admission.
Real database tests cover exact payload replay, supersession, concurrent proposals, and rollback.

The quality lane retains `ci-review-diagnostics.json`, binding removed tests, added skips/xfails,
swallowed test errors, and qualification-policy changes to the immutable base/head range.
These warnings require maintainer scrutiny and do not themselves authorize or prevent merging.

```text
uv lock --check
uv sync --frozen --group ci
uv run --frozen --group ci ruff check .
uv run --frozen --group ci python tests/architecture/test_dependency_direction.py
ASTRALDEEP_SOURCE_REPO=/path/to/AstralDeep \
ASTRALPLANE_TEST_POSTGRES_DSN=postgresql://user:password@127.0.0.1:5432/isolated_database \
  uv run --frozen --group ci pytest -q -p no:cacheprovider \
  --cov=astralplane --cov=scripts.check_changed_coverage \
  --cov=scripts.import_staging_fixture --cov=scripts.migrate_qualification_database \
  --cov=scripts.qualify_main_upgrade --cov=scripts.check_acceptance_faults \
  --cov=scripts.ci_review_diagnostics \
  --cov-branch --cov-report=xml --cov-fail-under=88.75
BASE_SHA="$(git merge-base origin/main HEAD)"
uv run --frozen --group ci diff-cover coverage.xml --compare-branch "$BASE_SHA" \
  --diff-range-notation '..' --ignore-staged --ignore-unstaged \
  --format json:changed-coverage.json
uv run --frozen --group ci python scripts/check_changed_coverage.py \
  --report changed-coverage.json --base-sha "$BASE_SHA" --fail-under 90
uv lock --check
uv build --build-constraints tooling/python-ci/build-requirements.lock.txt --require-hashes
actionlint .github/workflows/ci.yml
```

PostgreSQL integration checks use an isolated test database and the synthetic non-PHI fixture under
`tests/fixtures/pre_split`. Set `ASTRALPLANE_TEST_POSTGRES_DSN` to that isolated database, whose
role must be able to create and drop databases because migrated templates are cloned per test, and
run both `tests/integration/test_pre_split_upgrade.py` and
`tests/integration/test_empty_database_startup.py`. An unset URL reports the checks as skipped, not
passed. Runtime databases, blobs, uploads, logs, credentials, generated content, and local
environments must never be committed or placed beneath the package/submodule tree.

## Audit delivery retries

`AuditOutboxDelivery` retries a failed sink with exponential delays, saturating at
`max_retry_delay` before multiplying a `timedelta`. The exponent remains capped at 30;
defaults remain eight attempts, a five-second base delay, and a one-hour maximum delay.
Microsecond precision is preserved, including when the cap is not an exact multiple of
the base delay.

If the capped delay would place the next retry after UTC `datetime.max`, delivery raises
`PlaneError` with code `audit_retry_out_of_range` and the entry ID. It does not change the
claimed outbox row or report a successful settlement; the existing lease can be reclaimed
after expiry. A retry exactly at `datetime.max` is representable and remains valid.
An exhausted attempt dead-letters without calculating another retry time. Retry and
dead-letter outcomes are reported only after their fenced state transition commits.

See `docs/migration-and-recovery.md` before changing schema or durable roots.

## External-host example

`examples/external_host_example.py` is a standalone executable example that boots this package
through the public facade only — guarded migrations, an explicit absolute blob root, owner-scoped
caller-owned transactions, idempotent replay, compare-and-set fences, typed failures, and
shutdown — without importing any other Astral product. `docs/external-host-example.md` maps each
scenario to the public API stability boundary and explains where an MCP or A2A host maps its
protocol identities to the neutral owner identifiers Plane receives.

Projects are exposed through `create_project_repository()` and `catalog.projects`.
Schema `089.013` adds owner-scoped project rows, retained deletion identities, and normalized
same-owner chat membership. See [project migration and recovery](docs/migration-and-recovery.md#089013-projects-and-recovery)
for replay, pagination, and recovery contracts.

### Framework operation execution observations

`assignments.capture_framework_operation_execution_observation` captures the private
credential observation for an existing, source-less framework chat operation. Callers
pass the exact detached `AssignmentRecord` as `expected_record`, together with its
owner and assignment IDs. The repository locks owner, credential, and assignment in
that order, rejects changed state/control/instruction/operation/definition identity,
and bounds freshness by 15 seconds, credential expiry, authority expiry, and operation
deadline. Capture never consumes another admission. Claims, reservations, sends,
renewals and result retention still require their existing execution fences.

Administrative operation discovery now includes due framework chat operations with
a current submit credential. Discovery itself grants no execution authority. No schema
change is required; the existing qualified project and harness contracts are preserved.
Raw bearer values remain outside Plane, and credential hashes remain private typed
execution state rather than operation payload or public metadata.

### Harness admission captures

Schema `089.014` adds immutable private admission captures with ordinary operation retention
and preserves an assignment's initial capture independently of definition or guidance. The
public `preferences.harness` facade provides owner-scoped CAS selection. Work admission
stores capture inside new admission and checks its identity before the existing execution
binding; exact replay retains the original capture. The host owns authentication, deployment
qualification, configuration identity and selection policy. No provider URL or caller-supplied
qualification flag grants harness authority. See the guarded migration and recovery section.

### Conversation-scoped audit pages

Schema `089.015` adds `idx_audit_user_conversation_recorded`, a partial owner, conversation and
recorded-time index over `audit_events`. `audit.list_page` accepts an optional exact
`conversation_id` (at most 256 characters, no null characters). It combines that filter with the
existing owner, class, outcome, time, keyword and cursor filters, so one chat's events page
without scanning the owner's whole history. The filter is always owner-scoped: another owner's
conversation, an unknown conversation or a differently cased identifier returns an empty page.
Chain rows, digests and verification are unchanged. See the guarded migration and recovery section.

### Conversation branches and forks

Schema `089.017` stores a conversation as a tree. Every message records its parent,
each conversation has one compare-and-set branch head with an optional workspace-snapshot
pointer, and a forked conversation records its source chat and source message on the chat
row. The guarded migration backfills each existing linear chat as one branch in the exact
`list_visible` order, so visible output never changes. Branch-path, sibling-count, and
branch-head reads stay owner-scoped; fork creation copies references and never task grants,
approvals, bypass state, or pending cards. See the guarded migration and recovery section.

### Mac computer-use bookkeeping

Schema `089.018` adds owner-scoped durable bookkeeping for Mac computer use through the
`computer_use` repository: session records (host id, host incarnation, state, pause reason),
task-grant records (binding digest, expiry, hard cap, revocation), host effect intents
(remote operation id, verb, argument digest) with one-way states `pending`, then `committed`
or `unknown`, then `acknowledged`, and approval-continuation fences. Every statement carries
its owner predicate and revision or state fence; intent transitions are compare-and-set and
monotonic, so an `unknown` intent never returns to `pending`. Records store only digests and
bounded metadata; screenshots, file contents, command output, and clipboard text stay outside
Plane. Hosts list open intents after a restart, mark them unknown in bulk, and record the
owner's acknowledgment; ended sessions and expired grants have bounded cleanup paths. Deep
owns the recovery policy for what happens to unknown effects.

### Owner approval verification credentials

Schema `089.019` adds owner-scoped approval verification credential storage with revocation
and single-use challenge consumption for the macOS DirectDownload client's biometric-gated
approvals. `create_verification_credential_repository()` and
`catalog.verification_credentials` enroll bounded per-owner public keys (SEC1 or SPKI) with a
revision-fenced owner-scoped revocation compare-and-set, and consume each challenge digest
exactly once under a uniqueness constraint with a bounded retention prune. Locked owner-scoped
verification lookups never return a credential as active after a concurrent revocation
commits. Only public key material and digests are stored; biometric verification, possession
proof, and approval policy stay with the host. See the guarded migration and recovery section.

### Mesh event store

Schema `089.021` adds one owner's mesh event log. `create_event_roster_repository()` creates a
mesh whose id is the hash of its first signed roster entry, appends later entries that must follow
entries already held, and stores checkpoints with countersignatures. `create_event_log_repository()`
appends events to per-device feeds with contiguous sequence and previous-hash checks, stays
idempotent on the same event id, and keeps both events as evidence and freezes the feed when two
different events claim one position. It reads feed heads, ranges after a sequence, events by scope in
clock order, and hash-addressed sealed bodies that may be absent. `create_derived_chat_repository()`
upserts chat and message rows that a host derived from events, linked through a unique `event_id`
column on `chats` and `messages`. Signatures, encryption and the fold from events to chat state stay
with the host. See the guarded migration and recovery section.

### Run leases, goals and the learning ledger

Schema `089.022` adds event-derived, append-only history for long-running work on a mesh.
`create_run_repository()` records executor leases (take, transfer, takeover and state changes)
with contiguous epochs, one executor per run and no changes after a run finishes, and goal state
transitions with contiguous revisions, an unchanged goal text and non-decreasing rounds.
`create_learning_ledger_repository()` records proposals, adoptions and reversals with a content hash,
a scope, a review mode and the event ids that justify each entry, in that order and once each. Every
entry is idempotent by its event id. Authorization of the events and the decision to adopt stay with
the host. See the guarded migration and recovery section.

### Conversation audit range reads and segment proofs

`audit.list_conversation_range` returns one chat's events for the calling owner, optionally
narrowed to a turn id carried at the top level of an event's `inputs_meta` or `outputs_meta`
under the `turn_id` key and to a recorded-time window. It is cursor-paginated in the existing
descending order and bounded: a selection above `max_events` is refused with a typed
`audit_range_exceeds_bound` error instead of being truncated silently. The filter is always
owner-scoped in the SQL predicate, so another owner's conversation, an unknown conversation or
a differently cased identifier returns an empty page.

`audit.load_conversation_segment` returns the same selection in chain order as an
`AuditSegmentProof`: chain-contiguous `AuditSegmentRun` groups, each anchored with the previous
entry hash of its first record, together with the key ids and chain sequence numbers. A third
party recomputes every entry hash with `verify_segment_proof` and the chain authenticator
without the rest of the chain; any changed field or hash fails verification. Both reads use the
existing `idx_audit_user_conversation_recorded` index from schema `089.015` and add no schema.
Chain rows, digests and whole-chain verification are unchanged.
