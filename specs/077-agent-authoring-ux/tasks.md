# Tasks: Create Your Own Agents and Skills — the Easy Path

- [x] T001 Spec + plan (this directory).
- [x] T002 `agent_authoring.host_presence` (host sockets ∪ tunnels, labelled) — FR-005.
- [x] T003 `orchestrator/detached_context.py` shared with 076's continuation.
- [x] T004 `orchestrator/agent_quick_create.py` — the express-lane pipeline with in-process runs, Clarify stop/resume, Analyze failure, delivered / waiting / failed outcomes — FR-001..FR-004, FR-006.
- [x] T005 `chrome_events.open_surface_for` / `_note_open_surface` — progress pushes only while the surface is open.
- [x] T006 Surface home view (web + native): status, quick create, run cards, agents, editor sessions, skills, advanced form — FR-011.
- [x] T007 Step editor copy: "Find open questions"; stale-pass warning + Re-run Analyze — FR-007.
- [x] T008 `orchestrator/user_skills.py` store + `FF_USER_SKILLS` — FR-008, FR-015.
- [x] T009 `skill_packs.build_skill_digest(orch=, owner=)` merge; orchestrator wiring — FR-009.
- [x] T010 `slash_commands.expand_message(message, user_skills)`, `/help`, `reserved_names`; orchestrator wiring — FR-010.
- [x] T011 `GET /api/chrome/commands`; Projection `client.js` typeahead merge + `data-astral-commands` refresh — FR-012.
- [x] T012 Naming: "Owned by me" tab, Drafts form title, menu item "My agents & skills" / "My skills" (`skills_enabled`) — FR-014.
- [x] T013 Windows client: `ByoAgentHost.inventory()`, `local_agents.LocalAgentsDialog`, THIS PC menu entry — FR-013.
- [x] T014 Tests: `tests/test_authoring_ux_077.py`; `windows-client/tests/test_local_agents_077.py`; updated 058/063 pins.
- [x] T015 Docs: `docs/your-own-agents-and-skills.md`; CLAUDE.md entry; kos-wiki page.
- [x] T016 Live verification on the rig (web → express lane → RyzenRoll → chat call answered; agent survives a client restart; a skill + `/standup` through the typeahead). Eight v3 personal-agent defects found and fixed on the way (see CLAUDE.md). *Agents on this PC* dialog: offscreen tests only.
- [ ] T017 PRs (Projection first, then Deep with the composition repin).
- [ ] Follow-ups: skills as an AstralPlane repository (multi-instance); Android/Apple render check of the new home view (SDUI, no per-client code expected); learned-recipe visibility (`skill_memory`) once that flag graduates.
