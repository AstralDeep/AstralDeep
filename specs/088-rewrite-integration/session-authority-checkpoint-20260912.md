# Interactive session authority prerequisite — 2026-09-12

This local checkpoint advances T026 prerequisites. It does not enable one-shot
ingress or continuation, complete T026, qualify staging, or authorize release.

Plane is pinned to `552175d01ab1d89052c80082986d411453bb12cf`. Schema `088.001`,
its migration digest and the Projection UI protocol are unchanged. Plane now
exposes an exact owner/session credential fence, a pre-remote database timestamp
and an observation limited to 15 seconds. Ordered assignment guards recheck the
session after waits. Prepare/reserve/start wrappers roll back failed mutations
inside savepoints. Authentic stale outcomes still settle consumption once while
refusing stale content and checkpoint advancement.

Deep's encrypted refresh coordinator supplies the exact persisted ciphertext
fence when claiming and settling a refresh. A replacement session cannot inherit
a remote result from the previous credential family even when its SID and all
timestamps are reused. Existing ordinary refresh retry behavior remains intact.

The original session incarnation is not yet stored in an operation's bare-SID
authority reference. Old observations and different SIDs are refused, but a
newly issued observation for a recreated same-SID row can match that reference.
The host must bind the originally issued incarnation before enabling durable
one-shot execution. Request-scoped forced refresh, normal IAM verification and
caller adoption remain separate work; this checkpoint creates no auth bypass.

## Frozen-source verification

- Plane's full Linux suite: 2,593 passed, nine Windows-only skips and one stale
  provenance-output hash failure. Production bytes remained fixed; after the
  provenance ledger correction and added edge cases, the focused/provenance
  cohort passed 234 tests. The original failure is retained.
- Plane changed executable coverage: 145/145 for this increment; 990/1,036
  (95.56%) across the branch. Overall statement coverage is 18,165/19,714
  (92.14%). The combined statement/branch metric is separately 89.77%.
- Deep's two ciphertext-replacement races failed against the previous
  coordinator and passed with the fix. The shared refresh, Plane adapter,
  ordinary refresh and offline-grant lifecycle cohort passed 78 tests against
  isolated PostgreSQL. Changed call statements were executed; continuation
  argument lines are not separately executable coverage lines.
- Independent review found no additional issues in lock ordering, checks after
  waits, savepoint rollback, refresh replacement races or stale accounting.
- Plane's exact wheel passed fresh Python 3.11 installation, dependency and
  public-import checks; all 69 packaged Python files match qualified sources.
  Ruff, formatting, dependency-direction, lock and staged secret checks passed.

Exact source, commands, logs, coverage, wheel and post-commit identities remain
outside the candidate tree under the task's `build/088/session-authority/` and
Plane's `build/session-authority/review-final/`. No live session, provider,
schema, runtime dependency, product remote or store was changed.
