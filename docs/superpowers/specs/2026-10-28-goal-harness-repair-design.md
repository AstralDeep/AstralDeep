# Goal-directed request completion

The owner requests implementation, sandbox verification, merge, and a new Mac release. The existing Mac workbench specification already requires an observation–action–verification loop; this repair restores that behavior without changing its security or client contracts.

## Reproduced failure

The exact Minecraft prompt produced a canvas code artifact and static code review. The final answer said Done while telling the owner to install pyglet and run tests. The selected local project folder remained empty. This is partial delivery, not a verified runnable project.

## Behavior

Every request retains its original objective outside mutable and compacted conversation context. When enabled, a separate no-tools completion evaluation compares the proposed answer with the objective, validated project context, and recorded tool outcomes. Informational answers can complete without tools. Action requests continue through ordinary authorized dispatch until the requested effects and verification are evidenced, or report a concrete blocked/incomplete outcome.

For a runnable project in a selected folder, code generation alone does not satisfy the request. The planner uses the granted local folder, creates an appropriate isolated environment (a local `.venv` for Python), declares and installs dependencies, runs meaningful tests, and performs a startup smoke check. No product behavior is specific to Minecraft or Python.

The completion evaluator returns strict JSON with `status` (`complete`, `continue`, `blocked`), `remaining`, and `reason`. Invalid evaluation cannot authorize completion. Deterministic code also prevents completion while command jobs are pending or effects uncertain, bounds repeated failures/prose-only continuation, and distinguishes budget exhaustion from success. Continuation stays inside the originating request and its existing cancellation fence; it does not replay a request or confer new authority.

## Completion evidence contract

Before execution, a separate bounded classification freezes whether the original request asks for information, an action, or a runnable local project. Informational requests can finish without tools; actions require a cited completed tool outcome. Runnable local projects require references for files, environment, dependencies, tests and startup. The verifier judges whether an actual command is relevant and meaningful, while deterministic validation requires every reference to name a real successful command or appropriate file operation from the originating request, computer-use agent, authorized host, project and folder. File operations cannot prove test or startup execution; pending background starts require a correlated later successful terminal observation. Current folder authority and cancellation are checked again immediately before accepting completion.

Failure resolution cannot waive an invocation with an unrelated success. Retries retain the same invocation and trusted identity; background observations retain the same job. An unknown file write requires a same-path, untruncated read with a digest matching the requested bytes. Unknown deletions remain unresolved. The bounded classifier and evaluator still make semantic judgments; these guards establish observed execution and scope, without claiming a general proof that arbitrary software is correct.

## Progress checkpoints

After each eight tool outcomes, a separate bounded review compares recent execution with the original objective and frozen requirements. It can suggest concrete next steps or report a genuine block; it cannot declare completion, grant permission, or resolve uncertain effects. The ordinary planner receives its advice as quoted data within existing authority. Cancellation and the execution fence are rechecked before planning resumes, and the final evidence gate remains mandatory. Whole-file writes, parent creation, readback, setup, tests and startup form observable milestones rather than an open-ended series of tiny writes or probes.

Provider tool-call IDs remain correlation data for provider messages. Goal observations allocate unique request-local identities when those IDs repeat or collide, so failure and completion evidence cannot alias separate operations. A known failed write may be explicitly resolved by an authorized successful retry with the same canonical path, content digest and trusted identity when overwrite mode changes; uncertain writes still require the existing matching readback.

## Model transport and terminal failures

Goal planning retains permitted provider streaming while buffering the proposed narrative until completion verification. Buffering does not change provider eligibility, evidence capture, selected harness or persistent-worker restrictions. A buffered stream must carry a recognized terminal finish reason before assembled tool calls are returned; clean EOF without that evidence is malformed and cannot dispatch partial arguments. Truncation retains its explicit finish reason so the ordinary goal gate can reject it. Cancellation after an awaited planner response or dispatch-status send discards late tool calls before effects begin. Completed provider usage remains accounted for even when the result is cancelled. Exhausted model calls publish the original user request and a durable incomplete outcome atomically rather than returning before conversation publication; cancellation retains its own terminal publisher.

## Visible terminal outcomes

A failed goal retains its committed assistant warning in the canonical conversation snapshot. The chat-rail reducer preserves standalone and nested Alert and List components while retaining its existing canvas deduplication. Native-profile admitted tests verify the actual emitted snapshot, not only stored message rows, so a durable incomplete outcome cannot disappear during projection.

## Scope and rollout

Deep owns the request controller, evidence, project context, and evaluator integration. Existing tool, provider-disclosure, audit, project, confirmation, and Stop paths remain authoritative. No SQL migration, new primitive, or client protocol is required. A default-off `FF_GOAL_HARNESS` switch supports qualification and rollback; the sandbox will enable it for every ordinary request. A bounded configurable goal round budget replaces the small legacy budget only when enabled. No failed or incomplete request is marked completed.

This work uses independent cheaper implementation and verification agents, coordinated and reviewed by the root agent as the owner requests. Runtime model selection continues to use the owner's configured providers and normal routing.

## Latest native diagnosis and recovery design (2026-10-29)

The latest native trace establishes a concrete host-routing failure: `remote-compute-1.make_directory` targeted the granted Mac project path and correctly stopped for approval on the remote executor; the following `computer-use-1.write_file` then failed with `ENOENT` because the parent directory had never been created. The user’s project request remained incomplete. This trace identifies a bad executor choice and dependent-call ordering, not a reason to weaken approval or filesystem checks.

For a validated local-project grant, preflight now rejects the entire proposed batch if a remote-compute call targets a granted root, and returns the registered computer-use tools bound to the selected computer. Explicit host IDs or names that differ from the grant are rejected. No result or evidence is recorded for a rejected plan. A bounded planner repair can replan within the original objective; it cannot expand the grant or silently rewrite and dispatch the rejected calls.

Goal tool calls execute serially. Cancellation is checked before dispatch and between calls. The batch stops after any result that the shared progress tracker does not classify as completed success, including `None`, failure, uncertainty, incomplete background execution, and a pending approval. The observed result is retained for the normal progress recorder. An approval request is only an interlock: it is not evidence that the requested effect happened. After the owner approves, the same original objective and frozen requirements continue through ordinary dispatch; completion still requires the eventual observed effect and its required verification.

The no-tools completion review gets one bounded retry on unavailable or invalid output using the unchanged objective, requirements, and evidence. A missing planner response gets one bounded repair turn because no tool from that response ran; the repair continues the same objective without replaying completed effects. Exhaustion, cancellation, or an uncertain outcome uses the existing terminal incomplete/blocked path. Neither retry can authorize a tool, satisfy evidence, or override current project authority.

The original authority model and evidence contract remain unchanged: Keycloak owner identity, current project grant, selected host, existing approval, cancellation/lease and audit fences, normal tool dispatch, and egress controls remain authoritative. File contents and secrets are not added to proof records; privacy-safe file observations retain only bounded metadata needed to establish that a real scoped operation occurred. Tests, dependency installation, and startup still require completed successful commands on the selected host and project folder. A canvas, model statement, pending approval, or output from another computer is not proof.

The privacy regression repaired in [`4543763a`](https://github.com/AstralDeep/AstralDeep/commit/4543763a1c63e636a2dccd3a0131e37e8b6672c4) involved malformed reserved usage/reference groups containing a private warning. The rail fallback could retain that attached Alert when group validation failed. The repair identifies a reserved group from its metadata and rejects the whole malformed group, while preserving valid canonical groups and ordinary standalone Alert and List components. It does not classify arbitrary warning prose as reserved metadata.

## Harness comparison at reviewed source revisions

| Harness | Primary source and observed pattern | What this design takes from it |
| --- | --- | --- |
| Codex | [`session/turn.rs` at `468b71f7a64c0dcafe8d44f3cafd18f75932b8a3`](https://github.com/openai/codex/blob/468b71f7a64c0dcafe8d44f3cafd18f75932b8a3/codex-rs/core/src/session/turn.rs#L2464-L2487) and [`stream_events_utils.rs`](https://github.com/openai/codex/blob/468b71f7a64c0dcafe8d44f3cafd18f75932b8a3/codex-rs/core/src/stream_events_utils.rs#L1947). The [Codex agent-loop article](https://openai.com/index/unrolling-the-codex-agent-loop/) and [goals cookbook](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex) describe task/turn continuation. | Preserve request/task identity and tool-result correlation across turns; keep continuation bounded and responsive to user input. |
| Claude Code | [Official loop documentation](https://code.claude.com/docs/en/how-claude-code-works) and [Stop hook documentation](https://code.claude.com/docs/en/hooks#stop). Claude Code’s runtime implementation is closed source; these are documented behavior, not source-level evidence about internal retry or persistence mechanics. | Action and verification repeat until the task is done, with interruption still available. Do not infer or copy undocumented internals. |
| Pi | [`agent-session.ts` at `e7a0df27c09d11bf4161d3605246cbf57a129cb2`](https://github.com/earendil-works/pi/blob/e7a0df27c09d11bf4161d3605246cbf57a129cb2/packages/coding-agent/src/core/agent-session.ts#L1162) with its second session/control point at [line 3032](https://github.com/earendil-works/pi/blob/e7a0df27c09d11bf4161d3605246cbf57a129cb2/packages/coding-agent/src/core/agent-session.ts#L3032). | Preserve an explicit session loop and control decisions around tool work and continuation. |
| Hermes | [`turn_recovery_autorecover.py`](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/agent/turn_recovery_autorecover.py) and the [gateway session lifecycle guide](https://github.com/NousResearch/hermes-agent/blob/fcc26fbd9f1641755e49793d7ed608f3b7214c3d/website/docs/developer-guide/gateway-session-lifecycle.md#L355) at `fcc26fbd9f1641755e49793d7ed608f3b7214c3d`. | Use a bounded recovery path that resumes unfinished work without treating a plan marker as proof of an effect. |
| OpenClaw | [`agent-loop.md` at `90a5029e427822745c59e4f1268db08ade257d5b`](https://github.com/openclaw/openclaw/blob/90a5029e427822745c59e4f1268db08ade257d5b/docs/concepts/agent-loop.md#L176-L255). | Distinguish model-attempt outcomes from request settlement and keep terminal publication tied to the current execution owner. |

These sources inform the design; no implementation is copied and no parity claim is made. AstralDeep retains its own stricter project, host, approval, privacy, and evidence boundaries.
