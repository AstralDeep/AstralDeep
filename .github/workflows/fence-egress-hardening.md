# Fence Egress Hardening Pilot – AstralDeep

## Purpose

Qualify OpenAI Fence in a single, existing pure lint/SDK CI job on an ephemeral
GitHub-hosted x64 Ubuntu runner. The goal is to narrow dependency/untrusted-code
egress exposure while retaining all required AstralDeep checks.

## Reviewed baseline

- Fence security model: https://github.com/openai/fence/blob/567ebd3787467b0ea8eaddc00f5ad9247ace585b/docs/security.md
- Pinned release: v0.10.5 bundle SHA `abc5790eac3445fe3854eea6b506aa7d31bed67c`
- AstralDeep source review baseline: `f89ba2ae9c0e8decd271544e05a93f6c8a202a66` (2026-10-06)

## Approved egress endpoints

| Endpoint | Purpose |
|---|---|
| `https://pypi.org` | Package index (GET) |
| `https://files.pythonhosted.org` | Wheel/source distributions (GET) |
| `https://github.com` | Source repo access (GET) |
| `https://api.github.com` | API calls (GET, POST) |
| `https://objects.githubusercontent.com` | Asset objects (GET) |
| `https://release-assets.githubusercontent.com` | Release assets (GET) |
| `https://registry.uv.dev` | uv registry (GET) |
| `https://cdn.jsdelivr.net` | JS/CDN deps (GET) |

## Remaining GitHub/Azure metadata exceptions

- GitHub Actions runner metadata (built-in, out of Fence scope).
- Azure Pipelines agent metadata (not used in this pilot).

## Provenance

- Fence version: v0.10.5
- Bundle SHA: `abc5790eac3445fe3854eea6b506aa7d31bed67c`
- Source review accessed: 2026-10-06

## Rollback

Revert this PR and remove the `fence-egress-hardening` job from `.github/workflows/ci.yml`.

## Test classification

- **Source review**: immutable, accessed 2026-10-06
- **Tests**: deterministic/offline fixtures (`tests/sdk_contract/`)
- **Live qualification**: disposable-runner diagnostic (bounded, ≤30 min)
- **Deployment**: merged to `main`
<</file>>>
