# UI v2 source checkpoint and maintenance

Date: 2026-09-21 (America/New_York).

The owner requested current UI documentation, direct-main source synchronization
through the existing administrator bypass, a measured Jev/local-Laya decision
without switching providers, focused test/CI maintenance, and a local container
rebuild. Outstanding CI is accepted for this pass; no persistent policy or
repository protection is changed.

## Current source and scope

- Projection UI source checkpoint: `3f5a4fbf99c623e685822349c2ca1bace52cfc08`,
  annotated tag `ui-v2-2026-09-21`. See its `docs/UI_V2.md` for the interface map.
- Primitives CI correction: `842ef0f4499bafb152f3a8eeaabffec7451d1017`.
- Plane: existing merged main `37dfe02`; tree unchanged from the prior pin.
- LETS: retain qualified `v1.0.11` composition pin `6245189`; main is `f53f3f3`.
- Exact full pins remain in `config/astral-composition.json` and the gitlinks.
  UI protocol, schema `089.001`, primitive version `0.4.0`, migration digest,
  and feature flags are unchanged.

The existing uncommitted console design and backend integration changes were
reviewed and retained. The maintenance work adds documentation, updates tests,
moves an unchanged history helper outside a JavaScript switch for lint,
regenerates the shared export HTML/manifest and stale Apple bundled copy, and
aligns local LiveKit's advertised UDP range with Compose's `50000–50080` mapping.
Staging and production retain `50000–50099`. No provider migration or new UI
design is included. The untouched empty `temp.html` is a local scratch file.

## Test and CI disposition

Deep now runs a required, credential-free `Backend UI v2 contracts` CI job.
Its isolated `tooling/ui-ci/requirements.in` and hash lock do not enter the
product image. Primitives' clean-wheel smoke compares its owning manifest,
installed distribution and runtime versions instead of hardcoding `0.3.0`.
Projection fixtures now follow the actual shell and More-options menu, expand
all boot placeholders, and use portable Python paths/Git executable modes.

One redundant implementation-map test was consolidated into the existing
behavior parameterization, retaining every component-type case. Obsolete
settings-rail and Advanced-button assertions were rewritten. No Plane or LETS
suite was obsolete because of a web-only redesign; their data/authority tests
remain. No security, coverage, release or integrity gate was disabled.

| Local check | Result |
| --- | --- |
| Exact `ui-v2-contracts` pytest command from `.github/workflows/ci.yml`, fresh Python 3.11 with hash-locked dependencies | 340 passed |
| Product environment, current source: `python -m pytest -q tests/test_056_coverage.py tests/test_history_write_failures.py` | 34 passed |
| Projection: `python -m pytest tests/chrome tests/webrender tests/rote -q --tb=short` | 2,099 passed |
| Projection current source in isolated Linux image: `python -m pytest tests/ci/test_workflows.py -q --tb=short` | 64 passed |
| Projection Chromium `first-task-088.spec.js`, one worker | 11 passed, 1 failed |
| Projection Chromium `selection-088.spec.js`, one worker | 16 passed, 1 failed |
| Primitives full pytest | 69 passed |
| Primitives Ruff, constrained wheel/sdist build, executed clean-wheel smoke | Passed |
| Plane targeted revision/API/architecture/TypeSafe tests | 73 passed, 13 skipped (no PostgreSQL DSN) |
| LETS pinned client/executor/auth/authority tests | 46 passed |
| Changed-test Ruff, relevant JS ESLint, YAML/aggregate validation, diff whitespace | Passed |
| `python scripts/verify_composition.py --root .` | Four exact clean pins/contracts verified |
| `python scripts/install_local_components.py validate --root . --require-gitlinks` | Four initialized sources valid |

The two browser failures expose the same product limitation: at **320 CSS px
with 200% root text**, menu entries extend about **100.22 px** beyond the left
viewport edge. Assertions remain active and the UI is unchanged. Windows could
not execute four Apple shell-fixture tests with its unconfigured WSL Bash; those
same tests pass in the 64-test Linux run. Neither constitutes a native build or
live native-client verification. Browser checks used local Node 22; CI retains
its declared Node 24. These counts are separate scopes and must not be summed
as distinct coverage or described as complete CI.

## Open pull requests

GitHub inventory on this date found no open PRs in AstralDeep, AstralProjection,
AstralPlane or AstralPrimitives. LETS has six Dependabot PRs:

- [#55](https://github.com/AstralDeep/LETS/pull/55): setup-buildx-action 4.2.0 → 4.4.1.
- [#56](https://github.com/AstralDeep/LETS/pull/56): setup-qemu-action 4.3.0 → 4.4.0.
- [#57](https://github.com/AstralDeep/LETS/pull/57): build-push-action 7.3.0 → 7.4.0.
- [#58](https://github.com/AstralDeep/LETS/pull/58): uv Docker image 0.12.13 → 0.12.17.
- [#59](https://github.com/AstralDeep/LETS/pull/59): uv-build 0.12.12 → 0.12.15.
- [#60](https://github.com/AstralDeep/LETS/pull/60): Ruff 0.16.6 → 0.16.8.

These are inventoried, not merged by this maintenance pass. The source snapshot
tag is not a signed binary, store submission, or qualified production release.
The provider benchmark and recommendation are in [jev-laya/decision.md](jev-laya/decision.md).

## Rebuild and live smoke

`docker compose build astraldeep` completed. New image:
`sha256:ce3e95fe64a13838c7de9189679305a2074f52acde8f519bdc72194ed5c6a3b5`.
The build verified all four digest-bound component installations and `pip check`.
`docker compose up -d --force-recreate livekit astraldeep` completed; the app and
existing PostgreSQL service are healthy. The existing voice-worker image was
not rebuilt because its source/dependencies are unchanged.

Installed-wheel verification was repeated inside the new container:
`docker exec astraldeep python /app/scripts/install_local_components.py verify
--root /app --lock /opt/astral-component-wheels/astral-component-wheels.lock.json`.
It passed. Live HTTP `/healthz`, `/readyz`, `/static/client.js` and
`/static/astral.css` each returned 200.

An existing signed-in browser session loaded the rebuilt console, connected
to the backend, displayed History and all four More-options entries, restored
an existing synthetic dice result, opened/closed its full-screen view and
opened/closed the role-aware settings rail. No new chat, model call, permission,
credential or content edit was made by this smoke check. This is local web
verification; mobile/native live behavior and voice-media end-to-end were not
requalified.

## Additional reproduction detail

The Plane sample used Python 3.14.0 with
`uv run --frozen --group ci pytest -q -p no:cacheprovider tests/test_revision.py
tests/test_api.py tests/architecture tests/repositories/test_typesafe_credential.py`.
The LETS sample used Python 3.14.0 with
`uv run --frozen --extra dev --extra client pytest -q -p no:cacheprovider
tests/unit/test_client.py tests/unit/test_executor.py tests/unit/test_auth.py
tests/security/test_authority_boundaries.py` against the pinned release.
These are host sample checks, not a substitute for production Python 3.11 gates.
The 34-test Deep sample used Python 3.11.16 in the previous product image with
current source mounted read-only and network disabled; the 340-test clean CI
selection used Python 3.11.15. Terminal outputs are retained in the task;
separate raw pytest log files were not retained.
