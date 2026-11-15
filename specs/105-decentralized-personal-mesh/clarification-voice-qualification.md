# Voice acceptance clarification

Date: 2026-11-14. Authority: direct owner instruction in the active implementation chat.

The owner instructed: “modify the spec to not incorporate live voice testing to qualify spec 105. code tests are fine for anything related to voice”.

Automated code tests now qualify every voice requirement. Spec section 12.5 and plan Phase 4 describe the required unit/integration/simulator/client tests and controlled clock/audio/model/network fixtures. Live microphone, speaker, audible-response, acoustic timing, physical-phone/watch and human airplane-mode voice tests are optional diagnostics. The implementation, safety gates, asset verification and truthful failure requirements continue to apply.

Affected acceptance criteria: 4.1–4.6 and voice-specific playback, interruption, restart, offline and paired-watch checks elsewhere in the feature. This clarification does not change model/provider credential custody, permission gates or feature scope.
