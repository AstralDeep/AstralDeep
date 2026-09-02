# Your own agents and skills (feature 077)

This is the user's guide to *Settings → My agents & skills* — the one place to
build a **personal agent** that runs on your own PC and to write **skills**
the assistant follows for you. (Operators: enablement and the security posture of
personal agents are in [byo-client-agents.md](byo-client-agents.md).)

## Two things you can make

| | Personal agent | Skill |
|---|---|---|
| What it is | A small program the assistant writes for you, from your description, that runs on **your** desktop client as its own process and shows up as tools in chat. | Standing guidance in your own words — how you like things done, a checklist, a format — that the assistant follows. |
| Where it runs | Your PC (the AstralDeep desktop client). Never on the server. | Nowhere — it is text the assistant reads. Works in every client. |
| Needs | `FF_BYO_AGENTS` on, and the desktop client signed in with your account. | Nothing (`FF_USER_SKILLS`, default on). |

## Create an agent in three actions

1. Open **Settings → My agents & skills**. The top line tells you whether your
   desktop client is connected (by machine name when it announced one). If it is
   not: install the desktop client, sign in with the same account, keep it open —
   it appears here as soon as it connects.
2. In **Create an agent**, describe what you want in plain words, e.g. *"Every
   morning, read the CSV in my Downloads folder called sales.csv and tell me the
   three biggest changes since yesterday"*, and press **Create**. A checklist
   appears and ticks through: write the specification · check for open questions
   · plan the tools · break down the build · check the agent rules · generate the
   code · send to your desktop.
3. If the assistant has questions, they appear in the same card — answer them and
   press **Continue**. That is the only time it stops. When it finishes you see
   *Running on <your PC>* and can ask for it in chat.

What can go wrong, and what to do:

- **The agent rules refused this design** — the deterministic checker
  (Constitution A–L for personal agents: owner-only data, declared tools, least
  privilege, no sharing) found a problem. The card lists each problem in plain
  language; **Fix in the editor** opens the step-by-step editor at that point.
  Nothing was generated.
- **Waiting for your desktop** — the agent is built and verified but no desktop
  client was connected. Open the client, then press **Resend to my desktop**; no
  model call is made, the same verified bundle is sent.
- **Advanced: build it step by step** — the full editor (Specify → Clarify →
  Plan → Tasks → Analyze → Generate) where you write or edit every artifact
  yourself. It enforces exactly the same gates as the express lane.

## See and stop agents on your PC

On the desktop client, **Settings → Agents on this PC** lists every personal agent
installed on that computer with its status (online · starting · offline) and
process id. **Stop** ends it now; **Open folder** shows the generated code.
Delete lives in *My agents & skills* (it stops the agent and removes it
everywhere).

## Skills

In *My agents & skills → Your skills*, **Add a skill** with:

- **Name** — e.g. *Weekly status format*.
- **/command** (optional) — e.g. `standup`. Typing `/standup fixed the build` in
  chat hands the assistant your instructions plus what you typed. `/help` lists
  your commands; the web chat suggests them as you type `/`. Built-in command
  names are reserved.
- **Applies to** — leave empty for *every chat*, or list agent ids
  (e.g. `web-research-1`) so the skill joins only turns where that agent is in
  play.
- **Instructions** — up to 4,000 characters.

You can keep up to 20 skills; **Disable** keeps one without applying it. Skills
are stored as markdown files in the same format as the deployment's authored
skill packs (`backend/knowledge/user_skills/<owner>/<slug>.md`, one directory per
user, named by a hash of your account), so an operator can promote a good one to a
shared pack by copying the file.

## For operators

| Setting | Default | Effect |
|---|---|---|
| `FF_BYO_AGENTS` | off | Personal agents (unchanged from feature 058). |
| `FF_USER_SKILLS` | on | User skills: the surface section, the per-turn digest lines and `/command` expansion. Off is byte-identical to before. |

Skills live under the runtime knowledge directory (`./backend/knowledge`, bind-mounted
in Compose); back it up with the rest of that directory. Multi-instance
deployments need a shared mount — moving skills into an AstralPlane repository is
the documented follow-up.
