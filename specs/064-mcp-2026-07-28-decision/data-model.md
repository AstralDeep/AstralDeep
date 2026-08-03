# Data model: MCP admission class

Feature 064 does not persist MCP clients, bearer tokens, discovery documents,
subscriptions, advertisements, or protocol task state. It adds one bounded
workload class to feature 060's existing durable admission graph.

## Additive rows and constraint

`operation_admission_class` gains the permitted class name `mcp` and one row:

| Field | Value |
|---|---|
| `class_name` | `mcp` |
| `parent_class_name` | `global` |
| `active_limit` | `8` |
| `queue_limit` | `32` |
| `max_wait_ms` | `5000` |
| `config_revision` | `064-defaults` |

`operation_admission_slot` gains slots 1 through 8 for that class. Ordinary
MCP requests create existing `operation_record` rows with
`operation_kind=mcp_request`; the standard retention sweep removes terminal
records. No new table or column is introduced.

## Migration and repeat safety

`Database._migrate_mcp_admission_064()` runs inside the existing guarded
startup transaction after the feature-060 coordination schema exists. It
replaces the admission-class name check with the seven-value vocabulary and
uses `ON CONFLICT DO NOTHING` for the class and slots. Running it repeatedly
does not rewrite configured rows, allocate extra slots, or alter operation
history. `SCHEMA_REVISION=064.001` makes existing deployments run the delta.

Verification covers an empty database and a representative database carrying
legacy chats/messages/components and feature-060 operation state. Both must
retain existing truth and expose exactly the configured MCP class/slot set.

## Rollback and recovery

Set `FF_MCP_SERVER=false` and recreate the orchestrator. No route can submit
new MCP work. Do not delete the class or slot rows during rollback: retained
terminal/running operation rows may reference them. The idle class is isolated
from all other child classes and changes no capacity or behavior while the
endpoint is absent. If a deployment was interrupted, let normal lease expiry
and retention settle its operations before considering any later manual data
hygiene under a separately reviewed procedure.
