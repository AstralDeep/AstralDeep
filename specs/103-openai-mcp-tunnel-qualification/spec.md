# Feature Specification: OpenAI MCP Tunnel Qualification Against Astral Authentication and Discovery

**Feature Directory**: `specs/103-openai-mcp-tunnel-qualification`

**Created**: 2026-10-06

**Status**: Deterministic fixtures and operator guide implemented; real tunnel compatibility unqualified (operator-run, optional)

**Input**: AstralDeep issue #323 — "Qualify the OpenAI MCP tunnel against Astral authentication and discovery" (reviewed baseline `f89ba2ae9c0e8decd271544e05a93f6c8a202a66`, 2026-10-06; pinned tunnel-client commit `fda241e2343ddd49dd63a7da3e1afab2a6e91722`).

## Overview

The OpenAI secure-MCP-tunnel client could make Astral's private, flag-gated MCP endpoint reachable from OpenAI products over an outbound connection. The issue's source review hypothesizes that the client's startup sequence — a modern discovery probe answered by `401`, then a legacy `initialize` fallback — "may leave readiness at 503 against Astral's strict modern contract". This feature qualifies that interaction honestly and ships a pinned synthetic smoke test plus an operator qualification guide. It is distinct from the panel/resource work in 064 follow-ups and from #219.

## Scope decisions

| # | Question | Decision |
|---|---|---|
| D1 | Does the endpoint's strict modern contract change? | **No.** `MCP 2026-07-28`-only validation (`params._meta`, `Mcp-Method`/`MCP-Protocol-Version` mirror headers, RFC 9728 protected-resource metadata, header-bearer-only `astral-mcp` audience tokens) is retained exactly as ratified in 064. Any fix that had been required was forbidden from bypassing IAM or downgrading the protocol. |
| D2 | Who owns the inferred readiness defect? | **The reviewed upstream fallback is the suspected seam; actual binary readiness remains unverified.** Deterministic fixtures establish Astral's responses to the selected modern and legacy probes. The source-reviewed SDK falls back to legacy `initialize` after a non-modern discovery error; that dialect cannot satisfy Astral's contract. This remains a private conditional compatibility finding, with no Astral bypass or upstream PR. |
| D3 | What is pinned, and how? | `contracts/tunnel-client-pins.json` pins tunnel-client at `fda241e2343ddd49dd63a7da3e1afab2a6e91722` and its transitive `github.com/modelcontextprotocol/go-sdk v1.7.0`, including the archive and `go.mod` h1 checksums recorded upstream. Raw upstream `go.mod`/`go.sum` were retrieved and SHA-256 hashed on 2026-10-28. The original environment's egress restrictions are preserved as historical evidence. No module archive, binary, or live tunnel was qualified. |
| D4 | Does the MCP flag change state? | **No.** `FF_MCP_SERVER` remains default-off; this feature ships no flag, schema, security-gate, or release change. The synthetic suite asserts the default-off posture and installs the router directly, as the existing 064 endpoint suites do. |
| D5 | Is any OpenAI-host connection part of CI? | **No.** CI runs only deterministic offline fixtures. Any live connection to OpenAI hosts is a bounded, opt-in, non-gating operator qualification documented in `docs/mcp-openai-tunnel-qualification.md`. |
| D6 | What does "reproduce" mean here, honestly? | The deterministic suite sends a **hand-coded selection of requests** to the real endpoint (200 metadata, 401 unauthenticated modern discovery, 400 legacy refusal with and without fixture credentials; no 5xx). The separate credentialed legacy probe tests refusal, not an observed binary retry. The profile models NOT-READY from those results; actual tunnel readiness/503 requires operator observation of the pinned binary. |

## Honest compatibility decision

**PARTIAL — conditional compatibility; suspected upstream fallback gap, no Astral defect reproduced.**

- Modern discovery succeeds in the endpoint fixture when the host reads `/.well-known/oauth-protected-resource/mcp` unauthenticated (200), uses a fixture bearer with the `astral-mcp` audience and `mcp:*` scopes, and speaks `server/discover` with `params._meta` plus the `Mcp-Method`/`MCP-Protocol-Version` mirror headers (200). This does not prove a real host or Keycloak browser flow succeeds.
- The reviewed fallback path cannot succeed: a legacy `initialize` request is refused `400` ("params._meta is required" / unsupported version) before authorization and before any dispatch, with or without credentials. Legacy `initialize` is not in the endpoint's method table at all (404 after authorization when wrapped in the modern envelope).
- The tested Astral probe path emits no 5xx. The hypothesized readiness `503` concerns the tunnel client's health endpoint and has not been observed. It must be distinguished from Astral admission-capacity `503` with `Retry-After`; no general availability or compatibility guarantee follows from these fixtures.
- The unauthenticated legacy probe receives `400` rather than `401` because strict request validation precedes authorization by design (064); the modern discovery probe — the path a compliant client uses first — receives the correct `401` + `WWW-Authenticate: Bearer resource_metadata="…", scope="…", error="invalid_token"` challenge.

## Evidence classes (kept distinct)

1. **Source review** — issue-recorded review of the pinned tunnel client and Astral endpoint at `f89ba2ae`; independently retrieved pinned dependency/probe sources and official living guide on 2026-10-28. Immutable links, source hashes, and retrieval limits are recorded in the manifest and operator guide.
2. **Deterministic tests** — `backend/tests/test_mcp_openai_tunnel_qualification_103.py`, offline, driving real routes, `mcp_authz`, projection, and admission with local token/tool/in-memory admission fixtures and a simulated disconnect. Rejected-token fixtures do not exercise actual Keycloak expiration/revocation. The transcript retains sanitized paths, header names, and statuses.
3. **Live qualification** — **not performed**. The source is now reachable, but no binary build, TLS exchange, live OAuth, ChatGPT connection, or deployment was qualified. The bounded, opt-in, non-gating procedure is in `docs/mcp-openai-tunnel-qualification.md`.
4. **Deployment** — **none**. No production activation, tunnel publication, or flag flip occurs in this issue.

## Acceptance mapping

| Issue acceptance check | Where satisfied |
|---|---|
| Pin tunnel-client and its reviewed Go MCP SDK dependency; keep the flag off | `contracts/tunnel-client-pins.json` + `test_tunnel_client_pins_manifest_matches_reviewed_sources` + existing default-off guard asserted in-suite |
| Unauthenticated discovery, modeled ready/auth-required behavior, modern metadata/headers, owner-token success, cross-owner/rejected-token denial | `test_reviewed_probe_sequence_*`, `test_valid_owner_token_completes_modern_discovery`, `test_cross_owner_tool_projection_is_isolated`, `test_expired_and_revoked_tokens_are_denied_with_challenge`; real issuer/revocation and binary readiness remain operator checks |
| Reproduce or reject the startup fallback issue with retained sanitized requests/statuses | `test_reviewed_probe_sequence_leaves_legacy_fallback_not_ready` (pinned sanitized transcript, honest NOT-READY verdict, no 5xx) |
| Disconnect/reconnect, cancel, bounded payload/error handling, no effect replay or credential leak | Simulated cancellation/recovery in `test_disconnect_cancels_without_replay_and_reconnect_recovers`, `test_bounded_payload_and_error_refusals`, transcript credential non-leak assertions; transport recovery remains a live operator check |
| Document OpenAI traversal, strict-local-auth, Keycloak browser authorization, temporary-tunnel limits | `docs/mcp-openai-tunnel-qualification.md` |
| Deterministic local fixtures in CI; OpenAI-host connection bounded, opt-in, non-gating | suite uses local fixtures only; live steps are documented as operator-run and non-gating |
| ≥90% changed-line coverage, all owning gates, ≤30-minute jobs | Changed product lines: none (tests/docs/spec only). Offline smoke commands are in the guide; current-candidate checks and repository-wide CI must be reported separately. |
| Keycloak-only IAM, owner isolation, RFC 8693 attenuation, tool/PHI/egress/confirmation gates, LETS, hash-chained audit preserved | no product change; denials exercised through the real `mcp_authz`/projection/admission path |

## Risks and non-goals

- The synthetic profile is derived from the pinned source review, not from executing the binary; the manifest and guide say so explicitly.
- No MCP Apps, Tasks, prompts, or resource-surface changes; no CORS, schema, or default changes; no upstream PR in this bounty.
