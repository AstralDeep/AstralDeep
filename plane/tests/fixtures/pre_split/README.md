# Synthetic pre-split fixture

This directory is an executable, targeted `066.001` AstralDeep durable-state fixture. It includes
representative owner-scoped history, preferences, attachments, workspaces, scheduling, remote
metadata, voice metadata, a multi-owner audit chain, and two digest-bound blobs. Every identifier,
address, timestamp, and payload is synthetic and contains no PHI or real user data.

`loader.py` first executes Plane's provenance-bound, schema-only `066.001` compatibility builder,
then `baseline.sql` adds the migration-relevant predecessor quality tables and representative
synthetic rows. This models an actual historical startup without repairing a partial schema during
the `074.004` migration. It is not a production dump and must never be used as a backup substitute.
`database.json` is the semantic inventory, while `expected.json` is the machine-readable continuity
and blob manifest. The fixture identity binds those files, every blob, the compatibility-builder
source bytes, and its extracted historical source-blob identity.

Audit classes follow AstralDeep's historical public schema at commit
`96c2187b7e4ba3f1ac9fe78dcabbbcb98a0132d7`, whose database blob is the recorded
`066.001` predecessor. Its audit schema blob `963116f37d017f8b994bac1ecf54f8e6ba21b214`
declares `"agent_tool_call"` and `"auth"`; it never declares `"tool"` or `"session"`.
The synthetic rows and semantic inventory use those supported classes. The migration
test preserves every original audit field, including class, metadata, timestamps,
schema version and hash bytes, through upgrade. These fixture-generation values do
not normalize or rewrite any existing database or broaden the host's write vocabulary.

Use only an isolated PostgreSQL test database and a new absolute blob root:

```text
python tests/fixtures/pre_split/loader.py \
  --schema astralplane_fixture_0123456789abcdef0123456789abcdef \
  --blob-root /absolute/temporary/pre-split-blobs
```

The URL comes from `ASTRALPLANE_TEST_POSTGRES_DSN` unless `--database-url` is supplied. The loader
requires the exact generated-schema name shape, refuses an existing blob destination, stages and
verifies every blob before publication, and commits the schema only after the staged blob root has
been promoted. It never selects a runtime database or durable root implicitly.
