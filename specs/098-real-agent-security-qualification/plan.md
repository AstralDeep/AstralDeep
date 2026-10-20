# Implementation Plan: Real Agent Security Qualification

**Branch**: `codex/098-real-agent-security-qualification` | **Date**: 2026-10-20 | **Spec**: [spec.md](spec.md)

## Summary

Add an offline qualification path that drives signed synthetic human turns through the real orchestrator, registered tool dispatch, effect gates, and audit chain. Capture attempted calls and independently observed fixture effects; score missing evidence as indeterminate. Keep synthetic ablations explicitly diagnostic. Fail visibly when a requested external corpus cannot be loaded or mapped. No native-client code or protocol change is planned.

## Technical Context

**Language/Version**: Python 3.11 production target.

**Primary Dependencies**: Existing orchestrator, AstralPlane/PostgreSQL, signed fixture identity, pytest; `agentdojo` remains an evaluation-only optional dependency.

**Storage**: Isolated synthetic owners in the pinned Plane, plus gitignored per-run reports. No schema change.

**Testing**: `backend/security_benchmark/tests/`, targeted `backend/tests/`, grouped backend web suite, changed-line coverage, Ruff. Required cases use a local scripted proposer and real dispatch, with no third-party network or model sampling.

**Target Platform**: Python backend and web-facing orchestrator on Windows development and Linux CI. Apple and Android tests are omitted at the user's request.

**Project Type**: Evaluation-only harness and backend test gate.

**Performance Goals**: Required qualification finishes within the existing 30-minute hosted suite budget; no wall-clock assertion in a shared runner.

**Constraints**: No fake gate verdicts, fail-closed missing corpus/effect/audit evidence, no real user data, no retained attack material, no provider credentials. Existing product authn/authz and audit code stay authoritative.

**Scale/Scope**: At least 12 attack cases and 12 paired benign controls across three source surfaces, with visible unsupported mappings and a separate adaptive study.

## Constitution Check

| Rule | Design response |
|---|---|
| Security core and owner isolation | Signed fixture human identity and admitted turns enter `handle_chat_message`; tool calls traverse `execute_single_tool` and the real gate stack. A fixture agent records physical effects independently. |
| PostgreSQL ownership | Use the pinned Plane facade and existing harness cleanup; no SQL or new migration in Deep. Teardown failure withholds a pass. |
| Source documentation | New Python files get a header of at most three sentences and no narrative comments or docstrings. |
| CI and coverage | Required subset is offline, deterministic, and in the backend module group with the 30-minute cap. Changed Python lines require 90% coverage. Ruff targets `py311`. |
| Release evidence | Local reports bind candidate SHA, component pins, corpus digest, case version, policy posture, and audit/effect receipts. Protected CI independently checks the exact candidate. |
| Client parity | No new UI frame, component, chrome action, or client contract. Web behavior is exercised; Apple/Android behavior is not claimed verified. |
| Dependencies | `agentdojo` stays in the evaluation manifest. No provider or repository token broker is introduced. |

No constitution violation is required. Skipping native tests limits evidence claims and does not permit shared client behavior to change.

## Phase 0: Research Decisions

See [research.md](research.md). Reuse signed fixture admission, loopback agent registration, and Plane cleanup from `backend/verification/drivers/in_process.py` without its development-only delegation override. Preserve the old synthetic runner as a separately labeled diagnostic until the real path has its own effect oracle and corpus manifest. Pin the external corpus package and source digest; reject unknown mappings rather than silently using samples.

## Phase 1: Design

See [data-model.md](data-model.md), [contracts/qualification-report.md](contracts/qualification-report.md), and [quickstart.md](quickstart.md). The driver produces a case contract, an observed dispatch/effect/audit trace, and an adjudication. The report includes exact denominators, exclusions, benign utility, and incomplete states. Adaptive model studies are separate research artifacts and do not satisfy the deterministic CI gate.

## Project Structure

```text
backend/security_benchmark/
  adapters/                 # pinned corpus loading and explicit mapping
  drivers/                  # synthetic diagnostics and the real path
  qualification.py          # case contract, observation, adjudication
  report.py                 # exact-candidate report with distinct evidence labels
  tests/                    # deterministic and real-dispatch tests
backend/tests/helpers/      # signed human and Plane fixture helpers
scripts/run_backend_web_tests.py
specs/098-real-agent-security-qualification/
  research.md
  data-model.md
  contracts/qualification-report.md
  quickstart.md
  tasks.md
```

**Structure Decision**: Keep evaluation code isolated under the existing benchmark package. Production orchestrator and clients do not import it.

## Qualification and Failure Modes

1. Verify candidate, composition, corpus identity, mappings, and isolated owner setup before executing cases.
2. Freeze legitimate intent, attacker surface, objective, effect oracle, and expected permitted outcome. Deliver lower-trust text through a registered fixture read, then propose a tool call through ordinary chat.
3. Record proposal, real authorization/audit decisions, and fixture effect state separately. An attempted call without a complete trace or observable state is indeterminate.
4. Run the paired benign control, verify no cross-owner data or leaked fixture material, and tear down the namespace. Teardown errors invalidate the run.
5. Fail the gate on unauthorized effects, missing trace, empty supported corpus, mismatched digest, or incomplete teardown. Keep model-driven adaptive studies opt-in and non-gating until variability is reported.

## Post-Design Constitution Check

The design introduces no new product dependency, schema, protocol, or client contract. It keeps required qualification offline and within the hosted suite budget. Candidate-bound source/effect/audit evidence cannot itself authorize release.
