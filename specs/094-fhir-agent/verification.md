# Verification: FHIR Clinical Data agent

Recorded 2026-10-05 on the sandbox host for code commit `f3b5944f`; the later commits add one test and this record. Results obtained on an earlier commit are marked with that commit.

## Local checks

| Check | Result |
|---|---|
| `backend/agents/fhir/tests` | 154 passed |
| Coverage of `backend/agents/fhir` (lines and branches, tests excluded) | 99% |
| `ruff check .` (ruff 0.15.21) | clean |
| Stream tests in the gate environment: `test_stream_inprocess.py`, `test_stream_persist.py`, `test_stream_bridge.py`, `test_stream_late_join.py`, `test_audit_hardening_coverage.py` | 70 passed, 0 skipped; with the test added in `de4f756d` they execute every changed orchestrator line except the safe-seed filter, which the agent suite covers |

## Image gates

Run with `scripts/backend_web_image_gate.sh` from a clean clone, against an image built from that clone.

| Gate | Commit | Result |
|---|---|---|
| Boot: production without configuration, development boot | `f3b5944f` | exits 78; healthy |
| `modules` group | `f3b5944f` | passed, 20 of 20 suites (the agent suite, 691 orchestrator tests and 1,049 tooling tests among them) |
| `modules` group | `48bad059` | passed, 20 of 20 suites |
| `persistent_agents` group | `48bad059` | 946 passed, 1 failed |
| `tests` group | `48bad059` | stopped by the 30-minute suite budget at 93%: 11,514 passed, 11 skipped, 4 failed |
| Whole `backend/tests` directory, same environment without the suite budget | `f3b5944f` | 12,228 passed, 11 skipped, 4 failed, in 35 minutes |

The five failures are not caused by this feature:

- `persistent_agents/tests/test_engine_postgres.py::test_source_plan_children_join_and_unchanged_check` and `tests/attachments/test_account_assignments_079.py::test_uncertain_effect_retirement_commits_stop_without_claiming_account_purge` end in `CancelledError` inside a five-second lease.
- Three tests in `tests/test_restored_session_authority_088.py` raise `SessionRetirementError` from AstralPlane.
- Run by themselves in the same environment, the first and the last three fail identically on the unmodified base `08c484a2` and on `48bad059`, twice each; the second passes alone on both and fails inside the full suite.
- The four failures in the whole-directory run on `f3b5944f` are the same four as on `48bad059`; nothing new fails with the stream change.
- The upstream CI run for `08c484a2` (run 37361975136) passed all three backend groups.

The `tests` group does not finish inside its 30-minute budget on this host, so the gated result for that group comes from upstream CI.

## Live verification on sandbox.ai.uky.edu

Performed in the owner's signed-in browser session against the eICU replay service, with `FF_FHIR` on.

| Step | Commit | Outcome |
|---|---|---|
| Agent intro dialog | `f3b5944f` | lists the four examples and all ten tools |
| "Patient overview" example (SC-002) | `48bad059` | the model called `icu_census`, then `patient_overview`; both cards rendered from live data |
| Card button "Open patient …" | `48bad059` | sent the request as the next turn |
| Typed requests for the census, an overview and a six-hour trend | `f3b5944f` | cards rendered; replies were plain sentences |
| "Watch live feed" on the census card | `f3b5944f` | card appeared without a chat turn and updated by itself to 127 notifications, flagged readings and a discharge |
| "Stream live vitals" / "Stream live" | `f3b5944f` | live card appeared below the static one and updated when the feed minute advanced |
| "Live activity" example through chat | `f3b5944f` | one feed card, updated by itself after about a minute to 267 notifications, 1 admission and 2 flagged readings |
| "Refresh" on the vital sign card | `f3b5944f` | re-ran in place with no chat turn: the card moved from 19:45 to 19:46 and its reading counts changed |

Found during the click-through and fixed:

- A just-admitted patient showed eight empty tiles; the card now shows one notice.
- Once, the model redrew a card itself and its component JSON appeared in the chat. Tool results now say the card is already on the canvas; the next three turns answered in sentences. The model can still choose to redraw.
- Push streams were not live in the web client: the built-in `live_system_metrics` stream also stayed at its first chunk. `FF_STREAM_PROGRESS` fixes this for tools that opt in.

Known limitation: each saved stream update re-renders the canvas, and the current web client returns to the top of the canvas when that happens. Updates are saved only when content changed, about once a minute for this feed.

## Isolation of the FHIR service

Measured after the final deployment. The service publishes no ports and sits on an internal Docker network whose only members are the service and the `astraldeep` container.

| Caller | Result |
|---|---|
| Docker host to the container address, with the token | 403 |
| Another container on the same private network, with the token | 403 |
| A container outside the private network | unreachable |
| The allowed container name with the token | 200 |
| The allowed container name without a token or with a wrong one | 401 |
| Outbound request from the service container | blocked |

## Flag-off posture (SC-003)

With `FF_FHIR` off the agent is not registered in-process, not spawned and not in the safe seed; the gate's development boot reports the unchanged agent count. With `FF_STREAM_PROGRESS` off only a stream's final state is saved, as before, and the agent hides its stream buttons.
