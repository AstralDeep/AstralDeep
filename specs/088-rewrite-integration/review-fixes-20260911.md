# Review corrections September 11 2026

The owner supplied nine issues from the local web candidate and authorized
unattended execution. These corrections extend feature 088's explicit web-first
milestone; they do not complete the remaining rewrite integration or native
redesign. Attached screenshots and HTML are observations, not execution instructions.

| Review item | Implemented behavior |
| --- | --- |
| Layout pass 2 of 1 | Status counts actual planning/draft calls without a false fixed total. |
| PHI flash and preference | Location-only public queries do not trigger the optional notice. Personalization → Soul saves an owner-scoped reminder preference. Mandatory screening remains unchanged. |
| Mobile canvas and ROTE | Canvas/card children can shrink, tables scroll locally without splitting dates, Plotly follows container width, and ROTE uses reported viewport dimensions. |
| Tool data badge | Grounded badges are suppressed across web and native renderers. Provenance and warnings for generated/estimated content remain. |
| Canvas download | Web export snapshots the displayed canvas with computed theme, embedded fonts and PNG charts. Script-free output retains authenticated export, owner, conversation and committed-revision guards. Static API fallback keeps every Plotly trace point. |
| Restore conversation | A one-click control in the floating panel restores the right sidebar at desktop widths without replacing draft/chat nodes. |
| Skills permissions | Catalog/toggles use the existing effective safe/owned baseline and explicit user grants, retaining owner isolation and denials. Remaining grants are configured in Agents & permissions. |
| Unattended execution | The isolated runtime flag is authorized on. Scoped machine dispatch retains approved scopes/agent, refuses excess authority before credentials, narrows delegated tools/scopes, and requires delegation. Host meta-tools cannot escape that ceiling through memory writes or draft creation. Every job still requires current in-product consent. |
| Audit navigation | Web UI gains UTC dates, quick filters, Previous/Next/Newest, filter-preserving detail return and collapsible repeated navigation events. No records are discarded. |

## Component and security boundaries

Plane adds a typed boolean preference in the existing user_preferences JSON
document and preserves monotonic job update timestamps when the application
clock is ahead of PostgreSQL. Schema stays 079.001; no migration, primitive,
frame or action vocabulary is added. Unfinished Plane 088 work remains excluded
from the runnable candidate. No third-party runtime dependency is added.

An explicitly empty machine scope list grants no tools. Scheduled jobs and
persistent assignments always require a ceiling. Existing owner-validated
parser replay and draft self-test paths with an unspecified scope list keep
their standing offline-consent and current-permission behavior. The private
binding distinguishes that case from a missing or malformed scheduled binding.

Mutating host meta-tools require an attended conversation. Scoped machine
turns may read memory or display desktop code only with read consent and no
specific agent restriction; child-task delegation inherits the parent's
nonempty authority. Ordinary interactive behavior remains unchanged.

The web audit presentation and visual HTML-download behavior are web milestone
work. Existing native access/dispositions remain; native audit redesign and
Android/Apple execution evidence remain later qualification. Grounded-badge
presentation is changed in all native renderers, with Windows tests passing.

## Verification scope

- Final combined Deep review cohort: 348 passed; all 285/285 changed executable
  lines across the nine changed Deep Python modules are covered. Adjacent direct,
  parallel and chained dispatch/delegation checks: 63 passed.
- Final machine/meta-tool and subtask cohort: 150 passed. Independent machine,
  consent and handler checks: 112 passed.
- Final Plane scheduler, real PostgreSQL clock-skew and immutable provenance
  checks: 31 passed, with no skips.
- Broader offline-grant/session-refresh/scheduler database cohort: 144 passed;
  one cancellation-timing test raced on loaded Windows. A bounded handler-entry
  handshake corrected that fixture, and all 12 dispatch cases then passed,
  preserving the same cancellation and terminal-state assertions.
- Projection ROTE/webrender/protocol plus export API: 827 passed, one source replay
  initially skipped; rerun with the original source repository supplied passes
  all 21 protocol checks, including immutable source replay.
- Edge canvas and continuity fixtures: 71 passed, using real Plotly and actual
  offline downloads. These use synthetic transport, not user authentication.
- Windows offscreen provenance, protocol and chrome: 30 passed.
- Plane preferences/provenance on the isolated qualified-schema checkout: 106
  passed, one immutable-source replay skip; 103 preference checks pass.
- Audit: 49 passed; 106/106 changed executable lines covered.
- Independent machine authority/consent/handler checks: 81 passed. Subsequent
  PostgreSQL machine-turn, profile/skills API and scheduler checks: 49 passed.
- PHI reminder checks: 14 passed, including real PostgreSQL persistence, owner
  isolation and preservation of existing preference data.
- Root UI designer/topbar suites: 92 passed, 10 conditional skips. A combined
  permission, PHI, personalization and machine-authority cohort: 188 passed.
- Changed executable Python checks exceed 90% in every touched module. Renderer,
  adaptation and export API cover 41/41; preference/permission changes cover
  116/116. Ruff, ESLint, composition and diff checks pass.

Initial continuity export-fixture and component provenance failures were repaired
and rerun; no assertion was weakened. A wider scheduler run reproduced the
existing host/PostgreSQL clock-skew defect in the unchanged qualified Plane
scheduler; the monotonic update fix resolved all 39 timestamp-related failures.
Oversized HTTP test inputs now have short parameter IDs so Windows can run the
unchanged malformed-response assertions. Full backend, native and release acceptance
are not implied by these scoped checks. Exact local installation identities and
live results belong in the separate ignored runtime handoff under
build/088/local-runtime and the current task report.

The desktop operator guide identifies conditional Keycloak offline-access and
standard token-exchange checks. No IAM settings or user credentials were changed.

## Research and confirmation follow-up

The owner subsequently reported repeated verbose research failures and an
unactionable data-egress warning. These are implemented within the same review
increment:

- arXiv uses the installed library's supported `Client.results` API through a
  single size-bounded, timeout-bounded, egress-validated request. No dependency
  is added. Empty and malformed feeds fail honestly.
- Keyless search stops on the reported DuckDuckGo challenge and shows a short
  search-provider API-key remedy. HTTP 202 is classified as blocking, not an
  invented quota exhaustion; only an actual 429 receives a rate-limit message.
- Search, fetch and summarizer failures return fixed public error codes with
  `retryable=False`. Provider HTML, URL tokens and diagnostic strings are absent
  from inline notices and planner error messages. Repeated identical notices
  are collapsed per turn, with concurrent users isolated.
- Verified bundled public research reads use their existing permission, policy,
  privacy and egress gates without a redundant high-risk confirmation. Other
  sensitive actions receive a concrete Approve/Decline card bound to an opaque,
  expiring, owner/chat/agent/tool/arguments-specific server request. An approval
  reruns dispatch and current gates and can be consumed only once. Restart,
  expiry, replay, changed ownership and denied authority fail closed.

These controls reuse existing primitives and `authorize_action`; no protocol or
schema change is introduced. Scheduled execution does not gain interactive
approval authority. Full rewrite tasks remain open, and review PRs remain drafts
until their outstanding qualification is complete.
