# Data model

## RetentionGrant

Exact nonempty owner_id/conversation_id/audience_id/source_agent/source_tool, aware expires_at and source/conversation deadlines. Unique host-policy match; identity fingerprint rechecked on every read. Revocation/configuration failure invalidates immediately. Packing disable alone does not revoke retained source.

## Observation

Opaque distinct reference; owner/conversation/audience, source operation/agent/tool, safe gate arguments in memory only, order, actual outcome, permitted UTF-8 text, SHA-256, grant identity, capture time and absolute expiry. <=8 MiB/text and <=64 MiB/conversation plus stricter global bytes/record count. No eviction of live evidence. Empty is valid; unsupported/sensitive/unretained content leaves authorized view intact. Content removed on expiry/deletion/revocation.

## RecallPage

Identity/digest, byte start/end/total, exact Unicode text, next_offset or explicit end, partial/full state. <=16 KiB UTF-8. Offset is a nonnegative integer at a UTF-8 boundary within extent. End-of-source is an empty terminal page. Authorized concatenation equals the permitted capture.

## CompactionResult

Source fingerprint, protected-state identity, proposed messages, status unchanged/accepted/rejected/context_limit, bounded reason and estimates. No mutation before acceptance; candidate reduces and fits budget; instructions/current user/tool groups unchanged. Host records are neither summarized into authority nor overwritten.

## UsageRecord

Owner/conversation, stable physical-attempt UUID and ordered revision, purpose/provider/model/time, nullable input/output/cache usage, charged outcome, nullable dated pricing/currency, recall byte traffic and complete/unknown status. Stable audit identity deduplicates identical records and rejects conflicting delivery. Late cancellation completion reconciles same attempt once. No prompts/source text/endpoints/keys/raw errors.

## EvaluationReport

Frozen candidate/fixture/provider/scoring identities, fixed 12/12 split, mechanism selection, paired task/critical-evidence outcomes, complete known/unknown costs, live/formative availability and promotion verdict. Unknown/failed required criteria prohibit promotion; holdout is not a tuning set.
