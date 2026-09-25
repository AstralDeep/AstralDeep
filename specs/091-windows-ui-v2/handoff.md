# Windows UI v2 handoff

The separate Windows work uses `codex/091-windows-ui-v2` in Deep and Projection. Source qualification binds Deep `94353536f00b1e2d18102474cf0b01cc8822a0a6` and Projection `503b96287b4fbe7ae3a87bea6cb4c4b976b2c981`; later handoff commits contain documentation only. Read [verification](verification.md) for exact commands, results, prior failures and remaining gaps. This is implemented work under qualification, not complete acceptance or release.

## Shared ownership

Published shared090 remains Deep `4e2eae707e108ee868212cec653ae107fdb15518` / Projection `1d4a8642a888279dd42a67181412901960b50217`. The vault reports newer local Mac work, including Deep `d2a61145` / Projection `ce0c158`, but it is not remotely available yet. Do not recreate it or edit090 artifacts.

The snapshot identity/cache correction is shared behavior discovered by Windows qualification. Separate commit `f82a5ddcecf74ccea620e01a6ec6461809ffca30` on `codex/091-snapshot-identity-fix` is based directly on published090 with exactly these three files and no Windows pins or artifacts:

- `backend/orchestrator/orchestrator.py`
- `backend/tests/test_component_chrome_delivery_088.py`
- `backend/tests/test_canvas_consolidation.py`

Its canonical file blobs match the tested94353536 repair. The vault records its exact published commit for the Mac task to integrate. Preserve raw identities for chrome and the public ROTE cache; keep consolidated initial presentation. Do not change the original ambiguous-ID denial test to use distinct data.

## Local runtime and evidence

The loopback development backend runs image `sha256:4e531d5a3b0a49261093bbe4628e532e04216111d47b1997444670849899a168`, labeled94353536, with unchanged environment and existing data mounts. The ignored override is `build/windows-091/local-candidate.compose.yml`; ordinary Compose without that override still names its previous image tag. Do not overwrite credentials or reset data when resuming. Worker speech preflight passes; native audio is unverified.

The final unsigned executable is `components/AstralProjection/windows-client/dist/AstralDeep.exe`, SHA256 `6cec49b1c2c4bd9691a6cec46df1b6aabe924de775705c7ebc836f64e3825bc3`. It remains an ignored local diagnostic artifact. Build logs, JUnit, coverage and exact-source Linux reports are under `build/windows-091/`. Curated evidence and source-bound screenshots are in kos-wiki; the offscreen scaling captures are not physical-display acceptance.

## Remaining work

1. Obtain the user's normal native Keycloak sign-in. The ignored `build/windows-091/live_native.py` launcher connects the source client to local94353536 using ordinary PKCE, with token fallback disabled. Do not type credentials or manufacture/copy tokens.
2. Verify current native resize at reference logical dimensions, keyboard/focus, physical125/150/200% DPI/monitor transitions, attachments, real Advanced revision selection, conversation/state restoration, saved authorized export and available share actions. Current hardware reports no touch support; keep that limitation explicit.
3. Verify real native microphone capture, worker reply, playback, interruption and recovery. Keep the web-only passive voice warning absent while preserving actionable native errors.
4. Integrate the exact published090 fixture/provenance/voice corrections when available, then rerun affected shared gates and finish complete backend/connected packaged qualification without weakening assertions or deadlines. Current Projection has five inherited failures; the initial complete backend run stopped at its four-hour bound. No full-CI pass is claimed.
5. Continue curated vault/index/log/screenshot checkpoints and branch pushes under the owner's authorization. No merge, production deployment, signing or release is authorized.
