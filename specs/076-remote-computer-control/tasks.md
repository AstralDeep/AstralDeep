# Tasks: Remote Computer Control (076)

Legend: `[ ]` open · `[x]` done · `[~]` code-shaped, not live-verified. Paths are repo-relative; `P:` = AstralProjection.

## Phase 1 — Protocol
- [ ] T001 P: `contracts/ui_protocol.json` +3 push types, +7 accept actions, +1 additive field (sorted)
- [ ] T002 P: `tests/test_protocol.py` and any count expectations; Windows `protocol_manifest.py`; Android `ProtocolManifest.kt` + `ProtocolManifestTest.kt`; Apple `Dispositions.swift` + `ManifestDriftTests.swift` counts (by inspection)
- [ ] T003 Deep: `shared/protocol.py` `RegisterUI.computer_host` + `ComputerHostDescriptor` validation (exact fields, bounds)
- [ ] T004 Deep: `tests/test_ui_protocol_manifest.py` green with the new frames (SWEEP + accept sweep)

## Phase 2 — Backend core
- [ ] T010 `shared/feature_flags.py` `computer_use` (default off)
- [ ] T011 `orchestrator/computer_use_policy.py` (classification, tiers, unattended set, card summaries)
- [ ] T012 `orchestrator/computer_hosts.py` registry + request/response correlation + caps + presence pushes
- [ ] T013 `orchestrator/computer_sessions.py` lifecycle + timers + heartbeat watchdog + audit + pushes
- [ ] T014 `orchestrator.py`: register_ui hook, `computer_event`/`computer_response` dispatch, disconnect teardown, `chrome_computer_*` routing via projection_surfaces
- [ ] T015 `agents/computer_use/*` agent `computer-use-1` (verbs per contract; typed results; `_images`/`_ui_components` for screenshot with stable component id)
- [ ] T016 `local_agents.py` flag-gated dir + public id + dependency injection
- [ ] T017 `remote_confirmation.py` per-agent policy table; `orchestrator.py` gate hook covers both agents; `unattended` rule for computer-use-1
- [ ] T018 multimodal: `_images` → image parts after the tool message; prune to last 3; provider-rejection text-only retry + session flag; spotlighting caption
- [ ] T019 `taint.py` untrusted source
- [ ] T020 `projection_surfaces/my_computers.py` + registry + menu flag plumbing
- [ ] T021 tests: verbs contract; sessions; unattended/gate; flag-off byte-identity; transport correlation + caps; surface render/components; multimodal assembly/fallback
- [ ] T022 `docs/remote-computer-control.md`; CLAUDE.md Recent Changes

## Phase 3 — Windows host (P:)
- [ ] T030 `win_agent/computer_use.py` executor (capture, input, keys, windows, clipboard, files, run_command, coordinate mapping)
- [ ] T031 `astral_client/remote_control.py` consent (QSettings), host descriptor, session state machine, banner, presence detector, heartbeat
- [ ] T032 `astral_client/protocol.py` register_ui `computer_host`; response/event senders
- [ ] T033 `astral_client/app.py` settings entry + frame dispatch + wiring
- [ ] T034 tests (offscreen): state machine, key table, mapping, JPEG encode, refusal paths, manifest classification
- [ ] T035 live smoke on ryzenroll from source

## Phase 4 — Android (P:)
- [ ] T040 `Media.kt` data: URI images
- [ ] T041 `AppViewModel.kt` surface refresh on presence/session pushes; manifest tables + tests
- [ ] T042 emulator smoke (sign-in, My computers, session, live view, approval card)

## Phase 5 — Rig + delivery
- [ ] T050 Docker image rebuilt from the Deep branch with Projection pinned to the branch commit; `.env` `FF_COMPUTER_USE=1`
- [ ] T051 Demo task end to end (Android emulator → orchestrator → Windows client): evidence captured
- [ ] T052 `config/astral-composition.json` repin + verifier
- [ ] T053 PRs: AstralProjection then AstralDeep (Deep PR references the Projection commit)

## Phase 6 — Wiki
- [ ] T060 kos-wiki `astral-remote-computer-control.md` + `index.md` + `log.md` (design, decisions D1–D7, evidence, open items) + `astral-open-follow-ups` entry for the Apple follow-up and durable inventory
