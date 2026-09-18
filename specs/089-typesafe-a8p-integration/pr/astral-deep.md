Written for: the reviewer of the AstralDeep pull request.

# TypeSafe routing, brought to your own key

**Merge order: last**, after AstralPrimitives 0.4.0, AstralPlane 089.001 and
AstralProjection.

---

## What a user gets

If you save your own TypeSafe System One key in **Settings → LLM settings**,
Astral asks TypeSafe one question set per message — which agent, which tool,
how risky, how the answer should be laid out — at the same time as it prepares
your request. The answer narrows the model's first round, adds a second safety
check, and arranges the result without a second model call.

**Without a key nothing changes.** Not the tools you are offered, not the
speed, not a single frame. That is the first invariant this PR is built
around, and it is measured rather than asserted: an unkeyed turn makes **zero**
calls and adds **0.0 ms at p95**.

## What an operator gets, and cannot get

There is no deployment-wide TypeSafe key and no way to configure one. A
production-posture process **refuses to start (exit 78)** when
`TYPESAFE_API_KEY`, `TYPESAFE_BASE_URL` or `TYPESAFE_DEFAULT_MODEL` is set in
its environment; development mode logs a warning. Those names are also on the
sandbox environment denylist, so generated code cannot read them from a
misconfigured host.

If you set those expecting them to switch the feature on, they will stop the
server instead. That is deliberate: the alternative is routing every user's
traffic through one operator key without anyone noticing.

`FF_TYPESAFE_ROUTING=false` is the kill switch. Read once at import, so
recreate the container to change it. With it off, every user takes the no-key
path: zero network calls, byte-identical first round.

---

## The seven seams

| Seam | Where | What it does |
|---|---|---|
| I1 | turn start | opens the routing call, concurrently with request preparation |
| I2 | before round one | collects the decision within the budget |
| I3 | round one | narrows the tool list, and only round one |
| I4 | security | adds a verdict; never removes a check |
| I5 | layout | arranges a result the style names |
| I6 | credentials | records the verification outcome against the fingerprint |
| I7 | HTTP Work `kind="chat"` | screens a background submission, security questions only |

**Round one only.** Later rounds always get the full list. When the decision
is not confident, the model gets the full list too — exactly as without a key.

**The security screen is additive.** A verdict can require human approval for
every tool call in a turn, or refuse the turn. It cannot allow a call the gate
stack denies, satisfy an approval the gate stack requires, or change any
recorded gate decision.

**The submission screen asks nothing else.** The HTTP Work chat path has
nobody watching it, so it asks the three security questions and skips the
routing half — paying for answers nobody reads would be spending the user's
quota. A `confirm_tools` verdict there is a pass, because there is nobody to
confirm; the executor's own gate stack is unchanged and still runs.

---

## Measured, not assumed

Every constant in this PR was a provisional value until it was measured
against the real service on 2026-09-17. The numbers are in
`specs/089-typesafe-a8p-integration/verification.md` §8.2 and §8.16.

| Bound | Value | Where it came from |
|---|---|---|
| Per-turn routing budget | 1.5 s | the measured p95 with headroom for three attempts |
| Attempts per turn | 3 | |
| Per-attempt timeout | 400 ms | measured p95 205–365 ms across catalog shapes |
| Backoff | 100 ms, then 200 ms, ±20% jitter | clipped to the remaining budget |
| Circuit opens after | 3 consecutive turn-level fallbacks | |
| Circuit cool-down | 300 s | then one half-open trial |

Real-service latency: **p50 186–240 ms, p95 205–365 ms**, near-flat from 4 to
260 catalog options, across ~180 live calls with zero transport or API errors.

| Criterion | Result |
|---|---|
| SC-001 — unkeyed turns unchanged | **PASS**. 0 calls, 0.0 ms p95, with and without the kill switch. The first progress frame is emitted on accept, *before* the routing seam opens, so routing cannot delay it. |
| SC-002 — keyed added wait ≤ 150 ms | **PASS**. 203 keyed turns driven through the real socket: added wait `max(0, routing − preparation)` is **0.0 ms at p50, p95 and max**. Every routing call finished inside the preparation it overlapped. |
| SC-003 — ≤ 1.5 s under every injected failure | **PASS**. Seven failure modes, 25 turns each, all inside the budget, no hung turn, no decision from a failed call. |
| SC-004 — ≤ 5 ms once the circuit is open | **PASS**. 0.0 ms p95, 0 calls. |
| SC-005 — ≥ 95% high-tier acceptance | **PASS**. 1.000 (11/11); agent and tool accuracy 1.000. |
| SC-006 — security false-positive bound | Confirmation FPR **0.0%** after calibration (was 15%); 18/18 attacks flagged. |
| SC-007 — no key escapes | **PASS**. 0 hits for a synthetic canary or its prefix across 407 tests, log records, credential value objects, artifacts and the container log. |
| SC-011 — no client or workflow change | **PASS**. 0 across all five repositories, 197 changed files. |

### How SC-002 was measured

The keyed seam's p95 is 274.6 ms **with nothing overlapping it** — an upper bound, not the
"added wait" the criterion bounds. The turn opens routing early and collects the decision
several hundred lines later, after the history load, tool assembly, permission checks and prompt
build, all of which is time the routing call was already spending. So the added wait is
`max(0, routing − preparation)`, paired **per turn** — percentiles of a difference are not the
difference of percentiles.

Across 203 keyed turns driven over the same WebSocket the web client uses, that quantity was
**0.0 ms on every single turn**. Routing never finished later than the preparation it overlapped,
so it never delayed the first model call at all.

Read it with three qualifications, all recorded in verification.md §7f–§7g and §8.16: the local
posture is the product's own `USE_MOCK_AUTH` development path, which is not evidence about the
production realm; `FF_USER_SKILLS` and the four platform meta-tool flags were off for the batch,
which makes preparation *shorter* and the added wait *larger*, so the number is conservative;
and the client-side half — Send to the first frame that puts something on screen — is **9.6 ms
at p50**, on a frame emitted when the turn is accepted, before the routing seam opens.

### The refusal tier is disabled

In calibration it produced zero false refusals, but a 20-prompt benign corpus
cannot demonstrate the 0.5% false-positive rate the feature requires. Enabling
it needs a benign corpus of several hundred prompts drawn from real traffic.
Until then a would-be refusal is served one step down as "confirm every tool
call", so the signal is not lost.

---

## Five defects this work found in itself

**The committed key pattern was wrong.** The first TypeSafe pattern was
inferred from other vendors' prefixes. Checked against a real key, **it did
not match** — the log scrubber, the audit guard and the gitleaks rule would
all have passed a real key straight through. A scrubber that misses the
credential it exists for is worse than none, because it creates the belief
that logs are safe. The corrected pattern is derived from the observed format
and is regression-tested with a synthetic key in the real shape.

**ROTE did not enforce web-only for un-negotiated clients.** Fixed in
AstralProjection; described in that PR.

**The data-sharing gate never ran on the path the web client uses.** US7's gate lived in the
surface handlers; a browser save is admitted as a durable operation and executed elsewhere,
which never consulted it. On an account that had never acknowledged, the save went through, a
config row was written and the endpoint probe reached the provider. Fixed at the top of the
durable path, and re-verified through the browser.

**A TypeSafe key saved from the web client was never saved at all.** `chrome_typesafe_save` was
routed to an executor that only knows how to perform an LLM config set, and the TypeSafe store
has no fenced commit for it to call. The result through a real browser: no probe, no
persistence, no message, no log line, status `Not set` forever. The feature's headline
capability did not work through its own UI. Fixed; restoring the durable routing needs a fenced
TypeSafe commit in the Plane repository and is recorded as a follow-up.

**The quickstart's fault-injection switch was never installed.** `ASTRAL_TEST_TYPESAFE_FAULT`
existed as a wrapper class and a posture check that nothing ever called, so section 4 of the
walkthrough could not be performed and had never been. Fixed, with six tests pinning the path.

**This feature's own routing tests never saw a realistic tool list.** Seven of seventeen failed
on any Compose-started stack: they assert round one saw the *full* eligible list and hardcode the
fixture agent's two tools, while the orchestrator injects seven platform meta-tools into every
turn. They passed in CI and in the full-suite comparison because that runner uses
`docker run --env-file`, which passes inline comments through as part of the value — and 56 of
this `.env`'s variables carry one, so `FeatureFlags._read` silently read them as false. The
regression comparison stands (both sides ran the same way); the absolute "the TypeSafe tests
pass" claim did not. Fixed, and a new test pins what the seven were hiding: narrowing removes the
platform meta-tools from round one, and round two restores them.

---

## Audit and metrics

| Event class | Action types |
|---|---|
| `typesafe_credential` | `.saved`, `.cleared`, `.discarded` |
| `typesafe` | `.security_verdict`, `.routing_fallback` |
| `llm_data_sharing` | `.acknowledged`, `.save_blocked` |

None carries request text or key material. The credential events carry the
12-character fingerprint, which exists only to tell one saved key from another.

`typesafe_routing_total` and `typesafe_layout_applied_total` are labelled with
`result_code` and `phase`, both closed sets; the metrics layer refuses anything
that is not a bounded token, so a user id or a tool name cannot reach an
exporter.

---

## Owner exceptions in force

- **E1: CI is ignored.** Every CI-equivalent check was run locally and
  recorded. No workflow file is modified in any of the five repositories,
  enforced by `scripts/verification/check_089_scope.py` and its 14 guard tests.
- **E2: web-only client changes.**
- **E3: desktop-only parity target.**
- **E4: staging is a local candidate stack.** No deployment is authorised.
- **E5: `typesafe-sdk==0.6.0` pinned under Constitution v3.0.0**, which removed
  the approval gate; additionally owner-approved on 2026-09-17.

## Known divergences (owner-accepted)

KD-1, KD-2 and KD-3 all live in AstralProjection and are described there: the
native and Windows manifest drift guards fail once the six types exist, and are
deliberately not edited because they are client tests.

## Composition

`config/astral-composition.json` is repinned to the 089 commits: primitives
0.4.0, data plane 089.001, and the AstralProjection commit. Verified locally
with `scripts/verify_composition.py`.

## Outstanding at the time of writing

- **The refusal tier's benign corpus** — several hundred prompts from real traffic, after which
  re-running `scripts/verification/typesafe_routing_bench.py --mode benign` decides whether
  `REFUSE_TIER_ENABLED` flips. No code changes with it.
- **T021's native-client half** — no native client build was available. 089 changes no client
  code (SC-011: 0 client-directory changes across five repositories), so the same server-driven
  surface reaches a native client, but that is an argument rather than an observation.
- **Two follow-ups against the baseline, not this feature.** `skill_lookup_unavailable` fails
  every chat turn when `FF_USER_SKILLS` is on, traced to `current_socket_human_read` reading a
  ContextVar the chat path threads in explicitly; whether the production-auth path behaves the
  same is unresolved and needs one realm-authenticated turn. And `docker run --env-file` against
  a commented `.env` silently disables flags, which is how the suite comparison ran.
- **Restoring `chrome_typesafe_save` to the durable path**, which needs a fenced TypeSafe commit
  in AstralPlane.
