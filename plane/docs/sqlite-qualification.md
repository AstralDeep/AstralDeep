# SQLite local qualification checkpoint

The shared backend contracts are test-passing locally. This is not a release, protected CI result,
completed personal-node platform qualification, or authorization to adopt Deep's composition pin.
Base: `529120e`; package and lockfile: `0.1.12`; schema: `089.022`.

SQLite migration digest:
`e4ae392ed2951f1446cf065185a6b348bbed49c3e8cf37ca81546874887f6ef5`.
SQLite structure digest:
`5f45079d23dc20e1dc0bb1367e8076c55a582ba7df7a76224fc0cdbe1a2b4d0b`.
PostgreSQL's schema revision and migration registry remain unchanged. Deep's primary pin remains
unchanged. No shared AstralDeep service/database was modified and nothing was pushed.

## Current evidence

- Linux Python 3.11.18 / SQLite 3.46.1: **4,671 passed, 169 skipped in 79.49s**. The run includes
  every repository suite, authority, catalog contracts, all caller-rollback cases, lifecycle,
  streaming purge, generated-publication crash/recovery/convergent workers, immutable bundle and
  descriptor baseline cases, top-level projects, API/bootstrap/compatibility/schema/recovery, and
  SQLite driver/helper tests. Skips are 165 explicit PostgreSQL catalog/physical-lock cases and
  four Win32 filesystem cases.
- Linux PostgreSQL publication/native-filesystem/voice/authority/policy seams: **307 passed,
  seven skipped in 10.14s**. Three skips are SQLite-specific native trigger faults; four are
  Win32 filesystem cases. The PostgreSQL counterparts retain their original SQL CTEs.
- The earlier broad PostgreSQL shared regression passed 4,451 tests in 297.75s. Canonical harness
  cleanup, quality selection, streaming purge and portable contention follow-ups passed 47 tests;
  genuine native blocked-query classification passed five tests on each backend.
- Controlled-clock deadline tests cover execute, fetchall/fetchone, internal locking-read probes,
  short final steps, writer waits, idle cursor isolation and native VM interrupts. A separate real
  SQLite writer-wait test keeps the transaction clock old while the fresh database clock expires
  voice authority. No elapsed-time bound or automatic callback replay is used.
- Publication transitions preserve every PostgreSQL eligibility predicate. Real SQLite ABORT,
  ignored-write and deleted-parent triggers prove that split-write failures restore earlier
  changes and preserve caller transaction usability.
- The Linux shared run covers 1,605/1,692 changed executable lines relative to `529120e`, 94.85%.
  The committed-range gate is bound to the exact candidate SHA in the local handoff.

The Linux task image identity is
`sha256:c6ec1f0104b6f930ee37797f1853fdcef93318ec3b6bcbc7c020e406b0020c0b`.
Host diagnostics also used Python 3.11.15 / SQLite 3.53.1 on macOS. All databases and blob roots
were disposable. The long complete PostgreSQL diagnostic finished within its 30-minute cap:
5,920 passed, 42 macOS filesystem failures, 11 skipped, 33 passing subtests in 1,344.68s.
That diagnostic preceded final repairs and is not an exact-candidate release gate.

The first complete SQLite `pytest tests` diagnostic observed 5,474 passed, 159 failed, 173 setup
errors, 174 skipped and 33 passing subtests. Its historical test identifiers remain in
[sqlite-diagnostic-inventory.txt](sqlite-diagnostic-inventory.txt). They predate repairs and are not
current remaining failures; many directly instantiate PostgreSQL lineage/catalog/server fixtures.

## Remaining qualification and adoption boundaries

1. Deep must independently qualify the exact component artifact, portable pre-callback contention
   handling and composition metadata before adopting a pin or enabling SQLite. A genuine busy
   classification never authorizes replay of external effects or approval from an old snapshot.
2. PostgreSQL endpoint-readiness, failover/server timeline fences, `qualify_joint_restore` and
   `recover_joint_restore` remain explicitly PostgreSQL-only. SQLite provides guarded explicit
   restored-session retirement. Its registry starts empty at `089.022`; PostgreSQL predecessor
   lineage/catalog mutation fixtures are not SQLite migration contracts.
3. Completion-wake tests of PostgreSQL PID/unique-wait scheduling remain PostgreSQL physical
   assertions; shared completion-wake repository contracts pass on SQLite. PostgreSQL backend
   process-termination and the explicitly PostgreSQL external-host example retain their own gates.
4. Mac personal-node filesystem qualification remains blocked by baseline native no-replace rename
   and `/dev/fd` capability behavior. All three probes reproduce at base `529120e`; their Linux
   counterparts pass. No filesystem gate was skipped or weakened to hide this baseline.
5. The SQLite reconciliation/file exclusion implementation uses POSIX primitives. Windows personal
   node backend qualification remains outstanding; Windows thin-client behavior is not changed.

See [sqlite-backend.md](sqlite-backend.md) for composition, native locking and recovery configuration.
