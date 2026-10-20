# Validation Guide: Source-Bound Agent Actions

Run from the repository root with Python 3.11, the pinned components, and an isolated local backend/PostgreSQL environment. No Apple or Android device is needed for backend enforcement; the user requested web-only client tests.

1. Inventory every registered canonical tool and alias, verifying the declared source/effect class. An omitted or contradictory effect declaration must prevent execution.
2. Run `cd backend; python -m pytest tests/test_source_bound.py tests/test_source_bound_dispatch.py -q` after implementation. Cover permitted read, unrequested write, coincident text, derived/retained/delegated/delayed material, exact approval, replay, expiry, and failure cases.
3. Through the live local web client, read a fixture containing an instruction to send or mutate data. The read should succeed; the effect must be denied or presented as one exact owner review. Approve once, then alter or replay the call and verify refusal.
4. Disable provenance or audit in an isolated test and confirm an effect fails closed with a reconstructable diagnostic. Verify the ordinary permission, PHI, egress, delegation, and LETS gates still run.
5. Run grouped backend web tests, Ruff, and changed-line coverage. Do not claim native client verification: Apple and Android tests are intentionally omitted at the owner's direction.

The current optional taint and policy flags do not constitute this feature's enforcement.

## Local implementation evidence, 2026-10-20

The combined `codex/098-real-agent-security-qualification` branch includes the 097 host implementation. Registered tool scopes now provide the dispatch effect declaration; missing or invalid declarations fail closed. The host classifies the pinned in-process General agent's audited file-read set as reads; a registration change clears that exception, and other file-scope tools remain effectful. Effectful calls require exact attended review even when optional taint/HITL flags are off. The physical invocation rechecks that review, writes a pre-effect audit receipt, and records a response or uncertain-outcome receipt. Tool text and presentation components enter a bounded origin ledger; unknown or paraphrased material stays unknown. Effectful stream starts are refused because that transport cannot bind a completion to the same review.

On this Windows host, the combined focused backend suite passed **266 tests**; the stream denial, file-read classification, and exact review tests are included. The targeted Ruff E4/E7/E9/F check and `git diff --check` passed. Web renderer Python tests passed **64 tests**, and Projection tooling web tests passed **11 tests**. No Apple or Android client tests ran, as requested.

This is **implemented locally but not qualified for release**. Scope-derived classification does not prove every registered read tool is side-effect-free; the General file-read exception covers only host-registered in-process tools whose names match the pinned first-party inventory. Exact-string origin tracking does not preserve every transformation, upload, or remote provenance path. The registered dispatch integration gate cannot run here: Windows lacks `fcntl`, and Docker Desktop could not start an isolated PostgreSQL backend. Live web approval, changed-line coverage, grouped Linux backend tests, and full classification inventory remain required. No component pin, schema, or native client contract changed.
