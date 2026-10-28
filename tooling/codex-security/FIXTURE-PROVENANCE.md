# Frozen upstream mock evidence

Retrieved 2026-10-28 from official repository
https://github.com/openai/codex-security at
`f894f1ff64b1a332001f48593c1622b85701f9f3` (detached checkout).
The actual SDK package reports 0.2.0; its source plugin reports 0.1.95.

The TypeScript SDK was installed with pnpm 11.25.0 using
`pnpm install --frozen-lockfile --ignore-scripts`, then built using `pnpm run build`.
The lockfile SHA-256 is
`8139eb7ffa7e1bca07f9b5a5895409d6c8d201e1427cf1443d6b57bf534800f6`.
The source archive SHA-256 is
`4b31e070605bec72aa82b661f21bea245ad0bee84fee7544c884960516a324bf`.

Frozen output was produced by the actual upstream CLI against an **empty synthetic
directory**, with a freshly cleared environment and private home/state/output:

```text
node sdk/typescript/bin/codex-security.mjs scan <empty-synthetic-directory>
  --mock --headless --format json --output-dir <private-output>
  --plugin-path <exact-source>/plugins/codex-security
```

All twelve findings are upstream's fictional examples. No model, source-code
execution, credentials, provider call, security analysis or vulnerability claim
is involved. The four byte-preserved fixtures are the actual `scan-manifest.json`,
`findings.json`, `coverage.json`, and `exports/results.sarif`, renamed with an
`upstream-` prefix. Their shared scan identity is
`2bf35f0a-ac7a-4863-8d28-512ee71827bf`. Tests that mutate a fixture explicitly
reseal its findings/coverage hash only when testing a downstream status branch.

A separate build of `Runtime.Dockerfile` and the real CLI's `--mock` command also
completed in a Linux container with `--network none` and a read-only root. The
source plugin supports this offline profile without the universal native payloads
required by upstream's npm-pack pipeline. Building the universal plugin was
attempted separately and correctly failed on missing native prebuilt artifacts;
this workflow does not silently substitute that packaging surface.

The actual Bubblewrap 0.8.0 probe failed with:

```text
bwrap: No permissions to create new namespace, likely because the kernel does not allow non-privileged user namespaces.
```

That is unavailable containment evidence. No AppArmor, namespace, seccomp,
privilege or Docker configuration was weakened. Actual sandbox/canary
qualification must be collected on a supported host before containment is claimed.
