# Implementation Plan: Create Your Own Agents and Skills — the Easy Path

**Branch**: `077-agent-authoring-ux` (AstralDeep + AstralProjection) | **Spec**: [spec.md](spec.md)

## Technical context

Python 3.11 backend (orchestrator, projection surfaces), ES5 vanilla JS web render
layer (Projection `client.js`), PySide6 Windows client (Projection
`windows-client/`). Existing only — FastAPI, the OpenAI-compatible client through
`_call_llm_json` (the OWNER's LLM), `agent_authoring` (058 phase machine),
`skill_packs` / `knowledge_synthesis` (040), `slash_commands` (040),
`chrome_events` surfaces (043/044). **Zero new dependencies, no schema change, no
new wire frames** (Constitution V, XII).

## Shape

| Piece | Where | Note |
|---|---|---|
| Express lane | `orchestrator/agent_quick_create.py` | In-process runs keyed `(owner, draft_id)`; background task in a detached context (`orchestrator/detached_context.py`, shared with 076); drives `draft_phase` / `advance` / `run_analyze` / `generate_from_session` — the same gated functions as the editor. Stops at Clarify with questions; Analyze refusal ⇒ `failed`. Progress pushes re-render the surface for the initiating socket only while it is open (`chrome_events.open_surface_for`). |
| Desktop presence | `agent_authoring.host_presence` | host sockets ∪ tunnels; label from a 076 computer host name, else platform + version. |
| Surface | `projection_surfaces/authoring.py` | Home view (web + native): status · quick create · run cards · agents · editor sessions · skills · advanced form. Step editor: Clarify naming, stale-pass warning, Generate replaced by Re-run Analyze when stale. New handlers `chrome_author_quick_{create,answers,resend,dismiss}`, `chrome_user_skill_{save,edit,toggle,delete}`. Root carries `data-astral-commands`. |
| Skills | `orchestrator/user_skills.py` | File store under `<knowledge>/user_skills/<owner-hash>/<slug>.md` (authored-pack format), bounded, mtime-cached; `FF_USER_SKILLS`. Digest merge in `skill_packs.build_skill_digest(orch=, owner=)`; expansion in `slash_commands.expand_message(message, user_skills)`; `GET /api/chrome/commands` for discovery. |
| Naming | `projection_surfaces/agents.py`, `drafts.py`; Projection `menu_model.py` | "Owned by me" tab; Drafts form titled as a server-side agent; menu item "My agents & skills" / "My skills" (`skills_enabled` availability input). |
| Web typeahead | Projection `client.js` | Curated + the user's commands (fetched once, refreshed from the surface attribute on every modal render). |
| Windows client | Projection `win_agent/byo_host.py` (`inventory`), `astral_client/local_agents.py`, `app.py` | Client-local "Agents on this PC" dialog under a THIS PC heading in Settings; Stop / Open folder; 2 s refresh. |

## Verification

- Deep: `tests/test_authoring_ux_077.py` (pipeline end-to-end with a fake owner LLM, Clarify stop/resume, Analyze refusal, waiting/resend, presence, skill store bounds/validation, digest + expansion + `/help`, surface web/native, progress-push gating, step-editor copy) + the 058 authoring suites, 063 flag-off suite, chrome suites.
- Projection: `windows-client/tests/test_local_agents_077.py`, menu/topbar suites, provenance ledger.
- Live: the local rig (Docker orchestrator, Windows client from source, web + Android emulator) — the express lane from the web client on RyzenRoll.
