# Research and Decisions

Primary sources inspected2026-10-24; source inspection is not live-product qualification.

| Harness | Pin | Selected pattern |
|---|---|---|
| SPACE | [725115a](https://github.com/perplexityai/SPACE/tree/725115aded7f95252f5f9898c993f31ff65a0af5) | Linux sandbox service, not Perplexity browser source: separate operation/process/attachment lifetime, explicit exit and uncertain outcomes. Apache2.0. |
| Claude Code | [318b7e6](https://github.com/anthropics/claude-code/tree/318b7e6ac4e726e88a4ad326214866a93c373b1b) | Public docs/plugins, not core desktop source: independently implement documented narrow-actuator/browser/computer/review UX. Root all-rights-reserved/CommercialTerms. |
| Codex | [970111f](https://github.com/openai/codex/tree/970111f6cb62cc02fe36a82ea7d71b37a2889c97) | Actual Rust core/app-server: structured task/tool/approval/output events, task authority, browser origins and bundle policies. Apache2.0+NOTICE. |
| OpenCode | [097a93e](https://github.com/anomalyco/opencode/tree/097a93e511e2cd7d4fbfc12e0623b721b02f23b9) | Desktop/client/server UI, session navigation, typed extensions, queued-prompt recovery. MIT. |
| Pi | [38ff991](https://github.com/earendil-works/pi/tree/38ff99177fba21e9c57a670f4e95adcf407c99c7), canonical source linked by [pi.dev](https://pi.dev/) | Steer versus follow-up, branch-preserving context/history, extension UI. MIT; process permission is not isolation. |
| Hermes | [eb1b4c5](https://github.com/NousResearch/hermes-agent/tree/eb1b4c5b617dc318edb39683f408666c692d3bdf) | Bounded inspectable memory/skills, browser recovery, exact target and semantic effect evidence. MIT. |

References: [Claude Chrome](https://code.claude.com/docs/en/chrome), [Claude Desktop](https://code.claude.com/docs/en/desktop), [Codex policy](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/config/src/browser_computer_use_requirements.rs), [OpenCode queue](https://github.com/anomalyco/opencode/blob/097a93e511e2cd7d4fbfc12e0623b721b02f23b9/packages/app/src/session/composer/queue.ts), [Pi sessions](https://github.com/earendil-works/pi/blob/38ff99177fba21e9c57a670f4e95adcf407c99c7/packages/coding-agent/docs/sessions.md), [Hermes browser](https://github.com/NousResearch/hermes-agent/blob/eb1b4c5b617dc318edb39683f408666c692d3bdf/website/docs/developer-guide/browser-supervisor.md).

## Decisions

Use public native APIs for baseline; preserve reviewed Terminal rather than smuggling direct commands. Keep narrow existing connectors/isolated execution available without automatic authority escalation. Follow stale-target and postcondition discipline, while clearly recording that v1 pixels are not semantic DOM tokens. Redesign native arrangement around conversation/generated work and optional inspectable activity, preserving server menus/theme/capabilities. Retain exact approval instead of importing permissive extension assumptions.

## External Driver Assessment

Hermes pins MIT CuaDriver0.21.0 source [70db98d](https://github.com/trycua/cua/tree/70db98d1bcd92890d778f4978e0eb107a4b66c1b), universal archive SHA256 `f827a393c0dae24943ddb454d85abb6b592d8636e6a99f6c349f81f5f516975f`. It has embedded-host attribution, AX and semantic browser tools, but private Mac APIs, default telemetry/update checks and no verified MCP per-call cancellation. Portable checked-in manifest is not the full Mac schema. Credible future backend; not installed/exposed in baseline. Adoption needs artifact/schema qualification, trusted bounded manifest, telemetry/updates disabled and unchanged Astral authorization. [Embedding](https://github.com/trycua/cua/blob/70db98d1bcd92890d778f4978e0eb107a4b66c1b/libs/cua-driver/rust/Skills/cua-driver/EMBEDDING.md).

## Baseline

Deep3627625e/Projectioncd37c1c5 have no Mac executor. Installed1.7/build66 is sandboxed, source1.8/build67. Production read-only inspection found healthy `sha-ca822e68d5477fb60fa9076107a4dab4138eeda6`, Compose `/opt/AstralDeep`. No credentials are recorded. Current ConsoleSidebar favors agent cards over chats, generated result interaction is fullscreen-gated, and composer suggestions are hardcoded; these are concrete redesign seams, not evidence that new UI is implemented.
