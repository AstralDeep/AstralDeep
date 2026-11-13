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
