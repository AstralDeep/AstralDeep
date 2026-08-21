# Contract: Repository Replacement and Submodule Composition

**Contract version**: `astral.git-migration/v1`
**Policy**: ordinary descendant commits; no archive refs, force-push, orphan history, published-ref deletion, or cleanup of original worktrees

## Canonical repositories

| Project | Canonical URL | Visibility | Target default |
|---|---|---|---|
| AstralDeep | `https://github.com/AstralDeep/AstralDeep.git` | public | `main` |
| AstralProjection | `https://github.com/AstralDeep/AstralProjection.git` | private | `main` |
| AstralPlane | `https://github.com/AstralDeep/AstralPlane.git` | private | `main` |
| AstralPrimitives | `https://github.com/AstralDeep/AstralPrimitives.git` | public | `main` |
| LETS | `https://github.com/AstralDeep/LETS.git` | public | `main` |

Local origin fetch/push URLs, current package metadata, operational docs, workflows, and `.gitmodules` use this casing and HTTPS identity. Historical citations may preserve a former name only when labeled historical.

## Owner-selected overwrite semantics

AstralPlane and AstralProjection are content-replaced by new commits on their existing histories:

- the refreshed legacy `master` tip becomes the initial `main` tip;
- replacement work is committed normally on a feature branch based on that `main`;
- final integration uses a normal merge/descendant relationship;
- no archive tag or archive branch is created;
- no orphan branch, reset, rebase of published history, or force-push is used;
- `master` is retained and not deleted.

This preserves prior default-branch commits as normal ancestors. It does **not** promise recovery of commits that were never merged and whose remote branch was already deleted.

## Read-only preflight

Immediately before remote/ref mutation:

1. Verify each original worktree status, unstaged/staged diff, worktrees, and repository connectivity.
2. Query live remote `HEAD`, every live head, and tags with `git ls-remote --symref`.
3. Query GitHub default branch, visibility, viewer permission, workflows, open PRs, and available protections.
4. Compare each live default SHA to the recorded baseline. Any difference triggers a fresh audit.
5. Confirm `main`, feature branches, and fresh worktree paths do not collide.
6. Confirm legacy workflows will not run expensive or privileged jobs merely because a new branch is pushed.

Current planning observations, which must be refreshed before execution:

| Repository/ref | Observed commit |
|---|---|
| Plane live `master` | `2cd12602e361a0dfb3e74655070720be405c09a3` |
| Plane live `a2a` | `700151757a7e1fd2aef9a1d3fdde78112c8a6fb6` |
| Projection live `master` | `7a10727b78f06e3b573c23b61ead5be6233506bb` |

Projection's original local `master` was two commits behind the live remote and had no local-only commits, so all new work must start from the refreshed remote tip.

## Known non-preservation risk accepted by the owner

The Plane checkout currently has cached remote-tracking tips for branches that GitHub already reports deleted:

| Former branch | Cached commit | Reachability note |
|---|---|---|
| `dynamic_server_loading` | `fb2344824360bd78fc9ff2526d163afb69cb52e5` | Already reachable from legacy `master` |
| `oauth-implementation` | `8771e88bf85df4a7a725500c5181d6c238fe7929` | One commit absent from legacy `master` |
| `refactor` | `42d55969a9632c7b70c61fd7017d06149e5b6e2a` | Already reachable from legacy `master` |
| `sam/audio-upload` | `e4d4adcbc9d5a6ea50e4ba6201ad28427582ccb6` | Two commits absent from legacy `master` |

Feature 074 records these hashes but does not publish any ref for them. The `oauth-implementation` and `sam/audio-upload` commits are therefore not guaranteed to remain recoverable from GitHub or after local garbage collection. This is an explicit owner-directed risk, not an implementation failure.

Use `git fetch --no-prune origin` during migration so the current checkout does not unnecessarily delete its cached names. This is incidental local retention, not a supported archive or recovery mechanism.

## Normalize and refresh remotes

For Plane and Projection:

1. Set fetch and push origin URLs to the canonical URL.
2. Verify `git remote -v` and repository identity.
3. Fetch with explicit `--no-prune`.
4. Re-query live remote state and stop if the default tip changed between queries.
5. Do not fast-forward or clean an original worktree when ignored-file collisions or sensitive state could be involved; fresh worktrees are the mutation surface.

No ref is created to preserve pre-migration state beyond the normal branch history.

## Introduce `main`

For each Plane/Projection repository:

1. Create local `main` at the exact refreshed `origin/master` tip.
2. Push it as a new branch without force.
3. Change GitHub default to `main`.
4. Update local `origin/HEAD`.
5. Verify live remote `HEAD` symref, GitHub default metadata, and exact `main`/`master` hashes.
6. Retain remote `master`; do not delete or move it as part of feature 074.

No replacement content lands as part of the default-branch rename.

## Fresh worktree rule

Original checkouts contain ignored continuity/sensitive state:

- Plane: `app/app.db`, logs, environment, IDE/cache state;
- Projection: `node_modules`;
- Deep: ignored Android local/signing configuration;
- LETS: ignored manuscript and generated result evidence.

They are never cleaned, recursively replaced, or used as bulk-deletion targets. Replacement occurs only in collision-checked fresh worktrees outside the original repository roots:

| Repository | Branch | Worktree |
|---|---|---|
| AstralPlane | `codex/074-extract-data-plane` | `Y:\WORK\MCP\.migration-worktrees\AstralPlane-074` |
| AstralProjection | `codex/074-extract-projection` | `Y:\WORK\MCP\.migration-worktrees\AstralProjection-074` |

Before tracked-tree replacement, resolve and validate the exact worktree root, confirm its Git common directory and branch, inventory tracked targets, reject reparse points, and prove the root equals the intended path. No computed empty/broad path, glob-constrained deletion, nested shell, or original checkout is accepted.

## Replacement content and provenance

In each fresh worktree:

1. Inventory and remove only legacy tracked paths.
2. Import only selected tracked blobs from an immutable AstralDeep source commit.
3. Add project package/config/docs/tests/contracts without copying runtime/user/ignored state.
4. Stage exact paths; never use broad staging.
5. Commit a machine-readable provenance manifest containing source/destination identities, source commit, selected paths/blob IDs, recorded legacy default baseline, and canonical manifest digest.
6. Add `Source-Repository`, `Source-Commit`, and `Source-Manifest-SHA256` commit trailers.
7. Keep copied release workflows inactive or outside active workflow paths until local qualification and remote secret/environment recreation.
8. Prove the replacement commit descends from the recorded legacy default with `git merge-base --is-ancestor`.

AstralDeep retains original file history. The destination's normal legacy ancestry, provenance manifest, commit trailers, and reciprocal Deep removal commit provide cross-repository traceability without history rewriting.

## Feature pushes and draft PRs

- Push `codex/074-extract-data-plane` and `codex/074-extract-projection` as non-default branches.
- Verify remote head equals local `HEAD` after every push.
- Search all PR states for the exact head before creation; create at most one draft PR per repository, base `main`.
- Use merge commits rather than squash/rebase when later qualified and authorized.
- Retain feature branches until AstralDeep Gitlinks point to commits reachable from component `main`.
- A pushed draft is not a merge, release, or deployment claim.
- The owner's 2026-08-21 direction authorizes qualified merges after all local gates, repository CI, and required independent reviews pass. It does not authorize an admin/protection bypass, deployment, release, store submission, paper submission, or activation of privileged workflows.

## AstralDeep submodules

`.gitmodules` contains exact URLs and no floating `branch =` option:

```text
components/AstralProjection -> https://github.com/AstralDeep/AstralProjection.git
components/AstralPlane      -> https://github.com/AstralDeep/AstralPlane.git
components/AstralPrimitives -> https://github.com/AstralDeep/AstralPrimitives.git
components/LETS             -> https://github.com/AstralDeep/LETS.git
```

Initial immutable anchors:

- AstralPrimitives: `c1feada40e104ff345c3a94348305dcf27870054`.
- LETS tag `v1.0.10` peels to `82dbe4f5ddf410cc86778784bb612440725ec66d`; signed tag object `5ed575066a0c61a51dc55278fa7412f60772fac7` was verified during planning.
- Projection and Plane: exact pushed extraction checkpoints, advanced only by an explicit composition update.

AstralDeep is public while Plane and Projection remain private. A developer without access cannot initialize the full composition. Bootstrap diagnostics state which private components need authorization and never embed credentials in `.gitmodules`, scripts, logs, or CI.

## Remote checkpoint

A repository checkpoint is complete only after:

1. remote default/feature refs match expected hashes and replacement ancestry is verified;
2. local branch/worktree status is recorded;
3. relevant narrow checks pass and exact commands/results are retained;
4. migration provenance, the no-archive decision, and residual risks are added to curated kos-wiki pages;
5. the separate kos-wiki commit is pushed and its SHA recorded.

## Stop conditions

Stop without force, cleanup, or broader retry on any:

- live SHA/default/visibility/workflow change;
- remote rejects a new branch or non-fast-forward feature push;
- default-branch update does not verify;
- worktree/branch/path collision or reparse point;
- ignored/sensitive file appears in staged content;
- uncertain PR creation;
- source manifest/blob mismatch;
- replacement commit is not a descendant of the legacy default;
- user/repository credentials appear in configuration or output.

Already-created ordinary branches remain recoverable and are documented; they are not force-deleted merely because a later step failed.

## Rollback

- Before replacement PR merge: close/leave the draft; `main` still contains the legacy tree.
- After default rename but before replacement: GitHub default may be returned to `master`; no content restore is required.
- After a later replacement merge: first revert the AstralDeep Gitlink/composition, then create a rollback branch from current component `main` and revert the replacement/merge commit with `git revert` through a normal PR.
- The recorded legacy baseline is an ancestor and may also be used to construct a normal tree-restoration commit; never reset the published branch.
- Git rollback does not authorize a Plane schema downgrade; data recovery follows the Plane compatibility/recovery contract.
