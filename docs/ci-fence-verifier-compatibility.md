# Dormant Fence verifier compatibility

`scripts/ci_fence_fixture_verifier_v2.py` is an inactive, read-only compatibility
adapter. The active lint workflow, old verifier, peer, reviewed helper commit and
digests, source-conformance policy, and active inventory remain unchanged. This
companion neither installs nor activates Fence and creates no new authority path.
A passing source or parser check does not authorize activation or merge.

The new adapter accepts only two complete, published action identities:

| Release | Complete action SHA | Reviewed source SHA |
| --- | --- | --- |
| v0.10.5 | `abc5790eac3445fe3854eea6b506aa7d31bed67c` | `567ebd3787467b0ea8eaddc00f5ad9247ace585b` |
| v0.12.5 | `f9751ca5d767dc0199e88733c2b286bd4800a008` | `424c1ac6cc2f844986f38defc6d43150796f35f5` |

The future reviewed caller must explicitly provide the complete action SHA to
`verify(..., action_pin=...)`, or use the required `--action-pin` argument. Version
labels, source SHAs, unknown pins, environment variables and evidence documents
cannot select a profile. Immutable profile tables bind all six protected runtime
file hashes; a different complete bundle or a mixed tuple is refused. The
[separate dormant inventory](../tooling/ci-peer/inputs-v2.json) documents those
identities and is never a runtime configuration input.

Profile selection is scoped to one call. Nested profile changes are refused;
independent callers retain independent selections. A bound evidence state cannot
move to another profile, and scope resets after success or failure. The module
can be executed from a previously authenticated in-memory buffer without importing
candidate code or a package-local policy module.

Both releases retain runtime evidence schema 5, policy hash schema 9 and the
hosted bootstrap v5 profile. Their native contracts differ: v0.12.5 requires the
`hostname_refresh` worker in block mode and the
`not_configured_standard_block` Docker DNS routing disposition. Its native
readiness validator supports historical committed readiness during later reads.
The adapter uses setup validation initially, and invokes the reviewed historical
phase on later v0.12.5 reads; v0.10.5 retains its original later-read behavior.
Every read still requires fresh healthy current report/DNS/unit evidence,
unchanged startup bytes, identity, mode, resident and worker binding, and valid
counter transitions. Ready markers are no-follow, root-owned, exactly mode0644,
UTF-8, and bounded to 64 KiB. Report and DNS bounds are unchanged.

The existing controlled-denial protocol is retained: authentic peer baseline,
fresh connection tuples, live allowed TLS18443 around each negative probe,
excluded TLS19443, forced-address hostname TCP443 and direct UDP53 denials,
matching new rejection evidence with counter growth, unchanged peer counters,
local DNS REFUSED, copied-agent checksum rejection and fixed cleanup semantics.
Unknown native diagnostics stay bounded and opaque. A platform, syntax or module
failure never counts as native checksum rejection. No allowlist, resolver, runner
label, timeout, retry allowance, sudo or container policy changes are introduced.

The adapter uses Python3.11-compatible standard library code and the genuine
runner Node24. Required tests are offline and use deterministic bounded fixtures;
no provider or release service is contacted by those tests. Native parser checks
against exact released libraries are diagnostic compatibility evidence, and are
separate from actual packet-policy qualification.

Future activation requires a separately reviewed, genuinely merged and qualified
helper commit; exact Git-object and file hashes; the existing protected loader;
a coordinated source-conformance/workflow update; and fresh actual supported
hosted-runner evidence. This companion's first ordinary CI must pass the existing
old-Fence workflow on a genuinely supported layout. If setup fails, hold the
candidate. Do not retry a whole job for a favorable runner, change its label or
DNS, substitute a diagnostic job, waive trust, or bypass a required check.
