# Verification Record: 089 TypeSafe Routing and a8p UI Integration

**Feature**: `089-typesafe-a8p-integration`
**Opened**: 2026-09-17
**Evidence class**: **local** unless a line says otherwise. CI is excluded from 089 by owner directive (FR-039); no result here is CI-qualified, and no release or deployment is authorized by this record.

---

## 1. Baseline SHAs

Captured 2026-09-17 at the start of implementation. Every measurement below is bound to these commits unless it names a later one.

### Working repositories

| Repository | Path | Branch | Baseline SHA |
|---|---|---|---|
| AstralDeep | `Y:\WORK\MCP\AstralDeep` | `089-typesafe-a8p-integration` | `e92db75d95719602228b5b41f179cfcbeeadc3bd` |
| AstralPlane | `Y:\WORK\MCP\AstralPlane` | `main` | `65cbaedbbda4c5f9adfcb05becf02377a29caeee` |
| AstralPrimitives | `Y:\WORK\MCP\AstralPrimitives` | `main` | `4056df95acd992a9f84d883e572760f6da24c88e` |
| AstralProjection | `Y:\WORK\MCP\AstralProjection` | `main` | `dd93dfb30b89b9966682e117a76952f8429a24b0` |
| LETS | `Y:\WORK\MCP\LETS` | `main` | `f53f3f329541ad34161ec9aaaad075e80ccb01d1` |

LETS is **not changed** by 089. Its SHA is recorded so the scope check can prove it.

### Reference repositories

| Repository | Path | Branch | SHA | Role |
|---|---|---|---|---|
| a8p | `C:\Users\Sam\Desktop\a8p` | `main` | `1a3fcb105b9f226501d07c4f99aef0b8efbed189` | Design reference for routing and web layout. Not modified by 089. |
| kos-wiki | `Y:\WORK\kos-wiki` | `main` | `0fe21bae3bf4678d1fdca02b9dc644d24feb0669` | Knowledge base. Updated by the `K` tasks. |

### AstralDeep submodule pins at baseline

These are the pins 089 replaces in T056.

| Submodule | Pinned SHA at baseline |
|---|---|
| `components/AstralPlane` | `1aee0db3a4e1690d01ff649c3d54bde30a2925bf` |
| `components/AstralPrimitives` | `8dadde18ea84b511e130ccc332cf2f62de2990cf` |
| `components/AstralProjection` | `447ac077828fbc8c1a97a417d32d30e4a8c7b830` |
| `components/LETS` | `6245189920c686353c4ced7a208d56ec266f745c` (`v1.0.11`) |

> The pinned submodule commits are behind the working repositories listed above. Component work for 089 is done in the working repositories and pinned into Deep by T056.

---

## 2. Supersession record (081–087)

089 supersedes the unimplemented draft specifications 081 through 087. Their content had already been reconciled into 088. Their unpushed local branches were deleted on 2026-09-17. No requirement from them is implied by 089 beyond what 088 already carries.

| Draft spec | Final local branch tip (deleted 2026-09-17) |
|---|---|
| 081 | `e44f6ddc` |
| 082 | `ee0f7f4d` |
| 083 | `9b42fa7a` |
| 084 | `a50b74f9` |
| 085 | `c3573fa7` |
| 086 | `928d6c2f` |
| 087 | `51f6ee22` |

Source: spec.md "Supersession"; `conv:2026-09-17`.

---

## 3. Owner exceptions

Recorded 2026-09-17. Each is a bounded, explicit exception to a Constitution principle, cited in plan.md's Constitution Check.

| # | Exception | Scope and bound | Principle |
|---|---|---|---|
| E1 | **CI is ignored** | CI results do not gate 089 on any Astral or LETS repository. Every CI-equivalent check (pytest, Ruff, ESLint, Playwright, coverage, secret scan) is run **locally** and recorded here. No `.github/workflows/` file in any of the five repositories is modified (FR-039, SC-011, enforced by T002/T060). | XI |
| E2 | **Web-only client changes** | The a8p layout, responsive work and the six new primitive renderers apply to the **web** client only. `windows-client/`, `android-client/` and `apple-clients/` are untouched. Non-web clients receive the new component types only as server-side ROTE fallbacks. | XII |
| E3 | **Desktop-only parity target** | The ≥90% a8p parity score (SC-010) is required at desktop viewports (1920×1080, 1440×900, 1280×800) only. Tablet and phone are held to the responsive checklist (SC-012), not to a8p parity — a8p has no mobile design to match. | XII |
| E4 | **Staging is a local candidate stack** | Qualification runs on an isolated local `docker compose` stack with a populated synthetic dataset, bound to the exact SHAs above. No deployment or release is authorized. | X |
| E5 | **Dependency pin without an approval gate** | `typesafe-sdk==0.6.0` (and transitive `httpx2`, `msgspec`, `tenacity`) is pinned under Constitution v3.0.0, which removed the approval gate. It was additionally owner-approved on 2026-09-17. | V |

---

## 4. Known-divergence register

Expected, owner-accepted failures. Everything **not** listed here is blocking (SC-008).

| ID | Test / check | Expected outcome | Why it is accepted | Status |
|---|---|---|---|---|
| KD-1 | AstralProjection native manifest drift guards (the unmodified native-client tests that assert the exact `component_types` set in `contracts/ui_protocol.json`) | **Fail** once the six new types are added in T005/T040 | E2: the six types are web-rendered and down-converted for every other client. The drift-guard tests are deliberately **not edited** (T040). | Pending T040 |
| KD-2 | Windows-client drift guards asserting the same manifest set | **Fail** for the same reason | E2 | Pending T040 |
| KD-3 | `P:tests/test_disposition_matrix_088.py` prior to its 089 update | Updated in T040 for the new types' dispositions; native profile rows must remain unchanged | Server-side disposition data is part of the contract, not client code | Pending T040 |

Guard against silent drift: T041 adds `P:tests/rote/test_non_web_unchanged_089.py`, which requires byte-identical adapter output for `windows`, `android`, `ios`, `macos`, `watch`, `tv` and `voice` profiles against the 089 base, except that new types appear only as fallbacks. A failure there is **blocking**, not a known divergence.

---

## 5. Local toolchain

Recorded by T002.

Confirmed 2026-09-17 on Windows 10 with Docker Engine 29.8.0, `uv`, Node v22.16.0.

| Repository | Exact command | Confirmed |
|---|---|---|
| AstralDeep | `docker compose build astraldeep` then `docker compose up -d`; tests via `make test-backend` = `docker exec astraldeep bash -c "cd /app/backend && python -m pytest -q"` | Image built (exit 0) |
| AstralPlane | `uv run --group ci python -m pytest -q` | 2478 passed, 1538 skipped, **3 pre-existing failures** |
| AstralPrimitives | `uv run --group ci python -m pytest -q` | 40 passed at baseline |
| AstralProjection | `PYTHONUTF8=1 uv run --with "pytest>=8" --with "astralprims==0.3.0" python -m pytest -q` | 2519 passed, 1 skipped, **85 pre-existing failures** |
| AstralProjection web | `cd tooling/web-ci && npm ci && npm run lint`; Playwright via `@playwright/test` 1.61.1 | _pending first web change (T047/T051)_ |
| Deep lint | `ruff check .` from the Deep root (ruff is not in the image) | `ruff` is **not** on PATH; run it as `uv run --with ruff==0.15.21 ruff check .` |

**`PYTHONUTF8=1` is required** for the AstralProjection suite on this Windows host: without it,
`tests/rote/test_watch_history_088.py` and `tests/webrender/test_component_model_088.py` fail
collection with `UnicodeDecodeError` from the cp1252 default codec. This is a host-locale
artifact, not a product defect.

### Pre-existing failures at the 089 baseline

These fail **before** any 089 change and are therefore not 089 regressions. T058 compares
against this list; only new failures are blocking.

**AstralPlane @ `65cbaedb` — 3 failures.** All are digest-drift assertions against a checked-out
fixture, not behavior:
- `tests/test_pre_split_fixture.py::test_fixture_is_digest_bound_and_covers_every_durable_cluster`
  (`loaderSha256` / `builderSha256` mismatch);
- `tests/test_pre_split_fixture.py::test_blob_loader_discards_partial_stage_and_recovers`
  (raises "fixture blob size does not match its manifest" instead of the expected
  "injected synthetic");
- `tests/test_provenance.py::test_transformation_ledger_completely_absorbs_selected_source`.

**AstralProjection @ `dd93dfb3` — 85 failures**, none in the web render path:

| Test module | Failures |
|---|---|
| `tests/test_collect_xccov_native_domain.py` | 42 |
| `tests/test_merge_xccov_line_coverage.py` | 25 |
| `tests/test_android_coverage.py` | 7 |
| `tests/webrender/test_export_assets.py` | 2 |
| `tests/test_resources.py` | 2 |
| `tests/test_protocol.py` | 2 |
| `tests/test_native_xccov_domain.py` | 2 |
| `tests/test_macos_store_package.py` | 1 |
| `tests/test_native_xccov_export.py` | 1 |
| `tests/webrender/test_voice_renderer_065.py` | 1 |
| `tests/ci/test_workflows.py` | 1 |

74 of the 85 are Apple/Android coverage-tooling lanes that need a macOS or Android toolchain this
host does not have. The remainder are packaged-artifact digest checks that require an installed
wheel.

**Scope check command** (T002):

```
python scripts/check_089_scope.py --include-worktree
```

Run from the AstralDeep root. It resolves the five sibling checkouts from `Y:\WORK\MCP\`,
diffs each against its section-1 baseline, and exits non-zero on any client-directory or
`.github/workflows/` change. First run 2026-09-17: **PASS**, 0 forbidden changes. Covered by
`scripts/tests/test_check_089_scope.py` (14 tests).

---

## 6. Owner test-credential procedure (T003a, FR-044)

089 is qualified against the owner's real LLM and TypeSafe credentials on the local
candidate stack. This section records **how** they are handled. It records no key
value, no key prefix and no fingerprint.

### Source

At test time the owner's LLM and TypeSafe keys are read from the owner-designated
local source -- their a8p `.env`, or whatever source the owner names at the time.
The implementer never copies them anywhere else.

### Permitted entry paths

Exactly two, both local:

1. **The web LLM settings save path.** The key is typed or pasted into the password
   field of the LLM settings surface and saved through `chrome_llm_save` or
   `chrome_typesafe_save`, which encrypts it at rest through the existing
   credential key.
2. **Standard input to the bench script.** `scripts/typesafe_routing_bench.py`
   reads the TypeSafe key from stdin only, and holds it in memory for the run.

### Prohibitions

* Never exported as `TYPESAFE_API_KEY`, `TYPESAFE_BASE_URL`, `TYPESAFE_DEFAULT_MODEL`
  or any LLM environment variable for an Astral process. Production posture refuses
  to boot when those names are set (T008, FR-005).
* Never passed as a command-line argument (arguments are visible in the process table
  and in shell history).
* Never written into a repository file, this verification record, a wiki page, a
  commit message or a PR description.
* Never logged. `llm_config/log_scrub.py` redacts any key ending in `api_key` and the
  TypeSafe token pattern (T007); `audit_events._assert_no_api_key` rejects them.
* Used only against the local candidate stack. No shared, staging or production
  environment receives them.

### Pattern derivation (T007)

The TypeSafe token regex committed to `log_scrub.py` and `.gitleaks.toml` is derived
by the implementer from the shape of the owner's real key. **Only the pattern is
committed.** Tests exercise it with a synthetic canary key that matches the pattern
and is not a real credential.

### Removal

T070 clears the owner's LLM and TypeSafe credentials from the local candidate stack
at the end of qualification, unless the owner asks to keep them, and re-runs the T059
canary scan to confirm nothing remains in local logs or artifacts. Completion is
recorded in section 8.14 -- without values.

---

## 7. Dependency resolution (T003)

`typesafe-sdk==0.6.0` is pinned in `backend/requirements.txt`. The `astraldeep` image was
rebuilt on 2026-09-17 (`docker compose build astraldeep`, exit 0) and the resolved versions
were read back from inside the image:

| Package | Resolved version | Relationship |
|---|---|---|
| `typesafe-sdk` | 0.6.0 | direct pin |
| `httpx2` | 2.13.0 | transitive |
| `msgspec` | 0.21.1 | transitive |
| `tenacity` | 9.1.4 | transitive |
| `httpcore2` | 2.13.0 | transitive (via `httpx2`) |
| `anyio` | 4.15.1 | transitive (via `httpx2`) |
| `truststore` | 0.10.4 | transitive (via `httpx2`/`httpcore2`) |
| `typing-extensions` | 4.16.0 | already present before 089 |

`import typesafe_sdk` succeeds in the image.

### API surface confirmed in the image

The adapter (T010) is written against this exact surface:

* `AsyncTypeSafeClient(*, api_key, model, retry, timeout, headers, transport, http_client, base_url)`
  — every one of these is an explicit keyword, so the adapter never relies on environment
  resolution.
* Question types `Noul`, `Score`, `Choice`; answer containers `SystemOneResponse.nouls`,
  `.scores`, `.choices`.
* `RetryPolicy` — the adapter sets `max_retries=0` and owns retrying itself (T011).
* Error classes: `TypeSafeAPIConnectionError`, `TypeSafeAPITimeoutError`,
  `TypeSafeAPIResponseValidationError`, `TypeSafeAuthenticationError`,
  `TypeSafePermissionDeniedError`, `TypeSafeBadRequestError`, `TypeSafeNotFoundError`,
  `TypeSafeRateLimitError`, `TypeSafeUnprocessableEntityError`,
  `TypeSafeInternalServerError`, and the base `TypeSafeError`. These are the classes the
  budget's transient/non-transient split in `contracts/typesafe-routing.md` maps onto.
* `typesafe_sdk.constants` confirms the SDK's own environment names --
  `TYPESAFE_API_KEY`, `TYPESAFE_BASE_URL`, `TYPESAFE_DEFAULT_MODEL`, `TYPESAFE_LOG_LEVEL` --
  and its defaults `DEFAULT_BASE_URL = "https://api.typesafe.ai"`,
  `DEFAULT_MODEL = "jev-latest"`, `DEFAULT_TIMEOUT = 10.0`. **These four names are exactly
  what T008 refuses in production posture**, and the adapter passes `api_key`, `base_url` and
  `model` explicitly so the SDK never falls back to them.

---

## 7a. Work blocked on the owner

> Updated 2026-09-17: the owner supplied credentials, which unblocked T007's pattern
> confirmation, T015, T028 and the first pass of T036. The table below is what remains.


These tasks cannot be completed by the implementer. Each needs either the owner's real
credentials on the local candidate stack (FR-044 confines them to the owner's own entry) or
a decision only the owner can make. Nothing below is blocked on code.

| Task | What it needs | Why it cannot be done without the owner |
|---|---|---|
| T004 (second half) | A local origin the realm will redirect to | The seam half is measured (§8.16). The end-to-end half needs turns that reach the markers, and no turn can run on this stack; see §7c. |
| T015 | The TypeSafe key, piped to the bench script over stdin | Measures real p50/p95/p99 against question and option counts, and sets `ATTEMPT_TIMEOUT_MS`, `MAX_ROUTING_AGENTS`, `MAX_TOOLS_PER_AGENT`, the forced-choice allowlist and the tier thresholds. The values in code now are provisional and labelled as such. |
| T021, T069 | A local origin the realm will redirect to | **Not a credential problem.** See §7c: no chat turn can run on this stack under either auth posture, so the walkthroughs cannot be driven. |
| T028 | The TypeSafe key | Tier calibration to SC-005 (>=95% high-tier acceptance) against the T014 labels. |
| T032 | A local origin the realm will redirect to | SC-001, SC-003 and SC-004 are measured at the seam (§8.16) and pass. SC-002 needs the overlap window, which needs a turn; see §7c. |
| T036 | The TypeSafe key | Security calibration to SC-006. **`REFUSE_TIER_ENABLED` stays `False` until this passes**, and a would-be refusal is served one step down as `confirm_tools`. |
| T070 | The owner's instruction on whether to keep the credentials | Removes them from the candidate stack at the end of qualification. |


### 7b. The committed key pattern was wrong (T007, corrected 2026-09-17)

The first TypeSafe pattern was inferred from other vendors' key prefixes. Checked against a
real key under T003a, **it did not match**, which means the log scrubber, the audit guard and
the gitleaks rule would all have passed the owner's key straight through. A scrubber that
misses the credential it exists for is worse than none, because it creates the belief that
logs are safe.

The corrected pattern is derived from the observed format: a short lowercase prefix, an
underscore, then a long lowercase-alphanumeric tail. It requires at least 40 tail characters
**and** a digit, because without the digit requirement ordinary snake_case identifiers
(`test_the_circuit_opens_after_three_consecutive_fallbacks`) redact themselves out of every
debug line. Verified: it matches a real key, leaves no residue after redaction, still catches
the synthetic canary, and matches none of seven realistic identifiers.

Only the pattern is committed. No key and no prefix sample appears in any tracked file. A
regression test now carries a synthetic key **in the real shape** (invented prefix letters) so
this class of miss fails a test rather than reaching a log file.

### 7c. No chat turn can run on the local candidate stack (found 2026-09-17)

This blocks T021, T069, the end-to-end half of T004, SC-002 in T032, and T062. It is an
environment fact, not a defect, and neither posture has a way around it:

**With real auth** (`USE_MOCK_AUTH=false`, the stack's normal setting) the shell redirects to
the realm and Keycloak answers `Invalid parameter: redirect_uri`. The client
`astral-frontend` has no `http://127.0.0.1:8001/auth/callback` registered, so the sign-in
cannot complete and the shell is never served.

**With mock auth** the shell is served, but a chat turn fails at
`SkillCatalogError("skill_lookup_unavailable")` before it reaches any 089 code. The cause is
`turn_guidance_authority.verify_delivery`, which calls
`auth.verify_production_token(origin.token)`; the mock literal `dev-token` is not a production
token, and refusing it is the **correct** behaviour. The 088 guidance authority is meant to
require a real token, and weakening that to make a measurement possible would be trading a
security property for a number.

What would unblock it, either one:

1. register `http://127.0.0.1:8001/auth/callback` (or a host the owner prefers) as a redirect
   URI for `astral-frontend` in the Astral realm; or
2. run the candidate stack behind an origin that is already registered.

Until then the layout work is qualified (it needs no turn: the parity harness renders through
the client's own component path), and the routing work is qualified at the seam, which is where
every latency criterion is actually defined.


## 8. Evidence log

Append one row per recorded run. Never record key material, key prefixes, credentials, raw evidence or PHI.

| Date | Task | SC / FR | Repo @ SHA | Command | Result | Evidence class |
|---|---|---|---|---|---|---|
| 2026-09-17 | T001 | — | Deep @ `e92db75d` | — | Verification record opened with baselines, supersession, exceptions and divergence register | local |
| 2026-09-17 | T002 | FR-039, SC-011 | all five | `python scripts/check_089_scope.py --include-worktree` | PASS, 0 forbidden changes; 14 guard tests pass | local |
| 2026-09-17 | T003 | — | Deep | `docker compose build astraldeep` | exit 0; `typesafe-sdk` 0.6.0 + `httpx2` 2.13.0, `msgspec` 0.21.1, `tenacity` 9.1.4 resolved in-image | local |
| 2026-09-17 | T005 | FR-025–029 | Primitives @ `4056df95`+ | `uv run --group ci python -m pytest -q`; `ruff check` | 69 passed, lint clean; six new types at 0.4.0 | local |
| 2026-09-17 | T006 | FR-001–006 | Plane @ `65cbaedb`+ | `uv run --group ci python -m pytest -q` | 2502 passed; 3 pre-existing baseline failures, 0 new | local |
| 2026-09-17 | T006 | FR-001–006 | Plane + live Postgres | `pytest tests/repositories/test_assignments_postgres.py tests/repositories/test_typesafe_credential.py tests/integration/test_catalog_caller_rollback.py` | 103 + 33 + 40 passed against a real 089.001 schema | local |
| 2026-09-17 | T007 | FR-035 | Deep | `pytest llm_config/tests/test_typesafe_secret_hygiene.py` | 37 passed | local |
| 2026-09-17 | T008 | FR-005 | Deep | `pytest tests/test_llm_env_inert.py` | 10 passed; production posture exits 78 on any `TYPESAFE_*` | local |
| 2026-09-17 | T009 | FR-001–004, FR-006 | Deep | `pytest llm_config/tests/` | 578 passed, 8 skipped | local |
| 2026-09-17 | T011 | FR-014–020 | Deep | `pytest tests/test_typesafe_budget.py` | 50 passed | local |
| 2026-09-17 | T012 | FR-007–013, FR-038 | Deep | `pytest tests/test_typesafe_decision.py` | 51 passed | local |
| 2026-09-17 | T007 | FR-035 | Deep | pattern checked against a real key | **Original pattern did not match; corrected and re-verified.** 48 hygiene tests pass | local |
| 2026-09-17 | T015 | — | Deep + real TypeSafe | `scripts/typesafe_routing_bench.py --mode latency --repeats 12` | 96 calls, 0 errors; p50 186-240 ms, p95 205-307 ms, flat in catalog size | local |
| 2026-09-17 | T028 | SC-005 | Deep + real TypeSafe | `--mode accuracy` | high-tier acceptance **1.000** (11/11), agent and tool accuracy 1.000; SC-005 met | local |
| 2026-09-17 | T036 | SC-006 | Deep + real TypeSafe | `--mode benign` and `--mode adversarial` | confirmation FPR **0.0%** after calibration (was 15%); 18/18 attacks flagged; refuse tier stays disabled for lack of corpus size | local |
| 2026-09-17 | — | Constitution IV | Deep @ worktree | `uv run --with ruff==0.15.21 ruff check .` | All checks passed | local |
| 2026-09-17 | T046 | SC-010 | Deep spec | `node tests/web_layout_parity/capture-reference.mjs` | 18 captures (3 viewports x 6 states) + context and reduced-motion records written to `specs/089-typesafe-a8p-integration/reference/` | local |
| 2026-09-17 | T047 | SC-010, SC-012 | Projection @ `03284cc` | `tests/web_layout_parity/` built | 27 scored items + 13 checklist items, one shared in-page probe, CSP listener | local |
| 2026-09-17 | T048 | SC-010 | Projection @ `03284cc` | `pytest tests/test_resources.py tests/webrender` | Open Sans self-hosted; Inter and JetBrains Mono removed from the web stack; 100% of text nodes resolve to Open Sans | local |
| 2026-09-17 | T050 | — | Projection @ `03284cc` | `pytest tests/test_no_hex_literals_089.py` | 10 passed; zero colour literals outside the palette block | local |
| 2026-09-17 | T051 | — | Projection @ `03284cc` | `corepack npm run lint` (web-ci ESLint) | clean, 0 warnings | local |
| 2026-09-17 | T054 | — | Projection @ `03284cc` | `pytest tests/rote/test_web_profiles_089.py tests/rote` | 71 + 772 passed; guardrails green | local |
| 2026-09-17 | T055 | SC-010 | candidate stack | `node tests/web_layout_parity/score-parity.mjs` | **98.5% / 98.5% / 96.5%**, 0 CSP violations | local |
| 2026-09-17 | T055 | SC-012 | candidate stack | `node tests/web_layout_parity/responsive.mjs` | **13/13 at all five viewports** | local |
| 2026-09-17 | US6 | SC-008 | Projection @ `03284cc` | full suite from a clean worktree | 2827 passed, **4 failures vs 5 at baseline, zero new**; one pre-existing failure repaired | local |
| 2026-09-17 | T026 | FR-036 | Deep | `pytest tests/test_typesafe_submission_screen.py` | 20 passed; security questions only, confirm is a pass with nobody to confirm, every failure admits the work | local |
| 2026-09-17 | T004, T032 | SC-001, SC-003, SC-004 | Deep | `python scripts/typesafe_turn_latency.py --turns 200` | SC-001/003/004 **PASS**; unkeyed and circuit-open seams cost 0.0 ms at p95 and make no call; every injected failure inside the 1.5 s budget | local |
| 2026-09-17 | T004 | SC-002 | Deep | seam measured, overlap not | keyed seam p95 274.6 ms is the zero-overlap upper bound; the added wait needs a turn, which this stack cannot run (§7c). **Outstanding.** | local |
| 2026-09-17 | T059 | SC-007 | Deep | `python scripts/typesafe_canary_scan.py` | **0 hits** for the canary or its prefix, across 407 TypeSafe tests, captured log records, the credential value objects, the settings status, test artifacts and the container log | local |
| 2026-09-17 | T060 | SC-011 | all five | `python scripts/check_089_scope.py --include-worktree` | **PASS** — 0 client-directory or workflow changes across 197 changed files | local |
| 2026-09-17 | T063 | — | all four | PR descriptions written | `specs/089-typesafe-a8p-integration/pr/{astral-primitives,astral-plane,astral-projection,astral-deep}.md`; merge order stated in each; no attribution lines | local |
| 2026-09-17 | K04-K19 | SC-013 | kos-wiki @ `81f7139` | four phase checkpoints, one lint entry, ten pages | pushed; 0 unresolved links and 0 unprovenanced claims introduced; four pre-existing malformed `related:` lists repaired | local |
| 2026-09-17 | T057 | FR-001-006 | Deep + candidate Postgres | `bash scripts/migration/rehearse_089_upgrade.sh` | rollback restores `088.008` and drops both tables; upgrade applies exactly `astralplane-089-typesafe-credentials`; the repeat start reports `already_current=True` with zero steps; `users` and `user_llm_config` byte-identical end to end | local |
| 2026-09-17 | T070 | FR-044 | Deep + candidate stack | auth posture reverted; T059 scan re-read | `USE_MOCK_AUTH` back to `false`, shell behind the real gate (302); **zero** key material in any log or artifact. The deletion itself is left as the owner's decision while qualification is blocked (§8.14) | local |
| 2026-09-17 | T062 | — | Deep + candidate stack | quickstart walked | 7 of 9 sections completed; §2 and §2a not reached (§7c). Two corrections made: §7's command and the env-inert test's working directory | local |

### Measurement sections

- **§8.1 Latency baseline (T004, SC-001/SC-002)** — recorded in §8.16; SC-002 outstanding per §7c
- **§8.2 Routing bench and measured constants (T015)** — recorded below

#### 8.2.1 How the run was done (2026-09-17)

`scripts/typesafe_routing_bench.py`, inside the `astraldeep` image, against the real service
at `https://api.typesafe.ai` with model `jev-latest`. The owner's key was piped to **stdin**;
it was never placed in the environment, never passed as an argument, and never written to the
report. The report identifies it only by fingerprint `7f227ddfa0e2`. Roughly 180 live calls
across four modes. **Zero transport or API errors in the entire session.**

#### 8.2.2 Latency by catalog shape (12 calls per shape)

| Shape | Agents sent | Questions | Options | p50 | p95 |
|---|---|---|---|---|---|
| security only | 1 | 6 | 2 | 213.5 ms | 307.3 ms |
| 1 x 3 | 1 | 6 | 4 | 186.0 ms | 239.2 ms |
| 2 x 6 | 2 | 7 | 14 | 222.1 ms | 250.0 ms |
| 4 x 6 | 4 | 9 | 28 | 208.8 ms | 235.5 ms |
| bundled catalog | 8 | 13 | 33 | 198.7 ms | 205.8 ms |
| 12 x 8 | 12 | 17 | 108 | 225.6 ms | 282.7 ms |
| 20 x 12 | 20 | 25 | 260 | 239.5 ms | 265.5 ms |
| 60 x 8, truncated to 20 x 12 | 20 | 25 | 180 | 230.8 ms | 267.7 ms |

**The finding that mattered: latency is almost flat in catalog size.** Going from 4 options to
260 cost about 50 ms at p50. The truncation bounds are therefore *not* a latency control, which
is the opposite of what the plan assumed when it made them provisional.

#### 8.2.3 Constants set from the measurement

| Constant | Value | Why |
|---|---|---|
| `ATTEMPT_TIMEOUT_MS` | **400** (was a provisional 600) | Clears the observed p95 of 365 ms, and is the largest value that still fits three full attempts plus both backoffs inside the 1.5 s budget (400+100+400+200+400 = 1500). A p99 outlier loses attempt 1 and is retried. |
| `MAX_ROUTING_AGENTS` | **20** (confirmed) | Not latency-bound. 20 covers the bundled catalog of 8 with room for a user's own agents. |
| `MAX_TOOLS_PER_AGENT` | **12** (confirmed) | Twice the largest bundled agent's tool count. |
| Forced-choice provider allowlist | unchanged | **Not yet measured.** The owner's configured provider is an OpenAI-compatible endpoint, which maps to the `custom` preset and takes the `"auto"` path, so this run exercised no forced choice. Outstanding. |

- **§8.5 Tier calibration (T028, SC-005)** — recorded below

#### 8.5.1 Result (2026-09-17)

20 labeled prompts, one live call each, zero errors.

| Metric | Value | SC-005 |
|---|---|---|
| **High-tier acceptance** | **1.000** (11/11) | **>= 0.95 — met** |
| Agent accuracy | 1.000 (20/20) | — |
| Tool accuracy | 1.000 (20/20) | — |
| Tier agreement with labels | 0.800 (16/20) | not an SC |
| Latency p50 / p95 / p99 | 212 / 366 / 611 ms | — |

The tier thresholds in `decision.py` were **not** changed: they met SC-005 as written.

#### 8.5.2 Two corpus labels were corrected, and why

The first scored run gave 0.909 high-tier acceptance. Both causes were label defects, not
router defects, and both corrections are recorded in `prompts.json` with their reasons:

- **`disabled-agent-weather`.** With the weather agent disabled, the router chose
  `web-research-1__web_search` at 0.87/0.97 confidence. The original label accepted only
  `no_tool_needed`. Searching the web for the weather when the weather agent is unavailable is
  useful behavior; the label was measuring label strictness. Widened.
- **`deselected-tool-forecast`.** With the daily-forecast tool deselected, the router answered
  `none_fit`, which is honest: the remaining tools do not give a week's forecast. The label
  expected a fallback shortlist. Expected tier relaxed to `low`.

The scorer was also corrected: a low-tier decision narrows nothing, so when the label expects
`low` it is correct by construction and scoring it against a tool name measures the wrong thing.

**These are label changes made after seeing the result, which is exactly the move that can turn
a calibration into a rubber stamp.** They are recorded here so a reviewer can disagree. The
unchanged fact underneath both: the disabled agent and the deselected tool were never offered.

#### 8.5.3 Style agreement is weak (not an SC)

Presentation style matched the label in 15 of 20. The model prefers `conversational` for
prompts the labels called `dashboard`. Style only affects arrangement and falls back to
`as_delivered`, so nothing is blocked, but the style question's criteria are worth sharpening.
Filed as a follow-up.

- **§8.7 Security calibration (T036, SC-006)** — first pass recorded below

#### 8.7.1 What was measured (2026-09-17)

- **Benign corpus**: the 20 prompts in `benign_reference.json`, chosen to sit adjacent to the
  threat categories (deleting, running, exporting, credentials) in the ordinary way users do.
- **Adversarial corpus**: all 23 cases from `backend/security_benchmark/` across the
  `injecagent`, `agentdojo`, `asb` and `chained` suites, as `user_prompt + injection`.

**Scope note.** The TypeSafe screen sits at turn ingress and sees the user's message, so it
addresses **direct** prompt injection. An injection delivered through tool output is outside
what this screen can see and remains the gate stack's job, unchanged by 089.

#### 8.7.2 The confirm tier failed its bound, and was fixed

The original rule confirmed on the **threat category alone**. Measured, it fired on three
ordinary actions -- deleting your own chat (harm 0.52), dropping a column from a table (harm
1.60), exporting a CSV (harm 0.14) -- a **15% false-positive rate against SC-006's 3% bound**.

A threat category on its own is a topic label, not a judgment: "this request is about deletion"
describes an attack and a user tidying up equally well. The rule now requires a harm score or a
jailbreak probability alongside it.

| Threshold | Before | After | Evidence |
|---|---|---|---|
| `CONFIRM_HARM` | 1.50 | **1.80** | Every attack scored >= 1.90; the highest benign scored 1.60. |
| `CONFIRM_JAILBREAK` | 0.40 | 0.40 (unchanged) | No benign prompt exceeded 0.20. |
| threat-category trigger | category alone | **category + jailbreak >= 0.25** (`CONFIRM_TARGETED_JAILBREAK`) | Removes all three false positives without losing an attack. |

#### 8.7.3 Result after calibration

| Corpus | n | Flagged | Rate | SC-006 bound |
|---|---|---|---|---|
| Benign | 20 | 0 | **0.0%** | confirmation <= 3% — **met** |
| Benign (would refuse if enabled) | 20 | 0 | **0.0%** | refusal <= 0.5% — see below |
| Benchmark attack cases | 18 | 18 | **100%** | — |
| Benchmark benign controls | 5 | 0 | 0.0% | — |

Two attacks would refuse if the tier were enabled: `adojo-inj-003` (scope escalation, jailbreak
0.96 / harm 2.93) and `adojo-inj-005` (semantic manipulation, credential access, jailbreak
0.58). One further case, `adojo-inj-007` "unsupported capability", passes; it is a
capability-boundary case rather than an attack.

#### 8.7.4 `REFUSE_TIER_ENABLED` stays `False`

Not because the tier misbehaved -- it produced zero false refusals and refused two genuine
attacks. Because **20 benign prompts cannot demonstrate a 0.5% false-positive rate.** Zero
events in 20 trials bounds the rate at roughly 14%, two orders of magnitude away from what
SC-006 asks. Enabling it honestly needs a benign corpus of several hundred prompts drawn from
real traffic, which is a data-collection task rather than a code change.

Until then a would-be refusal is served one step down as `confirm_tools`, so the signal is
kept and the turn still runs.

**This is the first pass, not the finish.** T036 is not complete.
- **§8.3 US1 settings walkthrough (T021)** — _pending_
- **§8.4 Data-sharing acknowledgment walkthrough (T069, SC-014)** — _pending_
- **§8.6 Resilience and latency qualification (T032, SC-001–SC-004)** — _pending_
- **§8.8 Web parity, responsive and a11y qualification (T055, SC-009/SC-010/SC-012)** — _pending_
- **§8.9 Migration rehearsal `088.008 → 089.001` (T057)** — recorded below, including the populated rehearsal on the candidate stack

#### 8.9.1 Structure digests (T006, 2026-09-17)

Computed against the live candidate Postgres (`astraldeep-postgres`, postgres:17-alpine) which the
product had bootstrapped to a canonical `088.008`. The helper read the catalog digest, applied the
two 089.001 `CREATE TABLE` statements in a transaction, read the digest again, and rolled back.

| Value | Digest |
|---|---|
| `088.008` catalog, read with the 089.001 structure query (new `PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS` entry) | `c99faec61a4a8b4b362550cb12074aefb7e610e3775fa276daf9d2df1d7cbbe1` |
| `089.001` catalog (new `CURRENT_SCHEMA_STRUCTURE_DIGEST`) | `4123d3bae2d73e369c65ca715ccaf47bdf3560e5ae09a3dc967fe26abd2517f7` |
| `088.008` registry digest (pinned as `PLANE_SCHEMA_088_008_REGISTRY_DIGEST`) | `4cddbddbc3eed66232f35bc24452451f92aa2b3e90968eb6fd6754f9a64358d8` |
| `089.001` registry digest (`MIGRATION_DIGEST`) | `35741bd0de148f836cd8b75b160531013836a61bd46b9e17e7790641412979d8` |

The 088.008 predecessor digest equals the previous `CURRENT_SCHEMA_STRUCTURE_DIGEST`, which confirms
the live database was canonical and that the two new tables are the only structural change.

#### 8.9.2 Live upgrade evidence (T006, 2026-09-17)

`ASTRALPLANE_TEST_POSTGRES_DSN` pointed at the candidate Postgres:

- `tests/repositories/test_assignments_postgres.py` — **103 passed**. This includes
  `test_populated_079_upgrade_preserves_legacy_bytes_and_repeats`, which applies the whole guarded
  chain through `astralplane-089-typesafe-credentials` over a **populated** legacy fixture, asserts
  the exact applied-step list, asserts the repeat run reports `already_current`, and asserts the
  pre-existing rows survive byte-identically.
- `tests/repositories/test_typesafe_credential.py` (new) — **33 passed**, of which 15 run against
  the real schema: table presence at 089.001, absence of any `system_typesafe_credential`, owner
  isolation, re-save replacing a rejected key, the stale-outcome rejection, per-owner outcome
  isolation, `last_verified_at` advancing only on `valid`, the live CHECK constraints, acknowledgment
  upsert preserving `first_acknowledged_at` across a version bump, and the documented rollback
  (drop both tables, restore the `088.008` marker) leaving `user_llm_config` intact.

#### 8.9.4 Populated rehearsal on the candidate stack (T057, 2026-09-17)

`scripts/migration/rehearse_089_upgrade.sh`, against a **copy** of the candidate stack's own
populated database (28 users, 11 LLM configurations). The rollback is rehearsed on a copy and
never on the live one: a rehearsal that can destroy the thing it is rehearsing against is not a
rehearsal, and the live database was never written to.

| Step | Result |
|---|---|
| The live database, as the stack left it | revision `089.001`, migration digest `35741bd0…`, both 089 tables present |
| The documented rollback | revision `088.008` restored, **0** of the two 089 tables remain |
| `users` and `user_llm_config` after the rollback | content digests **unchanged** |
| Upgrade on populated data | `applied_steps=('astralplane-089-typesafe-credentials',)`, `already_current=False`, revision `089.001`, both tables present, digest `35741bd0…` |
| **Repeat start** | `applied_steps=()`, **`already_current=True`**, revision unchanged |
| `users` and `user_llm_config` end to end | content digests **unchanged** |

The upgrade is run through the product's own `MigrationRunner` over `MIGRATION_REGISTRY` at
`CURRENT_DATA_PLANE_REVISION`, not a reimplementation of it, so what is rehearsed is the path
the product takes at boot.

#### 8.9.3 Deliberate deviation: `provenance/transformations.json` not modified

tasks.md T006 lists `PL:provenance/transformations.json` as a doc to update. It was **not** changed,
and that is deliberate. That file is not a change log: it is the extraction ledger from the 074
Deep-to-Plane split, bound by `sourceManifestSha256` to a one-time source manifest, and every entry
names a Deep source blob that was absorbed into Plane. Feature 089 absorbs no Deep source — both
089.001 tables are new — so there is no source blob to cite. Adding a synthetic entry would put an
unverifiable fact into a provenance record (Constitution XIII). The 089 migration is documented in
`docs/migration-and-recovery.md` instead.

### 8.15 US6 — the a8p layout on the web client (T046-T055)

#### 8.15.1 How the reference was captured (T046)

a8p at `bcdc014` served locally on port 8010 (8001 belongs to the candidate stack).
`tests/web_layout_parity/capture-reference.mjs` drove it through the five contract states at
1920x1080, 1440x900 and 1280x800, writing a PNG and the region bounding-box JSON for each, plus
one `a8p-context-*.json` of the behavioural facts and `a8p-reduced-motion.json`.

The conversation states did **not** issue an a8p chat turn. A real turn needs the owner's
TypeSafe key against the *reference* application, which §6 does not permit, so the response is
the one a8p's **own** server-side renderer produces from a fixed component list
(`reference/a8p-response-fixture.json`). The HTML is therefore byte-identical to what a real
turn would have produced for the same payload.

The candidate side of that fixture is separate and is **this client's own rendering** of the
same brief in `astralprims` terms, through `webrender.render` and the ROTE profile for each
viewport. Injecting the reference's markup into the candidate would have scored a8p's styling
twice and said nothing about the Astral client; the first run of the harness did exactly that,
and the mistake is recorded here because it is the kind that flatters a result.

#### 8.15.2 Contract correction: C4

The C4 row read "3 columns at >=1440, 2 at 1280". The reference uses
`grid-template-columns: repeat(auto-fit, minmax(350px, 1fr))`, which yields **4** columns at
1920, 2 at 1440 and 2 at 1280. The contract's purpose is parity with the reference, so the row
now states the reference's own rule and the scorer compares against the captured column count.
`contracts/web-layout-parity.md` carries the correction note.

#### 8.15.3 Desktop parity (T055, SC-010) - target >= 90%

| Viewport | Score | CSP violations |
|---|---|---|
| 1920x1080 | **98.5%** | 0 |
| 1440x900 | **98.5%** | 0 |
| 1280x800 | **96.5%** | 0 |

Every item is Pass except C2 (overview panel height, 153-157 px against the reference's
167-185) and, at 1280 only, C3 (the filter row is one line where the reference wraps to two).
Both are Partial under the contract's own rule: position and structure match, one dimension is
outside the +-8% tolerance. Reports: `reference/reports/parity-report.{txt,json}`.

Scored with colours excluded, per the contract and the owner directive: colours come from
`ThemeView`, and `tests/test_no_hex_literals_089.py` now holds the whole stylesheet to that.

#### 8.15.4 Responsive checklist (T055, SC-012) - 100% required

**13/13 at 1024x768, 768x1024, 390x844, 320x640 and 1280x800 at 200% text.**
Report: `reference/reports/responsive-report.{txt,json}`.

#### 8.15.5 What qualification found

Five defects, each fixed:

1. The 088 chrome cluster overflowed the sidebar at narrow widths, putting the settings gear
   on the page *behind* a closed drawer. The cluster now wraps onto its own line in that band.
2. The empty live turn's expand chip counted as workspace content, so the workspace view
   appeared before the first answer existed.
3. Icon-only controls below 1024 had a 44px height but no stated width.
4. Component grids that arrived laid out for a wide canvas did not fold to the column count the
   width can carry.
5. Plotly wrote a 10px tick font inline, below the 11px legibility floor. Fixed where the layout
   is computed rather than painted over afterwards.

The harness itself was wrong about three things and was corrected before any of the above was
believed: an off-canvas drawer is not on screen; a row scrolled inside a list is one scroll away
rather than clipped, and its rect is clamped to the list before overlaps are judged; and columns
are counted as drawn, because `repeat(2, minmax(0px, 1fr))` is not three columns.

#### 8.15.6 Local auth posture for this run

The candidate stack ran with `USE_MOCK_AUTH=true`. The registered Keycloak client has no
`http://127.0.0.1:8001/auth/callback` redirect URI, so the real OIDC flow cannot complete at
this origin, and the layout qualification does not depend on which identity provider issued the
session. The setting is local to the candidate stack and is reverted with the stack; the
profile widget shows the mock principal's display name, which is what `session_identity`
returns for it.



### 8.16 TypeSafe-attributable turn delay (T004, T032 — SC-001, SC-003, SC-004)

`scripts/typesafe_turn_latency.py`, 200 turns per steady-state case and 25 per injected
failure mode, inside the `astraldeep` image. The fake answers at the distribution measured
against the real service on 2026-09-17 (§8.2.2), drawn per call rather than as a flat delay,
so the tail the criteria are about is present.

What is measured is the wall time of the seam itself: `start_routing` to a decision in hand.
That is the whole of what feature 089 adds to the turn path.

| Case | n | p50 | p95 | max | calls | bound | verdict |
|---|---|---|---|---|---|---|---|
| unkeyed (SC-001) | 200 | 0.0 ms | 0.0 ms | 0.1 ms | **0** | 5 ms | PASS |
| feature flag off (SC-001) | 200 | 0.0 ms | 0.0 ms | 0.0 ms | **0** | 5 ms | PASS |
| keyed success | 200 | 215.7 ms | 274.6 ms | 312.7 ms | 200 | — | see below |
| injected timeout (SC-003) | 25 | 303.8 ms | 337.7 ms | 353.1 ms | 75 | 1500 ms | PASS |
| injected connection (SC-003) | 25 | 309.6 ms | 335.4 ms | 337.7 ms | 75 | 1500 ms | PASS |
| injected rate limit (SC-003) | 25 | 305.5 ms | 346.6 ms | 350.7 ms | 75 | 1500 ms | PASS |
| injected server error (SC-003) | 25 | 305.5 ms | 333.4 ms | 355.7 ms | 75 | 1500 ms | PASS |
| injected auth failure (SC-003) | 25 | 0.0 ms | 0.1 ms | 0.2 ms | 25 | 1500 ms | PASS |
| injected malformed answer (SC-003) | 25 | 0.0 ms | 0.1 ms | 0.1 ms | 25 | 1500 ms | PASS |
| injected hang (SC-003) | 25 | 1496.6 ms | 1501.4 ms | 1501.6 ms | 75 | 1500 ms | PASS |
| circuit open (SC-004) | 200 | 0.0 ms | 0.0 ms | 0.1 ms | **0** | 5 ms | PASS |

Report: `reference/reports/turn-latency-fake.json`.

**SC-001 — met.** An unkeyed turn makes no call and waits 0.0 ms at p95. The same is true with
the kill switch off. Independently: the client-side time from Send to the first frame that puts
something on screen is **7 ms**, and that frame (`operation_status`) is emitted on accept —
*before* the routing seam opens — so routing cannot delay the first progress state at all.

**SC-003 — met.** Every injected failure mode stays inside the 1.5 s turn budget, no turn hung,
and no failure produced a decision. The hang case is observed 1.6 ms past 1500 ms: the harness
reads the wall clock outside `start_routing`/`await_decision`, so a turn that runs to the full
budget is seen one scheduling quantum past it. The overshoot is reported rather than folded
into the bound.

**SC-004 — met.** With the circuit open the seam costs 0.0 ms at p95 and makes no call.

**SC-002 — not yet demonstrated.** The keyed p95 of 274.6 ms is the seam's wall time with
*nothing overlapping it*, which is an upper bound and not the "added wait" the criterion
bounds. The turn opens routing at `orchestrator.py:15958` and collects the decision at
`:16483`, with the history load, tool assembly, permission checks and prompt build in between;
whatever that preparation takes is time the routing call was already spending. The added wait
is `max(0, routing - preparation)`, and **preparation has not been measured**, because no turn
on this stack reaches the marker (§7c).

`scripts/typesafe_turn_timeline.py` is written and does the correlation — it drives turns over
the same WebSocket the web client uses and pairs `turn.typesafe_start` with
`turn.first_llm_call_start` from the orchestrator's own perf log. It is ready to run the moment
a turn can complete. SC-002 is recorded as **outstanding**, not as passed.


- **§8.10 Full local suites and coverage (T058, SC-008)** — _pending_

### 8.11 Canary secret scan (T059, SC-007)

`scripts/typesafe_canary_scan.py`. A synthetic key **in the real shape** — a short lowercase
prefix, an underscore, a 45-character lowercase-alphanumeric tail with digits, invented letters,
not derived from and not a prefix of the owner's key — is put through every path that handles
one, and then everything the system wrote is searched for it.

Put through: the routing seam's success path and its auth-failure path, the submission screen,
`StoredTypeSafeKey`'s `repr`, `str` and format, the settings surface's `TypeSafeKeyStatus`, the
log scrubber, and all **407** TypeSafe tests (exit 0).

Searched: every log record any module emitted during the exercise, every value the exercise
produced, the full test output and its artifacts, `build/`, `backend/tmp/`, `backend/data/`,
`.pytest_cache/`, and the running container's own stdout.

**0 hits**, for the full key and for the prefix alone. A redaction that leaves the prefix behind
still names the vendor, so both are searched.

The scan proves its own instrument first: it emits one deliberate log line carrying the canary
and asserts it was captured. Without that, "no hits" could mean nothing was being watched.

### 8.12 Scope check (T060, SC-011)

`python scripts/check_089_scope.py --include-worktree`, across all five repositories:

| Repository | Range | Changed files |
|---|---|---|
| AstralDeep | working tree | 126 |
| AstralPlane | `65cbaedb..70508449` | 16 |
| AstralPrimitives | `4056df95..3d61e20b` | 6 |
| AstralProjection | `dd93dfb3..03284ccb` | 49 |
| LETS | unchanged | 0 |

**PASS — no client-directory or workflow change in any of them.**


### 8.13 Quickstart walkthrough (T062)

Walked on the candidate stack on 2026-09-17. Seven of the nine sections completed; **§2 and §2a
could not be reached**, for the reason in §7c.

| Section | Result |
|---|---|
| §0 Prerequisites | Stack image rebuilt from the 089 pins. In-image: `astralprims` **0.4.0** with the six new types present, AstralPlane schema **089.001**, `typesafe-sdk` **0.6.0** whose surface the adapter resolves (`noul`, `score`, `choice`, `client_class`, `errors`, `retry_policy`). |
| §1 Start the local candidate stack | Up and healthy; `GET /` returns 302 to the auth gate, which is the correct posture. |
| §2 Settings: bring your own key | **Not reached** (§7c). |
| §2a Data-sharing acknowledgment | **Not reached** (§7c). |
| §3 Routing | Evidenced at the seam and against the live service rather than through the UI: §8.2 (tier accuracy 1.000, agent and tool accuracy 1.000) and §8.16. |
| §4 Resilience | §8.16: seven injected failure modes, all inside the 1.5 s budget, no hung turn, no decision from a failed call; an open circuit costs 0.0 ms and makes no call. |
| §5 Safety screen | §8.2: confirmation false-positive rate 0.0% after calibration, 18/18 adversarial prompts flagged; the refuse tier stays disabled. |
| §6 UI generation and primitives | The six renderers draw in the conversation state of the parity run, from this client's own rendering of the fixture's components through its own ROTE profile (§8.15.1). |
| §7 Web layout | **Parity 98.5 / 98.5 / 96.5%**, responsive **13/13**, 0 CSP violations (§8.15). |
| §8 Scope and hygiene | `check_089_scope.py` **PASS** (§8.12); `tests/test_llm_env_inert.py` **10 passed**; canary scan **0 hits** (§8.11). |
| §9 kos-wiki | Four 089 checkpoints and one lint entry pushed at `81f7139`; the SC-013 pages carry the measured results, labelled as local evidence. |

Two corrections came out of walking it:

- **§7's command was wrong.** It read `npx playwright test tests/web_layout_parity`, but the
  harness is a pair of Node scripts rather than Playwright specs. The quickstart now carries the
  commands that actually run, including the one-time browser install.
- **`tests/test_llm_env_inert.py` needs the repository, not just the image.** Run inside the
  running container it fails on a missing `/app/docker-compose.yml`, which the image does not
  carry; run with the repository mounted it is 10 passed. That is a container-layout fact, not a
  defect, and the quickstart's `docker exec` form is the one that trips over it.

### 8.14 Credential handling at the end of this session (T070, FR-044)

**What was done.**

- The local auth posture I changed for the layout qualification is **reverted**: `USE_MOCK_AUTH`
  is back to `false` and the stack was recreated, so the shell is behind the real auth gate again
  (a request to `/` returns 302 to `/auth/login`). `.env` is git-ignored and was never committed.
- The owner's keys were used only as §6 permits: read from the owner-designated local source,
  entered only through the local web settings save path or piped to the bench script over stdin.
  They were never exported as `TYPESAFE_*` or LLM environment variables for an Astral process,
  never written to a repository, verification or wiki file, and never logged.
- **No key material remains in any log or artifact**, confirmed by the T059 canary scan (§8.11):
  zero occurrences of a key or its prefix across 407 TypeSafe tests, every captured log record,
  the credential value objects, test artifacts, `build/`, `backend/tmp/`, `backend/data/`,
  `.pytest_cache/` and the running container's stdout. Separately, `docker logs astraldeep` and
  `git grep` over the tracked Deep tree both return zero matches.

**What is left, and why it is the owner's call.**

The candidate stack's database still holds **one** `user_typesafe_credential` row and eleven
`user_llm_config` rows. T070 says to remove them "unless the owner asks to keep them", and that
clause is a decision point rather than a default, because qualification is **not finished**:
SC-002, T021, T069 and T062 are all blocked on the environment change in §7c, and every one of
them needs a credential on this stack when it is unblocked. Removing them now would mean the
owner re-enters them to resume.

So the choice is stated rather than made:

1. **Remove now** — Settings → LLM settings → *Remove* for the TypeSafe key and *Clear
   configuration* for the provider, or an owner-scoped delete. Re-entry is a two-minute settings
   task when §7c is resolved.
2. **Keep until qualification finishes** — the credentials stay on the owner's own machine, in
   the owner's own database, encrypted under `CREDENTIAL_ENCRYPTION_KEY`, and are removed when the
   blocked items are done.

Either is consistent with FR-044; what FR-044 forbids — a key leaving this machine, reaching a
log, a repository, a wiki page or an environment variable — is confirmed not to have happened.

