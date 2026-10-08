# Research: Apple Controls and Web/Mobile Consistency

**Date**: 2026-10-05. Sources: local Deep `e9a47d8b`, its Projection pin `f7d6358`, sibling Projection `8d0b84d`, and the dated [live audit](audit.md).

## Decisions

| Evidence | Decision | Alternative rejected |
|---|---|---|
| Direct ROTE reproduction converts untyped form actions to empty text components on every native profile. | Preserve metadata descriptors, adapt typed children, and include descriptors in interactivity/action budgets. | Patching generic Submit labels locally leaves routing broken. |
| Apple selects/checklists discard defaults, labeled options and mapping conditions. Android/Windows drop object option labels. | Normalize string and value/label options; display labels, retain wire values and untouched defaults. | Client-specific provider lists create another product definition. |
| Apple applies theme only on appearance, ignores preview container fill, applies custom colors before success and forces dark controls. | Reduce accepted server theme state, repaint replacements, support bounded swatch styling and validated arbitrary colors. | General CSS parsing and optimistic false success violate scope/authority. |
| Android has preset-only colors and hides initial failures like Apple. | Add validated colors and initial connection/recovery state. | Treating initial failure as an empty account is misleading. |
| Drafts is the only ordinary Deep adapter missing components; decision handlers return no refresh. | Compose existing primitives from owner-scoped getters and refresh through the normal dispatcher. | New storage/endpoints or client-local policy duplicate authority. |
| Advanced has an existing advertised/fenced guidance path; live error text alone does not prove timeout. | Add native ingress/delivery and failure tests; repair the evidenced seam without bypassing fences. | Replacing the route with an unfenced surface weakens authority. |
| Dashboard uses shared content but native card/filter density and narrow layout differ. | Align design roles and adaptive layout, preserving prompt loading without submission. | A separate native dashboard violates the web reference. |
| Signed phone idle metadata requests refuse an expired registered JWT, and Retry repeats the refusal; normal app relaunch restores the same account without new SSO. REST refresh does not renew the already registered socket. | Renew Apple socket identity through existing token/custody flow and signal existing authentication recovery only for a proven expired current registration; preserve denial and no automatic write replay. | Generic retries against the same expired identity remain unusable; accepting expired identities or treating every denial as refreshable weakens authority. |
| After normal reconnection, the retained phone provider form saves and reports an advisory failure, but remains open. Registration clears server modal ownership; the new Save ticket alone does not restore it. Real native-ingress regressions reproduce missing close/result delivery. | Reclaim an empty ordinary modal owner only for the current host-authorized canonical action and socket/registration/request identity; keep close correlation and post-await delivery fences. | Relaxing close ownership or automatically reloading retained drafts can dismiss or overwrite newer work. |
| Phone Advanced initially times out; Retry visibly opens Private notes. Three deterministic regressions expose the hardcoded list retry and missing selection-read renewal. | Preserve the canonical Advanced read destination and recheck its current server declaration, with explicit refusal when authorization is retired. | Silently falling back to the notes list changes the user's requested workflow; replaying selection changes or writes broadens authority. |

## Qualification Notes

Apple tools/Xcode and Android SDK/JBR are available. Projection's Python 3.11 virtualenv includes pytest, coverage, Ruff and PySide6. Android uses the committed wrapper and `JAVA_HOME=/Applications/Android Studio.app/Contents/jbr/Contents/Home`. Docker initially had no running services. Real service availability and native simulator/emulator remain explicit checks.

Watch generic forms/colors retain declared phone/desktop handoffs; approved administrator/tour web-only entries stay server-bounded. No new protocol type, schema migration or dependency. Authentication remains real PKCE completed by the owner; operations must not send a chat query or alter the owner's secrets/data.

The final recovery choices are implemented in Deep 20f98a38 and Projection d3f2b8d. Current real-ingress tests retain authorization through handler/render/delivery races, and current loopback tests retain read destinations through credential renewal. Signed final iPhone/iPad live checks reproduce normal transport replacement, then verify completed provider close/reopen and explicit Advanced retry without private writes or queries. Mac screen lock and complete cross-client qualification remain evidence gaps, not implementation success claims; verification.md records exact scope.
