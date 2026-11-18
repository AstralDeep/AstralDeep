# Security diagnostic ablations

The signed-turn fixture gate was retired with the identity server it exercised. Only the diagnostic ablations remain, and none of them is release qualification.

The pinned local population is twelve attack/control pairs over fixture tool results, loopback remote-agent replies, and retained conversation context. The effect oracle reads the fixture server's physical effect ledger, while the independent audit oracle verifies the owner's chain. Each control must complete a read. A withheld effect with a failed control is indeterminate, not a pass. The cases establish the host's deterministic gate; they are not a statistical prompt-injection success rate or an external benchmark score.

The historical `synthetic` ablation runner is still available for diagnostic comparisons:

```bash
cd backend
python -m security_benchmark --mode synthetic --benchmark agentdojo
```

Its bundled AgentDojo, ASB, and InjecAgent cases are **samples inspired by those suites**. They are not imported upstream cases, and their ASR must not be used as a release qualification result. The old `in_process` runner never implemented `_drive_real_turn`; it now exits with an error instead of reporting a real result. The `external` legacy mode shares that refusal.

`ASTRAL_BENCH_LOAD_REAL=1` currently refuses all three external suites because the project has no qualified mapping from their tools, tasks, and outcome oracles to AstralDeep. It never falls back to sample rows or relabels them as real. AgentDojo 0.1.35 is pinned for evaluation research in `requirements-eval.txt`; its wheel digest is documented there. A supported external mapping needs a separate manifest, eligibility count, exact corpus digest, paired utility controls, and physical effect oracle before it can enter the required gate.

The local fixture report records the candidate commit, policy and component digests, case population, observed effects, control outcomes, audit references, and teardown. It is diagnostic local evidence until the exact candidate's protected CI jobs pass. Apple and Android device tests are outside this server fixture; web behavior needs separate live verification.
