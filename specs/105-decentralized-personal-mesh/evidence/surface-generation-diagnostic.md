# Surface generation diagnostic

`scripts/verification/measure_surface_generation.py` sends five fixed synthetic prompts to
the explicitly selected provider, validates each returned primitive tree through
`astralprims.validate_generated`, and records the numerator, denominator, model, schema digest
and response hashes. Transport failures, malformed JSON, duplicate fields, non-finite JSON
numbers, raw links and unresolved bindings stay in the denominator. Raw responses, provider
errors and credentials are never written to the report.

Run with Python 3.11 and the feature's reviewed Primitives 0.13.0 wheel or source installed,
plus Deep's existing `requests`, `httpx` and `python-dotenv` dependencies:

```text
python scripts/verification/measure_surface_generation.py --env-file /path/to/authorized/.env --report /tmp/surface-rate.json --samples 5
```

The environment file must explicitly contain `OPENAI_BASE_URL`, `LLM_MODEL` and
`OPENAI_API_KEY`. This diagnostic does not change production credential resolution.
Requests use `backend/shared/external_http.py`, refuse redirects and have bounded time and
response size. Reports are written with owner-only permissions.

This measures literal primitive trees with JSON-object decoding. It does not measure the
kernel's complete `show_surface` dispatch path, imported A2UI, device rendering or an on-device
model. Criterion 6.2 requires those separate live results before it can be called qualified.
