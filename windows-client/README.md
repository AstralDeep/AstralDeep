# AstralDeep — Native Windows Client

A native Windows desktop client that renders the AstralDeep orchestrator's
**server-driven UI (SDUI)** components as native Qt widgets — not a web view.

It is a real ROTE/webrender *target*: it consumes the structured `components`
that the orchestrator places on every `ui_render` / `ui_upsert` (the non-web wire
layer) and draws native PySide6 widgets for the SDUI primitive vocabulary (text,
card, table, list, tabs, metric, hero, badge, keyvalue, timeline, rating, alert,
button, code, charts, …). Unknown types degrade to a labeled placeholder.

## Architecture

```
Orchestrator (:8001)  ──WebSocket /ws──►  OrchestratorClient (asyncio thread)
   ui_render/ui_upsert {components}            │  Qt signal
   chat_status / chat_created                  ▼
                                          MainWindow  ──►  renderer.render(dict) ─► native QWidget
   ◄── ui_event (chat_message, button       (chat rail + SDUI canvas)
        actions, load_chat, …) ──────────────┘
```

- `astral_client/protocol.py` — WebSocket client (connect, `register_ui` with
  token + device caps, message loop, `ui_event`/`chat_message` out).
- `astral_client/renderer.py` — structured component dict → native `QWidget`.
- `astral_client/charts.py` — bar/line/pie via QtCharts.
- `astral_client/app.py` — main window (chat rail + canvas) and message wiring.
- `astral_client/theme.py` — dark palette mirroring the web app.

## Deployment profile and precedence

The official 0.4.0 executable contains one reviewed, non-secret production
profile at `deployment/release-profile.json`. It resolves that whole profile
before importing Qt, starting authentication, opening a transport, or hosting
an agent. A clean install therefore opens without the **Configure AstralDeep**
dialog.

Profile sources are selected as complete documents in this order; fields are
never mixed between sources:

1. managed profile path in `ASTRAL_MANAGED_DEPLOYMENT_PROFILE`;
2. `--deployment-profile <json-path>`;
3. the permitted native QSettings value `deployment/profile_json`;
4. the bundled production profile;
5. local defaults only in an explicitly generic, non-frozen developer setup.

An invalid higher-precedence selection fails closed with exit code 78. It does
not fall back to the bundle or to localhost. Per-field deployment variables
such as `ASTRAL_WS_URL` and `KEYCLOAK_AUTHORITY` are intentionally not read
after profile resolution. Credentials are not profile fields.

Validate an installed executable non-interactively without exposing authority,
endpoint, or credentials:

```powershell
.\dist\AstralDeep.exe --validate-deployment --report deployment-validation.json
```

## Run (dev)

Requires **Python 3.11+** and a running orchestrator. For local dev set the
orchestrator to mock auth
(`USE_MOCK_AUTH=true`, `ASTRAL_ENV=development`) so the `dev-token` is accepted.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python main.py --token <JWT>    # uses the bundled production profile
# Explicit local development uses a complete schema-conforming local profile:
.venv/Scripts/python main.py --deployment-profile C:\path\to\local-profile.json --token dev-token
```

The local profile must satisfy the feature-060
[`windows-deployment-profile` schema](../specs/060-runtime-reliability-hardening/contracts/windows-deployment-profile.schema.json):
`distribution` is `generic_developer`, `local_only` is `true`, and its authority
and WebSocket endpoint are loopback URLs. `ASTRAL_TOKEN` remains an optional
credential input; it is not deployment configuration.

## Build the .exe

```powershell
.venv\Scripts\python -m pip install --require-hashes -r requirements-release.lock.txt
.venv\Scripts\pyinstaller --noconfirm --clean AstralDeep.spec
# → dist/AstralDeep.exe  (single-file, no console)
.\dist\AstralDeep.exe --validate-deployment
```

The reusable `build-windows-candidate.yml` workflow performs the authoritative
unsigned build-once candidate run from two clean locked Python 3.11
environments. It runs the actual frozen worker and GUI smokes, records coverage,
and archives the exact EXE with source/run/artifact identities. It does not
sign, tag, publish, or create a release.

## Tests

```bash
QT_QPA_PLATFORM=offscreen .venv/Scripts/python -m pytest -q tests/test_renderer.py
# live end-to-end against a mock-auth orchestrator:
QT_QPA_PLATFORM=offscreen .venv/Scripts/python tests/e2e_live.py --prompt "roll 3 dice"
```

## Auth

- **Dev**: `dev-token` against a mock-auth orchestrator (`--token dev-token`, or
  the fallback when no authority is configured and the first-run dialog is
  skipped).
- **Real Keycloak (OIDC)**: select a complete profile whose `authority`,
  `client_id`, and `auth_mode` are approved together. The app runs
  **Authorization-Code + PKCE with a loopback redirect** (RFC
  8252) — it opens the system browser, you log in, and the token is used for
  `register_ui`; it silently refreshes on expiry. An explicit `--token` always
  wins.

By default the desktop uses its own **dedicated public Keycloak client**,
`astral-desktop` (the by-the-book native-app posture, RFC 8252 / OAuth 2.0 for
Native Apps): the browser does PKCE, then the app exchanges the auth code and
refreshes tokens **directly against Keycloak** — it holds no secret and does not
depend on the orchestrator's BFF, and the web/desktop auth surfaces stay
  isolated. The orchestrator accepts the desktop client's `azp` via its
  `KEYCLOAK_ALLOWED_AZP` allow-list.

**One-time Keycloak setup** (create the public client + add it to the
allow-list): see [`docs/keycloak-windows-client-setup.md`](../docs/keycloak-windows-client-setup.md).

Legacy BFF reuse remains available only when an approved whole profile selects
`auth_mode: keycloak_bff`; it is not enabled by mixing command-line fields into
the production profile.

## Windows tools agent (client-hosted)

Generic/developer profiles may host the legacy **A2A agent in-process**
(`win_agent/`) that exposes
Windows-specific tools to the orchestrator, so the assistant can act on this PC:
`get_system_info`, `read_clipboard`, `write_clipboard`, `notify` (native toast),
`open_path` (file/folder/URL), `list_directory`, plus the **coding tools**
(`read_file`, `write_file`, `edit_file`, `run_command`, `run_shell`). Results
render natively.

The reviewed production profile disables this inbound legacy listener and uses
the authenticated UI-socket tunnel for user-authored agents. In a legacy
generic/developer launch, the client registers the listener at
`http://host.docker.internal:8771`; its local topology can be adjusted with
`ASTRAL_AGENT_HOST` / `WIN_AGENT_PORT`. The agent can also run standalone:
`python -m win_agent.agent --port 8771`.

### Authentication (both directions)

`AGENT_API_KEY` is a **shared secret both sides already hold**, and it now
authenticates **both** directions:

- *Outbound* — the agent presents it in its `register_agent` frame, as before.
- *Inbound* — every request to `GET /.well-known/agent-card.json` and to the
  `/agent` WebSocket must carry `X-Astral-Agent-Key` with a matching value, or
  it is refused with `401` before the socket is upgraded. Without this, any host
  that could reach the port could drive tools that read and write files and run
  commands on this PC — and the register frame, which *contains* the key, was
  pushed to whoever connected before a byte was read.

Consequences to know about:

- **The listener does not start at all without a usable key** — 16+ ASCII
  characters, not a shipped placeholder. There is deliberately **no development
  carve-out on the client**: an unauthenticated file-write/command-exec listener
  should not exist, and a developer who wants it exports `AGENT_API_KEY`. When
  it is refused, the app says so in a banner rather than failing silently.
- **`/health` stays open** and returns only `ok`, so a liveness probe works
  without a credential.
- **The bind stays `0.0.0.0`** because a containerized orchestrator reaches this
  host at `host.docker.internal`, which resolves to the bridge address and can
  never reach a loopback bind. Set `ASTRAL_AGENT_BIND=127.0.0.1` to narrow it
  when the orchestrator runs natively. A non-loopback bind logs a warning naming
  the header that guards it.
- **The orchestrator only sends the key to declared destinations** — loopback,
  the Docker host aliases, `A2A_EXTERNAL_AGENTS` entries, and hosts listed in
  its `AGENT_KEY_TRUSTED_HOSTS`. If this agent is reached at some other address,
  add that host to `AGENT_KEY_TRUSTED_HOSTS` on the orchestrator or the card
  fetch will 401. The restriction exists because `register_external_agent`
  accepts a user-supplied URL and this key can register any agent id.

### Coding agent (feature 039)

The coding tools let the assistant **generate, write, edit, and run code on your
machine**. They are gated by three independent safety layers:

1. **Workspace confinement.** Every file tool operates only inside
   `ASTRAL_WORKSPACE_DIR` (default `~/AstralWorkspace`). Path traversal
   (`../`) and absolute paths outside the workspace are refused; symlinks that
   escape are resolved and refused.
2. **Per-tool permissions.** Each tool declares a scope (`tools:read`,
   `tools:write`, `tools:execute`); you enable them one-by-one under
   **Agents & permissions**, exactly like every other agent. With nothing
   enabled, every coding tool is refused.
   - `read_file` / `list_directory` → `tools:read`
   - `write_file` / `edit_file` → `tools:write`
   - `run_command` → `tools:execute` — runs a **whitelisted** dev command
     (`git`, `python`, `pip`, `npm`, `cargo`, `go`, …) inside the workspace,
     bounded timeout + output cap.
3. **Dangerous bypass (off by default).** `run_shell` gives **full arbitrary
   shell access** (any command, any directory). It is only advertised when
   `ASTRAL_DANGEROUS_BYPASS=1` is set, and each call still requires a native
   confirmation showing the exact command. It is always audited as
   `dangerous_bypass`.

**PHI safety (fail-closed).** Every `read_file` / `list_directory` / `run_command`
/ `run_shell` result is run through a client-side PHI pre-filter
(`astral_client/phi_gate.py`, the same patterns as the orchestrator's gate)
**before** it is returned to the orchestrator/model. If PHI is detected, the
result is refused — it never leaves your machine. If the check itself errors,
the result is treated as PHI and refused (fail-closed).

**Audit (every action).** Each tool call is recorded in an append-only,
hash-chained JSONL log at `%APPDATA%/AstralDeep/audit.log` (`astral_client/
audit_log.py`): timestamp, actor, tool, redacted args, outcome
(`success`/`refused`/`phi_blocked`/`error`), and correlation id. The
orchestrator records its own `tool` audit event too, so actions are double-
audited.

### Integrity verification (feature 039)

When you download the app from a GitHub Release, the client verifies it **before
it ever runs** (`astral_client/integrity.py`):

1. resolves the latest release, downloads `AstralDeep.exe` + `SHA256SUMS` +
   `cosign.bundle`;
2. checks `sha256(exe) ==` the manifest entry;
3. verifies the **sigstore** (keyless) signature, asserting the signing
   identity is the `AstralDeep/AstralDeep` GitHub Actions workflow;
4. only then launches/replaces the binary.

A tampered exe, a bad hash, or an unverifiable signature ⇒ the download is
deleted and refused. Offline on an update check, the current already-verified
binary keeps running (an unverified download is never executed).

Releases are built and signed by [`.github/workflows/release-windows.yml`](../.github/workflows/release-windows.yml)
(fires on `v*` tags).

| Env | Default | Purpose |
|-----|---------|---------|
| `ASTRAL_WORKSPACE_DIR` | `~/AstralWorkspace` | Workspace confinement root |
| `WIN_CMD_TIMEOUT` | `60` | `run_command` timeout (s) |
| `WIN_CMD_MAX_BYTES` | `1048576` | `run_command` output cap |
| `ASTRAL_DANGEROUS_BYPASS` | unset | Enables `run_shell` (full shell) |
| `DESKTOP_RELEASE_REPO` | `AstralDeep/AstralDeep` | GitHub repo for releases |

## Scope / status

Working: real Keycloak OIDC (dedicated public client, silent refresh) → chat →
agents → native SDUI canvas, in-place `ui_upsert` updates, **live push
streaming** (`ui_stream_data` frames rendered in place on the canvas),
button/history interactions, client-hosted Windows tools agent, and native app
*chrome* dialogs: Agents & permissions, History, and the **Audit log** (a
read-only `/api/audit` viewer with event-class / outcome / keyword filters and
cursor pagination). The web app renders chrome as server HTML (`chrome_render`,
which this client acknowledges but never embeds — there is no web view); the
native Qt reimplementations are the parity path (see "Native-only" below).
**Since feature 044**: automatic reconnect (1→30 s backoff + frame resume) with a
connection status chip, identity-reconciled canvas convergence, **table
pagination**, a server-model-driven **top bar**, native **settings surfaces**
(LLM, personalization, theme, guide) delivered as SDUI via `chrome_surface`,
**live theme restyle** (a preset applies immediately), **chat attachments**
(paperclip upload + chips + attach-from-library), `image`/`plotly` renderers, a
robust sign-out ladder, and progress signals (steps / tool progress /
notifications) — each verified live under feature 044.

## Native-only (no embedded web browser)

This client is 100% native: every surface is a Qt widget. There is **no
WebView2 / QtWebEngine** anywhere (the PyInstaller build even excludes those
modules), and we do not embed the web app's HTML.

- **SDUI canvas + chat** → native widgets via `renderer.py`.
- **App chrome** (settings, agent management, modals) → native Qt
  reimplementation driven by the same WS events as the web chrome — *not* an
  embedded HTML view.
- **OIDC login** → the **system** browser (RFC 8252 external user-agent), which
  is the correct, more secure native pattern; it is the OS browser, **not** an
  in-app embedded web view. The app never sees your Keycloak password.
