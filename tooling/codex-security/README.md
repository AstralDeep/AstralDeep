# Codex Security report-only review (isolated developer environment)

Codex Security (https://github.com/openai/codex-security, reviewed source pinned at commit
`f894f1ff64b1a332001f48593c1622b85701f9f3`, accessed 2026-10-06) offers source-oriented
candidate findings and coverage receipts. This directory defines the opt-in, isolated,
**report-only** developer workflow that runs it against an approved AstralDeep checkout.
It is a developer tool, not an Astral runtime gate, a security gate, or a CI gate: no CI
job invokes it, no report is committed, and no automatic remediation exists. Findings are
candidates for human triage against actual code — never claims of vulnerabilities.

The wrapper is `backend/devtools/codex_security_review.py`. It is Python 3.11 standard-library only
and adds **no third-party runtime or tooling dependency** to any product or CI manifest;
nothing here is installed into the production image.

## Isolated developer environment

Create a dedicated virtual environment, separate from the product image and from other
tooling environments, and install the provider tool only from the reviewed source pin:

```sh
python3.11 -m venv ~/.venvs/codex-security-review
~/.venvs/codex-security-review/bin/python -m pip install --upgrade pip
git clone https://github.com/openai/codex-security /tmp/codex-security-src
git -C /tmp/codex-security-src checkout f894f1ff64b1a332001f48593c1622b85701f9f3
~/.venvs/codex-security-review/bin/python -m pip install --no-deps /tmp/codex-security-src
```

`--no-deps` keeps the install surface exactly at the reviewed tool: review and install any
provider-declared dependencies explicitly, from pinned versions, before enabling them.
Runtime prerequisites of the wrapper itself: Python 3.11, `git`, and Bubblewrap (`bwrap`)
with working unprivileged user namespaces. AppArmor or seccomp restrictions that break the
Bubblewrap probe are preserved verbatim and fail the run — they are never weakened or
bypassed. macOS and Windows are unsupported by this sandbox profile.

## Policy

`review-policy.json` is the digest-bound configuration every report binds to:

- Reviewed provider source pin (`provider.reviewed_source`) — the wrapper refuses any other
  commit. Changing the pin requires a new source review and a code change
  (`REVIEWED_PROVIDER_COMMIT` in the wrapper must keep matching).
- Source scope and exclusions (`scope`) — only git-tracked files under the declared include
  roots are inventoried, so `.env`, secrets, uploads, generated/user code, `backend/data`,
  `backend/tmp`, `backend/logs`, synthesized `backend/knowledge`, clinical-data artifact
  directories, and submodule internals are excluded by construction; the exclusion list is
  defense in depth on top of that.
- Sandbox (`sandbox`) — the provider runs under Bubblewrap with all namespaces unshared, a
  cleared environment, a read-only root, tmpfs masks over sensitive directories, and
  `/dev/null` masks over sensitive files (`.env`, keys, certificates). Network stays
  unshared unless egress is explicitly approved for the run.
- Budgets (`budgets`) — maximum runs, provider calls per run, total cost in USD, wall-clock
  seconds, and per-artifact bytes, enforced from a private append-only ledger.
- Reports (`reports`) — the private report root must be git-ignored
  (`backend/tmp/codex-security` by default). The wrapper refuses tracked locations.
- Preview (`preview`) — permanently disabled. The CLI path is the only path; the wrapper
  never starts a server and `codex_security_review.py preview` always refuses, so no
  unauthenticated findings API can be exposed by this workflow.

## Usage

From the approved checkout toplevel (the wrapper refuses a subdirectory):

```sh
# Read-only plan: resolved scope, exclusion counts, digests, budgets. No egress, no writes.
~/.venvs/codex-security-review/bin/python backend/devtools/codex_security_review.py plan

# One sandboxed, offline (egress-unshared) run with synthetic/frozen provider behavior:
... run --approve --provider-command ~/.venvs/codex-security-review/bin/codex-security

# One paid/provider-backed scan: explicit egress approval per run; the sandbox passes the
# policy endpoint allowlist (api.openai.com:443) to the provider and CODEX_SECURITY_* env:
... run --approve --egress-approve \
    --provider-command ~/.venvs/codex-security-review/bin/codex-security

# Re-verify a finished run against the exact source tree and policy:
... verify --run-dir backend/tmp/codex-security/<run-id>
```

`run` requires `--approve` (explicit operator opt-in for this checkout) and records the
approval in the report. `--egress-approve` is per-run, never persisted in the tracked
policy; without it the sandbox has no network namespace at all. Set
`CODEX_SECURITY_PROVIDER_API_KEY` in the wrapper's environment only when needed; it is
forwarded to the sandboxed provider solely when egress is approved and is never logged or
written to any report.

## Provider adapter contract

The wrapper invokes `<provider-command> scan --output-dir <run>/raw` with environment
`CODEX_SECURITY_SOURCE_ROOT`, `CODEX_SECURITY_OUTPUT_DIR`, `CODEX_SECURITY_MAX_CALLS`,
`CODEX_SECURITY_EGRESS_ALLOWLIST`, and `CODEX_SECURITY_EGRESS_APPROVED`. The provider must
write `raw/result.json`:

```json
{
  "schema_version": 1,
  "status": "complete|partial|interrupted|error",
  "provider": {"tool": "codex-security", "version": "<string>"},
  "calls_used": 0,
  "cost_usd": 0.0,
  "artifacts": {"sarif": "findings.sarif", "coverage_receipt": "coverage-receipt.json"}
}
```

`sarif`/`coverage_receipt` are optional simple filenames inside the raw directory. The
wrapper sanitizes both (repository-relative paths; code snippets and context regions
stripped; any reference to an excluded path or a path outside the checkout fails the run)
and writes them only to the private run directory. A run ends in exactly one of:
`complete` (clean scan, sanitized SARIF + coverage receipt written), `partial` (scan
finished but incomplete, artifacts still written and labeled), `interrupted` (signal or
wall-clock cap; raw evidence preserved, no sanitized findings), `stopped` (budget boundary:
refused before start, or call/cost budget exceeded mid-run — no sanitized findings), or
`error` (provider failure, contract violation, or sanitization violation — raw evidence
preserved for diagnosis). Only `complete` means a finished clean scan; everything else is
visibly distinct in `report.json` and the ledger.

## Reports and verification

Each run writes `report.json` (status, source commit + source digest, policy
`config_digest`, provider pin and reported version, budget limits/usage, egress decision,
sanitized-artifact SHA-256 digests, and a tamper-evident `report_digest`) plus the
sanitized artifacts and the preserved raw evidence, all mode 0600 under the private root,
outside tracked product data. `append`-only `ledger.json` records every run and refusal
for budget enforcement. `verify` recomputes the report digest, artifact digests, policy
digest, source commit and live source digest, and the ledger receipt, and fails closed on
any drift. Exit codes: `0` complete/partial or verified; `1` verification failed; `2`
denial/preflight failure/preview refusal; `3` stopped; `4` interrupted; `5` error.

## Tests and limits

`backend/tests/test_codex_security_review.py` exercises success, partial, interrupted,
stopped, error, denial, and recovery paths offline against frozen synthetic provider
fixtures and a recording sandbox fake — no provider account, no network, no CI dependency.
The synthetic fixtures are not vulnerability claims; triage any real finding against the
actual reviewed code before acting. Required CI stays deterministic, offline, and
unchanged by this workflow.
