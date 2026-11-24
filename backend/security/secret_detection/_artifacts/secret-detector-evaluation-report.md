# Contextual Secret Patterns Evaluation Report

**Evaluation Timestamp:** 2026-11-24T03:37:17Z  
**Corpus Version:** `2026.11.1-synthetic`  
**Corpus Digest:** `f2912deded6384f2054a61ad2cd51ecde4ff94c7186eaad1b51ec1798aa4e3e3`  
**Source Attribution:** `openai-guardrails-python@4e3f3717017cc5575ca7522719f34a340c210029`  
**License:** Adapted under MIT / Apache 2.0 terms. Dependency-free implementation.  

## Comparative Benchmark Results

| Metric | Baseline (`phi_redactor`) | Enhanced (`SecretDetector`) | Delta |
| :--- | :---: | :---: | :---: |
| **True Positives (TP)** | 9 | 14 | +5 |
| **False Negatives (FN)** | 5 | 0 | -5 |
| **True Negatives (TN)** | 14 | 14 | 0 |
| **False Positives (FP)** | 0 | 0 | 0 |
| **Recall** | 64.3% | 100.0% | **+35.7%** |
| **Precision** | 100.0% | 100.0% | 0.0% |
| **Clinical Meaning Loss** | 0.0% | 0.0% | **0.0% (Clean)** |
| **Execution Latency** | 5.90 ms | 16.78 ms | - |

## Qualified Adoption Recommendation

> **ADOPT: Enhanced detector achieves higher recall with 0% clinical meaning loss.**

### Key Findings
1. **Expanded Provider Coverage:** The enhanced patterns successfully detect modern OpenAI Project (`sk-proj-`), Service Account (`sk-svcacct-`), Anthropic (`sk-ant-`), Google AI (`AIzaSy`), Slack, and embedded database URI passwords that evade baseline regexes.
2. **Zero Clinical Meaning Loss:** Synthetic negatives (including medical hormonal secretin/secretion references, DNA primer sequences, ICD-10/LOINC codes, and token accounting fields) demonstrated 100% pass-through without any false-positive redaction.
3. **Safe Egress Streaming:** Streamed chunk buffers maintain an overlap window to capture boundary-spanning credentials without memory leaks or plaintext exposure.
