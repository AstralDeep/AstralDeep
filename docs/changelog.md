# Changelog

## Unreleased

AstralVessel becomes the base's headless installation. See [Migration from 0.6.0](run/migration.md).

- `astralvessel up` runs a personal AstralDeep node on SQLite in a named volume, with no PostgreSQL.
- Added the executor, which verifies tickets before a sandbox runs a tool, and `pair`, `conformance`, `ask` and `chat` against the node.
- The sandbox and egress containers are kept. The probes now check the node, the executor and the egress.
- The executor image build needs the pinned kernel wheel and checks its sha256. A missing kernel gives `executor.no_kernel`.
- Each agent has its own executor token, in `.generated/executor-agents.json`. The operator token lists and pairs only.
- The executor derives a call's owner and workspace from the deployment and the verified ticket, never from the request.
- Spent ticket ids persist across an executor restart.
- Removed the agent loop, the A2A server, identities, history, service tools, web tools and search.

## 0.6.0 (2026-11-12)

- **Four-command setup.** `astralvessel up` writes `deployment.toml` and a private `.env` when there is none (asking
  for agent folders, model URL, key and model, or taking them as options; `env:NAME` keeps the key out of every
  file), starts the deployment, waits until every sandbox is confined, runs the isolation probes and tests the
  model. `--example` sets up the example agent, which now ships in the package.
- `astralvessel check` runs the isolation probes against every agent of any deployment; they ship in the package
  and replace `deploy/verify-isolation.sh`.
- `astralvessel chat` (saved, resumable chats through the history store) and `astralvessel ask` (one turn), run in
  a short-lived container on the internal `front` network; `astralvessel down` stops a deployment.
- An optional `model` per agent in `deployment.toml`, overriding the agent folder's.
- **The conformance suite is removed.** Its checks that the tests did not already make are now in the test suite.
- **The vessel's address is reserved.** Containers that join `front` (applications, `chat`, `ask`) now get
  addresses only from the upper half of `front_subnet`, so none can take the vessel's address while it is stopped.
  `front_address` must be in the lower half (the default `.10` is), and `front_subnet` must be a /28 or larger. The
  next `up` (or `deploy` plus `docker compose up`) recreates an existing deployment's `front` network: stop applications attached to it first, and start
  them again afterward.
- **macOS with Docker Desktop on Apple silicon is supported**, after the isolation probes passed there.
- The documentation site now updates when a release is published, not on every change to `main`.
- `tool_error` is no longer a turn status; it was reserved and never produced. A failed tool call never ends a
  turn.
- `turn.finished` is documented as always carrying `files`, `[]` when none, with a regression test.

## 0.5.0 (2026-11-11)

- A workspace per turn that its tool calls share, deleted when the turn ends.
- Files from tools (PNG, SVG, CSV, PDF) through `$VESSEL_OUTBOX`, checked twice and streamed as A2A artifacts.
- `[vessel] project` for several deployments on one host.
- CI (lint, tests on Python 3.11 and 3.12, isolation probes on a running deployment), Dependabot, and a release
  workflow that publishes multi-architecture images to GHCR and the package to PyPI.
- **The model endpoint is required.** `[egress] upstream_url` has no default; set it to an `http://` or `https://`
  URL or to `env:NAME`. The example deployment reads it from `VESSEL_UPSTREAM_MODEL_URL`.
- **Disk limits per turn and per user.** `turn_disk` and `user_disk` in `[agents.sandbox]` (by default a quarter and
  half of `scratch`). No file may outgrow the turn's limit; each file or folder also counts 1 KB; files deleted but
  still open count. A turn over either limit has its calls stopped and its workspace emptied. One new isolation
  probe.
- **Calls stay in their session.** seccomp blocks `setsid` and `setpgid`, and each call may hold at most 4,096 open
  files.
- **Issuer key files work in the generated deployment.** A `jwks_file` is mounted into the vessel, a missing one
  is refused when the deployment loads, and changing its contents recreates the vessel on the next `up -d`.
- The conformance checks accept the `file` event.
- **A documentation site**, built with MkDocs and published to GitHub Pages.
- The isolation probes now number 43.

## 0.4.0

- Verified-token callers: an issuer allowlist with audience, expiry, authorized party and asymmetric signature
  checks; owners keyed by issuer and subject.
- Per-call scope narrowing through token scopes or `X-Vessel-Scopes`.
- Structured skill calls in the shape Astral sends to external agents.
- Web search through a private SearXNG container, with failed searches reported as errors.
- A token on the egress model passthrough, so only the vessel can spend the key.
- A turn survives tool arguments that are not JSON.
- The vessel and sandboxes are recreated when an agent's manifest, tools or the deployment change, and a sandbox is
  served only if it loaded the same tools as the vessel.

## 0.3.0

- Service tools answered by the application over HTTP, with credentials held by the vessel.
- Image input for agents whose model accepts images.
- Per-agent sandbox images built from a Dockerfile in the agent folder.
- Briefings reloaded on every turn, and `prompt_append` so one folder can serve as several agents.
- `volume:NAME` mount sources.

## 0.2.0

- One vessel serving many agents, each with its own sandbox, card and A2A address.
- `deployment.toml`, per-app keys, and Compose generated by `astralvessel deploy`.
- Application-owned history, with transcripts streamed in OpenAI chat format.
- Per-call confinement with Landlock and seccomp.

## 0.1.0

- The first harness: the agent loop, sandbox, egress, conformance checks and isolation probes.
