# Windows handoff — native UI v2

Status: work-in-progress collaboration checkpoint, September 25, 2026. This is not a merge, release, passing-CI or native-parity claim.

## Branches and ownership

The owner requested publishing `codex/090-native-ui-v2` in both AstralDeep and AstralProjection so another agent can implement Windows. Deep pins the published Projection checkpoint through `config/astral-composition.json` and its gitlink. Fetch both origins and initialize/update submodules before comparing code. Create a separate Windows work branch from this checkpoint to avoid competing pushes to the Apple/Android branch.

The current Mac task continues Apple (iPhone, iPad, macOS and watchOS), then Android. `components/AstralProjection/windows-client/` has no changes in this checkpoint. The Windows agent owns that client; coordinate shared contract/ROTE/manifest changes before editing the same files. Do not treat unfinished native views as the visual reference.

## Ground truth

The complete 54-image archive is published in kos-wiki at `92e4ed0c8254df10f0fc196c0aa8a11fb432ecae`:

- [Screenshot index and reproduction steps](https://github.com/Kentucky-Open-Science/kos-wiki/blob/92e4ed0c8254df10f0fc196c0aa8a11fb432ecae/assets/astral-native-ui-v2/web-reference/README.md)
- [Viewport, source, image and SHA256 manifest](https://github.com/Kentucky-Open-Science/kos-wiki/blob/92e4ed0c8254df10f0fc196c0aa8a11fb432ecae/assets/astral-native-ui-v2/web-reference/manifest.json)
- [Progress and owner decisions](https://github.com/Kentucky-Open-Science/kos-wiki/blob/92e4ed0c8254df10f0fc196c0aa8a11fb432ecae/wiki/synthesis-astral-native-ui-v2.md)

Seven viewport sizes each cover landing, settings, agent introduction, chat selection, More, real successful dice result and full-screen result. Four drawer captures and the earlier genuine provider error supplement the matrix. The complete archive was pushed before resuming UI implementation after the owner's explicit checkpoint instruction. Earlier partial native edits were preserved and this ordering is documented in the vault.

The bottom passive voice warning in some web captures is explicitly web-only. Native voice must function; preserve actionable errors and accessible disabled reasons. Main and system LLM are now `zai-org/GLM-5.3-Flash` through product settings. Credentials must stay out of source, screenshots and notes.

## Shared integration points

- `backend/webrender/chrome/console_model.py` in Projection owns shared labels, sanitized catalog and composer placement. Deep `backend/orchestrator/native_console.py` reuses the authenticated web landing catalog and attaches it to `chrome_menu.model.console` only for qualified clients.
- Registration opts in with `console_contract: "console/v2"`. Absent/unknown values retain legacy output. **Windows is intentionally still excluded in `native_console.py` and ROTE's qualifying device set at this checkpoint.** Enable Windows only together with its actual decoder/renderers; do not merely advertise the contract.
- `backend/rote/console.py` emits `rote_config.device_profile.console` with version2, navigation mode, sidebar width, content/composer insets, scenario columns, settings presentation/axis/width/height/rail width, `dialog_width`, result preview/body limits, fullscreen inset and minimum control height. Device profile changes compare the complete aggregate so capability changes at unchanged width are applied.
- Shared fixtures are `contracts/fixtures/console/chrome-console.json` and `rote-console.json`. Swift `ConsoleModel`, `ConsolePresentation` and `TurnSelection` illustrate validation, bounds and request semantics, not a separate source of policy.
- The six existing primitive types are `action_group`, `stat_group`, `gauge`, `pipeline_stepper`, `donut_chart`, `radar_chart`. Negotiation plus exact supported-type advertisement controls native delivery; all other cases retain existing ROTE fallbacks. Implement and test renderers before enabling each type.
- Agent introduction uses ordinary `chat_message` for Run and an exact-current-surface `compose_prompt` action for Load. Load only changes the local draft. Guidance uses the server-owned selection view and `chrome_turn_selection_set`; only its authenticated confirmation may carry `chrome_surface.selection`. Include a nonempty selection on ordinary chat dispatch, and clear it on account/chat reset.
- Preserve one live canvas/component identity across preview/fullscreen/dashboard transitions. Export/share use current owner/chat/render-revision authority. New Apple visual composites export measured rendered images through the existing canvas-export/v1 contract; interactive controls remain excluded.
- Watch preparation adds ROTE `stack`/`push`, zero sidebar, one column, compact padding, and optional per-item `availability` (`native` or `handoff` with a message). Watch UI is not implemented yet and its factory currently does not advertise console/v2. Generic watch `param_picker` remains a fallback; strict notes forms use the existing scoped guidance capability.

## Outstanding work and checks

Shared manifest inventory for additive console metadata, final fixtures/drift checks, all affected client dispositions and final composition/runtime rebuild remain required. No new primitive definition, Plane schema migration or Primitives publication is included.

The current partial macOS app build succeeds. Earlier 52 real-browser tests and targeted shared/Core/renderer/export suites passed; see `verification.md` for exact commands and remaining reruns. Full backend CI, final native coverage, iOS/watchOS qualification, signed-in visual/voice checks and Android work remain open. The local Xcode27 toolchain differs from the hosted pin. Do not infer passing CI from this handoff push.

Windows qualification should follow its existing CI/offscreen tests and real signed-in desktop walkthrough against the source-bound backend, including resizing, selection/owner denials, result continuity, export/share, microphone/worker playback and recovery. Record progress in kos-wiki before reporting checkpoints complete.


## Later local implementation checkpoint — September 25

The initial published handoff above remains Deep `4e2eae707e108ee868212cec653ae107fdb15518` / Projection `1d4a8642a888279dd42a67181412901960b50217`. Its descriptions of unfinished Apple/watch/Android work describe that initial publication only. Current reviewed local commits are Deep `d2a61145a1b75fef3cc2dd7280d17d2392f6b941` / Projection `ce0c1588546c485e8253197def0ac0eadbc97fd0`; they have not been pushed and are not yet available through origin.

The local implementation now includes Apple/watch and Android console/v2 negotiation and renderers, current shared dispositions/provenance/export assets, native voice transcript/result hydration, the Mac composer correction, and bounded voice guidance contention recovery. Watch uses shared ROTE stack/push presentation, server-owned availability and labeled handoff when a surface exceeds its supported capabilities; its ten navigation scenarios pass. Windows remains excluded from this task's edits and must retain its separately owned negotiation work.

Clean Projection tests pass 3,235; browser/responsive suites pass 334/105; strict web changed coverage is 99.38%. Local Mac294, iOS277, iOSUI31, watch104 plus one staging-only skip, watchUI10, Core283 and Android core161/app392/device116 pass. The complete Plane suite passes 4,087 with nine Windows-only skips and 91.23% coverage. Full backend CI, canonical pinned-toolchain evidence and owner-dependent native acceptance remain open. See `verification.md` and the updated kos-wiki page for exact result identities and superseded diagnostics. Do not substitute local or synthetic results for hosted/staging/native physical acceptance.

The shared contract's negotiation boundary remains deliberate: this task does not enable unimplemented Windows support or edit Windows source. Coordinate final composition/manifest integration after both owned branches are qualified. The vault retains the original 54 web references plus separately identified current Android and watch captures; none of the newer fixture images replaces the web ground truth.


## Current shared resize integration — September 25

The owner has authorized publishing current090 development work for the Windows owner. The prior published baseline is Deep5065aa04 / Projection81fddb7; the next coordinated revision implements the following additive contract in `presentation_contracts.console_v2.viewport_refresh` of Projection's `contracts/ui_protocol.json`. Obtain exact follow-up commits from the latest kos-wiki checkpoint. Earlier sections above are dated historical handoffs, not current implementation status.

- Negotiate `rote_config.viewport_snapshot_supported == true`. Send a live-only `ui_event` action `update_device`, with fresh UUID4 `submission_id` and `request_generation`, current `connection_generation`, and payload `device`, active `chat_id`, `base_render_revision`, `snapshot_purpose: hydration`. Preserve the existing duplicated identity envelope conventions.
- Open a fresh hydration generation only from the settled current conversation. Coalesce size changes; defer while commits, ordinary hydration, direct editing, voice, uploads, exports, settings/guidance/work, timeline or other interactions are active. Never queue this request across reconnect, invoke `load_chat` automatically, or loosen post-snapshot transient rejection.
- Hold the scoped `rote_config` (`chat_id`, `connection_generation`, `request_generation`) until the matching canonical `conversation_snapshot` validates. The snapshot must be hydration at exactly the declared base semantic revision. A newer canonical revision is a retryable refusal; ordinary semantic delivery owns that change.
- Errors use `viewport_snapshot_rejected` or `viewport_snapshot_retryable` with the same three scope fields and `retryable`. Ordinary admission refusal and `operation_status` also apply. Consume only this exact request's own status without treating it as competing work. Retire timers and request state immediately on authentication loss, disconnect, owner change or navigation.
- Preserve the committed result, composer, attachments, selection, scroll and fullscreen/collapse while waiting or on refusal. Provide an explicit retry after timeout/failure. Canonical ROTE cache changes only after successful current-owner delivery; failed adaptation or send retains the earlier cache/scope.

The shared server's209 focused cases pass, including actual isolated PostgreSQL admission and execution fences. New helper coverage is64/64 lines and18/18 branches. Apple and Android include matching consumers and regressions; source-specific native checks, refreshed coverage and final clean full CI are recorded separately. Windows source remains owned by the other machine; its1,586-pass/10-skip baseline and98.39% coverage do not include this consumer integration.
