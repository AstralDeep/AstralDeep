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

For normalized refusal comparison, adapters map only `allowance.share_spent` and
`allowance.solo_cap_reached` to the pure gate code `allowance.exhausted`. The
`journal_code` expectation retains the actual holder refusal. This preserves the detailed
allowance reasons permitted by `requests.md` without changing gate precedence or dispatch.

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

Production personal and institution signed mesh requests use the same Kernel `answer_request`
turn service. Python hosts ticketed agents through its protected executor; it does not run a
second production mesh model loop or receive a leased provider key. The Kernel fixture covers
all 32 pure states and its 30 reachable composed cases, retaining the two documented intrinsic
host skips. The fixture and gate expectations are unchanged.

Deep's `test_node_mesh_composed_gate.py` checks the actual paired-binding executor boundary,
including the fixture's all-clear effect, exact action/request/definition binding, current
permissions, audits, replay, concurrent reuse and uncertain results. The institution binding
suite adds separate owner chats and provider credentials plus allowed, unattested, stop,
revocation, replay and audit/unknown outcomes. The normal owner-scoped attestation HTTP suite
uses real signed tokens, verifies owner/nonce denial and covers a second accepted issuer.
These tests qualify the shared-engine architecture; they are not a separate 32-case Python
composed-loop run. Explicit Python runners remain test/integration adapters, and the argument
and repeat helpers above retain their independent legacy-loop checks.
