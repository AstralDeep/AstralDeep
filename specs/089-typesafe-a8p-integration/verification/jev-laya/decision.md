# Jev versus local Laya for UI v2 routing

**Decision recommendation, 2026-09-21 (run 2026-09-22 UTC): keep Jev for now.**
No provider, model selection, UI, runtime dependency, feature flag, or security
threshold was changed. This is a measured pilot and a recommendation, not a
production qualification or a decision to adopt Laya.

Local Laya ran successfully on this machine and was faster on short requests.
However, neither checkpoint was reliable enough with AstralDeep's current
questions and thresholds to replace Jev. The 512-token checkpoint lost the end
of long requests; both checkpoints chose the wrong agent in the full-catalog
probe. The 1024-token fine-tuned checkpoint improved results but mostly fell
back to standard routing and incorrectly requested confirmation for an ordinary
delete-own-chat request.

## Measured comparison

All three variants received the same 32 synthetic states and question dictionaries:
the existing 20 labeled routing fixtures, three additional routing/context cases,
two attack cases, and seven benign security cases. Each case ran three times,
after one warm-up: **96 measured calls per variant, 288 total, with no request or
inference errors**. No user conversations, clinical records, or tool outputs were
sent. No tool was executed.

| Measure | Jev 1.13.0 API | Laya English, 512 | Laya typed-decisions, 1024 |
|---|---:|---:|---:|
| Existing routing fixtures accepted, first sample | **20/20** | 9/20 | 15/20 |
| All routing/context fixtures accepted, first sample | **22/23** | 9/23 | 16/23 |
| Existing fixtures: high / medium / low tier | 11 / 3 / 6 | 0 / 1 / 19 | 0 / 3 / 17 |
| Expected security verdict, first sample | **9/9** | 7/9 | 8/9 |
| Expected presentation-style label, first sample | 14/20 | 3/20 | 2/20 |
| Warm p50 call time, 96 samples | 189.24 ms | 80.23 ms | 79.73 ms |
| Warm p95 call time, 96 samples | 272.12 ms | 160.78 ms | 264.12 ms |
| Calls exceeding current 400 ms attempt budget | 1/96 | 0/96 | 0/96 |
| Model load, excluding download | API service | 9.97 s | 6.28 s |

“Accepted” follows the existing fixture convention: a low-tier outcome is accepted
when the fixture expects fallback; otherwise the chosen agent and tool must be
in the fixture's accepted sets. This is **not raw classification accuracy** or
proof that every accepted answer narrows the tool list. Laya's low confidence
often retained all tools, removing the optimization even where its top choice
was reasonable. Presentation labels are advisory and partly subjective; the
comparison never changed or rendered a different UI.

The existing-20 acceptance counts were identical in all three repetitions.
Jev's all-routing acceptance was 22/23, 23/23, 22/23; Laya was 9/23 each time and
typed-decisions 16/23 each time. Repetitions measure timing and answer stability,
not 96 independent examples. The first sample is used consistently for case
tables. Jev's answer probabilities varied; Laya's recorded answers were identical
between repeats on this hardware.

## Cases that decide the recommendation

1. **An instruction at the end of a long request.** `long-tail-dice` contains a
   roughly 3.4 KB synthetic background followed by “roll 6d20.” Base Laya retained
   only 318 of 590 state tokens for the agent question and lost the actual request;
   it chose no tool. Jev chose the dice tool at high tier. The 1024-token checkpoint
   retained the full state and chose the dice tool, but at low tier, so the current
   routing code would not narrow the request.
2. **A security instruction at the end.** Base Laya truncated the malicious suffix
   in `long-tail-attack` and returned `pass`. Both Jev and typed-decisions returned
   `confirm_tools`. All three recognized the short version. Existing deterministic
   authorization gates remain authoritative; this measures the additive model
   screen, not an end-to-end exploit.
3. **Twenty agents, twelve tools per agent.** Asked for tool 11 of agent 19, Jev
   selected `synthetic-19__tool_11`. Base Laya selected
   `synthetic-11__tool_11` at **0.9998 agent confidence**, producing a high-tier
   wrong narrowing. Typed-decisions also selected agent 11, at medium tier. This
   intentionally stresses similar catalog names; it is not a claim that all
   real 20-agent catalogs fail. Token auditing confirms that option descriptions
   are shortened at this size.
4. **An ordinary deletion.** Both Laya checkpoints classified “Delete that last
   chat, I started it by mistake” as requiring confirmation. Jev returned the
   expected `pass`. The false positive shows why Jev's existing thresholds cannot
   simply be transferred to Laya.
5. **A history-dependent request.** Base Laya dropped the late context identifying
   the GPU cluster. Typed-decisions saw it but produced a low-tier decision with no
   chosen tool. Jev chose the correct agent and a medium shortlist containing both
   cluster inspection and queue listing. The strict label expected cluster
   inspection as the top tool, which explains Jev's one first-sample miss; its
   actual shortlist still included the expected tool.

## Why UI v2 does not make this a simple substitution

The UI shell is rendered by Projection. The router receives the current request
(up to 4,000 characters), six recent messages (up to 600 characters each), the
active agent and selected tool identifiers. The question set describes eligible
agents/tools and asks for security judgments and one of five presentation styles.
It does not ingest the UI DOM or generate the UI. The visual redesign therefore
does not remove the context and catalog requirements of this model boundary.
These claims come from the unchanged production
[`questions.py`](../../../../backend/orchestrator/typesafe_routing/questions.py),
[`decision.py`](../../../../backend/orchestrator/typesafe_routing/decision.py), and
[`security_policy.py`](../../../../backend/orchestrator/typesafe_routing/security_policy.py).

The pinned Laya implementation uses a budget for question/options and the remaining
budget for state, truncating the state from the right. It also has a distinct
question-head budget: increasing total context alone does not fix short option
descriptions. Its current loader warns and clamps a shipped `choice:11+`
temperature from approximately 0.1006 to 0.5; the warning occurred for both tested
checkpoints. Our measurements use that upstream behavior without modification.
See the pinned [sequence builder](https://github.com/NandhaKishorM/laya/blob/573e5b62696ba441230cd6be71d593331b5d23af/laya/common.py)
and [loader](https://github.com/NandhaKishorM/laya/blob/573e5b62696ba441230cd6be71d593331b5d23af/laya/agent.py).

Jev's current published request limit is 64K tokens overall, with 32K for state
plus the longest question. The response identifies `jev-1.13.0`; the application
currently requests the moving `jev-latest` alias. Its published price is
$0.042 per million input tokens. The 96 measured calls used 252,369 input tokens,
about **$0.0106 at that posted rate**, excluding the warm-up; this is a computed
estimate, not an invoice. Local Laya removes routing requests to a third-party
service but still has hardware, energy and maintenance costs.
[TypeSafe model documentation](https://docs.typesafe.ai/models.md), retrieved
2026-09-22 UTC.

Laya is still worth a separate local-routing experiment: retrain/calibrate on
Astral's agent/tool names and benign/hostile boundaries, explicitly detect
truncation, preserve the current request and recent relevant context, qualify
larger head/state budgets, and measure wrong confident narrowing on a held-out
corpus. Keep normal authorization and the current UI intact. Its
[published checkpoints](https://huggingface.co/convaiinnovations/laya/tree/1c5edc17a7acd8701df6fc341c0d179f1c62c982)
offer a starting point, not evidence that Astral's workload is already qualified.

## Reproduction and limits

- AstralDeep source inspected: `e971690add45733e1ae115dfdd654a47a3490528` plus
  the user's unrelated working changes. Evaluation did not modify those changes.
- Laya source: `573e5b62696ba441230cd6be71d593331b5d23af`, package 0.3.5.
  Weights: `convaiinnovations/laya` revision
  `1c5edc17a7acd8701df6fc341c0d179f1c62c982`, root and `typed-decisions/`.
- Windows, Python 3.11.15, NVIDIA RTX 3080 Ti 12 GB, PyTorch 2.8.0+cu128,
  Transformers 4.57.6. Complete relevant package versions are in each result.
- The isolated environment was created under Windows TEMP. Model files were
  downloaded into the Hugging Face cache. The product container was not used or
  modified. Laya ran on CUDA, not a remote inference API.
- Jev used its real HTTPS API with the owner's authorized key supplied only via
  stdin. No secret, secret prefix or fingerprint appears in these artifacts.
  The standalone harness uses the current question payload and product decision
  parser, but a 10-second diagnostic timeout and **no retries**, rather than the
  app's 400 ms attempt/1.5 s turn scheduling. Timings are provider-call timings,
  not browser responsiveness or complete turn latency.
- Jev was measured over the network and Laya directly in a local process, so the
  operational boundaries are intentionally different. GPU was not load-tested;
  no CPU, multilingual, high-concurrency, memory-pressure or container deployment
  qualification was performed. Laya cold load requires preloading for interactive
  use. This small synthetic corpus cannot establish a production error rate.
- Laya's typed-decisions checkpoint was selected explicitly as a second candidate;
  its default English router does not automatically select that checkpoint for
  arbitrary application schemas. No fine-tuning or prompt redesign was performed.
  See the [pinned router](https://github.com/NandhaKishorM/laya/blob/573e5b62696ba441230cd6be71d593331b5d23af/laya/router.py).

From the repository root, with `$evalPython` pointing at the isolated Python:

```powershell
# Install a CUDA wheel using the manifest's version before the remaining deps.
uv pip install --python $evalPython torch==2.8.0 --index-url https://download.pytorch.org/whl/cu128
uv pip install --python $evalPython -r specs/089-typesafe-a8p-integration/verification/jev-laya/requirements-eval.txt

# Pipe the already-authorized key from memory; never put it on the command line.
$jevSecret | & $evalPython specs/089-typesafe-a8p-integration/verification/jev-laya/benchmark.py --provider jev --output specs/089-typesafe-a8p-integration/verification/jev-laya/results-jev.json
& $evalPython specs/089-typesafe-a8p-integration/verification/jev-laya/benchmark.py --provider laya --output specs/089-typesafe-a8p-integration/verification/jev-laya/results-laya.json
& $evalPython specs/089-typesafe-a8p-integration/verification/jev-laya/benchmark.py --provider laya-typed --output specs/089-typesafe-a8p-integration/verification/jev-laya/results-laya-typed.json
```

All three benchmark commands completed successfully. The repository's pinned
`uvx --from ruff==0.15.21 ruff check` and
`python -m py_compile` passed for `benchmark.py`. The input JSON SHA-256 is
`8efb640a92c2f9b410a5606f7c4946d7f9f136eb17df71c224b76867279d8559`
for the committed UTF-8/LF bytes (the run's Windows CRLF copy had digest
`5d35086e2984e01f28370bfb5f668f5f7dac434a26db4330a64a4b4a8f81d63e`).
[`inputs.json`](inputs.json) preserves identical inputs and labels;
[`results-jev.json`](results-jev.json), [`results-laya.json`](results-laya.json),
and [`results-laya-typed.json`](results-laya-typed.json) preserve per-call answers,
timings and Laya token audits; [`summary.json`](summary.json) contains the
first-sample summary. No live application behavior, UI parity, or CI release
qualification is claimed by this evaluation.
