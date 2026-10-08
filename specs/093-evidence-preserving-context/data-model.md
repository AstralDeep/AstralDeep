# Data model

## RetentionGrant

Exact nonempty owner_id/conversation_id/audience_id/source_agent/source_tool, aware expires_at and source/conversation deadlines. Unique host-policy match; identity fingerprint rechecked on every read. Revocation/configuration failure invalidates immediately. Packing disable alone does not revoke retained source.

## Observation

Opaque distinct reference; owner/conversation/audience, source operation/agent/tool, safe gate arguments in memory only, order, actual outcome, permitted UTF-8 text, SHA-256, grant identity, capture time and absolute expiry. <=8 MiB/text and <=64 MiB/conversation plus stricter global bytes/record count. No eviction of live evidence. Empty is valid; unsupported/sensitive/unretained content leaves authorized view intact. Content removed on expiry/deletion/revocation.

## RecallPage

Identity/digest, byte start/end/total, exact Unicode text, next_offset or explicit end, partial/full state. <=16 KiB UTF-8. Offset is a nonnegative integer at a UTF-8 boundary within extent. End-of-source is an empty terminal page. Authorized concatenation equals the permitted capture.

## CompactionResult

Source fingerprint, protected-state identity, proposed messages, status unchanged/accepted/rejected/context_limit, bounded reason and estimates. No mutation before acceptance; candidate reduces and fits budget; instructions/current user/tool groups unchanged. Host records are neither summarized into authority nor overwritten.

## ContextView

Opaque `view_` reference; exact owner/conversation/audience, immutable integral generated text, SHA-256, source dependencies (reference, integrity identity, grant fingerprint, absolute expiry), capture time and fixed expiry. Lifetime is at most ten minutes and every earlier source deadline. The host cap is 16 KiB/view, 1 MiB/64 views per conversation and 16 MiB/256 views globally; no live-record eviction. Source deletion/revocation/expiry, privacy refusal, corruption, unavailable adapter or lost original authority prevents inspection; cleanup/restart cannot recover text. Each read rechecks all dependencies through ordinary source authorization and the current host read adapter.

## EvidenceInspection

Fresh typed read-only human metadata operation, canonical request generation, original issued session, exact current connection and active owner-owned conversation. A private navigation token and unchanged ContextAuthority bind the live operation/fence. Source/preview/summary/usage arguments carry only their bounded read references or offsets. Delivery proof binds the exact result/UI, host adapter, original source/grant/privacy state and current lease; the final live-operation check precedes synchronous proof reassertion and actual frame send. Client navigation, close, send, timeout, account/registration/connection changes retire the request and temporary content.

## DurableEvidenceMetadata

Canonical assistant component groups are strictly reconstructed from code-owned fixed labels, opaque source/view references, digests, byte positions, outcomes and known/unknown usage. Arbitrary extra text/actions/fields are rejected by the transcript rail recognizer. Source/preview/generated text is absent from durable conversation/workspace/completion-summary data; a temporary modal releases it only after a new authenticated inspection. Watch groups contain an explicit phone/desktop handoff and no unsupported modal action or source text.

## UsageRecord

Owner/conversation, stable physical-attempt UUID and ordered revision, purpose/provider/model/time, nullable input/output/cache usage, charged outcome, nullable dated pricing/currency, recall byte traffic and complete/unknown status. Stable audit identity deduplicates identical records and rejects conflicting delivery. Late cancellation completion reconciles same attempt once. No prompts/source text/endpoints/keys/raw errors.

## EvaluationReport

Frozen candidate/fixture/provider/scoring identities, fixed 12/12 split, mechanism selection, paired task/critical-evidence outcomes, complete known/unknown costs, live/formative availability and promotion verdict. Unknown/failed required criteria prohibit promotion; holdout is not a tuning set.
