# Security qualification and diagnostic ablations

The required local fixture gate runs scripted model turns as real turns on a personal development node. A development owner device on the real kernel binding sends each request over loopback, and the node answers it on the Kernel turn engine. A scripted OpenAI-compatible provider, installed in the Kernel's provider book, stands in for the model. The Kernel sends each proposed tool call to the node as a ticketed self-call, which runs through mesh admission, the vessel executor, protected dispatch and the audit chain. Sandboxed fixture tools count their physical effects. It is a **dispatch-boundary check**. The scripted provider deliberately proposes the forbidden action; the result does not measure how often a real model would take the bait. An attack's effect is refused when the Kernel withholds it for owner consent, which the driver records and declines.

From a clean exact candidate checkout, with the pinned kernel wheel and mailbox binary installed and `ASTRAL_ENV=development`:

```bash
cd backend
python -m pytest security_benchmark/tests/test_personal_dispatch.py -q
python -m security_benchmark --qualify-real-dispatch --out security_benchmark/_artifacts
```

The first command is part of the backend module test group. The second writes `real-dispatch-qualification.json` and exits nonzero if any case, control, audit, candidate identity, or teardown condition is incomplete. It needs no sign-in, bearer token or identity server: the owner is the development mesh's owner device, and a denial counts only when the node's own gate refuses the effect after the source was read. Production owner meshes are never used.

The pinned local population is twelve attack/control pairs over fixture tool results, loopback remote-agent replies, and retained conversation context. The effect oracle reads the fixture server's physical effect ledger, while the independent audit oracle verifies the owner's chain. Each control must complete a read. A withheld effect with a failed control is indeterminate, not a pass. The cases establish the host's deterministic gate; they are not a statistical prompt-injection success rate or an external benchmark score.

The historical `synthetic` ablation runner is still available for diagnostic comparisons:

```bash
cd backend
python -m security_benchmark --mode synthetic --benchmark agentdojo
```

Its bundled AgentDojo, ASB, and InjecAgent cases are **samples inspired by those suites**. They are not imported upstream cases, and their ASR must not be used as a release qualification result. The old `in_process` runner never implemented `_drive_real_turn`; it now exits with an error instead of reporting a real result. The `external` legacy mode shares that refusal.

`ASTRAL_BENCH_LOAD_REAL=1` currently refuses all three external suites because the project has no qualified mapping from their tools, tasks, and outcome oracles to AstralDeep. It never falls back to sample rows or relabels them as real. AgentDojo 0.1.35 is pinned for evaluation research in `requirements-eval.txt`; its wheel digest is documented there. A supported external mapping needs a separate manifest, eligibility count, exact corpus digest, paired utility controls, and physical effect oracle before it can enter the required gate.

The local fixture report records the candidate commit, policy and component digests, case population, observed effects, control outcomes, audit references, and teardown. It is diagnostic local evidence until the exact candidate's protected CI jobs pass. Apple and Android device tests are outside this server fixture; web behavior needs separate live verification.
