# Local mesh creation implementation plan

> For agentic workers:use subagent-driven implementation with independent review. Existing user authorization covers execution, local tests, pushes, merges and release of this app fix; do not reopen settled design questions.

**Goal:** Create and use a new personal mesh on the iPhone through the requested TestFlight replacement with device keys and local approval, without identity attestations or Keycloak.

**Architecture:** The Kernel owns issuer-free roster v2, its verified mesh principal and recoverable local creation. Apple calls the real Kernel and retains recovery protection. Existing user stores remain preserved, and outside-service authentication is not used as core mesh identity.

**Tech stack:** Rust/UniFFI, Swift/SwiftUI, existing Python3.11 node/reference tooling.

**Spec:** clarification-local-mesh-creation.md and approved spec.mdO1/C1/C2.

## Global constraints

- Every105gate stays on. No fake tokens, unsigned membership, model-selected auth, credential logging, global auth bypass or silent stubs.
- Owner isolation, stop/removal, tool/PHI/consent/egress/replay/audit controls stay enforced.
- Preserve existing user data and the unrelated DeepAGENTS.md edit; no destructive migration.
- Source files have at most a three-sentence header and no feature/task/history narration.
- Required tests are deterministic, use no live third-party services, and retain existing job budgets. Changed Python coverage is at least90%.
- Physical-device authentication and recovery-secret entry are performed by the owner.

## Review focus

- Duplicate or interrupted creation must not create two meshes or lose the recovery key.
- A legacy store or malformed/mixed-version roster must never be reset or trusted as v2.
- Local mesh membership must not become institution identity or authorize a different mesh.
- A stopped/removed device or expired/replayed/wrong-action ticket remains denied.
- The physical flow must show recovery without creating any system browser authentication session.

## Task 1: Kernel and authoritative roster contract

Files:Kernel crates/astral-mesh/src/{entry,writer,roster,recovery,pairing}.rs; crates/astral-kernel/src/{mesh_ops,runtime,turns_runtime}.rs; crates/astral-kernel-ffi/src/{lib,records}.rs; relevant module tests, Python binding smoke; Deep specs/106-sign-in-free-clients/contracts/ and vectors as needed. The Kernel implementer owns these contract paths and records their exact manifest/digests.

Produces:create_mesh()->RecoveryKit; confirm_mesh_recovery()->(); status.recovery_pending; a validated mesh-id principal without issuer identities. Roster envelopes/signing context have an explicit new version; identity-attestation entry families are absent.

- [ ] Write failing creation/contract tests with no configured issuer or network; verify missing API/current issuer requirement fails.
- [ ] Implement v2 parsing/signing and remove active identity attestation APIs/fields. Reject mixed/unsupported/invalid signatures without mutation.
- [ ] Implement presence-protected serialized creation, durable encrypted pending recovery, retry/reopen and confirmation. Test denial/no mutation and failures before/after persistence/activation.
- [ ] Update allowance mapping to explicit single personal allowance; retain PHI denial and all ticket/stop/removal/replay gates.
- [ ] Verify workspace tests, fmt/clippy, current generated Python binding and contract vectors; create new source-bound Apple artifacts after the exact source is committed.

## Task 2: Apple accountless creation and preserved custody

Files:Projection apple-clients/AstralApp/AstralApp/Mesh/{MeshKernelAPI,RealMeshKernel,InMemoryMeshKernel,MeshRuntime,MeshAppMode,KernelMapping,MeshOnboardingView,MeshPresentation,MeshSecretStore}.swift plus exact relevant tests. Additional views are touched only where removed fields/actions require it.

Consumes:Task1API/records. Produces:CreateMesh→device approval→recovery→confirmed mesh, with no enrollment call/browser; truthful device-key roster display; explicit v2 store/marker namespace preserving old state.

- [ ] Add failing first-launch Create/Join routing, no-web-auth, recovery confirmation/failure/resume and legacy-data-preservation tests.
- [ ] Wire the direct Kernel creation and confirmation APIs; remove attestation sign-in UI/dependencies from active mesh creation/membership.
- [ ] Preserve original storage/keys; select v2 namespace explicitly. Remove obsolete identity labels; fail closed for unsupported Watch/push binding.
- [ ] Exercise native Kernel model configuration/chat authority without an IdP, retaining consent/PHI/egress denials.
- [ ] Run focused tests then affected iPhone/Mac schemes with current generated bindings. The owner disconnected the phone and will check FaceID→recovery privately through the new TestFlight build; do not wait for physical installation.

## Task 3: Personal node consumers and qualification

Files:Deep backend/orchestrator/{node_mesh,mesh_admission,mesh_push_client}.py and backend/shared/node_identity.py only where revised principal use requires them; Vessel personal owner/pairing code if necessary; corresponding real-dispatch tests. Global legacy outside-service auth is not weakened.

- [ ] Identify native-vs-server paths. Preserve direct native Kernel chat/model configuration and do not claim legacy server endpoints are accountless.
- [ ] Bind fresh personal-node ownership through production startup/pairing to verified mesh_id/device/ticket evidence; preserve/refuse legacy owner bindings with explicit status; remove attested-pair assumptions. Test forged, other-mesh, wrong-action, revoked/stopped/expired/replayed proofs and cross-owner isolation.
- [ ] Keep normal institution/outside-service authentication isolated; no synthetic issuer/subject/role claims.
- [ ] Qualify touched Python suites/coverage and update exact component declarations/manifests. Do not widen the old framework reuse exception for a changed Kernel.

## Task 4: Release and closure

- [ ] Integrate only reviewed task changes; qualify exact Kernel/Deep/Projection sources with normal CI and real source-bound native artifacts.
- [ ] Upload after current source-bound code/native qualification; the owner will verify creation using TestFlight. Retain evidence without credentials or recovery words.
- [ ] Upload a monotonic iPhone build with embedded Watch using normal publisher approval and exact IPA/signature/profile checks.
- [ ] Update listing/reviewer steps for accountless mesh creation, attach the actual processed build and submit; record the actual Store state.
- [ ] Update curated vault/index/log at each major checkpoint, keep unrelated PRs/user edits intact and clean only confirmed task-owned temporary outputs after retaining evidence.
