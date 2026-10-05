# SoL-Pi review for the AstralDeep ecosystem

**Date:** 2026-10-04 (America/New_York). **Decision state:** research complete; adoption PROPOSED, not ratified, implemented, benchmarked, or deployed.

Give agents exact, owner-scoped recall of large tool outputs before making compaction more aggressive. That offers a practical route to shorter prompts while preserving the evidence users need to trust answers.

## Source identity and findings

Reviewed: **SoL-Pi: Recursively Scaling Auto-Research Loops for Efficient Agent Harness**, Haozhe Liu et al., NVIDIA/NTU/MIT, **arXiv:2609.20519v1**, submitted **2026-09-17**, a 15-page preprint retrieved **2026-10-04**. The retained mechanisms are Action Fusion, Online Context Compact, ObservationPack, and Evidence-Preserving Reducer. [Versioned paper](https://arxiv.org/html/2609.20519v1), [PDF](https://arxiv.org/pdf/2609.20519v1).

The paper reports the following point estimates; these are not AstralDeep reproductions:

| Evaluation | Pi baseline | Complete SoL-Pi stack | Qualification |
|---|---:|---:|---|
| EdgeBench / GPT-5.6 Sol: score; API cost | 44.833; $1,339 | 42.003; $894 | 49.0% less recorded token traffic; lower score |
| EdgeBench / Opus 5: score; API cost | 44.756; $1,741 | 42.224; $1,158 | 44.7% less recorded token traffic; lower score |
| Terminal-Bench 4: solved tasks | 18/63 | 15/63 | Fewer tasks solved |
| GPT-5.6 Sol / ObservationPack alone: score; cost | 44.833; $1,339 | 47.208; $1,271 | Promising standalone candidate |

Sources: [paper Tables 1–4](https://arxiv.org/html/2609.20519v1#S3). Prices are fixed at 2026-08-17. EdgeBench uses 51 public tasks, with 11 for acceptance and 40 for final evaluation. The study covers coding/formal mathematics, two backends, and one swarm run per configuration; it does not evaluate clinical workflows or user-interface usability. The performance operating point selects the best standalone mechanism. The compaction cost gate excludes the summarization call. [Methods, evaluation and limitations](https://arxiv.org/html/2609.20519v1).

The implementation is a TypeScript extension for Pi 0.85.1 requiring Node.js 22.19+, with mechanisms disabled by default. It is a reference for mechanisms, rather than an AstralDeep runtime dependency: Deep's backend is Python and its dispatcher already owns policy. [Upstream README](https://github.com/NVlabs/SoL-Pi/blob/e1a586af0ad8956f42ae5b26bba20e48fbf30e00/README.md).

Upstream archives remain after session exit; its reducer may send logs to another model. Its likely-secret detector is explicitly incomplete, and the extension carries the loading process's permissions rather than providing a sandbox. These behaviors need AstralDeep-specific treatment before adoption. [Upstream security policy](https://github.com/NVlabs/SoL-Pi/blob/e1a586af0ad8956f42ae5b26bba20e48fbf30e00/SECURITY.md).

Upstream documentation/code inspected at **e1a586af0ad8956f42ae5b26bba20e48fbf30e00**, resolved from its main ref on the retrieval date. A quote verifier demonstrates that a selected quotation occurs in the source and that status matches the observed error flag. It does not establish completeness, causal diagnosis, or the truth of the source. [Upstream verifier](https://github.com/NVlabs/SoL-Pi/blob/e1a586af0ad8956f42ae5b26bba20e48fbf30e00/src/sol-pi/extensions/evidence-preserving-reducer/receipt.ts).

## Verified AstralDeep baseline

Code was inspected at **c4a47d7067c5b3a475bf516423ba614640de784b** in the Gaia refresh worktree. The relevant code is identical to the initially inspected b63c961b0aeb1749909c3a1043d3ed535d26bd72 snapshot. Concurrent Gaia edits are outside this report's ownership. This review did not inspect production environment values.

- **Context trimming exists.** `edit_context()` preserves recent tool rounds, but replaces eligible older tool content with a generic tombstone. The placeholder contains no observation identity or exact-recall instruction. This describes the working prompt, not a claim that every source copy elsewhere has been erased. [context_engineering.py](https://github.com/AstralDeep/AstralDeep/blob/c4a47d7067c5b3a475bf516423ba614640de784b/backend/orchestrator/context_engineering.py#L57).
- **Compaction exists.** It estimates tokens using character counts and a model-name window table, triggers at a budget threshold, and protects the current user turn and tool/result groupings. Each older message supplied to the summarizer is cut at 3,000 characters. An exception or empty summary substitutes a generic notice, after which old turns can still be removed. [compaction.py](https://github.com/AstralDeep/AstralDeep/blob/c4a47d7067c5b3a475bf516423ba614640de784b/backend/orchestrator/compaction.py#L132).
- **Compaction has an owner-routing seam.** Its callback is invoked as `llm_call(None, summary_prompt)`. For ordinary chat, `_resolve_llm_client_for(None)` selects the system configuration, and `_llm_audit_principals(None)` records system/system. The main loop's usage accumulator covers its direct call; the compaction callback discards its returned usage. This is code-observed behavior behind a flag, not evidence of a live incident. [summary callback](https://github.com/AstralDeep/AstralDeep/blob/c4a47d7067c5b3a475bf516423ba614640de784b/backend/orchestrator/compaction.py#L215), [routing and identity](https://github.com/AstralDeep/AstralDeep/blob/c4a47d7067c5b3a475bf516423ba614640de784b/backend/orchestrator/orchestrator.py#L15450), [main loop](https://github.com/AstralDeep/AstralDeep/blob/c4a47d7067c5b3a475bf516423ba614640de784b/backend/orchestrator/orchestrator.py#L14637).
- **Model and rendered outputs already have separate paths.** `_tool_result_to_llm_content()` prefers `_model_digest`, then `_data`; rendered components have their own delivery path. A digest is not, by itself, a source-quotation verifier. The loop skips datamarking for this trusted digest path, so a new reducer must not promote untrusted excerpts into trusted instructions. [model output selection](https://github.com/AstralDeep/AstralDeep/blob/c4a47d7067c5b3a475bf516423ba614640de784b/backend/orchestrator/orchestrator.py#L16227), [datamarking boundary](https://github.com/AstralDeep/AstralDeep/blob/c4a47d7067c5b3a475bf516423ba614640de784b/backend/orchestrator/orchestrator.py#L14900).
- **Bounded, grounded research is already implemented.** `page_passages()` creates deterministic passages; `build_page_result()` selects only IDs that exist in the captured observation and binds source identities/digests. `project_research_result()` rechecks owner, current state, source actions, and retained result references. Extend these patterns rather than introduce a second authority system. [research_result.py](https://github.com/AstralDeep/AstralDeep/blob/c4a47d7067c5b3a475bf516423ba614640de784b/backend/persistent_agents/research_result.py#L170), [work_result.py](https://github.com/AstralDeep/AstralDeep/blob/c4a47d7067c5b3a475bf516423ba614640de784b/backend/orchestrator/work_result.py#L203).
- **Progress and accounting foundations exist.** Chat steps persist redacted state and emit shared `chat_step` events. Main chat accumulates prompt/completion/total tokens; LLM audit stores total tokens and routing identity. The inspected paths do not yet provide full cache-read/cache-write and reducer/compaction cost accounting. [chat_steps.py](https://github.com/AstralDeep/AstralDeep/blob/c4a47d7067c5b3a475bf516423ba614640de784b/backend/orchestrator/chat_steps.py#L77), [usage accumulator](https://github.com/AstralDeep/AstralDeep/blob/c4a47d7067c5b3a475bf516423ba614640de784b/backend/orchestrator/orchestrator.py#L15397), [audit_events.py](https://github.com/AstralDeep/AstralDeep/blob/c4a47d7067c5b3a475bf516423ba614640de784b/backend/llm_config/audit_events.py#L170).
- **The relevant flags default off.** `FF_MESSAGE_COMPACTION` and `FF_CONTEXT_ENGINEERING` are false by default. Their deployed values are unverified. [feature_flags.py](https://github.com/AstralDeep/AstralDeep/blob/c4a47d7067c5b3a475bf516423ba614640de784b/backend/shared/feature_flags.py).

Existing tests cover prompt trimming, tool/result grouping, two-tier output and deterministic research results. This report reviewed them as evidence of intended behavior; it did not rerun or qualify them.

## Prioritized adoption candidates

All benefits below are **inferences for AstralDeep**, not paper-established or locally measured outcomes. Effort estimates are engineering estimates for one experienced contributor and exclude unavailable platform runners and external release scheduling.

| Order | Proposed work | Practical user benefit | Primary seam | Initial effort |
|---|---|---|---|---|
| 0 | Establish task quality and complete usage baseline | Avoid trading reliable answers for an attractive token chart | LLM audit, usage accumulator, perf spans, evaluation tooling | 2–4 days |
| 1 | Make compaction owner-bound and failure-preserving | Fewer forgotten facts and predictable billing/provider behavior | `compaction.py`, callback in ordinary dispatcher | 2–4 days |
| 2 | Add owner-scoped exact observation recall | Long dataset/research sessions can revisit precise evidence without repeating every large output | `context_engineering.py`, tool-content projection, ordinary dispatcher | 5–10 days for bounded ephemeral pilot |
| 3 | Verify diagnostic receipts before accepting reductions | Shorter failure explanations backed by exact source excerpts | Tool-result projection; existing passage/digest validation patterns | 3–6 days |
| 4 | Add measured, plan-boundary compaction decisions | Longer tasks complete within budgets with fewer disruptive resets | Host task/step state, compaction, full accounting | 3–5 days after baseline and reliable recall |
| 5 | Fuse one fixed, authorized workflow | Fewer waiting turns for predictable work | Bundled agent tools, normal dispatch, per-step audit | 3–7 days for one workflow |
| 6 | Run offline, isolated harness experiments | Systematically improve the harness on representative work | Existing eval/security harnesses and immutable candidates | 1–3 weeks for first bounded campaign |

### 1. Reliable compaction is the prerequisite

Bind the summarization callback to the same owner, configured route, permitted data policy and accounting context as the originating call. Record compaction as its own operation, charge all model use, and preserve dispatch budgets for persistent work. Do not silently switch to system credentials because a callback lacks a WebSocket.

Treat a failed, empty, unverifiable, or ineffective summary as a rejected optimization: retain the original prompt where it fits. At actual context exhaustion, return a clear, recoverable outcome or bounded continuation carrying verified evidence rather than assert successful summarization. Protect user corrections, pending confirmations, authorization outcomes, outstanding work and source references. A narrative summary must never serve as an authorization record.

An initial feature can improve this failure behavior independently of an economic compaction model. Unit tests should make the current fallback and owner-routing seams explicit before changing them.

### 2. Observation recall is the strongest functionality candidate

Separate the authoritative observation from the provider-facing projection. For an eligible large result, preserve a bounded source object and send a shorter envelope containing an opaque identity, digest, size, excerpt completeness, provenance, and exact paging instructions. Use recent full observations when they aid the immediate decision; measure the number of full sends and threshold instead of fixing them to another harness's values.

An exact-recall operation must verify owner, conversation/operation, source revision and current session incarnation through the existing dispatcher. A content digest is an integrity check, never read authority. Bound page bytes, total retained bytes, active handles and recall count; reject foreign, expired, revoked and malformed handles. Enforce retention cleanup and cancellation, and make restart behavior explicit.

Start with transient, already-authorized, non-sensitive text. A durable source archive requires AstralPlane's facade and declared storage contract; any new schema/blob mechanics belong in Plane first. Do not silently add retained copies of PHI or credentials. For GaiaKeep workflows, a handle can point to an authorized immutable dataset/version observation; it must not turn the dataset digest into a download credential.

The inferred usability benefit is a reliable answer to “show me the exact result behind that conclusion,” even after a long conversation. Present excerpts through existing approved primitives and shared server-owned surfaces; add a new primitive only if the existing vocabulary cannot express the interaction.

### 3. Diagnostic reduction should select evidence, not invent explanations

Pilot on a closed set of non-sensitive build/validation outputs. Prefer deterministic extraction or model-selected passage IDs backed by host-owned source bytes. If a secondary model is used, require the same normal provider/data authorization, a separately reserved budget, timeout and complete accounting.

Validate the closed receipt schema, source identity/digest, command result, valid quote ranges and bounds. A failure result remains a failure even if the selected excerpts sound reassuring. Keep an explicit completeness/uncertainty field and exact recall. On timeout, invalid selection, hash mismatch, privacy refusal, missing failure evidence or no net reduction, keep the original authorized observation.

Do not route model-produced receipts directly into the trusted `_model_digest` exemption. Exact source text remains untrusted data even when its bytes are verified. Reuse the deterministic passage-selection pattern already present in persistent research rather than trust free-form summaries.

This could reduce users' time locating the actionable part of a failure, but quotation accuracy alone cannot prove the reducer found every important failure or made the right diagnosis.

### 4. Economic compaction needs a complete cost model

Use completion of host-verified work as a decision boundary; a model-authored plan is a scheduling hint, not proof that a task succeeded. The current phase recorder marks LLM and tool phases, so it needs an explicit distinction between those phases and completed user-level subtasks.

Estimate savings from remaining requests and measured growth, then account for cache reads/writes, summarization, recalls, secondary calls, retries and output. Unknown prices or cache counters must disable speculative early compaction, while a verified context-limit guard still applies. Use verified model limits and a calibrated token estimate rather than extend the current substring table indefinitely.

Maintain stable prompt prefixes where practical, but optimize total cost per accepted outcome. A higher cache-hit ratio alone is not a useful acceptance criterion.

### 5. Action fusion fits fixed workflows with independent authority

Choose a deterministic chain such as materializing an approved dataset version and validating its manifest/checksum locally. Return separate step outcomes, preserve cancellation and idempotency, and never label a partial result as success. Fuse only when the second step does not need a new user decision or model inspection of the first result.

Authorize and audit each operation. A combined tool must not inherit the permissions of its least-sensitive step, turn reads into writes, conceal a required confirmation, or expose a model-authored arbitrary-shell follow-up. Existing parallel dispatch already reduces independent calls; fusion is for a known sequential dependency.

This is likely a smaller latency improvement for specific agent workflows than a platform-wide transformation. Measure end-to-end waiting time on a dedicated runner and the number of provider requests for the chosen workflow.

### 6. Auto-research belongs in isolated candidate workspaces

Have analysis agents propose small harness changes from sanitized development traces, then evaluate candidates in disposable workspaces with fixed budgets and independent review. They may propose code; deterministic tooling owns acceptance. Freeze the candidate, metric definitions and tolerances before held-out evaluation.

Keep acceptance criteria and held-out tasks outside the optimizing agent's writable scope. Evaluate several authorized providers because AstralDeep supports user-selected model routes. Promote through normal repository/component review and qualification; no model edits production policy, releases itself, or changes its own success criteria.

## Evaluation and adoption decision

Create a modest initial suite of **24 synthetic, non-PHI workflows**, divided in advance into 12 development and 12 held-out tasks. Include dataset retrieval/manifest validation, retrieval of a middle-of-output fact, multi-source research with exact passage references, interrupted/resumed work, denied foreign-owner access, revoked access, failed provider calls, rejected reducer receipts and cancellation. The partition must prevent near-duplicate task variants crossing sets. Expand before drawing population-wide conclusions.

Use a paired baseline for each candidate mechanism, with identical source snapshots, tool environments and a few predeclared repetitions to characterize model variance. Repetitions are optional research measurements outside required CI; deterministic integration fixtures carry the required regression gates. Freeze each candidate before the held-out run. Failure on held-out work rejects that frozen candidate and does not justify tuning against that same test set.

The following thresholds are proposed, not ratified:

| Dimension | Proposed acceptance rule |
|---|---|
| Task functionality | Every deterministic golden-path oracle passes; no known capability regression on the paired held-out suite |
| Authority/privacy | Every owner-isolation, revocation, confirmation and prohibited-retention denial passes; zero boundary bypasses |
| Evidence integrity | Every selected quotation/page matches its bound source; synthetic fixtures test omitted critical facts and misleading success excerpts |
| Failure handling | Reducer/compactor failure preserves usable evidence or produces an explicit recoverable limit; no fake success |
| Efficiency | At least 10% lower complete cost per accepted task on the selected long-output workload, including all auxiliary calls |
| Interaction | Users can recover the source of each displayed claim; record rereads, corrections and unnecessary clarification turns |
| Latency | Report distributions and time-to-first-progress on a dedicated runner; no shared hosted-runner wall-clock gate |
| Retention | Bounded space, cleanup/revocation and explicit restart behavior pass deterministic fixtures |

Do not treat 12 held-out tasks as statistical proof of generalization. Report paired outcomes and uncertainty, absolute dollars at dated provider prices, recorded token categories, calls, retries, recall traffic and output sizes. Preserve missing provider usage as unknown/conservatively charged rather than zero.

Inspect the existing `agent_eval.py` trajectory metrics and security-benchmark tooling as starting points. Tool sequence agreement alone is insufficient: the user can receive an incorrect result with the expected sequence, or a correct result with a different valid sequence.

## Repository and release scope

This review follows AstralDeep constitution **v6.1.0** (Principles I, II, VII, IX, X, XII, XIII and XIV), AstralPlane constitution **v1.0.0**, and AstralProjection constitution **v2.2.0**, read from the component repositories' current checkouts. Their requirements preserve Python 3.11, Keycloak/RFC 8693 authority, PHI/egress/confirmation gates, hash-chained audit, Plane-owned persistence and shared server-owned presentation.

A backend-only candidate can begin in Deep without a UI vocabulary change. Durable archives or schema changes must land and qualify in Plane before Deep moves its pin. Shared presentation changes land in Projection and reach all affected clients through its protocol and server definitions. No Primitives or LETS contract change is required merely to explore these harness mechanisms; reassess their owning constitutions if scope later crosses those contracts.

Future implementation must cover golden paths, edge cases, denials and failures with the required changed-line coverage and ordinary qualification. New live flows need the owning feature's staging and affected-client evidence. A research result does not authorize a production default or substitute for those checks.

## Completed review and remaining work

Completed: read the versioned full paper and PDF metadata, inspect reported results and method boundaries, inspect the official upstream README/security/verifier, read governing constitutions, and verify concrete code/test seams at the stated Deep snapshot.

Changed by this task: this Markdown report only. No product source, flag default, schema, dependency, component pin, protocol, issue, Git branch, commit, merge, deployment or release was changed by the research task. No tests or live client/staging experiments were executed for the report because no executable behavior was modified.

Recommended next checkpoint: define one bounded feature for owner-bound, failure-preserving compaction and exact observation recall, establish the baseline, and evaluate those mechanisms independently. Adoption remains PROPOSED until its implementation and exact-candidate evidence are reviewed.

## Current-work comparison, refreshed October 4

The following primary-source comparison was refreshed after the owner's research-first instruction. These papers do not establish clinical usability, tenant isolation or AstralDeep production safety; the application choices below are engineering inferences.

| Work and publication status | Demonstrated mechanism | AstralDeep inference |
|---|---|---|
| Rui Ye et al., *AgentFold: Long-Horizon Web Agents with Proactive Context Management*, [arXiv 2510.24699](https://arxiv.org/abs/2510.24699), 2025 preprint version consulted; conference status not verified because OpenReview returned a browser challenge | Learned multiscale folding for long web-search trajectories | Useful later for planning-boundary experiments; model training and lossy summaries add scope to the first infrastructure repair. |
| Alex L. Zhang, Tim Kraska and Omar Khattab, *Recursive Language Models*, [arXiv 2512.24601v3](https://arxiv.org/html/2512.24601v3), revised May 2026 preprint version consulted | External prompt variables and programmatic recursive model calls over long-context tasks | Consider for bounded corpus analysis later. A general REPL plus recursive calls needs separate containment, delegation, egress and spend qualification. |
| Xiaochuan Li, Ryan Ming, Meng Chu, Shuai Shao, Rong Jin and Chenyan Xiong, *ACM: Agentic Context Management for Long Horizon Tasks*, [arXiv 2607.23809v1](https://arxiv.org/html/2607.23809v1), July 2026 preprint | Stores discarded raw messages and uses context-management/recall tools; post-training improves evaluated search/coding outcomes | Supports preserving raw evidence first. Its model-mediated recall can still omit details; storage-level preservation does not prove recall correctness. |

The first recommendation remains owner-scoped exact observation pages and failure-preserving compaction. It fits SoL-Pi's ObservationPack and ACM's preservation direction with a smaller authority surface than model-controlled history replacement or a general recursive REPL. Deterministic authorization, integrity and bounded retrieval still win because none of these benchmarks qualifies those security properties. Keep measured quality, full costs and ordinary dispatch denials in the experiment; no reported gain here is an AstralDeep result.
