# Windows UI v2 — owner approved, 2026-09-26

**Windows 091 is done by explicit owner approval.** The owner requested a stopping point, approved the current implementation as close enough, and authorized pushing and merging both product repositories. See [verification.md](verification.md) for source-bound checks and remaining limitations; actual PR/merge identities are recorded in kos-wiki.

The current executable is `../AstralProjection/windows-client/dist/AstralDeep.exe`, SHA256 `96bc5b1335a00478385fc1b4fbfadff26dd6b964c26bf497954ae84e623ab6bf`, built from Projection `9b44995f6ca3bba682ea28525ce7361acd8566f2`. Opening it normally selects the bundled `wss://sandbox.ai.uky.edu/ws` profile, as the owner explicitly requested for production use. The task-created local override was removed; ordinary Keycloak PKCE remains required. An optional local-testing launcher remains available with separate runtime settings, but it is not the selected launch target.

Keep the app pointed at sandbox, per the latest owner instruction. No remote server deployment or signed/store release was performed by this push/merge task. No Computer Use or visible app launch is permitted while the owner works. Only the user performs sign-in.

Latest correction: 137 focused source tests pass with four packaged skips, changed-line coverage is 100% (42/42), four actual frozen console-bootstrap fixture checks pass and six package checks pass. The broad final aggregate hit an intermittent Qt access violation; the exact prefix through that boundary passes, but no fix is proven. Physical-display, authenticated-session and real-audio acceptance remain recorded follow-ups, not silently passed checks. Owner approval closes the feature despite these limitations.

No new dependency, shared protocol, primitive, migration, feature flag or other-client implementation was introduced. The executable is unsigned and has not been published as a product release. The earlier comparison gallery remains at kos-wiki `assets/astral-native-ui-v2/windows/consistency-2026-09-26/`, with its original capture hashes and source identities.
