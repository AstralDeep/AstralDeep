# Phase R results

**Date:** 2026-11-17. Phase R of [plan.md](../plan.md) moved four repositories into the repositories that own them. All four are now archived with a pointer README.

## What moved

| Old repository | New home | Deep or LETS merge | Archived |
|---|---|---|---|
| AstralVessel | `vessel/` in AstralDeep | Deep #637 `f0e18e94`, release metadata #640 `4151567f` | 2026-11-17 00:21 UTC |
| AstralPlane | `plane/` in AstralDeep | Deep #659 `18e04242` | 2026-11-17 15:03 UTC |
| LETS-demo | `demo/` in LETS | LETS #168 `50614592` | 2026-11-17 00:52 UTC |
| mcp_server_examples | `examples/` in AstralDeep | Deep #657 `cd0fe08b` | 2026-11-17 14:29 UTC |

History came with each move. Plane's schema revision and migration digest did not change, and existing SQLite and PostgreSQL databases open unchanged.

## Issues

- AstralVessel #35, #31, #29, #28, #27, #26 and #23 became AstralDeep #641 to #647. The six contributor pull requests (#37 to #42) were closed with a pointer.
- AstralPlane #124, #119, #118, #117 and #52 became AstralDeep #660 to #664 with the label `plane`. Plane had no open pull request.
- mcp_server_examples and LETS-demo had no open issues.

## Docs switch (Vessel, D7 step 2)

Site PR #22 (`446d76a5`) published the Vessel docs under `astraldeep.github.io/AstralVessel/`. The project Pages site was turned off at 2026-11-17 00:20 UTC. The folder then won: `astral-docs.json` was served with the site deploy time, and 24 of 24 sitemap URLs returned 200. The repository was archived after that check. Rollback was to POST the Pages `build_type` back to `workflow` and rerun the docs workflow.

## Sweep (task 13)

- `AGENTS.md` routes `plane/`, `vessel/` and `examples/`, and carries both package gates.
- `README.md` names four submodules plus `plane/`, and its layout table lists `plane/`, `vessel/` and `examples/`.
- `scripts/tests/test_moved_repository_links.py` fails on a tracked link to a moved repository outside the listed history and provenance files.
- `scripts/verification/check_089_scope.py` no longer expects a sibling AstralPlane checkout.
- Two dormant release workflows still read the v1 composition schema from the trusted builder commit. They move with `RELEASE_TRUSTED_BUILDER_SHA` (TO.R1).

## Other repositories

Search used: `AstralDeep/(AstralVessel|AstralPlane|LETS-demo|mcp_server_examples)` and `components/AstralPlane` on each default branch.

| Repository | Result |
|---|---|
| AstralKernel | No hits |
| AstralPrimitives | No hits |
| AstralVesselVscode | No hits |
| AstralProjection | One refused-input fixture, `tests/migration/test_materialize_extraction.py` |
| LETS | History only: `CHANGELOG.md`, `results/`, and the pinned benchmark anchors under `benchmarks/nsdi_strengthening/` |
| Site | History and bounty records: `data/config.json`, `data/awards.json`, `state/`, the bounty actions and their tests, and one commit-pinned link in `docs/INTRODUCTION.md` |
