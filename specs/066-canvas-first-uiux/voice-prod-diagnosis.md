# Voice 503 on sandbox — diagnosis evidence and runbook (2026-08-03)

Symptom: `POST /api/voice/sessions → 503 Service Unavailable` on https://sandbox.ai.uky.edu
when clicking the mic. All four containers report healthy/up.

## What was verified from outside the box (2026-08-03)

| Check | Result |
|---|---|
| Factory `/v1/models` with the rotated key | 200 — inventory lists `zai-org/GLM-5.2-FP8`, `Systran/faster-whisper-large-v3` (task=ASR), `speaches-ai/Kokoro-82M-v1.0-ONNX` (task=TTS, 24 kHz, `af_heart` voice present) |
| Factory `/v1/audio/transcriptions` (real WAV, rotated key) | 200 `{"text":""}` — ASR route live |
| Factory `/v1/audio/speech` (Kokoro, `af_heart`, rotated key) | 200, 34,860-byte WAV — TTS route live |
| Factory auth behavior | no/empty bearer → 200; wrong bearer → 403 (relevant to keyless configs elsewhere, not to voice with a valid key) |
| `https://sandbox-voice.ai.uky.edu/` (Apache → LiveKit) | 200 |
| **Local repro with the SAME code + SAME speech endpoint + SAME key** | Voice worker builds, preflights, is admitted; mic button enables; session leaves `connecting` (stops only at the browser mic-permission prompt). **The code path works.** |

Conclusion: the speech stack and code are good; the sandbox failure is environmental —
the orchestrator has **no admitted voice worker** at session-create time.

## Ranked suspects (what the wiki + timeline + local repro point at)

1. **Stale env in a restarted (not recreated) container.** All secrets were rotated.
   `docker restart` does NOT re-read the env file — a worker container that was
   restarted rather than recreated still presents the OLD `VOICE_CONTROL_SECRET`,
   and every control connection is refused. Local repro shows exactly what this
   looks like in the orchestrator log: `"WebSocket /api/voice/worker-control" 401`.
   The `docker ps` snapshot (worker up 11 min, orchestrator up 8 min, mixed ordering)
   is consistent with per-container restarts around the rotation.
2. **Worker preflighted before the factory served the speech models/routes.** The
   wiki recorded `asr_unavailable` on 2026-08-02; the models and audio routes are
   live as of 2026-08-03. If the worker's preflight failed at container start and
   only retries slowly (or not at all), it never registered.
3. **Closure digest provenance.** Orchestrator and worker compare the SAME env var
   (`VOICE_WORKER_CLOSURE_SHA256`), so equality holds when both containers carry the
   rotated env. Note for hygiene: the current repo `CLOSURE.json` hashes to
   `9ef9e195cd73ba3ff536b7c4d3ec4c15f8ab13472c7fcfe8dc10e4749da6b074`; the env pins a
   different value — verify the pinned digest matches the *deployed* worker image's
   closure, or admission-side validation may refuse once strict digest checks apply.

## Runbook (run on the sandbox box)

```bash
cd /opt/AstralDeep
# 1) See WHY: worker's own story + orchestrator's admission story
docker logs astraldeep-voice-worker --tail 60
docker logs astraldeep 2>&1 | grep -iE "voice|worker-control" | tail -40
# Interpretation:
#   repeated 'worker-control" 401'      -> control-secret mismatch => stale env => recreate worker
#   'asr_unavailable'/'tts_unavailable' -> preflight failed at start => recreate worker (models are live now)
#   nothing at all from the worker      -> also observed locally pre-admission; rely on orchestrator side

# 2) The fix for both top suspects — RECREATE (not restart) so the rotated env loads:
docker compose up -d --force-recreate voice-worker
# (if the orchestrator was also only restarted since the rotation:)
docker compose up -d --force-recreate astraldeep

# 3) Confirm admission, then click the mic again:
docker logs astraldeep --since 2m 2>&1 | grep -i worker-control   # expect "[accepted]" with no 401 loop

# 4) Verify the closure pin matches the deployed image:
docker exec astraldeep-voice-worker sh -c 'sha256sum $(find / -name CLOSURE.json 2>/dev/null | head -1)'
```

## Spec linkage

US9 / FR-033..FR-037 exist because every step above required correlating raw logs
across two containers: the feature adds a single voice-readiness surface (admitted
workers, last refusal reason, last preflight verdict), bounded preflight re-checks
so speech-service recovery doesn't require restarts, and composer-visible refusal
reasons so a 503 is never the only signal. Local finding folded in: an activation
timeout while the mic-permission prompt is pending reports `network_interrupted` —
it should report a permission-shaped reason.
