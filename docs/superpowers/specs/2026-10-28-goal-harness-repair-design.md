# Goal-directed request completion

The owner requests implementation, sandbox verification, merge, and a new Mac release. The existing Mac workbench specification already requires an observation–action–verification loop; this repair restores that behavior without changing its security or client contracts.

## Reproduced failure

The exact Minecraft prompt produced a canvas code artifact and static code review. The final answer said Done while telling the owner to install pyglet and run tests. The selected local project folder remained empty. This is partial delivery, not a verified runnable project.

## Behavior

Every request retains its original objective outside mutable and compacted conversation context. When enabled, a separate no-tools completion evaluation compares the proposed answer with the objective, validated project context, and recorded tool outcomes. Informational answers can complete without tools. Action requests continue through ordinary authorized dispatch until the requested effects and verification are evidenced, or report a concrete blocked/incomplete outcome.

For a runnable project in a selected folder, code generation alone does not satisfy the request. The planner uses the granted local folder, creates an appropriate isolated environment (a local `.venv` for Python), declares and installs dependencies, runs meaningful tests, and performs a startup smoke check. No product behavior is specific to Minecraft or Python.

The completion evaluator returns strict JSON with `status` (`complete`, `continue`, `blocked`), `remaining`, and `reason`. Invalid evaluation cannot authorize completion. Deterministic code also prevents completion while command jobs are pending or effects uncertain, bounds repeated failures/prose-only continuation, and distinguishes budget exhaustion from success. Continuation stays inside the originating request and its existing cancellation fence; it does not replay a request or confer new authority.

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
