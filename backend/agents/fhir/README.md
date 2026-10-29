# Clinical note extraction pilot

`extract_note_fields` is default off. It requires the canonical in-process FHIR
registration and `clinical_note_extract`, plus the existing enabled context
accounting service (`observation_packing` or `safe_compaction`). Enabling only the
clinical flag returns `EXTRACT_CONTEXT_UNAVAILABLE`; the tool does not create a
second service or model authority. Standalone/network extraction returns
`EXTRACT_HOST_UNAVAILABLE`. Other FHIR tools keep their normal placement.

A current foreground human turn must retain its original context lease. Its
owner must have a captured USER provider configuration, current data-sharing
acknowledgment and an exact operator context-budget entry. Machine, background,
parent and persistent-assignment contexts are unavailable for this pilot. Normal
permissions, PHI/taint, confirmation, governed dispatch and tool auditing remain
in force.

The host performs one fixed JSON completion over the approved isolated HTTP
transport with verified HTTPS and the current operator private-host policy. HTTP
endpoints are unavailable for this pilot. It caps the output at 4096 tokens (or the smaller operator budget),
the encoded request at 256 KiB, the response at 384 KiB, proposal content at
64,000 characters and the supervised request at 60 seconds. It does not retry,
probe parameters, stream, offer tools or fall back to another provider/model.
Uncertain usage remains unknown in the existing durable context ledger.
Cancellation joins the supervised attempt cleanup; no cancelled proposal is
returned.

The 33 regression cases remain developer-authored synthetic data. The owner
accepted their unchanged case array on 2026-10-28 and removed the clinician
approval prerequisite. That acceptance is recorded in fixture metadata; it is
not independent clinical validation or a claim about live provider quality.
Configured-provider, IAM/LETS, affected-client and release qualification remain
separate from the deterministic regression suite.
