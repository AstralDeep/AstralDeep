Local backend runtime checkpoint — September 26, 2026

The local backend runs diagnostic image `sha256:ab800558d0ed4433a283f5feffc352e98dc9f938994970be328d658696cc9100` built from Deep `3aa18e0af5032d0518bbd7c3b494808d78d8ec26` and Projection `e0419ddaad927a64c575eac21bfcc26f54378a3a`. All LETS, Plane and Primitives pins are unchanged. The image reuses the prior dependency/model layers; it is not the clean production-image CI gate.

All 1,432 baked backend files and four installed component locks were verified. After replacing only `astraldeep`, 1,319 nonmounted source files and the component locks were verified again. Liveness/readiness returned HTTP 200. Redacted configuration digests match for environment, mounts, ports, restart policy, entrypoint and network names; PostgreSQL, LiveKit and worker container IDs are unchanged.

Focused verification: 140 unique backend cases passed after correcting two missing-source fixtures; the original failure and skips remain in their reports. Current Windows resize admission passes 5 cases in 1.41s after adding `typing.Optional` to its AST harness namespace. ROTE/console passes 310 cases in 2.44s. Chromium fullscreen, focus, privacy, preview, settings and Run regressions pass 18 cases in 20.2s. Initial Windows Python alias/encoding setup failures are retained. No Qt test suite ran here.

All seven named test containers, the private network and its anonymous PostgreSQL volume were removed. The running diagnostic image, its ignored Compose override, source snapshot `/home/sam/astral091-live-l4p07kjs/context` and ordinary BuildKit cache remain identified in `runtime-evidence.json`. User services/data were preserved.

The complete frozen-source Windows suite, changed-line coverage, clean image/full CI and authenticated visual/worker acceptance remain required. These backend and fixture checks do not establish screenshot fidelity.
