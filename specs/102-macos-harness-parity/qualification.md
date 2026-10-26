# macOS harness parity qualification

## State

Implementation lanes are complete on `claude/102-macos-harness-parity` in Deep and Projection and on `claude/102-mac-appcast` in the website repository. Nothing is deployed, installed or published yet. This file records only what has actually run.

## Local checks

| Check | Observation | Evidence |
|---|---|---|
| Deep `tests` group on the host venv (pinned component wheels) | 15,759 passed, 72 failed, 2,621 skipped, 4 collection errors | 71 of 72 failures and all 4 collection errors reproduce identically on untouched `main` in this host environment (Apple coverage tooling, tokenizer, PDF and DOCX parsing dependencies, BYO authoring, sandbox memory limit, host bash `set -e` behavior); the remaining failure is the native intro fixture comparison that stays red until the Projection pin moves |
| Deep Ruff over `backend` and `scripts` | passed | |
| Projection Python suite on the branch | 4,898 passed, 2 failed, 7 skipped | the interop wheel test fails on untouched `main` too; the provenance ledger entry awaits the workbench lane's final commit |
| Projection theme parity (web, Windows, Android, Apple) | 21 passed | Graphite in every table with the same seven values |
| Lane unit suites (Swift) | reported green per lane: host 72, commands 12, browser 24, updater 46, theme and copy 182 minus 4 baseline accessibility-tree failures | per lane reports; the full macOS app suite in one pass is pending the workbench lane |
| Website | `npm test` 136 passed, `npm run check`, `npm run build:preview` | appcast cross-check added |

## Pending

- Full macOS app suite in one pass after the workbench lane commits.
- Projection pin move in Deep (`components/AstralProjection`, `config/astral-composition.json`).
- Server image build and sandbox rollout with paired backup and rollback.
- Signed, notarized 1.9 (70) build installed over 69.
- Live verification of each story's independent test against the sandbox.
- Publication of 1.9 (70) through the release command on the owner's word.
