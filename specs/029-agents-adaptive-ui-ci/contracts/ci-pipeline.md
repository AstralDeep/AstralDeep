# Contract: CI Pipeline (.github/workflows/ci.yml)

**Triggers**: `pull_request` (all branches) and `push` to `main`. Concurrency group `ci-${{ github.ref }}` with cancel-in-progress for PRs. All jobs report independently (FR-034/FR-038: failures attributable per gate).

| Job | Needs | Environment | Gate (fails when…) |
|---|---|---|---|
| **lint** | — | runner, Python 3.11, `pip install ruff` | `ruff check .` from repo root (where ruff.toml lives) reports any violation |
| **build** | — | runner, buildx + GHA layer cache | `docker build` of the production Dockerfile fails (includes the spaCy `en_core_web_lg` bake) — image exported as artifact for downstream jobs |
| **test** | build | built image; `services: postgres:17-alpine` (healthcheck-gated); checkout mounted over `/app/backend`; env `ASTRAL_ENV=development`, `DB_HOST/PORT/NAME/USER/PASSWORD` → service, test-safe `AGENT_API_KEY`/`AUDIT_HMAC_SECRET` | either pytest invocation fails: ① `python -m pytest -q -m "not integration"` (default suite, ~1265 tests) ② `python -m pytest audit/tests llm_config/tests orchestrator/tests onboarding/tests personalization/tests scheduler/tests dreaming/tests -q` (~316 tests). Both run under `coverage`/pytest-cov producing a combined `coverage.xml` (uploaded as artifact) |
| **coverage-gate** | test | runner, `pip install diff-cover`; full-history checkout (`fetch-depth: 0`) | `diff-cover coverage.xml --compare-branch origin/main --fail-under 90` < 90% on changed Python lines. **Vacuous pass** when the diff contains no measurable Python lines |
| **smoke** | build | built image + postgres service | ① dev-posture boot: `/healthz` or `/readyz` fail to answer 200 within the start budget (readyz must confirm DB). ② fail-closed proof: a production-posture run (`ASTRAL_ENV` unset, `USE_MOCK_AUTH=true`, placeholder `AUDIT_HMAC_SECRET`, no Keycloak vars) exits with a code ≠ 78 — the gate requires **exactly 78** (EX_CONFIG) |
| **secret-scan** | — | checksum-pinned official Gitleaks CLI, archive digest verified before execution, full-history checkout, no repository secret | any committed credential material detected |
| **publish** | lint, test, coverage-gate, smoke, secret-scan, build | `push` to `main` only; `permissions: packages: write`; `docker/login-action` → ghcr.io | image push fails. Tags: `ghcr.io/<owner>/<repo>:sha-<full-sha>` (immutable) + `ghcr.io/<owner>/<repo>:latest` (moving). Publish failure is its own job failure — verification gates stay green/visible (FR-038) |

## Deployment hand-off (no live deploy job — FR-039)

`docs/production-deployment.md` gains a "Deploying to sandbox.ai.uky.edu" section: pull `ghcr.io/<owner>/<repo>:sha-<sha>`, compose override using `image:` instead of `build:`, `.env` posture for the host — `PUBLIC_BASE_URL=https://sandbox.ai.uky.edu`, `BACKEND_PUBLIC_URL=https://sandbox.ai.uky.edu`, `KEYCLOAK_AUTHORITY=https://iam.ai.uky.edu/realms/<realm>` (+ client id/secret per docs/keycloak-realm-settings.md), `FORWARDED_ALLOW_IPS=<proxy ip>`, TLS reverse proxy upgrading `/ws`, registered redirect URI `https://sandbox.ai.uky.edu/auth/callback`. Production boot gate (exit 78) remains the final guard on the host.

## CI-only tooling declaration (Constitution V/XI)

`ruff`, `pytest-cov`/`coverage`, `diff-cover`, and the checksum-pinned Gitleaks CLI are pipeline-environment tools, not product dependencies; recorded here and in the PR description per Principle XI.
