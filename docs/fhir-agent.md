# FHIR Clinical Data agent

This guide covers the fail-closed `FF_FHIR` capability. The in-process agent `fhir-1`
("FHIR Clinical Data") reads one operator-configured HL7 FHIR R5 server and answers with
clinical dashboards built from astralprims: an ICU census, patient overviews, vital sign
trends, laboratory results, medication reviews, timelines, free-form record searches and
a live activity feed. It also provides separate patient measurement and synthetic population
query interfaces. It never writes clinical data.

With the flag off (the default) the agent is not registered in-process, not spawned as a
subprocess and not seeded with permissions; behavior is byte-identical to a build without
it.

## Enable it

1. Make the FHIR server reachable from the orchestrator container. For the eICU demo
   replay service, attach the orchestrator to that service's private Docker network.
2. Set the boot values in the deployment's Compose environment without printing them:

   ```text
   FF_FHIR=true
   FHIR_BASE_URL=http://eicu-fhir:8080/fhir
   FHIR_ACCESS_TOKEN=<the server's bearer token>
   ```

3. Recreate the container. Flags are read once at import, so a restart of an unchanged
   container does not pick them up.

   ```bash
   docker compose up -d --force-recreate astraldeep
   docker compose exec -T astraldeep python -c 'from shared.feature_flags import flags; print("fhir enabled:", flags.is_enabled("fhir"))'
   ```

The agent then appears in the Agent Directory with six example prompts. Because it is
read-only it is seeded as a safe public agent while the flag is on, so users do not have
to enable its tools one by one.

## Tools

| Tool | Scope | What it returns |
|---|---|---|
| `patient_measurements` | `tools:read` | Latest usable A1C, glucose or complete blood-pressure panel for one patient, including collection time, source observation and provenance |
| `aggregate_a1c` | `tools:search` | Synthetic population A1C by county through SQL on FHIR, with cohort and measured-patient counts and calculation provenance |
| `icu_census` | `tools:read` | Current ICU census, census by unit type, admissions per hour, most recently admitted patients with latest vitals |
| `patient_overview` | `tools:read` | Demographics, stay, latest vitals, APACHE risk, problems, allergies, key labs, active orders |
| `vital_sign_trends` | `tools:read` | Heart rate, respiratory rate, SpO2, blood pressure and temperature over time |
| `laboratory_results` | `tools:read` | Latest result per test with reference interval, flag and change, plus history charts |
| `medication_review` | `tools:read` | Orders by status, charted infusion rates, medication before admission |
| `patient_timeline` | `tools:read` | Admissions, diagnoses, treatments, orders and critical results in time order |
| `query_fhir_records` | `tools:search` | A read-only FHIR search over one of twelve resource types, as a table |
| `fhir_source_status` | `tools:read` | FHIR version, resource types and search parameters, replay clock and dataset |
| `watch_icu_activity` | `tools:read` | A push-streaming card fed by FHIR R5 topic subscriptions |
| `stream_patient_vitals` | `tools:read` | A push-streaming card with one patient's latest vital signs and two-hour trend |

Each tool returns one top-level card with a stable id, so calling it again for the same
patient updates the card in place. The model receives a compact summary instead of the
raw resources.

Long windows stay bounded. Trends read at most the 6,000 most recent readings and the
medication review at most the 3,000 most recent charted doses; when the server holds more,
the card says how many are shown. Charts are thinned to a few hundred points per series in
a way that keeps each interval's highest and lowest value, so brief spikes stay visible.

## Separate reference interfaces

For patient questions, search `Patient` records using `query_fhir_records` with a `name`
filter and select the correct FHIR id from the returned names. `patient_measurements`
accepts that id or an exact MyHealthSafe `DEMO-1001` style identifier. Identifier lookup
is scoped to `urn:myhealthsafe:demo`; multiple matches are refused. It reads at most 100
observations using standard search, without depending on `$lastn` or a replay clock.
Only final, amended or corrected results with valid units, numbers and collection times
are used. New measurement values require the UCUM system and an accepted UCUM code.
A1C uses LOINC `4548-4` or `17856-6` in percent; glucose uses `2345-7`, `2339-0` or
`41653-7` and preserves `mg/dL` or `mmol/L`;
blood pressure requires systolic and diastolic values from the same panel. Source records
tagged `urn:astraldeep:sandbox` / `synthetic` are explicitly labeled synthetic; records
tagged `public-deidentified` are labeled public de-identified, and missing or mixed tags
are reported without inventing provenance.
Effective dates are preferred; if only `issued` is present the returned date is explicitly
labeled as the reporting date field. Timezone-free dates, foreign absolute patient
references and duplicate blood-pressure components are excluded. Versioned subject
references are currently excluded rather than inferred.

For population questions, `aggregate_a1c` calls the same configured service's
`GET /$aggregate-a1c` operation. Inputs are an optional two-letter state code, up to 20
county names and an optional boolean pregnancy filter. No tool accepts SQL, a service
URL or credentials. The service runs SQL over official SQL-on-FHIR ViewDefinitions and
returns a FHIR `Parameters` resource whose single `result` parameter contains a JSON
summary. The agent validates the query identity, exact scope, synthetic flag, source,
definition, timestamp and at most 100 county rows before displaying results. The
calculation uses each patient's latest valid percent A1C and latest explicit pregnancy
status, excluding missing and incompatible values. Synthetic county results do not
describe actual Kentucky residents or KHIE data.

The patient card and population card remain separate, with distinct titles and source
labels. These tools add no durable AstralDeep state, schema change or new dependency.

## Live streams

The two streaming tools keep one card up to date for a bounded time (the feed up to 10
minutes, a patient's vital signs up to 15). The census card offers **Watch live feed**, and
the patient overview and vital sign cards offer **Stream live vitals**. Those buttons send
`stream_subscribe` straight to the orchestrator, so no model call is involved, and a
finished vitals stream offers **Stream again**.

The web client applies committed canvas updates but ignores stream frames outside an
in-flight turn, so a stream is only visible when the orchestrator saves its progress to
the canvas. That is the fail-closed `FF_STREAM_PROGRESS` flag: with it on, a streaming
tool that declares `persist_progress_s` and a duration argument has its latest content
saved at most that often (15 seconds for these two) and only when it changed. With the
flag off the agent hides the stream buttons, and a stream called through chat shows its
first card only.

A stream runs for the duration it was asked for, counted from its first start. Both tools
declare `minutes` as their duration argument, so the orchestrator holds the deadline and
the tool does not have to. Leaving the chat or losing the connection pauses the stream,
and coming back resumes it by running the tool again, but the deadline does not move. A
stream whose deadline passed while it was paused is ended instead of resumed, and a run
still going 30 seconds after the deadline is ended by the orchestrator. Asking again after
that starts a new stream. A stream the orchestrator ends keeps its last live card, because
the tool never sent its closing one.

```text
FF_TOOL_STREAMING=true
FF_STREAM_ARTIFACTS=true
FF_STREAM_PROGRESS=true
```

The replay feed advances once a minute, so expect about one update a minute. Each update
re-renders the canvas, and the current web client returns to the top of the canvas when it
does. The other cards' **Refresh** buttons re-run that card's own tool in place through
`component_action`, also without a model call.

## Security posture

- **Egress.** Every request goes through `shared/external_http.py`. Only the host named in
  `FHIR_BASE_URL` is passed as an allowed private host, and only for the agent's own calls;
  `EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS` is not involved and no other caller is widened.
  Paging links that point at any other origin are refused rather than followed.
- **Credentials.** The token is operator configuration read from the environment. It is
  sent as a bearer header, never placed in a URL, a tool result or a log line.
- **Untrusted data.** `fhir-1` is registered as an untrusted taint source, so text from the
  feed cannot be laundered into a write or egress tool.
- **Read-only.** No tool name matches a sink pattern. The only non-read requests the agent
  makes are creating and deleting its own short-lived `Subscription` resources for the
  live feed; it deletes them when the stream ends or is abandoned. The vital sign stream
  only reads.
- **Failures.** Users see one of seven fixed messages (`FHIR_NOT_CONFIGURED`,
  `FHIR_AUTH_FAILED`, `FHIR_BLOCKED`, `FHIR_UNAVAILABLE`, `FHIR_NOT_FOUND`,
  `FHIR_BAD_REQUEST`, `FHIR_INVALID_RESPONSE`); server text is never shown.

Vital sign colours and laboratory flags come from fixed display thresholds and typical
adult reference intervals in `backend/agents/fhir/clinical.py`. They are a presentation
aid and every card says so; they are not clinical decision support.

## What the server must provide

A FHIR R5 server with `Patient`, `Encounter`, `Observation` and the `Observation/$lastn`
operation covers the census, overview, trend and laboratory tools. The medication,
timeline and risk views use `Condition`, `Procedure`, `AllergyIntolerance`,
`MedicationRequest`, `MedicationAdministration`, `MedicationStatement` and
`RiskAssessment` when present. The live feed needs R5 topic subscriptions with the
`$events` operation and topics whose ids are `encounter` and `observation`. A
`$replay-status` operation is optional; without it the agent uses the orchestrator's clock.
The reference patient measurements need only standard Patient and Observation reads and
searches. Population A1C additionally needs the bounded `$aggregate-a1c` adapter described
above. The ICU subscription tool returns an explicit failure when the replacement server
does not provide its required topics; a conventional reference server is not a replay feed.

## Rollback

Set `FF_FHIR=false` (or remove it) and recreate the container. Nothing is stored by the
agent, so there is no data to migrate or remove.
