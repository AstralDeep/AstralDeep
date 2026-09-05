# Bounded local assignment observations

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
