# Labels and gate `astral.labels/v2`

Version 2 of the label contract adds one input and one rule to 105 [labels.md](../../105-decentralized-personal-mesh/contracts/labels.md). Every effect states whether it reaches an outside service, and PHI may reach an outside service only when its destination is approved by name ([phi-destinations.md](phi-destinations.md)). Everything else in version 1 stands, including the axes, the join, the running label, quarantined readers and the order of every rule and gate.

Existing stores are untouched. Labels are not stored differently and no event changes. The Kernel computes both inputs below for every effect.

Reference code: `reference/labels_v2.py`, which imports the version 1 join and quarantine from the preserved 105 reference. Vectors: `vectors/labels-v2.json`, written by `reference/generate_phi_destination_vectors.py`.

## 1. The effect

The effect is `{"kind", "target", "argument_integrity", "endpoint_phi_approved", "outside_service"}`. Kinds and targets are those of version 1.

| Field | Rule |
|---|---|
| `outside_service` | A boolean. False only for local first-party tools and for peer tools. True for every other `remote_tool`, including a legacy `remote.mcp.*` server, a connection, an A2A agent and a launched connection, so a new kind of host is outside by default |
| `endpoint_phi_approved` | A boolean. True when the effect's destination may receive PHI: an on-device model or a loopback model on a device in the PHI group, or a destination for which `phi_destination_allowed(kind, target)` is true. Never supplied by a host or a request |

An effect without `outside_service` or without `endpoint_phi_approved`, with either value not a boolean, or with `outside_service` true and a target other than `remote_tool` is `labels.invalid`. So every outside service is a remote tool and none can be labelled another way.

Peer tools stay false because the executing peer runs its own PHI check and refuses a ticket with a `phi` label when it is not in the group. Caller tickets never cover connection tools.

## 2. Flow rules

The rules of version 1 section 4 run in their order. One rule is new, after `flow.phi_to_model`.

| Rule | Outcome |
|---|---|
| Target `model`, and a secret value is in the context or `secret_in_context` is set | Refuse, `flow.secret_in_context` |
| Target `model`, running confidentiality is `phi`, and the endpoint is not PHI-approved | Refuse, `flow.phi_to_model` |
| `outside_service` is set, the context holds a `phi` label, and `endpoint_phi_approved` is false | Refuse, `flow.phi_to_service` |
| Target `sync` | Allow here |
| The three consent rules of version 1 | Consent with their reasons, in order |

The new rule reads the context list, not the running label. The join puts `secret` above `phi`, so a context of `phi` and `secret` joins to `secret`, and a rule on the joined label would let that PHI through. A kernel that keeps only the joined running label also keeps whether a `phi` label was joined. A context holding only `secret` keeps its version 1 outcome.

The rule refuses whatever the kind of the call, a read included, because a call sends its arguments to the service. A refusal comes before any consent reason.

## 3. Gate order

The nine steps of version 1 section 5 stand. Step 5 refuses with `flow.secret_in_context`, `flow.phi_to_model` or `flow.phi_to_service`.

## 4. Server hints

Connection records of `astral.connection/v1` carry no server confidentiality hint, so the flow takes no hint input in this version. A later hint could only raise a connection's class, never lower it, and the label of a `phi` connection's output stays `phi`.

## 5. Vectors

`vectors/labels-v2.json` holds every case of 105 `vectors/labels.json` under its name with the prefix `v1-`, unchanged except that each effect states `outside_service` false, and its outcome is the same. The new cases are:

| Case | Shows |
|---|---|
| `flow-phi-to-an-unapproved-outside-service` | The refusal |
| `flow-phi-to-an-approved-outside-service` | An approved destination is allowed |
| `flow-phi-and-secret-to-an-unapproved-outside-service` | The rule reads the context list |
| `flow-secret-only-to-an-outside-service-keeps-its-v1-outcome` | A secret context alone is unchanged |
| `flow-private-to-an-unapproved-outside-service` | Content that is not PHI flows |
| `flow-phi-to-a-peer-tool` | A peer tool is unaffected |
| `flow-phi-to-a-local-tool` | A local tool is unaffected |
| `flow-phi-read-from-an-unapproved-outside-service` | A read is refused too |
| `flow-service-refusal-comes-before-consent` | Refusal before consent |
| `flow-approved-service-still-asks-for-consent` | Approval does not skip the consent rules |
| `flow-outside-service-on-a-model-target`, `flow-outside-service-on-a-local-tool`, `flow-outside-service-on-sync` | An outside service is a remote tool |
| `flow-outside-service-missing`, `flow-outside-service-not-a-boolean` | The field is required |
| `flow-endpoint-approved-missing`, `flow-endpoint-approved-not-a-boolean` | That field is required and strict too |
| `flow-phi-to-model-is-unchanged-for-services` | The model rule stands |
| `gate-5-phi-to-service`, `gate-5-approved-service-dispatches`, `order-policy-before-service-flow` | The gate order |

Verify with `python specs/106-sign-in-free-clients/contracts/reference/generate_phi_destination_vectors.py --check`.

## 6. Versioning

A change to a rule, a field or a code raises the format version. This document, the vectors and the reference change together.
