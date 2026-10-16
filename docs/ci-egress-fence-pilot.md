# Fence egress pilot

The pilot protects the existing `lint` job on an ephemeral GitHub-hosted Ubuntu24.04 x64 runner. It retains the job's ten-minute limit, ordinary candidate checkout, pinned Python setup with pip cache, hash-locked tooling install and Ruff command. Other jobs and product configuration are outside its scope.

The [pilot verifier](../scripts/ci_fence_egress_pilot.py) checks the actual workflow. An absent pilot is a failure; a generated patch or an independent packet-policy model cannot qualify it. Until the separately reviewed helper companion is genuinely merged and its two file digests are bound, the verifier returns `missing_trust`. The neutral companion's lifecycle evidence does not prove Fence enforcement.

## Provenance and trust boundary

Fence is pinned to the published v0.10.5 production action bundle `openai/fence@abc5790eac3445fe3854eea6b506aa7d31bed67c`, rather than upstream's source main. Its [release documentation](https://github.com/openai/fence/blob/abc5790eac3445fe3854eea6b506aa7d31bed67c/README.md) and [allowlist contract](https://github.com/openai/fence/blob/abc5790eac3445fe3854eea6b506aa7d31bed67c/docs/allowlist.md) define the reviewed behavior. Source review is separate from supported-runner acceptance.

`ReviewedHelpers` binds one actual merged AstralDeep default-branch commit and SHA256 identities for `scripts/ci_network_fixture.py` and `scripts/ci_fence_fixture_verifier.py`. An unmerged candidate ref, a placeholder digest or a candidate-controlled assertion of trust cannot authorize the pre-Fence launcher. The [owning supply-chain gate](../backend/tests/test_python_ci_supply_chain_060.py) requires actual workflow conformance, verifies that the helper commit is already an ancestor of `origin/main`, checks both Git-object digests and compares the candidate's helper files with those exact bytes.

The lint job follows one fixed order:

1. A fixed system-Python launcher fetches only the two immutable helper URLs over verified HTTPS. A no-redirect opener refuses any redirect before contacting its target. Inside root, a sanitized environment and Python isolated mode bound and hash both buffers before executing the verified peer buffer. Its fixed start operation creates and baselines the owned fixture. The same two buffers are written exclusively as root-owned read-only files inside that fresh fixture directory. A copy failure requires fixed close plus removal and supervisor-exit attestation before returning an error.
2. The pinned production Fence action activates explicit block mode. The job grants exactly `contents: read`; it gains no secret, OIDC permission, protected dispatch surface or credential. Sudo/container preservation and audit enforcement are forbidden.
3. The unprivileged trusted verifier opens only the fixed root/fixture directories and helper files, using no-follow descriptors and root-ownership/nonwritability checks. It bounds and rehashes both buffers before executing either. The verified peer module is passed in memory; no candidate import fills this boundary. Fence and verifier share the explicit invocation identity `astral-lint-${{ github.run_id }}-${{ github.run_attempt }}`.
4. An `always()` close step loads both verified buffers before removing the fixture directory, requests only that peer's fixed close and requires `resources_removed` and `supervisor_exited`. The supervisor removes its own child, namespace, veth and directory by recorded kernel identities. Failed or uncertain cleanup fails the job and prevents candidate checkout. Runner disposal is containment, not a successful cleanup receipt.
5. Only after successful verification and cleanup does the normal candidate checkout, Python setup/cache, hash-locked install and Ruff execution begin. No privileged fixture supervisor remains for candidate code.

The bootstrap uses the runner's system Python before dependencies exist; its source remains Python3.11 compatible. Helper identity updates require a separately reviewed and qualified default-branch merge, followed by a coordinated digest/workflow update and fresh pilot qualification.

## Reviewed egress

The action's explicit allowlist is exactly:

| Entry | Required operation |
| --- | --- |
| `github.com:443` | Candidate Git transport and toolchain release resolution |
| `codeload.github.com:443` | Pinned action/repository bundle retrieval |
| `raw.githubusercontent.com:443` | Python toolchain versions manifest |
| `objects.githubusercontent.com:443` | Python toolchain artifacts |
| `release-assets.githubusercontent.com:443` | Python toolchain release artifacts |
| `pypi.org:443` | Hash-locked lint tooling index |
| `files.pythonhosted.org:443` | Hash-locked lint package artifacts |
| `ip 192.0.2.2 tcp 18443` | Owned peer health and authenticated positive control |

`allow_github_artifacts: true` is explicit because the unchanged job uses GitHub's pip cache. Upstream documents this storage opt-in as an additional way later steps can send data off the runner. It is not an arbitrary storage-domain allowlist and is not enabled silently. The ordinary cache/setup/install/lint results must establish compatibility in the actual protected job.

Fence is not a complete sandbox. Its reviewed built-in policy retains required GitHub Actions/job-reporting and Azure connections. Azure Instance Metadata Service remains reachable at `169.254.169.254:80`; Azure WireServer access is restricted to root-owned host processes. Loopback is also permitted, so failure to reach a loopback listener cannot prove Fence denial. The pilot neither hides these exceptions nor expands them through `unsafe_preserve`.

## Controlled acceptance

The peer occupies only its owned documentation-prefix namespace/veth topology on `192.0.2.0/30`. It has no default route, does not change host default routes, system DNS, trust store or firewall rules, and drops peer groups/UID/GID/capabilities after binding. Each invocation uses a new private test CA and nonce. No PHI, user credential or production service is involved.

Before Fence, every synthetic TLS/DNS endpoint must answer authentically. The neutral qualification runs the same denial assertions without a firewall and requires failure specifically because an excluded endpoint answers. After activation, fresh connection tuples must distinguish the probes from baseline established flows. Allowed TLS18443 remains healthy around every negative. Excluded TLS19443, the `pypi.org` hostname forced to the peer's excluded443 address and direct UDP53 must be denied with matching new actual destination/protocol/port evidence and unchanged live peer counters. Generic timeout, TLS or routing failure alone is insufficient.

The trusted verifier also checks actual bounded ready/report/DNS/unit state: block mode, protected readiness, verified network, healthy resident, disabled sudo/containers and no critical findings. It reuses the pinned upstream validators through the genuine runner Node24 executable. It requires the actual local DNS mediator's REFUSED response for an outside-policy `.invalid` query. A bounded private copy of the protected action runtime, with only its copied agent checksum corrupted, must reach the native checksum-mismatch rejection; unrelated syntax/module/platform errors cannot pass. The live registered runtime remains intact. This is startup integrity rejection, not a resident-drift injection claim.

Required evidence includes the exact candidate/action/helper identities, genuine baseline and post-Fence rejection/health/integrity results, successful fixture removal/supervisor exit, unchanged normal job operations and the healthy action post-hook report. Deterministic unit documents and source conformance remain diagnostic; neither authorizes merge without the actual supported-runner pilot and complete owning gates.

## Failure and rollback

Missing or mismatched trust, unsupported runtime, setup conflict, absent/malformed/stale evidence, unexpected successful excluded response, failed ordinary operation or incomplete cleanup keeps the pilot failed. Diagnose the precise failure rather than adding retries, arbitrary endpoints, timeout increases, audit mode, sudo/container preservation or absent-plus-patch acceptance.

A reviewed rollback must revert the pilot activation and its coordinated strict-conformance bridge together while preserving the original lint operations and every unrelated gate. It must record the resulting unqualified state and leave the egress-hardening issue open. No generated patch, policy waiver or fabricated runtime receipt is a rollback mechanism.
