# Windows UI v2 local handoff — 2026-09-26

Implementation and local automated qualification are complete on `codex/091-windows-ui-v2`. Projection `c2c9f06dacc0574f6d0784f0f1db6255cdb893a0` is pinned by this Deep commit. Product commits are local and unpushed. The prior stopped-work handoff is superseded; its evidence remains in Git and the historical section of [verification.md](verification.md).

The owner prohibited Computer Use while working. Continue through offscreen Qt, files and tests; do not open or manipulate the desktop. Only the user performs ordinary Keycloak sign-in. Physical native display, authenticated interaction and real voice-worker acceptance remain open; local test success is not acceptance, merge, deployment or release.

The refreshed native shell, narrow first layout, typography, modal settings, result presentation and scoped viewport hydration are implemented. Test/package profiles use isolated UUID4 settings namespaces and preserve the user's normal profile. No new dependency, protocol, primitive, migration, other-client change or 090 artifact change was introduced.

Current evidence: **1965 Windows tests passed with 10 declared skips**, separate **16 supervision tests / 800 trials**, **97.69% changed executable coverage**, **3269 Linux Projection tests**, **300 Qt scaling executions**, **61 backend plus 2 admission tests**, **88 Deep workflow tests with 3 declared skips**, helper **37+1 tests**, and **6 frozen executable tests plus actual offscreen retry**. Exact commands, scope and failures are in [verification.md](verification.md) and ignored `build/windows-091/` reports.

The usable local executable is `../AstralProjection/windows-client/dist/AstralDeep.exe` (SHA256 `3f19af0316d434ecb8a75c8b65128e0c17fa2fc95063190c4d37b5a93bec8f3c`). It remains unsigned. The current backend image and its exact source limits are recorded in verification; do not describe it as a rebuilt final component image.

The final 53-view comparison archive is kos-wiki `assets/astral-native-ui-v2/windows/consistency-2026-09-26/`; open `comparison.html` for paired references and overlays. The tracked [diagnostic matrix](diagnostics/diagnostic_matrix.py) reproduces the captures using the pinned component without Computer Use. They contain only synthetic/public content and do not establish authenticated or physical-display acceptance.

Remaining acceptance tasks are T010 real voice, T013 physical displays/touch, T015 live authenticated behavior, and T016 any future separately authorized product publication. The curated wiki checkpoint is maintained in a separate repository under standing permission; its current log records publication status. Do not push, merge, sign or release product repositories without applicable user authorization.
