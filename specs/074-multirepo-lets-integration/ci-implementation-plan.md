# Feature 074 Repository-Owned CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace migration-era and misplaced CI with locally qualified, repository-owned pull-request gates for AstralDeep, AstralProjection, AstralPlane, AstralPrimitives, and LETS, then mark the five existing PRs ready only after one clean hosted run.

**Architecture:** Each repository runs the tests for its mutable source and exposes one stable aggregate check. AstralDeep's public workflow validates Deep-owned policy and exact composition declarations without private submodule credentials; the full five-repository composition remains a mandatory local Feature 074 qualification. LETS retains fail-closed signed-tag validation and obtains the complete Git history it already requires.

**Tech Stack:** GitHub Actions; Python 3.9/3.11/3.14; uv; pytest/coverage/diff-cover/Ruff/mypy; Gitleaks 8.30.1; Node 24/npm 11.16.0/Playwright; PySide6; Gradle/Kotlin/Android emulator; Swift/Xcode 26.6; PostgreSQL 17; Docker.

**Spec:** `specs/074-multirepo-lets-integration/ci-ownership-design.md`

## Global Constraints

- Preserve Python 3.11 production compatibility in Deep, Plane, and Projection; Primitives retains its declared `>=3.9` floor and LETS retains `>=3.11,<3.15`.
- Add no third-party runtime dependency; CI-only dependencies must be locked and must not enter built product artifacts.
- Every external GitHub Action reference is a reviewed 40-character commit SHA.
- Pull-request jobs receive only `contents: read`; publication and OIDC authority never run for pull-request events.
- Do not add cross-repository secrets, repository-scoped GitHub Apps, custom token brokers, or candidate-controlled privileged jobs.
- Use exact reviewed Gitleaks fingerprints; no path-wide or rule-wide security suppression.
- Keep at least 90% coverage for changed executable product lines in the owning repository, using the established per-language producer where one exists.
- Do not merge, publish, deploy, release, sign, or submit to stores.
- Keep every branch recoverable with ordinary commits; LETS synchronizes `origin/main` by merge, never rebase/force-push.
- Push each repository only after its locally executable gates pass; then mark its existing PR ready and observe the hosted result.

## File Map

### LETS

- Modify `benchmarks/astraldeep/check_version_disposition.py`: add a fail-closed `verify-anchor` command that reuses the immutable signed-tag validator.
- Modify `tests/benchmarks/test_astraldeep_case_study.py`: construct stable integration-only candidates from the signed baseline and cover full/shallow history behavior.
- Modify `.github/workflows/ci.yml`: fetch full history for every pytest lane and run the anchor preflight before dependency sync.

### AstralProjection

- Create `.github/workflows/ci.yml`: Python, web, Windows, and aggregate owner gates.
- Create `.github/workflows/android-ci.yml`: Android build/unit/coverage/emulator and aggregate gates.
- Create `.github/workflows/apple-ci.yml`: Apple format/package/app/watch tests and aggregate gate.
- Create `tests/ci/test_workflows.py`: structural owner/workflow security contract.
- Modify `tests/test_protocol.py` and `tests/release/test_windows_bridge.py`: distinguish three active CI workflows from six inactive release workflows.
- Delete `workflows-disabled/ci.yml`, `workflows-disabled/android-ci.yml`, and `workflows-disabled/apple-ci.yml` after their active replacements exist.
- Modify `README.md`, `apple-clients/README.md`, and `provenance/transformations.json`: record activation and the two moved extraction paths while retaining historical checks as historical evidence.

### AstralPlane

- Create `.github/workflows/ci.yml`: quality/security, PostgreSQL, package compatibility, and aggregate gates.
- Create `tests/architecture/test_ci_workflow.py`: structural workflow contract.
- Create `tooling/python-ci/build-requirements.lock.txt`: hash-lock the isolated build backend.
- Modify `pyproject.toml` and `uv.lock`: add a locked CI-only dependency group.
- Modify `tests/test_blob_store.py`: remove an `os.scandir()` ordering assumption from the purge retry test.
- Delete `workflows-disabled/ci.yml` and update `README.md` to name the active owner workflow.

### AstralPrimitives

- Create `.github/workflows/ci.yml`: quality/package, Python compatibility, and aggregate gates.
- Create `tests/test_ci_workflow.py`: pull-request and publication authority contract.
- Create `uv.lock` and `tooling/python-ci/build-requirements.lock.txt`, and modify `pyproject.toml`: locked CI-only dependencies plus a hash-locked isolated build backend.
- Modify `src/astralprims/base.py`: remove the two imports rejected by the new Ruff gate without changing runtime behavior.
- Modify `.github/workflows/python-publish.yml`: pin actions and split unprivileged verification/build from OIDC publication.
- Modify `CLAUDE.md`: document CI and unchanged release/version semantics.

### AstralDeep

- Modify `.gitleaksignore` and `backend/tests/test_python_ci_supply_chain_060.py`: add exactly seven reviewed history fingerprints.
- Rewrite `.github/workflows/ci.yml`: retain only Deep-owned source-free jobs, exact composition declarations, secret scan, and a truthful aggregate.
- Delete `.github/workflows/android-ci.yml` and `.github/workflows/apple-ci.yml`: their active owners are Projection.
- Modify `scripts/tests/test_component_build_surfaces_074.py`, `backend/tests/test_ci_javascript_lint.py`, `backend/tests/test_python_ci_supply_chain_060.py`, `backend/tests/test_release_workflows_060.py`, and `backend/tests/test_voice_release_evidence_producers_065.py`: enforce the new ownership boundary.
- Modify `scripts/verify_release_evidence_bootstrap.py` and `backend/tests/test_release_evidence_bootstrap.py`: the Deep PR workflow inventory contains only Deep's `ci.yml`.
- Modify `docs/production-deployment.md`: mark native-client evidence production as Projection-owned and release activation as still parked.
- Modify `specs/074-multirepo-lets-integration/execution/local-ci-qualification.json`: append exact final candidate identities and results without granting merge or release authority.

---

### Task 1: Repair LETS signed-history CI without weakening the anchor

**Files:**
- Modify: `/Users/sam/Desktop/Work/LETS/benchmarks/astraldeep/check_version_disposition.py`
- Modify: `/Users/sam/Desktop/Work/LETS/tests/benchmarks/test_astraldeep_case_study.py`
- Modify: `/Users/sam/Desktop/Work/LETS/.github/workflows/ci.yml`

**Interfaces:**
- Consumes: immutable constants `BASELINE_TAG_OBJECT`, `BASELINE_COMMIT`, `BASELINE_TREE`, and `_validate_repository_anchor(repository, anchor)`.
- Produces: CLI `python -m benchmarks.astraldeep.check_version_disposition verify-anchor --repository PATH`, returning `0` only when the exact signed v1.0.10 objects exist and `2` on refusal.

- [ ] **Step 1: Synchronize the feature branch with current main without rewriting history**

```bash
cd /Users/sam/Desktop/Work/LETS
git fetch origin main
git merge --no-edit origin/main
git status --short --branch
```

Expected: an ordinary merge or fast-forward, no conflicts, and no rebase/force-push requirement.

- [ ] **Step 2: Make integration-only candidates independent of current HEAD**

Replace the body of `_candidate_clone` with the signed baseline checkout plus one integration-only commit:

```python
@contextmanager
def _candidate_clone(tmp_path: Path) -> Iterator[Path]:
    candidate = tmp_path / "candidate"
    subprocess.run(["git", "clone", "--quiet", str(REPOSITORY_ROOT), str(candidate)], check=True)
    _git(candidate, "config", "user.name", "Case Study Test")
    _git(candidate, "config", "user.email", "case-study-test@example.invalid")
    _git(candidate, "checkout", "--quiet", "--detach", versioning.BASELINE_COMMIT)
    marker = candidate / "benchmarks/astraldeep/integration-only-test.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("integration-only candidate\n", encoding="utf-8")
    _git(candidate, "add", marker.relative_to(candidate).as_posix())
    _git(candidate, "commit", "--quiet", "-m", "test integration-only candidate")
    yield candidate
```

- [ ] **Step 3: Write the failing full-history and shallow-history CLI tests**

Add tests that call the real command path, not a mocked Git helper:

```python
def test_verify_anchor_accepts_full_clone_and_rejects_depth_one_no_tags(tmp_path: Path) -> None:
    full = tmp_path / "full"
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "--quiet", str(REPOSITORY_ROOT), str(full)], check=True)
    subprocess.run(
        ["git", "clone", "--quiet", "--depth=1", "--no-tags", REPOSITORY_ROOT.as_uri(), str(shallow)],
        check=True,
    )
    assert versioning.main(["verify-anchor", "--repository", str(full)]) == 0
    assert versioning.main(["verify-anchor", "--repository", str(shallow)]) == 2
```

- [ ] **Step 4: Run the targeted tests and verify RED**

```bash
uv run --frozen pytest \
  tests/benchmarks/test_astraldeep_case_study.py::test_verify_anchor_accepts_full_clone_and_rejects_depth_one_no_tags \
  tests/benchmarks/test_astraldeep_case_study.py::test_signed_v1010_comparison_classifies_integration_only_and_runtime_changes \
  tests/benchmarks/test_astraldeep_case_study.py::test_current_candidate_validation_recomputes_signed_tree_comparison -q
```

Expected: `verify-anchor` is rejected by argparse because it is not implemented; the comparison tests expose any current-HEAD fixture dependency.

- [ ] **Step 5: Add the minimal fail-closed CLI**

Add this parser and dispatch path without fetching or fallback behavior:

```python
verify_anchor = subparsers.add_parser(
    "verify-anchor", help="verify the exact local signed v1.0.10 Git anchor"
)
verify_anchor.add_argument(
    "--repository", type=Path, default=Path(__file__).resolve().parents[2]
)
```

```python
if arguments.command == "verify-anchor":
    _validate_repository_anchor(repository, {"version": BASELINE_RELEASE})
    print("signed v1.0.10 repository anchor verified")
    return 0
```

- [ ] **Step 6: Give every pytest lane full history and fail early**

For `quality` and matrix `test`, make checkout and preflight structurally identical to:

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v6.0.2
  with:
    fetch-depth: 0
- uses: astral-sh/setup-uv@ae6289f6599a82fc94e0c245b5663c4733dc3351 # v10.0.0
  with:
    python-version: ${{ matrix.python }}
    enable-cache: true
- name: Verify the immutable signed comparison anchor
  run: uv run --no-project python -m benchmarks.astraldeep.check_version_disposition verify-anchor --repository .
```

Use literal Python `3.14` instead of `matrix.python` in `quality`; leave distributed acceptance at `fetch-depth: 0`.

- [ ] **Step 7: Run LETS local gates**

```bash
uv lock --check
uv sync --all-extras --frozen
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy src
uv run --frozen pytest tests/benchmarks/test_astraldeep_case_study.py -q
uv run --frozen pytest -m "not e2e" --cov=lets --cov-report=term-missing
uv build
actionlint -shellcheck= -pyflakes= .github/workflows/ci.yml
uv run --frozen python deploy/run_acceptance.py
```

Expected: all commands pass; the immutable constants and missing-tag negative case remain unchanged.

- [ ] **Step 8: Commit LETS locally**

```bash
git add benchmarks/astraldeep/check_version_disposition.py \
  tests/benchmarks/test_astraldeep_case_study.py .github/workflows/ci.yml
git commit -m "ci: verify signed history before LETS tests"
```

### Task 2: Activate Projection core/web/Windows CI

**Files:**
- Create: `/Users/sam/Desktop/Work/AstralProjection/tests/ci/test_workflows.py`
- Create: `/Users/sam/Desktop/Work/AstralProjection/.github/workflows/ci.yml`
- Modify: `/Users/sam/Desktop/Work/AstralProjection/tests/test_protocol.py`
- Modify: `/Users/sam/Desktop/Work/AstralProjection/tests/release/test_windows_bridge.py`
- Modify: `/Users/sam/Desktop/Work/AstralProjection/README.md`
- Delete: `/Users/sam/Desktop/Work/AstralProjection/workflows-disabled/ci.yml`

**Interfaces:**
- Produces stable job IDs `python`, `web`, `windows`, and `required`.
- Preserves six inactive release/candidate workflows under `workflows-disabled/` after Task 3 moves Android and Apple CI.

- [ ] **Step 1: Write the active/inactive workflow contract**

Create a dependency-free structural test with these core assertions:

```python
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
ACTIVE = ROOT / ".github" / "workflows"
INACTIVE = ROOT / "workflows-disabled"

def _job_ids(text: str) -> set[str]:
    jobs = text.partition("\njobs:\n")[2]
    assert jobs
    return set(re.findall(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", jobs))

def test_core_ci_is_active_read_only_and_projection_owned() -> None:
    text = (ACTIVE / "ci.yml").read_text(encoding="utf-8")
    assert _job_ids(text) == {"python", "web", "windows", "required"}
    assert "pull_request:" in text and "branches: [main]" in text
    assert "permissions:\n  contents: read" in text
    assert "if: ${{ false }}" not in text
    assert "components/AstralProjection/" not in text
    assert "id-token: write" not in text and "secrets." not in text
    for action in re.findall(r"(?m)^\s*uses:\s*[^@\s]+@([^\s#]+)", text):
        assert re.fullmatch(r"[0-9a-f]{40}", action)
```

- [ ] **Step 2: Run the contract and verify RED**

```bash
ASTRALDEEP_SOURCE_REPO=/Users/sam/Desktop/Work/AstralDeep \
  .venv/bin/python -m pytest tests/ci/test_workflows.py -q
```

Expected: `.github/workflows/ci.yml` is missing.

- [ ] **Step 3: Create the owner workflow**

Implement these exact job commands under read-only permissions and SHA-pinned checkout/setup actions:

```yaml
python:
  runs-on: ubuntu-24.04
  steps:
    - uses: actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd # v5.0.1
      with:
        fetch-depth: 0
    - uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1 # v6.3.0
      with:
        python-version: "3.11"
    - run: python -m pip install '.[dev]'
    - run: ruff check src backend tests scripts windows-client
    - run: pytest -q
    - run: python -m build
    - run: python -m pip install --force-reinstall dist/*.whl && python -c "import astralprojection, rote, webrender"
```

The Python job also installs checksum-pinned Gitleaks `8.30.1` with Linux-x64
SHA-256 `551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb`
and scans the complete history with `gitleaks git --redact --log-opts="--all"`.

```yaml
web:
  runs-on: ubuntu-24.04
  steps:
    - uses: actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd # v5.0.1
    - uses: actions/setup-node@249970729cb0ef3589644e2896645e5dc5ba9c38 # v6.5.0
      with:
        node-version: "24"
        cache: npm
        cache-dependency-path: tooling/web-ci/package-lock.json
    - working-directory: tooling/web-ci
      run: test "$(corepack npm --version)" = "11.16.0" && corepack npm ci --ignore-scripts
    - working-directory: tooling/web-ci
      run: corepack npm run check:package-manager && corepack npm run check:product-isolation && corepack npm run lint && corepack npm run test:coverage-conversion && corepack npm run test:coverage-conversion:node
```

The web job must additionally run `test:coverage-conversion:browser`, `continuity-contract-060.spec.js`, and `voice-conversation-065.spec.js` inside the digest-pinned image in `tooling/web-ci/playwright-image.txt`.

```yaml
windows:
  runs-on: windows-latest
  steps:
    - uses: actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd # v5.0.1
    - uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1 # v6.3.0
      with:
        python-version: "3.11"
    - run: python -m pip install --require-hashes -r windows-client/requirements-release.lock.txt
    - run: python -m pip install '.[dev]'
    - env:
        QT_QPA_PLATFORM: offscreen
        PYTHONPATH: windows-client
      run: python -m pytest windows-client/tests -q
```

`required` uses `if: always()`, needs the three jobs, and shell-compares each result to `success`.

- [ ] **Step 4: Update inactive-workflow tests and documentation**

Change the inactive workflow count from nine to six, require the three active CI paths, preserve hard-false checks for all remaining release jobs, and update the README to say CI is active while release workflows remain inert.

- [ ] **Step 5: Run Projection core/web/Windows equivalents**

```bash
ASTRALDEEP_SOURCE_REPO=/Users/sam/Desktop/Work/AstralDeep .venv/bin/python -m pytest -q
.venv/bin/ruff check src backend tests scripts windows-client
actionlint .github/workflows/ci.yml
QT_QPA_PLATFORM=offscreen PYTHONPATH=windows-client \
  .venv/bin/python -m pytest windows-client/tests -q
cd tooling/web-ci
test "$(corepack npm --version)" = "11.16.0"
corepack npm ci --ignore-scripts
corepack npm run check:package-manager
corepack npm run check:product-isolation
corepack npm run lint
corepack npm run test:coverage-conversion
corepack npm run test:coverage-conversion:node
cd ../..
```

Run the three Playwright targets in the exact image named by `playwright-image.txt`; do not substitute a floating browser image.

- [ ] **Step 6: Commit Projection core CI locally**

```bash
git add .github/workflows/ci.yml tests/ci/test_workflows.py tests/test_protocol.py \
  tests/release/test_windows_bridge.py README.md workflows-disabled/ci.yml
git commit -m "ci: activate Projection owner gates"
```

### Task 3: Activate Projection Android and Apple CI

**Files:**
- Create: `/Users/sam/Desktop/Work/AstralProjection/.github/workflows/android-ci.yml`
- Create: `/Users/sam/Desktop/Work/AstralProjection/.github/workflows/apple-ci.yml`
- Modify: `/Users/sam/Desktop/Work/AstralProjection/tests/ci/test_workflows.py`
- Modify: `/Users/sam/Desktop/Work/AstralProjection/apple-clients/README.md`
- Modify: `/Users/sam/Desktop/Work/AstralProjection/provenance/transformations.json`
- Delete: `/Users/sam/Desktop/Work/AstralProjection/workflows-disabled/android-ci.yml`
- Delete: `/Users/sam/Desktop/Work/AstralProjection/workflows-disabled/apple-ci.yml`

**Interfaces:**
- Produces Android aggregate `android-required` and Apple aggregate `apple-required`.
- Consumes standalone paths `android-client/**`, `apple-clients/**`, `contracts/**`, and root `scripts/**`; no `components/AstralProjection` prefix remains.

- [ ] **Step 1: Extend the contract and verify RED**

Require active files, job sets, path ownership, and no hard-false guards:

```python
def test_native_ci_is_active_and_uses_standalone_paths() -> None:
    android = (ACTIVE / "android-ci.yml").read_text(encoding="utf-8")
    apple = (ACTIVE / "apple-ci.yml").read_text(encoding="utf-8")
    assert _job_ids(android) == {
        "build-test", "next-major-readiness", "instrumented", "android-required"
    }
    assert _job_ids(apple) == {
        "swift-lint", "core-tests", "app-unit-tests", "first-login-ui",
        "watch-continuity", "apple-required",
    }
    assert "components/AstralProjection/" not in android + apple
    assert "if: ${{ false }}" not in android + apple
    assert "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'" in android
```

Run:

```bash
.venv/bin/python -m pytest tests/ci/test_workflows.py -q
```

Expected: both active native workflows are missing.

- [ ] **Step 2: Activate Android with the committed wrapper**

Move the seed workflow, remove every hard-false guard, keep PR/main/nightly/manual triggers, and replace every `gradle ...` execution with `./gradlew ...`. Keep the existing SHA-pinned setup-java, setup-gradle, emulator-runner, and artifact actions. Set only the diagnostic job to:

```yaml
if: ${{ github.event_name == 'schedule' || github.event_name == 'workflow_dispatch' }}
```

The aggregate must require `build-test` and `instrumented`; it must not require the nightly diagnostic.

- [ ] **Step 3: Activate Apple without changing its platform matrix**

Move the seed workflow, remove all hard-false guards, retain Xcode `26.6` build `17F113`, iOS/watchOS `26.5`, unsigned builds, three coverage exporters, success-marker artifacts, and the fail-closed six-job aggregate. Adjust only standalone paths.

- [ ] **Step 4: Update provenance and docs**

Add transformation entries for the removed imported paths `workflows-disabled/android-ci.yml` and `workflows-disabled/apple-ci.yml`, set their result status to removed with active result paths, update the Apple README digest entry, keep entries sorted, and change the asserted removed count from 14 to 16.

- [ ] **Step 5: Run Android local gates**

```bash
cd /Users/sam/Desktop/Work/AstralProjection/android-client
./gradlew ktlintCheck :app:lintDebug --no-daemon --stacktrace
./gradlew :core:test :app:testDebugUnitTest --no-daemon --stacktrace
./gradlew :core:koverVerify :app:koverXmlReport :core:koverXmlReport --no-daemon --stacktrace
./gradlew :app:assembleDebug --no-daemon --stacktrace
```

The API-34 x86_64 emulator remains a hosted confirmation because this Mac does not reproduce Linux KVM.

- [ ] **Step 6: Run Apple local gates**

```bash
cd /Users/sam/Desktop/Work/AstralProjection
xcrun swift-format lint --strict --recursive --configuration apple-clients/.swift-format \
  apple-clients/AstralCore apple-clients/AstralApp apple-clients/AstralWatch
python3 apple-clients/Scripts/generate_app_icons.py --check
swift test --package-path apple-clients/AstralCore --enable-code-coverage
```

Then execute the exact unsigned `xcodebuild` commands from `.github/workflows/apple-ci.yml` for the iOS 26.5 simulator, macOS, first-login UI, and watchOS 26.5 destinations.

- [ ] **Step 7: Run the full Projection suite and commit locally**

```bash
actionlint .github/workflows/ci.yml .github/workflows/android-ci.yml .github/workflows/apple-ci.yml
ASTRALDEEP_SOURCE_REPO=/Users/sam/Desktop/Work/AstralDeep .venv/bin/python -m pytest -q
git add .github/workflows/android-ci.yml .github/workflows/apple-ci.yml \
  tests/ci/test_workflows.py apple-clients/README.md provenance/transformations.json \
  workflows-disabled/android-ci.yml workflows-disabled/apple-ci.yml
git commit -m "ci: activate Projection native client gates"
```

### Task 4: Activate Plane CI with real PostgreSQL qualification

**Files:**
- Create: `/Users/sam/Desktop/Work/AstralPlane/tests/architecture/test_ci_workflow.py`
- Create: `/Users/sam/Desktop/Work/AstralPlane/.github/workflows/ci.yml`
- Create: `/Users/sam/Desktop/Work/AstralPlane/tooling/python-ci/build-requirements.lock.txt`
- Modify: `/Users/sam/Desktop/Work/AstralPlane/pyproject.toml`
- Modify: `/Users/sam/Desktop/Work/AstralPlane/uv.lock`
- Modify: `/Users/sam/Desktop/Work/AstralPlane/README.md`
- Modify: `/Users/sam/Desktop/Work/AstralPlane/tests/test_blob_store.py`
- Delete: `/Users/sam/Desktop/Work/AstralPlane/workflows-disabled/ci.yml`

**Interfaces:**
- Produces jobs `quality`, `postgresql`, `package-compatibility`, and `gates`.
- PostgreSQL tests consume `ASTRALPLANE_TEST_POSTGRES_DSN`; provenance replay consumes a public Deep checkout at commit `fc113c4f99121b2053bb71523835c5c4743f1f56`.

- [ ] **Step 1: Add locked CI tooling and the RED workflow contract**

Add this dependency group, then generate `uv.lock`:

```toml
[dependency-groups]
ci = [
  "diff-cover==10.3.0",
  "pytest==8.4.2",
  "pytest-asyncio==1.3.0",
  "pytest-cov==7.0.0",
  "ruff==0.15.21",
]
```

Create a test asserting the four job IDs, read-only permissions, SHA-pinned actions, no hard-false/continue-on-error/stale component paths, mandatory DSN, digest-pinned PostgreSQL, the measured 88.75% combined branch-coverage non-regression floor, 90% changed-line coverage, a hash-constrained build backend, and the all-success aggregate. Pin `setuptools==80.10.2` and generate a hash-locked build constraint without adding it to runtime dependencies.

```bash
uv lock
uv run --frozen --group ci pytest tests/architecture/test_ci_workflow.py -q -p no:cacheprovider
```

Expected: `.github/workflows/ci.yml` is missing.

- [ ] **Step 2: Implement quality and package compatibility jobs**

Use full-history checkout `3d3c42e5aac5ba805825da76410c181273ba90b1`, setup-uv `c771a70e6277c0a99b617c7a806ffedaca235ff9` with exact `uv` version `0.11.26`, `ubuntu-24.04`, and:

```yaml
- run: uv lock --check
- run: uv sync --frozen --group ci
- run: uv run --frozen --group ci ruff check .
- run: uv run --frozen --group ci python tests/architecture/test_dependency_direction.py
```

Package compatibility uses Python 3.11 and 3.14, `uv lock --check`, a hash-constrained `uv build`, a clean venv, wheel install, and:

```bash
python -c "import astralplane; assert astralplane.CONTRACT_VERSION == 'astralplane.contract/v1'"
```

- [ ] **Step 3: Implement the real PostgreSQL gate**

Use:

```yaml
services:
  postgres:
    image: postgres:17-alpine@sha256:dc17045ccfd343b49600570ea734b9c4991cf1c3f3302e67df51e3b402dd55c4
    env:
      POSTGRES_USER: astralplane
      POSTGRES_PASSWORD: astralplane_ci
      POSTGRES_DB: astralplane
```

Checkout public Deep at the exact provenance commit into `source-deep`, set `ASTRALDEEP_SOURCE_REPO`, run the whole suite once with the DSN and branch coverage, then run `diff-cover`:

```bash
uv run --frozen --group ci pytest -q -p no:cacheprovider \
  --cov=astralplane --cov-branch --cov-report=xml --cov-fail-under=88.75
uv run --frozen --group ci diff-cover coverage.xml --compare-branch origin/main --fail-under=90
```

Do not permit the eight DSN-bearing test modules to skip in this job.

- [ ] **Step 4: Add the fail-closed aggregate, update docs, and run local gates**

First make the existing filesystem-denial test deterministic: its injected first
unlink must fail before either the payload or persistent publication fence can be
removed, then the successful retry must report both files and the replay zero.
This preserves the product's bounded, idempotent recovery semantics without
depending on unspecified `os.scandir()` order.

```bash
uv lock --check
uv sync --frozen --group ci
uv run --frozen --group ci pytest tests/architecture/test_ci_workflow.py -q -p no:cacheprovider
uv run --frozen --group ci ruff check .
uv run --frozen --group ci python tests/architecture/test_dependency_direction.py
ASTRALDEEP_SOURCE_REPO=/Users/sam/Desktop/Work/AstralDeep \
ASTRALPLANE_TEST_POSTGRES_DSN='postgresql://astralplane:astralplane_ci@127.0.0.1:5432/astralplane' \
  uv run --frozen --group ci pytest -q -p no:cacheprovider \
  --cov=astralplane --cov-branch --cov-report=xml --cov-fail-under=88.75
uv run --frozen --group ci diff-cover coverage.xml --compare-branch origin/main --fail-under=90
uv lock --check
uv build --build-constraints tooling/python-ci/build-requirements.lock.txt --require-hashes
actionlint .github/workflows/ci.yml
```

Because this Mac's Darwin descriptor/filesystem behavior is not the hosted Linux
environment, repeat the full PostgreSQL/coverage command in the digest-pinned
`ghcr.io/astral-sh/uv@sha256:58683a39536f1f4ed2e1dd79cf155edccfb47731aba8964bd0312aac942126cf`
Python 3.11 Bookworm image with a container-native temporary directory and the
disposable PostgreSQL 17 service network. Require 2,021 passes, only the nine
Windows-specific skips, at least 88.75% combined branch coverage, and at least
90% changed-line coverage.

Do not add `ruff format --check`; it would mechanically rewrite provenance-bound files outside this task.

- [ ] **Step 5: Commit Plane locally**

```bash
git add .github/workflows/ci.yml tests/architecture/test_ci_workflow.py \
  tooling/python-ci/build-requirements.lock.txt pyproject.toml uv.lock README.md \
  tests/test_blob_store.py workflows-disabled/ci.yml
git commit -m "ci: qualify Plane with PostgreSQL owner gates"
```

### Task 5: Add Primitives PR CI and harden publication separation

**Files:**
- Create: `/Users/sam/Desktop/Work/AstralPrimitives/tests/test_ci_workflow.py`
- Create: `/Users/sam/Desktop/Work/AstralPrimitives/.github/workflows/ci.yml`
- Create: `/Users/sam/Desktop/Work/AstralPrimitives/uv.lock`
- Create: `/Users/sam/Desktop/Work/AstralPrimitives/tooling/python-ci/build-requirements.lock.txt`
- Modify: `/Users/sam/Desktop/Work/AstralPrimitives/pyproject.toml`
- Modify: `/Users/sam/Desktop/Work/AstralPrimitives/src/astralprims/base.py`
- Modify: `/Users/sam/Desktop/Work/AstralPrimitives/.github/workflows/python-publish.yml`
- Modify: `/Users/sam/Desktop/Work/AstralPrimitives/CLAUDE.md`

**Interfaces:**
- Produces PR aggregate `gates`; publication consumes an unprivileged built artifact and retains OIDC only in the environment-protected publish job.
- Compatibility endpoints are Python 3.9, 3.11, and 3.14; package version remains `0.3.0`.

- [ ] **Step 1: Add the CI group and workflow contract**

Use:

```toml
[dependency-groups]
ci = [
  "diff-cover==10.3.0; python_version >= '3.10'",
  "pytest==8.4.2",
  "pytest-cov==7.0.0",
  "ruff==0.15.21",
  "twine==6.2.0",
]
```

The new test asserts PR jobs `{quality-package, compatibility, gates}`, Python `3.9`, `3.11`, `3.14`, both distribution formats, clean-install import/serialization smoke, `py.typed`, all 40-character action SHAs, exact `uv` `0.11.26`, no PR OIDC, and a publish workflow whose OIDC job depends on unprivileged verification. Exact-pin `hatchling==1.27.0` and generate a Python-3.9-compatible hash-locked build constraint for Hatchling and all of its isolated-build dependencies.

```bash
uv lock
uv run --isolated --frozen --python 3.11 --group ci pytest \
  tests/test_ci_workflow.py -q -p no:cacheprovider
```

Expected: `.github/workflows/ci.yml` is missing and the current publish workflow contains floating actions.

- [ ] **Step 2: Implement PR quality/package and compatibility**

Use checkout `3d3c42e5aac5ba805825da76410c181273ba90b1`, setup-uv `c771a70e6277c0a99b617c7a806ffedaca235ff9` with exact `uv` version `0.11.26`, upload-artifact `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a`, and:

```bash
uv lock --check
uv sync --frozen --group ci
uv run --frozen --group ci ruff check .
uv run --frozen --group ci pytest -q -p no:cacheprovider \
  --cov=astralprims --cov-branch --cov-report=xml --cov-fail-under=90
uv run --frozen --group ci diff-cover coverage.xml --compare-branch origin/main --fail-under=90
uv build --build-constraints tooling/python-ci/build-requirements.lock.txt --require-hashes
uv run --frozen --group ci twine check dist/*
```

The clean install smoke imports `astralprims`, verifies `importlib.metadata.version("astralprims") == "0.3.0"`, verifies `py.typed` exists in the installed package, and round-trips `Text(content="ci").to_dict()`.

- [ ] **Step 3: Make the minimal Ruff fix**

Change:

```python
from typing import Any, ClassVar, Dict, List, Optional
```

to:

```python
from typing import Any, Dict, Optional
```

No runtime or serialization code changes.

- [ ] **Step 4: Pin and separate publication authority**

Use checkout `3d3c42e5aac5ba805825da76410c181273ba90b1`, setup-python `ece7cb06caefa5fff74198d8649806c4678c61a1`, setup-uv `c771a70e6277c0a99b617c7a806ffedaca235ff9` with exact `uv` `0.11.26`, upload `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a`, download `3e5f45b5eea1f12f8d447405e9c5ef416f0a38d4`, and publisher `dc37677b2e1c63e2034f94d8a5b11f265b73ba33`. Build/test with the same frozen CI group and hash-constrained backend without OIDC, upload distributions, then give only the environment-protected publisher `id-token: write`. Treat a PyPI lookup result of `error` as a hard failure.

- [ ] **Step 5: Run Primitives local gates**

```bash
uv lock --check
uv run --isolated --frozen --python 3.11 --group ci ruff check .
uv run --isolated --frozen --python 3.11 --group ci pytest -q -p no:cacheprovider \
  --cov=astralprims --cov-branch --cov-report=xml --cov-fail-under=90
uv run --isolated --frozen --python 3.11 --group ci \
  diff-cover coverage.xml --compare-branch origin/main --fail-under=90
uv run --isolated --frozen --python 3.9 --group ci pytest -q -p no:cacheprovider
uv run --isolated --frozen --python 3.14 --group ci pytest -q -p no:cacheprovider
uv build --build-constraints tooling/python-ci/build-requirements.lock.txt --require-hashes
uv run --isolated --frozen --python 3.11 --group ci twine check dist/*
actionlint .github/workflows/ci.yml .github/workflows/python-publish.yml
```

- [ ] **Step 6: Commit Primitives locally**

```bash
git add .github/workflows/ci.yml .github/workflows/python-publish.yml \
  tests/test_ci_workflow.py tooling/python-ci/build-requirements.lock.txt \
  pyproject.toml uv.lock src/astralprims/base.py CLAUDE.md
git commit -m "ci: add Primitives pull-request qualification"
```

### Task 6: Disposition Deep's reviewed history findings exactly

**Files:**
- Modify: `/Users/sam/Desktop/Work/AstralDeep/backend/tests/test_python_ci_supply_chain_060.py`
- Modify: `/Users/sam/Desktop/Work/AstralDeep/.gitleaksignore`

**Interfaces:**
- Produces a unique 19-entry exact-fingerprint baseline; permits both `generic-api-key` and the single reviewed `private-key` fixture fingerprint.

- [ ] **Step 1: Make the exact baseline test RED**

Replace the count-only expectation with the complete required addition:

```python
REVIEWED_074_FINGERPRINTS = {
    "7bc9d1f683c535863b5426ab3053db3bdefc6a1a:config/astral-composition.json:generic-api-key:56",
    "839b4e3840ac31c2cadb7c7ab7657818f0ad46a0:windows-client/tests/test_win_agent_startup_gate.py:generic-api-key:121",
    "839b4e3840ac31c2cadb7c7ab7657818f0ad46a0:windows-client/tests/test_win_agent_startup_gate.py:generic-api-key:133",
    "839b4e3840ac31c2cadb7c7ab7657818f0ad46a0:windows-client/tests/test_win_agent_startup_gate.py:generic-api-key:201",
    "839b4e3840ac31c2cadb7c7ab7657818f0ad46a0:windows-client/tests/test_win_agent_inbound_auth.py:generic-api-key:124",
    "839b4e3840ac31c2cadb7c7ab7657818f0ad46a0:windows-client/tests/test_win_agent_inbound_auth.py:generic-api-key:267",
    "40cc17aba0c6bd4d7ca3e22b76829b7b657e5b90:windows-client/tests/test_remote_machines_surface.py:private-key:42",
}
```

Assert `len(fingerprints) == 19`, uniqueness, exact inclusion, and the regex `r"[0-9a-f]{40}:[^:]+:(?:generic-api-key|private-key):[1-9][0-9]*"`.

- [ ] **Step 2: Verify RED, add exactly the fingerprints, and rescan**

```bash
.venv/bin/python -m pytest \
  backend/tests/test_python_ci_supply_chain_060.py::test_gitleaks_history_baseline_is_exact_fingerprint_only -q
gitleaks git --redact --config .gitleaks.toml \
  --gitleaks-ignore-path .gitleaksignore --log-opts="--all" .
```

Expected before edit: test failure and seven findings. Expected after appending the seven lines: test pass and zero findings.

- [ ] **Step 3: Commit the exact security disposition locally**

```bash
git add .gitleaksignore backend/tests/test_python_ci_supply_chain_060.py
git commit -m "security: disposition reviewed CI history findings"
```

### Task 7: Rewrite Deep CI around source ownership

**Files:**
- Modify/Delete the AstralDeep files listed in the File Map for this task.

**Interfaces:**
- Produces active jobs `lint`, `release-tooling-tests`, `component-contract-tests`, `composition-declarations`, `voice-worker-test`, `secret-scan`, and `gates`.
- `gates` succeeds only when every active job succeeds; it explicitly records that composed qualification is local and never authorizes release.

- [ ] **Step 1: Write ownership assertions first and verify RED**

In `scripts/tests/test_component_build_surfaces_074.py`, assert:

```python
job_ids = set(re.findall(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", workflow.partition("\njobs:\n")[2]))
assert job_ids == {
    "lint", "release-tooling-tests", "component-contract-tests",
    "composition-declarations", "voice-worker-test", "secret-scan", "gates",
}
assert not (REPOSITORY_ROOT / ".github/workflows/android-ci.yml").exists()
assert not (REPOSITORY_ROOT / ".github/workflows/apple-ci.yml").exists()
for stale in ("javascript-lint", "voice-contract-validator", "voice-web-conformance", "windows-client"):
    assert stale not in job_ids
gates = _workflow_job(workflow, "gates")
assert "Composed qualification unavailable" not in gates
assert "exit 1" not in gates
```

Update the named legacy workflow tests to assert Projection ownership or parked release behavior instead of requiring Deep client jobs.

```bash
.venv/bin/python -m pytest -q \
  scripts/tests/test_component_build_surfaces_074.py \
  backend/tests/test_ci_javascript_lint.py \
  backend/tests/test_python_ci_supply_chain_060.py \
  backend/tests/test_release_workflows_060.py \
  backend/tests/test_voice_release_evidence_producers_065.py \
  backend/tests/test_release_evidence_bootstrap.py
```

Expected: failures identify the old job IDs, files, inventory, and forced-failure aggregate.

- [ ] **Step 2: Replace Deep's workflow with owner-only jobs**

Retain the existing pinned actions and locked tooling. `release-tooling-tests`
runs these Deep-owned modules and enforces 90% coverage on `scripts`:

```text
backend/tests/test_changed_coverage_060.py
backend/tests/test_release_tooling_coverage_060.py
backend/tests/test_documentation_060.py
backend/tests/test_quickstart_commands.py
backend/tests/test_python_ci_supply_chain_060.py
backend/tests/test_candidate_staging_060.py
backend/tests/test_release_evidence_validator.py
backend/tests/test_prepare_release_evidence_060.py
backend/tests/test_extract_release_artifact_060.py
backend/tests/test_release_evidence_bootstrap.py
scripts/tests/test_component_build_surfaces_074.py
scripts/tests/test_install_local_components.py
scripts/tests/test_verify_composition.py
scripts/tests/test_verify_migration_provenance.py
scripts/tests/test_verify_primitive_coverage.py
```

`component-contract-tests` runs the five `scripts/tests/*074.py` and verifier
test modules above against temporary repositories; `composition-declarations`
performs `--declarations-only --require-gitlinks`; `voice-worker-test` retains
its current closure-variable no-op contract; `secret-scan` retains
checksum-pinned Gitleaks.

The aggregate is:

```yaml
gates:
  name: Deep owner CI aggregate
  if: always()
  needs:
    - lint
    - release-tooling-tests
    - component-contract-tests
    - composition-declarations
    - voice-worker-test
    - secret-scan
  runs-on: ubuntu-24.04
  steps:
    - name: Enforce every hosted owner gate
      run: |
        [[ '${{ needs.lint.result }}' == 'success' ]]
        [[ '${{ needs.release-tooling-tests.result }}' == 'success' ]]
        [[ '${{ needs.component-contract-tests.result }}' == 'success' ]]
        [[ '${{ needs.composition-declarations.result }}' == 'success' ]]
        [[ '${{ needs.voice-worker-test.result }}' == 'success' ]]
        [[ '${{ needs.secret-scan.result }}' == 'success' ]]
        echo "Full private composition remains a required local Feature 074 qualification."
```

There is no build/image artifact, publish job, client job, or synthetic composed-success claim.

- [ ] **Step 3: Move release-policy ownership references**

Delete Deep Android/Apple workflows. Apply these exact ownership mappings in
the local-only policy tests:

```python
PROJECTION_WORKFLOWS = REPO_ROOT / "components" / "AstralProjection" / ".github" / "workflows"
APPLE_CI = PROJECTION_WORKFLOWS / "apple-ci.yml"
WORKFLOW_ROOT = PROJECTION_WORKFLOWS
```

Remove the Android/Apple workflow mounts from the disabled composed-job fixture,
and reduce both the verifier and its fixtures to:

```python
REQUIRED_PR_WORKFLOWS = {".github/workflows/ci.yml"}
```

Update deployment documentation to state that Projection owns native PR
evidence and Deep release activation remains parked.

- [ ] **Step 4: Run targeted Deep workflow tests and Ruff**

```bash
.venv/bin/python -m pytest -q \
  scripts/tests/test_component_build_surfaces_074.py \
  backend/tests/test_ci_javascript_lint.py \
  backend/tests/test_python_ci_supply_chain_060.py \
  backend/tests/test_release_workflows_060.py \
  backend/tests/test_voice_release_evidence_producers_065.py \
  backend/tests/test_release_evidence_bootstrap.py
.venv/bin/ruff check .
actionlint .github/workflows/ci.yml
```

- [ ] **Step 5: Commit Deep CI ownership locally**

```bash
git add .github/workflows/ci.yml .github/workflows/android-ci.yml \
  .github/workflows/apple-ci.yml scripts/tests/test_component_build_surfaces_074.py \
  backend/tests/test_ci_javascript_lint.py backend/tests/test_python_ci_supply_chain_060.py \
  backend/tests/test_release_workflows_060.py \
  backend/tests/test_voice_release_evidence_producers_065.py \
  scripts/verify_release_evidence_bootstrap.py \
  backend/tests/test_release_evidence_bootstrap.py docs/production-deployment.md
git commit -m "ci: enforce repository-owned qualification"
```

### Task 8: Run final local qualification at exact candidate commits

**Files:**
- Modify: `/Users/sam/Desktop/Work/AstralDeep/specs/074-multirepo-lets-integration/execution/local-ci-qualification.json`

**Interfaces:**
- Consumes all five local candidate commits and exact Deep submodule pins.
- Produces evidence only; `mergeAuthorization` and `releaseAuthorization` remain `false` until hosted owner checks are observed and the user merges manually.

- [ ] **Step 1: Refresh Deep's component pins to the final owner commits**

Keep LETS on immutable signed `v1.0.10`. Fetch and detach the other three
submodules at the final local PR heads:

```bash
cd /Users/sam/Desktop/Work/AstralDeep
git -C components/AstralProjection fetch origin codex/074-extract-projection
git -C components/AstralProjection checkout --detach \
  "$(git -C /Users/sam/Desktop/Work/AstralProjection rev-parse HEAD)"
git -C components/AstralPlane fetch origin codex/074-extract-data-plane
git -C components/AstralPlane checkout --detach \
  "$(git -C /Users/sam/Desktop/Work/AstralPlane rev-parse HEAD)"
git -C components/AstralPrimitives fetch origin codex/074-canonical-identity
git -C components/AstralPrimitives checkout --detach \
  "$(git -C /Users/sam/Desktop/Work/AstralPrimitives rev-parse HEAD)"
```

Update the three `components.*.commit` values in
`config/astral-composition.json` with `apply_patch`. Recompute the Primitives
contract digest from the initialized component and patch
`compatibility.primitives.contract_sha256`:

```bash
.venv/bin/python -c \
  'from pathlib import Path; from scripts.verify_composition import compute_primitives_digest; print(compute_primitives_digest(Path("components/AstralPrimitives")))'
.venv/bin/python scripts/verify_composition.py --root .
git add components/AstralProjection components/AstralPlane \
  components/AstralPrimitives config/astral-composition.json
git commit -m "build: refresh final component qualification pins"
```

- [ ] **Step 2: Verify all five worktrees and pins before expensive tests**

```bash
for repo in AstralDeep AstralProjection AstralPlane AstralPrimitives LETS; do
  git -C "/Users/sam/Desktop/Work/$repo" status --short --branch
  git -C "/Users/sam/Desktop/Work/$repo" rev-parse HEAD
done
cd /Users/sam/Desktop/Work/AstralDeep
make composition-preflight
```

Expected: no uncommitted changes before evidence recording; every gitlink matches its declared commit or is deliberately updated and recommitted first.

- [ ] **Step 3: Run full Deep composition gates**

Run the same flags-on/flags-off primary, nested, performance, migration/recovery, ownership, composition, voice-contract, image-build, boot/readiness, and exit-78 commands recorded in the existing local qualification receipt. Generate every repository-owned coverage input and run `scripts/check_changed_coverage.py --coverage-mode strict --repository-profile deep` with the exact candidate/base pair.

- [ ] **Step 4: Re-run each owner suite at its final commit**

Repeat the complete local command blocks from Tasks 1–5 after all commits are final. A cached earlier result is not final evidence.

- [ ] **Step 5: Record exact evidence and commit Deep locally**

Update `recordedAtUtc`, `candidateCommit`, all four component commits, command results, coverage results, and residual platform-only confirmations. Preserve:

```json
"mergeAuthorization": false,
"releaseAuthorization": false
```

Then:

```bash
git add specs/074-multirepo-lets-integration/execution/local-ci-qualification.json
git commit -m "test: record final repository-owned CI qualification"
```

### Task 9: Push once, activate PRs, verify hosted checks, and checkpoint the vault

**Files:**
- Modify curated pages in `/Users/sam/Desktop/Work/kos-wiki` selected through its `index.md` after reading `CLAUDE.md`.

**Interfaces:**
- Consumes the five clean local branches and existing PRs #177, #2, #5, #2, and #26.
- Produces five ready-for-review PRs and one separate pushed vault checkpoint; no merge.

- [ ] **Step 1: Push each exact branch once**

```bash
git -C /Users/sam/Desktop/Work/AstralPrimitives push origin codex/074-canonical-identity
git -C /Users/sam/Desktop/Work/AstralProjection push origin codex/074-extract-projection
git -C /Users/sam/Desktop/Work/AstralPlane push origin codex/074-extract-data-plane
git -C /Users/sam/Desktop/Work/LETS push origin codex/074-astral-case-study
git -C /Users/sam/Desktop/Work/AstralDeep push origin 074-multirepo-lets-integration
```

- [ ] **Step 2: Update PR bodies with exact local evidence and mark ready**

Use `gh pr edit` for evidence summaries and `gh pr ready` for the existing PR numbers. Do not create replacement PRs and do not merge.

- [ ] **Step 3: Wait for one complete hosted run**

```bash
gh pr checks 2 --repo AstralDeep/AstralPrimitives --watch
gh pr checks 2 --repo AstralDeep/AstralProjection --watch
gh pr checks 5 --repo AstralDeep/AstralPlane --watch
gh pr checks 26 --repo AstralDeep/LETS --watch
gh pr checks 177 --repo AstralDeep/AstralDeep --watch
```

If a hosted lane fails, reproduce it locally before another push whenever the platform permits. Windows, Linux KVM, and GitHub event-semantics failures are diagnosed from logs and corrected with a focused local contract test first.

- [ ] **Step 4: Update and push the knowledge vault checkpoint**

Read `kos-wiki/CLAUDE.md`, refresh the repository anchor, update affected curated pages plus `index.md` and `log.md`, commit separately, and push. Record all five candidate SHAs, PR readiness, local commands/results, hosted check URLs/results, merge order, and residual risks.

- [ ] **Step 5: Report merge readiness**

Report each PR as mergeable only when its stable aggregate is green and GitHub reports no merge conflict or required-check blocker. Recommend merge order: Primitives, Projection, Plane, LETS, Deep. State explicitly that ready-for-review is not merged, deployed, published, or released.
