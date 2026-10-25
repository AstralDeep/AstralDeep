# Structured document reader profile

`structured-reader-profile.json` is the host-selected profile for the opt-in
PDF/DOCX structure adapter. It is disabled by default and is selected only when
`FF_STRUCTURED_DOCUMENT_PARSER` is enabled and a caller explicitly requests the
structured reader.

The profile declares the exact worker dependency closure, process containment,
resource and concurrency ceilings, and disabled enrichment features. There are no model, OCR,
native-library, or downloadable assets: the adapter reads only the bounded bytes
of an owner-scoped attachment and returns text, table rows/cells, block order,
and source page/region or DOCX element identifiers.

The worker runs as a non-root Linux child through `ProcessSupervisor`, with a
scrubbed environment, read-only copies of its worker profile/script, disposable
scratch, resource limits, and a network guard. Missing or degraded profile,
dependency, worker, or containment state fails closed; it never falls back to an
unsandboxed or provider-backed parser.

License identifications in the profile are informational. The owning manifests
are `backend/requirements.txt`, `backend/requirements-document-worker.txt`, and
the corresponding package metadata.