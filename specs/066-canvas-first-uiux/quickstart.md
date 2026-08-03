# Quickstart: verifying 066 locally

```bash
# 1. Stack (postgres + livekit + orchestrator; voice worker optional)
docker compose up -d astraldeep
# after ANY .env change, remember compose RECREATES containers -> re-sync source:
git ls-files backend > .sync_files.txt && tar -cf .sync_backend.tar -T .sync_files.txt \
  && MSYS_NO_PATHCONV=1 docker cp .sync_backend.tar astraldeep:/tmp/b.tar \
  && docker exec astraldeep bash -c "cd /app && tar -xf /tmp/b.tar && rm /tmp/b.tar" \
  && docker restart astraldeep

# 2. Voice (optional, real speech endpoint): OPENAI_BASE_URL/OPENAI_API_KEY in
#    .env feed the worker's VOICE_SPEECH_*; then:
docker compose up -d voice-worker

# 3. Open http://localhost:8001 (dev: USE_MOCK_AUTH=true signs you in)
```

Manual sweep (mirrors SC-001/003/005/007/008):

1. **Layout modes**: ≥1024px → split rail with "Conversation" header; click
   the `»` collapse → full-width canvas + floating composer (persists across
   reload); resize 700–1023 → collapsed by default; <700 → stacked with the
   Messages bar. Input shows ≥20 chars at every width.
2. **Composer honesty**: mic button always present (SVG icon; disabled with a
   tooltip reason when voice is unavailable). `docker stop astraldeep` →
   pill appears, a send queues visibly; `docker start astraldeep` → queued
   message dispatches once.
3. **First message of a new chat**: fresh session or ＋New chat → welcome
   examples render; clicking one (or typing) completes a turn with canvas
   content (the 060-fence regression is pinned by tests).
4. **Failure drill**: point the user LLM config at an unreachable endpoint →
   send → user bubble stays, inline error card with ↻ Retry appears near the
   composer, canvas content from before the turn is untouched.
5. **Calm chrome**: dashboard example → no per-component action rows at
   rest; hover/focus (or tap on touch) reveals refine/history/provenance;
   the Export-page toolbar has a solid backdrop and never overlaps content
   while scrolling.
6. **Live envelope**: resize across 1024 → server log shows a fresh
   `ROTE: registered device` with the new viewport (no reconnect).

Tests:

```bash
docker exec astraldeep bash -c "cd /app/backend && python -m pytest -q \
  orchestrator/tests/test_bind_chat_066.py llm_config/tests -q"
# full suites per ci.yml posture before merging (three pytest invocations)
```
