# Feature 088 verification record

Status: local implementation started, 2026-09-10. No full integrated feature, live staging, merge, deployment or release is claimed.

## Source and environment

- Deep base `2de5d867413ce2aec5b7448dca8eeaa42674a7e3`; local feature branch `codex/088-rewrite-integration` after refreshed all-origin-tree ownership preflight. Source 081–087 branches remain untouched.
- Donor `daeea32b6e99b9f7cf0ff32abe372731744c639e` remains read-only; its data/services are not imported or changed.
- Plane `b20c8f3e06fc5302262fe3c8f049fa18a562e30f`; Projection `07e5c90cb310c48c2315997f74dc8dd6f5fa22ea`; Primitives `8dadde18ea84b511e130ccc332cf2f62de2990cf`; LETS `6245189920c686353c4ced7a208d56ec266f745c`.
- Host Python 3.11.15, Node 24.13.1, Corepack 0.34.6. Initial Deep environment lacked installed Plane/Projection, build, pytest-cov and PySide6. Pinned component checkouts are authoritative; sibling Projection has a different older tree.
- Only donor containers were running at the preparation observation. No Deep live verification has run.

## Baselines and setup actually executed

| Command/context | Result |
| --- | --- |
| Projection: Deep Python `-B -m pytest -p no:cacheprovider tests/chrome/test_menu_model_contract.py tests/chrome/test_shell_blocks.py tests/webrender/test_render_golden.py tests/webrender/test_escaping.py tests/rote/test_rote_identity_preservation.py tests/test_resources.py -q` | 75 passed, one packaging test failed because `build` was absent. Source baseline; no product edits. |
| Deep/backend: explicit pinned Projection source paths, `pytest -p no:cacheprovider tests/test_welcome.py tests/test_welcome_identity.py tests/test_shell_assets.py tests/test_client_js_contract.py -q` | 42 passed. Source baseline, not installed/live qualification. |
| `uv pip install --python .venv/Scripts/python.exe --require-hashes -r components/AstralProjection/tooling/python-ci/requirements.lock.txt` | Succeeded; installed declared build/coverage/lint test tooling, including build 1.5.0 and pytest-cov 7.0.0. |
| `uv pip install --python .venv/Scripts/python.exe pip==26.1.2 wheel==0.45.1 hatchling==1.27.0 uv_build==0.12.3` | Succeeded; local build tools match composition declaration; pip is local installation tooling. No runtime manifest change. |
| `.venv/Scripts/python.exe scripts/install_local_components.py sync` | Synchronized all four exact local component wheels; subsequent Plane/Projection/pytest_cov imports passed. |
| Spec/task structural check | 36 FR/SC identifiers, 67 unique tasks, no unmapped FR/SC. Story counts US1–US7: 8, 7, 10, 8, 7, 8, 8. Independent semantic analysis still required. |
| `git diff --check` | Passed for prepared artifacts at this checkpoint. |

Existing `.gitignore`/`.dockerignore` cover environment, secrets, generated data, caches and builds. No new package publishing/terraform/Helm ignore files are needed for the selected architecture. Existing Projection web ESLint tooling remains authoritative; added JS would have to enter its tracked lint input list.

## Required five-defect regressions

| Donor reproduced defect | Integrated test boundary / task |
| --- | --- |
| Credential mint uses stale pre-lock authority | Real owner-lock contention against local revoke/expiry at final mint, T046–T047. |
| Draft crosses owners in same tab | Verified owner A/logout/B, return to A and same-owner reconnect, T009. |
| Ten held oldest schedules starve another owner | Bounded eligibility/rotation through Plane and scheduler, T039–T041. |
| Discarded source bytes cannot retry after provider outage | Non-retention source refetch with preserved charges and fresh grounding, T025/T028/T029. |
| Public config fails because session changes generation | Config-first/session-first completion, true failure/retry and logout during startup, T009. |

Donor test passes and reproductions are historical review evidence, never integrated acceptance passes. Capability and detailed source requirement evidence remains pending until executed.

## Remaining qualification

Repeated Projection baseline after installing tooling and synchronizing components: **76 passed in 9.51s**, including packaging. Formal read-only Analyze found 0 critical issues and one high ordering inconsistency; all 36 FR/SC and 35 adopted/12 retained families have task coverage, with no unmapped tasks. The Accept contract now authenticates/resolves the original receipt before new-admission guidance expansion; integration tests must verify changed guidance after accepted submission.

Changed coverage, browser tests/live visual review, real institutional authenticated dispatch, representative migration/recovery, native compatibility/redesign, source grounding/human evaluation and full component/backend CI remain required. Public OIDC discovery confirms the issuer/S256 only; it does not verify client administration, mappers or authenticated journeys. No product push is authorized by this record.
