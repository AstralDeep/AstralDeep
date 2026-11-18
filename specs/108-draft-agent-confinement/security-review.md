# Security review: draft agents that run unconfined

**Date:** 2026-11-18. **Status:** open. The disposition in section 6 waits for the owner.

**Scope.** Model-written agent servers on a personal node. That is a draft the owner is testing, and
a generated agent the node starts at boot. Read at Deep `350759eb`. Nothing was run. Every finding
below comes from reading the code and the contracts.

## 1. The exception

Spec 106 says untrusted code never runs without an enforcing backend (`spec.md` line 825, and
`plan.md` lines 229 to 231). The execution policy contract lists draft agents as untrusted code
(`contracts/exec-policy.md` line 5).

The owner granted one exception on 2026-11-16, at the phase 5 preflight.

- A node in the PHI group refuses draft agents with `exec.agent_server_unsupported` and the reason
  `phi_node`.
- On a node the owner excluded from the PHI group, a draft agent runs as the node's user with
  resource limits and no confinement.

The reason given was that the policy cannot express what a draft agent needs. It has five fields
(reads, workspace, hosts, limits, time). It has no `listen` field, so no child may accept inbound
connections (`exec-policy.md` line 11). A draft agent needs an inbound port, and writable LETS
database and authority roots outside any one workspace.

Until this review the exception lived in a checkpoint note. The spec, the plan and the tasks do not
mention it. `vessel/docs/security/isolation.md` line 24 still says "Nothing untrusted runs
unconfined."

## 2. What the code does today

| Fact | Where |
|---|---|
| The draft is started with `subprocess.Popen` in a new session, as the node's user | `backend/orchestrator/agent_lifecycle.py` 5160 to 5247, `backend/shared/process_supervision.py` 650 to 662 |
| No confinement backend is used. The draft launch never calls `astral_exec` | `backend/orchestrator/sandbox.py`, `agent_lifecycle.py` |
| Limits are address space 2048 MB, file size 256 MB, 512 open files, 512 processes | `sandbox.py` 41 to 47 and 67 to 85 |
| The environment drops a deny list and names ending in `_SECRET`, `_PASSWORD`, `_TOKEN`, `_API_KEY`, `_PRIVATE_KEY` | `sandbox.py` 15 to 33 and 98 to 107 |
| `AGENT_API_KEY` and `DB_PASSWORD` are kept on purpose | `sandbox.py` 33 |
| `FF_SANDBOX_CODEGEN=false` removes the limits and the environment scrub | `sandbox.py` 36 to 37, `agent_lifecycle.py` 5198 |
| The refusal on a PHI member | `backend/orchestrator/phi_node_policy.py` 31 to 42, called from `agent_lifecycle.py` 5166, `node_api.py` 119, `api.py` 2207 |
| A Kernel error counts as "member", so the refusal fails closed | `phi_node_policy.py` 39 to 41 |
| A node with no mesh, or not enrolled, is not refused | `phi_node_policy.py` 32 to 36 |
| A running draft is stopped when the node joins the PHI group | `phi_node_policy.py` 67 to 76 |
| The launch is announced in the log, the progress log and the `confinement` status | `agent_lifecycle.py` 247 to 253, 5218 to 5219, 5321 |

## 3. Findings

Severity is the reviewer's reading. Each finding was read in the code and not reproduced.

### F1. Generated agents start at boot with the full environment and no limits. High.

`GeneratedAgents.launch` (`backend/start.py` 135 to 144) starts every agent directory that is not
first-party and not marked `.draft`. It passes no environment, so the child inherits everything. It
sets no limits.

This is the same kind of code as a draft agent. It gets less than a draft gets.

### F2. The boot check for the PHI refusal fails open. High.

`_agent_servers_refused` (`start.py` 111 to 118) returns "not refused" when the node's own endpoint
cannot be reached, and on a 404. It is called once before launch (`start.py` 223), after a health
wait that continues on timeout (`start.py` 104).

The endpoint also answers "allowed" while the mesh has not started (`phi_node_policy.py` 32 to 33).

So on a node that is a PHI member, a generated agent can start during boot and run until the next
policy tick stops it (`start.py` 146 to 156 and 309). In that window it has the full environment and
no confinement.

### F3. The agent listens on every interface. High.

The exception was recorded as "an inbound loopback port". The base agent binds `HOST`, which defaults
to `0.0.0.0` (`backend/shared/base_agent.py` 78 and 796). The launch does not set `HOST`.

`/health`, the agent card and the `/agent` websocket are mounted with no authentication middleware
(`base_agent.py` 772 to 786). A model-written server is therefore reachable from the local network.

### F4. Secrets remain within reach. High.

- `AGENT_API_KEY` and `DB_PASSWORD` stay in the draft's environment.
- The scrub is a deny list. A secret whose name fits neither the list nor the suffixes is inherited.
- The agent runs as the node's user. It can read every file that user can read. That includes the
  node keystore, which is a file with mode 0600 (`backend/orchestrator/node_connection_keystore.py`),
  the node's data directory and the mesh store.
- The LETS database and authority roots are separated by a directory with mode 0700
  (`backend/orchestrator/dynamic_runtime_authority.py` 128 to 192). The agent is the same user, so
  the mode does not stop it.
- Egress is open. The draft launch has no proxy and no host list.

On a node outside the PHI group this exposes no PHI. It does expose the owner's mesh identity on
that node, the owner's other chats there, and every credential the node can open.

### F5. No test reaches the refusal through the real path. Medium.

`backend/tests/test_phi_node_gates.py` and `test_phi_node_upgrade.py` cover the policy function with
a real development mesh, and the launch and the route with a stand-in. No test starts a draft agent
through `start_draft_agent` on a real PHI member and sees it refused before any process exists.

`AGENTS.md` asks for authorization changes to be verified through the real dispatch path, with the
denial.

### F6. "Excluded by the owner" is wider in the code than in the ruling. Medium.

The ruling allows the unconfined launch only on a node the owner excluded from the PHI group. The
code also allows it on a node with no mesh and on a node that is not enrolled. Such a node holds no
PHI, and the owner made no choice about it either.

### F7. A flag turns off the remaining limits. Medium.

With `FF_SANDBOX_CODEGEN=false` a draft starts with no limits and the full environment. The notice
then says "with no resource limits". The flag has no place in a production node.

### F8. The written record contradicts itself. Low.

The contract, the spec, the plan and the Vessel docs all say untrusted code never runs unconfined.
The code and the status notice say it does. `tooling/sandbox-qualification/manifest.json` lists
"long-lived draft agents" and "any production draft-launch change" as out of scope, so the sandbox
qualification does not cover this path at all.

## 4. What the policy would need

A draft agent needs three rights the policy cannot grant.

1. **Listen.** Accept connections on one loopback port that the node chose.
2. **State roots.** Write to named directories outside the workspace, for the LETS database and the
   authority root.
3. **Lifetime.** Run until stopped. The policy's `time` field is a wall-clock bound for a tool call.

The policy also changed status. `exec-policy.md` was amended in place before release because v1 had
no consumer outside the program (line 219). The 2.1.0 node pack now ships the v1 evaluator, and
section 9 says a change to a rule raises the format version. So these rights mean
`astral.exec-policy/v2`. They cannot be an edit to v1.

Platform notes.

- **macOS.** The canonical Seatbelt profile allows no `network-bind` and no `network-inbound`
  (`exec-policy.md` line 117). v2 adds a rule for one loopback port.
- **Linux.** Landlock grants no TCP bind, and the seccomp list allows stream sockets only when
  `hosts` is not empty (`exec-policy.md` lines 135 to 157). v2 adds a bind grant for one port, or an
  inbound bridge where the node listens and passes the connection to the confined child.
- **Windows.** No enforcing backend exists. Issue 647 tracks Landlock under Docker Desktop.

## 5. Options

| Option | What it means | Cost | What remains |
|---|---|---|---|
| A. Extend the policy | `astral.exec-policy/v2` with listen, state roots and a service lifetime. The Linux bridge or bind grant. Draft and generated agents run confined on every node | A contract, its vectors and reference, both backends, the conformance suite. Weeks | Nothing on macOS and Linux. Windows refuses |
| B. Refuse | Draft and generated agent servers are refused on every node until A lands | Small. The feature is off for everyone | Nothing |
| C. Keep the exception, close the holes | Fix F1 to F7 now. The launch stays unconfined on a node the owner excluded | Days | F4 in full. A model-written server still runs as the owner |

## 6. Recommended disposition

**C now, A as the target with a date, and B as the default for any node the owner has not opted
in.**

In concrete terms.

1. **Now.** Fix F1, F2, F3, F5, F6 and F7.
   - Generated agents go through the same launch as drafts, with the scrub and the limits.
   - The boot check fails closed. No generated agent starts until the node has answered "allowed".
   - The launch sets `HOST` to loopback.
   - The unconfined launch needs an explicit, recorded owner choice on that node. A node with no mesh
     is refused like any other node that has not opted in.
   - `FF_SANDBOX_CODEGEN` cannot disable the limits when `ASTRAL_ENV` is unset.
   - A denial test goes through `start_draft_agent` on a real PHI member.
2. **Now.** Drop `AGENT_API_KEY` and `DB_PASSWORD` from the draft's environment, or record why each
   must stay and what it can reach.
3. **Next.** Spec the v2 policy (option A). The exception ends when v2 ships.
4. **Record.** Amend `exec-policy.md`, `spec.md` and the Vessel isolation page so the exception and
   its end are written where the rule is.

Why this order. F1 to F3 are defects in the exception as ruled. They widen it past what the owner
accepted, and two of them reach PHI nodes. They should not wait for a policy version.

F4 is the exception itself. Only A or B closes it. A is the design the spec already wants, and B is
the fallback if A slips past its date.

**The owner decides** between A, B and C, the date for A, and whether step 1 may land before that
decision. Until then this review stays open.

## 7. Related work

- The re-homed Vessel capabilities (approval-gated calls, delegation, jobs) add no execution path of
  their own. A Windows port that runs tools without confinement would be a second exception and
  needs its own review.
- Issue 647: Landlock under Docker Desktop on macOS and Windows.
