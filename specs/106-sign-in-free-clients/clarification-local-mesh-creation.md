# Local mesh creation implementation scope

Date:2026-11-15. Status:authorized implementation in progress. This implements the approved local mesh-key identity design for the owner's explicit request to create a new mesh on the iPhone without Keycloak and to remove identity attestations. The owner has asked for execution without further design questions. The original approved design at199385e35845ed1b83fd7f1061ce1c407a0b233c is adopted unchanged alongside this clarification; the remaining Docker, connection-adapter, contact-sharing and PHI-destination projects are not claimed complete.

## Outcome

The first app screen offers Create a Mesh and Join a Mesh directly, including an existing install left at the old unauthenticated login screen. Server sign-in remains an explicit separate action.

Create a Mesh performs real local device approval, generates/signs the issuer-free roster, durably protects its keys, and shows the recovery words. It makes no identity-provider request. Confirming the recovery words opens a functional native mesh. Native Kernel model configuration/chat and normal policy-authorized tools use the verified mesh principal. No fabricated token, attestation or anonymous/global-auth bypass is permitted.

## Chosen approach

Use roster v2 with the mesh id as principal, as already approved inO1/C1/C2. Remove identity attestations, issuers, re-attestation and unattested/verified-person status from the active mesh contract, Kernel API and Apple creation/membership path. Signed device membership, stop/removal, action-bound tickets, expiry/replay, owner isolation, confirmation, PHI and egress controls remain enforced. Existing outside/institution services retain their own authentication; their bearer tokens are not mesh identities.

The rejected shortcuts are a hidden browser step, fabricated ID tokens, declaring an unattested device externally verified, and disabling authorization. They cannot establish the requested local identity safely.

## Interfaces

- Kernel and UniFFI: create_mesh() -> RecoveryKit, with no issuer/token argument; confirm_mesh_recovery() -> ().
- MeshStatus exposes recovery_pending. Identity-attestation fields and methods are removed, rather than populated with synthetic values.
- MeshPrincipal is derived from a validated roster and current device/feed, with owner_id equal to mesh_id; no issuer/subject pair is manufactured.
- Apple MeshKernelAPI mirrors createMesh() -> MeshRecoveryKit and confirmMeshRecovery(). MeshRuntime.createMesh() calls the local operation directly; the enrollment closure is absent from this path.
- Creation is serialized and recoverable. An interrupted successful creation can reveal the same encrypted pending recovery kit after fresh local approval; confirmation removes that pending export. Failure cannot leave a falsely enrolled runtime or silently lose the only recovery words.

## Data and scope safeguards

The prior design assumed105 was not deployed. It now has real user stores. Existing mesh directories, Keychain keys, restore markers and credentials must not be deleted, implicitly rewritten, or assigned to another principal. The new active roster/store namespace is explicitly v2; legacy data is preserved and unsupported-format errors never trigger reset. No upgrade reader is required for this milestone. The Apple marker/namespace selection must avoid treating preserved legacy data as fresh v2 membership.

A new personal mesh receives one explicit policy allowance, not a fake attested flag. PHI remains fail closed; this work does not silently approve outside PHI destinations. Watch/push paths must not treat empty former issuer fields as proof; unsupported accountless bindings remain unavailable with accurate UI until implemented. Deep/Vessel personal node ownership must validate the real mesh principal where those paths consume the revised Kernel; ordinary external server authentication stays isolated.

## Acceptance and order

1. Contract vectors and Kernel tests cover issuer-free creation, signing domains, invalid/mixed versions, membership, owner consent denial, duplicate calls, persistence/activation failures, encrypted pending recovery and reopen/confirmation.
2. Native tests prove creation never invokes web authentication, recovery remains mandatory, and chat/model settings reach the normal Kernel with no identity provider. There is no sign-in-to-verify label for mesh-key membership.
3. The owner subsequently disconnected the phone and requested a new TestFlight build for their retest. Upload after code/native qualification without waiting for a connected-device run. The owner completes Face ID and writes/confirms recovery words privately when testing; no agent reads/types those words or signs into services.
4. Qualify current Kernel bindings/artifacts, Deep contracts/coverage and Apple tests. A changed Kernel cannot reuse the oldED5 framework; produce new source-bound artifacts through normal protected workflows.
5. Upload the corrected iPhone build, update the reviewer instructions to the actual accountless flow and resubmit. Build98 is withdrawn after its real continuation crash; prior green/code-completion records remain historical evidence.
