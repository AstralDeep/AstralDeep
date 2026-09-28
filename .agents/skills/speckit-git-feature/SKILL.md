---
name: speckit-git-feature
description: Create one collision-safe Spec Kit feature branch after checking local and remote spec trees. Use for the mandatory before-specify hook or an explicit feature-branch request.
---

# Create Feature Branch

Create and switch to one feature branch for the supplied feature description. This skill creates the branch only; `$speckit-specify` creates the spec directory and files.

## Input

Use `$ARGUMENTS` as the feature description. If the caller explicitly supplies `GIT_BRANCH_NAME`, preserve that exact override.

## Procedure

1. Inspect `git status --short --branch` and active work before mutation.
2. Refresh `origin`, then inventory existing feature/spec numbers in the working tree and in every remote ref tree. A directory such as `specs/059-name` on `origin/058-other-work` reserves 059 even when no branch begins with 059. If remotes cannot be checked, require an explicit collision-checked `GIT_BRANCH_NAME` rather than guessing.
3. Resolve numbering from `branch_numbering` in `.specify/extensions/git/git-config.yml`, then `feature_numbering` in `.specify/init-options.json`, then its deprecated `branch_numbering`; default to `sequential`. An empty `branch_template` and `branch_prefix` in `git-config.yml` keep the `{number}-{slug}` branch shape.
4. Generate a concise 2-4 word action-noun short name. When invoked by `$speckit-specify`, use the exact `GIT_BRANCH_NAME` and `SPECIFY_FEATURE_DIRECTORY` that its mandatory remote-tree preflight already reserved; do not allocate independently.
5. Run exactly one platform-appropriate command:
   - Bash sequential: `.specify/extensions/git/scripts/bash/create-new-feature-branch.sh --json --short-name "<short-name>" "<description>"`
   - Bash timestamp: add `--timestamp`.
   - PowerShell sequential: `.specify/extensions/git/scripts/powershell/create-new-feature-branch.ps1 -Json -ShortName "<short-name>" "<description>"`
   - PowerShell timestamp: add `-Timestamp`.
6. Parse and report `BRANCH_NAME` and `FEATURE_NUM`. Never rerun the script for the same feature.

The stock script's local scan is not sufficient for this cross-machine repository. Do not let it choose a sequential number until the remote-tree inventory proves the number free; otherwise use the pre-reserved exact `GIT_BRANCH_NAME`.
