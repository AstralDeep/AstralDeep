# Pull request triage

Maintainers review the current full diff, the owning constitution, relevant issue acceptance
checks, existing implementation, tests, and any replacement work before deciding a PR's fate.
This policy applies to all contributors and preserves meaningful, repairable contributions.

## Context and truthful scope

Link a real issue and explain which acceptance checks the PR addresses. Use `Closes #N` only
when the final diff fulfills that issue; references to another PR do not count as issue links.
A standalone bug fix, test, documentation improvement, or maintenance change is welcome:
include a concrete paragraph starting with `Standalone:` explaining the problem and why this
change belongs here. State exactly what was checked, the results, and what remains unverified.
Partial work must describe its actual scope and must not claim to complete a wider task.

The triage workflow periodically requests missing context with one bot comment and the
`triage:needs-context` label. Providing a real issue reference or standalone explanation
resolves that request. Draft PRs are excluded. Missing links do not automatically close a PR.

## Evidence required for closure

Prioritize these reasons only after verifying that the diff has no independently useful work:

| Reason | Required finding |
| --- | --- |
| `no-op` | No new behavior, usable documentation, or meaningful verification; for example, adding only an unconditional `assert True`. |
| `unsupported-completion` | The advertised deliverable is absent, fabricated, or unrelated, and the diff has no useful contribution to retain or narrow. |
| `duplicate` | The existing implementation already provides the same behavior and the PR supplies no distinct fix, control, test, or documentation improvement. |
| `superseded` | Identify the completed merged replacement and verify that no useful uncovered behavior or assertion remains. |

Explain the exact finding with file or source links. Keep useful partial work open for repairs
or narrower scope, even when its description overclaims. Tests-only, documentation-only, and
small PRs can be valuable. Competing PRs are not superseded merely because another PR exists.
Missing issue links, age, diff size, author identity or submission volume, AI assistance,
conflicts, and failed or pending CI alone never establish a closure reason. Green CI also
does not establish that a feature was implemented.

## Maintainer closure procedure

Review the latest full diff and any replacement first. A maintainer with current repository
write, maintain, or admin permission may either close manually with the same public evidence,
or post a new comment in this exact form:

```text
/astral-triage close <full 40-character current head SHA> <reason>
Explain the verified finding with concrete source or replacement links (at least 40 characters).
```

The controller accepts only the four reasons above, re-reads the provider comment, verifies
the maintainer's immutable identity and current permission, refuses edited comments, checks
the open non-draft main-targeted PR and exact head again before closing, and verifies the
result. A head or PR scope that changes during closure is reopened for fresh review.
GitHub's closure API has no atomic head precondition: rechecks and conservative
reopening are compensating controls. An unverifiable close reply or post-close readback
attempts a verified reopen and reports any remaining uncertainty. The controller
records the decision and reason label publicly; a command receipt is never replayed, even
after manual reopening or an API failure. Review again and post a fresh comment to retry.

Closing an unmerged PR leaves task issues, contributor branches, and bounty points unchanged.
Unfulfilled tasks stay open. A contributor can ask for reopening when a substantive revision
addresses the findings; a maintainer reviews the new head before any further closure.

## Controller boundary

`pr-triage.yml` runs only from exact `refs/heads/main` using a reviewed, full-SHA-pinned
community action and the built-in short-lived token. Its only permissions are `issues: write`
and `pull-requests: write`. It serializes comment events and periodic recovery, checks out no
repository code, executes no contributor text, downloads no artifacts, and has no secrets,
OIDC, contents-write, workflow approval/rerun, merge, publishing, or release authority.
Semantic quality judgments remain maintainer decisions. This policy does not replace product
review, required checks, security policy, the owning constitution, or release qualification.
