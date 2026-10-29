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

## Model transport and terminal failures

Goal planning retains permitted provider streaming while buffering the proposed narrative until completion verification. Buffering does not change provider eligibility, evidence capture, selected harness or persistent-worker restrictions. A buffered stream must carry a recognized terminal finish reason before assembled tool calls are returned; clean EOF without that evidence is malformed and cannot dispatch partial arguments. Truncation retains its explicit finish reason so the ordinary goal gate can reject it. Exhausted model calls publish the original user request and a durable incomplete outcome atomically rather than returning before conversation publication; cancellation retains its own terminal publisher.

## Scope and rollout

Deep owns the request controller, evidence, project context, and evaluator integration. Existing tool, provider-disclosure, audit, project, confirmation, and Stop paths remain authoritative. No SQL migration, new primitive, or client protocol is required. A default-off `FF_GOAL_HARNESS` switch supports qualification and rollback; the sandbox will enable it for every ordinary request. A bounded configurable goal round budget replaces the small legacy budget only when enabled. No failed or incomplete request is marked completed.

This work uses independent cheaper implementation and verification agents, coordinated and reviewed by the root agent as the owner requests. Runtime model selection continues to use the owner's configured providers and normal routing.

## Primary research, retrieved 2026-10-28

- Codex: https://openai.com/index/unrolling-the-codex-agent-loop/ and https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex — tool observations and bounded, idle-boundary goal continuation with user input precedence.
- Claude Code: https://code.claude.com/docs/en/how-claude-code-works and https://code.claude.com/docs/en/hooks#stop — repeated action/verification and completion rejection without bypassing interruption.
- Pi: https://github.com/earendil-works/pi/blob/e65b3b88c08412bb8ffbd21fb5dc5756064a6e5a/packages/agent/src/agent-loop.ts — inner tool loop, finish-turn decision, steering precedence, and truncation handling.
- Hermes: https://github.com/NousResearch/hermes-agent/blob/cf6623e43359ed87b145dd7f4db9596747535ef0/agent/conversation_loop.py and tools/todo_tool.py at the same revision — bounded recovery and retained unfinished work across compaction.
- OpenClaw: https://github.com/openclaw/openclaw/blob/7e75d4900ebea39e593b12680875b38fdc6e84de/docs/concepts/agent-loop.md — distinct provider-response and request-terminal outcomes.

These sources inform the design; no copied implementation or blanket parity claim is made.
