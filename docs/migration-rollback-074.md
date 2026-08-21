# Feature 074 migration rollback

Feature 074 changes source ownership and component composition without moving
the production PostgreSQL database or configured durable blob roots. Rollback
therefore has two independent parts: select a compatible code composition, then
recover data forward if the selected Plane cannot read the current schema.

## Before component pull requests merge

Leave or close the draft pull request. The component `main` branch still
contains its legacy tree. If only the GitHub default branch was renamed, the
repository owner may point the default back to the unchanged `master` branch;
no content rewrite or archive ref is needed.

Never reset, rebase, force-push, delete a published ref, or create an orphan
replacement branch. AstralPlane and AstralProjection replacement commits must
remain ordinary descendants of their recorded legacy `master` baselines.

## Roll back an AstralDeep composition

1. Stop admitting new work and record the deployed Deep SHA, all four Gitlink
   SHAs, the composition-manifest digest, Plane schema revision, and configured
   blob roots.
2. Select an earlier AstralDeep commit whose manifest and four Gitlinks form a
   self-consistent composition. Do not edit the manifest or Gitlinks separately.
3. Revert the Deep composition/removal commit with `git revert` on a normal
   rollback branch and qualify that branch through the same ownership,
   compatibility, startup, and flag-off gates. Do not move a published branch
   backwards.
4. Deploy only after the selected Plane reports that the existing database is
   read-compatible. Keep writers quiesced if compatibility is uncertain.

Reverting Deep first removes traffic from a component revision before its
component repository is reverted. A private component must remain reachable at
the exact Gitlink SHA for as long as any supported Deep composition references
it.

## Roll back component source

After Deep no longer references the unwanted component commit, create a normal
branch from the current component `main` and use `git revert` on the replacement
or merge commit. Open an ordinary pull request; do not reset `main` to the
legacy baseline. A tree-restoration commit constructed from the recorded
baseline is also acceptable when it remains a normal descendant and retains the
provenance record.

For AstralProjection, keep release workflows inactive during rollback and
re-run protocol/resource, Windows bridge, and client identity checks before any
later release decision. For AstralPrimitives and LETS, select only releases
that satisfy the exact composition compatibility fields; never mutate a signed
LETS tag.

## AstralPlane schema boundary

Git rollback does not authorize a database downgrade. Feature 074 migrations
are forward, guarded, and repeat-safe; no automatic down migration exists.

- If an earlier Plane revision declares the current schema read-compatible,
  it may be selected after its startup compatibility check succeeds.
- If it is not read-compatible, keep the newer compatible Plane in the
  composition or ship a reviewed forward recovery migration from the current
  revision. Do not run ad-hoc SQL.
- A restore from backup is an operator incident procedure, not a Git rollback.
  Quiesce writers, preserve the failed database and blob roots, restore a
  mutually consistent database/blob snapshot to new verified locations, and
  validate record counts, sizes, and SHA-256 values before switching config.
- Never copy through symlinks or junctions, delete the previous durable roots,
  or claim purge completion until Plane verifies physical absence.

An incomplete migration is recovered through AstralPlane's documented
initialization/recovery path. Capture the failing revision, migration digest,
safe error code, and recovery result without recording credentials or user
content.

### Retired Deep migration commands

Feature 074 removes Deep's `migrate_sqlite_to_postgres`, `migrate_user_ids`,
and `migrate_agent_ownership` commands. They opened independent driver pools,
performed schema or repository work outside Plane, and in some failure cases
continued after only part of the requested operation succeeded. They are not
valid recovery tools for a decomposed deployment.

Container startup now exits with configuration status 78 when either legacy
SQLite file is present. It does not modify the file, write a completion marker,
or start against a possibly incomplete PostgreSQL copy. Preserve the SQLite
files and all paired blob roots. Use a verified PostgreSQL/blob backup with the
joint restore procedure above, or keep the prior compatible composition
quiesced while a separately reviewed Plane-owned importer is designed. No such
production arbitrary-SQL importer exists in feature 074.

The old feature-060 candidate staging restore is retired for the same reason.
Deep no longer starts a legacy schema container, pipes fixture SQL into
PostgreSQL, or queries schema metadata itself. Until a bounded Plane-owned
staging import contract exists, the deploy command fails before creating a
Compose namespace. Migration qualification comes from Plane's immutable
pre-split fixture replay and `provenance/checks.json`; those test artifacts do
not authorize production import or ad-hoc SQL.

## Completion evidence

A rollback is complete only when the deployed Deep SHA and every Gitlink are
reachable from their normal repository histories; the composition manifest
matches those exact commits; Plane reports a compatible ready schema; the
authenticated off-mode golden flow passes; and the curated kos-wiki checkpoint
records exact SHAs, checks, residual risks, and the no-release/no-deployment
posture where applicable.
