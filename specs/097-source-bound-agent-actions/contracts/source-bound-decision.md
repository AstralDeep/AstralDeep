# Source-Bound Decision Contract

Internal host contract; no new wire frame or client action.

| Input | Validation |
|---|---|
| Actor and owner | Existing signed human request, current session, and delegated authority |
| Action | Canonical registered agent/tool plus reviewed effect declaration and alias |
| Material | Source-origin set, sensitivity, and exact argument/material digest |
| Target | Resolved destination or resource under current owner |
| Approval | Existing single-use `authorize_action` decision bound to the exact call |
| Audit | Hash-chained decision and physical outcome reference; no raw PHI, secrets, or source text |

Decision values are `allow_read`, `deny`, `review_required`, and `allow_exact_effect`. An unknown input, unavailable policy, missing classification, unavailable audit, or invalid approval produces `deny` with a stable diagnostic. An effect never defaults to allow.

The existing Card/Text/CodeBlock/Button render and `authorize_action` action carry the review request across web and native clients. The server owns visibility and authorization. This feature changes no Projection manifest, client parser, or primitive definition.
