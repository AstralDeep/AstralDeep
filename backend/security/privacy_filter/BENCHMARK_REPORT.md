# Contextual Privacy Filter Benchmark Report (Issue #318)

## Executive Summary
- **Recommendation**: ✅ **GO (RECOMMENDED FOR OPT-IN ADOPTION)**
- **Baseline Recall**: 33.3%
- **Union-of-Detections Recall**: 100.0%
- **Recall Boost**: +200.0%
- **Clinical Meaning Loss**: 0.0% (Negative controls preserved)

## Detection Breakdown
- **Total Positive Test Cases**: 9
- **Negative Clinical Controls**: 5
- **Detected Entity Categories**:
  - `NAME`: 6 detections
  - `ADDRESS`: 3 detections
  - `MRN`: 1 detections
  - `DATE`: 1 detections

## Performance & Latency Profile
- **Average Latency**: 0.077 ms
- **P50 Latency**: 0.06 ms
- **P95 Latency**: 0.226 ms
- **Throughput**: 1096.9 KB/sec

## Hardware & Environment Disclosure
- **Operating System**: Windows 11 (AMD64)
- **Processor**: Intel64 Family 6 Model 151 Stepping 5, GenuineIntel
- **Python Version**: 3.13.12
- **Execution Mode**: 100% Offline, Deterministic, Zero Network Egress

## Safety & Redaction Integrity
- **Safe Serialization Verified**: `SafeRedactionResult.to_dict()` excludes all original unredacted text and span texts.
- **Audit Trail Hygiene**: Telemetry logs record only category counts and aggregate metrics.
- **Fail-Closed Protection**: Corrupt inputs, offset errors, and exceptions safely trigger fail-closed redaction marker.