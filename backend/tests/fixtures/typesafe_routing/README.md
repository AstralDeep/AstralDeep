# TypeSafe routing fixture corpus (feature 089, T014)

Three files, three jobs.

| File | What it is for |
|---|---|
| `catalog.json` | The agents and tools the prompts are labeled against. Real bundled agent ids and tool names; abridged descriptions. |
| `prompts.json` | The routing corpus. Labeled prompts with accepted tools and an expected tier. Drives T028's accuracy calibration (SC-005). |
| `benign_reference.json` | The false-positive corpus for the security screen. Drives T036's calibration (SC-006). |

## What is deliberately not in here

No credential, key prefix or key-shaped string. No PHI, and no real patient,
user or institutional record. Every prompt is written for this corpus.

## How the labels work

`accepted_tools` is a **set**. Several prompts have more than one defensible
target, and scoring a router against a single arbitrary pick would measure
agreement with whoever wrote the label rather than usefulness to the user. A
decision counts as correct when its tool is in the set.

`expected_tier` is what the tiering rules in
`orchestrator/typesafe_routing/decision.py` should conclude. It is the thing
T028 calibrates, so it is allowed to disagree with a recorded model answer: if
the model is confident about an ambiguous prompt, the *thresholds* are wrong,
and that is the finding.

Some prompts are labeled `low` on purpose. `ambiguous-status` has no right
answer without an active agent, and the correct behavior is to leave round one
alone. Treating those as failures would push calibration toward a router that
guesses.

## Categories in `prompts.json`

| Category | What it exercises |
|---|---|
| `single_tool` | The high tier: one confident agent, one confident tool. |
| `multi_tool` | The medium tier: confident agent, spread across its tools. |
| `conversational_followup` | `no_tool_needed`, including follow-ups that only make sense with history. |
| `ambiguous` | That under-determined prompts land in the low tier rather than a guess. |
| `name_collision` | Two agents exposing the same unqualified verb (`collision_catalog`). |
| `disabled_agent` | An agent the user disabled is never offered. |
| `deselected_tool` | A tool the user deselected is never offered, and the router works with the rest. |
| `large_catalog` | A 60x8 synthetic catalog, which is what exercises the truncation bounds. |

## Optional per-case keys

| Key | Meaning |
|---|---|
| `history` | Prior turns to include, for follow-ups that are meaningless without them. |
| `catalog` | Use a named alternate catalog from `catalog.json` instead of the default. |
| `excluded_agents` / `excluded_tools` | Remove these from the eligible set before building the request. |
| `synthetic_catalog` | Pad the catalog to `{agents, tools_per_agent}` before appending the real target. |
| `note` | Why this case is labeled the way it is. Read it before "fixing" a label. |

## Security benchmark cases

The adversarial half of the security calibration comes from
`backend/security_benchmark/`, not from this directory. T036 runs its existing
suites through the fake with recorded answers and pairs the result with
`benign_reference.json`: the benchmark gives the true-positive rate, the benign
corpus gives the false-positive rate, and the refuse tier is enabled only if
both land inside SC-006.
