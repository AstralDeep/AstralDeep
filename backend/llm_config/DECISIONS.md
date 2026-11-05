# Decisions providers

LLM settings offers an optional decisions model independently of the normal chat
provider. The shared server-owned form serves web and native clients, including
macOS, through existing select and conditional-field contracts.

TypeSafe AI requires the user's key and uses the fixed hosted endpoint and
`jev-latest` model. Custom accepts a base URL, model name, and optional API key
for a System One-compatible server. A base URL ending in `/v1` receives
`/systemone`; other base URLs receive `/v1/systemone`. LLM Factory uses
`https://api-llm-factory.ai.uky.edu/v1` and
`bespokelabs/Bespoke-Nimble-9B`, and requires its own API key.

Saves require the existing data-sharing acknowledgment and a successful probe.
Custom probes exercise System One inference rather than only the model catalog.
Blank keys reuse a saved key only for the same provider and endpoint. To switch
to a keyless endpoint after saving a key, remove the decisions configuration
first. Failed probes preserve the working configuration.

All custom requests use the approved HTTP egress path, including TLS, target,
redirect, and size controls. Operator private-host policy still applies. Routing
and persistent-agent screening use the selected provider with the existing
bounded retries, circuit breaker, authorization, and fallback behavior.

Custom configurations are encrypted inside the existing opaque credential
column with a `decisions:v1:` ciphertext prefix. No database migration,
component pin, feature flag, or protocol vocabulary change is required. Legacy
TypeSafe records remain readable. An older server rejects and discards the
versioned custom row rather than sending its key to TypeSafe; after rollback,
users must configure that optional decisions provider again.
