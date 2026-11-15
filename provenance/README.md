# Provenance records

This directory holds the evidence recorded when AstralPlane was extracted from AstralDeep and while
its first repository slices were qualified. `tests/test_provenance.py` verifies two records on
every run; every other record is historical. A historical record describes the revision and
bytes it names, not the current schema, and is never cited as current evidence. The current
qualification of the schema, the repositories, and changed lines is the CI workflow in
`.github/workflows/ci.yml`, which runs the complete suite on real PostgreSQL.

## Verified on every run

- `extraction.json` is the immutable manifest of the 52 AstralDeep source files selected at
  AstralDeep commit `fc113c4f99121b2053bb71523835c5c4743f1f56` (tree
  `914b04d369faa4ee0d7c2bb59ce09db38a18d45a`), with each file's blob, mode, and size.
  `tests/test_provenance.py` verifies its manifest digest and fixed identities, and replays every
  selection root against that AstralDeep commit when `ASTRALDEEP_SOURCE_REPO` names a checkout, as
  it does in CI.
- `transformations.json` is the ledger of how each extracted file was absorbed, with the SHA-256 of
  every current AstralPlane file that holds the result. `tests/test_provenance.py` recomputes each
  digest from current bytes, so a change to a bound file refreshes its entry in the same change.

## Historical records

- `checks.json` is the historical `074.004` migration qualification record, not current evidence.
  `scripts/record_migration_evidence.py` recorded it on 2026-08-21 for candidate
  `75a28dd48ff1116050ba2e2c78cbf9be46d35d9b`, against migration registry digest
  `31495e9b916301e5d9d5011f256224e62e0a0822e25fdf3b9c339beb695eff50`: eight sequential PostgreSQL
  migration and recovery cases, all passed. The recorded digests of
  `src/astralplane/database/migrations.py`, `src/astralplane/database/revision.py`, both integration
  suites, `docs/migration-and-recovery.md`, and the recorder no longer match current bytes, and the
  current schema is `089.022`; only its `066.001` baseline builder and pre-split fixture inputs
  still match. The current migration registry digest is
  `dde302967743cf8f6f5d55e82f8355ef405823dfee638909c3a2f11b85f5cb27`.
  `tests/test_provenance.py` checks only its format, status, and case count and that
  the two slice records citing it agree with it; it does not compare its input digests with current
  bytes. Running the recorder again rewrites the file, so this description changes with it.
- `attachment-parser-and-blob-composition.json`, `conversation-extended-state.json`,
  `credentials-and-grants.json`, `identity-agent-state.json`,
  `knowledge-scheduler-and-async-contracts.json`, and `work-admission-and-quality-audit.json` are
  repository-slice records (`astralplane.repository-slice/v1`). Each lists one slice's factories,
  tables, invariants, and verification as recorded at schema `074.001` or `074.004`. Their digests,
  test counts, and statuses describe that time; nothing re-verifies them against current bytes. The
  attachment-parser and work-admission records cite `checks.json` as their migration evidence,
  which is equally historical.
- `authority-contracts.json` and `repository-contracts.json` record the authority test family and
  the repository contract matrix at `074.004`, when the catalog had 36 members. The current
  descriptions are `docs/authority-contract-evidence.md` and `docs/repository-contract-evidence.md`.
