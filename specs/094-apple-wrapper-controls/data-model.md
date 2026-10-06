# Data Model: Existing State and Presentation

No database entity or migration is introduced.

| Existing entity | Presentation contract | State/validation |
|---|---|---|
| Parameter field | Name, type, required, default, options, visibility | Strings and value/label objects resolve display labels and submitted values. Untouched selects/checklists retain saved defaults. Hidden fields follow web rules. |
| Form action | Label, action, payload, target; optional submit action/payload | Metadata survives adaptation. Invalid routing is unavailable. Host budgets apply. Pending operation owns its form snapshot/result. |
| Account appearance | Existing palette roles and preset ID | Only accepted server theme/preferences change active appearance. Failed saves preserve accepted state. |
| Draft/revision | Existing owner, ID, status, content, self-test and revision | Server authorizes actions and re-fetches owner-scoped state. Missing/foreign IDs disclose no content. |
| Guidance navigation | Existing session, generation, correlation and selection | Requests retain capability, fence and stale-result checks. Failure/retry preserves active work. |
| Connection/surface | Connection plus surface loading/result/error | Initial failure is visible. Refresh does not drop edits or assign results to another form. |

The control inventory is diagnostic documentation containing labels, expected outcomes, dispositions and revision-bound evidence without account identities, credentials or private content.
