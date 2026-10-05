# FHIR Clinical Data agent

This guide covers the fail-closed `FF_FHIR` capability. The in-process agent `fhir-1`
("FHIR Clinical Data") reads one operator-configured HL7 FHIR R5 server and answers with
clinical dashboards built from astralprims: an ICU census, patient overviews, vital sign
trends, laboratory results, medication reviews, timelines, free-form record searches and
a live activity feed. It never writes clinical data.

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

The agent then appears in the Agent Directory with four example prompts. Because it is
read-only it is seeded as a safe public agent while the flag is on, so users do not have
to enable its tools one by one.

## Tools

| Tool | Scope | What it returns |
|---|---|---|
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

## Live streams

The two streaming tools keep one card up to date for a bounded time (the feed up to 10
minutes, a patient's vital signs up to 15). The census card offers **Watch live feed**, and
the patient overview and vital sign cards offer **Stream live vitals**. Those buttons send
`stream_subscribe` straight to the orchestrator, so no model call is involved, and a
finished vitals stream offers **Stream again**.

The web client applies committed canvas updates but ignores stream frames outside an
in-flight turn, so a stream is only visible when the orchestrator saves its progress to
the canvas. That is the fail-closed `FF_STREAM_PROGRESS` flag: with it on, a streaming
tool that declares `persist_progress_s` has its latest content saved at most that often
(15 seconds for these two) and only when it changed. With the flag off the agent hides the
stream buttons, and a stream called through chat shows its first card only.

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

## Rollback

Set `FF_FHIR=false` (or remove it) and recreate the container. Nothing is stored by the
agent, so there is no data to migrate or remove.
