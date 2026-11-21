# User Agent Constitution (baked runtime asset)

`agent_constitution.md` here is the **runtime** copy the Analyze gate reads
(`orchestrator/agent_constitution.py`). It MUST be **byte-identical** to the
authoritative source at `specs/057-byo-client-agents/agent-constitution.md`.

Why a copy: `Dockerfile` bakes only `backend/` into the image; `specs/` is not
present at runtime. This mirrors the feature-040 skill-pack
precedent (authored markdown baked in, read `__file__`-relative). Do NOT hand-copy
the constitution text into a Python literal (see `mcp_tools_dev.py` for how that
drifts).

A CI test (`backend/tests/test_agent_constitution_identity.py`) asserts the two
copies are identical. When you edit the constitution, edit the `specs/` source and
re-copy here (or vice versa) so they stay in lockstep and bump the `Version:`
header per its Governance section.
