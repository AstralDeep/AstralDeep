# Backend and browser CI qualification

The owner's 2026-09-21 release scope is the backend and browser, including
responsive phone/tablet layouts and enlarged text. Windows, Android, iOS,
macOS and watchOS qualification and publication are paused. Backend security,
client protocol dispositions, data isolation and LETS tests remain in scope.

The required Deep CI aggregate now includes:

- Python 3.11 tests from every backend `tests/` and `qual_audit/suites/`
  directory, the explicitly named concurrency probes, and release-tooling
  tests. Suites run in separate processes to avoid module/conftest collisions.
- Disposable PostgreSQL 17 for those tests, and the exact pinned Plane's
  complete PostgreSQL suite. The test producer refuses absent/mismatched
  database URLs and requires an `ad_gate_` database plus an explicit isolation
  declaration. Missing-database skips fail the producer.
- Deep Python lint; exact Projection Python and JavaScript lint and tests;
  pinned browser interaction tests; exact component-source integrity.
- A clean backend image build, installed-wheel verification, development boot,
  positive production-configuration boot, unauthenticated route denial, and negative
  no-secrets production boot with exit 78.
- Locked voice-worker image tests, with no conditional green no-op.
- At least 90% changed Python coverage, against immutable event identities.
  Empty/self comparisons fail closed. Manual qualification requires the
  immutable reviewed PR base via `base_sha`; this maintenance branch started
  at `b3ae2dc549928aeaee518ad60c9428790f67fc05`. Component comparisons use that
  base's exact gitlinks. The deployed baseline
  `013a06922759ae737b24b20ab02705e35614789d` separately anchors upgrade/recovery
  rehearsal; its wider coverage comparison is diagnostic, not a replacement
  for or additional threshold on the Constitution's changed-by-PR gate.
- At least 90% changed executable JavaScript, using exact candidate source
  maps and the canonical Node/browser/offline/export union. The explicit
  `projection-web` profile retains strict Python and JavaScript producers and
  records paused native paths; it does not change the complete Projection
  release profile. Responsive interactions run in Chromium, Firefox and WebKit.

The complete-root diagnostic reached 32% after 35 minutes on this machine.
An isolated `EXPLAIN ANALYZE` attributed about 366 of 480 milliseconds per
schema verification to repeated dependency-catalog scans. The CI test job has
a four-hour budget and each suite a three-hour timeout; the Plane job has a
three-hour budget. These bounds are based on partial diagnostics; a complete
run's duration has not yet been established. A timeout fails the gate and retains
the partial log; it never turns an unfinished suite into a pass.

`requirements.lock.txt` is CI-only. Its validator versions match the existing
voice-contract validator's reviewed versions. Neither this lock nor the test
tools are copied into the shipping image. The tests use its exact installed
component wheels, including the pinned Primitives source rather than a stale
index package.

Run the image gate from a clean Linux checkout with Docker, Python and Node 24
available on `PATH`:

```sh
mkdir -p build/backend-web
docker build --pull --no-cache --iidfile build/backend-web/image-id.txt .
bash scripts/backend_web_image_gate.sh "$(cat build/backend-web/image-id.txt)"
```

The shell gate creates its own Docker namespace, PostgreSQL server, test and
smoke databases and synthetic keys, and removes its containers on exit. It
does not read `.env`, publish ports, or mount live database/blob directories.
Use a clean checkout: tests may create fixture files under their source tree.

For a separately verified policy checkout, set `ASTRAL_GATE_POLICY_ROOT` to
that checkout and keep the working directory at the candidate checkout. The
policy is mounted read-only at `/qualification-policy`; its hash-locked
tooling, suite runner and immediate failure reporter choose and execute the
tests. This interface does not itself establish a protected producer or a
release authorization. Test containers receive neither the Docker socket nor
the host's GitHub credentials. The optional
`ASTRAL_GATE_NAMESPACE` must begin with `ad-gates-` or `ad-bwq-gates-` and contain
only lowercase letters, digits and hyphens.

All pytest exit codes and JUnit failures/errors are enforced. Logs and
`test-results.json` retain skipped test names and reasons separately; a skipped
live-provider test is never reported as a passed test. The historical mock-token
WebSocket probe runs only against the separately isolated development smoke
server. The test runner emits `production_qualified: false` even on a
successful CI run.

Boot smoke uses synthetic keys, an unreachable synthetic identity-provider
address and LETS off. It only proves startup/configuration and anonymous-denial
behavior. It does **not** qualify login, renewal, model/provider access, governed
tool execution, enabled voice media, or live LETS enforcement. Those remain
mandatory protected staging evidence, alongside actual-baseline migration,
durable retention, audit continuity, paired backup restoration and recovery.
The isolated CI result alone never authorizes merge, deployment or release.
