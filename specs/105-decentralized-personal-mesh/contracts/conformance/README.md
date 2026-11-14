# Shared loop checks

`loop.json` supplies hand-expected argument preparation and repeat-guard scenarios to the
production Python node and Rust engine. These are loop fixtures, separate from the versioned
wire-format vectors and their generator. Both runners exercise valid, malformed, denied and
recovered inputs, including equivalent representations that must share a repeat count.

Run the Python checks from Deep:

```text
python -m pytest scripts/tests/test_loop_conformance.py -q
```

Run the Rust checks from Kernel with `ASTRAL_CONTRACT_VECTORS` set to this feature's
`contracts/vectors` directory:

```text
cargo test -p astral-engine --test loop_vectors --locked
```

Gate precedence already has shared vectors: `labels.json` contains `labels_gate` cases, run
by the Python contract reference and `astral-authz/tests/authz_vectors.rs`. Those prove the
contract's pure gate decision. These checks do not by themselves qualify every composed host
dispatch path or replace the real-dispatch authorization and denial suites.

## Composed gate

`composed_gate.json`, named by `loop.json` under `composed_gate`, takes every `labels_gate`
state to a host that runs the real turn loop. Each case names its `source` state and lists
`setup` operations with their timing, one `call` (tool, arguments and effect), a `consent`
answer and an `expect` block: how many times the tool reached its runner, the normalized
outcome, dotted code and ordered reasons, the consent prompt, the call's journaled result and
the turn status. The file's `baseline`, `operations`, `timing`, `consent` and `expect` fields
define each term. Cases also carry the gate `state` the setup produces when the deciding
evaluation runs, and the pure gate must return the expectation over that state.

A host adapter may seed state through the host's own grant, policy, roster, tool, allowance and
consent paths. It must not replace a gate. A case that a host cannot reach through those paths
names the host and the reason under `skip`.

Check the expectations against the pure gate in both languages:

```text
python -m pytest specs/105-decentralized-personal-mesh/contracts/reference/tests/test_composed_gate.py -q
cargo test -p astral-authz --test authz_vectors --locked
```

Run the composed Kernel host:

```text
cargo test -p astral-kernel --test composed_gate --locked
```

A Python host runs the same cases against a real two-member mesh through the `astral_kernel`
binding. Its adapter needs the host's operations for each `setup` entry, a hook while the model
request that proposes the call is in flight, the pending consent prompt with its reasons, a
presence signer that can sign another action's digest, the tool runner's call log and the
chat journal's `chat.tool_result` and `chat.tool_authorized` bodies for the call.
