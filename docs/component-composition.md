# Astral component composition and recovery

AstralDeep is the composition and orchestration repository. The four projects under
`components/` remain independent Git repositories; the parent commit records exact gitlinks and
`config/astral-composition.json` records the compatible public contracts. Default branches are not
runtime pins.

This runbook is for local development and a future authorized deployment. The current production
system remains on its monorepo-derived artifact until final qualification and a separate deployment
decision.

## Clean checkout

Use a normal authenticated Git credential helper or SSH-to-HTTPS credential mapping. Never place a
token in `.gitmodules`, a remote URL, a command transcript, or the composition manifest.

```powershell
git clone --recurse-submodules https://github.com/AstralDeep/AstralDeep.git
Set-Location AstralDeep
git checkout 074-multirepo-lets-integration
git submodule sync --recursive
git submodule update --init --recursive
python scripts/verify_composition.py
```

`AstralPlane` and `AstralProjection` are private. An authentication failure is different from a
missing gitlink or incompatible component and must be reported as such. Fix repository access, then
rerun `git submodule sync --recursive` and `git submodule update --init --recursive`; do not delete a
partially initialized component or overwrite a dirty component worktree.

Hosted composed-image jobs remain disabled while those components are private. The public
AstralDeep workflow must not upload a composed image, export a shared cache containing component
layers, or publish that image to a public registry. Fork jobs also cannot initialize the private
submodules with the repository-scoped job token. Activation requires an explicitly private
checkout, runner, artifact, cache, and publication boundary (or an owner-approved visibility
change); `pull_request_target` is not an acceptable credential workaround.

The verifier must reject:

- an absent or uninitialized submodule;
- a component HEAD or parent gitlink that differs from the manifest commit;
- a dirty component worktree;
- a noncanonical `.gitmodules` URL or floating branch selector;
- a mismatched UI protocol, Plane schema/migration contract, primitive contract, or LETS OpenAPI;
- a LETS checkout other than the signed `v1.0.11` commit.

## Deliberate component re-pin

Re-pinning is a reviewed parent-repository change, never a branch-following update.

1. Verify the candidate component commit in its owning repository and finish that repository's
   required tests and provenance checkpoint.
2. For Plane, prove that the candidate's `read_compatible_from` includes the currently deployed
   schema or attach an executable database-and-blob migration/recovery plan.
3. Check out the exact commit in the component worktree with `git checkout --detach <sha>`.
4. Update the matching commit and compatibility fields in `config/astral-composition.json`.
5. Stage only the exact gitlink, manifest, and directly related evidence paths.
6. Run the composition verifier, component contract tests, sensitive staged-path guard, and the
   affected integration tests before committing the parent change.

Do not add a `.gitmodules` branch field, copy component source into AstralDeep, force-update a
component ref, or infer compatibility from commit dates.

## Rollback decision and ordering

Treat PostgreSQL and configured blob/workspace roots as one recovery unit. A Git re-pin alone is not
a database downgrade.

1. Stop new admission and drain or explicitly fence in-flight effects. Keep existing evidence and
   transaction/outbox state intact.
2. Record the current parent commit, canonical composition SHA-256, four gitlinks, Plane schema
   revision, database backup identity, and blob/workspace snapshot identity.
3. Select a previously qualified parent commit. Verify its Plane contract against the current
   schema before changing any checkout.
4. Stop AstralDeep application/worker processes. Do not mutate PostgreSQL or blob roots while a
   writer remains active.
5. If the older Plane declares the current schema readable, check out the parent commit, synchronize
   exact submodules, verify the composition, and start normally without a destructive downgrade.
6. If it does not declare the current schema readable, keep traffic closed. Restore the matched
   database and blob/workspace snapshot together, following Plane's recovery document, before
   starting the older composition.
7. Start Plane initialization and required reconciliation first. Admit normal AstralDeep traffic
   only after Plane reports the exact compatible revision and durable reconciliation proof.
8. Run the authenticated golden flow and compare redacted audit/outbox correlation evidence. If any
   check fails, stop admission and preserve the failed state for diagnosis; do not repeatedly retry
   a non-idempotent effect.

The independent LETS warden is not started from its source submodule. With the master flag disabled,
the composed product makes no LETS call. In `enforce` mode, warden unavailability or trust failure
denies protected effects; rollback must not silently change enforce to shadow/off or bypass receipt
verification.

## Access and state recovery

- Missing private-repository permission: obtain authorized read access, confirm the canonical HTTPS
  remote, then rerun submodule initialization. Never persist credentials in repository files.
- Dirty component: preserve and hand off the local changes. Do not use `reset --hard`, `clean`, or a
  forced submodule update.
- Wrong component commit: inspect the parent gitlink and manifest. Check out the exact declared
  commit only when the component worktree is clean.
- Incompatible Plane revision: keep readiness closed and follow
  `components/AstralPlane/docs/migration-and-recovery.md`; do not bless or rewrite schema metadata.
- Failed or uncertain protected effect: reconcile durable Plane and LETS evidence before retry.
  Never fabricate success or reuse a claimed receipt.
