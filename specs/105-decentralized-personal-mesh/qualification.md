# Spec 105 qualification map

Date: 2026-11-12. This maps every exit criterion in plan.md to its evidence. It was written by reading the tests and the recorded results. No suite was rerun for this pass. Nothing here is protected CI, deployment or release evidence.

Revisions read:

- Kernel: `k/integration` 34ef4a5 (includes `k/node-mint` and `k/attach`). Paths are under `crates/`.
- Contracts and Python reference: `contracts-mint` 14672d7a (includes contracts-attach). Deep: `feat/105-integration`.
- Apple app: Projection `feat/105-mesh-final` 503988a.
- LETS `feat/105-personal-mesh` 1321306. Plane `feat/105-plane-sqlite`. Vessel `feat/105-vessel-final`.

Statuses: Proven by automated test, Partly covered, Not covered, Live only. "Live" marks criteria the plan tags (live) or that only a device can show. Test names are `file::test`.

Owner live results so far (2026-11-12): Mac localtest app, iPhone simulator and CLI peer paired. Chats synced both ways. Real model turns on both. iPhone turn with the Mac closed, Mac caught up after relaunch. A granted tool turn on the Mac. Not yet done: voice on device, airplane mode, node on the owner's network, push wake, institution.

## Phase R

| # | Criterion | Status | Evidence |
|---|---|---|---|
| R1 | No first token surfaces within the deadline | Proven by automated test | Deep `tests/test_reliability_exit_criteria.py::test_a_provider_that_never_sends_a_first_token_surfaces_within_the_deadline` (three attempts, returns inside 5 s with tiny deadlines). Defaults in `test_llm_deadlines.py::test_remote_and_local_defaults`. |
| R2 | Mid-answer stall ends within idle deadline, keeps partial | Proven by automated test | Same file `::test_a_stream_that_stalls_mid_answer_keeps_the_partial_text`. Also `test_llm_stream_watchdog.py`. |
| R3 | Each malformed-argument case costs at most one extra model call | Proven by automated test | Same file `::test_each_malformed_argument_case_costs_at_most_one_extra_model_call` (four parametrized cases, asserts call count). Kernel twin: `astral-engine/src/args.rs` and `engine_tests::unrepairable_arguments_get_one_regeneration_under_the_strict_schema`. |
| R4 | No identical failing call dispatched more than twice | Proven by automated test | Same file `::test_no_identical_failing_call_is_dispatched_more_than_twice`. Also `test_repeat_guard.py`, `test_repeat_guard_loop.py`. |
| R5 | Primary outage completes on fallback with visible notice | Proven by automated test | Same file `::test_a_primary_model_outage_completes_on_the_fallback_with_a_notice`. |

## Phase 0

| # | Criterion | Status | Evidence and gap |
|---|---|---|---|
| 0.1 | Every contract has Python-reference vectors verified in a second language | Proven by automated test | 22 vector files under `contracts/vectors`, 1177+ reference tests. Rust checks them in `astral-core/src/vector_tests.rs`, `astral-mesh/src/vector_tests.rs`, `astral-authz/tests/authz_vectors.rs`, `astral-log/tests/vectors_*.rs`, `astral-ui/tests/surface_vectors.rs`, `astral-mailbox/tests/vectors.rs`, `astral-engine/tests/vectors_{learning,runs}.rs`. Note: the gate order has no vector file of its own (see 3.11). |
| 0.2 | Spike builds for macOS, iOS, Android, Windows, Linux inside the CI limit and each binding round-trips an event | Partly covered | Apple targets build (clean 467 s) and the Swift wrapper tests pass. A Linux arm64 wheel builds (122 s) and `python/smoke/runtime_smoke.py` passes. Gap: no Android binding, no Windows binding, no x86_64 Linux wheel, and the kernel repo has no CI workflow that measures time. Belongs in the kernel repo (build scripts plus a CI job). |
| 0.3 | LETS vectors verify in Python reference and the spike | Proven by automated test | LETS `protocol/vectors` run by the Python reference. Rust: `astral-authz/tests/lets_wire_vectors.rs` (receipts, leases, vouchers, acks) and `lets_period_vectors.rs::{period_index_vectors,derivation_vectors_are_byte_identical_to_the_python,device_scenarios}`. |
| 0.4 | Owner signed off the rule changes | Not covered | Owner decision. The owner must read the contract rule changes and AGENTS.md amendment and say yes in chat. |

Phase 0 checks against real systems: the Keycloak sign-in with the kernel nonce worked live on the Mac. A silent refresh carrying the nonce is unchecked. The Kotlin transport on Android is unchecked.

## Phase 1

| # | Criterion | Status | Evidence and gap |
|---|---|---|---|
| 1.1 | With sandbox blocked, Mac and iPhone paired to a node on the owner's network chat with a tool call | Partly covered (live) | Automated: Deep `test_node_mesh_kernel_integration.py::test_the_node_claims_runs_and_answers_a_request_from_another_member`, `test_node_mesh_calls.py`, `evidence/sqlite-node` (node on SQLite ran a tool turn in containers). Kernel mint path: `astral-kernel/tests/node_mint.rs`, `answer_call.rs`. Live so far used the Mac app and a CLI peer, not a node. Gap: no run with a real node, both devices paired and sandbox blocked. |
| 1.2 | With Keycloak blocked after sign-in, session works and tools dispatch | Proven by automated test | Deep `test_node_keycloak_blocked.py::test_session_keeps_working_and_tools_dispatch_with_the_issuer_blocked` (asserts no Keycloak exchange, node token verifies). Grace limit in `::test_verification_stops_when_the_grace_runs_out`. |
| 1.3 | Token for one node refused by another | Proven by automated test | `test_node_identity.py::test_a_token_from_one_node_is_refused_by_another`, `test_node_delegation.py::test_a_token_minted_for_this_node_is_refused_by_another`. |
| 1.4 | Second issuer's token with first issuer's subject refused | Proven by automated test | `test_token_verify.py::test_a_personal_node_bound_to_one_issuer_refuses_the_others_subject`, `::test_same_subject_from_two_issuers_is_two_owners`. |
| 1.5 | Raising the stop counter halts new effects on the node | Proven by automated test | `test_node_delegation.py::test_raising_the_stop_counter_halts_new_delegation` and `::test_stop_counter_is_durable_and_only_rises`. Kernel: `turns_compose.rs::a_stop_raised_after_the_turn_started_refuses_the_effect`. |

## Phase 2

| # | Criterion | Status | Evidence and gap |
|---|---|---|---|
| 2.1 | Chat started on one device appears on the other, both ways, internet off | Proven by automated test | `astral-kernel/tests/mesh_flow.rs::two_paired_devices_converge_on_identical_chats` (loopback, asserts identical ids and `path == "direct"`). Discovery: `astral-net/tests/local_discovery.rs`. Live: synced both ways with the owner present. |
| 2.2 | Airplane-mode phone shows history and accepts input, delivered on reconnect | Proven by automated test | `mesh_flow.rs::an_offline_message_is_queued_then_delivered` (Queued status, Waiting sync, delivery). App: `MeshRuntimeTests::testMessageStaysQueuedWhenTheTurnCannotRun`. Real airplane mode not yet done (see live list). |
| 2.3 | Two branches when two devices continue apart | Proven by automated test | `astral-log/tests/views.rs::two_devices_continuing_one_turn_apart_show_two_branches_not_an_interleaving`, `session.rs::three_peers_diverge_while_partitioned_and_converge_when_they_reconnect`. |
| 2.4 | Removed device cannot decrypt later events and its events are refused | Proven by automated test | `astral-mesh/src/scenario_tests.rs::removing_a_device_starts_a_new_epoch_its_keys_cannot_open` (removed device lacks the new epoch key), `mesh_flow.rs::three_devices_and_a_removed_member_whose_later_events_are_refused`, `session.rs::a_removed_devices_events_are_fetched_only_up_to_its_cutoff`. |
| 2.5 | Altered event, gap, or feed shorter than a countersigned checkpoint is detected | Proven by automated test | `log_flow.rs::{bad_envelopes_are_rejected_with_the_contract_codes,missing_after_references_are_listed_until_their_events_arrive,checkpoints_detect_contradictions_and_short_feeds}`, `session.rs::a_peer_claiming_a_feed_shorter_than_a_checkpoint_is_recorded_as_evidence`. |
| 2.6 | Restored device starts a new feed | Proven by automated test | `mesh_flow.rs::a_restored_install_starts_a_new_feed_and_re_enrolls`, `log_flow.rs::a_restored_device_starts_a_new_feed_and_never_writes_the_old_one`, `restore_marker.rs`. |
| 2.7 | Same generated surface renders correctly on Mac and iPhone from one stored event | Partly covered | Kernel: `surface_render.rs::one_stored_surface_renders_for_a_watch_a_phone_and_a_desktop` (asserts value, spoken, compact, capability on three profiles). App: `MeshFinalTests::testRenderedSurfaceUsesResolvedImagesAndNeverRawLinks`. Gap: no test or live check that the Mac and iPhone native renderers draw the same stored surface. Belongs in an app UI test with one fixture surface on both targets, then a live look. |
| 2.8 | PHI body not sent without approval, header only | Proven by automated test | `session.rs::phi_bodies_stay_on_approved_devices_while_headers_reach_everyone`, `::a_phi_claim_is_checked_against_the_roster_before_bodies_are_sent`. Product gap: no way yet to create a PHI chat in the app. |

## Phase 3

| # | Criterion | Status | Evidence and gap |
|---|---|---|---|
| 3.1 | (live) iPhone in airplane mode completes a chat on the on-device model with a notice | Live only | Supporting: `engine/provider/client_tests.rs::the_on_device_model_is_last_and_comes_with_a_notice`, app `OnDeviceModelAdapterTests`. Known live bug: on-device answer shows a red provider error. Known limit: on-device models cannot call tools. |
| 3.2 | iPhone with internet, no peer, completes a turn calling a remote MCP tool | Partly covered | Engine only: `astral-engine/src/mcp/tests.rs` (listing, approval, tasks, pins). Gap: no kernel or FFI test of a turn calling a remote MCP server, and no app way to add one. Belongs in `astral-kernel/tests/turns_compose.rs` with a local MCP fixture, then an FFI export. |
| 3.3 | Spent share refuses the next effect with a clear reason, a transfer restores it | Proven by automated test | LETS vector scenario `transfer-restores-headroom` run by `lets_period_vectors.rs::device_scenarios`, `gate_stack.rs::the_holder_meter_probes_solo_then_fresh_charges_idempotently_and_exhausts` (`allowance.share_spent`), `turns_models.rs::an_exhausted_effect_budget_refuses_with_its_code_and_runs_nothing`. Note: only pairing headroom is exported by the kernel, there is no owner-initiated transfer call. |
| 3.4 | Two devices derive the same period budget and disjoint shares | Proven by automated test | `lets_period_vectors.rs::derivation_vectors_are_byte_identical_to_the_python`, `policy_mapping.rs::mapping_builds_a_verifiable_lets_policy_with_disjoint_shares`. |
| 3.5 | No contact for a day drops to solo share, outside effect needs consent | Proven by automated test | Vector scenarios `freshness-window`, `solo-share-and-edge`, `alone-in-the-mesh`. `gate_stack.rs::solo_outside_effects_need_exact_consent_and_are_counted_apart_from_the_cap`, `::solo_exhaustion_of_a_capped_class_says_solo_cap_reached`. |
| 3.6 | Consent drawn by the client, signed by the presence key, changed action refused | Proven by automated test | `turns_compose.rs::{approving_signs_the_exact_digest...,a_signature_by_another_key_is_not_accepted_as_consent,the_prompt_carries_the_exact_bytes_the_consent_digest_covers,a_tool_definition_mutated_after_approval_is_refused_at_dispatch}`. App: `MeshConsentTests::{testActionRejectsDifferentArgumentsAndDifferentAction,testBoundPromptRejectsChangedInputBeforePresenter}`. Real biometric approval was not exercised. |
| 3.7 | Ticket from a removed device or before a stop is refused by a peer and a node | Proven by automated test | `ticket_flow.rs::expiry_stop_removal_and_foreign_roots_are_refused` (`ticket.stopped`, `ticket.device_removed`), `node_mint.rs::a_member_verifier_reports_the_expiry_and_refuses_a_revoked_grant_or_a_removed_member`, Deep `test_node_mesh_chat.py::test_a_forged_or_refused_ticket_is_refused_with_the_kernel_code_and_nothing_runs`. |
| 3.8 | Untrusted content causing an outward effect prompts, across a fixed corpus of recorded attacks | Partly covered | One scenario: `turns_compose.rs::an_untrusted_source_makes_an_outside_effect_ask_and_a_denial_stops_it`, plus `engine_tests::the_gate_sees_the_events_that_raised_the_running_label`. Gap: no recorded attack corpus replayed with the fake provider. Belongs in `astral-kernel/tests` with a JSON corpus like `learning/injection_corpus.json`. |
| 3.9 | Untrusted content remembered in one turn still marks a later turn that loads it | Partly covered | Label inheritance: `astral-authz/src/labels.rs::derived_events_inherit_sources_and_declared_class`, `runs/compaction.rs::the_notes_inherit_the_least_trusted_and_most_confidential_source`. Gap: no two-turn test where a stored memory or note from an untrusted turn raises a later turn's label. Belongs in `astral-kernel/tests/turns_compose.rs`. |
| 3.10 | (live) Adaptive attacks against a live model and a small on-device planner, success rate reported | Live only | Nothing measured. Threshold is not set. |
| 3.11 | Python loop and kernel loop pass the same conformance vectors | Not covered | The two loops have parallel tests with matching names (`test_repeat_guard.py` and `guard.rs`, `test_tool_arguments.py` and `args.rs`) but no shared vector file drives both, and the gate order has no vector. Belongs in `contracts/vectors` with a runner in Deep and in `astral-engine`. |

## Phase 4

| # | Criterion | Status | Evidence and gap |
|---|---|---|---|
| 4.1 | (live) Speaking over the assistant ducks within about 100 ms and silences within 500 ms, measured on device | Live only | Logic with an injected clock: `OnDeviceBargeInTests::{testSoundWhileSpeakingDucksImmediately,testConfirmedInterruptionIsSilentWithinHalfASecondOfOnset}`. Real audio latency unmeasured. |
| 4.2 | Cough or short acknowledgement does not stop it | Proven by automated test | `OnDeviceBargeInTests::{testCoughEndingBeforeConfirmationRestoresVolumeAndKeepsSpeaking,testShortAcknowledgementWordsDoNotStopSpeech}`, `OnDeviceVoicePipelineTests::testCoughDoesNotStopSpeech`. |
| 4.3 | After interruption, history holds only played words | Proven by automated test | `OnDeviceBargeInTests::{testOnlyPlayedTextIsRecorded,testPartialWordAtTheCutIsDropped}`, `OnDeviceVoicePipelineTests::testRealInterruptionStopsSpeechCancelsTheStreamAndRecordsOnlyWhatWasPlayed`, kernel `preview_and_spoken.rs::spoken_text_is_an_owner_visible_event_that_leaves_model_output_alone`. |
| 4.4 | (live) Voice session works in airplane mode on hardware with an on-device model | Live only | The LFM3 model has not been downloaded on any device. Download gating is tested in `VoiceModelStoreTests` and `OnDeviceModelAssetsTests`. |
| 4.5 | Risky action cannot be approved by saying yes | Proven by automated test | `OnDeviceVoicePipelineTests::testSayingYesToAPendingRiskyActionIsOnlyEverATranscript` (asserts `approvalEligible == false`, nothing cancelled), `MeshVoiceLinkTests`. Kernel approval needs a presence signature (`turns_compose.rs`). |
| 4.6 | No voice path contacts a voice server | Partly covered | `OnDeviceVoiceControllerTests::testAppMicrophoneNeverActivatesTheHostedVoiceController` (hosted start count 0). Gap: the hosted controller code and Deep `backend/voice_agent` still exist, and no test blocks the network during a voice session. Belongs in the app tests (a transport that fails on any request) plus the planned retirement of `voice_agent`. |

## Phase 5

| # | Criterion | Status | Evidence and gap |
|---|---|---|---|
| 5.1 | Killing the app mid-turn and reopening resumes without repeating an effect | Proven by automated test | `astral-kernel/tests/runs_service.rs::killing_the_app_mid_turn_and_reopening_resumes_the_run_without_repeating_the_effect` (asserts effect entered once, none after reopen). `engine_tests::a_crash_at_every_journal_step_resumes_without_replaying_an_effect`. |
| 5.2 | Dispatched step with no result shows unknown and is not retried | Proven by automated test | `engine_tests::a_dispatched_call_with_no_result_is_unknown_and_never_retried_without_the_owner`, `views.rs::a_running_turn_and_a_takeover_show_as_running_then_outcome_unknown`, `MeshRuntimeTests::testOutcomeUnknownIsShownAndTheOwnerCanLeaveItOrCheckFirst`. |
| 5.3 | Goal started on the phone is handed to the Mac and finishes there | Proven by automated test | `runs_service.rs::a_hand_off_moves_the_lease_in_the_log_and_the_other_runtime_finishes_the_goal`, `a_phone_with_no_background_time_hands_the_run_to_a_reachable_peer`, FFI `runs_tests::handoff_and_takeover_work_through_two_surfaces_sharing_a_log`. Real devices not yet run. |
| 5.4 | Proposed skill adopted only after replay passes, never widens permission, can be undone | Proven by automated test | `learning_service.rs::a_skill_is_adopted_after_replay_proves_it_fixes_the_failure_and_can_be_undone`, `::a_candidate_that_breaks_a_clean_recorded_turn_is_refused...`, `learning/tests.rs::a_learned_item_never_widens_a_permission_and_stops_applying_when_its_tool_grant_is_revoked`. |
| 5.5 | Proposal with untrusted evidence never adopted automatically, across a fixed corpus | Proven by automated test | `astral-engine/src/learning/tests.rs::no_injection_style_proposal_is_ever_adopted_automatically` (loads `injection_corpus.json`, asserts at least 30 entries), `learning_service.rs::evidence_that_includes_untrusted_content_waits_for_the_owner_even_when_replay_passes`. |
| 5.6 | A Mac with no node runs a bundled first-party agent | Partly covered | Node pack: `NodePackRealRunTests::testDownloadInstallStartHealthDescriptorAndStop` (health, descriptor, stop). `evidence/sqlite-node` ran a bundled agent tool in a container. Gap: no test or live run where the Mac app's own node pack dispatches a bundled agent tool. Belongs in a `NodePackRealRunTests` step that sends one tool call, then a live run on the Mac. |

## Phase 6

| # | Criterion | Status | Evidence and gap |
|---|---|---|---|
| 6.1 | Corpus surfaces parse and validate under the grammar, the validator rejects unresolved bindings and capabilities | Proven by automated test | `astral-ui/tests/grammar.rs::{every_corpus_tree_is_valid_under_the_validator,every_corpus_tree_parses_under_the_gbnf_grammar}`, `strict_profile.rs::{a_binding_must_resolve,an_unknown_capability_is_rejected}`, `generate.rs::an_unknown_binding_handle_or_capability_is_refused`. |
| 6.2 | (live) Valid-surface rate from live models including the on-device one, reported | Live only | Not measured. Grammar is passed to the on-device adapter (`OnDeviceRequest.grammar`). |
| 6.3 | A bound value equals the tool result it came from | Proven by automated test | `surface_rules.rs::a_resolved_document_carries_only_returned_values`, `surface_render.rs::an_unresolvable_binding_shows_nothing_and_is_reported`, `surface_generate.rs::a_model_shows_a_bound_surface_and_a_press_runs_its_tool_through_the_gates`. |
| 6.4 | A button cannot trigger more than the turn's capability | Proven by automated test | `surface_act.rs::a_capability_wider_than_the_originating_turn_is_refused` (`surface.capability_exceeds_turn`), `::an_exhausted_capability_is_refused_even_with_a_forged_or_missing_field`, `surface_generate.rs::a_surface_cannot_hold_a_control_for_a_tool_the_turn_does_not_have`. |
| 6.5 | Actionable component with no spoken form is rejected | Proven by automated test | `strict_profile.rs::{actionable_components_require_all_three_forms_and_a_capability,a_button_without_a_form_or_capability_is_rejected_with_an_example}`, `surface_rules.rs::actionable_components_need_every_form_and_a_known_capability`. |
| 6.6 | A live surface updated on the Mac updates the phone and catches up after offline | Partly covered | `surface_render.rs::a_patch_updates_the_rendered_surface_and_an_untrusted_event_marks_it` (one kernel), `views.rs::surface_events_are_kept_in_order_for_the_ui_crate`. Gap: no two-runtime test where a patch written on one device is rendered on the other after a partition. Belongs in `astral-kernel/tests/surface_render.rs` using the `mesh_flow` pairing helpers. |
| 6.7 | A2UI surface from an outside agent renders through primitives and is labeled untrusted | Partly covered | Kernel (merged): `astral-kernel/tests/surface_a2ui.rs::an_agent_page_is_recorded_as_an_untrusted_surface_without_controls`, `astral-ui/tests/a2ui_surface.rs`. Gap: nothing yet detects A2UI in a tool result automatically, there is no Python reference translator, and no client renders an imported surface. Belongs in the kernel turn path and an app test. |
| 6.8 | A changed MCP tool definition is a new tool | Proven by automated test | `astral-engine/src/mcp/tests.rs::a_changed_definition_is_a_new_tool_until_approved_again`, `tool_catalog.rs::a_granted_catalog_tool_sends_a_request_and_a_changed_definition_is_a_new_tool`. |

## Phase 7

| # | Criterion | Status | Evidence and gap |
|---|---|---|---|
| 7.1 | Devices never online together converge through the mailbox, which holds only ciphertext | Partly covered | `astral-kernel/tests/mailbox_sync.rs::two_devices_that_are_never_online_together_converge_through_the_mailbox`, `astral-mailbox/tests/network.rs::two_devices_that_are_never_online_together_converge`. Gap: nothing scans the mailbox store for message text or roster claims. Belongs in `astral-mailbox/tests/network.rs`. No real host exists yet. |
| 7.2 | (live) Backgrounded phone woken by a content-free push, then syncs | Live only | Deep `tests/test_push_wake.py::test_the_apns_wake_is_a_background_push_with_no_content` covers the payload only. APNs credentials, a real device and the client wake handler are unverified. |
| 7.3 | Removal notice reaches a device that comes online after every other is off | Proven by automated test | `mailbox_sync.rs::a_removal_notice_reaches_a_device_that_comes_online_after_the_remover_is_gone`, `network.rs::a_removal_notice_waits_for_a_device_that_comes_online_after_every_other_is_off`. |
| 7.4 | Institution node refuses stale attestation and unapproved PHI work | Proven by automated test | Deep `test_institution_policy.py::{test_a_stale_attestation_is_refused_with_a_dotted_code_and_nothing_runs,test_phi_work_from_a_peer_outside_the_phi_group_is_refused}`, `test_institution_kernel_integration.py` (real wheel). No live institution. |
| 7.5 | Devices keep working through an issuer change | Proven by automated test | Deep `test_keycloak_move_kernel.py::test_a_mesh_keeps_working_while_devices_attest_under_the_old_and_the_new_issuer`, `scenario_tests.rs::a_keycloak_move_adds_an_issuer_and_old_attestations_stay_on_record`. Runbook `docs/keycloak-move.md` not rehearsed on a real realm. |

## Counts

Total 58 criteria.

- Proven by automated test: 39
- Partly covered: 11 (0.2, 1.1, 2.7, 3.2, 3.8, 3.9, 4.6, 5.6, 6.6, 6.7, 7.1)
- Not covered: 2 (0.4, 3.11)
- Live only: 6 (3.1, 3.10, 4.1, 4.4, 6.2, 7.2)

## Automated tests to write next, smallest first

1. 7.1: mailbox store scan finds no message text or roster claims (`astral-mailbox/tests/network.rs`).
2. 3.9: two-turn kernel test where untrusted memory marks the later turn (`turns_compose.rs`).
3. 4.6: voice session with a transport that fails on any network request (app tests).
4. 6.6: two-runtime test of a surface patch crossing a partition (`surface_render.rs`).
5. 3.8: recorded attack corpus replayed through the turn gate with the fake provider.
6. 2.7: one fixture surface rendered by the Mac and iOS app targets, compared.
7. 3.2: kernel turn calling a local MCP server fixture, then expose it through the FFI.
8. 5.6: node pack test that dispatches one bundled agent tool.
9. 6.7: auto-detect A2UI in a tool result and render an imported surface in an app test.
10. 3.11: shared loop and gate-order vector file run by Deep and by `astral-engine`.
11. 0.2: Android and Windows binding builds, an x86_64 Linux wheel, and a timed kernel CI job.

## Live demonstrations for the owner

1. 1.1: run `astralvessel up` or the Mac node pack as the node. Pair the Mac app and the iPhone to it. Block `sandbox.ai.uky.edu` and Keycloak, then chat with a granted tool. Devices: Mac, iPhone, one node host.
2. 2.2 and 2.1: put the iPhone in airplane mode with Wi-Fi off. Read history, send a message, then reconnect and check the Mac. Then do two devices on Wi-Fi with no internet.
3. 3.1: iPhone in airplane mode, ask a question on the on-device model, check the notice (and the red error bug).
4. 4.4: download the LFM3 model on the Mac (Hugging Face upload needed first), then speak with Wi-Fi off. Repeat on the iPhone.
5. 4.1: on the iPhone, interrupt the assistant mid-sentence and read the logged duck and silence times.
6. 7.2: install the push gateway with APNs credentials, background the iPhone, trigger an event from the Mac, check it wakes and syncs.
7. 5.3: start a goal on the iPhone, hand it to the Mac, and confirm it finishes there. Kill the Mac app mid-turn and reopen.
8. 5.6: on a Mac with no node, run a bundled agent through "Node on this Mac".
9. 2.7 and 6.2: have a model show a surface and view it on both devices, then record valid-surface rates for a live model and the on-device one.
10. 3.10: run adaptive attacks against a live model and the on-device planner and record the rate (threshold first).
11. 0.4: read the rule changes and sign them off.
