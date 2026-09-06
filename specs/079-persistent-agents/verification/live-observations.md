# Bounded local assignment observations

## Source diagnosis and final repair (no additional model allowance)

A subsequent public read identified the concrete extraction cause: 261 release
version occurrences were inside a content list with CSS class
`list-row-container menu`. The shared reader's generic `menu` chrome heuristic
discarded that entire list before privacy processing or model context bounds.
This explains the missing rows in the reader; it does not identify the original
discarded model response that triggered a privacy refusal.

Runtime `5b055eb9af958039f45c4387690cb7d669fb4783` fixes ambiguous content-menu
classification in both existing readers and passes 203 tests. A read-only
diagnostic on the installed final image retained 210 version occurrences from
one normal public-page fetch; 59 remained after the existing full injection
scan, source-only Presidio redaction, PHI checks and 4,096-character observation
bound. An authored planner context retained the observed version and fit the
existing limits. No deployed module was overridden, and no model, assignment,
grant or private database was used. This is source/guard verification, not a
new model-derived assignment finding or a reset of the exhausted test budget.

A fresh authenticated Schedule view after the final rebuild showed all three
temporary assignments Stopped, their exact earlier counters retained and no
next check. The earlier lifecycle observations below keep their actual runtime
identities; the new extraction repair does not relabel those findings.

## Final remaining-allowance test — 2026-09-06 UTC

The owner's standing authorization covered completion of the already reviewed
bounded test. **Public release monitor — remaining allowance** received only
the unused daily/lifetime capacity from the stopped follow-up: 3 model calls,
8 tool calls, 19,588 tokens and 249,955 ms. URL, cadence, tool, one-task
concurrency, depth zero, four-task cap, retry and timeout remained unchanged.
It requested the first observed stable version and completion only after an
actual version baseline. Earlier assignments and charges were not modified.

Clean runtime `b525671c3b5dab0a7fb523080bed12476c408052`, image `sha256:d08279202003782159968c8546d0f135f85a80191e799fe3ee195ede1e5a7b12`, passed 591 source bindings and
the startup/authentication checks before activation. Creation acknowledged
revision 1/control 1 and a next check of `2026-09-06T00:05:01.330055+00:00`.
At `00:05:10.628891+00:00`, a real planner, child and parent episode completed.
The child's recorded result was incorporated, but its claim of completion
despite unknown version data was explicitly rejected by the parent finding.
The supplied fallback's release tables remained empty; no populated version
baseline was accepted. The assignment correctly remained Waiting, with the
completion condition unmet. No privacy refusal occurred in this episode.

Stop was acknowledged at `00:05:40.030292+00:00`, revision 1/control 2,
**Stopped, no next check and zero reservations**. Final usage: **3/3 model
calls, 1/8 tool calls, 2,776/19,588 tokens and 6,455/249,955 ms**. The current
cadence wait is blank on the terminal view; the completed task, uncertainty
and activity remain visible. Together with the prior follow-up, total spending
is **8/8 model calls, 5/12 tool calls, 15,188/32,000 tokens and 56,500/300,000
ms**. No remaining model capacity is reused or replenished.

The richer-context regression is verified with authored source fixtures; this
live result does not establish that earlier truncation caused the missing
version. Source-specific baseline/change verification remains open. Earlier
quiet-poll and idle-restart evidence below retains its actual runtime identity.

## Follow-up test authorization and diagnosis

The owner's subsequent permission to perform the necessary work authorized the
already reviewed follow-up test on 2026-09-05. **Public release monitor —
follow-up verification** uses the same URL, cadence, daily/lifetime call,
token and active-time caps as the original test below, one concurrent task,
four tasks, zero recursive depth, one retry and a 30-second step timeout.
Its instructions require release versions only, omit dates/names/identifiers,
use the supplied observation once, and prohibit links and external changes.
The original stopped assignment and its spending were left intact.

Creation stored revision 1 / control version 1, with a next check at
`2026-09-05T23:09:26.159467+00:00`. One public read and a real model planner
produced a durable `baseline` task. Its worker returned the exact provider
truncation code at `23:09:57.347318+00:00`; no accepted result or parent finding
was stored. Pause at `23:10:56.965108+00:00` advanced control version to 2 and
removed the next check. Charged usage remained **2/8 model calls, 1/12 tool
calls, 9,896/32,000 tokens and 40,112/300,000 ms**, with zero reservations.

The public System LLM settings identify `zai-org/GLM-5.3-Flash`. Its
[maintainer documentation](https://huggingface.co/zai-org/GLM-5.3-Flash/commit/04c4e9e95c5da8862dced7e5056455116f83a7e0)
states that omitted reasoning effort defaults to `max`. The follow-up runtime
repair requests supported `low` effort for new persistent-agent model intents
through the existing central dispatch. The 4,096 completion-token bound and
owner limits remain unchanged. Existing three-key model intents retain their
original request and digest, including the absence of reasoning effort;
failed or uncertain begun actions are not replayed by an upgrade.

### Completed episode and quiet polling

Clean runtime `48f8ea5ff6638061ff6a869b8e5bc4b45f9b9af0`, image
`sha256:c565761d9048eb786deed5c04990db97c6a78c7fa7d3d68007078f1bdfca2d0c`,
matches all 591 runtime files and passes health/readiness, absent/invalid
authentication denials and both isolated production exit-78 cases. Its 145
installed package versions/metadata and Python version match the audited image;
the advisory findings remain open.

The reviewed revision 2 narrowed the requested baseline to the newest stable
version and first-observation status at `23:30:17.711676+00:00`, retaining all
limits and usage. Resume at `23:30:42.752460+00:00` advanced control version to
4. Three model calls completed the planner, child and parent integration by
`23:30:52.508157+00:00`; the child is Completed with its result Incorporated.
The finding honestly reports that no stable version was available in the
supplied fallback view. This verifies a completed observation/task/result
episode, **not a populated Python version baseline or a release-change event**.

Usage then measured 5/8 model calls, 2/12 tool calls, 12,412/32,000 tokens and
47,066/300,000 ms, with zero reservations. The unchanged scheduled check at
`23:32:00.180809+00:00` consumed one read and 1,460 ms, with no new model call,
token charge, finding or activity item. A full application restart preserved
the exact completed child/result receipt, parent finding, revision/control,
next check and all four counters. The first immediate HTTP probe during startup
disconnected; a subsequent probe passed all five checks and all file bindings.
This is idle-assignment recovery with an already completed result, not a
mid-effect interruption/reconciliation test.

After restart, the scheduled check at `23:33:12.474406+00:00` consumed one
additional read and 1,519 ms, again without new model calls, tokens, findings or
activity. Stop was acknowledged at `23:33:55.127347+00:00`: **Stopped, revision
2 / control version 5, no next check, zero reservations**. Final usage is
**5/8 model calls, 4/12 tool calls, 12,412/32,000 tokens and 50,045/300,000 ms**.
The completed child and incorporated result remain visible. No limits were
increased, spending reset, rejected content retained or privacy rule relaxed.
After the previously scheduled next-check time passed, a fresh authenticated
detail view still showed the same stopped state and counters; no new work ran.

## Original stopped test

These are observations from the real signed-in browser and locally deployed
candidate, recorded on 2026-09-05. They are diagnostic notes, not canonical
provider-attested staging evidence. No credentials, rejected model content or
private application records are included.

The owner approved the temporary **Public release monitor — verification**:
read only `https://www.python.org/downloads/` every 60 seconds; no external
changes; daily and lifetime limits of eight model calls, twelve tool calls,
32,000 tokens and 300,000 ms of active execution. Monetary cost is unpriced.
The narrower runtime settings are one retry, two concurrent tasks, four tasks,
zero recursive delegation depth, and 30,000 ms per step.

| Observed UTC time | Candidate / action | Result |
| --- | --- | --- |
| 22:02:31 | `42b7ce56`, initial creation/run | The reviewed grant was accepted and one public read completed. `assignment_model_unconfigured`; zero model calls/tokens. |
| 22:03:31 | Pause | Acknowledged; control version 2. |
| Before 22:08:09 | Owner configured System LLM in-product | Owner replied ready. No personal credentials were borrowed or copied. |
| 22:08:09–22:08:10 | Resume | Old unstarted planner permission digest caused `assignment_precondition_changed`; no additional physical calls. |
| 22:09:14 | Pause | Acknowledged; control version 4. |
| 22:23:56–22:24:20 | `0dfc768f`, resume after recovery repair | Real model calls and a task plan were recorded, then `assignment_result_refused`. Rejected content was discarded, so the cause is unknown. |
| 22:26:08 | Pause | Version 6; usage 4 model / 3 tool calls, 9,579 tokens, 45,789 ms. |
| 22:37:50 | `9913610f`, reviewed revision 2 | Instructions narrowed to one concise baseline task using the supplied source observation. Original caps and lifetime usage retained; old task superseded. |
| 22:38:07–22:38:16 | Resume | Exact provider completion reason `length` produced `assignment_model_output_truncated`; usage 5 model / 4 tool calls, 16,813 tokens, 77,306 ms. |
| 22:38:29 | Pause | Acknowledged; control version 9. |
| 22:47:58 | `7c8cd1b7`, reviewed revision 3 | Explicit no-following-links instruction added. Original limits retained at version 10; no spending reset. |
| 22:48:22–22:48:30 | Resume | The planner got past the truncation check but the privacy gate returned `assignment_phi_refused`. No accepted baseline or task result was stored. |
| 22:49:14 | Stop | Acknowledged; revision 3, control version 12, status Stopped, no next check, zero outstanding reservations. |

Final charged lifetime usage is **6/8 model calls, 5/12 tool calls,
27,143/32,000 tokens and 108,817/300,000 ms**. Failed/rejected work retains its
conservative reserved charge; these counters are resource accounting, not a
statement of the provider's bill. No budget was refunded, reset or increased.
The remaining 4,857-token capacity cannot reserve another complete planner
request under the current input/output bound. No more model calls were attempted.

After a full application restart, a fresh authenticated page load again showed
Stopped, revision 3 / control version 12, no scheduled check, zero reservations
and exactly the same four usage counters. All 591 runtime file bindings,
health/readiness and absent/invalid bearer denials passed again. This verifies
terminal-state persistence; it is not active-work restart recovery evidence.

The privacy failure establishes only that the result was refused by the
configured gate. Rejected content was not retained. It does not establish
whether the model produced sensitive content or the detector had a false
positive; no privacy rule was relaxed to make the test pass.

A separate read-only audit against the installed local detector used seven
authored synthetic planner responses and no live output/provider call. Six
passed, including compact plans, semantic versions and the reviewed URL. A
public-release date in ISO format triggered the existing broad date/DOB regex
prefilter. That is a reproducible policy interaction, but does not establish
what the discarded live response contained. No exception or bypass was added.

Verified here: real owner creation/consent, public reads, model execution,
retained failure/task history, revisions, pause/resume/stop, preserved spending,
and continued enforcement of privacy and resource gates. A successful baseline,
quiet unchanged polling, completed child-result incorporation, active-work
restart recovery and affected native-client behavior remain unverified in this
live test. Automated tests covering those behaviors are recorded separately.
