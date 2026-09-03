# Feature Specification: Create Your Own Agents and Skills — the Easy Path

**Feature Branch**: `077-agent-authoring-ux` (AstralDeep + AstralProjection)

**Created**: 2026-09-02

**Status**: Draft — implementation in progress on the feature branches

**Input**: Owner request: "Run through the UI/UX and make it easier and more intuitive for users to create their own agents and skills and run them locally."

## Why now (verified problem statement)

A walk through the product on `main` at `AstralDeep@4c1f24d` / `AstralProjection@3d42976` (2026-09-02) found the following.

1. **"My agents" names three different things.** The personal-agent authoring surface is titled "My agents" (`backend/orchestrator/projection_surfaces/authoring.py:42`); the *Agents & permissions* surface has a first tab also labelled "My agents" that means "agents whose owner e-mail is mine" (`projection_surfaces/agents.py:194`); its empty state sends the user to "the Drafts tab or chat" (`:262`) — a third place with a second "Create a new agent" form of a different shape (`drafts.py:127`). A user who clicks *Agents & permissions → My agents* lands somewhere unrelated to the menu item called *My agents*.
2. **Eleven deliberate actions to a first agent, no express lane.** Settings → My agents → name → description → Start → Specify *Save & continue* → Clarify *Save & continue* → Plan *Save & continue* → Tasks *Save & continue* → *Run Analyze* → *Generate & send to my desktop* → back to chat. Every phase needs a decision the assistant could have made; nothing offers "just build it from what I said", although `agent_authoring.author_and_deliver` (`:1514`) already exists as a one-shot server path and every phase has an assistant-drafting function (`draft_phase`, `:487`).
3. **A guaranteed dead-end for the first-time user.** `host_online` (`agent_authoring.py:1502`) is true only when one of the owner's agents *already* has a live tunnel, so a brand-new user with a healthy, signed-in desktop client always reads "They are offline while none of your desktop hosts is online" (`authoring.py:47`) — and the surface never says how to get a host.
4. **The desktop client is invisible.** The Windows client runs LLM-written personal agents as child processes (`windows-client/win_agent/byo_host.py`) but has no list, no status, no Stop; the user's only feedback is a transient banner (`astral_client/app.py:2307`).
5. **Wording that names the wrong control.** Clarify's gate says "Run Clarify first" (`agent_authoring.py:817`) while the button is "Ask the assistant" (`authoring.py:352`); a post-Analyze edit silently revokes the pass (`generation_gate`, `:938-954`) and the user learns it only at Generate; Generate has seven outcomes, three of which ask the user to distinguish "press Generate again", "press Generate again but fenced" and "never press Generate again".
6. **Skills have no UI, and the word is taken.** Skill packs are committed markdown under `backend/knowledge_packs/techniques/` (`skill_packs.py`; format in `knowledge_packs/README.md`) — authoring one means a commit and a rebuild — while *Personalization → Skills* (`personalization.py:59`) means per-tool permission toggles. Learned recipes (`skill_memory.py`) are a volatile in-process dict behind an unregistered flag with no UI.
7. **Slash commands are fixed and undiscoverable outside the web typeahead.** `slash_commands.COMMANDS` (`:58`) is a literal; the web typeahead hardcodes the same six (`client.js:7894`); users cannot add a command.

## Owner decisions (2026-09-02)

| # | Question | Decision |
|---|---|---|
| D1 | Scope of "run them locally" | Personal agents keep running as child processes of the user's **own desktop client** (057/058 model, untrusted at the boundary). This feature makes them **visible and controllable** there; it does not add a new runtime. |
| D2 | Storage for user skills | **Files, not a schema change.** A user skill is one markdown file in the same format as an authored skill pack, under the runtime knowledge directory (`backend/knowledge/user_skills/<owner-hash>/<slug>.md`, bind-mounted in Compose). Bounded per user. Promotion to an AstralPlane repository is a follow-up (`docs/`). |
| D3 | Which agents a skill applies to | The user chooses: **every chat** ("always"), or named agents. Plus an optional `/command` alias that turns the skill into a slash command. |
| D4 | Hard gates | The Clarify and Analyze gates are **unchanged**. The express lane stops at Clarify when the assistant has questions and shows them as one compact form; Analyze failures show the same plain-language violations and hand the user the step editor. Nothing generates code that the deterministic checker refused. |
| D5 | Flags | `FF_BYO_AGENTS` stays the gate for personal agents (default off). User skills ride a new `FF_USER_SKILLS` (**default on**) because they need no host. Flag-off states are byte-identical to today (pinned). |
| D6 | Dependencies | **None.** |

## User stories

### US1 — Describe it and it exists (P1)

As a signed-in user with the desktop client open, I open *Settings → My agents & skills*, type one paragraph about what I want, press **Create**, and watch a live checklist (Specify · Clarify · Plan · Tasks · Analyze · Generate · Deliver) tick through. If the assistant has questions I answer them in one form and press **Continue**. When it finishes I see "Running on RyzenRoll" and can ask for it in chat.

**Acceptance**: from the settings menu to a running agent takes **3 deliberate actions** (open, describe + Create, and — only when the assistant asks — answer + Continue). Every step is visible while it runs; a failure names the step, the reason and the one next action. The advanced step editor is one click away at every point and is unchanged in what it enforces.

### US2 — I can see my desktop client and what runs on it (P1)

As a user, the surface tells me whether my desktop client is connected (by machine name when it announced one, else platform + version) and, when it is not, exactly what to do (install, sign in with the same account, keep it open). On the PC, *Settings → Agents on this PC* lists every personal agent installed on that machine with its status and lets me stop it or open its folder.

### US3 — My own skills (P1)

As a user, I create a skill — a name, when it applies (always / for these agents), an optional `/command`, and the instructions — and the assistant follows it: always-skills are part of every turn's guidance, agent-skills join the skill digest when that agent is in play, and `/command text` expands to the skill's instructions plus my text. I can edit, disable and delete my skills; `/help` and the web typeahead list my commands.

### US4 — Words that match (P2)

Agents & permissions' first tab is **Owned by me**; its empty state points to *My agents & skills*; the Drafts form is labelled as what it is (a server-side agent that needs admin approval). The Clarify button and its gate use one name; the Generate outcomes collapse to *delivered* / *waiting for your desktop* / *failed* with the next action; an edit after a passed Analyze shows a warning where the edit happens.

## Functional requirements

- **FR-001** A `chrome_author_quick_create {description, agent_name?}` action opens a session, drafts Specify, and runs Clarify → Plan → Tasks → Analyze → Generate → deliver as a **background pipeline** that re-renders the surface after every step for the initiating socket. The pipeline uses the same `agent_authoring` functions the step editor uses; it never bypasses `advance`, `run_analyze` or `generate_from_session`.
- **FR-002** When Clarify yields questions, the pipeline stops in state `needs_answers`; `chrome_author_quick_answers {draft_id, fields}` saves the answers through `advance` (the hard gate) and resumes the pipeline. An answer set that still leaves a question open re-renders the form with the gate's message.
- **FR-003** Analyze failure ends the pipeline in state `failed` with the violations rendered exactly as the step editor renders them and a **Fix in the editor** action that opens the session at Analyze.
- **FR-004** Pipeline state is in-process, per `(owner, draft_id)`, with a bounded step log; a restart loses only the in-flight run, never the session (the session rows are the durable truth and the step editor can resume any of them).
- **FR-005** `host_presence(orch, owner)` reports `{online, label}` where online = any owner host socket (`owner_host_sockets`) **or** any live tunnel; the label is the 076 computer-host name when the same owner announced one, else `platform vX`.
- **FR-006** Generate/deliver outcomes are presented as three states: delivered (agent id + host label); waiting for desktop (`no_host`, `delivery_pending`) with a **Resend to my desktop** action; failed (`analyze_failed`, `generation_failed`, `delivery_failed`, `gate_blocked`) with the reason and one next action.
- **FR-007** The step editor's Clarify action is named **Find open questions** in the button, the empty body and the gate message; a session at `generate` whose spec fingerprint no longer matches the stored Analyze record shows a warning and the Generate action is replaced by **Re-run Analyze**.
- **FR-008** User skills: `orchestrator/user_skills.py` stores ≤ `MAX_SKILLS` (20) files per owner, each ≤ `MAX_INSTRUCTIONS_CHARS` (4000), slug from the name, frontmatter `name`, `type: user_skill`, `owner`, `command`, `applies_to` (`always` or a list of agent ids), `enabled`, `updated_at`. Reads are cached by mtime. Path components are derived from a hash of the owner, never from user text.
- **FR-009** `skill_packs.build_skill_digest` gains an `owner` parameter: enabled always-skills are included first (bounded), then agent-scoped skills for the agents in play, then authored/synthesized packs; the total stays under `MAX_DIGEST_CHARS` (raised to 2500 to make room).
- **FR-010** `slash_commands.expand_message(message, user_skills=…)`: an enabled skill with a `command` alias expands `/<command> text` to a prompt that quotes the skill's instructions and the user's text; user commands shadow nothing in the curated set (a clash is refused at save time); `/help` lists the user's commands after the curated ones.
- **FR-011** The surface gains a **Skills** section: list (name, `/command`, applies-to, enabled), create/edit form, enable/disable and delete actions (`chrome_user_skill_save`, `chrome_user_skill_toggle`, `chrome_user_skill_delete`). Web `render()` and native `components()` both.
- **FR-012** Web discovery: `serve_shell` injects the signed-in user's commands, and the surface root carries `data-astral-commands` so the typeahead refreshes after edits (client.js merges curated + user commands).
- **FR-013** Windows client: `ByoAgentHost.inventory()` returns `[{agent_id, name, status, pid, directory, revision}]`; a client-local **Agents on this PC** dialog (Settings menu, always present when the host disposition is active) lists them with **Stop** and **Open folder**, refreshing every 2 s while open.
- **FR-014** Agents & permissions: tab label **Owned by me**; empty state names *My agents & skills*; Drafts create form titled **Create a server-side agent (admin approval)**.
- **FR-015** Flag-off (`FF_BYO_AGENTS` off) keeps the personal-agent part exactly as today's disabled notice; the Skills section is present whenever `FF_USER_SKILLS` is on. `FF_USER_SKILLS` off removes the section, the digest merge and the command expansion (pinned).
- **FR-016** No new third-party dependencies; no schema change; no new wire frames (the surface is rendered through the existing `chrome_render` / `chrome_surface` frames; actions are `chrome_*` ui_events).

## Success criteria

- **SC-001** A user with a connected desktop client creates and runs an agent from one description with ≤ 3 deliberate actions (pinned by a surface test that walks the pipeline with a fake LLM).
- **SC-002** No code is generated for a session whose Analyze did not pass, on the express lane exactly as in the editor (pinned).
- **SC-003** A first-time user with a connected desktop client sees it as connected before creating anything.
- **SC-004** A skill created in the UI changes the next turn's system prompt and `/command` expansion (pinned), and disappears from both when disabled or deleted.
- **SC-005** The Windows client lists and can stop a running personal agent (offscreen test with a fake host).
