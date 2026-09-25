# Verification workflow

1. Python 3.11 with declared Windows requirements and CI tools; `QT_QPA_PLATFORM=offscreen`; run `python -m pytest components/AstralProjection/windows-client/tests -q` with coverage.
2. Run Projection ROTE/chrome, Deep chrome/protocol/surface and browser checks from committed CI invocations; retain inherited baseline failures separately.
3. Build exact candidate backend with real Keycloak/PostgreSQL/voice worker; ordinary user sign-in, no copied tokens or bypass.
4. Compare web/Windows at seven manifest sizes: scenarios, history, search, settings/themes, composer/attachments/Advanced, successful result collapse/expand/fullscreen, export/share and continuity.
5. Exercise keyboard, available touch, four display scales, and voice capture/playback/interruption/recovery; record unavailable checks.
6. Record screenshot hashes/dimensions/source/backend identities in vault; update curated pages/index/log and push vault only.
