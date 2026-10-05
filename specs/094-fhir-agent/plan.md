# Implementation Plan: FHIR Clinical Data agent

**Branch**: `fhir-agent` | **Updated**: 2026-10-05 | **Spec**: [spec.md](spec.md)

## Summary

A bundled first-party Python agent, `fhir-1`, reads one operator-configured FHIR R5 server and returns astralprims cards. It is registered in-process only when `FF_FHIR` is on, follows the same flag-gated pattern as the remote-compute agent, and adds no dependency, schema change, protocol change or primitive.

## Structure

| Path | Role |
|---|---|
| `backend/agents/fhir/client.py` | Settings from the environment; REST calls through `shared/external_http.py`; paging; error codes |
| `backend/agents/fhir/clinical.py` | Pure view models: readings, display thresholds, reference intervals, series, timeline events |
| `backend/agents/fhir/presentation.py` | One card builder per tool |
| `backend/agents/fhir/mcp_tools.py` | Eight request/response tools, two push-streaming tools, the registry |
| `backend/agents/fhir/mcp_server.py` | Dispatch, coded errors, first-chunk path for the streaming tool |
| `backend/agents/fhir/fhir_agent.py` | Agent class and a standalone entry point that refuses to start with the flag off |
| `backend/agents/fhir/tests/` | In-memory FHIR server and the suites for each module |

Touched outside the agent directory: the stream progress save in `orchestrator/orchestrator.py` and `orchestrator/stream_manager.py` with its `stream_progress` flag; the `fhir` flag in `shared/feature_flags.py`; the id and gated directory in `orchestrator/local_agents.py`; the subprocess gate in `start.py`; the safe-seed filter in `orchestrator/orchestrator.py`; the untrusted set in `orchestrator/taint.py`; seven public messages in `orchestrator/tool_feedback.py`; the seed expectations in `tests/test_remote_orchestrator_wiring_063.py`; `.env.example`, `README.md`, `.gitignore` and `docs/fhir-agent.md`.

## Decisions

- **Flag-gated, not always-on.** The agent needs a deployment-specific server, so it stays out of `BUILT_IN_AGENT_DIRS` and is appended only when the flag is on. That keeps the always-on inventory and its tests unchanged.
- **Safe-seeded while on.** Every tool is `tools:read` or `tools:search`. Seeding it as safe when the flag is on avoids asking users to enable nine read tools; with the flag off it is filtered out of the seed like the other gated agents.
- **One card per tool.** A single top-level component keeps the arrangement out of the layout designer, which would otherwise receive excerpts of clinical components, and lets a repeat call replace the card in place by id.
- **Per-call private-host allowance.** `external_http.request(..., allowed_private_hosts=[configured host])` admits exactly the configured server for this agent's calls. The global `EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS` is left alone so no other caller can reach it.
- **Environment configuration.** There is no operator-level secret store for agent service keys; the precedent for operator configuration is the environment. Per-user credentials do not apply because the feed is a deployment resource, not a user account.
- **Untrusted source.** A clinical feed can carry free text written by anyone upstream, so its output is treated like the other remote data sources for taint tracking.
- **Live feed as a push stream.** The feed uses the existing `@streaming_tool` contract and polls the server's `$events` operation through the same egress helper, so no WebSocket client or second egress path is introduced.
- **Saved stream progress.** The web client drops stream frames unless a turn is in flight and applies only committed canvas updates, so a stream that persisted only its final state looked frozen. Behind the default-off `FF_STREAM_PROGRESS` flag the orchestrator now saves a running stream's latest content with the same detached mutation it already used for the final state, for tools that opt in, throttled and skipped when nothing changed. No protocol frame, primitive or client changes.
- **Buttons instead of a model call.** Stream buttons send `stream_subscribe` and refresh buttons send `component_action`; both are existing client actions that the orchestrator authorizes like any tool call.
- **Display thresholds in code.** Alert colours and reference intervals are fixed constants in `clinical.py` and every card states that they are not clinical advice. They never alter data.

## Constitution check

- I Primary language: Python, compatible with 3.11. Pass.
- II UI delivery: existing astralprims 0.4.0 primitives only; no new primitive, protocol frame or client change. Pass.
- III Testing: module-local suite with an in-memory server; golden paths, empty states, denials and failures. Pass.
- V Dependencies: none added. Pass.
- VI Self-documenting code: each file has a header of at most three sentences and no other comments. Pass.
- VII Security: egress through the approved helper, fail-closed flag, untrusted classification, no bypass of dispatch. Pass.
- IX Data plane: no schema, migration or storage. Pass.
- XII Cross-client consistency: structured components only; clients render what they already support. Pass.
