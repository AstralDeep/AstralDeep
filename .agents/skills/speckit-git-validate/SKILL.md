---
name: speckit-git-validate
description: Validate a Spec Kit feature branch and its matching spec directory without mutation. Use before feature work or when branch/spec alignment is uncertain.
---

# Validate Feature Branch

Read the current branch with `git rev-parse --abbrev-ref HEAD`, falling back to `SPECIFY_FEATURE` only when Git is unavailable.

Accept a branch whose final path segment starts with either marker, so namespaced names such as `feat/042-name` also qualify:

- Sequential: `[0-9]{3,}-`
- Timestamp: `[0-9]{8}-[0-9]{6}-`

For a valid feature branch, locate the matching `specs/<prefix>-*` directory, ignoring any namespace before the final segment, and report both. For an invalid branch, report the current name and the expected patterns. This skill is read-only: do not create, rename, or switch branches.
